"""Tests for mirror.py — kiro-to-cc configuration mirroring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_claw.mirror import (
    _is_aim_managed,
    _merge_mcp_servers,
    _merge_permissions,
    _rename_allowed_tools_in_skill,
    _translate_auto_approve,
    mirror_kiro_to_cc,
)


@pytest.fixture
def home_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up fake ~/.kiro and ~/.claude under tmp_path."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    kiro = fake_home / ".kiro"
    claude = fake_home / ".claude"
    kiro.mkdir()
    claude.mkdir()
    return {"home": fake_home, "kiro": kiro, "claude": claude}


class TestMirrorDryRun:
    """dry_run=True reports what would change without writing."""

    def test_reports_agents_without_writing(self, home_dirs):
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        agents_dir = kiro / "agents"
        agents_dir.mkdir()
        (agents_dir / "my-agent.json").write_text(
            json.dumps({"name": "my-agent", "prompt": "Hello."}),
            encoding="utf-8",
        )

        result = mirror_kiro_to_cc(dry_run=True)

        assert any(a["name"] == "my-agent" and a["action"] == "mirrored" for a in result["agents"])
        # File should NOT exist
        assert not (claude / "agents" / "my-agent.md").exists()

    def test_reports_skills_without_writing(self, home_dirs):
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        skill_dir = kiro / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\nContent.", encoding="utf-8")

        result = mirror_kiro_to_cc(dry_run=True)

        assert any(
            s["name"] == "my-skill" and s["action"] == "mirrored" for s in result["skills"]
        )
        assert not (claude / "skills" / "my-skill" / "SKILL.md").exists()

    def test_reports_mcp_without_writing(self, home_dirs):
        kiro = home_dirs["kiro"]
        home = home_dirs["home"]

        settings_dir = kiro / "settings"
        settings_dir.mkdir()
        (settings_dir / "mcp.json").write_text(
            json.dumps({
                "mcpServers": {
                    "test-server": {"command": "node", "args": ["server.js"]},
                },
            }),
            encoding="utf-8",
        )

        result = mirror_kiro_to_cc(dry_run=True)

        assert any(m["name"] == "test-server" for m in result["mcp"])
        # .claude.json should NOT be created
        assert not (home / ".claude.json").exists()


class TestExistingFilePreserved:
    """Existing ~/.claude/agents/<name>.md is preserved unless force=True."""

    def test_skips_existing_without_force(self, home_dirs):
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        agents_dir = kiro / "agents"
        agents_dir.mkdir()
        (agents_dir / "existing.json").write_text(
            json.dumps({"name": "existing", "prompt": "New content."}),
            encoding="utf-8",
        )

        # Pre-create the target
        cc_agents = claude / "agents"
        cc_agents.mkdir(parents=True)
        target = cc_agents / "existing.md"
        target.write_text("Original content.", encoding="utf-8")

        result = mirror_kiro_to_cc(force=False)

        assert any(
            a["name"] == "existing" and a["action"] == "skipped_exists" for a in result["agents"]
        )
        assert target.read_text(encoding="utf-8") == "Original content."

    def test_overwrites_with_force(self, home_dirs):
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        agents_dir = kiro / "agents"
        agents_dir.mkdir()
        (agents_dir / "existing.json").write_text(
            json.dumps({"name": "existing", "prompt": "New content."}),
            encoding="utf-8",
        )

        cc_agents = claude / "agents"
        cc_agents.mkdir(parents=True)
        target = cc_agents / "existing.md"
        target.write_text("Original content.", encoding="utf-8")

        result = mirror_kiro_to_cc(force=True)

        assert any(
            a["name"] == "existing" and a["action"] == "mirrored" for a in result["agents"]
        )
        assert "New content." in target.read_text(encoding="utf-8")


class TestAimManaged:
    """AIM-managed agents are skipped with a hint."""

    def test_aim_prefix_skipped(self, home_dirs):
        kiro = home_dirs["kiro"]

        agents_dir = kiro / "agents"
        agents_dir.mkdir()
        (agents_dir / "aim-builder.json").write_text(
            json.dumps({"name": "aim-builder", "prompt": "Hi."}),
            encoding="utf-8",
        )

        result = mirror_kiro_to_cc()

        assert any(
            a["name"] == "aim-builder" and a["action"] == "skipped_aim" for a in result["agents"]
        )

    def test_aim_subdir_skipped(self, home_dirs):
        kiro = home_dirs["kiro"]

        aim_dir = kiro / "agents" / "aim"
        aim_dir.mkdir(parents=True)
        (aim_dir / "managed.json").write_text(
            json.dumps({"name": "managed", "prompt": "Hi."}),
            encoding="utf-8",
        )

        result = mirror_kiro_to_cc()

        aim_agents = [a for a in result["agents"] if a["action"] == "skipped_aim"]
        assert len(aim_agents) >= 1

    def test_hint_message_present(self, home_dirs):
        kiro = home_dirs["kiro"]

        agents_dir = kiro / "agents"
        agents_dir.mkdir()
        (agents_dir / "aim-test.json").write_text(
            json.dumps({"name": "aim-test", "prompt": "Hi."}),
            encoding="utf-8",
        )

        result = mirror_kiro_to_cc()

        aim_entry = next(a for a in result["agents"] if a["name"] == "aim-test")
        assert "not mirrored" in aim_entry.get("hint", "")


class TestDisabledMcpNotMirrored:
    """MCP entries with disabled: true are NOT mirrored."""

    def test_disabled_skipped(self, home_dirs):
        kiro = home_dirs["kiro"]

        settings_dir = kiro / "settings"
        settings_dir.mkdir()
        (settings_dir / "mcp.json").write_text(
            json.dumps({
                "mcpServers": {
                    "active-server": {"command": "node", "args": ["a.js"]},
                    "disabled-server": {"command": "node", "args": ["b.js"], "disabled": True},
                },
            }),
            encoding="utf-8",
        )

        result = mirror_kiro_to_cc()

        actions = {m["name"]: m["action"] for m in result["mcp"]}
        assert actions.get("disabled-server") == "skipped_disabled"
        assert actions.get("active-server") == "mirrored"


class TestAutoApprovePermissions:
    """autoApprove entries land in ~/.claude/settings.local.json permissions.allow."""

    def test_auto_approve_mirrored(self, home_dirs):
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        settings_dir = kiro / "settings"
        settings_dir.mkdir()
        (settings_dir / "mcp.json").write_text(
            json.dumps({
                "mcpServers": {
                    "srv": {"command": "node", "args": ["s.js"]},
                },
                "autoApprove": ["mcp__kiroclaw-core__*", "Bash(cat *)"],
            }),
            encoding="utf-8",
        )

        result = mirror_kiro_to_cc()

        # Check permissions entry reported
        perm_entries = [m for m in result["mcp"] if m.get("action") == "mirrored_permissions"]
        assert len(perm_entries) == 1
        assert perm_entries[0]["count"] == 2

        # Check file was written
        settings_path = claude / "settings.local.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "mcp__kiroclaw-core__*" in data["permissions"]["allow"]
        assert "Bash(cat *)" in data["permissions"]["allow"]

    def test_does_not_duplicate_existing_permissions(self, home_dirs):
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        settings_dir = kiro / "settings"
        settings_dir.mkdir()
        (settings_dir / "mcp.json").write_text(
            json.dumps({
                "mcpServers": {"srv": {"command": "node", "args": ["s.js"]}},
                "autoApprove": ["existing-tool"],
            }),
            encoding="utf-8",
        )

        # Pre-create settings with the same entry
        claude.mkdir(exist_ok=True)
        settings_path = claude / "settings.local.json"
        settings_path.write_text(
            json.dumps({"permissions": {"allow": ["existing-tool"]}}),
            encoding="utf-8",
        )

        mirror_kiro_to_cc()

        data = json.loads(settings_path.read_text(encoding="utf-8"))
        # Should not be duplicated
        assert data["permissions"]["allow"].count("existing-tool") == 1


class TestInternalAgentSkipped:
    """Agents with _kiroclaw_internal: true are skipped."""

    def test_internal_flag_skipped(self, home_dirs):
        kiro = home_dirs["kiro"]

        agents_dir = kiro / "agents"
        agents_dir.mkdir()
        (agents_dir / "internal.json").write_text(
            json.dumps({"name": "internal", "prompt": "Hi.", "_kiroclaw_internal": True}),
            encoding="utf-8",
        )

        result = mirror_kiro_to_cc()

        assert any(
            a["name"] == "internal" and a["action"] == "skipped_internal" for a in result["agents"]
        )


class TestSkillMirroring:
    """Skills are copied from ~/.kiro/skills/ to ~/.claude/skills/."""

    def test_skill_directory_copied(self, home_dirs):
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        skill_dir = kiro / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\n---\nProcedure here.",
            encoding="utf-8",
        )
        (skill_dir / "helper.sh").write_text("#!/bin/bash\necho hi", encoding="utf-8")

        result = mirror_kiro_to_cc()

        assert any(s["name"] == "my-skill" and s["action"] == "mirrored" for s in result["skills"])
        dest = claude / "skills" / "my-skill" / "SKILL.md"
        assert dest.exists()
        assert (claude / "skills" / "my-skill" / "helper.sh").exists()

    def test_allowed_tools_renamed(self, home_dirs):
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        skill_dir = kiro / "skills" / "rename-test"
        skill_dir.mkdir(parents=True)
        content = "---\nname: rename-test\nallowed-tools: Bash, Read\n---\nBody."
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

        mirror_kiro_to_cc()

        dest = claude / "skills" / "rename-test" / "SKILL.md"
        out = dest.read_text(encoding="utf-8")
        assert "allowedTools:" in out
        assert "allowed-tools:" not in out

    def test_sensitive_sibling_file_skipped(self, home_dirs, monkeypatch):
        """A skill sibling whose resolved path is sensitive (e.g. a symlink to
        ~/.aws/credentials) must not be copied into ~/.claude/skills/."""
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        skill_dir = kiro / "skills" / "with-secret"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: with-secret\n---\nBody.", encoding="utf-8")
        secret = skill_dir / "leaked.txt"
        secret.write_text("AWS_SECRET=xyz", encoding="utf-8")

        # Flag only the sibling as sensitive.
        monkeypatch.setattr(
            "kiro_claw.mirror.is_sensitive_path",
            lambda p: p == str(secret.resolve()),
        )

        mirror_kiro_to_cc()

        # SKILL.md still mirrored, but the sensitive sibling is not copied.
        assert (claude / "skills" / "with-secret" / "SKILL.md").exists()
        assert not (claude / "skills" / "with-secret" / "leaked.txt").exists()

    def test_sensitive_file_in_subdir_skipped(self, home_dirs, monkeypatch):
        """A sensitive file nested inside a skill SUBDIRECTORY must not be
        copied — shutil.copytree would follow it, so the subtree copy applies
        per-entry is_sensitive_path checks."""
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        skill_dir = kiro / "skills" / "deep"
        sub = skill_dir / "scripts"
        sub.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: deep\n---\nBody.", encoding="utf-8")
        (sub / "ok.sh").write_text("echo ok", encoding="utf-8")
        secret = sub / "creds"
        secret.write_text("AWS_SECRET=xyz", encoding="utf-8")

        monkeypatch.setattr(
            "kiro_claw.mirror.is_sensitive_path",
            lambda p: p == str(secret.resolve()),
        )

        mirror_kiro_to_cc()

        base = claude / "skills" / "deep"
        assert (base / "SKILL.md").exists()
        assert (base / "scripts" / "ok.sh").exists()  # benign nested file copied
        assert not (base / "scripts" / "creds").exists()  # sensitive nested file skipped


class TestHelpers:
    """Unit tests for helper functions."""

    def test_is_aim_managed_prefix(self, tmp_path: Path):
        assert _is_aim_managed(tmp_path / "aim-builder.json")
        assert not _is_aim_managed(tmp_path / "my-agent.json")

    def test_is_aim_managed_subdir(self, tmp_path: Path):
        aim_path = tmp_path / "aim" / "agent.json"
        assert _is_aim_managed(aim_path)

    def test_merge_mcp_servers_no_clobber(self):
        existing = {"mcpServers": {"a": {"command": "old"}}}
        new = {"mcpServers": {"a": {"command": "new"}, "b": {"command": "new"}}}
        merged = _merge_mcp_servers(existing, new)
        assert merged["mcpServers"]["a"]["command"] == "old"
        assert merged["mcpServers"]["b"]["command"] == "new"

    def test_merge_permissions_dedup(self):
        existing = {"permissions": {"allow": ["tool1"]}}
        merged = _merge_permissions(existing, ["tool1", "tool2"])
        assert merged["permissions"]["allow"] == ["tool1", "tool2"]

    def test_translate_auto_approve_passthrough_globs(self):
        result = _translate_auto_approve(["mcp__core__*", "", "Bash(ls *)"])
        assert "mcp__core__*" in result
        assert "Bash(ls *)" in result
        assert "" not in result

    def test_translate_auto_approve_kiro_tool_names(self):
        """Bare kiro tool names are translated via _translate_tool_name."""
        result = _translate_auto_approve(["fs_read", "execute_bash", "grep"])
        assert "Read" in result
        assert "Bash" in result
        assert "Grep" in result

    def test_translate_auto_approve_at_server_prefix(self):
        """@server patterns without wildcards translate to mcp__server."""
        result = _translate_auto_approve(["@kiroclaw-core"])
        assert "mcp__kiroclaw-core" in result

    def test_rename_allowed_tools_in_skill(self):
        content = "---\nname: test\nallowed-tools: Bash\n---\nBody."
        result = _rename_allowed_tools_in_skill(content)
        assert "allowedTools: Bash" in result

    def test_rename_allowed_tools_no_frontmatter(self):
        content = "No frontmatter here."
        assert _rename_allowed_tools_in_skill(content) == content


class TestAgentPromptBodyPassthrough:
    """Mirrored agent markdown uses resolved prompt_body from file:// URIs."""

    def test_file_uri_prompt_resolved_in_agent_body(self, home_dirs):
        """file:// prompt URI resolves and appears as the CC agent body."""
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        agents_dir = kiro / "agents"
        agents_dir.mkdir()

        # Write a prompt file that the agent references
        prompt_file = agents_dir / "my-prompt.md"
        prompt_file.write_text("Custom agent instructions here.", encoding="utf-8")

        (agents_dir / "prompted-agent.json").write_text(
            json.dumps({
                "name": "prompted-agent",
                "prompt": f"file://{prompt_file}",
                "model": "opus",
            }),
            encoding="utf-8",
        )

        result = mirror_kiro_to_cc(force=True)

        assert any(
            a["name"] == "prompted-agent" and a["action"] == "mirrored"
            for a in result["agents"]
        )
        dest = claude / "agents" / "prompted-agent.md"
        assert dest.exists()
        content = dest.read_text(encoding="utf-8")
        # The resolved prompt body must appear in the generated markdown
        assert "Custom agent instructions here." in content
        # The file:// URI itself must NOT appear in the output
        assert "file://" not in content

    def test_inline_prompt_used_as_body(self, home_dirs):
        """Inline (non-file://) prompt string appears as the CC agent body."""
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        agents_dir = kiro / "agents"
        agents_dir.mkdir()
        (agents_dir / "inline.json").write_text(
            json.dumps({"name": "inline", "prompt": "Inline body text."}),
            encoding="utf-8",
        )

        mirror_kiro_to_cc(force=True)

        dest = claude / "agents" / "inline.md"
        content = dest.read_text(encoding="utf-8")
        assert "Inline body text." in content


class TestAutoApproveTranslation:
    """Integration: autoApprove with kiro tool names produces CC permissions."""

    def test_fs_read_becomes_read_in_permissions(self, home_dirs):
        """autoApprove: ['fs_read'] mirrors to permissions.allow with 'Read'."""
        kiro = home_dirs["kiro"]
        claude = home_dirs["claude"]

        settings_dir = kiro / "settings"
        settings_dir.mkdir()
        (settings_dir / "mcp.json").write_text(
            json.dumps({
                "mcpServers": {
                    "srv": {"command": "node", "args": ["s.js"]},
                },
                "autoApprove": ["fs_read", "execute_bash"],
            }),
            encoding="utf-8",
        )

        mirror_kiro_to_cc()

        settings_path = claude / "settings.local.json"
        assert settings_path.exists()
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        allow = data["permissions"]["allow"]
        assert "Read" in allow
        assert "Bash" in allow
        # Bare kiro names should NOT appear
        assert "fs_read" not in allow
        assert "execute_bash" not in allow
