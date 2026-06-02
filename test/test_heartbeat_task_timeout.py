"""Tests for the heartbeat per-task timeout + session teardown (F3).

An unattended heartbeat turn can block on a human-approval wait with no human
present. Without a bound, a single non-allowlisted tool approval would freeze the
whole heartbeat subsystem up to the 2h approval window. ``_heartbeat_task`` wraps
``stream_and_collect`` in ``asyncio.wait_for(timeout=HEARTBEAT_TASK_TIMEOUT_SECS)``
and, on timeout, resets the background session (killing the lingering turn/process)
before releasing it, returning a graceful incomplete result instead of crashing.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_claw.slack.gateway as gw_mod
from kiro_claw.heartbeat import HEARTBEAT_TASK_TIMEOUT_SECS
from kiro_claw.session import BACKGROUND_KEY


def _make_orchestrator():
    """GatewayOrchestrator with the minimal wiring _heartbeat_task touches."""
    orch = gw_mod.GatewayOrchestrator.__new__(gw_mod.GatewayOrchestrator)

    sessions = MagicMock()
    client = MagicMock()
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    sessions.reset = AsyncMock()
    sessions.release = MagicMock()
    sessions.recycle_background = AsyncMock()
    orch.sessions = sessions

    ctx_builder = MagicMock()
    ctx_builder.build_message = MagicMock(return_value=("full message", None))
    ctx_builder.hooks = MagicMock()
    ctx_builder.memory = MagicMock()
    orch.ctx_builder = ctx_builder

    orch.consolidator = None
    # Approval callback is irrelevant here — stream_and_collect is mocked.
    orch._interactive_approval = MagicMock(return_value=AsyncMock())
    orch._deliver_result = AsyncMock()
    return orch, sessions


async def _capture_task(orch):
    """Run _init_heartbeat with HeartbeatService.start stubbed; return on_task."""
    started = {}

    async def _fake_start(self):
        started["on_task"] = self._on_task

    orig_start = gw_mod.HeartbeatService.start
    gw_mod.HeartbeatService.start = _fake_start  # type: ignore[assignment]
    try:
        await orch._init_heartbeat()
    finally:
        gw_mod.HeartbeatService.start = orig_start  # type: ignore[assignment]
    return started["on_task"]


class TestHeartbeatTaskTimeout:
    @pytest.mark.asyncio()
    async def test_timeout_resets_and_releases_session(self, monkeypatch):
        orch, sessions = _make_orchestrator()

        # stream_and_collect hangs forever — simulates blocking on an approval
        # wait with no human present.
        async def _hang(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(gw_mod, "stream_and_collect", _hang)
        # Force the wait_for deadline to fire immediately instead of after 30 min.

        async def _fast_wait_for(awaitable, timeout):
            # Close the never-resolving coroutine to avoid "never awaited" noise
            # and raise the timeout the production code handles.
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise asyncio.TimeoutError

        monkeypatch.setattr(gw_mod.asyncio, "wait_for", _fast_wait_for)

        on_task = await _capture_task(orch)

        # Should NOT raise — timeout is handled gracefully.
        result = await on_task("do a thing", "")

        # In-flight turn torn down via reset on the background key before release.
        sessions.reset.assert_awaited_once_with(BACKGROUND_KEY)
        # finally still ran: session released + background recycled.
        sessions.release.assert_called_once_with(BACKGROUND_KEY)
        sessions.recycle_background.assert_awaited_once()
        # Graceful incomplete result mentions the deadline; loop not wedged.
        assert str(HEARTBEAT_TASK_TIMEOUT_SECS) in result
        assert "timed out" in result.lower()
        # No delivery suppression error — _deliver_result still invoked.
        orch._deliver_result.assert_awaited_once()

    @pytest.mark.asyncio()
    async def test_timeout_does_not_wedge_when_reset_fails(self, monkeypatch):
        """A failing reset is swallowed; release still runs so the loop continues."""
        orch, sessions = _make_orchestrator()
        sessions.reset = AsyncMock(side_effect=RuntimeError("reset boom"))

        async def _hang(*args, **kwargs):
            await asyncio.Event().wait()

        monkeypatch.setattr(gw_mod, "stream_and_collect", _hang)

        async def _fast_wait_for(awaitable, timeout):
            if asyncio.iscoroutine(awaitable):
                awaitable.close()
            raise asyncio.TimeoutError

        monkeypatch.setattr(gw_mod.asyncio, "wait_for", _fast_wait_for)

        on_task = await _capture_task(orch)
        result = await on_task("do a thing", "")

        sessions.reset.assert_awaited_once_with(BACKGROUND_KEY)
        sessions.release.assert_called_once_with(BACKGROUND_KEY)
        sessions.recycle_background.assert_awaited_once()
        assert "timed out" in result.lower()

    @pytest.mark.asyncio()
    async def test_success_path_does_not_reset(self, monkeypatch):
        """A normal (non-hanging) turn never resets the session."""
        orch, sessions = _make_orchestrator()

        async def _ok(*args, **kwargs):
            return "all good"

        monkeypatch.setattr(gw_mod, "stream_and_collect", _ok)

        on_task = await _capture_task(orch)
        result = await on_task("do a thing", "")

        sessions.reset.assert_not_awaited()
        sessions.release.assert_called_once_with(BACKGROUND_KEY)
        sessions.recycle_background.assert_awaited_once()
        assert result == "all good"
