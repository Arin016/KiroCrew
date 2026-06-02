"""Tests for the Slack channel challenge-and-redirect feature."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

from kiro_claw.dashboard.token_auth import (
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
        tampered_payload = base64.urlsafe_b64encode(
            json.dumps(data, separators=(",", ":")).encode()
        ).rstrip(b"=").decode()

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

        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, \
             patch("kiro_claw.slack.allowlist.get_tunnel_url", return_value=None), \
             patch("kiro_claw.slack.allowlist.dashboard_origin", return_value="http://localhost:7777"), \
             patch("kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 7777)), \
             patch("kiro_claw.slack.allowlist.is_local_only", return_value=True), \
             patch("kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:7777"
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

        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, \
             patch("kiro_claw.slack.allowlist.get_tunnel_url", return_value=tunnel), \
             patch("kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 7777)), \
             patch("kiro_claw.slack.allowlist.is_local_only", return_value=True), \
             patch("kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:7777"
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

        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, \
             patch("kiro_claw.slack.allowlist.get_tunnel_url", return_value=tunnel), \
             patch("kiro_claw.slack.allowlist.dashboard_origin", return_value="http://localhost:7777"), \
             patch("kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 7777)), \
             patch("kiro_claw.slack.allowlist.is_local_only", return_value=True), \
             patch("kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:7777"
            mock_cfg.load.return_value.slack.use_tunnel_url = False

            url = await send_channel_challenge(slack, "C123", "U456", "test")

        assert not url.startswith(tunnel)
        assert "localhost:7777" in url

    @pytest.mark.asyncio
    async def test_falls_back_to_localhost_without_tunnel(self):
        from kiro_claw.slack.allowlist import send_channel_challenge

        slack = AsyncMock()
        slack.post_ephemeral = AsyncMock()

        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, \
             patch("kiro_claw.slack.allowlist.get_tunnel_url", return_value=None), \
             patch("kiro_claw.slack.allowlist.dashboard_origin", return_value="http://localhost:7777"), \
             patch("kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 7777)), \
             patch("kiro_claw.slack.allowlist.is_local_only", return_value=True), \
             patch("kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:7777"
            mock_cfg.load.return_value.slack.use_tunnel_url = False

            url = await send_channel_challenge(slack, "C123", "U456", "test")

        assert "localhost:7777" in url

    @pytest.mark.asyncio
    async def test_prompt_only_in_token_not_url(self):
        from kiro_claw.slack.allowlist import send_channel_challenge

        slack = AsyncMock()
        slack.post_ephemeral = AsyncMock()

        with patch("kiro_claw.slack.allowlist.KiroClawConfig") as mock_cfg, \
             patch("kiro_claw.slack.allowlist.get_tunnel_url", return_value=None), \
             patch("kiro_claw.slack.allowlist.dashboard_origin", return_value="http://localhost:7777"), \
             patch("kiro_claw.slack.allowlist.parse_dashboard_url", return_value=("localhost", 7777)), \
             patch("kiro_claw.slack.allowlist.is_local_only", return_value=True), \
             patch("kiro_claw.slack.allowlist.resolve_dashboard_host", return_value="localhost"):
            mock_cfg.load.return_value.dashboard.url = "http://localhost:7777"
            mock_cfg.load.return_value.slack.use_tunnel_url = False

            url = await send_channel_challenge(
                slack, "C123", "U456", "what's the status?"
            )

        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        # No separate prompt param in URL
        assert "prompt" not in params
        # Prompt is inside the signed token
        token = params["token"][0]
        assert extract_prompt_from_token(token) == "what's the status?"


class TestChallengeRedirectDefault:
    """The challenge-and-redirect gate is OFF by default (the redirect flow is
    not yet ready); operators opt in with KIROCLAW_ENABLE_CHALLENGE=1."""

    def _reload_events(self):
        import importlib

        import kiro_claw.slack.events as ev

        return importlib.reload(ev)

    def test_disabled_by_default_when_env_unset(self, monkeypatch):
        monkeypatch.delenv("KIROCLAW_ENABLE_CHALLENGE", raising=False)
        monkeypatch.delenv("KIROCLAW_DISABLE_CHALLENGE", raising=False)
        try:
            ev = self._reload_events()
            assert ev._CHALLENGE_REDIRECT_ENABLED is False
        finally:
            self._reload_events()  # restore module to ambient env

    def test_enabled_only_with_explicit_opt_in(self, monkeypatch):
        monkeypatch.setenv("KIROCLAW_ENABLE_CHALLENGE", "1")
        try:
            ev = self._reload_events()
            assert ev._CHALLENGE_REDIRECT_ENABLED is True
        finally:
            monkeypatch.delenv("KIROCLAW_ENABLE_CHALLENGE", raising=False)
            self._reload_events()

    def test_legacy_disable_var_no_longer_enables(self, monkeypatch):
        # The old KIROCLAW_DISABLE_CHALLENGE var must NOT re-enable the gate —
        # default is off, and only the positive opt-in turns it on.
        monkeypatch.delenv("KIROCLAW_ENABLE_CHALLENGE", raising=False)
        monkeypatch.setenv("KIROCLAW_DISABLE_CHALLENGE", "0")
        try:
            ev = self._reload_events()
            assert ev._CHALLENGE_REDIRECT_ENABLED is False
        finally:
            monkeypatch.delenv("KIROCLAW_DISABLE_CHALLENGE", raising=False)
            self._reload_events()
