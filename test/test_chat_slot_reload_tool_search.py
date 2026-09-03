"""Tests for POST /api/chat/slots/{slot}/reload-tool-search endpoint.

This route is the dashboard entry point that makes
``AcpProvider.reload_tool_search`` (the manual escape hatch for the issue
#8082 compaction hole) reachable at runtime. Its contract:

- delegate to the live provider's ``reload_tool_search`` and report
  ``reloaded`` (True when the backend was actually restarted, False when the
  provider declined because Tool Search is unmanaged / disabled / non-kiro);
- refuse with 409 ``turn_in_flight`` while a turn is running, so the restart
  never races a streaming prompt;
- serialize the whole sequence under the slot lock, which is the same
  serialization the effort route relies on and which reload_tool_search's
  check-then-act turn guard depends on the caller to provide;
- validate the optional ``min_pct`` / ``min_tokens`` override and forward it;
- report ``reloaded=False`` (not 404) when there is no live ACP session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import api_chat_slot_reload_tool_search
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post(
        "/api/chat/slots/{slot}/reload-tool-search", api_chat_slot_reload_tool_search
    )
    return app


def _mock_state(slot: _ChatSlot | None = None, provider: object = None) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    if slot:
        state._slots[slot.key] = slot
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.get_provider = MagicMock(return_value=provider)
    return state


def _acp_provider(reloaded: bool = True, active_turn: bool = False) -> AcpProvider:
    provider = MagicMock(spec=AcpProvider)
    provider.has_active_turn = MagicMock(return_value=active_turn)
    provider.reload_tool_search = AsyncMock(return_value=reloaded)
    return provider


class TestChatSlotReloadToolSearch:
    @pytest.mark.asyncio
    async def test_reload_delegates_and_reports_reloaded(self):
        slot = _ChatSlot("test")
        provider = _acp_provider(reloaded=True)
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/reload-tool-search", json={})
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "reloaded": True}
            provider.reload_tool_search.assert_awaited_once_with(min_pct=None, min_tokens=None)

    @pytest.mark.asyncio
    async def test_provider_declines_reports_reloaded_false(self):
        # Provider returns False (e.g. Tool Search disabled / unmanaged); the
        # route reports reloaded=False, still 200 — nothing to retry.
        slot = _ChatSlot("test")
        provider = _acp_provider(reloaded=False)
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/reload-tool-search", json={})
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "reloaded": False}

    @pytest.mark.asyncio
    async def test_threshold_override_forwarded(self):
        slot = _ChatSlot("test")
        provider = _acp_provider(reloaded=True)
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/test/reload-tool-search",
                json={"min_pct": 12, "min_tokens": 1234},
            )
            assert resp.status == 200
            provider.reload_tool_search.assert_awaited_once_with(min_pct=12, min_tokens=1234)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "payload",
        [
            {"min_pct": -1},
            {"min_pct": "5"},
            {"min_pct": True},
            {"min_tokens": -10},
            {"min_tokens": 1.5},
        ],
    )
    async def test_invalid_override_rejected(self, payload):
        slot = _ChatSlot("test")
        provider = _acp_provider(reloaded=True)
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/reload-tool-search", json=payload)
            assert resp.status == 400
            # A rejected override never restarts the backend.
            provider.reload_tool_search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_active_turn_returns_409_no_reload(self):
        slot = _ChatSlot("test")
        provider = _acp_provider(active_turn=True)
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/reload-tool-search", json={})
            assert resp.status == 409
            data = await resp.json()
            assert data["code"] == "turn_in_flight"
            provider.reload_tool_search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_live_acp_session_reports_reloaded_false(self):
        # No provider (cold slot) -> reloaded=False, not 404: nothing deferred
        # to recompute, so "nothing to do" is a success from the caller's view.
        slot = _ChatSlot("test")
        state = _mock_state(slot, provider=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/reload-tool-search", json={})
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "reloaded": False}

    @pytest.mark.asyncio
    async def test_unknown_slot_returns_404(self):
        state = _mock_state(slot=None)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/missing/reload-tool-search", json={})
            assert resp.status == 404
            assert (await resp.json())["code"] == "slot_not_found"

    @pytest.mark.asyncio
    async def test_reload_failure_reports_500(self):
        # reload_tool_search rolls its own thresholds back before re-raising on
        # a failed restart; the route surfaces a 500 so the operator knows the
        # reload did not take effect.
        slot = _ChatSlot("test")
        provider = _acp_provider()
        provider.reload_tool_search = AsyncMock(side_effect=RuntimeError("spawn failed"))
        state = _mock_state(slot, provider=provider)
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/test/reload-tool-search", json={})
            assert resp.status == 500
            assert (await resp.json())["code"] == "reload_failed"
