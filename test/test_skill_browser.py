"""Tests for the skill directory browser API.

Covers:
- ``list_kiro_skills`` discovery of ``~/.kiro/skills/`` and workspace ``.kiro/skills/``
- ``_resolve_loaded_by_agents`` glob-matching against installed agent JSONs
- ``list_skill_tree`` / ``read_skill_file`` size + sensitive-path + escape guards
- ``_resolve_skill_root`` cross-source resolution (kiroclaw / kiro-user / aim)
- ``GET /api/skills/<name>/tree`` and ``GET /api/skills/<name>/file`` end-to-end

Tests use a tmp_path fake $HOME so we never touch the real filesystem.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_claw.dashboard.handlers._shared import (
    SKILL_FILE_MAX_BYTES,
    SKILL_TREE_MAX_ENTRIES,
    _agent_loads_skill,
    _expand_resource_uri,
    _parse_skill_description,
    _resolve_loaded_by_agents,
    _resolve_skill_root,
    list_kiro_skills,
    list_skill_tree,
    read_skill_file,
)

# ── Fixtures ──


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """Pin $HOME to tmp_path so Path.home() returns a writable sandbox.

    Also clears KIROCLAW_HOME so ``skills_dir()`` resolves to
    ``<tmp>/.kiroclaw/skills`` rather than any value leaked from the
    surrounding build environment.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("KIROCLAW_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return tmp_path


def _write_skill(root: Path, name: str, *, description: str = "", body: str = "body") -> Path:
    """Materialize a SKILL.md under root/<name>/SKILL.md."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm = ["---", f"name: {name}"]
    if description:
        fm.append(f"description: {description}")
    fm.append("---")
    skill_dir.joinpath("SKILL.md").write_text("\n".join(fm) + f"\n{body}\n")
    return skill_dir


# ── _parse_skill_description ──


class TestParseSkillDescription:
    def test_extracts_description_and_always(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("---\nname: x\ndescription: hello world\nalways: true\n---\nbody\n")
        desc, always = _parse_skill_description(f)
        assert desc == "hello world"
        assert always is True

    def test_no_frontmatter_returns_empty(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text("# No frontmatter here\n")
        assert _parse_skill_description(f) == ("", False)

    def test_truncated_frontmatter_returns_empty(self, tmp_path):
        f = tmp_path / "SKILL.md"
        # Open frontmatter, never closed.
        f.write_text("---\nname: x\n" + ("padding\n" * 100))
        # Cap on read means we may see partial frontmatter; either way the
        # closing ``---`` is missing so parser returns empty.
        desc, always = _parse_skill_description(f)
        assert desc == ""
        assert always is False

    def test_strips_quotes_from_description(self, tmp_path):
        f = tmp_path / "SKILL.md"
        f.write_text('---\ndescription: "quoted desc"\n---\n')
        desc, _ = _parse_skill_description(f)
        assert desc == "quoted desc"

    def test_symlink_to_sensitive_file_returns_empty(self, fake_home):
        """Security: a SKILL.md that is a symlink to a sensitive credential
        file must not be read, even though it sits under a trusted skills
        root.  The resolved target is what's gated, not the link location."""
        creds = fake_home / ".aws" / "credentials"
        creds.parent.mkdir(parents=True)
        creds.write_text("---\ndescription: SECRET\n---\n")
        skill_dir = fake_home / ".kiro" / "skills" / "evil"
        skill_dir.mkdir(parents=True)
        link = skill_dir / "SKILL.md"
        link.symlink_to(creds)
        # The credential content must never surface as a description.
        assert _parse_skill_description(link) == ("", False)


# ── list_kiro_skills ──


class TestListKiroSkills:
    def test_lists_global_kiro_skills(self, fake_home):
        kiro = fake_home / ".kiro" / "skills"
        _write_skill(kiro, "alpha", description="alpha desc")
        _write_skill(kiro, "beta", description="beta desc")
        out = list_kiro_skills(project_dir=None)
        names = [s["name"] for s in out]
        assert "alpha" in names and "beta" in names
        for s in out:
            assert s["source"] == "kiro-user"
            assert s["key"].startswith("kiro-user/")

    def test_lists_workspace_skills_too(self, fake_home, tmp_path):
        proj = tmp_path / "proj"
        ws = proj / ".kiro" / "skills"
        _write_skill(ws, "ws-skill", description="workspace one")
        out = list_kiro_skills(project_dir=proj)
        keys = [s["key"] for s in out]
        assert "kiro-workspace/ws-skill" in keys

    def test_skips_directories_without_skill_md(self, fake_home):
        kiro = fake_home / ".kiro" / "skills"
        kiro.mkdir(parents=True)
        (kiro / "dangling").mkdir()  # no SKILL.md inside
        assert list_kiro_skills(None) == []

    def test_skips_dotfile_dirs(self, fake_home):
        kiro = fake_home / ".kiro" / "skills"
        _write_skill(kiro, ".hidden", description="should skip")
        out = list_kiro_skills(None)
        assert all(s["name"] != ".hidden" for s in out)

    def test_returns_empty_when_no_kiro_dir(self, fake_home):
        # ~/.kiro/skills/ does not exist at all
        assert list_kiro_skills(None) == []


# ── _expand_resource_uri / _agent_loads_skill / _resolve_loaded_by_agents ──


class TestResourceUriExpansion:
    def test_skill_uri_with_tilde_expands_to_home(self, fake_home, tmp_path):
        agent_path = tmp_path / "agent.json"
        out = _expand_resource_uri("skill://~/.kiro/skills/*/SKILL.md", agent_path)
        assert out == str(fake_home / ".kiro" / "skills" / "*" / "SKILL.md")

    def test_non_skill_uri_returns_none(self, tmp_path):
        agent_path = tmp_path / "agent.json"
        assert _expand_resource_uri("file://foo", agent_path) is None
        assert _expand_resource_uri("other://x", agent_path) is None

    def test_workspace_relative_resolves_against_project_root(self, tmp_path):
        # ``<project>/.kiro/agents/foo.json`` — a workspace-relative
        # ``.kiro/skills/...`` URI must resolve to ``<project>/.kiro/skills``
        # NOT ``<project>/.kiro/.kiro/skills`` (the doubled-segment bug).
        proj = tmp_path / "proj"
        agents_dir = proj / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        agent_path = agents_dir / "foo.json"
        out = _expand_resource_uri("skill://.kiro/skills/*/SKILL.md", agent_path)
        expected = str(proj / ".kiro" / "skills" / "*" / "SKILL.md")
        assert out == expected
        assert ".kiro/.kiro" not in out

    def test_absolute_skill_uri_passthrough(self, tmp_path):
        agent_path = tmp_path / "agent.json"
        assert _expand_resource_uri("skill:///abs/path", agent_path) == "/abs/path"


class TestAgentLoadsSkill:
    def test_glob_matches_skill_md(self, fake_home, tmp_path):
        skill_md = fake_home / ".kiro" / "skills" / "linear" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\n---\n")
        agent_json = {
            "name": "my-agent",
            "resources": ["skill://~/.kiro/skills/*/SKILL.md"],
        }
        assert _agent_loads_skill(agent_json, tmp_path / "a.json", skill_md) is True

    def test_no_match_when_glob_excludes(self, fake_home, tmp_path):
        skill_md = fake_home / ".kiro" / "skills" / "linear" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\n---\n")
        agent_json = {
            "name": "my-agent",
            "resources": ["skill://~/.kiro/skills/specific-only/SKILL.md"],
        }
        assert _agent_loads_skill(agent_json, tmp_path / "a.json", skill_md) is False

    def test_handles_non_string_resources(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        agent_json = {"name": "x", "resources": [{"oops": "object"}, None, "skill://*/SKILL.md"]}
        # Should not crash on garbage; should still match the valid glob
        # (matches anything ending in /SKILL.md so depending on path).
        skill_md.write_text("")
        # Returns True/False — just checks no exception.
        _agent_loads_skill(agent_json, tmp_path / "a.json", skill_md)

    def test_resources_can_be_missing_or_non_list(self, tmp_path):
        skill_md = tmp_path / "SKILL.md"
        skill_md.write_text("")
        assert _agent_loads_skill({}, tmp_path / "a.json", skill_md) is False
        assert _agent_loads_skill({"resources": "not a list"}, tmp_path / "a.json", skill_md) is False


class TestResolveLoadedByAgents:
    def test_finds_agents_that_load_skill(self, fake_home):
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        skill_md = fake_home / ".kiro" / "skills" / "x" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\n---\n")

        (agents_dir / "loader.json").write_text(json.dumps({
            "name": "loader",
            "resources": ["skill://~/.kiro/skills/*/SKILL.md"],
        }))
        (agents_dir / "non-loader.json").write_text(json.dumps({
            "name": "non-loader",
            "resources": ["file://something-else"],
        }))
        out = _resolve_loaded_by_agents(skill_md)
        assert out == ["loader"]

    def test_skips_unparseable_agent_json(self, fake_home):
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        skill_md = fake_home / ".kiro" / "skills" / "x" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\n---\n")

        (agents_dir / "broken.json").write_text("{ this is not json")
        # Does not raise, returns empty.
        assert _resolve_loaded_by_agents(skill_md) == []

    def test_returns_empty_when_no_agents_dir(self, fake_home):
        skill_md = fake_home / "elsewhere.md"
        skill_md.write_text("")
        assert _resolve_loaded_by_agents(skill_md) == []

    def test_skips_symlink_to_sensitive_file(self, fake_home):
        """Security: a ``*.json`` symlink under ~/.kiro/agents/ that points at
        a sensitive credential file must NOT be read.  Otherwise an attacker
        could exfiltrate ~/.aws/credentials via the loaded_by_agents scan."""
        agents_dir = fake_home / ".kiro" / "agents"
        agents_dir.mkdir(parents=True)
        skill_md = fake_home / ".kiro" / "skills" / "x" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text("---\n---\n")

        # Plant a credential file and symlink it in as a fake agent config.
        creds = fake_home / ".aws" / "credentials"
        creds.parent.mkdir(parents=True)
        creds.write_text('{"name": "evil", "resources": ["skill://~/.kiro/skills/*/SKILL.md"]}')
        (agents_dir / "evil.json").symlink_to(creds)

        # Even though the file *would* match, the sensitive-path guard skips
        # it — the credential file is never read and "evil" never returned.
        out = _resolve_loaded_by_agents(skill_md)
        assert "evil" not in out
        assert out == []


# ── list_skill_tree ──


class TestListSkillTree:
    def test_returns_files_and_dirs(self, fake_home):
        skill = fake_home / ".kiroclaw" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\n---\n")
        (skill / "helper.sh").write_text("echo hi\n")
        (skill / "references").mkdir()
        (skill / "references" / "doc.md").write_text("# doc\n")
        out = list_skill_tree(skill)
        kinds = {(e["path"], e["type"]) for e in out}
        assert ("SKILL.md", "file") in kinds
        assert ("helper.sh", "file") in kinds
        assert ("references", "dir") in kinds
        assert ("references/doc.md", "file") in kinds

    def test_caps_at_max_entries(self, fake_home):
        skill = fake_home / ".kiroclaw" / "skills" / "huge"
        skill.mkdir(parents=True)
        for i in range(SKILL_TREE_MAX_ENTRIES + 50):
            (skill / f"f{i:04d}.txt").write_text("x")
        out = list_skill_tree(skill)
        assert len(out) == SKILL_TREE_MAX_ENTRIES

    def test_empty_skill_dir_returns_empty(self, fake_home):
        skill = fake_home / ".kiroclaw" / "skills" / "empty"
        skill.mkdir(parents=True)
        assert list_skill_tree(skill) == []


# ── read_skill_file ──


class TestReadSkillFile:
    def test_reads_file_inside_skill(self, fake_home):
        skill = fake_home / ".kiroclaw" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("hello\n")
        content, err = read_skill_file(skill, "SKILL.md")
        assert err is None
        assert content == "hello\n"

    def test_rejects_path_traversal(self, fake_home):
        skill = fake_home / ".kiroclaw" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("x")
        outside = fake_home / "secret.txt"
        outside.write_text("PASSWORD")
        _, err = read_skill_file(skill, "../../secret.txt")
        assert err == "invalid path"

    def test_rejects_absolute_path(self, fake_home):
        skill = fake_home / ".kiroclaw" / "skills" / "demo"
        skill.mkdir(parents=True)
        _, err = read_skill_file(skill, "/etc/passwd")
        assert err == "invalid path"

    def test_rejects_oversized_file(self, fake_home, monkeypatch):
        skill = fake_home / ".kiroclaw" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "big.txt").write_bytes(b"x" * (SKILL_FILE_MAX_BYTES + 1))
        _, err = read_skill_file(skill, "big.txt")
        assert err and err.startswith("file too large")

    def test_missing_file_returns_not_found(self, fake_home):
        skill = fake_home / ".kiroclaw" / "skills" / "demo"
        skill.mkdir(parents=True)
        _, err = read_skill_file(skill, "no-such.txt")
        assert err == "not found"

    def test_directory_target_rejected(self, fake_home):
        skill = fake_home / ".kiroclaw" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "subdir").mkdir()
        _, err = read_skill_file(skill, "subdir")
        assert err == "not a file"


