"""Tests for the AutoNudge silent-death backstops.

These pin the failure mode where a dashboard-bound loop (``chat-N-TS`` slot)
stops firing forever while its persisted record still reads ``active=true`` with
no armed timer and no journal trace. Three defences guard the invariant "an
active loop always has an armed timer, observably":

1. The observation gate in ``_timer`` is exception-safe -- a raising gate still
   re-arms a live loop instead of killing its timer task silently.
2. A periodic reconciler re-arms any active loop found without an armed timer,
   the backstop for re-arm losses that originate outside this module (a nudge
   turn that never emits HOOK_EVENT_STOP; a dropped deferred re-arm).
3. A delivered fire and a reconciler rescue both log at INFO, so a dead loop and
   a calm one are no longer byte-identical from outside the process.

Each test exercises the real code path and fails if its fix is reverted.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import AutoNudgeService, NudgeLoop


@pytest.fixture(autouse=True)
def _enable(monkeypatch):
    monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture
def svc(tmp_path):
    return AutoNudgeService(base_dir=tmp_path)


def _nosleep_timer(monkeypatch) -> None:
    """Make the timer's ``asyncio.sleep`` a no-op so a fire runs immediately."""

    async def _nosleep(_secs):
        return None

    monkeypatch.setattr(_an.asyncio, "sleep", _nosleep)


@pytest.mark.asyncio
async def test_dashboard_fire_without_turn_complete_still_arms(svc, monkeypatch):
    """A delivered dashboard fire with NO notify_turn_complete must not orphan.

    A ``chat-N-TS`` slot is not a channel key, so ``_run_fire_cycle`` does not
    self-re-arm it; its only in-band re-arm is ``notify_turn_complete``. When
    that hook never arrives (the nudge turn errored/timed out/was cancelled on a
    path that skips it), the loop is left active with no armed timer. The
    reconciler is the backstop that revives it.
    """
    _nosleep_timer(monkeypatch)

    async def on_fire(loop):
        return True  # delivered, but nothing calls notify_turn_complete

    svc._on_fire = on_fire
    await svc.start()
    try:
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
        # Let the fire cycle run to completion. Delivered dashboard slot => the
        # cycle clears the deadline and does NOT re-arm (no channel key, no hook).
        # The finished timer task lingers in self._timers -- an orphan, not a
        # live armed timer.
        original = svc._timers[loop.id]
        await original
        assert original.done(), "precondition: dashboard fire left only a finished timer"
        assert svc._loops[loop.id].active is True

        # The reconciler is what revives it: a fresh, live timer replaces the
        # done residue.
        await svc._reconcile_loops()
        assert loop.id in svc._timers
        assert not svc._timers[loop.id].done()
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_raising_gate_leaves_loop_armed_and_active(svc, monkeypatch):
    """An exception escaping the observation gate must still re-arm a live loop.

    Without the exception-safe wrap, the gate exception kills the timer task with
    no re-arm and no state change -- a live loop with no armed timer.
    """
    _nosleep_timer(monkeypatch)

    fired: list[NudgeLoop] = []

    async def on_fire(loop):
        fired.append(loop)
        return True

    calls: list[str] = []

    async def _boom(loop):
        calls.append(loop.id)
        raise RuntimeError("gate blew up")

    # Spy the re-arm: record that recovery asked for it and drop in a live,
    # parked stand-in task so the assertions can observe an armed timer WITHOUT
    # the real _arm_from_deadline spawning a fresh self-scheduling _timer that
    # (sleep being a no-op) would re-raise unboundedly.
    rearmed: list[str] = []

    def _spy_arm(loop):
        rearmed.append(loop.id)
        svc._cancel_timer(loop.id, drop_claims=False)
        svc._timers[loop.id] = asyncio.ensure_future(asyncio.Event().wait())

    svc._on_fire = on_fire
    monkeypatch.setattr(svc, "_monitor_tick_is_quiet", _boom)
    await svc.start()
    try:
        loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
        first = svc._timers[loop.id]
        # Swap in the spy only now: add()'s initial arm used the real path, so the
        # first timer is a genuine _timer that will run the raising gate. The
        # recovery re-arm inside that timer then routes through the spy.
        monkeypatch.setattr(svc, "_arm_from_deadline", _spy_arm)
        await first  # runs the gate, which raises; the recovery path re-arms
        assert fired == [], "gate raised, so no fire should have happened"
        assert calls[0] == loop.id, "the gate must have been invoked and raised"
        assert rearmed == [loop.id], "the raising gate must trigger a recovery re-arm"
        assert svc._loops[loop.id].active is True
        assert loop.id in svc._timers, "raising gate must still leave an armed timer"
    finally:
        for t in list(svc._timers.values()):
            t.cancel()
        svc.stop()


