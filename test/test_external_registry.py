"""Tests for kiro_claw.apps.registry — External (federated) registry support."""

from __future__ import annotations

import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_claw.apps.registry import (
    _external_registry_cache_path,
    _fetch_external_registry_index,
    _load_external_registries,
    _read_external_registry_cache,
    _write_external_registry_cache,
    get_registry_app,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def cache_dir(tmp_path, monkeypatch):
    """Redirect manifest cache to a temp directory."""
    cache = tmp_path / "cache" / "app-manifests"
    cache.mkdir(parents=True)
    monkeypatch.setattr(
        "kiro_claw.apps.registry._manifest_cache_dir",
        lambda: cache,
    )
    return cache


@pytest.fixture()
def sample_entries():
    return [
        {"name": "my-app", "repo": "MyAppRepo", "branch": "mainline"},
        {"name": "other-app", "repo": "OtherRepo", "branch": "mainline"},
    ]


# ---------------------------------------------------------------------------
# _read_external_registry_cache / _write_external_registry_cache
# ---------------------------------------------------------------------------


class TestExternalRegistryCache:
    def test_read_returns_none_when_no_file(self, cache_dir):
        assert _read_external_registry_cache("nonexistent") is None

    def test_write_then_read(self, cache_dir, sample_entries):
        _write_external_registry_cache("myorg", sample_entries)
        result = _read_external_registry_cache("myorg")
        assert result == sample_entries

    def test_read_returns_none_when_stale(self, cache_dir, sample_entries):
        _write_external_registry_cache("myorg", sample_entries)
        # Backdate the file to make it stale
        path = _external_registry_cache_path("myorg")
        old_time = time.time() - 7200  # 2 hours ago
        os.utime(path, (old_time, old_time))
        assert _read_external_registry_cache("myorg") is None

    def test_read_with_ignore_ttl_returns_stale_data(self, cache_dir, sample_entries):
        _write_external_registry_cache("myorg", sample_entries)
        path = _external_registry_cache_path("myorg")
        old_time = time.time() - 7200
        os.utime(path, (old_time, old_time))
        result = _read_external_registry_cache("myorg", ignore_ttl=True)
        assert result == sample_entries

    def test_read_returns_none_for_invalid_json(self, cache_dir):
        path = _external_registry_cache_path("bad")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json", encoding="utf-8")
        assert _read_external_registry_cache("bad") is None

    def test_read_returns_none_for_non_list_json(self, cache_dir):
        path = _external_registry_cache_path("obj")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"not": "a list"}', encoding="utf-8")
        assert _read_external_registry_cache("obj") is None


# ---------------------------------------------------------------------------
# _fetch_external_registry_index — input validation
# ---------------------------------------------------------------------------