# ── _resolve_skill_root ──


class TestResolveSkillRoot:
    def test_kiroclaw_skill(self, fake_home):
        skill_dir = _write_skill(fake_home / ".kiroclaw" / "skills", "foo")
        state = MagicMock(_slots={})
        out = _resolve_skill_root("foo", state)
        assert out == skill_dir.resolve()

    def test_kiro_user_prefix(self, fake_home):
        skill_dir = _write_skill(fake_home / ".kiro" / "skills", "bar")
        state = MagicMock(_slots={})
        out = _resolve_skill_root("kiro-user/bar", state)
        assert out == skill_dir.resolve()

    def test_path_traversal_rejected(self, fake_home):
        _write_skill(fake_home / ".kiroclaw" / "skills", "ok")
        state = MagicMock(_slots={})
        assert _resolve_skill_root("../etc", state) is None
        assert _resolve_skill_root("/abs/path", state) is None

    def test_missing_skill_returns_none(self, fake_home):
        state = MagicMock(_slots={})
        assert _resolve_skill_root("does-not-exist", state) is None

    def test_symlinked_kiro_skill_resolves(self, fake_home, tmp_path):
        """AIM ``--local`` installs and similar manual setups symlink
        ``~/.kiro/skills/<name>`` to a directory elsewhere (commonly
        ``~/.agents/skills/<name>``).  Resolver must accept these even
        though the resolved target sits outside the kiro skills root."""
        # Real skill directory off in some other tree.
        target_dir = tmp_path / "agents-tree" / "skills" / "linked"
        target_dir.mkdir(parents=True)
        (target_dir / "SKILL.md").write_text("---\nname: linked\n---\nbody")

        # ``~/.kiro/skills/`` exists with a symlink pointing at the target.
        kiro_skills = fake_home / ".kiro" / "skills"
        kiro_skills.mkdir(parents=True)
        (kiro_skills / "linked").symlink_to(target_dir)

        state = MagicMock(_slots={})
        out = _resolve_skill_root("kiro-user/linked", state)
        assert out == target_dir.resolve()

    def test_nested_kiroclaw_skill_resolves(self, fake_home):
        """Regression: category-keyed skills (``utils/multi-badger``,
        ``code/builder-toolbox``) live one level below the skills root.
        An over-strict symlink guard that required the candidate's parent
        to *be* the root 404'd every nested skill even though the GET
        ``/api/skills`` listing (via SkillsLoader) surfaced them fine."""
        skill_dir = _write_skill(fake_home / ".kiroclaw" / "skills", "utils/multi-badger")
        state = MagicMock(_slots={})
        out = _resolve_skill_root("utils/multi-badger", state)
        assert out == skill_dir.resolve()

    def test_nested_kiro_user_skill_resolves(self, fake_home):
        """Nesting must work for the kiro-user source too."""
        skill_dir = _write_skill(fake_home / ".kiro" / "skills", "cat/nested-one")
        state = MagicMock(_slots={})
        out = _resolve_skill_root("kiro-user/cat/nested-one", state)
        assert out == skill_dir.resolve()

    def test_kiroclaw_skill_honors_kiroclaw_home(self, tmp_path, monkeypatch):
        """``_resolve_skill_root`` must resolve kiroclaw skills under the
        active config home (``skills_dir()``), not a hardcoded
        ``~/.kiroclaw``.  An isolated dev gateway sets KIROCLAW_HOME to a
        separate directory; the tree/file endpoints must follow it."""
        home_dir = tmp_path / "real-home"
        home_dir.mkdir()
        monkeypatch.setenv("HOME", str(home_dir))
        monkeypatch.setattr(Path, "home", lambda: home_dir)

        # Isolated config home elsewhere, selected via KIROCLAW_HOME.
        mc_home = tmp_path / "dev-home"
        monkeypatch.setenv("KIROCLAW_HOME", str(mc_home))
        skill_dir = _write_skill(mc_home / "skills", "isolated-skill")

        state = MagicMock(_slots={})
        out = _resolve_skill_root("isolated-skill", state)
        assert out == skill_dir.resolve()
        # And nothing was created under the real ~/.kiroclaw.
        assert not (home_dir / ".kiroclaw" / "skills" / "isolated-skill").exists()

    def test_symlinked_intermediate_dir_escape_rejected(self, fake_home, tmp_path):
        """Security: a leaf skill symlink is allowed (AIM installs), but a
        symlinked *intermediate* directory that points outside the root
        must NOT let ``evil/skill`` escape the skills tree."""
        # Secret tree outside any skills root.
        outside = tmp_path / "outside" / "skill"
        outside.mkdir(parents=True)
        (outside / "SKILL.md").write_text("---\nname: x\n---\nsecret")

        skills_root = fake_home / ".kiroclaw" / "skills"
        skills_root.mkdir(parents=True)
        # ``evil`` is a symlinked intermediate dir → points at ../../outside.
        (skills_root / "evil").symlink_to(tmp_path / "outside")

        state = MagicMock(_slots={})
        # ``evil/skill`` resolves to outside/skill, whose parent (outside)
        # is not at/under the root → rejected.
        assert _resolve_skill_root("evil/skill", state) is None

    def test_aim_skill_symlink_to_sensitive_rejected(self, fake_home):
        """Security: the aim/ branch must re-check the *resolved* target, not
        just the unresolved candidate.  An AIM skill dir symlinked to a
        sensitive location must be rejected."""
        # Sensitive target with a SKILL.md inside.
        creds_dir = fake_home / ".aws"
        creds_dir.mkdir(parents=True)
        (creds_dir / "SKILL.md").write_text("---\nname: x\n---\nsecret")

        # AIM layout: ~/.aim/skills/<pkg>/<name>/SKILL.md, where <name> dir is
        # a symlink pointing into the sensitive ~/.aws directory.
        pkg_dir = fake_home / ".aim" / "skills" / "pkg"
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "evil").symlink_to(creds_dir)

        state = MagicMock(_slots={})
        # candidate.parent (the symlinked ``evil`` dir) resolves to ~/.aws,
        # which is sensitive → must return None, never the credentials dir.
        assert _resolve_skill_root("aim/evil", state) is None


