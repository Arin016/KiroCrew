"""Tests for file_send channel parameter feature.

Tests the api_slack_upload_file handler's channel routing:
- When channel is provided and tracked, upload goes to that channel
- When channel is provided but not tracked, request is denied (403)
- When channel is omitted, falls back to owner DM (existing behavior)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_claw.dashboard.handlers.files import api_slack_upload_file
from kiro_claw.dashboard.state import DashboardState


def _make_app(slack_client, tmp_path):
    """Minimal app with the upload-file route and a mock Slack client."""
    app = web.Application()
    state = MagicMock(spec=DashboardState)
    state.slack_client = slack_client
    app["state"] = state
    app.router.add_post("/api/slack/upload-file", api_slack_upload_file)
    return app


@pytest.fixture
def outbox_file(tmp_path):
    """Create a valid UTF-8 file inside a fake outbox directory."""
    outbox = tmp_path / "outbox"
    outbox.mkdir()
    f = outbox / "report.txt"
    f.write_text("hello world", encoding="utf-8")
    return f


class TestFileUploadChannel:
    @pytest.mark.asyncio
    async def test_upload_to_tracked_channel(self, tmp_path, outbox_file):
        """When channel is provided and tracked, file uploads to that channel."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_claw.config.loader.outbox_dir",
            return_value=outbox_file.parent,
        ), patch(
            "kiro_claw.config.loader.workspace_root",
            return_value=tmp_path,
        ), patch(
            "kiro_claw.dashboard.handlers.files.is_tracked_channel",
            return_value=True,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(outbox_file),
                        "filename": "report.txt",
                        "thread_ts": "",
                        "channel": "C0TRACKED123",
                    },
                )
                body = await resp.json()

        assert resp.status == 200
        assert body.get("ok") is True
        # Verify upload went to the specified channel, not owner DM
        slack.upload_file.assert_called_once()
        call_args = slack.upload_file.call_args
        assert call_args[0][0] == "C0TRACKED123"

    @pytest.mark.asyncio
    async def test_upload_to_untracked_channel_denied(self, tmp_path, outbox_file):
        """When channel is provided but NOT tracked, returns 403."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_claw.config.loader.outbox_dir",
            return_value=outbox_file.parent,
        ), patch(
            "kiro_claw.config.loader.workspace_root",
            return_value=tmp_path,
        ), patch(
            "kiro_claw.dashboard.handlers.files.is_tracked_channel",
            return_value=False,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(outbox_file),
                        "filename": "report.txt",
                        "thread_ts": "",
                        "channel": "C0UNTRACKED9",
                    },
                )
                body = await resp.json()

        assert resp.status == 403
        assert "not in tracked channels" in body.get("error", "")
        slack.upload_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_upload_without_channel_uses_owner_dm(self, tmp_path, outbox_file):
        """When channel is omitted, falls back to owner DM."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        slack.open_dm = AsyncMock(return_value="D_OWNER_DM")
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_claw.config.loader.outbox_dir",
            return_value=outbox_file.parent,
        ), patch(
            "kiro_claw.config.loader.workspace_root",
            return_value=tmp_path,
        ), patch(
            "kiro_claw.config.loader.KiroClawConfig.load",
        ) as mock_cfg:
            mock_cfg.return_value.load_credentials.return_value = {
                "KIROCLAW_OWNER_ID": "U_OWNER"
            }
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(outbox_file),
                        "filename": "report.txt",
                        "thread_ts": "",
                        "channel": "",
                    },
                )
                body = await resp.json()

        assert resp.status == 200
        assert body.get("ok") is True
        slack.upload_file.assert_called_once()
        call_args = slack.upload_file.call_args
        assert call_args[0][0] == "D_OWNER_DM"

    @pytest.mark.asyncio
    async def test_upload_with_invalid_channel_returns_400(self, tmp_path, outbox_file):
        """When channel exceeds max length, returns 400."""
        slack = MagicMock()
        slack.upload_file = AsyncMock()
        app = _make_app(slack, tmp_path)

        with patch(
            "kiro_claw.config.loader.outbox_dir",
            return_value=outbox_file.parent,
        ), patch(
            "kiro_claw.config.loader.workspace_root",
            return_value=tmp_path,
        ):
            async with TestClient(TestServer(app)) as client:
                resp = await client.post(
                    "/api/slack/upload-file",
                    json={
                        "file_path": str(outbox_file),
                        "filename": "report.txt",
                        "thread_ts": "",
                        "channel": "C" * 600,
                    },
                )
                body = await resp.json()

        assert resp.status == 400
        assert "invalid channel value" in body.get("error", "")
        slack.upload_file.assert_not_called()
