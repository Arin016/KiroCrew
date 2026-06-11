"""Tests for kiro_claw.apps.bridges — resource registration bridges."""
from __future__ import annotations

import json

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from kiro_claw.apps.bridges import (
    RegistrationResult,
    _deregister_agents,
    _deregister_crons,
    _deregister_mcp_servers,
    _deregister_skills,
    _namespace,
    _register_agents,
    _register_crons,
    _register_mcp_servers,
    _register_skills,
    _safe_link_name,
    deregister_app,
    load_app_cron_defs,
    register_app,
)
from kiro_claw.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_claw.apps.manifest import AppManifest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app_source(tmp_path, name="test-app", **extras):
    """Create a minimal app source with agents and skills."""
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app",
        "author": "tester",
        "agents": ["agents/my-agent.json"],
        "skills": ["skills/my-skill"],
        "crons": [{"name": "refresh", "every": 3600, "agent": "my-agent", "message": "go"}],
        **extras,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    # Create agent file
    (src / "agents").mkdir()
    (src / "agents" / "my-agent.json").write_text(
        json.dumps({"name": "my-agent", "model": "auto"})
    )
    # Create skill directory
    (src / "skills" / "my-skill").mkdir(parents=True)
    (src / "skills" / "my-skill" / "SKILL.md").write_text("# My Skill\nDoes things.")
    return src


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Set up isolated KIROCLAW_HOME and KIRO agents dir."""
    home = tmp_path / "kiroclaw-home"
    home.mkdir()
    monkeypatch.setenv("KIROCLAW_HOME", str(home))

    kiro_agents = tmp_path / "kiro-agents"
    kiro_agents.mkdir()
    # Patch the KIRO_AGENTS_DIR in bridges module
    import kiro_claw.apps.bridges as bridges_mod
    monkeypatch.setattr(bridges_mod, "KIRO_AGENTS_DIR", kiro_agents)

    # Patch _MCP_JSON_PATH to avoid file descriptor errors in tests
    mcp_path = tmp_path / "mcp.json"
    monkeypatch.setattr(bridges_mod, "_MCP_JSON_PATH", mcp_path)

    return {"home": home, "kiro_agents": kiro_agents}


# ---------------------------------------------------------------------------
# Namespace helpers
# ---------------------------------------------------------------------------

class TestNamespace:
    def test_namespace(self):
        assert _namespace("my-app", "agent-1") == "my-app/agent-1"

    def test_safe_link_name(self):
        assert _safe_link_name("my-app/agent-1") == "my-app--agent-1"


# ---------------------------------------------------------------------------
# Agent registration
# ---------------------------------------------------------------------------

class TestAgentRegistration:
    def test_register_agents(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"

        registered = _register_agents("test-app", manifest, app_root)
        assert len(registered) == 1
        assert "test-app/my-agent" in registered

        # Verify symlink exists
        link = app_env["kiro_agents"] / "test-app--my-agent.json"
        assert link.is_symlink()
        # Verify it points to the right file
        target = json.loads(link.read_text())
        assert target["name"] == "my-agent"

    def test_deregister_agents(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        _register_agents("test-app", manifest, app_root)

        removed = _deregister_agents("test-app")
        assert removed == 1
        assert not (app_env["kiro_agents"] / "test-app--my-agent.json").exists()

    def test_missing_agent_file_skipped(self, tmp_path, app_env):
        src = _make_app_source(tmp_path, agents=["agents/nonexistent.json"])
        # Don't create the file
        (src / "agents").mkdir(exist_ok=True)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        registered = _register_agents("test-app", manifest, app_root)
        assert registered == []


# ---------------------------------------------------------------------------
# Skill registration
# ---------------------------------------------------------------------------

class TestSkillRegistration:
    def test_register_skills(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"

        registered = _register_skills("test-app", manifest, app_root)
        assert len(registered) == 1
        assert "test-app/my-skill" in registered

        # Verify symlink exists under ~/.kiroclaw/skills/test-app/my-skill
        skill_link = app_env["home"] / "skills" / "test-app" / "my-skill"
        assert skill_link.is_symlink()
        assert (skill_link / "SKILL.md").is_file()

    def test_deregister_skills(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        _register_skills("test-app", manifest, app_root)

        _deregister_skills("test-app")
        assert not (app_env["home"] / "skills" / "test-app").exists()

    def test_missing_skill_dir_skipped(self, tmp_path, app_env):
        src = _make_app_source(tmp_path, skills=["skills/nonexistent"])
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        app_root = app_env["home"] / "apps" / "test-app"
        registered = _register_skills("test-app", manifest, app_root)
        assert registered == []


# ---------------------------------------------------------------------------
# Cron registration
# ---------------------------------------------------------------------------

class TestCronRegistration:
    def test_register_crons(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )

        registered = _register_crons("test-app", manifest)
        assert len(registered) == 1
        assert "test-app/refresh" in registered

        # Verify cron manifest written
        defs = load_app_cron_defs("test-app")
        assert len(defs) == 1
        assert defs[0]["name"] == "test-app/refresh"
        assert defs[0]["every"] == 3600

    def test_deregister_crons(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        _register_crons("test-app", manifest)

        _deregister_crons("test-app")
        assert load_app_cron_defs("test-app") == []

    def test_no_crons(self, tmp_path, app_env):
        src = _make_app_source(tmp_path, crons=[])
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_crons("test-app", manifest)
        assert registered == []


# ---------------------------------------------------------------------------
# Top-level register / deregister
# ---------------------------------------------------------------------------

class TestTopLevel:
    def test_register_app(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        result = register_app("test-app")
        assert len(result.agents) == 1
        assert len(result.skills) == 1
        assert len(result.crons) == 1
        assert result.errors == []

    def test_register_nonexistent_app(self, app_env):
        result = register_app("nonexistent")
        assert len(result.errors) > 0

    def test_register_app_resources_app_skips_all(self, tmp_path, app_env, monkeypatch):
        """Apps with resources='app' manage their own registration.

        register_app must skip all bridge work (agents, skills, crons, MCP)
        to avoid creating duplicates that confuse kiro-cli.  This is the
        exact scenario that caused Mochi's subagent MCP tools to disappear:
        bridge created mochi-pet--mochi-pet-bg.json (empty mcpServers) alongside
        the real mochi-pet-bg.json, and kiro-cli loaded the empty one.
        """
        import kiro_claw.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        src = _make_app_source(tmp_path, mcpServers={
            "backend": {"url": "http://localhost:8080/mcp"},
        })
        install_app(src)

        # Mark as self-managed (like Mochi does via registerExternal)
        from kiro_claw.apps.manager import register_external_app
        register_external_app("test-app", "1.0.0", "Test App", resources="app")

        result = register_app("test-app")

        # Nothing registered — all skipped
        assert result.agents == []
        assert result.skills == []
        assert result.crons == []
        assert result.mcp_servers == []
        assert result.errors == []

        # No agent symlinks created
        assert not any(
            f.name.startswith("test-app--")
            for f in app_env["kiro_agents"].iterdir()
        )
        # No skill symlinks created
        assert not (app_env["home"] / "skills" / "test-app").exists()
        # No MCP entries written
        assert not mcp_path.exists()

    def test_deregister_app(self, tmp_path, app_env):
        src = _make_app_source(tmp_path)
        install_app(src)
        register_app("test-app")
        result = deregister_app("test-app")
        assert result.errors == []
        # Verify agents removed
        assert not any(
            f.name.startswith("test-app--")
            for f in app_env["kiro_agents"].iterdir()
        )

    def test_register_deregister_cycle(self, tmp_path, app_env):
        """Register, deregister, re-register — no stale state."""
        src = _make_app_source(tmp_path)
        install_app(src)

        r1 = register_app("test-app")
        assert len(r1.agents) == 1

        deregister_app("test-app")
        # Verify clean
        assert not any(
            f.name.startswith("test-app--")
            for f in app_env["kiro_agents"].iterdir()
        )

        r2 = register_app("test-app")
        assert len(r2.agents) == 1


# ---------------------------------------------------------------------------
# RegistrationResult
# ---------------------------------------------------------------------------

class TestRegistrationResult:
    def test_to_dict(self):
        r = RegistrationResult(
            agents=["a/b"], skills=["a/s"], crons=["a/c"], errors=[]
        )
        d = r.to_dict()
        assert d["agents"] == ["a/b"]
        assert d["errors"] == []


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------


class TestMCPRegistration:
    def test_register_mcp_servers(self, tmp_path, app_env, monkeypatch):
        import kiro_claw.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        src = _make_app_source(tmp_path, mcpServers={
            "my-mcp": {"url": "http://localhost:9000/mcp"},
        })
        install_app(src)
        manifest = AppManifest.from_json_file(
            app_env["home"] / "apps" / "test-app" / APP_MANIFEST_FILENAME
        )
        registered = _register_mcp_servers("test-app", manifest)
        assert registered == ["test-app:my-mcp"]

        data = json.loads(mcp_path.read_text())
        assert "test-app:my-mcp" in data["mcpServers"]

    def test_deregister_mcp_servers(self, tmp_path, app_env, monkeypatch):
        import kiro_claw.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        # Pre-populate with entries from two apps
        mcp_path.parent.mkdir(parents=True, exist_ok=True)
        mcp_path.write_text(json.dumps({
            "mcpServers": {
                "app-a:srv1": {"url": "http://localhost:1"},
                "app-a:srv2": {"url": "http://localhost:2"},
                "app-b:srv1": {"url": "http://localhost:3"},
            }
        }))

        removed = _deregister_mcp_servers("app-a")
        assert removed == 2

        data = json.loads(mcp_path.read_text())
        assert "app-a:srv1" not in data["mcpServers"]
        assert "app-a:srv2" not in data["mcpServers"]
        assert "app-b:srv1" in data["mcpServers"]

    def test_deregister_no_servers(self, tmp_path, monkeypatch):
        import kiro_claw.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)
        assert _deregister_mcp_servers("nonexistent") == 0

    def test_register_no_mcp_servers(self, tmp_path, app_env, monkeypatch):
        import kiro_claw.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        manifest = AppManifest(name="test", mcpServers={})
        assert _register_mcp_servers("test", manifest) == []

    def test_register_app_includes_mcp(self, tmp_path, app_env, monkeypatch):
        import kiro_claw.apps.bridges as bmod
        mcp_path = tmp_path / "mcp.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        src = _make_app_source(tmp_path, mcpServers={
            "backend": {"url": "http://localhost:8080/mcp"},
        })
        install_app(src)
        result = register_app("test-app")
        assert len(result.mcp_servers) == 1
        assert "test-app:backend" in result.mcp_servers


# ---------------------------------------------------------------------------
# MCP property tests
# ---------------------------------------------------------------------------

_app_name_st = st.from_regex(r"[a-z][a-z0-9\-]{0,15}", fullmatch=True)
_server_name_st = st.from_regex(r"[a-z][a-z0-9\-]{0,15}", fullmatch=True)


class TestMCPProperties:
    # Feature: app-classification-redesign, Property 10: MCP 服务器注册命名空间
    @given(
        app_name=_app_name_st,
        servers=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:9000")}),
            min_size=1, max_size=5,
        ),
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_register_namespace(self, app_name, servers, tmp_path, monkeypatch):
        """**Validates: Requirements 8.1, 8.2**"""
        import uuid

        import kiro_claw.apps.bridges as bmod
        mcp_path = tmp_path / f"mcp-{uuid.uuid4().hex[:8]}.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        manifest = AppManifest(name=app_name, mcpServers=servers)
        registered = _register_mcp_servers(app_name, manifest)

        for server_name in servers:
            expected = f"{app_name}:{server_name}"
            assert expected in registered

        data = json.loads(mcp_path.read_text()) if mcp_path.is_file() else {}
        for name in registered:
            assert name in data.get("mcpServers", {})

    # Feature: app-classification-redesign, Property 11: MCP 服务器注销隔离性
    @given(
        app_a=_app_name_st,
        app_b=_app_name_st.filter(lambda s: len(s) > 1),
        servers_a=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:1")}),
            min_size=1, max_size=3,
        ),
        servers_b=st.dictionaries(
            _server_name_st,
            st.fixed_dictionaries({"url": st.just("http://localhost:2")}),
            min_size=1, max_size=3,
        ),
    )
    @settings(max_examples=100, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_deregister_isolation(self, app_a, app_b, servers_a, servers_b, tmp_path, monkeypatch):
        """**Validates: Requirements 8.3**"""
        assume(app_a != app_b)
        import uuid

        import kiro_claw.apps.bridges as bmod
        mcp_path = tmp_path / f"mcp-iso-{uuid.uuid4().hex[:8]}.json"
        monkeypatch.setattr(bmod, "_MCP_JSON_PATH", mcp_path)

        # Register both apps
        _register_mcp_servers(app_a, AppManifest(name=app_a, mcpServers=servers_a))
        _register_mcp_servers(app_b, AppManifest(name=app_b, mcpServers=servers_b))

        # Deregister app_a
        _deregister_mcp_servers(app_a)

        data = json.loads(mcp_path.read_text()) if mcp_path.is_file() else {}
        remaining = data.get("mcpServers", {})

        # app_a entries gone
        for name in servers_a:
            assert f"{app_a}:{name}" not in remaining
        # app_b entries preserved
        for name in servers_b:
            assert f"{app_b}:{name}" in remaining


# ---------------------------------------------------------------------------
# Cron service bridge (register_app_crons_with_service)
# ---------------------------------------------------------------------------


class TestCronServiceBridge:
    """Tests for register_app_crons_with_service — promoting app crons to scheduler."""

    def _write_app_crons(self, tmp_path, app_name, cron_defs):
        """Write a fake app-crons.json for testing."""
        app_dir = tmp_path / "kiroclaw-home" / "apps" / app_name
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "app-crons.json").write_text(json.dumps(cron_defs, indent=2))

    def test_registers_cron_with_all_fields(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        from kiro_claw.apps.bridges import register_app_crons_with_service

        cron_defs = [{
            "name": "test-app/refresh",
            "every": 600,
            "cron_expr": "",
            "agent": "my-agent",
            "message": "do stuff",
            "app": "test-app",
            "agent_sequence": ["a1", "a2"],
            "env": {"FOO": "bar"},
            "persistent_session": False,
            "silent": True,
        }]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job.return_value = MagicMock(id="abc123")

        with patch("kiro_claw.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

        assert result == ["test-app/refresh"]
        mock_sdk.add_job.assert_called_once_with(
            name="test-app/refresh",
            message="do stuff",
            every_secs=600,
            cron_expr="",
            agent="my-agent",
            agent_sequence=["a1", "a2"],
            env={"FOO": "bar"},
            persistent_session=False,
            silent=True,
        )

    def test_idempotent_skips_existing(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        from kiro_claw.apps.bridges import register_app_crons_with_service

        cron_defs = [{"name": "test-app/refresh", "every": 600, "message": "go"}]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        existing_job = MagicMock()
        existing_job.name = "test-app/refresh"
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = [existing_job]

        with patch("kiro_claw.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

        assert result == []
        mock_sdk.add_job.assert_not_called()

    def test_returns_empty_when_no_cron_service(self, tmp_path, app_env):
        from kiro_claw.apps.bridges import register_app_crons_with_service

        result = register_app_crons_with_service("test-app", None)
        assert result == []

    def test_returns_empty_when_no_app_crons_file(self, tmp_path, app_env):
        from unittest.mock import MagicMock

        from kiro_claw.apps.bridges import register_app_crons_with_service

        result = register_app_crons_with_service("nonexistent-app", MagicMock())
        assert result == []

    def test_handles_malformed_entry_gracefully(self, tmp_path, app_env, monkeypatch):
        from unittest.mock import MagicMock, patch

        from kiro_claw.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {"name": "", "every": 600, "message": "bad"},  # empty name — skipped
            {"name": "test-app/good", "every": 300, "message": "ok"},
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        mock_sdk.add_job.return_value = MagicMock(id="x")

        with patch("kiro_claw.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

        assert result == ["test-app/good"]

    def test_register_crons_serializes_all_fields(self, tmp_path, app_env):
        """Verify _register_crons writes all CronEntry fields to app-crons.json."""
        from kiro_claw.apps.bridges import _register_crons, load_app_cron_defs

        manifest = AppManifest(
            name="test-app",
            version="1.0.0",
            displayName="Test",
            description="",
            author="t",
            crons=[],
        )
        # Manually construct a CronEntry with all fields set
        from kiro_claw.apps.manifest import CronEntry
        entry = CronEntry(
            name="refresh",
            every=600,
            agent="my-agent",
            message="go",
            agent_sequence=["a1"],
            env={"K": "V"},
            persistent_session=False,
            silent=True,
        )
        manifest.crons = [entry]

        _register_crons("test-app", manifest)
        defs = load_app_cron_defs("test-app")

        assert len(defs) == 1
        d = defs[0]
        assert d["agent_sequence"] == ["a1"]
        assert d["env"] == {"K": "V"}
        assert d["persistent_session"] is False
        assert d["silent"] is True

    def test_add_job_exception_logged_and_skipped(self, tmp_path, app_env):
        """Exception from CronSDK.add_job is caught, logged, and execution continues."""
        from unittest.mock import MagicMock, patch

        from kiro_claw.apps.bridges import register_app_crons_with_service

        cron_defs = [
            {"name": "test-app/bad", "every": 600, "message": "x"},
            {"name": "test-app/good", "every": 300, "message": "y"},
        ]
        self._write_app_crons(tmp_path, "test-app", cron_defs)

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.list_jobs.return_value = []
        # First call raises, second succeeds
        mock_sdk.add_job.side_effect = [RuntimeError("boom"), MagicMock(id="ok")]

        with patch("kiro_claw.apps.bridges.CronSDK", return_value=mock_sdk):
            result = register_app_crons_with_service("test-app", mock_cron_service)

        # Failed entry skipped, good entry registered
        assert result == ["test-app/good"]
        assert mock_sdk.add_job.call_count == 2


class TestCronServiceDeregister:
    """Tests for deregister_app_crons_from_service — scheduler cleanup helper."""

    def test_returns_zero_when_no_cron_service(self, tmp_path, app_env):
        from kiro_claw.apps.bridges import deregister_app_crons_from_service

        assert deregister_app_crons_from_service("test-app", None) == 0

    def test_calls_remove_all_and_returns_count(self, tmp_path, app_env):
        from unittest.mock import MagicMock, patch

        from kiro_claw.apps.bridges import deregister_app_crons_from_service

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.remove_all.return_value = 3

        with patch("kiro_claw.apps.bridges.CronSDK", return_value=mock_sdk):
            result = deregister_app_crons_from_service("test-app", mock_cron_service)

        assert result == 3
        mock_sdk.remove_all.assert_called_once()

    def test_returns_zero_on_exception(self, tmp_path, app_env):
        from unittest.mock import MagicMock, patch

        from kiro_claw.apps.bridges import deregister_app_crons_from_service

        mock_cron_service = MagicMock()
        mock_sdk = MagicMock()
        mock_sdk.remove_all.side_effect = RuntimeError("scheduler unavailable")

        with patch("kiro_claw.apps.bridges.CronSDK", return_value=mock_sdk):
            result = deregister_app_crons_from_service("test-app", mock_cron_service)

        assert result == 0  # exception swallowed, zero returned
