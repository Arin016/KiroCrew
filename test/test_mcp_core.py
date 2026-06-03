"""Tests for mcp_core session key routing."""

from __future__ import annotations

from unittest.mock import patch

from kiro_claw.mcp_core import _call_tool


class TestSpawnRunSessionKeyRouting:
    def test_uses_env_var_when_set(self):
        """KIROCLAW_SESSION_KEY env var is used as parent_session."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "sess-from-env"}
        ):
            mock_post.return_value = {"id": "agent1"}

            _call_tool("spawn_run", {"task": "test"})

            call_body = mock_post.call_args[0][1]
            assert call_body["parent_session"] == "sess-from-env"

    def test_falls_back_to_pid_file(self, tmp_path):
        import os

        with patch("kiro_claw.mcp_core._post") as mock_post, patch(
            "pathlib.Path.home", return_value=tmp_path / "fake_home"
        ):
            env = os.environ.copy()
            env.pop("KIROCLAW_SESSION_KEY", None)
            env.pop("KIROCLAW_HOME", None)  # ensure config_dir() uses patched Path.home()
            with patch.dict("os.environ", env, clear=True):
                kiroclaw_dir = tmp_path / "fake_home" / ".kiroclaw"
                kiroclaw_dir.mkdir(parents=True)
                (kiroclaw_dir / f"session_pid_{os.getppid()}.txt").write_text("sess-from-pid")

                mock_post.return_value = {"id": "agent1"}
                _call_tool("spawn_run", {"task": "test"})

                assert mock_post.call_args[0][1]["parent_session"] == "sess-from-pid"


class TestSendMessageUnfurlForwarding:
    def test_unfurl_params_forwarded_in_payload(self):
        """unfurl_links and unfurl_media are forwarded to /api/send-message."""
        with patch("kiro_claw.mcp_core._post") as mock_post:
            mock_post.return_value = {"ok": True}

            _call_tool(
                "send_message",
                {
                    "text": "test",
                    "unfurl_links": False,
                    "unfurl_media": False,
                },
            )

            payload = mock_post.call_args[0][1]
            assert payload["unfurl_links"] is False
            assert payload["unfurl_media"] is False

    def test_unfurl_params_omitted_when_absent(self):
        """unfurl params are not in payload when not provided."""
        with patch("kiro_claw.mcp_core._post") as mock_post:
            mock_post.return_value = {"ok": True}

            _call_tool("send_message", {"text": "test"})

            payload = mock_post.call_args[0][1]
            assert "unfurl_links" not in payload
            assert "unfurl_media" not in payload


class TestSendMessageCronSession:
    """session param is explicit opt-in only — no auto-default.
    Default delivery is notification-only; session="slack" adds Slack DM.
    """

    def test_default_notification_only(self):
        """Bare send_message(text=...) → no session in payload, notification only."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True}
            result = _call_tool("send_message", {"text": "build passed"})

            payload = mock_post.call_args[0][1]
            assert "session" not in payload
            assert "Notification delivered" in result

    def test_explicit_session_origin_passes_through(self):
        """LLM explicitly passes session=origin → origin in payload."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True}
            _call_tool("send_message", {"text": "hi", "session": "origin"})

            payload = mock_post.call_args[0][1]
            assert payload.get("session") == "origin"

    def test_explicit_session_slack(self):
        """session='slack' routes to Slack DM + notification."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True, "slack": True, "ts": "123.456"}
            result = _call_tool("send_message", {"text": "hi", "session": "slack"})

            payload = mock_post.call_args[0][1]
            assert payload.get("session") == "slack"
            assert "Slack" in result

    def test_invalid_session_value_rejected(self):
        """session must be 'origin' or 'slack'; other values rejected."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "cron:abc123"}
        ):
            result = _call_tool("send_message", {"text": "hi", "session": "bogus"})
            assert "session" in result.lower() or "error" in result.lower()
            mock_post.assert_not_called()