class TestFetchExternalRegistryValidation:
    @pytest.fixture(autouse=True)
    def mock_sel(self, monkeypatch):
        """Patch _sel_fn so tests don't abort on SEL unavailability."""
        mock_sel_instance = MagicMock()
        monkeypatch.setattr(
            "kiro_claw.apps.registry._sel_fn",
            mock_sel_instance,
        )

    @pytest.mark.asyncio
    async def test_rejects_repo_with_path_traversal(self):
        result = await _fetch_external_registry_index("../evil", "mainline")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_repo_with_spaces(self):
        result = await _fetch_external_registry_index("my repo", "mainline")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_repo_with_slashes(self):
        result = await _fetch_external_registry_index("pkg/sub", "mainline")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_branch_with_double_dots(self):
        result = await _fetch_external_registry_index("ValidRepo", "main/../evil")
        assert result is None

    @pytest.mark.asyncio
    async def test_rejects_branch_with_shell_chars(self):
        result = await _fetch_external_registry_index("ValidRepo", "main;rm -rf /")
        assert result is None

    @pytest.mark.asyncio
    async def test_accepts_valid_repo_and_branch(self):
        """Valid inputs pass validation but fail on git (no network in tests)."""
        # External registries are now cloned via generic ``git clone``, so the
        # repo must be a cloneable URL (https/ssh/git). This passes validation
        # but fails on the actual git command (no network in unit tests). We
        # just verify it doesn't return None from validation alone.
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
            mock_proc.returncode = 128
            mock_exec.return_value = mock_proc
            result = await _fetch_external_registry_index(
                "https://github.com/example/ValidRepo-123.git", "mainline"
            )
            # Should have attempted git clone (passed validation)
            assert mock_exec.called
            assert result is None  # git failed but validation passed

    @pytest.mark.asyncio
    async def test_accepts_branch_with_slashes(self):
        """Branch names like 'feature/foo' are valid git refs."""
        with patch("asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))
            mock_proc.returncode = 128
            mock_exec.return_value = mock_proc
            await _fetch_external_registry_index(
                "https://github.com/example/MyRepo.git", "feature/branch-name"
            )
            assert mock_exec.called


# ---------------------------------------------------------------------------
# _fetch_external_registry_index — app-registry.json parsing
# ---------------------------------------------------------------------------


class TestFetchExternalRegistryParsing:
    @pytest.fixture(autouse=True)
    def mock_sel(self, monkeypatch):
        """Patch _sel_fn so tests don't abort on SEL unavailability."""
        mock_sel_instance = MagicMock()
        monkeypatch.setattr(
            "kiro_claw.apps.registry._sel_fn",
            mock_sel_instance,
        )

    @pytest.mark.asyncio
    async def test_parses_app_registry_json_from_clone(self, tmp_path):
        """Simulates a successful git clone whose checkout has app-registry.json."""
        registry_data = [{"name": "cool-app", "repo": "CoolApp", "branch": "mainline"}]
        repo_url = "https://github.com/example/CoolApp.git"

        clone_dir = tmp_path / "clone"

        # ``git clone`` is mocked: instead of cloning, populate the checkout
        # directory with the files the function reads back from disk.
        async def mock_exec_side_effect(*args, **kwargs):
            clone_dir.mkdir(parents=True, exist_ok=True)
            (clone_dir / "app-registry.json").write_text(
                json.dumps(registry_data), encoding="utf-8"
            )
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            return mock_proc

        with (
            patch("tempfile.mkdtemp", return_value=str(clone_dir)),
            patch("asyncio.create_subprocess_exec", side_effect=mock_exec_side_effect),
        ):
            result = await _fetch_external_registry_index(repo_url, "mainline")
            assert result == registry_data

    @pytest.mark.asyncio
    async def test_falls_back_to_apps_dir_scan(self, tmp_path):
        """When app-registry.json is absent, scans apps/*/app.json in the clone."""
        repo_url = "https://github.com/example/MyRepo.git"
        clone_dir = tmp_path / "clone"

        # ``git clone`` is mocked: populate the checkout with an apps/ tree but
        # no app-registry.json, exercising the fallback scan.
        async def mock_exec_side_effect(*args, **kwargs):
            app_dir = clone_dir / "apps" / "my-tool"
            app_dir.mkdir(parents=True, exist_ok=True)
            (app_dir / "app.json").write_text('{"name": "my-tool"}', encoding="utf-8")
            # A non-matching file that should be ignored.
            (clone_dir / "apps" / "README.md").write_text("hello", encoding="utf-8")
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            return mock_proc

        with (
            patch("tempfile.mkdtemp", return_value=str(clone_dir)),
            patch("asyncio.create_subprocess_exec", side_effect=mock_exec_side_effect),
        ):
            result = await _fetch_external_registry_index(repo_url, "mainline")
            assert result is not None
            assert len(result) == 1
            assert result[0]["name"] == "my-tool"
            assert result[0]["subdirectory"] == "apps/my-tool"


# ---------------------------------------------------------------------------
# _load_external_registries
# ---------------------------------------------------------------------------


class TestLoadExternalRegistries:
    @pytest.mark.asyncio
    async def test_returns_empty_when_no_registries_configured(self, monkeypatch):
        mock_config = MagicMock()
        mock_config.registries = []
        monkeypatch.setattr(
            "kiro_claw.config.loader.KiroClawConfig.load",
            lambda: mock_config,
        )
        result = await _load_external_registries()
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_cached_entries(self, cache_dir, monkeypatch):
        entries = [{"name": "cached-app", "repo": "R", "branch": "mainline"}]
        _write_external_registry_cache("myorg", entries)

        mock_reg = MagicMock()
        mock_reg.name = "myorg"
        mock_reg.repo = "MyOrgRepo"
        mock_reg.branch = "mainline"

        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_claw.config.loader.KiroClawConfig.load",
            lambda: mock_config,
        )

        result = await _load_external_registries()
        assert len(result) == 1
        assert result[0]["name"] == "cached-app"
        assert result[0]["_registry"] == "myorg"

    @pytest.mark.asyncio
    async def test_tags_entries_with_registry_name(self, cache_dir, monkeypatch):
        entries = [{"name": "app1"}, {"name": "app2"}]
        _write_external_registry_cache("identity", entries)

        mock_reg = MagicMock()
        mock_reg.name = "identity"
        mock_reg.repo = "IdentityApps"
        mock_reg.branch = "mainline"

        mock_config = MagicMock()
        mock_config.registries = [mock_reg]
        monkeypatch.setattr(
            "kiro_claw.config.loader.KiroClawConfig.load",
            lambda: mock_config,
        )

        result = await _load_external_registries()
        assert all(e["_registry"] == "identity" for e in result)


