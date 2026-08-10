"""Per-run auto-approve authorization for an armed AutoNudge loop.

The behaviour under test is a security boundary, so the tests are written around
the properties that make the feature defensible rather than around its plumbing:
the window is a closed set, the grant is scoped rather than session-wide, it is
never extended, and it is handed back on every path that ends a run.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.autonudge_grant import (
    AUTHORIZED_WINDOWS,
    authorize_run,
    release_run_grant,
    run_grant_scope,
)
from kiro_crew.safety_override import ActivationResult


def _granted_result() -> ActivationResult:
    """A REAL ``ActivationResult``, not a mock with an invented attribute.

    A ``MagicMock(activated=True)`` double answers any attribute, so a production
    read of the wrong field name still looks granted here while always refusing
    in the live path. Constructing the real dataclass makes the field name part
    of what these tests verify.
    """
    return ActivationResult(active=True, ttl=3600, source="dashboard", activated_at_iso="")


class TestTheWindowIsAClosedSet:
    """An operator picks from offered values; nothing mints its own duration."""

    def test_an_offered_window_is_granted(self) -> None:
        so = MagicMock()
        so.is_scope_active.return_value = False  # nothing granted yet
        so.activate_scoped.return_value = _granted_result()
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            with patch("kiro_crew.autonudge_grant.sel", return_value=MagicMock()):
                assert authorize_run("chat-1-x", AUTHORIZED_WINDOWS[0], source="dashboard")
        assert so.activate_scoped.call_args.kwargs["ttl"] == AUTHORIZED_WINDOWS[0]

    @pytest.mark.parametrize("bad", [1, 3599, 86400, 999999, 0, -3600])
    def test_a_window_outside_the_offer_is_refused_not_clamped(self, bad: int) -> None:
        """Mutation: clamp instead of refuse -- a caller could then name any
        duration and receive the nearest legal one, which is a grant it was never
        offered.
        """
        so = MagicMock()
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            with patch("kiro_crew.autonudge_grant.sel", return_value=MagicMock()):
                assert authorize_run("chat-1-x", bad, source="dashboard") is False
        so.activate_scoped.assert_not_called()

    def test_a_refused_window_is_audited(self) -> None:
        sink = MagicMock()
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=MagicMock()):
            with patch("kiro_crew.autonudge_grant.sel", return_value=sink):
                authorize_run("chat-1-x", 99, source="dashboard")
        assert sink.log_api_access.call_args.kwargs["outcome"] == "denied"

    def test_the_longest_window_stays_under_the_day_ceiling(self) -> None:
        """The offer must not lean on the 24h hard cap: a run authorized for the
        ceiling is indistinguishable from leaving auto-approve on.
        """
        assert max(AUTHORIZED_WINDOWS) < 86400

    def test_a_blank_slot_key_grants_nothing(self) -> None:
        so = MagicMock()
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            assert authorize_run("", AUTHORIZED_WINDOWS[0], source="dashboard") is False
        so.activate_scoped.assert_not_called()


class TestTheGrantIsScopedNotSessionWide:
    def test_it_never_flips_the_global_override(self) -> None:
        """Mutation: use activate() instead of activate_scoped() -- the run's
        window would become every session's window.
        """
        so = MagicMock()
        so.is_scope_active.return_value = False  # nothing granted yet
        so.activate_scoped.return_value = _granted_result()
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            with patch("kiro_crew.autonudge_grant.sel", return_value=MagicMock()):
                authorize_run("chat-1-x", AUTHORIZED_WINDOWS[0], source="dashboard")
        so.activate.assert_not_called()
        so.activate_declared.assert_not_called()
        so.activate_scoped.assert_called_once()

    def test_two_sessions_do_not_share_a_scope(self) -> None:
        assert run_grant_scope("chat-1-x") != run_grant_scope("chat-2-y")

    def test_the_scope_is_derived_from_the_slot_key(self) -> None:
        assert "chat-1-x" in run_grant_scope("chat-1-x")


class TestTheGrantIsNeverExtended:
    def test_authorizing_does_not_renew(self) -> None:
        """The deadline is fixed when the operator chooses it. A renew path is
        what the security review objects to, so its absence is asserted rather
        than left to reviewer memory.
        """
        so = MagicMock()
        so.is_scope_active.return_value = False
        so.activate_scoped.return_value = _granted_result()
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            with patch("kiro_crew.autonudge_grant.sel", return_value=MagicMock()):
                authorize_run("chat-1-x", AUTHORIZED_WINDOWS[1], source="dashboard")
        so.renew_scoped.assert_not_called()
        so.renew.assert_not_called()

    def test_the_module_contains_no_renewal_call(self) -> None:
        """A future edit that slides the deadline forward on activity fails here.

        Asserted on the module rather than through behaviour because the defect
        is the EXISTENCE of a call this module never makes -- there is no input
        that provokes it today.
        """
        from pathlib import Path

        import kiro_crew.autonudge_grant as mod

        src = Path(mod.__file__).read_text(encoding="utf-8")
        body = src.split('"""', 2)[-1]  # skip the module docstring
        assert "renew_scoped(" not in body
        assert ".renew(" not in body


