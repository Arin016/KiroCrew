"""Tests for the _cancel_requested flag in ClaudeCodeProvider.

When the user presses the stop button, _cancel_persistent() may need to
kill the CC process (SIGINT timed out). The process death produces a None
sentinel in the event queue. Without the fix, _stream_persistent() yields
stop_reason="error: process died", which triggers chat_runner's retry logic
and re-queues the message — unintended for user-initiated aborts.

The fix sets _cancel_requested=True before killing, causing
_stream_persistent() to yield stop_reason="cancelled" instead, which
chat_runner treats as an intentional stop (no retry).
"""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_claw.providers.claude_code import ClaudeCodeProvider


@pytest.fixture
def provider():
    """Return a ClaudeCodeProvider wired for persistent mode with a fake process."""
    p = ClaudeCodeProvider(connection_mode="per_session")
    p._reader_task = None
    p._stderr_task = None
    p._sandbox_cleanup = None
    p._child_track_task = None
    return p


class _FakeProc:
    """Minimal fake process for cancel tests."""

    def __init__(self, *, responds_to_sigint: bool = False, pid: int = 54321):
        self.pid = pid
        self._rc: int | None = None
        self._responds_to_sigint = responds_to_sigint
        self.signals_sent: list[int] = []
        self.stdin = MagicMock()
        self.stdin.write = MagicMock()
        self.stdin.drain = AsyncMock()
        self.stdout = AsyncMock()

    @property
    def returncode(self) -> int | None:
        return self._rc

    def send_signal(self, sig: int) -> None:
        self.signals_sent.append(sig)
        if sig == signal.SIGINT and self._responds_to_sigint:
            self._rc = -2

    def terminate(self) -> None:
        self._rc = -15

    def kill(self) -> None:
        self._rc = -9

    async def wait(self) -> int | None:
        return self._rc


class TestCancelRequestedFlag:
    """Verify _cancel_requested prevents retry on user-initiated stop."""

    @pytest.mark.asyncio
    async def test_cancel_sets_flag_before_sigint(self, provider):
        """_cancel_persistent sets _cancel_requested=True before sending SIGINT."""
        proc = _FakeProc(responds_to_sigint=True)
        provider._proc = proc
        provider._turn_in_progress = True
        provider._turn_done = asyncio.Event()

        # Simulate CC responding to SIGINT by setting turn_done
        original_send_signal = proc.send_signal

        def _send_and_ack(sig):
            original_send_signal(sig)
            if sig == signal.SIGINT:
                provider._turn_done.set()

        proc.send_signal = _send_and_ack

        outcome = await provider._cancel_persistent()

        assert provider._cancel_requested is True
        assert outcome == "acked"
        assert signal.SIGINT in proc.signals_sent

    @pytest.mark.asyncio
    async def test_cancel_no_turn_does_not_set_flag(self, provider):
        """When no turn is in progress, flag should not be set."""
        proc = _FakeProc()
        provider._proc = proc
        provider._turn_in_progress = False

        outcome = await provider._cancel_persistent()

        assert outcome == "no_turn"
        assert provider._cancel_requested is False

    @pytest.mark.asyncio
    async def test_cancel_no_proc_does_not_set_flag(self, provider):
        """When process is None, flag should not be set."""
        provider._proc = None

        outcome = await provider._cancel_persistent()

        assert outcome == "no_turn"
        assert provider._cancel_requested is False

    @pytest.mark.asyncio
    async def test_process_death_after_cancel_yields_cancelled(self, provider):
        """When _cancel_requested is set during a turn and event queue returns
        None, _stream_persistent should yield stop_reason='cancelled'."""
        proc = _FakeProc()
        provider._proc = proc
        provider._turn_done = asyncio.Event()
        provider._event_queue = asyncio.Queue()
        provider._reconnect_lock = asyncio.Lock()

        # Simulate: cancel is requested mid-turn (after _stream_persistent
        # starts and clears the flag, the cancel sets it again)
        async def _inject_cancel_then_death():
            # Small delay to let _stream_persistent start its event loop
            await asyncio.sleep(0.01)
            provider._cancel_requested = True
            await provider._event_queue.put(None)

        asyncio.create_task(_inject_cancel_then_death())

        events = []
        async for event in provider._stream_persistent("test message"):
            events.append(event)

        assert len(events) == 1
        assert events[0].stop_reason == "cancelled"
        assert provider._cancel_requested is False

    @pytest.mark.asyncio
    async def test_process_death_without_cancel_yields_error(self, provider):
        """Without _cancel_requested, process death should yield 'error: process died'."""
        proc = _FakeProc()
        provider._proc = proc
        provider._turn_done = asyncio.Event()
        provider._event_queue = asyncio.Queue()
        provider._reconnect_lock = asyncio.Lock()

        async def _inject_death():
            await asyncio.sleep(0.01)
            await provider._event_queue.put(None)

        asyncio.create_task(_inject_death())

        events = []
        async for event in provider._stream_persistent("test message"):
            events.append(event)

        assert len(events) == 1
        assert events[0].stop_reason == "error: process died"

    @pytest.mark.asyncio
    async def test_flag_reset_at_turn_start(self, provider):
        """_cancel_requested is reset to False at the start of each new turn,
        so a stale flag from a previous cancel doesn't affect the next turn."""
        proc = _FakeProc()
        provider._proc = proc
        provider._turn_done = asyncio.Event()
        provider._event_queue = asyncio.Queue()
        provider._reconnect_lock = asyncio.Lock()

        # Set flag as if a previous cancel was in flight
        provider._cancel_requested = True

        # Inject death without setting cancel again
        async def _inject_death():
            await asyncio.sleep(0.01)
            await provider._event_queue.put(None)

        asyncio.create_task(_inject_death())

        events = []
        async for event in provider._stream_persistent("test message"):
            events.append(event)

        # Flag was reset at turn start, so process death is NOT treated as cancel
        assert len(events) == 1
        assert events[0].stop_reason == "error: process died"
        assert provider._cancel_requested is False

    @pytest.mark.asyncio
    async def test_cancel_kill_timeout_still_yields_cancelled(self, provider):
        """When SIGINT times out and process is killed, the death event
        should still be treated as cancelled (not retried)."""
        proc = _FakeProc(responds_to_sigint=False)
        provider._proc = proc
        provider._turn_in_progress = True
        provider._turn_done = asyncio.Event()

        # Mock _kill_persistent_process to just set returncode
        async def _fake_kill():
            proc._rc = -9

        provider._kill_persistent_process = _fake_kill

        # wait_for will timeout since turn_done is never set
        outcome = await provider._cancel_persistent()

        assert outcome == "acked"
        assert provider._cancel_requested is True
        assert proc._rc == -9
