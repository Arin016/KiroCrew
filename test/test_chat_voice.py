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


class TestVoiceVoices:
    @pytest.mark.asyncio
    async def test_voices_returns_list(self, tmp_path, monkeypatch):
        """Test successful voice listing."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(aws_profile="polly", region="us-east-1")
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._vc", mock_vc)
        # Reset cache
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._voices_cache_ts", 0)

        mock_data = json.dumps({"Voices": [
            {"Id": "Takumi", "Name": "Takumi", "LanguageName": "Japanese",
             "LanguageCode": "ja-JP", "Gender": "Male", "SupportedEngines": ["neural", "standard"]},
            {"Id": "Mizuki", "Name": "Mizuki", "LanguageName": "Japanese",
             "LanguageCode": "ja-JP", "Gender": "Female", "SupportedEngines": ["standard"]},
        ]})

        async def mock_exec(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 0

            async def comm():
                return mock_data.encode(), b""
            proc.communicate = comm
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_exec)

        from kiro_claw.dashboard.chat_voice import api_voice_voices
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 200
            data = await resp.json()
            assert len(data["voices"]) == 2
            assert data["voices"][0]["id"] == "Mizuki"  # sorted by languageCode+name
            assert "engines" in data["voices"][0]

    @pytest.mark.asyncio
    async def test_voices_uses_cache(self, tmp_path, monkeypatch):
        """Test that cached voices are returned without subprocess call."""
        import time
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(aws_profile="", region="")
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._vc", mock_vc)
        cached = [
            {"id": "Ruth", "name": "Ruth", "language": "English",
             "languageCode": "en-US", "gender": "Female", "engines": ["neural"]}
        ]
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._voices_cache", cached)
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._voices_cache_ts", time.time())

        from kiro_claw.dashboard.chat_voice import api_voice_voices
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 200
            data = await resp.json()
            assert data["voices"] == cached

    @pytest.mark.asyncio
    async def test_voices_cli_failure(self, tmp_path, monkeypatch):
        """Test error handling when aws cli fails."""
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(aws_profile="", region="")
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._vc", mock_vc)
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._voices_cache_ts", 0)

        async def mock_exec(*args, **kwargs):
            proc = MagicMock()
            proc.returncode = 1

            async def comm():
                return b"", b"AccessDenied"
            proc.communicate = comm
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_exec)

        from kiro_claw.dashboard.chat_voice import api_voice_voices
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 502

    @pytest.mark.asyncio
    async def test_voices_timeout(self, tmp_path, monkeypatch):
        """Test timeout handling."""
        import asyncio
        monkeypatch.setattr("kiro_claw.dashboard.state.config_dir", lambda: tmp_path)
        mock_vc = MagicMock(aws_profile="", region="")
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._vc", mock_vc)
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._voices_cache", None)
        monkeypatch.setattr("kiro_claw.dashboard.chat_voice._voices_cache_ts", 0)

        async def mock_exec(*args, **kwargs):
            proc = MagicMock()

            async def comm():
                raise asyncio.TimeoutError()
            proc.communicate = comm
            proc.kill = MagicMock()

            async def _wait():
                return 0
            proc.wait = _wait
            return proc

        monkeypatch.setattr("asyncio.create_subprocess_exec", mock_exec)

        from kiro_claw.dashboard.chat_voice import api_voice_voices
        app = web.Application()
        app["state"] = _make_state(tmp_path)
        app.router.add_get("/api/voice/voices", api_voice_voices)

        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/api/voice/voices")
            assert resp.status == 504
