"""Tests for the dashboard module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from kiro_claw.dashboard.state import (
    DashboardState,
    _fmt_duration,
    _load_notifications,
    _maybe_trim_notifications,
    _persist_notification,
)


class TestDashboard:
    def test_fmt_duration_minutes(self) -> None:
        assert _fmt_duration(125) == "2m 5s"

    def test_fmt_duration_hours(self) -> None:
        assert _fmt_duration(3661) == "1h 1m"

    def test_fmt_duration_zero(self) -> None:
        assert _fmt_duration(0) == "0m 0s"

    def test_state_init(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=3),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        assert state.sessions.count == 3
        assert state.messages_received == 0

    def test_state_init_with_slack_client(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
            slack_client=MagicMock(),
            owner_id="U123",
        )
        assert state.slack_client is not None
        assert state.owner_id == "U123"


class TestNotificationPersistence:
    def test_persist_and_load(self, monkeypatch, tmp_path) -> None:
        """Notifications are persisted to JSONL and loaded on restart."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        _persist_notification({"kind": "cron", "title": "Job A", "body": "result"})
        _persist_notification({"kind": "subagent", "title": "Sub B", "body": "done"})

        loaded = _load_notifications()
        assert len(loaded) == 2
        assert loaded[0]["title"] == "Job A"
        assert loaded[1]["title"] == "Sub B"

    def test_load_empty(self, monkeypatch, tmp_path) -> None:
        """Loading from nonexistent file returns empty list."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        assert _load_notifications() == []

    def test_load_corrupted_lines_skipped(self, monkeypatch, tmp_path) -> None:
        """Corrupted JSON lines are skipped during load."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        path = tmp_path / "notifications.jsonl"
        lines = [
            json.dumps({"kind": "cron", "title": "Good", "body": "ok"}),
            "this is not json",
            json.dumps({"kind": "cron", "title": "Also good", "body": "ok"}),
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        loaded = _load_notifications()
        assert len(loaded) == 2
        assert loaded[0]["title"] == "Good"
        assert loaded[1]["title"] == "Also good"

    def test_trim_large_file(self, monkeypatch, tmp_path) -> None:
        """File is trimmed when exceeding 2x max notifications."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr("kiro_claw.dashboard.state._MAX_PERSISTED_NOTIFICATIONS", 5)
        path = tmp_path / "notifications.jsonl"
        # Write 11 lines (> 2 * 5)
        lines: list[str] = []
        for i in range(11):
            lines.append(json.dumps({"kind": "cron", "title": f"n{i}", "body": "x"}))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        _maybe_trim_notifications(path)

        remaining = path.read_text(encoding="utf-8").splitlines()
        assert len(remaining) == 5
        # Should keep the last 5
        assert json.loads(remaining[0])["title"] == "n6"
        assert json.loads(remaining[-1])["title"] == "n10"

    def test_notify_persists(self, monkeypatch, tmp_path) -> None:
        """DashboardState.notify() persists to disk."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        state.notify("cron", "Test Job", "Result text")

        # Check in-memory
        assert len(state._notification_log) == 1
        assert state._notification_log[0]["title"] == "Test Job"

        # Check on disk
        loaded = _load_notifications()
        assert len(loaded) == 1
        assert loaded[0]["title"] == "Test Job"
        assert "ts" in loaded[0]  # timestamp added

    def test_state_loads_existing_on_init(self, monkeypatch, tmp_path) -> None:
        """DashboardState.__init__ loads existing notifications from disk."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        # Pre-persist some notifications
        _persist_notification({"kind": "cron", "title": "Old", "body": "data"})
        _persist_notification({"kind": "cron", "title": "Old2", "body": "data2"})

        state = DashboardState(
            sessions=MagicMock(count=0),
            crons=MagicMock(),
            lessons=MagicMock(),
            start_time=0.0,
        )
        # Should have loaded existing notifications
        assert len(state._notification_log) == 2
        assert state._notification_log[0]["title"] == "Old"
        assert state._notification_log[1]["title"] == "Old2"