# ── GET endpoints (integration) ──


def _make_app(state):
    from kiro_claw.dashboard.handlers import api_skill_file, api_skill_tree, api_skills

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/skills", api_skills)
    # Route order matters — specific routes must come before {name:.+}.
    # ``/-/`` separator avoids collision with skills named ``.../tree``.
    app.router.add_get("/api/skills/{name:.+}/-/tree", api_skill_tree)
    app.router.add_get("/api/skills/{name:.+}/-/file", api_skill_file)
    return app


class TestEndpoints:
    @pytest.mark.asyncio
    async def test_tree_endpoint_returns_entries(self, fake_home):
        skill_dir = _write_skill(fake_home / ".kiro" / "skills", "demo")
        (skill_dir / "helper.sh").write_text("#!/bin/sh\n")

        state = MagicMock(_slots={}, context_builder=None)
        # SkillsLoader will use ~/.kiroclaw/skills (empty here) — fine.
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/demo/-/tree")
            assert resp.status == 200
            data = await resp.json()
            paths = [e["path"] for e in data["entries"]]
            assert "SKILL.md" in paths
            assert "helper.sh" in paths
            # The absolute home path must be redacted to ``~`` — never leak
            # the server's real filesystem layout to the client.
            assert data["root"].startswith("~")
            assert str(fake_home) not in data["root"]

    @pytest.mark.asyncio
    async def test_tree_endpoint_404_for_unknown(self, fake_home):
        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/nope/-/tree")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_file_endpoint_returns_content(self, fake_home):
        skill_dir = _write_skill(fake_home / ".kiro" / "skills", "demo")
        (skill_dir / "helper.sh").write_text("#!/bin/sh\necho ok\n")

        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/demo/-/file?path=helper.sh")
            assert resp.status == 200
            data = await resp.json()
            assert "echo ok" in data["content"]

    @pytest.mark.asyncio
    async def test_file_endpoint_400_without_path(self, fake_home):
        _write_skill(fake_home / ".kiro" / "skills", "demo")
        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/demo/-/file")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_file_endpoint_400_on_traversal(self, fake_home):
        _write_skill(fake_home / ".kiro" / "skills", "demo")
        (fake_home / "secret.txt").write_text("PASSWORD")
        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/demo/-/file?path=../../secret.txt")
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_file_endpoint_413_for_oversized(self, fake_home):
        skill_dir = _write_skill(fake_home / ".kiro" / "skills", "demo")
        (skill_dir / "big.txt").write_bytes(b"x" * (SKILL_FILE_MAX_BYTES + 1))
        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.get("/api/skills/kiro-user/demo/-/file?path=big.txt")
            assert resp.status == 413

    @pytest.mark.asyncio
    async def test_endpoints_emit_sel_audit_events(self, fake_home, monkeypatch):
        """Tree/file access — including failed access — must emit SEL audit
        events.  Failed access (traversal/sensitive-path) is a probing signal."""
        _write_skill(fake_home / ".kiro" / "skills", "demo")
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_claw.dashboard.handlers.sel", lambda: sel_mock)

        state = MagicMock(_slots={}, context_builder=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            await client.get("/api/skills/kiro-user/demo/-/tree")
            await client.get("/api/skills/kiro-user/demo/-/file?path=SKILL.md")
            await client.get("/api/skills/kiro-user/demo/-/file?path=../../secret.txt")

        # Every access logged a tool invocation.
        tools = [c.kwargs.get("tool_name") for c in sel_mock.log_tool_invocation.call_args_list]
        outcomes = [c.kwargs.get("outcome") for c in sel_mock.log_tool_invocation.call_args_list]
        assert "api_skill_tree" in tools
        assert tools.count("api_skill_file") == 2
        assert "ok" in outcomes        # successful tree + file
        assert "blocked" in outcomes   # traversal attempt audited as blocked

    @pytest.mark.asyncio
    async def test_skill_named_tree_hits_detail_not_browser(self, fake_home):
        """Route collision regression: a nested skill whose last path segment
        is literally ``tree`` (``utils/tree``) must reach the detail endpoint,
        not the tree browser.  The ``/-/`` separator keeps them distinct."""
        from kiro_claw.dashboard.handlers import (
            api_skill_detail,
            api_skill_file,
            api_skill_tree,
        )
        from kiro_claw.skills import SkillsLoader

        # A real skill literally named ``utils/tree`` under the kiroclaw root.
        _write_skill(fake_home / ".kiroclaw" / "skills", "utils/tree", description="edge")

        app = web.Application()
        # Seed a *real* SkillsLoader so api_skill_detail can load the skill —
        # a bare MagicMock state would make ``_get_skills`` return a mock whose
        # load_skill() yields an unserializable MagicMock (500).
        state = MagicMock(_slots={}, context_builder=None)
        state._standalone_skills = SkillsLoader(install_builtins=False)
        app["state"] = state
        # Same registration order as server.py: browser routes (with /-/)
        # before the catch-all detail route.
        app.router.add_get("/api/skills/{name:.+}/-/tree", api_skill_tree)
        app.router.add_get("/api/skills/{name:.+}/-/file", api_skill_file)
        app.router.add_get("/api/skills/{name:.+}", api_skill_detail)

        async with TestClient(TestServer(app)) as client:
            # The detail endpoint for the skill named ``utils/tree``.
            resp = await client.get("/api/skills/utils/tree")
            assert resp.status == 200
            data = await resp.json()
            assert data["name"] == "utils/tree"
            assert "content" in data  # detail payload, not a tree listing

            # Its actual file browser lives under the /-/ separator.
            resp2 = await client.get("/api/skills/utils/tree/-/tree")
            assert resp2.status == 200
            assert "entries" in (await resp2.json())
