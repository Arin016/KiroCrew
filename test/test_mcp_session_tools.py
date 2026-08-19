"""Behaviour of ``create_session`` and ``send_to_session``.

The endpoint SHAPES these depend on are pinned separately, in
``test_mcp_session_tools_contract.py``, against the real handlers -- because the
first version of these tests was written against fakes the author also wrote, and
three defects passed straight through them (guards reading fields the endpoint
never returns, a project write with the wrong body key, and a response parsed as
JSON when it is a stream).

Two rules follow from that and are enforced by the fake here:

* an unscripted path RAISES. A fake that answers every URL with success cannot
  fail when the code calls the wrong one, which is exactly how the project bug
  survived.
* request BODIES are asserted, not just paths.

Identifiers are the real two shapes: a caller holds a SESSION key
(``dashboard:chat-1-...``) while a slot row holds a SLOT key (``chat-4-...``).
The earlier tests used one value for both, which made the self-target guard look
like it worked.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import mcp_core
from kiro_crew.mcp_tools import sessions

CALLER_SESSION = "dashboard:chat-1-1787000000"
CALLER_SLOT = "chat-1-1787000000"
TARGET_SLOT = "chat-4-1786999999"
NEW_SLOT = "chat-9-1787000001"


class _Gateway:
    """Scripted loopback. Unscripted paths raise, so a wrong URL fails a test."""

    def __init__(self, posts: dict[str, Any] | None = None, rows: Any = None) -> None:
        self.posts = posts or {}
        self.rows = rows
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, body: dict | None = None, **kw: Any) -> Any:
        self.calls.append((path, dict(body or {})))
        if path not in self.posts:
            raise AssertionError(f"unscripted POST {path!r} -- the tool called an unexpected URL")
        return self.posts[path]

    def get(self, path: str, **kw: Any) -> Any:
        if path != "/api/chat/slots":
            raise AssertionError(f"unscripted GET {path!r}")
        return self.rows

    @property
    def paths(self) -> list[str]:
        return [p for p, _ in self.calls]

    def body(self, path: str) -> dict:
        return next(b for p, b in self.calls if p == path)


def _row(**over: Any) -> dict[str, Any]:
    """A slot row shaped as ``GET /api/chat/slots`` serializes it."""
    base: dict[str, Any] = {
        "key": TARGET_SLOT,
        "title": "Fix the migration",
        "memory_mode": "default",
        "pending_approval": False,
        "running": False,
    }
    base.update(over)
    return base


@pytest.fixture()
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    entries: list[dict] = []

    class _Sel:
        def log_tool_invocation(self, **kw: Any) -> None:
            entries.append(kw)

    monkeypatch.setattr(mcp_core, "sel", lambda: _Sel())
    # STRICT resolver: the tools must not accept the lenient PID-walk identity.
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: CALLER_SESSION)
    monkeypatch.setattr(mcp_core, "_governance_app", lambda: "")
    monkeypatch.setattr(mcp_core, "_redact_history_output", lambda text: text)
    import kiro_crew.dashboard.chat_utils as chat_utils

    monkeypatch.setattr(chat_utils, "dashboard_slot_key", lambda sk: CALLER_SLOT)
    import kiro_crew.subagent as subagent

    monkeypatch.setattr(subagent, "_vet_spawn_governance", lambda *a, **k: None)
    return entries


def _wire(monkeypatch: pytest.MonkeyPatch, gw: _Gateway) -> _Gateway:
    monkeypatch.setattr(mcp_core, "_post", gw.post)
    monkeypatch.setattr(mcp_core, "_get", gw.get)
    return gw


class TestCreateSession:
    def test_orders_the_three_writes_and_sends_the_right_bodies(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        gw = _wire(
            monkeypatch,
            _Gateway(
                {
                    "/api/chat/slots": {"key": NEW_SLOT},
                    f"/api/chat/slots/{NEW_SLOT}/project": {"ok": True},
                    "/api/chat?ws=1": {"ok": True},
                }
            ),
        )

        sessions.create_session(
            "create_session",
            {"title": "Fix the build", "goal": "Find why CI is red", "project": "/repo"},
        )

        # Order: a turn that begins before the directory is set runs file work in
        # the wrong tree, and nothing recovers a turn that already ran.
        assert gw.paths == [
            "/api/chat/slots",
            f"/api/chat/slots/{NEW_SLOT}/project",
            "/api/chat?ws=1",
        ]
        # Bodies, not just URLs. `path` instead of `project` silently CLEARED the
        # directory while answering 200.
        assert gw.body(f"/api/chat/slots/{NEW_SLOT}/project") == {"project": "/repo"}
        assert gw.body("/api/chat?ws=1") == {"message": "Find why CI is red", "slot": NEW_SLOT}
        assert gw.body("/api/chat/slots")["title"] == "Fix the build"

    def test_does_not_start_a_turn_without_a_goal(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        gw = _wire(monkeypatch, _Gateway({"/api/chat/slots": {"key": NEW_SLOT}}))

        out = sessions.create_session("create_session", {"title": "Parked for later"})

        # No /api/chat at all: opening a session must be free, so an orchestrator
        # can set work up without committing the user's credits to it.
        assert gw.paths == ["/api/chat/slots"]
        assert "Nothing is running yet" in out
        assert audit[-1]["metadata"]["seeded"] is False

    def test_says_queued_when_the_slot_was_busy(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        _wire(
            monkeypatch,
            _Gateway(
                {
                    "/api/chat/slots": {"key": NEW_SLOT},
                    "/api/chat?ws=1": {"ok": True, "queued": True},
                }
            ),
        )

        out = sessions.create_session("create_session", {"title": "Busy", "goal": "Go"})

        # "Its first turn is running now" would be false, and a caller polling for
        # movement would conclude the session ignored the instruction.
        assert "queued" in out

    def test_surfaces_the_gateway_error_when_no_key_comes_back(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        _wire(monkeypatch, _Gateway({"/api/chat/slots": {"error": "crew_unsupported_slot"}}))

        out = sessions.create_session("create_session", {"title": "Nope", "goal": "Go"})

        # The endpoint returns actionable bodies; a generic failure mis-describes
        # every one of them.
        assert "crew_unsupported_slot" in out
        assert audit[-1]["outcome"] == "error"

    def test_reports_a_session_whose_project_did_not_take(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        _wire(
            monkeypatch,
            _Gateway(
                {
                    "/api/chat/slots": {"key": NEW_SLOT},
                    f"/api/chat/slots/{NEW_SLOT}/project": {"error": "Not a directory"},
                    "/api/chat?ws=1": {"ok": True},
                }
            ),
        )

        out = sessions.create_session(
            "create_session", {"title": "Wrong tree", "project": "/nope", "goal": "Go"}
        )

        assert "was NOT set" in out
        assert "Not a directory" in out
        assert audit[-1]["metadata"]["project_set"] is False

    def test_refuses_without_a_strict_identity(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        gw = _wire(monkeypatch, _Gateway({}))

        out = sessions.create_session("create_session", {"title": "Anon"})

        # Without a strict identity the action cannot be governed or attributed,
        # and a lenient PID walk would resolve a subagent to its parent's slot.
        assert "cannot identify the calling session" in out
        assert gw.calls == []

    def test_omits_a_blank_agent_rather_than_sending_it(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        gw = _wire(monkeypatch, _Gateway({"/api/chat/slots": {"key": NEW_SLOT}}))

        sessions.create_session("create_session", {"title": "Plain"})

        assert "agent" not in gw.body("/api/chat/slots")
        assert audit[-1]["metadata"]["agent"] == "default"

    def test_refuses_when_governance_denies(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        import kiro_crew.subagent as subagent

        monkeypatch.setattr(
            subagent, "_vet_spawn_governance", lambda *a, **k: "spawn capability disabled"
        )
        gw = _wire(monkeypatch, _Gateway({}))

        out = sessions.create_session("create_session", {"title": "Denied"})

        assert "Refused" in out
        assert gw.calls == []
        assert audit[-1]["outcome"] == "denied"
        # The denial record must carry WHY policy refused.
        assert audit[-1]["metadata"]["denial"] == "spawn capability disabled"


class TestSendToSession:
    def _gw(self, monkeypatch, rows, posts=None) -> _Gateway:
        return _wire(monkeypatch, _Gateway(posts or {"/api/chat?ws=1": {"ok": True}}, rows=rows))

    def _args(self, **over: Any) -> dict:
        base = {
            "session_key": TARGET_SLOT,
            "message": "The path is /repo/app",
            "reason": "unblocking",
        }
        base.update(over)
        return base

    def test_delivers_to_the_canonical_key_and_records_the_reason(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        gw = self._gw(monkeypatch, [_row()])

        out = sessions.send_to_session(
            "send_to_session", self._args(reason="it stalled on a missing path")
        )

        # Posted to the key the LIST returned, not the caller's raw string: the
        # send path goes through get_or_create_slot, which normalizes and MINTS
        # on a miss, so the two resolutions must not be allowed to disagree.
        assert gw.body("/api/chat?ws=1") == {
            "message": "The path is /repo/app",
            "slot": TARGET_SLOT,
        }
        outcomes = [e["outcome"] for e in audit]
        # Audited BEFORE the send, synchronously, so the record gates the action.
        assert outcomes == ["invoked", "success"]
        assert audit[0]["critical"] is True
        assert audit[-1]["metadata"]["caller_reason"] == "it stalled on a missing path"
        assert "Its turn is running now" in out

    def test_says_queued_rather_than_running_when_the_slot_is_busy(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        self._gw(monkeypatch, [_row()], {"/api/chat?ws=1": {"ok": True, "queued": True}})

        out = sessions.send_to_session("send_to_session", self._args())

        assert "queued behind the turn" in out
        assert audit[-1]["metadata"]["queued"] is True

    def test_refuses_a_session_waiting_on_an_approval(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        gw = self._gw(monkeypatch, [_row(pending_approval=True)])

        out = sessions.send_to_session("send_to_session", self._args())

        assert "only the user can" in out
        assert gw.calls == []
        assert audit[-1]["outcome"] == "awaiting_approval"

    def test_refuses_when_the_row_cannot_say_whether_an_approval_is_owed(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        row = _row()
        del row["pending_approval"]
        gw = self._gw(monkeypatch, [row])

        out = sessions.send_to_session("send_to_session", self._args())

        # Fail CLOSED. A response shape that stops carrying the field must refuse,
        # which is the defect that made this guard inert in the first version.
        assert "cannot tell" in out
        assert gw.calls == []
        assert audit[-1]["outcome"] == "shape"

    def test_refuses_a_private_session(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        gw = self._gw(monkeypatch, [_row(memory_mode="incognito")])

        out = sessions.send_to_session("send_to_session", self._args())

        assert "private" in out
        assert gw.calls == []

    def test_refuses_when_the_row_cannot_say_whether_it_is_private(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        row = _row()
        del row["memory_mode"]
        gw = self._gw(monkeypatch, [row])

        out = sessions.send_to_session("send_to_session", self._args())

        assert "cannot tell" in out
        assert gw.calls == []

    def test_refuses_its_own_session_across_the_two_identifier_shapes(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        # The caller holds dashboard:chat-1-...; the row holds chat-1-.... The
        # guard must see through that, which is why it maps via
        # dashboard_slot_key rather than comparing the raw strings.
        gw = self._gw(monkeypatch, [_row(key=CALLER_SLOT)])

        out = sessions.send_to_session("send_to_session", self._args(session_key=CALLER_SLOT))

        assert "this session" in out
        assert gw.calls == []
        assert audit[-1]["outcome"] == "self_target"

    def test_refuses_a_session_that_is_not_on_the_board(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        gw = self._gw(monkeypatch, [_row(key="chat-77-1")])

        out = sessions.send_to_session("send_to_session", self._args())

        # Refused, not created: a typo must not open a thread nobody asked for.
        assert "no live session" in out
        assert gw.calls == []
        assert audit[-1]["outcome"] == "not_found"

    def test_refuses_when_the_session_list_cannot_be_read(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        # An error envelope, not a list.
        gw = self._gw(monkeypatch, {"error": "transport"})

        out = sessions.send_to_session("send_to_session", self._args())

        assert "could not read the session list" in out
        assert gw.calls == []
        assert audit[-1]["outcome"] == "unreadable"

    def test_reports_a_failed_delivery_rather_than_claiming_success(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        self._gw(monkeypatch, [_row()], {"/api/chat?ws=1": {"error": "slot busy"}})

        out = sessions.send_to_session("send_to_session", self._args())

        assert "Could not deliver" in out
        assert audit[-1]["outcome"] == "error"

    def test_refuses_without_a_strict_identity(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
        gw = self._gw(monkeypatch, [_row()])

        out = sessions.send_to_session("send_to_session", self._args())

        assert "cannot identify the calling session" in out
        assert gw.calls == []

    def test_separates_the_callers_justification_from_the_policy_denial(
        self, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        import kiro_crew.subagent as subagent

        monkeypatch.setattr(subagent, "_vet_spawn_governance", lambda *a, **k: "policy says no")
        gw = self._gw(monkeypatch, [_row()])

        sessions.send_to_session("send_to_session", self._args(reason="my justification"))

        # One field cannot mean both "why the agent wanted this" and "why policy
        # refused" -- the denial query exists to answer the second.
        assert audit[-1]["metadata"]["caller_reason"] == "my justification"
        assert audit[-1]["metadata"]["denial"] == "policy says no"
        assert gw.calls == []
