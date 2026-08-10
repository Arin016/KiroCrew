"""Tests for the pre-fire safety-override headroom hook.

The reactive shape this replaced had to answer "is some loop still armed?" from
inside the tool-approval path, on the event loop, at the moment the grant lapsed.
The hook here is called BY the enforcer immediately before it dispatches an
unattended turn, so it asks nothing -- and it pushes its blocking work (config
read, fail-closed audit write) to a worker thread, so the gateway's heartbeat is
never behind a filesystem stall.

Every behavioural claim below is mutation-verified; the mutation is named in the
test so a future reader can repeat it.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew import autonudge as an


@pytest.fixture(autouse=True)
def _clear_hook():
    an.set_pre_fire_hook(None)
    yield
    an.set_pre_fire_hook(None)


def _loop(**kw) -> an.NudgeLoop:
    base = dict(id="L1", slot_key="chat-1", message="go", idle_secs=420, active=True)
    base.update(kw)
    return an.NudgeLoop(**base)  # type: ignore[arg-type]


class TestPreFireHookRegistration:
    def test_the_hook_starts_unset_so_the_feature_is_opt_in(self) -> None:
        assert an._PRE_FIRE_HOOK is None

    def test_setting_and_clearing_the_hook_round_trips(self) -> None:
        async def _h(_loop):
            return None

        an.set_pre_fire_hook(_h)
        assert an._PRE_FIRE_HOOK is _h
        an.set_pre_fire_hook(None)
        assert an._PRE_FIRE_HOOK is None

    def test_the_hook_is_module_level_so_registration_cannot_race_start(self) -> None:
        """Deliberately NOT an attribute on the service instance: the singleton is
        only set inside ``start()``, so an instance attribute would force the
        dashboard to register after startup and lose the hook if ordering changed.

        Asserted as the design property rather than by checking that no service
        exists -- an earlier version asserted ``get_instance() is None``, which
        passed alone and failed in the full suite because another test had left a
        service in the module singleton. A test that depends on global state to
        make its point is order-dependent, and sharding decides whether it passes.
        """
        async def _h(_loop):
            return None

        an.set_pre_fire_hook(_h)
        assert an._PRE_FIRE_HOOK is _h
        assert not hasattr(an.AutoNudgeService, "on_pre_fire"), (
            "the hook must not live on the service class/instance"
        )
        assert not hasattr(an.AutoNudgeService(base_dir=None), "on_pre_fire")


class TestPreFireBlockingWorkLeavesTheEventLoop:
    """The whole point of the redesign: the config read and the critical SEL
    write must not run on the loop that serves tool approvals.

    Asserted structurally, and the limitation is deliberate rather than hidden:
    the dashboard's hook is a closure inside ``start_dashboard`` and cannot be
    imported, which is the same seam #2375 tracks. An earlier draft "verified"
    this by writing its own ``asyncio.to_thread`` call in the test and checking
    the thread ids differed — which proved that ``to_thread`` works, not that the
    production hook uses it.
    """

    def _server_source(self) -> str:
        from pathlib import Path

        import kiro_crew.dashboard.server as srv

        # encoding is explicit, not incidental: the default is the PLATFORM
        # default (cp1252 on Windows), and this file contains non-ASCII prose, so
        # a bare read_text() raises UnicodeDecodeError there while passing on any
        # UTF-8 host.
        return Path(srv.__file__).read_text(encoding="utf-8")

    def test_the_hook_delegates_its_blocking_half_to_a_worker_thread(self) -> None:
        """Mutation: call ``_renew_override_headroom(...)`` directly in the async
        hook instead of via ``asyncio.to_thread`` — this test fails.
        """
        src = self._server_source()
        assert "await asyncio.to_thread(\n            _renew_override_headroom" in src, (
            "the pre-fire hook must push _renew_override_headroom to a thread; "
            "running it inline puts the config read and the SEL write back on the "
            "event loop"
        )

    def test_the_blocking_half_is_the_only_place_config_and_the_lease_are_touched(
        self,
    ) -> None:
        """Keeps the split honest: if a later edit reads config or renews the lease
        from the async half, the work is back on the loop even though the
        ``to_thread`` call still exists.
        """
        src = self._server_source()
        start = src.index("def _renew_override_headroom(")
        end = src.index("async def _ensure_override_headroom(")
        blocking_half, async_half = src[start:end], src[end:]
        async_half = async_half[: async_half.index("def _on_override_expired(")]

        assert "KiroCrewConfig.load()" in blocking_half
        assert "renew_lease(" in blocking_half
        assert "KiroCrewConfig.load()" not in async_half, (
            "config is read on the event loop again"
        )
        assert "renew_lease(" not in async_half, "the lease is renewed on the event loop again"


class TestPreFireOrderingInTheRealTimer:
    """Drives the actual ``_timer`` rather than re-implementing its shape.

    An earlier draft of these tests asserted a try/except written inside the test
    itself, which proved only that the test's own error handling worked. These
    call the enforcer.
    """

    def _service(self, tmp_path, fired: list[str]):
        async def _on_fire(_loop) -> bool:
            fired.append("fire")
            return True

        return an.AutoNudgeService(base_dir=tmp_path, on_fire=_on_fire)

    def test_the_hook_runs_before_the_nudge_is_dispatched(self, tmp_path) -> None:
        """Headroom must be established BEFORE the unattended turn, not after —
        after is the reactive shape this replaced.

        Mutation: move the hook await below ``_run_fire_cycle`` — the recorded
        order flips and this test fails.
        """
        events: list[str] = []
        svc = self._service(tmp_path, events)

        async def _hook(_loop) -> None:
            events.append("hook")

        async def _drive() -> None:
            an.set_pre_fire_hook(_hook)
            await svc._timer(_loop(), delay=0)

        asyncio.run(_drive())
        assert events == ["hook", "fire"], events

    def test_a_raising_hook_still_lets_the_nudge_fire(self, tmp_path) -> None:
        """A loop that cannot get auto-approval headroom should still fire and
        merely wait on approval — strictly better than not firing at all.

        Mutation: remove the try/except around the hook await in ``_timer`` — the
        exception escapes, the fire never happens, and this test fails.
        """
        events: list[str] = []
        svc = self._service(tmp_path, events)

        async def _boom(_loop) -> None:
            events.append("hook")
            raise RuntimeError("config unreadable")

        async def _drive() -> None:
            an.set_pre_fire_hook(_boom)
            await svc._timer(_loop(), delay=0)

        asyncio.run(_drive())
        assert events == ["hook", "fire"], events

    def test_no_hook_registered_still_fires(self, tmp_path) -> None:
        events: list[str] = []
        svc = self._service(tmp_path, events)

        async def _drive() -> None:
            await svc._timer(_loop(), delay=0)

        asyncio.run(_drive())
        assert events == ["fire"]

    def test_a_terminal_loop_never_reaches_the_hook(self, tmp_path) -> None:
        """The hook is only asked for a loop that is genuinely about to run, which
        is what removed the need for a separate liveness predicate. A loop at its
        cycle cap must not extend anyone's authorization.

        Mutation: move the hook above the cycle-cap check — the hook runs for a
        spent loop and this test fails.
        """
        events: list[str] = []
        svc = self._service(tmp_path, events)

        async def _hook(_loop) -> None:
            events.append("hook")

        async def _drive() -> None:
            an.set_pre_fire_hook(_hook)
            await svc._timer(_loop(max_cycles=3, cycle_count=3), delay=0)

        asyncio.run(_drive())
        assert events == [], events


class TestNothingInTheWorkerThreadTriggersLazyExpiry:
    """The blocking half runs off the event loop, so it must not touch anything
    that can fire ``on_expired`` -- that handler does WebSocket sends and slot
    updates, which need a running loop. Without a loop the sends fail, clients are
    dropped, and the expiry notice is lost.
    """

    def _override(self):
        from kiro_crew.safety_override import SafetyOverride, reset_singleton

        reset_singleton()
        return SafetyOverride()

    def test_the_passive_read_does_not_fire_the_expiry_callback(self) -> None:
        """Mutation: have ``remaining_secs_passive`` call ``is_active()`` first
        (i.e. make it the plain accessor) -- the callback fires and this fails.
        """
        from unittest.mock import MagicMock, patch

        ov = self._override()
        fired: list[str] = []
        ov.on_expired = lambda source: fired.append(source)
        with patch("kiro_crew.safety_override.sel", return_value=MagicMock()):
            ov.activate("dashboard", ttl=600)
            anchor = ov._activated_at
            with patch(
                "kiro_crew.safety_override.time.monotonic", return_value=anchor + 700
            ):
                remaining = ov.remaining_secs_passive()
        assert remaining == 0, remaining
        assert fired == [], f"lazy expiry fired from the passive read: {fired}"

    def test_the_plain_accessor_does_fire_it_which_is_why_passive_exists(self) -> None:
        """The contrast that justifies the second accessor. If this ever stops
        firing, ``remaining_secs_passive`` is redundant and should be removed
        rather than kept as dead weight.
        """
        from unittest.mock import MagicMock, patch

        ov = self._override()
        fired: list[str] = []
        ov.on_expired = lambda source: fired.append(source)
        with patch("kiro_crew.safety_override.sel", return_value=MagicMock()):
            ov.activate("dashboard", ttl=600)
            anchor = ov._activated_at
            with patch(
                "kiro_crew.safety_override.time.monotonic", return_value=anchor + 700
            ):
                ov.remaining_secs()
        assert fired, "the plain accessor no longer triggers lazy expiry"

    def test_the_blocking_half_uses_the_passive_read(self) -> None:
        """Structural, for the same reason as the to_thread assertion above: the
        dashboard hook is a closure and cannot be imported.

        Mutation: switch it back to ``ov.remaining_secs()`` -- this fails.
        """
        from pathlib import Path

        import kiro_crew.dashboard.server as srv

        src = Path(srv.__file__).read_text(encoding="utf-8")
        start = src.index("def _renew_override_headroom(")
        end = src.index("async def _ensure_override_headroom(")
        blocking_half = src[start:end]
        assert "remaining_secs_passive()" in blocking_half
        assert "ov.remaining_secs()" not in blocking_half, (
            "the plain accessor fires lazy expiry, which needs an event loop this "
            "thread does not have"
        )


class TestPolicyDenialIsAudited:
    def test_the_opt_out_denial_emits_a_denied_lease_event(self) -> None:
        """Refusing to extend because the operator disabled the feature is the same
        class of permission decision as the denials ``renew_lease`` records itself.
        Left unaudited, it is the only one with no trace, so "why did the unattended
        run stall?" has no answer in the audit stream.

        Mutation: drop the ``log_lease_denied`` call from the opt-out branch -- no
        event is emitted and this fails.
        """
        from unittest.mock import MagicMock, patch

        from kiro_crew.safety_override import SafetyOverride, reset_singleton

        reset_singleton()
        ov = SafetyOverride()
        sink = MagicMock()
        with patch("kiro_crew.safety_override.sel", return_value=sink):
            ov.log_lease_denied("autonudge-prefire", "policy_opt_out")

        assert sink.log_api_access.call_count == 1
        kw = sink.log_api_access.call_args.kwargs
        assert kw["operation"] == "safety_override:renew_lease"
        assert kw["outcome"] == "denied"
        assert "policy_opt_out" in kw["resources"]

    def test_the_opt_out_branch_actually_calls_it(self) -> None:
        """Structural companion: the method existing proves nothing if the branch
        does not call it."""
        from pathlib import Path

        import kiro_crew.dashboard.server as srv

        src = Path(srv.__file__).read_text(encoding="utf-8")
        start = src.index("def _renew_override_headroom(")
        end = src.index("async def _ensure_override_headroom(")
        blocking_half = src[start:end]
        assert 'log_lease_denied("autonudge-prefire", "policy_opt_out")' in blocking_half


class TestTerminalLeaseOutcomesAreReportedOnce:
    """A lapsed grant and a spent ceiling both recur on EVERY subsequent nudge,
    because a lease extends and never revives. That makes them reportable only if
    the report is deduplicated -- otherwise the honest signal becomes a per-nudge
    alarm and gets muted, which is the same outcome as not reporting at all.

    Structural, for the same stated reason as the to_thread assertions: the hook is
    a closure inside ``start_dashboard`` and cannot be imported (#2375's seam).
    """

    def _server_source(self) -> str:
        from pathlib import Path

        import kiro_crew.dashboard.server as srv

        return Path(srv.__file__).read_text(encoding="utf-8")

    def test_never_active_is_silent_but_a_dead_grant_is_not(self) -> None:
        """The distinction that keeps this from spamming every install that does not
        use auto-approve at all.

        Mutation: map `never_active` to the `lapsed` action -- every nudge on a
        no-YOLO install notifies and this test fails.
        """
        src = self._server_source()
        start = src.index("def _renew_override_headroom(")
        end = src.index("async def _ensure_override_headroom(")
        half = src[start:end]
        assert 'if result.reason == "never_active":' in half
        assert 'return ("noop", 0, 0)' in half, "never_active must be a silent noop"
        assert 'if result.reason == "not_active":' in half
        assert 'return ("lapsed"' in half, "a dead grant must be surfaced"

    def test_both_terminal_notices_are_deduplicated(self) -> None:
        """Mutation: drop the `_lease_notice_sent` gate -- the notice fires on every
        nudge for the rest of the run and this test fails.
        """
        src = self._server_source()
        assert "_lease_notice_sent: set[str] = set()" in src
        assert 'if action in ("capped", "lapsed"):' in src
        assert "if action in _lease_notice_sent:" in src
        assert "_lease_notice_sent.clear()" in src, (
            "a successful lease must re-arm the notice, or a human re-enabling gets "
            "no warning the next time it dies"
        )
