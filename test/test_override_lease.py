"""Tests for the automatic safety-override lease granted to armed nudge loops.

Covers three pieces that together keep an unattended loop working past its
grant's TTL without turning that grant into an unbounded one:

- ``SafetyOverride.renew_lease`` — the charged, short, machine-issued renewal,
  and its cumulative cap.
- ``SafetyOverride.is_active`` — that the caller which TRIGGERS the lapse sees
  the renewal the expiry callback performed, rather than a stale ``False``.
- ``dashboard.server._override_lease_secs`` — that the lease is derived from the
  loops' own cadence and not a constant.

Every behavioural claim below was mutation-verified: the production line was
broken deliberately, the test observed failing, then the line restored. The
mutation is named in each test so a future reader can repeat it.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.dashboard.server import _OVERRIDE_LEASE_FLOOR_SECS, _override_lease_secs
from kiro_crew.safety_override import SafetyOverride, reset_singleton


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_singleton()
    yield
    reset_singleton()


@pytest.fixture
def override() -> SafetyOverride:
    """Fresh instance bypassing the singleton, matching test_safety_override.py."""
    inst = object.__new__(SafetyOverride)
    inst._active = False
    inst._source = ""
    inst._activated_at = 0.0
    inst._expires_at = 0.0
    inst._activation_count = 0
    inst._last_renewed_at = 0.0
    inst._last_renewed_by = ""
    inst._on_expired = None
    inst._on_activated = None
    return inst


def _quiet_sel():
    """Silence the SEL sink the way the sibling suite does."""
    return patch("kiro_crew.safety_override.sel", return_value=MagicMock())


class TestLeaseDerivation:
    def test_lease_is_twice_the_slowest_loop_interval(self) -> None:
        """Derived, not constant. A lease shorter than the idle interval would
        lapse between two cycles of the very loop it is granted for.

        Mutation: return the floor unconditionally — this test fails.
        """
        assert _override_lease_secs([300, 900, 600]) == 1800

    def test_the_floor_wins_for_fast_loops(self) -> None:
        assert _override_lease_secs([60]) == _OVERRIDE_LEASE_FLOOR_SECS

    def test_the_slowest_loop_sets_the_lease_not_the_fastest(self) -> None:
        """Taking min() would leave the slow loop unprotected for the tail of
        its own interval.

        Mutation: swap max() for min() — this test fails.
        """
        assert _override_lease_secs([300, 5400]) == 10800

    def test_no_loops_means_no_lease(self) -> None:
        assert _override_lease_secs([]) == 0


def _barely_live(override, now: float) -> None:
    """Leave *override* live at ``now`` with ~1s of headroom.

    A lease only extends, so a renewal is only meaningful when the grant has LESS
    time left than the lease would give it -- which is also the only state the
    production caller renews from (it checks ``remaining < needed`` first).
    """
    override._active = True
    override._expires_at = now + 1


def _make_live_past(override, instant: float) -> None:
    """Leave *override* LIVE at monotonic ``instant``, past its automatic ceiling.

    "Live grant, spent machine ceiling" is the only state in which ``lease_cap`` is
    reachable now that a lease refuses to revive anything, and it is the honest
    statement of the policy: a human may carry a grant past the automatic bound;
    the machine may not.

    Constructed directly rather than by calling ``renew()`` at ``instant`` --
    ``renew()`` also refuses a grant that lapsed long ago, so it cannot manufacture
    this state in one step. Reality reaches it through a chain of human renewals
    each taken while the grant was still in force; the fields are set here to stand
    in for that chain without simulating hours of it.
    """
    override._active = True
    override._expires_at = instant + 1


# ─── renew_lease ────────────────────────────────────────────────────────────


class TestRenewLease:
    def test_lease_extends_a_live_grant_by_exactly_the_requested_window(
        self, override: SafetyOverride
    ) -> None:
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        now = override._activated_at + 10
        _barely_live(override, now)
        before = override._expires_at
        with _quiet_sel(), patch(
            "kiro_crew.safety_override.time.monotonic", return_value=now
        ):
            result = override.renew_lease("autonudge-lease:dashboard", 900)
        assert result.renewed is True
        assert result.ttl == 900
        # The new deadline is the lease from NOW, not the old deadline plus the
        # lease: an extension must not compound with time already granted.
        assert override._expires_at < before + 900

    def test_a_lapsed_grant_is_NOT_revived(self, override: SafetyOverride) -> None:
        """A lease extends a LIVE grant; it never brings a dead one back.

        This is stricter than the earlier "lapsed is distinguishable from stopped"
        rule, and the strictness is the point: ``deactivate()`` returns early when
        the grant is already inactive, so it leaves ``_expires_at`` positive for a
        grant that had lapsed. An operator switching back to normal mode in that
        state was indistinguishable from one waiting for a renewal, and a lease
        would have re-enabled auto-approval they had just turned off.

        Proactive callers never need revival: they renew while headroom remains.

        Mutation: accept a grant on ``_expires_at > 0`` alone -- a lapsed grant is
        revived and this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=600)
        anchor = override._activated_at
        with _quiet_sel(), patch(
            "kiro_crew.safety_override.time.monotonic", return_value=anchor + 700
        ):
            result = override.renew_lease("autonudge-prefire", 900)
            assert result.renewed is False
            assert result.reason == "not_active"
            assert override.is_active() is False

    def test_a_grant_deactivated_after_it_lapsed_is_not_revived(
        self, override: SafetyOverride
    ) -> None:
        """The exact reported hole: lazy expiry clears ``_active`` but leaves
        ``_expires_at`` positive, and ``deactivate()`` then no-ops -- so the
        operator's "switch to normal mode" left no trace the lease could see.

        Mutation: test only ``_expires_at``/``_activated_at`` instead of liveness --
        the deactivated grant is revived and this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=600)
        anchor = override._activated_at
        with _quiet_sel(), patch(
            "kiro_crew.safety_override.time.monotonic", return_value=anchor + 700
        ):
            override.is_active()  # trip lazy expiry: _active False, _expires_at kept
            assert override._expires_at > 0, "precondition: the stale timestamp remains"
            override.deactivate("dashboard")  # no-ops, so _expires_at stays positive
            assert override._expires_at > 0, "precondition: deactivate did not zero it"
            result = override.renew_lease("autonudge-prefire", 900)
        assert result.renewed is False
        assert result.reason == "not_active"
        assert override.is_active() is False

    def test_a_failed_sel_audit_denies_the_lease_and_leaves_it_inactive(
        self, override: SafetyOverride
    ) -> None:
        """No extension of auto-approval authority without a trace of it.

        This path needs the fail-closed audit MORE than a human renewal does, not
        less: nobody is present to notice that tools kept being auto-approved. A
        swallowed audit error would leave the grant live and unrecorded.

        The failure is injected at the SEL WRITE, not at ``_log_sel`` — patching
        ``_log_sel`` itself would raise whether or not ``critical=True`` was
        passed, so it would assert the try/except while silently accepting the
        loss of the flag that makes the re-raise happen at all. Mutation-checked
        both ways.

        Mutation: drop `critical=True`, or make the except body fall through —
        the lease is granted anyway and this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=600)
        anchor = override._activated_at
        before_expiry = override._expires_at
        broken = MagicMock()
        broken.log_api_access.side_effect = RuntimeError("SEL disk full")
        # Inside the TTL, so the grant is live and the audit is what decides.
        with patch(
            "kiro_crew.safety_override.time.monotonic", return_value=anchor + 60
        ), patch("kiro_crew.safety_override.sel", return_value=broken):
            result = override.renew_lease("autonudge-lease:dashboard", 900)
        assert result.renewed is False
        assert result.reason == "audit_failed"
        # The decisive half: refusing must not have applied the lease. The grant
        # was live before the call and stays live on its OWN deadline -- what must
        # not happen is the deadline moving.
        assert override._expires_at == pytest.approx(before_expiry, abs=0.01)

    def test_a_deactivate_racing_the_audit_is_not_undone_by_the_lease(
        self, override: SafetyOverride
    ) -> None:
        """The audit runs with the lock RELEASED (a SEL write must not be done
        under it), so a ``deactivate`` landing in that window would otherwise be
        reversed by a lease authorized against the pre-deactivate state.

        Simulated by deactivating from inside the audit call — the one moment the
        lock is not held. The pre-audit guard cannot catch this; only the
        re-verify inside the commit lock can.

        Mutation: drop the re-verify in the commit block — the deactivated grant
        comes back live and this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)

        real_sel = _quiet_sel

        def _deactivate_mid_audit(*_a, **_k):
            with real_sel():
                override.deactivate("dashboard")

        with _quiet_sel():
            with patch.object(
                override, "_log_sel", side_effect=_deactivate_mid_audit
            ):
                result = override.renew_lease("autonudge-lease:dashboard", 900)
        assert result.renewed is False
        assert result.reason == "not_active"
        assert override.is_active() is False

    def test_a_concurrent_activation_is_not_overwritten_by_a_stale_lease(
        self, override: SafetyOverride
    ) -> None:
        """The bug this guards against was introduced BY the fail-closed audit:
        moving the audit before the commit opened a window in which a fresh
        activation can land, and the original re-verify only rejected an
        explicitly deactivated grant — a re-activated one has both fields
        positive, so it sailed through and a multi-hour grant got overwritten with
        one short lease.

        Simulated by activating from inside the audit call, the one moment the
        lock is not held.

        Mutation: drop the generation comparison (keep only the `<= 0` check) —
        the fresh grant's expiry is replaced and this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)

        def _reactivate_mid_audit(*_a, **_k):
            # One-shot: activate() itself calls _log_sel, so re-entering here
            # would recurse forever (the first draft of this test hung).
            if fired:
                return
            fired.append(True)
            with _quiet_sel():
                override.activate("dashboard", ttl=SafetyOverride._MAX_TTL)

        fired: list[bool] = []
        with _quiet_sel():
            with patch.object(override, "_log_sel", side_effect=_reactivate_mid_audit):
                result = override.renew_lease("autonudge-lease:dashboard", 900)
            fresh_expiry = override._expires_at

        assert result.renewed is False
        assert result.reason == "raced"
        # The decisive assertion: the fresh grant still has its own long deadline,
        # not the 900s lease that was authorized against the grant it replaced.
        assert fresh_expiry - time.monotonic() > 3600
        assert override.is_active() is True

    def test_a_concurrent_human_renew_is_not_overwritten_by_a_stale_lease(
        self, override: SafetyOverride
    ) -> None:
        """The expiry half of the generation guard, which the activation test
        cannot reach: ``activate()`` moves BOTH fields, so comparing only
        ``_activated_at`` would still pass that test. A human ``renew()`` moves
        only the expiry — and overwriting it would silently cut an operator's
        fresh 6h refill down to one 900s lease.

        Mutation: compare only `_activated_at` — this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)

        fired: list[bool] = []

        def _human_renew_mid_audit(*_a, **_k):
            if fired:
                return
            fired.append(True)
            with _quiet_sel():
                override.renew("dashboard")

        with _quiet_sel():
            with patch.object(override, "_log_sel", side_effect=_human_renew_mid_audit):
                result = override.renew_lease("autonudge-lease:dashboard", 900)
            renewed_expiry = override._expires_at

        assert result.renewed is False
        assert result.reason == "raced"
        # renew() refills with the configured ad-hoc TTL (6h by default), so the
        # operator's deadline must still be far beyond the 900s lease.
        assert renewed_expiry - time.monotonic() > 3600
        assert override.is_active() is True

    def test_a_lease_that_fits_is_granted_exactly_on_any_clock_fraction(
        self, override: SafetyOverride
    ) -> None:
        """Regression: the grant was computed as
        ``int(min(now + lease, ceiling) - now)``, and for some clock values
        ``(now + 900.0) - now`` is 899.9999999999999, which int() truncates to
        899. It passed on this workstation and failed on three CI platforms at
        once, because which fractional bits you get depends on the host's uptime.

        Sweeps deliberately awkward fractions instead of trusting the ambient
        clock, so the assertion does not depend on the machine it runs on.

        Mutation: restore the float subtraction — at least one fraction below
        yields 899 and this test fails.
        """
        for frac in (0.1, 0.3, 0.7, 0.123456789, 0.987654321, 1 / 3, 2 / 3):
            # Set the anchor to a chosen POSITIVE magnitude rather than derive it
            # from the ambient clock: the whole point is that CI (small monotonic,
            # fresh boot) and this box (millions) produce different float bits, so
            # the test must pin the magnitude instead of inheriting it. Never by
            # subtraction — a negative instant trips the "never activated" guard.
            for anchor in (300.0, 1e6, 1e7):
                with _quiet_sel():
                    override.activate("dashboard", ttl=3600)
                override._activated_at = anchor
                override._active = True
                # Barely live, so the full 900s lease is an extension.
                override._expires_at = anchor + 10 + frac + 1
                with _quiet_sel(), patch(
                    "kiro_crew.safety_override.time.monotonic",
                    return_value=anchor + 10 + frac,
                ):
                    result = override.renew_lease("autonudge-lease:dashboard", 900)
                assert result.ttl == 900, f"anchor={anchor} frac={frac} gave {result.ttl}"

    def test_sub_second_headroom_is_refused_not_granted_as_a_zero_lease(
        self, override: SafetyOverride
    ) -> None:
        """``now_mono >= ceiling`` let a fraction of a second through, which the
        int arithmetic would then floor to a 0s lease — reported as renewed while
        expiring immediately. Refuse instead.

        Mutation: test `remaining < 0` instead of `remaining <= 0` — a 0s lease is
        granted and this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        anchor = override._activated_at
        near = anchor + 4 * 3600 - 0.4
        with _quiet_sel():
            _make_live_past(override, near)
        with _quiet_sel(), patch(
            "kiro_crew.safety_override.time.monotonic", return_value=near
        ):
            result = override.renew_lease("autonudge-lease:dashboard", 900)
        assert result.renewed is False
        assert result.reason == "lease_cap"

    def test_a_refused_commit_is_never_audited_as_a_renewal(
        self, override: SafetyOverride
    ) -> None:
        """The audit must not over-report. Auditing before the commit is required
        (round 2: a failed audit must not leave authority live), but the commit can
        still be refused — so a pre-commit event saying "renewed" would put a
        renewal in the log that never happened, which is the one thing an auditor
        consults it for.

        Contract: ``authorized`` before, then exactly one of ``renewed`` /
        ``aborted``. On a refused commit, no ``renewed`` event exists.

        Mutation: change the pre-commit outcome back to "renewed" — a renewal is
        recorded for a lease that was refused and this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)

        fired: list[bool] = []
        seen: list[str] = []

        def _record_then_race(*_a, **kw):
            # Only this method's own events: the nested activate() below runs
            # through the same patched _log_sel and emits its own "enabled".
            if kw.get("operation") == "safety_override:renew_lease":
                seen.append(kw.get("outcome", ""))
            if fired:
                return
            fired.append(True)
            with _quiet_sel():
                override.activate("dashboard", ttl=SafetyOverride._MAX_TTL)

        with _quiet_sel():
            with patch.object(override, "_log_sel", side_effect=_record_then_race):
                result = override.renew_lease("autonudge-lease:dashboard", 900)

        assert result.renewed is False
        assert result.reason == "raced"
        assert "renewed" not in seen, f"audit claimed a renewal that did not happen: {seen}"
        assert seen == ["authorized", "aborted"], seen

    def test_a_committed_lease_is_audited_as_renewed_after_the_fact(
        self, override: SafetyOverride
    ) -> None:
        """The other half: a lease that DOES commit must still produce a
        ``renewed`` record, so the pre-commit ``authorized`` event is resolved
        rather than left dangling.

        Mutation: drop the post-commit renewed event — this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        now = override._activated_at + 10
        _barely_live(override, now)
        seen: list[str] = []
        with _quiet_sel(), patch(
            "kiro_crew.safety_override.time.monotonic", return_value=now
        ):
            with patch.object(
                override, "_log_sel", side_effect=lambda **kw: seen.append(kw.get("outcome", ""))
            ):
                result = override.renew_lease("autonudge-lease:dashboard", 900)
        assert result.renewed is True
        assert seen == ["authorized", "renewed"], seen

    def test_a_grant_that_expires_during_the_audit_is_not_revived(
        self, override: SafetyOverride
    ) -> None:
        """Every commit-block decision is re-derived from a FRESHLY read clock.

        The SEL write happens with the lock released and takes real time. Reusing
        the pre-audit instant let a grant that expired during that write still read
        as live, and then installed a deadline measured from an instant already in
        the past -- reviving expired auto-approval.

        Simulated by advancing the patched clock from inside the audit call, which
        is exactly the window the real write occupies.

        Mutation: pass the pre-audit `now_mono` to the commit guard instead of
        re-reading the clock -- the stale instant reports live and this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=600)
        anchor = override._activated_at
        clock = {"now": anchor + 300}  # live: 300s in, 300s left
        fired: list[bool] = []

        def _now() -> float:
            return clock["now"]

        def _expire_mid_audit(*_a, **_k):
            if fired:
                return
            fired.append(True)
            clock["now"] = anchor + 601  # the grant lapses during the SEL write

        with patch("kiro_crew.safety_override.time.monotonic", side_effect=_now):
            with patch.object(override, "_log_sel", side_effect=_expire_mid_audit):
                result = override.renew_lease("autonudge-prefire", 900)

        assert result.renewed is False
        assert result.reason == "not_active"
        with patch("kiro_crew.safety_override.time.monotonic", side_effect=_now):
            with _quiet_sel():
                assert override.is_active() is False

    def test_a_lease_never_shortens_a_human_renewed_grant(
        self, override: SafetyOverride
    ) -> None:
        """A human ``renew()`` can carry a grant past the automatic ceiling, so the
        ceiling-truncated lease can be SHORTER than the deadline the operator
        already has. Installing it would cut their grant short in the name of
        extending it.

        Mutation: drop the `new_deadline <= self._expires_at` check -- the
        operator's later deadline is replaced by the earlier ceiling and this
        test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        anchor = override._activated_at
        ceiling = anchor + 4 * 3600
        # Operator's deadline sits 600s out; only 300s of machine ceiling remains,
        # so a lease could grant at most 300s -- earlier than what they have.
        now = ceiling - 300
        override._active = True
        override._expires_at = now + 600
        human_deadline = override._expires_at

        with _quiet_sel(), patch(
            "kiro_crew.safety_override.time.monotonic", return_value=now
        ):
            result = override.renew_lease("autonudge-prefire", 900)

        assert result.renewed is False
        assert result.reason == "no_extension"
        assert override._expires_at == pytest.approx(human_deadline, abs=0.01), (
            "the lease moved the deadline backwards"
        )

    def test_the_installed_deadline_is_measured_from_the_commit_clock(
        self, override: SafetyOverride
    ) -> None:
        """The SEL write takes real time, and the grant can still be live when it
        finishes. The deadline must then be measured from the instant the commit
        happens, not from the instant the decision was made -- otherwise every
        lease silently loses however long the audit took, and a slow write installs
        a deadline that is already partly spent.

        The advance here is large so the loss is unmistakable rather than a
        rounding artefact.

        Mutation: compute `new_deadline` (or `granted`) from the pre-audit
        `now_mono` instead of `commit_now` -- the deadline lands ~500s early and
        this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        anchor = override._activated_at
        start = anchor + 10
        override._active = True
        # Live at start AND still live 500s later, but with only ~1s of headroom
        # then -- so the lease is a real extension at the moment of commit.
        override._expires_at = start + 501
        clock = {"now": start}
        fired: list[bool] = []

        def _now() -> float:
            return clock["now"]

        def _slow_audit(*_a, **_k):
            if fired:
                return
            fired.append(True)
            clock["now"] = start + 500  # still inside the 3600s grant

        with patch("kiro_crew.safety_override.time.monotonic", side_effect=_now):
            with patch.object(override, "_log_sel", side_effect=_slow_audit):
                result = override.renew_lease("autonudge-prefire", 900)

        assert result.renewed is True
        assert override._expires_at == pytest.approx(start + 500 + 900, abs=1), (
            "the deadline was measured from the pre-audit clock, losing the audit's "
            "duration"
        )

    def test_a_lease_landing_exactly_on_the_existing_deadline_is_refused(
        self, override: SafetyOverride
    ) -> None:
        """Equality changes nothing, so granting it would be a lie in the audit
        stream: a ``renewed`` event and a bumped ``has_been_renewed()`` for a lease
        that moved no deadline. The comparison is ``<=`` for that reason, not for a
        timing one.

        Mutation: use `<` instead of `<=` -- the no-op lease is reported as renewed
        and this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        now = override._activated_at + 10
        override._active = True
        override._expires_at = now + 900  # exactly what a 900s lease would give
        seen: list[str] = []

        with patch("kiro_crew.safety_override.time.monotonic", return_value=now):
            with patch.object(
                override, "_log_sel", side_effect=lambda **kw: seen.append(kw.get("outcome", ""))
            ):
                result = override.renew_lease("autonudge-prefire", 900)

        assert result.renewed is False
        assert result.reason == "no_extension"
        assert "renewed" not in seen, f"a no-op lease was audited as renewed: {seen}"
        assert override.has_been_renewed() is False

    def test_a_slow_audit_cannot_push_the_deadline_past_the_ceiling(
        self, override: SafetyOverride
    ) -> None:
        """``granted`` is re-derived under the commit lock, not carried over from
        the pre-audit decision. Otherwise an audit that spans the moment the ceiling
        comes into range installs the amount that fitted BEFORE the write and
        overshoots the ceiling -- which is the security bound of the whole feature,
        so it is the one number that must never be stale.

        Set up so exactly one lease fits at decision time, then let the audit
        consume most of it.

        Mutation: drop the `granted = min(lease, remaining)` re-derivation -- the
        pre-audit 900s is installed and the deadline lands past the ceiling.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        anchor = override._activated_at
        ceiling = anchor + 4 * 3600
        start = ceiling - 900  # exactly one full lease fits, at decision time
        override._active = True
        override._expires_at = start + 501  # live through the audit, low headroom
        clock = {"now": start}
        fired: list[bool] = []

        def _now() -> float:
            return clock["now"]

        def _slow_audit(*_a, **_k):
            if fired:
                return
            fired.append(True)
            clock["now"] = start + 500  # only 400s of ceiling left now

        with patch("kiro_crew.safety_override.time.monotonic", side_effect=_now):
            with patch.object(override, "_log_sel", side_effect=_slow_audit):
                result = override.renew_lease("autonudge-prefire", 900)

        assert result.renewed is True
        assert result.ttl == 400, f"granted was not re-derived: {result.ttl}"
        assert override._expires_at <= ceiling + 0.01, (
            "the lease pushed the deadline past the ceiling"
        )

    def test_a_never_activated_override_is_refused_as_never_active(
        self, override: SafetyOverride
    ) -> None:
        """Separated from ``not_active`` because only one of them is an event.

        "No grant has ever existed" is the ordinary state of every install that does
        not use auto-approve. Reporting it the same way as "your grant just died
        mid-run" would notify those operators on every single nudge, forever.

        Mutation: collapse the two into one `not_active` -- the caller can no longer
        tell the ordinary state from the terminal one and this test fails.
        """
        with _quiet_sel():
            result = override.renew_lease("autonudge-prefire", 900)
        assert result.renewed is False
        assert result.reason == "never_active"

    def test_a_grant_that_existed_and_died_is_refused_as_not_active(
        self, override: SafetyOverride
    ) -> None:
        """The other half of the same distinction: this one IS an event, and it is
        reachable without anyone deactivating anything -- a turn can outrun the lease
        that covered it and lapse mid-turn.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=600)
        anchor = override._activated_at
        with _quiet_sel(), patch(
            "kiro_crew.safety_override.time.monotonic", return_value=anchor + 700
        ):
            result = override.renew_lease("autonudge-prefire", 900)
        assert result.renewed is False
        assert result.reason == "not_active"

    def test_a_non_positive_lease_is_refused(self, override: SafetyOverride) -> None:
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
            assert override.renew_lease("x", 0).reason == "invalid_ttl"
            assert override.renew_lease("x", -5).reason == "invalid_ttl"

    def test_a_permanent_grant_is_left_alone(self, override: SafetyOverride) -> None:
        """Installing a finite deadline on a declared grant would be a silent
        downgrade of the operator's standing decision.

        Mutation: remove the `_permanent` early return — this test fails
        because _expires_at moves.
        """
        with _quiet_sel():
            override.activate_declared()
            before = override._expires_at
            result = override.renew_lease("autonudge-lease:config", 900)
        assert result.renewed is True
        assert result.ttl == -1
        assert override._expires_at == before

    def test_successive_leases_do_not_grow(self, override: SafetyOverride) -> None:
        """Exponential backoff would enlarge the unsupervised window exactly as
        the run gets longer. Each renewal must re-ask over the same horizon.
        """
        granted = []
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
            base = override._activated_at
            # Re-establish barely-live between calls: each granted lease leaves
            # 900s of headroom, and the next lease would then not extend.
            for i in range(4):
                now = base + 10 * (i + 1)
                _barely_live(override, now)
                with patch("kiro_crew.safety_override.time.monotonic", return_value=now):
                    granted.append(override.renew_lease("autonudge-lease:dashboard", 900).ttl)
        assert granted == [900, 900, 900, 900]

    def test_the_ceiling_is_anchored_on_activation_not_on_extension_volume(
        self, override: SafetyOverride
    ) -> None:
        """A cumulative extension BUDGET was the first shape and it was wrong: a
        24h activation plus 24h of leases authorizes 48h on one human decision,
        which is what _MAX_TTL exists to forbid. Anchoring on _activated_at makes
        the bound absolute.

        Mutation: anchor the ceiling on `now_mono` instead of `_activated_at` —
        this test fails because the lease is granted.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=SafetyOverride._MAX_TTL)
            # Advance a patched clock past the ceiling rather than back-dating
            # the activation: monotonic counts from boot, so subtracting 24h on a
            # fresh CI runner yields a NEGATIVE instant and trips the
            # "never activated" sentinel instead of the ceiling under test.
            anchor = override._activated_at
            past = anchor + SafetyOverride._MAX_TTL + 1
            _make_live_past(override, past)
            with patch(
                "kiro_crew.safety_override.time.monotonic", return_value=past
            ):
                result = override.renew_lease("autonudge-lease:dashboard", 900)
        assert result.renewed is False
        assert result.reason == "lease_cap"

    def test_a_lease_is_truncated_to_land_exactly_on_the_ceiling(
        self, override: SafetyOverride
    ) -> None:
        """Truncate rather than refuse when only part of the lease fits: refusing
        would drop the grant early for no gain, and the ceiling still holds
        exactly.

        Mutation: grant the full requested lease instead of the truncated one —
        this test fails because ttl becomes 900.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=21600)
            anchor = override._activated_at
            # 100s of headroom left against the ceiling (6h TTL x4 = the 24h cap).
            near = anchor + SafetyOverride._MAX_TTL - 100
            _make_live_past(override, near)
            with patch(
                "kiro_crew.safety_override.time.monotonic", return_value=near
            ):
                result = override.renew_lease("autonudge-lease:dashboard", 900)
        assert result.renewed is True
        assert result.ttl == 100

    def test_many_leases_cannot_walk_past_the_ceiling(self, override: SafetyOverride) -> None:
        """The bound is a deadline, not a budget, so it cannot be walked down in
        ever-smaller retries — the property a cumulative budget lacked.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
            anchor = override._activated_at
            ceiling = anchor + SafetyOverride._MAX_TTL
            with patch(
                "kiro_crew.safety_override.time.monotonic",
                return_value=ceiling - 50,
            ):
                for _ in range(20):
                    override.renew_lease("autonudge-lease:dashboard", 900)
        assert override._expires_at <= ceiling

    def test_the_renew_grace_window_is_irrelevant_because_nothing_is_revived(
        self, override: SafetyOverride
    ) -> None:
        """``renew``'s 300s grace window has no analogue here: a lease never
        renews past expiry at all, so how far past the deadline the caller arrives
        cannot matter. Recorded as a test because the previous design DID depend on
        that distance, and a future reader will otherwise wonder why the window is
        unused.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        anchor = override._activated_at
        for past_expiry in (1, 60, 3000):  # inside, at, and far beyond the window
            with _quiet_sel(), patch(
                "kiro_crew.safety_override.time.monotonic",
                return_value=anchor + 3600 + past_expiry,
            ):
                result = override.renew_lease("autonudge-prefire", 900)
            assert result.renewed is False, past_expiry
            assert result.reason == "not_active", past_expiry

    def test_an_explicitly_deactivated_grant_is_never_revived(
        self, override: SafetyOverride
    ) -> None:
        """Dropping the grace requirement must not make a deliberate stop
        reversible. deactivate() zeroes _expires_at, which is the discriminator.

        Mutation: drop the `_expires_at <= 0` guard — this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
            override.deactivate("dashboard")
            result = override.renew_lease("autonudge-lease:dashboard", 900)
        assert result.renewed is False
        assert result.reason == "not_active"

    def test_has_been_renewed_distinguishes_the_first_lease(
        self, override: SafetyOverride
    ) -> None:
        """Drives the notify-once-per-grant decision, so it must be false before
        the first lease and true after."""
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        assert override.has_been_renewed() is False
        now = override._activated_at + 10
        _barely_live(override, now)
        with _quiet_sel(), patch(
            "kiro_crew.safety_override.time.monotonic", return_value=now
        ):
            assert override.renew_lease("autonudge-lease:dashboard", 900).renewed is True
        assert override.has_been_renewed() is True

    def test_a_new_activation_clears_the_renewed_marker(self, override: SafetyOverride) -> None:
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
            override.renew_lease("autonudge-lease:dashboard", 900)
            override.activate("dashboard", ttl=3600)
        assert override.has_been_renewed() is False

    def test_ceiling_remaining_is_minus_one_for_a_permanent_grant(
        self, override: SafetyOverride
    ) -> None:
        with _quiet_sel():
            override.activate_declared()
        assert override.lease_ceiling_remaining_secs() == -1


# ─── proportional lease ceiling ─────────────────────────────────────────────


class TestProportionalCeiling:
    """The ceiling scales with the TTL the operator chose, capped at _MAX_TTL.

    A flat _MAX_TTL erased the operator's judgement: a deliberately tight 1h grant
    was softened to 24h the moment any loop was armed, landing in the same place
    as a 6h grant. The cautious setting bought nothing.
    """

    def test_a_tight_grant_stays_proportionally_tight(
        self, override: SafetyOverride
    ) -> None:
        """1h chosen -> 4h ceiling, NOT 24h.

        Mutation: use `self._MAX_TTL` as the span instead of the scaled one — the
        ceiling becomes 24h and this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        assert override.lease_ceiling_remaining_secs() == pytest.approx(4 * 3600, abs=5)

    def test_the_default_six_hour_grant_still_reaches_the_24h_cap(
        self, override: SafetyOverride
    ) -> None:
        """The common case is unchanged: 6h x 4 = exactly the 24h cap, so nobody
        on the default loses anything from the change."""
        with _quiet_sel():
            override.activate("dashboard", ttl=21600)
        assert override.lease_ceiling_remaining_secs() == pytest.approx(
            SafetyOverride._MAX_TTL, abs=5
        )

    def test_the_cap_still_bounds_a_long_grant(self, override: SafetyOverride) -> None:
        """A 24h activation must not scale to 96h.

        Mutation: drop the `min(_MAX_TTL, ...)` — this test fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=SafetyOverride._MAX_TTL)
        assert override.lease_ceiling_remaining_secs() == pytest.approx(
            SafetyOverride._MAX_TTL, abs=5
        )

    def test_a_tight_grant_is_refused_past_its_scaled_ceiling(
        self, override: SafetyOverride
    ) -> None:
        """The scaled ceiling actually denies, rather than only being reported."""
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        anchor = override._activated_at
        past = anchor + 4 * 3600 + 1
        with _quiet_sel():
            _make_live_past(override, past)
        with _quiet_sel(), patch(
            "kiro_crew.safety_override.time.monotonic", return_value=past
        ):
            result = override.renew_lease("autonudge-prefire", 900)
        assert result.renewed is False
        assert result.reason == "lease_cap"

    def test_the_ceiling_is_frozen_at_activation_not_re_read_from_config(
        self, override: SafetyOverride
    ) -> None:
        """Storing the activation TTL rather than re-reading it means a later
        config edit cannot retroactively widen a grant that is already live.

        Mutation: recompute the span from `self._adhoc_ttl` — this test fails once
        the configured default is changed underneath the live grant.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=3600)
        override._adhoc_ttl = SafetyOverride._MAX_TTL  # operator edits config after
        assert override.lease_ceiling_remaining_secs() == pytest.approx(4 * 3600, abs=5)


