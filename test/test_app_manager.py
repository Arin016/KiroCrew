"""Tests for kiro_claw.apps.manager — App lifecycle management."""
from __future__ import annotations

import json

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_claw.apps.manager import (
    APP_MANIFEST_FILENAME,
    AppResult,
    InstalledApp,
    _read_installed,
    _validate_source_path,
    disable_app,
    enable_app,
    get_app,
    get_app_manifest,
    install_app,
    list_apps,
    uninstall_app,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_app_source(tmp_path, name="test-app", **manifest_overrides):
    """Create a minimal app source directory with a valid app.json."""
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    manifest = {
        "name": name,
        "version": "1.0.0",
        "displayName": "Test App",
        "description": "A test app for unit tests",
        "author": "tester",
        **manifest_overrides,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest, indent=2))
    return src


@pytest.fixture()
def app_home(tmp_path, monkeypatch):
    """Set KIROCLAW_HOME to a temp directory for isolated testing."""
    home = tmp_path / "kiroclaw-home"
    home.mkdir()
    monkeypatch.setenv("KIROCLAW_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TestValidation:
    def test_valid_source(self, tmp_path):
        src = _make_app_source(tmp_path)
        assert _validate_source_path(src) == []

    def test_missing_manifest(self, tmp_path):
        src = tmp_path / "empty"
        src.mkdir()
        errors = _validate_source_path(src)
        assert any("missing" in e for e in errors)

    def test_invalid_json(self, tmp_path):
        src = tmp_path / "bad"
        src.mkdir()
        (src / APP_MANIFEST_FILENAME).write_text("{not valid json")
        errors = _validate_source_path(src)
        assert any("invalid" in e.lower() for e in errors)

    def test_manifest_validation_errors(self, tmp_path):
        src = _make_app_source(tmp_path, name="")
        errors = _validate_source_path(src)
        assert any("name" in e for e in errors)


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

class TestInstall:
    def test_install_from_directory(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        result = install_app(src)
        assert result.ok
        assert result.name == "test-app"
        # Verify files copied
        installed_dir = app_home / "apps" / "test-app"
        assert installed_dir.is_dir()
        assert (installed_dir / APP_MANIFEST_FILENAME).is_file()
        # Verify installed.json
        meta = _read_installed("test-app")
        assert meta is not None
        assert meta.name == "test-app"
        assert meta.version == "1.0.0"
        assert meta.enabled is False  # installed but not enabled
        assert meta.installedAt != ""

    def test_install_creates_data_dir(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        data = app_home / "apps" / "test-app" / "data"
        assert data.is_dir()

    def test_install_nonexistent_source(self, app_home):
        result = install_app("/nonexistent/path")
        assert not result.ok
        assert "not a directory" in result.error

    def test_install_invalid_manifest(self, tmp_path, app_home):
        src = tmp_path / "bad-app"
        src.mkdir()
        (src / APP_MANIFEST_FILENAME).write_text('{"name": ""}')
        result = install_app(src)
        assert not result.ok
        assert "name" in result.error

    def test_install_duplicate_rejected(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        r1 = install_app(src)
        assert r1.ok
        r2 = install_app(src)
        assert not r2.ok
        assert "already installed" in r2.error

    def test_install_with_agents_and_skills(self, tmp_path, app_home):
        src = _make_app_source(
            tmp_path,
            agents=["agents/analyst.json"],
            skills=["skills/triage"],
        )
        # Create the referenced files
        (src / "agents").mkdir()
        (src / "agents" / "analyst.json").write_text('{"name": "analyst"}')
        (src / "skills" / "triage").mkdir(parents=True)
        (src / "skills" / "triage" / "SKILL.md").write_text("# Triage skill")

        result = install_app(src)
        assert result.ok
        # Verify files were copied
        installed = app_home / "apps" / "test-app"
        assert (installed / "agents" / "analyst.json").is_file()
        assert (installed / "skills" / "triage" / "SKILL.md").is_file()


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

class TestUninstall:
    def test_uninstall(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        result = uninstall_app("test-app")
        assert result.ok
        assert not (app_home / "apps" / "test-app").exists()

    def test_uninstall_not_installed(self, app_home):
        result = uninstall_app("nonexistent")
        assert not result.ok
        assert "not installed" in result.error

    def test_uninstall_keep_data(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        # Write some data
        data_dir = app_home / "apps" / "test-app" / "data"
        (data_dir / "cache.json").write_text('{"key": "value"}')

        result = uninstall_app("test-app", keep_data=True)
        assert result.ok
        # Data preserved
        assert (app_home / "apps" / "test-app" / "data" / "cache.json").is_file()
        # App files removed
        assert not (app_home / "apps" / "test-app" / APP_MANIFEST_FILENAME).exists()


# ---------------------------------------------------------------------------
# Enable / Disable
# ---------------------------------------------------------------------------

class TestEnableDisable:
    def test_enable(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        result = enable_app("test-app")
        assert result.ok
        meta = _read_installed("test-app")
        assert meta is not None
        assert meta.enabled is True

    def test_enable_already_enabled(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        enable_app("test-app")
        result = enable_app("test-app")
        assert result.ok
        assert "already enabled" in result.message

    def test_enable_not_installed(self, app_home):
        result = enable_app("nonexistent")
        assert not result.ok

    def test_disable(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        enable_app("test-app")
        result = disable_app("test-app")
        assert result.ok
        meta = _read_installed("test-app")
        assert meta is not None
        assert meta.enabled is False

    def test_disable_already_disabled(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        result = disable_app("test-app")
        assert result.ok
        assert "already disabled" in result.message

    def test_disable_not_installed(self, app_home):
        result = disable_app("nonexistent")
        assert not result.ok


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------

class TestListing:
    def test_list_empty(self, app_home):
        assert list_apps() == []

    def test_list_installed_apps(self, tmp_path, app_home):
        src1 = _make_app_source(tmp_path, name="app-one")
        src2 = _make_app_source(tmp_path, name="app-two")
        install_app(src1)
        install_app(src2)
        apps = list_apps()
        assert len(apps) == 2
        names = {a["name"] for a in apps}
        assert names == {"app-one", "app-two"}

    def test_get_app(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        info = get_app("test-app")
        assert info is not None
        assert info["name"] == "test-app"
        assert "manifest" in info
        assert info["manifest"]["name"] == "test-app"

    def test_get_app_not_installed(self, app_home):
        assert get_app("nonexistent") is None

    def test_get_manifest(self, tmp_path, app_home):
        src = _make_app_source(tmp_path)
        install_app(src)
        m = get_app_manifest("test-app")
        assert m is not None
        assert m.name == "test-app"
        assert m.version == "1.0.0"

    def test_get_manifest_not_installed(self, app_home):
        assert get_app_manifest("nonexistent") is None


# ---------------------------------------------------------------------------
# InstalledApp dataclass
# ---------------------------------------------------------------------------

class TestInstalledApp:
    def test_round_trip(self):
        meta = InstalledApp(
            name="my-app", version="1.0.0", displayName="My App",
            enabled=True, installedAt="2026-04-10T00:00:00Z", source="/tmp/src",
            origin="registry", resources="gateway", lifecycle="gateway",
        )
        d = meta.to_dict()
        meta2 = InstalledApp.from_dict(d)
        assert meta2.name == meta.name
        assert meta2.version == meta.version
        assert meta2.enabled == meta.enabled
        assert meta2.origin == meta.origin
        assert meta2.resources == meta.resources
        assert meta2.lifecycle == meta.lifecycle
        assert meta2.schemaVersion == 2

    def test_from_empty_dict(self):
        meta = InstalledApp.from_dict({})
        assert meta.name == ""
        assert meta.enabled is True  # default
        assert meta.origin == "registry"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"

    def test_builtin_fields(self):
        meta = InstalledApp.from_dict({
            "name": "channels", "origin": "builtin",
            "resources": "gateway", "lifecycle": "locked",
        })
        assert meta.origin == "builtin"
        assert meta.lifecycle == "locked"

    def test_external_fields(self):
        meta = InstalledApp.from_dict({
            "name": "mochi-pet", "origin": "external",
            "resources": "app", "lifecycle": "app",
        })
        assert meta.origin == "external"
        assert meta.resources == "app"
        assert meta.lifecycle == "app"

    def test_invalid_origin_falls_back(self):
        meta = InstalledApp.from_dict({"name": "bad", "origin": "typo"})
        assert meta.origin == "registry"  # default fallback

    def test_invalid_lifecycle_falls_back(self):
        meta = InstalledApp.from_dict({"name": "bad", "lifecycle": "gatway"})
        assert meta.lifecycle == "gateway"

    def test_invalid_resources_falls_back(self):
        meta = InstalledApp.from_dict({"name": "bad", "resources": "self"})
        assert meta.resources == "gateway"

    def test_validate_fields_valid(self):
        meta = InstalledApp(origin="builtin", resources="app", lifecycle="locked")
        assert meta.validate_fields() == []

    def test_validate_fields_invalid(self):
        meta = InstalledApp(origin="bad", resources="bad", lifecycle="bad")
        errors = meta.validate_fields()
        assert len(errors) == 3

    def test_schema_version_persisted(self):
        meta = InstalledApp(name="x")
        d = meta.to_dict()
        assert d["schemaVersion"] == 2

    # ── Migration from old "managed" field ──

    def test_migrate_managed_self(self):
        """Old managed='self' → external/app/app classification."""
        meta = InstalledApp.from_dict({"name": "old", "managed": "self"})
        assert meta.origin == "external"
        assert meta.resources == "app"
        assert meta.lifecycle == "app"
        assert meta.schemaVersion == 2

    def test_migrate_managed_builtin(self):
        """Old managed='builtin' → builtin/gateway/locked classification."""
        meta = InstalledApp.from_dict({"name": "old", "managed": "builtin"})
        assert meta.origin == "builtin"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "locked"
        assert meta.schemaVersion == 2

    def test_migrate_managed_kiroclaw(self):
        """Old managed='kiroclaw' with no source → defaults to registry."""
        meta = InstalledApp.from_dict({"name": "old", "managed": "kiroclaw"})
        assert meta.origin == "registry"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"
        assert meta.schemaVersion == 2

    def test_migrate_managed_kiroclaw_local_source(self):
        """Old managed='kiroclaw' with filesystem source → origin='local'."""
        meta = InstalledApp.from_dict({
            "name": "old", "managed": "kiroclaw",
            "source": "/Users/dev/my-tool",
        })
        assert meta.origin == "local"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"

    def test_migrate_managed_kiroclaw_registry_source(self):
        """Old managed='kiroclaw' with registry: source → origin='registry'."""
        meta = InstalledApp.from_dict({
            "name": "old", "managed": "kiroclaw",
            "source": "registry:my-app",
        })
        assert meta.origin == "registry"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"

    def test_migrate_skipped_when_origin_present(self):
        """If origin is already in the dict, migration is skipped even with schemaVersion < 2."""
        meta = InstalledApp.from_dict({
            "name": "old", "managed": "self",
            "origin": "local", "schemaVersion": 1,
        })
        # origin was explicitly set — migration should NOT override it
        assert meta.origin == "local"
        assert meta.resources == "gateway"  # default, not migrated to "app"

    def test_uninstall_locked_rejected(self, tmp_path, app_home):
        """lifecycle=locked apps cannot be uninstalled."""
        from kiro_claw.apps.manager import register_builtin_apps
        register_builtin_apps()
        result = uninstall_app("agent-worlds")
        assert not result.ok
        assert "locked" in result.error


# ---------------------------------------------------------------------------
# InstalledApp property tests (Hypothesis)
# ---------------------------------------------------------------------------

_valid_origins = st.sampled_from(["builtin", "registry", "local", "external"])
_valid_resources = st.sampled_from(["gateway", "app"])
_valid_lifecycles = st.sampled_from(["gateway", "app", "locked"])


class TestInstalledAppProperties:
    # Feature: app-classification-redesign, Property 1: InstalledApp 序列化往返一致性
    @given(
        name=st.from_regex(r"[a-z][a-z0-9\-]{0,20}", fullmatch=True),
        version=st.from_regex(r"[0-9]+\.[0-9]+\.[0-9]+", fullmatch=True),
        enabled=st.booleans(),
        origin=_valid_origins,
        resources=_valid_resources,
        lifecycle=_valid_lifecycles,
    )
    @settings(max_examples=200)
    def test_round_trip_property(self, name, version, enabled, origin, resources, lifecycle):
        """**Validates: Requirements 1.4**"""
        meta = InstalledApp(
            name=name, version=version, displayName=f"App {name}",
            enabled=enabled, installedAt="2026-01-01T00:00:00Z",
            source="test", origin=origin, resources=resources, lifecycle=lifecycle,
        )
        d = meta.to_dict()
        restored = InstalledApp.from_dict(d)
        assert restored.name == meta.name
        assert restored.version == meta.version
        assert restored.enabled == meta.enabled
        assert restored.origin == meta.origin
        assert restored.resources == meta.resources
        assert restored.lifecycle == meta.lifecycle
        assert restored.schemaVersion == meta.schemaVersion

    # Feature: app-classification-redesign, Property 2: 无效字段值回退到默认值
    @given(
        bad_origin=st.text(min_size=1, max_size=10).filter(
            lambda s: s not in {"builtin", "registry", "local", "external"}
        ),
        bad_resources=st.text(min_size=1, max_size=10).filter(
            lambda s: s not in {"gateway", "app"}
        ),
        bad_lifecycle=st.text(min_size=1, max_size=10).filter(
            lambda s: s not in {"gateway", "app", "locked"}
        ),
    )
    @settings(max_examples=200)
    def test_invalid_fields_fallback_property(self, bad_origin, bad_resources, bad_lifecycle):
        """**Validates: Requirements 1.6**"""
        meta = InstalledApp.from_dict({
            "name": "test", "origin": bad_origin,
            "resources": bad_resources, "lifecycle": bad_lifecycle,
        })
        assert meta.origin == "registry"
        assert meta.resources == "gateway"
        assert meta.lifecycle == "gateway"


# ---------------------------------------------------------------------------
# AppResult
# ---------------------------------------------------------------------------

class TestAppResult:
    def test_success(self):
        r = AppResult(ok=True, name="x", message="done")
        d = r.to_dict()
        assert d["ok"] is True
        assert d["name"] == "x"
        assert "error" not in d

    def test_failure(self):
        r = AppResult(ok=False, name="x", error="bad")
        d = r.to_dict()
        assert d["ok"] is False
        assert d["error"] == "bad"
