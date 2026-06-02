"""Tests for channel agent model inheritance (Mesh-1513).

Verifies that ``SessionManager.get_or_create()`` falls back to the global
``agent.model`` config when no explicit model is passed.  This ensures
channel agents (and any other caller that omits ``model=``) inherit the
user's configured model instead of silently defaulting to 'auto' (Sonnet).
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from kiro_claw.config.loader import KiroClawConfig
from kiro_claw.session import SessionManager


@pytest.fixture
def cfg():
    c = KiroClawConfig()
    # The default provider is now ``claude_code`` (it reads ``agent.cc_model``).
    # These tests exercise the legacy ``agent.model`` fallback path, which only
    # applies to non-claude_code providers, so pin the provider to ``acp``.
    c.agent.provider = "acp"
    c.agent.model = "claude-opus-4.6"
    c.session.timeout_secs = 2  # short for testing
    return c


def _capturing_factory(captured: dict):
    """Factory that records kwargs passed to it."""

    def factory(session_key=None, agent=None, channel_id=None, **kwargs):
        captured.update(kwargs)
        m = AsyncMock()
        m.start = AsyncMock()
        m.shutdown = AsyncMock()
        m.context_usage_pct = lambda: 0.0
        m.is_alive.return_value = True
        return m

    return factory


class TestModelFallbackToGlobalConfig:
    """get_or_create() falls back to global model when caller omits it."""

    @pytest.mark.asyncio
    async def test_no_model_uses_global_config(self, cfg):
        """When caller omits model=, factory receives the global model."""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=_capturing_factory(captured))
        await mgr.get_or_create("test-fallback", agent="deep-researcher")
        assert captured["model_override"] == "claude-opus-4.6"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_explicit_model_overrides_global(self, cfg):
        """Explicit model= wins over global config."""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=_capturing_factory(captured))
        await mgr.get_or_create("test-explicit", model="claude-haiku")
        assert captured["model_override"] == "claude-haiku"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_channel_agent_call_pattern(self, cfg):
        """Mirror the channel.py call: agent name, no model arg."""
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=_capturing_factory(captured))
        # This is exactly how run_channel_agent() calls get_or_create
        await mgr.get_or_create(
            "channel:abc:agent1",
            agent="deep-researcher",
            approval_policy="trusted",
        )
        # Without the fix, model_override would be None and kiro-cli
        # would default to 'auto' (Sonnet) for non-kiroclaw agents.
        assert captured["model_override"] == "claude-opus-4.6"
        await mgr.close_all()


class TestCcModelFallback:
    """Provider-aware fallback: when provider==claude_code, get_or_create()
    must read agent.cc_model (not agent.model). agent.model is the kiro/ACP
    slot and can legitimately be \"auto\" or a stale kiro id from before the
    user switched providers; using it would override the user-set cc_model.

    Mirrors the bug where Slack DMs and `kiroclaw chat` ran on Opus 4.6
    despite cc_model being set to global.anthropic.claude-opus-4-8[1m].
    """

    @pytest.fixture
    def cc_cfg(self):
        c = KiroClawConfig()
        c.agent.provider = "claude_code"
        c.agent.model = "auto"  # default kiro/ACP slot — sentinel
        c.agent.cc_model = "global.anthropic.claude-opus-4-8[1m]"
        c.session.timeout_secs = 2
        return c

    @pytest.mark.asyncio
    async def test_cc_provider_uses_cc_model(self, cc_cfg):
        """provider=claude_code: fallback reads cc_model, not model."""
        captured: dict = {}
        mgr = SessionManager(cc_cfg, provider_factory=_capturing_factory(captured))
        await mgr.get_or_create("cli_chat")
        assert captured["model_override"] == "global.anthropic.claude-opus-4-8[1m]"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cc_provider_ignores_stale_kiro_model(self, cc_cfg):
        """A non-sentinel agent.model (e.g. left over from kiro use) MUST NOT
        override cc_model when the active provider is claude_code."""
        cc_cfg.agent.model = "claude-opus-4.6"  # stale kiro id, must be ignored
        captured: dict = {}
        mgr = SessionManager(cc_cfg, provider_factory=_capturing_factory(captured))
        await mgr.get_or_create("slack:dm")
        assert captured["model_override"] == "global.anthropic.claude-opus-4-8[1m]"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cc_provider_with_auto_cc_model_passes_none(self, cc_cfg):
        """cc_model=auto is a sentinel — caller-omitted model stays None so
        the factory defaults to _CC_DEFAULT_MODEL."""
        cc_cfg.agent.cc_model = "auto"
        captured: dict = {}
        mgr = SessionManager(cc_cfg, provider_factory=_capturing_factory(captured))
        await mgr.get_or_create("cli_chat")
        assert captured["model_override"] is None
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_cc_provider_explicit_model_still_wins(self, cc_cfg):
        """Explicit model= (e.g. dashboard slot picker) overrides cc_model."""
        captured: dict = {}
        mgr = SessionManager(cc_cfg, provider_factory=_capturing_factory(captured))
        await mgr.get_or_create("dashboard-slot", model="claude-sonnet-4.6")
        assert captured["model_override"] == "claude-sonnet-4.6"
        await mgr.close_all()

    @pytest.mark.asyncio
    async def test_acp_provider_unchanged(self, cfg):
        """Non-cc providers still fall back to agent.model (regression guard
        for the channel-agent fix this builds on)."""
        cfg.agent.provider = "acp"
        captured: dict = {}
        mgr = SessionManager(cfg, provider_factory=_capturing_factory(captured))
        await mgr.get_or_create("cli_chat", agent="deep-researcher")
        assert captured["model_override"] == "claude-opus-4.6"
        await mgr.close_all()