# ─── is_active after an expiry callback ─────────────────────────────────────


class TestIsActiveAfterExpiryCallback:
    def test_a_renewal_inside_the_callback_is_visible_to_the_triggering_caller(
        self, override: SafetyOverride
    ) -> None:
        """The caller that trips lazy expiry is frequently a tool-approval
        check. Returning a hardcoded False there produced one unexplained
        approval prompt right after the boundary and normal operation
        afterwards — a near-unattributable flake.

        The renewal here is a HUMAN ``renew()``, which is the only kind that can
        happen at this point: a lease refuses a grant that is no longer live, so it
        cannot be the renewer. That makes this fix independent of the lease feature
        rather than a supporting part of it.

        Mutation: restore `return False` at the end of is_active() — this test
        fails.
        """
        with _quiet_sel():
            override.activate("dashboard", ttl=1)

        def _renew_on_expiry(_source: str) -> None:
            with _quiet_sel():
                override.renew("dashboard")

        override._on_expired = _renew_on_expiry
        override._expires_at = time.monotonic() - 1
        with _quiet_sel():
            assert override.is_active() is True

    def test_without_a_renewal_the_expiry_still_reports_inactive(
        self, override: SafetyOverride
    ) -> None:
        """The re-read must not paper over a real lapse."""
        with _quiet_sel():
            override.activate("dashboard", ttl=1)
        override._on_expired = MagicMock()
        override._expires_at = time.monotonic() - 1
        with _quiet_sel():
            assert override.is_active() is False
        override._on_expired.assert_called_once()

    def test_a_callback_that_raises_still_reports_inactive(
        self, override: SafetyOverride
    ) -> None:
        with _quiet_sel():
            override.activate("dashboard", ttl=1)
        override._on_expired = MagicMock(side_effect=RuntimeError("boom"))
        override._expires_at = time.monotonic() - 1
        with _quiet_sel():
            assert override.is_active() is False
