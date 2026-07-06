"""Tests for the override-expiry Slack notification gate (agent.notify_override_expiry)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kiro_claw.dashboard.server import _dispatch_override_expiry_notification


def _make_state() -> MagicMock:
    state = MagicMock()
    state._background_tasks = set()
    return state


def _cfg(notify: bool) -> SimpleNamespace:
    return SimpleNamespace(agent=SimpleNamespace(notify_override_expiry=notify))


def test_dispatch_skipped_when_disabled() -> None:
    """notify_override_expiry=False skips the DM and schedules no task."""
    state = _make_state()
    factory = MagicMock()
    with patch("kiro_claw.dashboard.server.KiroClawConfig.load", return_value=_cfg(False)):
        scheduled = _dispatch_override_expiry_notification(state, factory)

    assert scheduled is False
    assert state._background_tasks == set()
    factory.assert_not_called()


def test_dispatch_schedules_when_enabled() -> None:
    """notify_override_expiry=True schedules the DM task on the running loop."""

    async def _run() -> bool:
        state = _make_state()

        async def _noop() -> None:
            return None

        with patch(
            "kiro_claw.dashboard.server.KiroClawConfig.load", return_value=_cfg(True)
        ):
            scheduled = _dispatch_override_expiry_notification(state, _noop)
        # A task was registered (tracked to prevent GC); drain it to completion.
        assert len(state._background_tasks) == 1
        await asyncio.gather(*list(state._background_tasks))
        return scheduled

    assert asyncio.run(_run()) is True


def test_dispatch_skipped_without_event_loop() -> None:
    """No running event loop → skipped gracefully (returns False)."""
    state = _make_state()
    factory = MagicMock()
    with patch("kiro_claw.dashboard.server.KiroClawConfig.load", return_value=_cfg(True)):
        scheduled = _dispatch_override_expiry_notification(state, factory)

    assert scheduled is False
    assert state._background_tasks == set()
