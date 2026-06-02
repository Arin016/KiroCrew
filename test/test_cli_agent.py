"""Unit tests for the ``kiroclaw agent`` CLI subcommand group.

Tests cover list output format, create with defaults, create duplicate,
update non-existent, and delete default agent.
"""

from __future__ import annotations

import json
import unittest.mock
from pathlib import Path

import pytest

from kiro_claw.cli import main


def _write_config(tmp_path: Path, data: dict) -> Path:
    """Write a config.json to *tmp_path* and return the path."""
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return p


def _base_config() -> dict:
    """Return a minimal valid config with a default agent."""
    return {
        "agents": {
            "default": {
                "kiro_agent": "kiroclaw",
                "workspace": "default",
                "memory_store": "default",
            },
        },
        "default_agent": "default",
        "workspaces": {"default": {"dir": "workspace"}},
        "memory_stores": {"default": {}},
    }


class TestAgentList:
    """Test ``kiroclaw agent list`` output format."""

    def test_list_output_format(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_claw.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", ["kiroclaw", "agent", "list"]),
        ):
            main()

        out = capsys.readouterr().out
        # Header row
        assert "NAME" in out
        assert "KIRO_AGENT" in out
        assert "WORKSPACE" in out
        assert "MEMORY_STORE" in out
        # Default agent marked with *
        assert "default *" in out or "default*" in out

    def test_list_multiple_agents(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        data = _base_config()
        data["agents"]["oncall"] = {
            "kiro_agent": "oncall-agent",
            "workspace": "oncall-ws",
            "memory_store": "oncall-mem",
        }
        cfg_path = _write_config(tmp_path, data)

        with (
            unittest.mock.patch("kiro_claw.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch("sys.argv", ["kiroclaw", "agent", "list"]),
        ):
            main()

        out = capsys.readouterr().out
        assert "oncall" in out
        assert "oncall-agent" in out


class TestAgentCreate:
    """Test ``kiroclaw agent create``."""

    def test_create_with_defaults(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_claw.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kiroclaw", "agent", "create", "--name", "research"],
            ),
        ):
            main()

        out = capsys.readouterr().out
        assert "Created agent: research" in out

        # Verify persisted to disk
        saved = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert "research" in saved["agents"]
        assert saved["agents"]["research"]["kiro_agent"] == "kiroclaw"
        assert saved["agents"]["research"]["workspace"] == "default"
        assert saved["agents"]["research"]["memory_store"] == "default"

    def test_create_duplicate_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_claw.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kiroclaw", "agent", "create", "--name", "default"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "already exists" in err


class TestAgentUpdate:
    """Test ``kiroclaw agent update``."""

    def test_update_nonexistent_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_claw.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kiroclaw", "agent", "update", "nonexistent", "--kiro-agent", "x"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "not found" in err


class TestAgentDelete:
    """Test ``kiroclaw agent delete``."""

    def test_delete_default_agent_exits_nonzero(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cfg_path = _write_config(tmp_path, _base_config())

        with (
            unittest.mock.patch("kiro_claw.config.loader.config_path", return_value=cfg_path),
            unittest.mock.patch(
                "sys.argv",
                ["kiroclaw", "agent", "delete", "default"],
            ),
            pytest.raises(SystemExit) as exc_info,
        ):
            main()

        assert exc_info.value.code != 0
        err = capsys.readouterr().err
        assert "cannot delete default agent" in err