class TestTheGrantIsHandedBack:
    def test_releasing_deactivates_the_scope(self) -> None:
        so = MagicMock()
        so.is_scope_active.return_value = True
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            with patch("kiro_crew.autonudge_grant.sel", return_value=MagicMock()):
                release_run_grant("chat-1-x", reason="cycle_cap")
        so.deactivate_scope.assert_called_once_with(run_grant_scope("chat-1-x"))

    def test_releasing_an_ungranted_run_is_silent(self) -> None:
        """Most loops are never authorized. An event per ordinary stop would bury
        the ones that mattered.
        """
        so = MagicMock()
        so.is_scope_active.return_value = False
        sink = MagicMock()
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            with patch("kiro_crew.autonudge_grant.sel", return_value=sink):
                release_run_grant("chat-1-x", reason="manual")
        sink.log_api_access.assert_not_called()

    def test_a_real_release_is_audited_with_its_reason(self) -> None:
        so = MagicMock()
        so.is_scope_active.return_value = True
        sink = MagicMock()
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            with patch("kiro_crew.autonudge_grant.sel", return_value=sink):
                release_run_grant("chat-1-x", reason="runtime_budget")
        assert "runtime_budget" in sink.log_api_access.call_args.kwargs["resources"]

    def test_a_failing_release_does_not_raise_into_the_stop_path(self) -> None:
        """A loop MUST still finish stopping when the grant store misbehaves."""
        so = MagicMock()
        so.is_scope_active.side_effect = RuntimeError("boom")
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            with patch("kiro_crew.autonudge_grant.sel", return_value=MagicMock()):
                release_run_grant("chat-1-x", reason="manual")  # must not raise


