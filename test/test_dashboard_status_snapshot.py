"""Tests for DashboardState.status_snapshot() — shared status payload."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from kiro_claw.dashboard.state import DashboardState


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
    crons = MagicMock()
    crons.list_jobs.return_value = [{"id": "j1"}, {"id": "j2"}]
    lessons = MagicMock()
    lessons.load_all.return_value = [{"rule": "r1"}]
    return DashboardState(
        sessions=MagicMock(count=3),
        crons=crons,
        lessons=lessons,
        start_time=time.time() - 120,
        subagents=MagicMock(count=1),
    )


class TestStatusSnapshot:
    def test_contains_core_fields(self, state: DashboardState) -> None:
        snap = state.status_snapshot()
        assert snap["sessions"] == 3
        assert snap["cron_jobs"] == 2
        assert snap["lessons"] == 1
        assert snap["subagents"] == 1
        assert snap["no_crons"] is False
        assert "uptime" in snap
        assert "start_time" in snap

    def test_no_crons_true(self, state: DashboardState) -> None:
        state.no_crons = True
        assert state.status_snapshot()["no_crons"] is True

    def test_governance_health_field_present(self, state: DashboardState) -> None:
        # AVP-23427: the snapshot surfaces governance enforcement health.
        snap = state.status_snapshot()
        assert snap["governance"] in {"active", "degraded", "disabled", "unknown"}

    def test_no_subagents(self, state: DashboardState) -> None:
        state.subagents = None
        assert state.status_snapshot()["subagents"] == 0

    def test_slack_connected_reflects_client(self, state: DashboardState) -> None:
        # No Slack client wired up (pure-dashboard / Slack disabled).
        assert state.slack_client is None
        assert state.status_snapshot()["slack_connected"] is False
        # Gateway wires up a live Slack client once Socket Mode connects.
        state.slack_client = MagicMock()
        assert state.status_snapshot()["slack_connected"] is True

    def test_new_fields_propagate_to_all_callers(self, state: DashboardState) -> None:
        """Any field added to status_snapshot is automatically in SSE/WS/API."""
        snap = state.status_snapshot()
        # These keys must exist — if one is missing, a caller will lose it
        required = {"uptime", "start_time", "sessions", "messages",
                    "cron_jobs", "lessons", "subagents", "update_available",
                    "no_crons", "slack_connected"}
        assert required.issubset(snap.keys())

    def test_cached_overrides_skip_expensive_calls(self, state: DashboardState) -> None:
        """Passing cron_jobs/lessons skips list_jobs()/load_all()."""
        state.crons.list_jobs.reset_mock()
        state.lessons.load_all.reset_mock()
        snap = state.status_snapshot(cron_jobs=99, lessons=42)
        assert snap["cron_jobs"] == 99
        assert snap["lessons"] == 42
        state.crons.list_jobs.assert_not_called()
        state.lessons.load_all.assert_not_called()

    def test_update_available_passthrough(self, state: DashboardState) -> None:
        assert state.status_snapshot()["update_available"] is False
        assert state.status_snapshot(update_available=True)["update_available"] is True


class TestAllStatusSnapshotCallersPassUpdateAvailable:
    """Every call to status_snapshot() must pass update_available explicitly."""

    def test_ws_passes_update_available(self) -> None:
        """Regression: ws.py must pass update_available to status_snapshot()."""
        import inspect

        from kiro_claw.dashboard import ws
        source = inspect.getsource(ws)
        assert "update_available=" in source, (
            "ws.py calls status_snapshot() without update_available — "
            "it will default to False, hiding real update availability from WebSocket clients"
        )

    def test_sse_handler_passes_update_available(self) -> None:
        import inspect

        from kiro_claw.dashboard import handlers
        source = inspect.getsource(handlers)
        assert "update_available=" in source

    def test_system_api_passes_update_available(self) -> None:
        import inspect

        from kiro_claw.dashboard import handlers_system
        source = inspect.getsource(handlers_system)
        assert "update_available=" in source
