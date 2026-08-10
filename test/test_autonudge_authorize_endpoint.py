"""The authorize endpoint — the point where a human, and only a human, grants.

The endpoint is the security boundary of this feature: it is what makes the
window an operator's decision rather than the agent's. These drive the real
handler so the owner gate, the closed window set, and the run-must-be-live rule
are asserted against the code that actually serves the route.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.autonudge_grant import AUTHORIZED_WINDOWS
from kiro_crew.dashboard.handlers.autonudge import (
    api_autonudge_authorize,
    api_autonudge_revoke,
)

_LOOP_ID = "loop-abc"
_SLOT = "chat-1-test"


def _request(body: dict, *, loop_id: str = _LOOP_ID) -> MagicMock:
    req = MagicMock()
    req.match_info = {"loop_id": loop_id}

    async def _json() -> dict:
        return body

    req.json = _json
    return req


def _svc(*, active: bool = True, loop_id: str = _LOOP_ID) -> MagicMock:
    svc = MagicMock()
    svc.list_all.return_value = [
        SimpleNamespace(id=loop_id, slot_key=_SLOT, active=active)
    ]
    return svc


def _body_of(response) -> dict:
    return json.loads(response.body.decode())


def _call(request, svc, *, owner: bool, authorize=None):
    mod = "kiro_crew.dashboard.handlers.autonudge"
    granted = MagicMock(return_value=True) if authorize is None else authorize
    with patch(f"{mod}._autonudge_get", return_value=svc):
        with patch(f"{mod}.is_owner_dashboard_request", return_value=owner):
            with patch(f"{mod}.sel", return_value=MagicMock()):
                with patch(f"{mod}.authorize_run", granted) as spy:
                    resp = asyncio.run(api_autonudge_authorize(request))
    return resp, spy


class TestOnlyAnOwnerMayWidenWhatARunCanDo:
    def test_a_non_owner_is_refused(self) -> None:
        """Mutation: drop the owner gate — any holder of a dashboard session
        could then grant a run auto-approval, which is the one thing this
        endpoint exists to keep in an operator's hands.
        """
        resp, spy = _call(
            _request({"window_secs": AUTHORIZED_WINDOWS[0]}), _svc(), owner=False
        )
        assert resp.status == 403
        spy.assert_not_called()

    def test_an_owner_is_granted(self) -> None:
        resp, spy = _call(
            _request({"window_secs": AUTHORIZED_WINDOWS[1]}), _svc(), owner=True
        )
        assert resp.status == 200
        assert spy.call_args.args[1] == AUTHORIZED_WINDOWS[1]
        assert spy.call_args.kwargs["source"] == "dashboard"

    def test_the_grant_is_keyed_to_the_loops_own_slot(self) -> None:
        """The caller names a loop, never a slot: accepting a slot key from the
        body would let one session's click authorize another session's run.
        """
        resp, spy = _call(
            _request(
                {"window_secs": AUTHORIZED_WINDOWS[0], "slot_key": "chat-99-other"}
            ),
            _svc(),
            owner=True,
        )
        assert resp.status == 200
        assert spy.call_args.args[0] == _SLOT


class TestTheRunMustBeRealAndRunning:
    def test_an_unknown_loop_is_a_404(self) -> None:
        resp, spy = _call(
            _request({"window_secs": AUTHORIZED_WINDOWS[0]}, loop_id="nope"),
            _svc(),
            owner=True,
        )
        assert resp.status == 404
        spy.assert_not_called()

    def test_a_stopped_loop_is_refused(self) -> None:
        """A stopped run's release has already fired, so nothing would hand this
        grant back — it would outlive the work it was granted for.
        """
        resp, spy = _call(
            _request({"window_secs": AUTHORIZED_WINDOWS[0]}),
            _svc(active=False),
            owner=True,
        )
        assert resp.status == 409
        spy.assert_not_called()


class TestTheWindowComesFromTheOfferedSet:
    @pytest.mark.parametrize("bad", [60, 3599, 86400, 0, -1])
    def test_an_unoffered_window_is_rejected_with_the_offer(self, bad: int) -> None:
        resp, _ = _call(
            _request({"window_secs": bad}),
            _svc(),
            owner=True,
            authorize=MagicMock(return_value=False),
        )
        assert resp.status == 400
        assert _body_of(resp)["offered"] == list(AUTHORIZED_WINDOWS)

    @pytest.mark.parametrize("bad", ["8h", None, [], {}, 7200.0, 7200.5, True, False])
    def test_a_non_integer_window_is_refused_not_coerced(self, bad: object) -> None:
        """Mutation: coerce with `int(...)` -- `int(7200.5)` is an OFFERED window,
        so coercion hands out a real grant for a value the closed-set check was
        supposed to reject. `7200.0` and `True` are the same class of bypass.
        """
        resp, spy = _call(_request({"window_secs": bad}), _svc(), owner=True)
        assert resp.status == 400
        spy.assert_not_called()

    def test_a_non_object_body_is_a_400_not_a_500(self) -> None:
        """`body.get` on a list raises, which would surface as a 500."""
        req = MagicMock()
        req.match_info = {"loop_id": _LOOP_ID}

        async def _json() -> list:
            return [7200]

        req.json = _json
        resp, spy = _call(req, _svc(), owner=True)
        assert resp.status == 400
        spy.assert_not_called()

    def test_a_missing_window_is_refused(self) -> None:
        resp, _ = _call(
            _request({}), _svc(), owner=True, authorize=MagicMock(return_value=False)
        )
        assert resp.status == 400


class TestADisabledServiceSaysSo:
    def test_no_service_is_a_503(self) -> None:
        mod = "kiro_crew.dashboard.handlers.autonudge"
        with patch(f"{mod}._autonudge_get", return_value=None):
            resp = asyncio.run(
                api_autonudge_authorize(_request({"window_secs": AUTHORIZED_WINDOWS[0]}))
            )
        assert resp.status == 503


class TestTheAuditWriteStaysOffTheEventLoop:
    """`activate_scoped` audits fail-closed with a synchronous SEL filesystem
    write. On the event loop that stalls every other gateway task, so the call is
    offloaded -- and offloading opens a window in which the run can stop.
    """

    def test_the_grant_is_offloaded_to_a_thread(self) -> None:
        """Mutation: call authorize_run directly -- a slow disk then blocks chat,
        heartbeat and liveness for the duration of the write.
        """
        mod = "kiro_crew.dashboard.handlers.autonudge"
        seen: dict[str, object] = {}

        async def _fake_to_thread(fn, *args, **kwargs):
            seen["fn"] = fn
            return fn(*args, **kwargs)

        with patch(f"{mod}._autonudge_get", return_value=_svc()):
            with patch(f"{mod}.is_owner_dashboard_request", return_value=True):
                with patch(f"{mod}.sel", return_value=MagicMock()):
                    with patch(f"{mod}.authorize_run", MagicMock(return_value=True)) as spy:
                        with patch(f"{mod}.asyncio.to_thread", _fake_to_thread):
                            resp = asyncio.run(
                                api_autonudge_authorize(
                                    _request({"window_secs": AUTHORIZED_WINDOWS[0]})
                                )
                            )
        assert resp.status == 200
        assert seen.get("fn") is spy, "authorize_run was not the offloaded callable"

    def test_a_run_that_stops_during_the_offload_has_its_grant_released(self) -> None:
        """Otherwise the window outlives the work: the release path already ran
        for the stopped loop, so nothing would ever hand this grant back.
        """
        mod = "kiro_crew.dashboard.handlers.autonudge"
        svc = MagicMock()
        # Live on the pre-flight read, stopped on the post-offload re-read.
        svc.list_all.side_effect = [
            [SimpleNamespace(id=_LOOP_ID, slot_key=_SLOT, active=True)],
            [SimpleNamespace(id=_LOOP_ID, slot_key=_SLOT, active=False)],
        ]
        with patch(f"{mod}._autonudge_get", return_value=svc):
            with patch(f"{mod}.is_owner_dashboard_request", return_value=True):
                with patch(f"{mod}.sel", return_value=MagicMock()):
                    with patch(f"{mod}.authorize_run", MagicMock(return_value=True)):
                        with patch(f"{mod}.release_run_grant") as rel:
                            resp = asyncio.run(
                                api_autonudge_authorize(
                                    _request({"window_secs": AUTHORIZED_WINDOWS[0]})
                                )
                            )
        assert resp.status == 409
        rel.assert_called_once()
        assert rel.call_args.args[0] == _SLOT


class TestRevokingIsAlwaysAllowedButStillOwnerGated:
    """Revoking only moves the deadline EARLIER.

    That asymmetry drives the rules: it needs no window argument, it is idempotent,
    and it is permitted for a loop in any state -- refusing it would leave standing
    authority an operator explicitly asked to drop. Without it, shedding a
    mis-clicked 12h window means stopping the run, i.e. destroying the work to
    reduce the grant.
    """

    def _call(self, svc, *, owner: bool):
        mod = "kiro_crew.dashboard.handlers.autonudge"
        req = MagicMock()
        req.match_info = {"loop_id": _LOOP_ID}

        async def _to_thread(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch(f"{mod}._autonudge_get", return_value=svc):
            with patch(f"{mod}.is_owner_dashboard_request", return_value=owner):
                with patch(f"{mod}.sel", return_value=MagicMock()):
                    with patch(f"{mod}.release_run_grant") as rel:
                        with patch(f"{mod}.asyncio.to_thread", _to_thread):
                            resp = asyncio.run(api_autonudge_revoke(req))
        return resp, rel

    def test_an_owner_can_revoke(self) -> None:
        resp, rel = self._call(_svc(), owner=True)
        assert resp.status == 200
        rel.assert_called_once()
        assert rel.call_args.args[0] == _SLOT
        assert rel.call_args.kwargs["reason"] == "revoked"

    def test_a_non_owner_cannot(self) -> None:
        """Mutation: drop the owner gate -- any viewer could then cancel someone
        else's authorization, which is a denial of service on the run even though
        it cannot escalate anything.
        """
        resp, rel = self._call(_svc(), owner=False)
        assert resp.status == 403
        rel.assert_not_called()

    def test_a_stopped_run_can_still_be_revoked(self) -> None:
        """Mutation: copy authorize's 409-on-inactive guard here -- an operator
        would then be unable to drop a grant precisely when the run that justified
        it has ended.
        """
        resp, rel = self._call(_svc(active=False), owner=True)
        assert resp.status == 200
        rel.assert_called_once()

    def test_an_unknown_loop_is_a_404(self) -> None:
        resp, rel = self._call(_svc(loop_id="other"), owner=True)
        assert resp.status == 404
        rel.assert_not_called()

    def test_the_sel_write_stays_off_the_event_loop(self) -> None:
        """deactivate_scope writes to the SEL; on the loop a slow disk stalls
        every other gateway task.
        """
        mod = "kiro_crew.dashboard.handlers.autonudge"
        req = MagicMock()
        req.match_info = {"loop_id": _LOOP_ID}
        seen: dict[str, object] = {}

        async def _to_thread(fn, *args, **kwargs):
            seen["fn"] = fn
            return fn(*args, **kwargs)

        with patch(f"{mod}._autonudge_get", return_value=_svc()):
            with patch(f"{mod}.is_owner_dashboard_request", return_value=True):
                with patch(f"{mod}.sel", return_value=MagicMock()):
                    with patch(f"{mod}.release_run_grant") as rel:
                        with patch(f"{mod}.asyncio.to_thread", _to_thread):
                            resp = asyncio.run(api_autonudge_revoke(req))
        assert resp.status == 200
        assert seen.get("fn") is rel, "release_run_grant was not the offloaded call"


class TestChannelLoopsAreRefusedUntilTheirPathsConsumeTheScope:
    """Only the dashboard approval path reads the run scope.

    A `slack:`/`discord:` loop would take the grant and still stall on every
    approval, so a 200 here would be a promise the system cannot keep -- worse
    than a refusal, because the operator walks away believing it worked.
    """

    def _svc_channel(self, key: str):
        svc = MagicMock()
        svc.list_all.return_value = [
            SimpleNamespace(id=_LOOP_ID, slot_key=key, active=True)
        ]
        return svc

    @pytest.mark.parametrize("key", ["slack:C123", "discord:U456"])
    def test_a_channel_loop_is_refused(self, key: str) -> None:
        resp, spy = _call(
            _request({"window_secs": AUTHORIZED_WINDOWS[0]}),
            self._svc_channel(key),
            owner=True,
        )
        assert resp.status == 409
        assert _body_of(resp)["code"] == "channel_loop_unsupported"
        spy.assert_not_called()

    def test_a_dashboard_loop_is_still_allowed(self) -> None:
        resp, spy = _call(
            _request({"window_secs": AUTHORIZED_WINDOWS[0]}), _svc(), owner=True
        )
        assert resp.status == 200
        spy.assert_called_once()
