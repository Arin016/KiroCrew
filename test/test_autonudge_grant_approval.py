"""The per-run grant as an auto-approve authority in the live approval path.

Reuses the approval harness from ``test_dashboard_approval`` so these drive the
real ``EVENT_PERMISSION_REQUEST`` loop rather than a re-implementation of it: the
claim under test is that the grant *decides a tool call*, and only the real path
can show that.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

# Bare module name, not `test.test_dashboard_approval`: `test/` ships no
# __init__.py, so it is not a package -- the dotted form resolves only by
# accident of the local rootdir and fails collection on CI.
from test_dashboard_approval import _drive_deny_turn, _make_slot, _make_state

from kiro_crew.autonudge_grant import run_grant_scope
from kiro_crew.dashboard.chat_runner import _run_grant_active

_SLOT = "chat-1-test"


def _grant_for(*scopes: str):
    """A SafetyOverride whose only live scopes are *scopes* (global YOLO off)."""
    so = MagicMock()
    so.is_scope_active.side_effect = lambda scope: scope in scopes
    so.is_active.return_value = False
    return so


class TestTheGrantDecidesToolCalls:
    @pytest.mark.asyncio
    async def test_a_granted_run_auto_approves_without_yolo(self, tmp_path) -> None:
        """Mutation: drop `or run_grant_active` from the decision — the call
        falls through to an interactive prompt nobody is there to answer, which
        is the exact failure this feature exists to remove.
        """
        state, client = _make_state(tmp_path)
        state.is_yolo_active = MagicMock(return_value=False)
        slot = _make_slot(key=_SLOT)
        so = _grant_for(run_grant_scope(_SLOT))
        with patch("kiro_crew.dashboard.chat_runner.safety_override", return_value=so):
            with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
                mock_sel.return_value = MagicMock()
                await _drive_deny_turn(state, client, slot, title="fs_read")
        client.approve_tool.assert_awaited()

    @pytest.mark.asyncio
    async def test_the_audit_names_the_run_grant_not_yolo(self, tmp_path) -> None:
        """An auditor must be able to tell a per-run window from a session-wide
        grant; collapsing both into `yolo` would hide which authority was used.
        """
        state, client = _make_state(tmp_path)
        state.is_yolo_active = MagicMock(return_value=False)
        slot = _make_slot(key=_SLOT)
        so = _grant_for(run_grant_scope(_SLOT))
        with patch("kiro_crew.dashboard.chat_runner.safety_override", return_value=so):
            with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
                audit = MagicMock()
                mock_sel.return_value = audit
                await _drive_deny_turn(state, client, slot, title="fs_read")
        reasons = [
            (c.kwargs.get("metadata") or {}).get("reason")
            for c in audit.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "auto_approved"
        ]
        assert "run_grant" in reasons, reasons

    @pytest.mark.asyncio
    async def test_another_sessions_grant_does_not_approve_this_one(
        self, tmp_path
    ) -> None:
        """The scope is per-slot, so a grant issued elsewhere must not leak in.

        ``approve_prompt`` answers the interactive future the call must now reach:
        without it the turn would hang, which is itself the proof that no
        auto-approval happened — but a hang is a bad test, so the prompt is
        answered and the absence of a ``run_grant`` auto-approval is asserted.
        """
        state, client = _make_state(tmp_path)
        state.is_yolo_active = MagicMock(return_value=False)
        slot = _make_slot(key=_SLOT)
        so = _grant_for(run_grant_scope("chat-99-other"))
        with patch("kiro_crew.dashboard.chat_runner.safety_override", return_value=so):
            with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
                audit = MagicMock()
                mock_sel.return_value = audit
                await _drive_deny_turn(
                    state, client, slot, title="fs_read", approve_prompt=True
                )
        auto = [
            (c.kwargs.get("metadata") or {}).get("reason")
            for c in audit.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "auto_approved"
        ]
        assert "run_grant" not in auto, auto


class TestTheLookupIsSafeInTheApprovalPath:
    def test_a_non_nudgeable_session_holds_no_grant(self) -> None:
        assert _run_grant_active("cron:nightly") is False

    def test_a_blank_session_holds_no_grant(self) -> None:
        assert _run_grant_active("") is False

    def test_a_raising_lookup_falls_through_instead_of_denying(self) -> None:
        """A failure here must degrade to the ordinary prompt, never crash the
        turn or silently approve.
        """
        so = MagicMock()
        so.is_scope_active.side_effect = RuntimeError("boom")
        with patch("kiro_crew.dashboard.chat_runner.safety_override", return_value=so):
            assert _run_grant_active(f"dashboard:{_SLOT}") is False

    def test_a_dashboard_session_resolves_to_its_slot_scope(self) -> None:
        so = _grant_for(run_grant_scope(_SLOT))
        with patch("kiro_crew.dashboard.chat_runner.safety_override", return_value=so):
            assert _run_grant_active(f"dashboard:{_SLOT}") is True