class TestEveryTerminalTransitionReleases:
    """The release must sit on the transitions themselves, not on a sweeper.

    These drive the real AutoNudgeService so a new terminal path that forgets to
    release fails here rather than leaking a live grant into an ended run.
    """

    def _svc(self, tmp_path):
        from kiro_crew.autonudge import AutoNudgeService

        return AutoNudgeService(base_dir=tmp_path)

    def test_deactivating_a_loop_releases_its_grant(self, tmp_path) -> None:
        async def _go() -> None:
            svc = self._svc(tmp_path)
            loop = await svc.add(
                slot_key="chat-9-z", message="check", idle_secs=60, max_cycles=3
            )
            with patch("kiro_crew.autonudge.release_run_grant") as rel:
                await svc.update(loop.id, active=False, stopped_reason="cycle_cap")
            rel.assert_called_once()
            assert rel.call_args.args[0] == "chat-9-z"

        asyncio.run(_go())

    def test_removing_a_loop_releases_its_grant(self, tmp_path) -> None:
        async def _go() -> None:
            svc = self._svc(tmp_path)
            loop = await svc.add(
                slot_key="chat-9-z", message="check", idle_secs=60, max_cycles=3
            )
            with patch("kiro_crew.autonudge.release_run_grant") as rel:
                await svc.remove(loop.id)
            rel.assert_called_once()
            assert rel.call_args.args[0] == "chat-9-z"

        asyncio.run(_go())

    def test_reviving_a_loop_does_not_release(self, tmp_path) -> None:
        """Resuming a paused loop must not hand back a grant the operator may
        have just authorized for it.
        """
        async def _go() -> None:
            svc = self._svc(tmp_path)
            loop = await svc.add(
                slot_key="chat-9-z", message="check", idle_secs=60, max_cycles=3
            )
            await svc.update(loop.id, active=False)
            with patch("kiro_crew.autonudge.release_run_grant") as rel:
                await svc.update(loop.id, active=True)
            rel.assert_not_called()

        asyncio.run(_go())


class TestReArmingASlotDropsThePreviousRunsWindow:
    """One loop per slot: `_add_locked` replaces via `remove_sync`.

    The replacement is a DIFFERENT run — new goal, new cycle budget — so it must
    not inherit a window the operator granted to the run it displaced. Dropping is
    the fail-safe direction; the point of the distinct reason is that the drop is
    legible in the audit rather than silent.
    """

    def _svc(self, tmp_path):
        from kiro_crew.autonudge import AutoNudgeService

        return AutoNudgeService(base_dir=tmp_path)

    def test_re_arming_releases_the_previous_grant(self, tmp_path) -> None:
        async def _go() -> None:
            svc = self._svc(tmp_path)
            await svc.add(slot_key="chat-9-z", message="first", idle_secs=60, max_cycles=3)
            with patch("kiro_crew.autonudge.release_run_grant") as rel:
                await svc.add(
                    slot_key="chat-9-z", message="second", idle_secs=60, max_cycles=3
                )
            rel.assert_called_once()
            assert rel.call_args.args[0] == "chat-9-z"

        asyncio.run(_go())

    def test_the_replace_path_is_audited_as_replaced_not_removed(self, tmp_path) -> None:
        """Mutation: drop the `reason` argument so the replace path reports
        `removed` — an auditor then cannot tell a vanished overnight window from an
        operator stopping their own run.
        """
        async def _go() -> None:
            svc = self._svc(tmp_path)
            await svc.add(slot_key="chat-9-z", message="first", idle_secs=60, max_cycles=3)
            with patch("kiro_crew.autonudge.release_run_grant") as rel:
                await svc.add(
                    slot_key="chat-9-z", message="second", idle_secs=60, max_cycles=3
                )
            assert rel.call_args.kwargs["reason"] == "replaced"

        asyncio.run(_go())

    def test_an_explicit_stop_still_reports_its_own_reason(self, tmp_path) -> None:
        """The replace reason must not leak onto ordinary removals."""
        async def _go() -> None:
            svc = self._svc(tmp_path)
            loop = await svc.add(
                slot_key="chat-9-z", message="first", idle_secs=60, max_cycles=3
            )
            with patch("kiro_crew.autonudge.release_run_grant") as rel:
                await svc.remove(loop.id)
            assert rel.call_args.kwargs["reason"] == "removed"

        asyncio.run(_go())


