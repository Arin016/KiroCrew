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
    # The default provider is now ``acp`` (KiroACP/kiro-cli). These tests
    # exercise the ``agent.model`` fallback path, so pin the provider to
    # ``acp`` explicitly to be robust to future default changes.
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
