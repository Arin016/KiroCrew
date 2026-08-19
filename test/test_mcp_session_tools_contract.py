"""The endpoint contracts ``create_session`` / ``send_to_session`` depend on.

These tools are thin HTTP proxies, so a test that hands the handler a dict the
author wrote proves nothing about them. Three defects shipped behind exactly that
kind of test, and each is pinned here against the REAL handler:

* the guards read ``pending_approval`` / ``memory_mode``, which the DETAIL route
  does not return -- both guards passed on hand-built fixtures and failed OPEN in
  production. Asserted both ways: present on the list, absent on the detail, so
  the endpoint choice is deliberate rather than incidental.
* the project write sent ``{"path": ...}``; the handler reads ``body["project"]``,
  so it received ``""`` and CLEARED the directory while answering 200.
* ``POST /api/chat`` answers with a stream unless ``?ws=1``, and its early JSON
  returns are the BUSY branches -- so a successful delivery to an idle slot came
  back unparseable while a merely-queued one returned JSON, inverting the receipt
  on the path the tool exists for.

Driven over a loopback aiohttp server with a real ``DashboardState``, the same way
``test_chat_slot_create_folder.py`` does.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

# Bare import: `test/` is not a package and `test` is a stdlib package name.
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def _app(state: DashboardState) -> web.Application:
    from kiro_crew.dashboard.chat import (
        api_chat_slot_create,
        api_chat_slot_detail,
        api_chat_slot_project,
        api_chat_slots,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", api_chat_slot_create)
    app.router.add_get("/api/chat/slots", api_chat_slots)
    app.router.add_get("/api/chat/slots/{slot}", api_chat_slot_detail)
    app.router.add_post("/api/chat/slots/{slot}/project", api_chat_slot_project)
    return app


async def _new_slot(cl: TestClient, title: str = "Contract") -> str:
    resp = await cl.post("/api/chat/slots", json={"title": title})
    assert resp.status == 200, await resp.text()
    return str((await resp.json())["key"])


class TestSlotResponseShapes:
    @pytest.mark.asyncio
    async def test_the_list_route_carries_the_fields_the_guards_read(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_app(state))) as cl:
            key = await _new_slot(cl)
            rows: Any = await (await cl.get("/api/chat/slots")).json()

            assert isinstance(rows, list), "the tools branch on a JSON array here"
            row = next(r for r in rows if r["key"] == key)
            # The private-session and approval refusals are decided from these two.
            assert "memory_mode" in row
            assert "pending_approval" in row
            # And the queued-vs-running receipt from this one.
            assert "running" in row

    @pytest.mark.asyncio
    async def test_the_detail_route_does_not_carry_them(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_app(state))) as cl:
            key = await _new_slot(cl)
            body = await (await cl.get(f"/api/chat/slots/{key}")).json()

            # This is WHY the tools read the list. If the detail route ever grows
            # these fields, this fails and the choice can be revisited
            # deliberately -- rather than the guards silently failing open again.
            assert "pending_approval" not in body
            assert "memory_mode" not in body

    @pytest.mark.asyncio
    async def test_create_returns_a_key_the_sibling_routes_accept(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_app(state))) as cl:
            key = await _new_slot(cl, title="Round trip")

            # The create response's `key` must work as {slot} on the sibling
            # routes. The descriptor previously promised it was a
            # get_chat_session identifier, which is a different space --
            # transcript stems are `dashboard_<slot>`.
            assert key in state._slots
            assert (await cl.get(f"/api/chat/slots/{key}")).status == 200
            rows = await (await cl.get("/api/chat/slots")).json()
            assert any(r["key"] == key for r in rows)


class TestProjectWrite:
    @pytest.mark.asyncio
    async def test_the_project_key_is_what_sets_the_directory(self, tmp_path):
        state = _make_state(tmp_path)
        # A REAL directory: the handler rejects a non-existent path with 400
        # "Not a directory", which is itself part of the contract the tool
        # inherits -- an agent passing a plausible-but-absent path is refused.
        repo = tmp_path / "repo"
        repo.mkdir()
        async with TestClient(TestServer(_app(state))) as cl:
            key = await _new_slot(cl)

            ok = await cl.post(f"/api/chat/slots/{key}/project", json={"project": str(repo)})
            assert ok.status == 200, await ok.text()
            assert state._slots[key].project == str(repo)

    @pytest.mark.asyncio
    async def test_an_absent_directory_is_refused(self, tmp_path):
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_app(state))) as cl:
            key = await _new_slot(cl)

            resp = await cl.post(
                f"/api/chat/slots/{key}/project", json={"project": str(tmp_path / "nope")}
            )
            assert resp.status == 400
            assert state._slots[key].project == ""

    @pytest.mark.asyncio
    async def test_any_other_body_key_clears_it_and_still_answers_200(self, tmp_path):
        state = _make_state(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        async with TestClient(TestServer(_app(state))) as cl:
            key = await _new_slot(cl)
            await cl.post(f"/api/chat/slots/{key}/project", json={"project": str(repo)})
            assert state._slots[key].project == str(repo)

            # The shipped defect: `path` is not read, so the handler sees "" and
            # CLEARS the directory -- while answering 200, which the tool then
            # reported and audited as success.
            resp = await cl.post(f"/api/chat/slots/{key}/project", json={"path": str(repo)})
            assert resp.status == 200
            assert state._slots[key].project == "", "a wrong body key silently clears the project"


def test_the_chat_route_streams_unless_ws_is_set() -> None:
    """``/api/chat`` returns a stream; only ``?ws=1`` makes it answer JSON.

    Read from the handler rather than driven, because driving a real turn needs a
    live agent. What matters for the tools is the ORDER: the early JSON returns
    are the busy branches and the ws switch sits after them, so an idle slot --
    the case ``send_to_session`` targets -- takes the stream path.
    """
    from kiro_crew.dashboard import chat_handlers

    src = inspect.getsource(chat_handlers.api_chat)
    assert 'request.query.get("ws") == "1"' in src
    assert src.index('ws_mode = request.query.get("ws")') > src.index(
        '{"ok": True, "queued": True}'
    )


def test_both_tools_send_chat_with_ws_set() -> None:
    """The tools must carry ``?ws=1``, or they parse a stream as JSON."""
    from kiro_crew.mcp_tools import sessions

    for fn in (sessions.create_session, sessions.send_to_session):
        src = inspect.getsource(fn)
        assert '"/api/chat?ws=1"' in src, f"{fn.__name__} must post to /api/chat?ws=1"


def test_the_project_write_uses_the_project_key() -> None:
    """Pins the body key in the TOOL, so the contract above cannot drift from it."""
    from kiro_crew.mcp_tools import sessions

    src = inspect.getsource(sessions.create_session)
    assert '{"project": project}' in src
