"""Tests for CC session handling in SessionMap."""

from __future__ import annotations

from pathlib import Path

from kiro_claw.session import SessionMap


class TestSessionMapCC:
    def test_store_and_retrieve_cc(self, tmp_path: Path):
        sm = SessionMap()
        sm._path = tmp_path / "map.json"
        sm._data = {}
        sm.set("t1", "cc-sid", provider="claude_code")
        assert sm._data["t1"]["provider"] == "claude_code"
        assert sm.get("t1") == "cc-sid"

    def test_delete(self, tmp_path: Path):
        sm = SessionMap()
        sm._path = tmp_path / "map.json"
        sm._data = {"t1": {"sid": "s1", "slack_thread_ts": None, "slack_channel_id": None, "provider": "claude_code"}}
        sm.delete("t1")
        assert "t1" not in sm._data
