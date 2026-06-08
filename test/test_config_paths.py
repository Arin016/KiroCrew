"""Tests for the pure path-primitives leaf ``kiro_claw.config.paths``.

These pin two properties of the config-loader decoupling refactor:

1. The path primitives behave identically to their historical
   ``kiro_claw.config.loader`` definitions (back-compat).
2. ``kiro_claw.config.paths`` is a genuine leaf — importing it pulls in **no**
   ``kiro_claw`` modules (in particular not the heavy ``config.loader``), so the
   modules that only need ``config_dir()`` don't transitively load the DTOs,
   schema validation, the process-global cache, and the provider factory.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from kiro_claw.config import paths


class TestConfigDir:
    """``config_dir()`` resolves ~/.kiroclaw, honoring KIROCLAW_HOME."""

    def test_default_is_home_dotkiroclaw(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("KIROCLAW_HOME", raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = paths.config_dir()
        assert result == tmp_path / ".kiroclaw"
        assert result.is_dir()  # created on access

    def test_kiroclaw_home_override(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        home = tmp_path / "custom-home"
        monkeypatch.setenv("KIROCLAW_HOME", str(home))
        result = paths.config_dir()
        assert result == home.resolve()
        assert result.is_dir()

    def test_kiroclaw_home_system_dir_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # A system directory must be refused and fall back to ~/.kiroclaw.
        monkeypatch.setenv("KIROCLAW_HOME", "/usr")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        result = paths.config_dir()
        assert result == tmp_path / ".kiroclaw"


class TestConfigPackageDir:
    """``config_package_dir()`` points at the installed ``kiro_claw/config/``."""

    def test_points_at_config_package_with_defaults_json(self) -> None:
        pkg = paths.config_package_dir()
        assert pkg.name == "config"
        # The bundled agent defaults ship in this directory.
        assert (pkg / "defaults.json").is_file()

    def test_is_paths_module_parent(self) -> None:
        assert paths.config_package_dir() == Path(paths.__file__).resolve().parent


class TestDefaultWorkspaceBase:
    def test_linux_uses_home_workplace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert paths._default_workspace_base() == tmp_path / "workplace"

    def test_macos_prefers_volumes_then_falls_back(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        # Force the /Volumes/workplace probe to report absent so the fallback
        # path is exercised regardless of the host's real filesystem (this dev
        # box is itself rooted at /Volumes/workplace; CI is not).
        monkeypatch.setattr(Path, "is_dir", lambda self: False)
        assert paths._default_workspace_base() == tmp_path / "workplace"


class TestSafeDirName:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a/b", "a_b"),
            ("a\\b", "a_b"),
            ("a:b", "a_b"),
            ("a b", "a_b"),
            ("plain", "plain"),
            ("x/y:z w", "x_y_z_w"),
        ],
    )
    def test_sanitizes_separators(self, raw: str, expected: str) -> None:
        assert paths._safe_dir_name(raw) == expected


class TestLeafPurity:
    """The whole point of the extraction: importing the leaf is cheap.

    Importing ``kiro_claw.config.paths`` in a fresh interpreter must NOT import
    ``kiro_claw.config.loader`` (or any other ``kiro_claw`` submodule). Run in a
    subprocess so the already-warm modules in this test process don't mask a
    regression.
    """

    def test_importing_paths_pulls_no_kiro_claw_modules(self) -> None:
        code = (
            "import sys\n"
            "import kiro_claw.config.paths\n"
            "leaked = sorted(\n"
            "    m for m in sys.modules\n"
            "    if m.startswith('kiro_claw')\n"
            "    and m not in {'kiro_claw', 'kiro_claw.config', 'kiro_claw.config.paths'}\n"
            ")\n"
            "print(','.join(leaked))\n"
        )
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )
        leaked = [m for m in out.stdout.strip().split(",") if m]
        assert leaked == [], f"config.paths leaf leaked kiro_claw modules: {leaked}"


class TestBackCompatReexport:
    """All primitives remain importable from ``kiro_claw.config.loader``."""

    def test_loader_reexports_match_paths(self) -> None:
        from kiro_claw.config import loader

        for name in (
            "config_dir",
            "config_package_dir",
            "_default_workspace_base",
            "_safe_dir_name",
            "CONFIG_DIR_NAME",
            "OUTBOX_DIR_NAME",
            "_WORKSPACE_DIR_NAME",
        ):
            assert getattr(loader, name) is getattr(paths, name), name

    def test_config_package_lazy_surface(self) -> None:
        # `from kiro_claw.config import X` still resolves the public surface
        # without eagerly importing the loader at package import time.
        import kiro_claw.config as cfg

        assert cfg.config_dir is paths.config_dir
        assert cfg.KiroClawConfig.__name__ == "KiroClawConfig"
