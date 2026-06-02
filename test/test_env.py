"""Tests for kiro_claw.env."""

from __future__ import annotations

import os

from kiro_claw.env import _node_version_manager_bins, augmented_path


class TestAugmentedPath:
    def test_prepends_aim_mcp_servers(self) -> None:
        result = augmented_path("/usr/bin")
        dirs = result.split(os.pathsep)
        assert dirs[-1] == "/usr/bin"
        assert any(".aim/mcp-servers" in d for d in dirs)

    def test_aim_before_toolbox(self) -> None:
        result = augmented_path("")
        dirs = result.split(os.pathsep)
        aim_idx = next(i for i, d in enumerate(dirs) if ".aim/mcp-servers" in d)
        toolbox_idx = next(i for i, d in enumerate(dirs) if ".toolbox/bin" in d)
        assert aim_idx < toolbox_idx

    def test_empty_base(self) -> None:
        result = augmented_path("")
        assert result  # not empty
        assert not result.endswith(os.pathsep)  # no trailing separator

    def test_no_arg_defaults_empty(self) -> None:
        result = augmented_path()
        assert ".aim/mcp-servers" in result

    def test_includes_nvm_node_bins(self, tmp_path, monkeypatch) -> None:
        # Simulate a home with two nvm-installed node versions.
        nvm = tmp_path / ".nvm" / "versions" / "node"
        (nvm / "v18.0.0" / "bin").mkdir(parents=True)
        (nvm / "v22.5.0" / "bin").mkdir(parents=True)
        monkeypatch.setattr(os.path, "expanduser", lambda p: str(tmp_path) if p == "~" else p)

        dirs = augmented_path("/usr/bin").split(os.pathsep)
        nvm_bins = [d for d in dirs if ".nvm/versions/node" in d]
        assert len(nvm_bins) == 2
        # Newest version first (reverse-sorted).
        assert "v22.5.0" in nvm_bins[0]
        assert "v18.0.0" in nvm_bins[1]


class TestNodeVersionManagerBins:
    def test_empty_when_no_managers(self, tmp_path) -> None:
        assert _node_version_manager_bins(str(tmp_path)) == []

    def test_skips_version_dir_without_bin(self, tmp_path) -> None:
        # A node version dir that has no bin/ subdir is ignored.
        (tmp_path / ".nvm" / "versions" / "node" / "v20.0.0").mkdir(parents=True)
        assert _node_version_manager_bins(str(tmp_path)) == []

    def test_returns_existing_bin(self, tmp_path) -> None:
        bin_dir = tmp_path / ".nvm" / "versions" / "node" / "v20.0.0" / "bin"
        bin_dir.mkdir(parents=True)
        result = _node_version_manager_bins(str(tmp_path))
        assert result == [str(bin_dir)]