# ---------------------------------------------------------------------------
# get_registry_app — external cache lookup
# ---------------------------------------------------------------------------


class TestGetRegistryAppExternal:
    def test_finds_app_in_external_cache(self, cache_dir, monkeypatch):
        entries = [
            {"name": "ext-app", "repo": "ExtRepo", "branch": "mainline"},
        ]
        _write_external_registry_cache("myorg", entries)

        # Mock config to have one registry
        mock_reg = MagicMock()
        mock_reg.name = "myorg"
        mock_reg.repo = "MyOrgRepo"
        mock_reg.branch = "mainline"

        mock_config = MagicMock()
        mock_config.registries = [mock_reg]

        monkeypatch.setattr(
            "kiro_claw.apps.registry._load_registry_file",
            lambda: [],  # empty core registry
        )
        monkeypatch.setattr(
            "kiro_claw.config.loader.KiroClawConfig.load",
            lambda: mock_config,
        )

        result = get_registry_app("ext-app")
        assert result is not None
        assert result["name"] == "ext-app"

    def test_returns_none_when_not_found(self, cache_dir, monkeypatch):
        mock_config = MagicMock()
        mock_config.registries = []
        monkeypatch.setattr(
            "kiro_claw.apps.registry._load_registry_file",
            lambda: [],
        )
        monkeypatch.setattr(
            "kiro_claw.config.loader.KiroClawConfig.load",
            lambda: mock_config,
        )

        result = get_registry_app("nonexistent")
        assert result is None

    def test_prefers_core_registry_over_external(self, cache_dir, monkeypatch):
        core_entry = {"name": "shared-app", "repo": "CoreRepo", "branch": "mainline"}
        ext_entries = [{"name": "shared-app", "repo": "ExtRepo", "branch": "mainline"}]
        _write_external_registry_cache("myorg", ext_entries)

        mock_reg = MagicMock()
        mock_reg.name = "myorg"
        mock_reg.repo = "MyOrgRepo"
        mock_reg.branch = "mainline"

        mock_config = MagicMock()
        mock_config.registries = [mock_reg]

        monkeypatch.setattr(
            "kiro_claw.apps.registry._load_registry_file",
            lambda: [core_entry],
        )
        monkeypatch.setattr(
            "kiro_claw.config.loader.KiroClawConfig.load",
            lambda: mock_config,
        )

        result = get_registry_app("shared-app")
        assert result["repo"] == "CoreRepo"  # core wins
