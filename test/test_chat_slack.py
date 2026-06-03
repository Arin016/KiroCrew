"""Unit tests for chat_slack.py — Slack link, handoff, channel listing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state


def _make_slack_app(state):
    from kiro_claw.dashboard.chat_slack import (
        api_chat_slot_handoff,
        api_chat_slot_slack_link,
        api_handoff_channels,
        api_slack_channels,
    )

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/slack-link", api_chat_slot_slack_link)
    app.router.add_get("/api/slack/channels", api_slack_channels)
    app.router.add_post("/api/chat/slots/{slot}/handoff", api_chat_slot_handoff)
    app.router.add_get("/api/handoff-channels", api_handoff_channels)
    return app


class TestSlackLink:
    @pytest.mark.asyncio
    async def test_slot_not_found(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/nope/slack-link")
            assert resp.status == 404

    @pytest.mark.asyncio
    async def test_no_slack_client(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.slack_client = None
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link")
            assert resp.status == 503

    @pytest.mark.asyncio
    async def test_link_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.drain()
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="C123")
        state.slack_client.post_message = AsyncMock(return_value="ts123")
        state.owner_id = "U123"
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.sessions.set_slack_link = MagicMock()
        state.push_slots_update = MagicMock()
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/slack-link", json={})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["thread_ts"] == "ts123"

    @pytest.mark.asyncio
    async def test_link_to_existing_thread_no_new_post(self, tmp_path, monkeypatch):
        """challenge-redirect auto-link: link to an existing thread_ts.

        Must NOT post a new root thread message and must NOT replay context
        (the thread already has it), but MUST register the reverse link so a
        later reply in that thread routes back to this session.
        """
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot.append("user", "hello")
        slot.append("assistant", "hi there")
        slot.drain()
        state.slack_client = MagicMock()
        state.slack_client.open_dm = AsyncMock(return_value="C123")
        state.slack_client.post_message = AsyncMock(return_value="newts")
        state.owner_id = "U123"
        state.sessions.get_slack_link = MagicMock(return_value=(None, None))
        state.sessions.set_slack_link = MagicMock()
        state.push_slots_update = MagicMock()
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post(
                "/api/chat/slots/s1/slack-link",
                json={"channel": "C999", "thread_ts": "1700.42"},
            )
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            # Links to the supplied thread, not a freshly posted one.
            assert data["thread_ts"] == "1700.42"
            assert data["channel"] == "C999"
        # No message posted (neither a new root thread nor replayed context).
        assert state.slack_client.post_message.await_count == 0
        # Reverse link registered so future thread replies find this session.
        state.sessions.set_slack_link.assert_called_once()
        args = state.sessions.set_slack_link.call_args.args
        assert args[1] == "1700.42"
        assert args[2] == "C999"


class TestSlackChannels:
    @pytest.mark.asyncio
    async def test_list_channels(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.slack.tracking_channels = [{"channel_id": "C1", "name": "general"}]
        mock_cfg.slack_channels = {}
        monkeypatch.setattr("kiro_claw.config.loader.KiroClawConfig.load", lambda: mock_cfg)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.get("/api/slack/channels")
            assert resp.status == 200
            data = await resp.json()
            assert data[0]["id"] == "dm"
            assert any(c["id"] == "C1" for c in data)

    @pytest.mark.asyncio
    async def test_resolves_names_for_slack_channels_dict(self, tmp_path, monkeypatch):
        """cfg.slack_channels entries (no name field) should have names resolved."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.slack.tracking_channels = []
        cc = MagicMock()
        cc.activation = "always"
        mock_cfg.slack_channels = {"C0AU38Q0E4B": cc}
        monkeypatch.setattr("kiro_claw.config.loader.KiroClawConfig.load", lambda: mock_cfg)
        state = _make_state(tmp_path)
        state.slack_client = MagicMock()
        state.slack_client.conversations_list = AsyncMock(
            return_value=[
                {"id": "C0AU38Q0E4B", "name": "pcn-orchestrator-interest"},
            ]
        )
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.get("/api/slack/channels")
            assert resp.status == 200
            data = await resp.json()
            resolved = next((c for c in data if c["id"] == "C0AU38Q0E4B"), None)
            assert resolved is not None
            assert resolved["name"] == "pcn-orchestrator-interest"

    @pytest.mark.asyncio
    async def test_no_slack_client_falls_back_to_id(self, tmp_path, monkeypatch):
        """Without a Slack client, unresolved channels keep id as name (no crash)."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_cfg = MagicMock()
        mock_cfg.slack.tracking_channels = []
        cc = MagicMock()
        cc.activation = "always"
        mock_cfg.slack_channels = {"C0AU38Q0E4B": cc}
        monkeypatch.setattr("kiro_claw.config.loader.KiroClawConfig.load", lambda: mock_cfg)
        state = _make_state(tmp_path)
        state.slack_client = None
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.get("/api/slack/channels")
            assert resp.status == 200
            data = await resp.json()
            unresolved = next((c for c in data if c["id"] == "C0AU38Q0E4B"), None)
            assert unresolved is not None
            assert unresolved["name"] == "C0AU38Q0E4B"


class TestHandoff:
    @pytest.mark.asyncio
    async def test_handoff_no_slack(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.slack_client = None
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/handoff")
            assert resp.status == 503


class TestHandoffChannels:
    @pytest.mark.asyncio
    async def test_deprecated_endpoint(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_slack_app(state))) as client:
            resp = await client.get("/api/handoff-channels")
            assert resp.status == 200
            assert await resp.json() == {}
