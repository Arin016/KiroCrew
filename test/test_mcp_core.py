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

            _call_tool("send_message", {
                "text": "test",
                "unfurl_links": False,
                "unfurl_media": False,
            })

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


class TestSendMessageCronAutoOrigin:
    """Auto-default logic: when the caller is a cron job and the LLM didn't
    explicitly set session/channel/user, `send_message` auto-applies
    session="origin" so cron updates inject into the session that spawned
    them. Explicit channel/user/session values always win.
    """

    def test_auto_applies_origin_for_cron_caller(self):
        """Cron caller with bare send_message(text=...) → session=origin injected."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True}
            _call_tool("send_message", {"text": "build passed"})

            payload = mock_post.call_args[0][1]
            assert payload.get("session") == "origin"

    def test_explicit_channel_suppresses_auto_default(self):
        """Cron caller with explicit channel=... → no session auto-default."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True}
            _call_tool("send_message", {"text": "hi", "channel": "C12345"})

            payload = mock_post.call_args[0][1]
            assert "session" not in payload
            assert payload.get("channel") == "C12345"

    def test_explicit_user_suppresses_auto_default(self):
        """Cron caller with explicit user=... (intentional Slack DM) → no auto-default."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True}
            _call_tool("send_message", {"text": "hi", "user": "U05J78ZGYNQ"})

            payload = mock_post.call_args[0][1]
            assert "session" not in payload
            assert payload.get("user") == "U05J78ZGYNQ"

    def test_non_cron_session_skips_auto_default(self):
        """Dashboard (non-cron) caller → no auto-default, sends to owner DM as before."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "dashboard:chat-1"}
        ):
            mock_post.return_value = {"ok": True}
            _call_tool("send_message", {"text": "hi"})

            payload = mock_post.call_args[0][1]
            assert "session" not in payload

    def test_missing_env_var_skips_auto_default(self):
        """Absent KIROCLAW_SESSION_KEY → no auto-default."""
        import os

        with patch("kiro_claw.mcp_core._post") as mock_post:
            env = os.environ.copy()
            env.pop("KIROCLAW_SESSION_KEY", None)
            env.pop("KIROCLAW_HOME", None)  # ensure config_dir() uses patched Path.home()
            with patch.dict("os.environ", env, clear=True):
                mock_post.return_value = {"ok": True}
                _call_tool("send_message", {"text": "hi"})

                payload = mock_post.call_args[0][1]
                assert "session" not in payload

    def test_explicit_session_origin_is_idempotent(self):
        """LLM explicitly passes session=origin from cron → still origin, no double-application."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True}
            _call_tool("send_message", {"text": "hi", "session": "origin"})

            payload = mock_post.call_args[0][1]
            assert payload.get("session") == "origin"

    def test_explicit_session_slack_is_accepted(self):
        """session='slack' is a valid explicit opt-out value."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "cron:abc123"}
        ):
            mock_post.return_value = {"ok": True}
            _call_tool("send_message", {"text": "hi", "session": "slack"})

            payload = mock_post.call_args[0][1]
            assert payload.get("session") == "slack"

    def test_invalid_session_value_rejected(self):
        """session must match the ^(origin|slack)$ pattern; other values rejected."""
        with patch("kiro_claw.mcp_core._post") as mock_post, patch.dict(
            "os.environ", {"KIROCLAW_SESSION_KEY": "cron:abc123"}
        ):
            result = _call_tool("send_message", {"text": "hi", "session": "bogus"})
            # Validator-level rejection (pattern mismatch on FieldSpec), not
            # the handler's post-validation check. Either way, no network call.
            assert "session" in result.lower() or "error" in result.lower()
            mock_post.assert_not_called()