class TestRewritingTheGoalReleasesTheWindow:
    """A live loop's Save is a PATCH, not a replace, so `remove_sync` never runs.

    The operator authorized a window against a specific set of instructions. New
    instructions inheriting it is the same defect as a re-armed slot inheriting
    it -- and here the UI used to promise the window had cleared while the run
    carried on under it.
    """

    def _svc(self, tmp_path):
        from kiro_crew.autonudge import AutoNudgeService

        return AutoNudgeService(base_dir=tmp_path)

    def test_changing_the_message_releases_the_grant(self, tmp_path) -> None:
        async def _go() -> None:
            svc = self._svc(tmp_path)
            loop = await svc.add(
                slot_key="chat-9-z", message="first goal", idle_secs=60, max_cycles=3
            )
            with patch("kiro_crew.autonudge.release_run_grant") as rel:
                await svc.update(loop.id, message="a different goal", active=True)
            rel.assert_called_once()
            assert rel.call_args.args[0] == "chat-9-z"
            assert rel.call_args.kwargs["reason"] == "goal_rewritten"

        asyncio.run(_go())

    def test_resaving_the_same_message_does_not_release(self, tmp_path) -> None:
        """Mutation: drop the `!=` comparison -- every Save would then silently
        drop a window the operator just granted, including a Save that changed
        nothing.
        """
        async def _go() -> None:
            svc = self._svc(tmp_path)
            loop = await svc.add(
                slot_key="chat-9-z", message="same goal", idle_secs=60, max_cycles=3
            )
            with patch("kiro_crew.autonudge.release_run_grant") as rel:
                await svc.update(loop.id, message="same goal", active=True)
            rel.assert_not_called()

        asyncio.run(_go())

    def test_editing_only_the_budget_does_not_release(self, tmp_path) -> None:
        """The budget changes how long the run may go, not what it is told to do."""
        async def _go() -> None:
            svc = self._svc(tmp_path)
            loop = await svc.add(
                slot_key="chat-9-z", message="goal", idle_secs=60, max_cycles=3
            )
            with patch("kiro_crew.autonudge.release_run_grant") as rel:
                await svc.update(loop.id, idle_secs=120, max_cycles=9, active=True)
            rel.assert_not_called()

        asyncio.run(_go())


class TestTheWindowCannotBeExtendedByRepetition:
    """"Never extended" has to hold against REPETITION, not just a renew call.

    `activate_scoped` overwrites the scope's expiry, so a second authorization
    while the first is live would push the deadline past the window the operator
    declared -- an extension by another name. Asserting the absence of
    `renew_scoped` did not cover this: the bypass uses the same call the grant
    itself is made with.
    """

    def test_a_second_authorization_is_refused_while_one_is_live(self) -> None:
        so = MagicMock()
        so.is_scope_active.return_value = True
        sink = MagicMock()
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            with patch("kiro_crew.autonudge_grant.sel", return_value=sink):
                assert authorize_run("chat-1-x", AUTHORIZED_WINDOWS[0], source="dashboard") is False
        so.activate_scoped.assert_not_called()
        assert "already_authorized" in sink.log_api_access.call_args.kwargs["resources"]

    def test_the_first_authorization_still_succeeds(self) -> None:
        so = MagicMock()
        so.is_scope_active.return_value = False
        so.activate_scoped.return_value = _granted_result()
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            with patch("kiro_crew.autonudge_grant.sel", return_value=MagicMock()):
                assert authorize_run("chat-1-x", AUTHORIZED_WINDOWS[1], source="dashboard")
        so.activate_scoped.assert_called_once()

    def test_revoking_then_authorizing_again_is_allowed(self) -> None:
        """Changing the window is expressible -- revoke first. That ordering can
        only ever reduce authority, which is why it is the permitted path.
        """
        so = MagicMock()
        so.is_scope_active.side_effect = [False]
        so.activate_scoped.return_value = _granted_result()
        with patch("kiro_crew.autonudge_grant.safety_override", return_value=so):
            with patch("kiro_crew.autonudge_grant.sel", return_value=MagicMock()):
                assert authorize_run("chat-1-x", AUTHORIZED_WINDOWS[2], source="dashboard")
        assert so.activate_scoped.call_args.kwargs["ttl"] == AUTHORIZED_WINDOWS[2]
