"""/side conversation invariants — one test per load-bearing property.

1. Memory isolation: parent ``build_session_context`` is byte-equal after a
   /side round-trip.
2. Same session: open/turn/close never invokes ``get_or_create_slot``.
3. Non-blocking: ``api_side_turn`` returns before ``_run_side_turn`` finishes.
4. Channel separation: side run_id never appears in main-channel payloads.
5. Tool rejection: empty LLM output produces a visible fallback bubble.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_claw.context import ContextBuilder
from kiro_claw.dashboard.handlers.side import (
    _run_side_turn,
    api_side_close,
    api_side_open,
    api_side_turn,
)
from kiro_claw.dashboard.side_state import SideState
from kiro_claw.learn import LessonStore
from kiro_claw.memory import MemoryStore
from kiro_claw.skills import SkillsLoader

_SIDE_QUESTION = "what is the difference between TCP and UDP?"
_SIDE_ANSWER = "TCP is connection-oriented and UDP is not."
_MAIN_CHAT_EVENT_TYPES = frozenset(
    {"chat_message", "chat_done", "chat_segment", "chat_status"}
)


def _make_side_app(state) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/side/open", api_side_open)
    app.router.add_post("/api/chat/slots/{slot}/side/turn", api_side_turn)
    app.router.add_post("/api/chat/slots/{slot}/side/close", api_side_close)
    return app


def _capture_broadcasts(state) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    state.broadcast_ws = lambda msg_type, data: events.append((msg_type, data))
    return events


def _stub_run_side_turn(monkeypatch, *, answer: str = _SIDE_ANSWER):
    async def _fake_run(state, slot, run_id, question, *, is_first_turn):
        if slot._side is not None and slot._side.open:
            slot._side.append_assistant(answer)

    monkeypatch.setattr(
        "kiro_claw.dashboard.handlers.side._run_side_turn", _fake_run
    )


@pytest.mark.asyncio
async def test_memory_isolation_byte_equal_after_round_trip(
    tmp_path, monkeypatch
):
    """Parent build_session_context is byte-equal pre/post a /side round-trip."""
    _stub_run_side_turn(monkeypatch)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    parent = state.get_or_create_slot("parent")
    parent.append("user", "hi main", "msg msg-u")
    parent.append("assistant", "hello main", "msg msg-a")
    parent.drain()
    state.conversation_log.append("parent", "user", "hi main")
    state.conversation_log.append("parent", "assistant", "hello main")

    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(
            skills_path=tmp_path / "skills", install_builtins=False
        ),
        lessons=LessonStore(base_dir=tmp_path / "lessons"),
        conversation_log=state.conversation_log,
    )
    ctx_before = builder.build_session_context(session_key="parent")

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": _SIDE_QUESTION},
        )
        await client.post("/api/chat/slots/parent/side/close", json={})

    ctx_after = builder.build_session_context(session_key="parent")
    assert ctx_after == ctx_before, "main context diverged after /side round-trip"
    assert _SIDE_QUESTION not in ctx_after
    assert _SIDE_ANSWER not in ctx_after
    assert parent._side is None


@pytest.mark.asyncio
async def test_side_path_never_creates_a_new_slot(tmp_path, monkeypatch):
    """open/turn/close on the parent must not invoke get_or_create_slot."""
    _stub_run_side_turn(monkeypatch)
    state = _make_state(tmp_path)
    state.get_or_create_slot("parent")
    state.sessions.destroy = AsyncMock()

    seen_keys: list[str] = []
    original = state.get_or_create_slot

    def _spy(*args, **kwargs):
        seen_keys.append(args[0] if args else kwargs.get("name", ""))
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "get_or_create_slot", _spy)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "ping"},
        )
        await client.post("/api/chat/slots/parent/side/close", json={})

    assert seen_keys == [], f"side path called get_or_create_slot: {seen_keys}"


@pytest.mark.asyncio
async def test_side_turn_returns_before_run_finishes(tmp_path, monkeypatch):
    """api_side_turn must return its 200 before the LLM stream completes."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def _blocking(state, slot, run_id, question, *, is_first_turn):
        started.set()
        await release.wait()

    monkeypatch.setattr(
        "kiro_claw.dashboard.handlers.side._run_side_turn", _blocking
    )
    state = _make_state(tmp_path)
    state.get_or_create_slot("parent")
    app = _make_side_app(state)

    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        resp = await asyncio.wait_for(
            client.post(
                "/api/chat/slots/parent/side/turn",
                json={"question": "blocking?"},
            ),
            timeout=5.0,
        )
        assert resp.status == 200
        assert started.is_set(), "_run_side_turn did not start before HTTP return"
        release.set()
        for _ in range(50):
            if not state._background_tasks:
                break
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_side_run_id_never_leaks_to_main_channels(tmp_path, monkeypatch):
    """Side broadcasts go on chat.side_result; run_id never appears on main channels."""
    side_started = asyncio.Event()
    side_release = asyncio.Event()

    async def _streaming(state, slot, run_id, question, *, is_first_turn):
        from kiro_claw.dashboard.ws import broadcast_side_result

        side_started.set()
        await side_release.wait()
        broadcast_side_result(
            state, slot_key=slot.key, run_id=run_id,
            role="assistant", content="answer",
        )

    monkeypatch.setattr(
        "kiro_claw.dashboard.handlers.side._run_side_turn", _streaming
    )
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    state.get_or_create_slot("parent")

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        turn_resp = await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "q"},
        )
        side_run_id = (await turn_resp.json())["run_id"]
        await asyncio.wait_for(side_started.wait(), timeout=5.0)

        state.broadcast_ws("chat_message", {"slot": "parent", "content": "main"})
        state.broadcast_ws("chat_done", {"slot": "parent"})

        side_release.set()
        for _ in range(50):
            if not state._background_tasks:
                break
            await asyncio.sleep(0.01)

    main = [(t, p) for t, p in events if t in _MAIN_CHAT_EVENT_TYPES]
    for etype, payload in main:
        assert side_run_id not in repr(payload), (
            f"side run_id leaked into main {etype}: {payload}"
        )
    side_payloads = [p for t, p in events if t == "chat.side_result"]
    assert any(p.get("run_id") == side_run_id for p in side_payloads)


@pytest.mark.asyncio
async def test_empty_llm_output_produces_visible_fallback(tmp_path, monkeypatch):
    """When stream_and_collect returns empty, /side broadcasts a fallback bubble."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("run ls /tmp")
    parent._side.last_run_id = "run-abc"  # match what api_side_turn would set
    parent._side.is_complete = False

    mock_provider = MagicMock()

    async def _fake_get_or_create(key, **kwargs):
        return mock_provider, True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    monkeypatch.setattr(
        "kiro_claw.dashboard.handlers.side.stream_and_collect",
        AsyncMock(return_value=""),
    )

    await _run_side_turn(
        state, parent, "run-abc", "run ls /tmp", is_first_turn=True,
    )

    assistant_broadcasts = [
        (t, d) for t, d in events if d.get("role") == "assistant"
    ]
    assert assistant_broadcasts, "expected at least one assistant broadcast"
    last_content = assistant_broadcasts[-1][1]["content"]
    assert last_content and "tool" in last_content.lower()
    stored = [m for m in parent._side.messages if m["role"] == "assistant"]
    assert stored and stored[-1]["content"] == last_content
