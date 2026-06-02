"""Unit tests for chat_voice.py — voice config and synthesis endpoints."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state


def _make_voice_app(state):
    from kiro_claw.dashboard.chat_voice import api_voice_config, api_voice_synthesize

    app = web.Application()
    app["state"] = state
    app.router.add_get("/api/voice/config", api_voice_config)
    app.router.add_put("/api/voice/config", api_voice_config)
    app.router.add_post("/api/voice/synthesize", api_voice_synthesize)
    return app


class TestVoiceConfig:
    @pytest.mark.asyncio
    async def test_get_config(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=True, default_voice="Joanna", default_engine="neural",
            default_rate="100%", default_pitch="0%", aws_profile="", region="us-east-1",
        )
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._vc", mock_vc)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.get("/api/voice/config")
            assert resp.status == 200
            data = await resp.json()
            assert data["voice"] == "Joanna"
            assert data["engine"] == "neural"
            assert data["enabled"] is True

    @pytest.mark.asyncio
    async def test_put_config_updates_voice(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            global_enabled=False, default_voice="Joanna", default_engine="neural",
            default_rate="100%", default_pitch="0%", aws_profile="", region="us-east-1",
        )
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._vc", mock_vc)
        # Write a config file so PUT can persist
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({}))
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice.config_path", lambda: cfg_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", json={"voice": "Matthew", "enabled": True})
            assert resp.status == 200
            assert mock_vc.default_voice == "Matthew"
            assert mock_vc.global_enabled is True

    @pytest.mark.asyncio
    async def test_put_config_invalid_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock()
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._vc", mock_vc)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.put("/api/voice/config", data=b"not json", headers={"Content-Type": "application/json"})
            assert resp.status == 400


class TestVoiceSynthesize:
    @pytest.mark.asyncio
    async def test_synthesize_empty_text_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "", "slot": "s1"})
            assert resp.status == 400

    @pytest.mark.asyncio
    async def test_synthesize_success(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            default_voice="Joanna", default_engine="neural",
            default_rate="100%", default_pitch="0%", aws_profile="", region="us-east-1",
        )
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._vc", mock_vc)

        # Mock streaming_voice_reply to yield one chunk
        async def mock_stream(*a, **kw):
            yield 0, "Hello", b"\x00\x01\x02"

        monkeypatch.setattr("kiro_claw.dashboard.chat_voice.streaming_voice_reply", mock_stream)
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice.stitch_mp3s", AsyncMock(return_value=None))

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "Hello world", "slot": "s1"})
            assert resp.status == 200
            data = await resp.json()
            assert data["ok"] is True
            assert data["chunks"] == 1
        state.broadcast_ws.assert_called()

    @pytest.mark.asyncio
    async def test_synthesize_exception_returns_500_and_broadcasts_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(
            default_voice="Joanna", default_engine="neural",
            default_rate="100%", default_pitch="0%", aws_profile="", region="us-east-1",
        )
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._vc", mock_vc)

        # Mock streaming_voice_reply to raise an exception
        async def mock_stream_error(*a, **kw):
            raise RuntimeError("Polly synthesis failed")
            yield  # noqa: unreachable - makes this a generator

        monkeypatch.setattr("kiro_claw.dashboard.chat_voice.streaming_voice_reply", mock_stream_error)

        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        async with TestClient(TestServer(_make_voice_app(state))) as client:
            resp = await client.post("/api/voice/synthesize", json={"text": "Hello", "slot": "s1"})
            assert resp.status == 500
            data = await resp.json()
            assert data["ok"] is False
            assert "error" in data
        # Verify voice_error was broadcast
        state.broadcast_ws.assert_called()
        call_args = state.broadcast_ws.call_args
        assert call_args[0][0] == "voice_error"
        assert call_args[0][1]["slot"] == "s1"
