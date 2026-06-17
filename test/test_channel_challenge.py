"""Tests for the Slack channel challenge-and-redirect feature."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from kiro_claw.dashboard.token_auth import (
    extract_claims_from_token,
    extract_prompt_from_token,
    generate_token,
    revoke_all_sessions,
    validate_token,
)


@pytest.fixture(autouse=True)
def clear_nonces():
    revoke_all_sessions()
    yield
    revoke_all_sessions()


# -- Token with prompt --


class TestTokenWithPrompt:
    """Token generation and validation with embedded prompt."""

    def test_generate_token_includes_prompt_in_payload(self):
        token = generate_token("user1", 3600, prompt="hello world")
        prompt = extract_prompt_from_token(token)
        assert prompt == "hello world"

    def test_generate_token_without_prompt_returns_empty(self):
        token = generate_token("user1", 3600)
        prompt = extract_prompt_from_token(token)
        assert prompt == ""

    def test_prompt_covered_by_hmac_signature(self):
        """Tampering with the prompt in the payload invalidates the signature."""
        import base64
        import json

        token = generate_token("user1", 3600, prompt="original")
        encoded_payload, sig = token.split(".", 1)

        # Decode, tamper, re-encode
        padding = 4 - len(encoded_payload) % 4
        payload_bytes = base64.urlsafe_b64decode(encoded_payload + "=" * (padding % 4))
        data = json.loads(payload_bytes)
        data["prompt"] = "tampered"
        tampered_payload = (
            base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode())
            .rstrip(b"=")
            .decode()
        )

        tampered_token = f"{tampered_payload}.{sig}"
        valid, _, reason = validate_token(tampered_token)
        assert valid is False
        assert reason == "invalid signature"

    def test_token_with_prompt_validates_normally(self):
        token = generate_token("user1", 3600, prompt="test prompt")
        valid, user_id, reason = validate_token(token)
        assert valid is True
        assert user_id == "user1"
        assert reason == ""

    def test_prompt_with_special_characters(self):
        prompt = "What's the status of @user's deployment? 🚀 <script>alert(1)</script>"
        token = generate_token("user1", 3600, prompt=prompt)
        extracted = extract_prompt_from_token(token)
        assert extracted == prompt


# -- send_channel_challenge --


class TestSendChannelChallenge:
    """Tests for the channel challenge URL generation and posting."""

    @pytest.mark.asyncio
    async def test_posts_ephemeral_challenge(self):
        from kiro_claw.slack.allowlist import send_channel_challenge

        slack = AsyncMock()
        slack.post_ephemeral = AsyncMock()

        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, patch(
            "kiro_claw.slack.allowlist.get_tunnel_url", return_value=None
        ), patch(
            "kiro_claw.slack.allowlist.dashboard_origin", return_value="http://localhost:8765"
        ), patch(
            "kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 8765)
        ), patch(
            "kiro_claw.slack.allowlist.is_local_only", return_value=True
        ), patch(
            "kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"
        ):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:8765"
            mock_cfg.load.return_value.slack.use_tunnel_url = False

            url = await send_channel_challenge(slack, "C123", "U456", "hello bot")

        assert url != ""
        assert "token=" in url
        # Prompt is NOT in the URL as a separate param — only inside the token
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        assert "prompt" not in params
        # Verify prompt is inside the token
        token = params["token"][0]
        assert extract_prompt_from_token(token) == "hello bot"
        # Verify ephemeral (not public) message
        slack.post_ephemeral.assert_called_once()
        call_args = slack.post_ephemeral.call_args
        assert call_args[0][0] == "C123"  # channel
        assert call_args[0][1] == "U456"  # user_id
        assert "Open a session" in call_args[0][2]  # message text

    @pytest.mark.asyncio
    async def test_uses_tunnel_url_when_available(self):
        from kiro_claw.slack.allowlist import send_channel_challenge

        slack = AsyncMock()
        slack.post_ephemeral = AsyncMock()
        tunnel = "https://gsanc-kiroclaw.tunnels.corp.amazon.com"

        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, patch(
            "kiro_claw.slack.allowlist.get_tunnel_url", return_value=tunnel
        ), patch(
            "kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 8765)
        ), patch(
            "kiro_claw.slack.allowlist.is_local_only", return_value=True
        ), patch(
            "kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"
        ):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:8765"
            mock_cfg.load.return_value.slack.use_tunnel_url = True

            url = await send_channel_challenge(slack, "C123", "U456", "test")

        assert url.startswith(tunnel)

    @pytest.mark.asyncio
    async def test_ignores_tunnel_when_use_tunnel_url_disabled(self):
        """Even when a tunnel is active, the link uses the local origin if
        slack.use_tunnel_url is false (the default)."""
        from kiro_claw.slack.allowlist import send_channel_challenge

        slack = AsyncMock()
        slack.post_ephemeral = AsyncMock()
        tunnel = "https://gsanc-kiroclaw.tunnels.corp.amazon.com"

        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, patch(
            "kiro_claw.slack.allowlist.get_tunnel_url", return_value=tunnel
        ), patch(
            "kiro_claw.slack.allowlist.dashboard_origin", return_value="http://localhost:8765"
        ), patch(
            "kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 8765)
        ), patch(
            "kiro_claw.slack.allowlist.is_local_only", return_value=True
        ), patch(
            "kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"
        ):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:8765"
            mock_cfg.load.return_value.slack.use_tunnel_url = False

            url = await send_channel_challenge(slack, "C123", "U456", "test")

        assert not url.startswith(tunnel)
        assert "localhost:8765" in url

    @pytest.mark.asyncio
    async def test_falls_back_to_localhost_without_tunnel(self):
        from kiro_claw.slack.allowlist import send_channel_challenge

        slack = AsyncMock()
        slack.post_ephemeral = AsyncMock()

        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, patch(
            "kiro_claw.slack.allowlist.get_tunnel_url", return_value=None
        ), patch(
            "kiro_claw.slack.allowlist.dashboard_origin", return_value="http://localhost:8765"
        ), patch(
            "kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 8765)
        ), patch(
            "kiro_claw.slack.allowlist.is_local_only", return_value=True
        ), patch(
            "kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"
        ):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:8765"
            mock_cfg.load.return_value.slack.use_tunnel_url = False

            url = await send_channel_challenge(slack, "C123", "U456", "test")

        assert "localhost:8765" in url

    @pytest.mark.asyncio
    async def test_prompt_only_in_token_not_url(self):
        from kiro_claw.slack.allowlist import send_channel_challenge

        slack = AsyncMock()
        slack.post_ephemeral = AsyncMock()

        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, patch(
            "kiro_claw.slack.allowlist.get_tunnel_url", return_value=None
        ), patch(
            "kiro_claw.slack.allowlist.dashboard_origin", return_value="http://localhost:8765"
        ), patch(
            "kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 8765)
        ), patch(
            "kiro_claw.slack.allowlist.is_local_only", return_value=True
        ), patch(
            "kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"
        ):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:8765"
            mock_cfg.load.return_value.slack.use_tunnel_url = False

            url = await send_channel_challenge(slack, "C123", "U456", "what's the status?")

        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        # No separate prompt param in URL
        assert "prompt" not in params
        # Prompt is inside the signed token
        token = params["token"][0]
        assert extract_prompt_from_token(token) == "what's the status?"

    @pytest.mark.asyncio
    async def test_session_outlives_link_click_window(self):
        """Regression: the session must outlast the 5-min link window.

        Previously the click window (LINK_WINDOW_SECS) was passed as the
        session TTL, so session_exp == exp and the session was dead the
        instant the link expired, surfacing as a permanently locked session.
        """
        import base64
        import json

        from kiro_claw.dashboard.token_auth import LINK_WINDOW_SECS
        from kiro_claw.slack.allowlist import send_channel_challenge

        slack = AsyncMock()
        slack.post_ephemeral = AsyncMock()

        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, patch(
            "kiro_claw.slack.allowlist.get_tunnel_url", return_value=None
        ), patch(
            "kiro_claw.slack.allowlist.dashboard_origin", return_value="http://localhost:8765"
        ), patch(
            "kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 8765)
        ), patch(
            "kiro_claw.slack.allowlist.is_local_only", return_value=True
        ), patch(
            "kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"
        ):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:8765"
            mock_cfg.load.return_value.slack.use_tunnel_url = False

            url = await send_channel_challenge(slack, "C123", "U456", "hello?")

        token = parse_qs(urlparse(url).query)["token"][0]
        encoded_payload = token.split(".", 1)[0]
        padding = 4 - len(encoded_payload) % 4
        data = json.loads(base64.urlsafe_b64decode(encoded_payload + "=" * (padding % 4)))
        # The session must outlive the link click window.
        assert data["session_exp"] > data["exp"]
        # exp is the 5-min click window; session_exp is the (longer) session.
        assert data["exp"] == pytest.approx(data["iat"] + LINK_WINDOW_SECS, abs=2)
        assert data["session_exp"] == pytest.approx(data["iat"] + 3600, abs=2)


# -- Thread context in token (reconnect / auto-link) --


class TestTokenSlackContext:
    """Token carries channel/thread_ts/session_key signed claims."""

    def test_extra_claims_signed_and_extractable(self):
        token = generate_token(
            "U1",
            3600,
            prompt="hi",
            extra={"channel": "C9", "thread_ts": "1700.5", "session_key": "dashboard:chat-1-9"},
        )
        claims = extract_claims_from_token(token, ("channel", "thread_ts", "session_key"))
        assert claims == {
            "channel": "C9",
            "thread_ts": "1700.5",
            "session_key": "dashboard:chat-1-9",
        }

    def test_extra_cannot_override_reserved_claims(self):
        token = generate_token("U1", 3600, extra={"sub": "evil", "nonce": "x", "channel": "C9"})
        valid, user_id, _ = validate_token(token)
        assert valid is True
        assert user_id == "U1"  # sub not overridden
        assert extract_claims_from_token(token, ("channel",)) == {"channel": "C9"}

    def test_claims_empty_when_token_tampered(self):
        token = generate_token("U1", 3600, extra={"channel": "C9"})
        encoded, sig = token.split(".", 1)
        tampered = f"{encoded}.{'A' * len(sig)}"
        assert extract_claims_from_token(tampered, ("channel",)) == {}

    def test_claims_absent_returns_empty(self):
        token = generate_token("U1", 3600, prompt="hi")
        assert extract_claims_from_token(token, ("channel", "thread_ts", "session_key")) == {}


class TestChallengeThreadRouting:
    """send_channel_challenge embeds thread/session context in the token."""

    @pytest.fixture
    def _patched(self):
        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, patch(
            "kiro_claw.slack.allowlist.get_tunnel_url", return_value=None
        ), patch(
            "kiro_claw.slack.allowlist.dashboard_origin", return_value="http://localhost:8765"
        ), patch(
            "kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 8765)
        ), patch(
            "kiro_claw.slack.allowlist.is_local_only", return_value=True
        ), patch(
            "kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"
        ):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:8765"
            mock_cfg.load.return_value.slack.use_tunnel_url = False
            yield

    @pytest.mark.asyncio
    async def test_existing_session_carried_for_linked_thread(self, _patched):
        from kiro_claw.slack.allowlist import send_channel_challenge

        slack = AsyncMock()
        slack.post_ephemeral = AsyncMock()
        url = await send_channel_challenge(
            slack,
            "C123",
            "U456",
            "reply text",
            thread_ts="1700.5",
            session_key="dashboard:chat-2-42",
        )
        token = parse_qs(urlparse(url).query)["token"][0]
        claims = extract_claims_from_token(token, ("channel", "thread_ts", "session_key"))
        assert claims["session_key"] == "dashboard:chat-2-42"
        assert claims["thread_ts"] == "1700.5"
        assert claims["channel"] == "C123"

    @pytest.mark.asyncio
    async def test_fresh_thread_carries_channel_thread_no_session(self, _patched):
        from kiro_claw.slack.allowlist import send_channel_challenge

        slack = AsyncMock()
        slack.post_ephemeral = AsyncMock()
        url = await send_channel_challenge(
            slack,
            "C123",
            "U456",
            "new thread",
            thread_ts="1800.9",
        )
        token = parse_qs(urlparse(url).query)["token"][0]
        claims = extract_claims_from_token(token, ("channel", "thread_ts", "session_key"))
        assert claims.get("thread_ts") == "1800.9"
        assert claims.get("channel") == "C123"
        assert "session_key" not in claims

    @pytest.mark.asyncio
    async def test_no_thread_carries_channel_only(self, _patched):
        from kiro_claw.slack.allowlist import send_channel_challenge

        slack = AsyncMock()
        slack.post_ephemeral = AsyncMock()
        url = await send_channel_challenge(slack, "C123", "U456", "top-level msg")
        token = parse_qs(urlparse(url).query)["token"][0]
        claims = extract_claims_from_token(token, ("channel", "thread_ts", "session_key"))
        assert claims.get("channel") == "C123"
        assert "thread_ts" not in claims
        assert "session_key" not in claims


class TestExtractClaimsAfterLinkWindow:
    """extract_claims_from_token must survive past the 5-min link window."""

    def test_claims_recoverable_after_link_exp(self):
        import time as _time
        from unittest.mock import patch

        # Mint a challenge token (link exp = now+5min, session_exp = now+1h).
        with patch("kiro_claw.dashboard.token_auth.time") as mock_time:
            mock_time.time.return_value = 1000.0
            token = generate_token("U1", 3600, extra={"channel": "C9", "thread_ts": "1700.5"})
        # Advance past the 5-min link window but within the 1h session.
        with patch("kiro_claw.dashboard.token_auth.time") as mock_time:
            mock_time.time.return_value = 1000.0 + 301
            claims = extract_claims_from_token(token, ("channel", "thread_ts"))
        # Validated against session_exp, so the thread context is still recoverable.
        assert claims == {"channel": "C9", "thread_ts": "1700.5"}
        assert _time  # keep import referenced