@pytest.mark.asyncio
async def test_reconciler_rearms_active_and_skips_inactive(svc, monkeypatch):
    """The reconciler re-arms an active timerless loop and ignores an inactive one."""
    _nosleep_timer(monkeypatch)
    await svc.start()
    try:
        active = await svc.add(slot_key="chat-1-active", message="go", idle_secs=15)
        inactive = await svc.add(slot_key="chat-2-inactive", message="go", idle_secs=15)

        # Strip both timers to model the failure state, then deactivate one.
        svc._cancel_timer(active.id)
        svc._cancel_timer(inactive.id)
        svc._loops[inactive.id].active = False
        assert active.id not in svc._timers
        assert inactive.id not in svc._timers

        await svc._reconcile_loops()

        assert active.id in svc._timers, "active loop must be re-armed"
        assert inactive.id not in svc._timers, "inactive loop must be left alone"
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_firing_loop_is_not_touched_by_reconciler(svc, monkeypatch):
    """A loop mid-fire (in self._firing) is skipped: the fire cycle owns its timer."""
    _nosleep_timer(monkeypatch)
    await svc.start()
    try:
        loop = await svc.add(slot_key="chat-1-firing", message="go", idle_secs=15)
        svc._cancel_timer(loop.id)
        svc._firing.add(loop.id)
        try:
            await svc._reconcile_loops()
            assert loop.id not in svc._timers, "a firing loop must not be re-armed"
        finally:
            svc._firing.discard(loop.id)
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_delivered_fire_logs_at_info(svc, monkeypatch, caplog):
    """A confirmed delivered fire emits an INFO line naming the loop and slot."""
    _nosleep_timer(monkeypatch)

    async def on_fire(loop):
        return True

    svc._on_fire = on_fire
    await svc.start()
    try:
        with caplog.at_level(logging.INFO, logger="kiro_crew.autonudge"):
            loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=15)
            await svc._timers[loop.id]
        fire_lines = [
            r.getMessage()
            for r in caplog.records
            if "fired loop" in r.getMessage() and loop.id in r.getMessage()
        ]
        assert fire_lines, "a delivered fire must log an INFO line"
        assert "chat-1-123" in fire_lines[0]
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_reconciler_rescue_logs_at_info(svc, monkeypatch, caplog):
    """A reconciler rescue emits an INFO line so the re-arm is observable."""
    _nosleep_timer(monkeypatch)
    await svc.start()
    try:
        loop = await svc.add(slot_key="chat-1-rescue", message="go", idle_secs=15)
        svc._cancel_timer(loop.id)
        with caplog.at_level(logging.INFO, logger="kiro_crew.autonudge"):
            await svc._reconcile_loops()
        rescue_lines = [
            r.getMessage()
            for r in caplog.records
            if "re-arming active loop" in r.getMessage() and loop.id in r.getMessage()
        ]
        assert rescue_lines, "a reconciler rescue must log an INFO line"
        assert loop.id in svc._timers
    finally:
        svc.stop()


@pytest.mark.asyncio
async def test_reconciler_task_lifecycle(svc):
    """start() launches the reconciler task; stop() cancels and clears it."""
    await svc.start()
    assert svc._reconcile_task is not None
    assert not svc._reconcile_task.done()
    svc.stop()
    assert svc._reconcile_task is None
    # Yield so the cancellation is delivered and the task is not left pending.
    await asyncio.sleep(0)
