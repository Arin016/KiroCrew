"""Unit tests for AcpProvider slash-command and compact routing.

claude-agent-acp does not implement the kiro-only
``_kiro.dev/commands/execute`` JSON-RPC method, so slash commands and
/compact must flow through ``session/prompt`` for that backend.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_claw.acp.types import ACP_BACKEND_CLAUDE, AcpEvent
from kiro_claw.providers.acp import AcpProvider


def _build_provider(backend: str) -> AcpProvider:
    with patch("kiro_claw.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    return provider


async def _drain(it):
    out = []
    async for x in it:
        out.append(x)
    return out


def _async_iter(items):
    async def _gen():
        for it in items:
            yield it

    return _gen()


class TestStreamCommandRouting:
    @pytest.mark.asyncio
    async def test_kiro_backend_uses_commands_execute(self):
        provider = _build_provider(backend="")
        provider._client.stream_command = MagicMock(
            return_value=_async_iter([AcpEvent(kind="text_chunk", text="ok")])
        )
        provider._client.stream_events = MagicMock(return_value=_async_iter([]))

        events = await _drain(provider.stream_command("/compact"))

        provider._client.stream_command.assert_called_once_with("/compact")
        provider._client.stream_events.assert_not_called()
        assert len(events) == 1
        assert events[0].text == "ok"

    @pytest.mark.asyncio
    async def test_claude_backend_uses_session_prompt(self):
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        provider._client.stream_events = MagicMock(
            return_value=_async_iter([AcpEvent(kind="text_chunk", text="ok")])
        )
        provider._client.stream_command = MagicMock(return_value=_async_iter([]))

        events = await _drain(provider.stream_command("/compact"))

        provider._client.stream_events.assert_called_once_with("/compact")
        provider._client.stream_command.assert_not_called()
        assert len(events) == 1
        assert events[0].text == "ok"


class TestCompactRouting:
    @pytest.mark.asyncio
    async def test_kiro_backend_uses_send_command(self):
        provider = _build_provider(backend="")
        provider._client.send_command = AsyncMock(return_value="")
        provider._client.stream_events = MagicMock(return_value=_async_iter([]))

        await provider.compact()

        provider._client.send_command.assert_awaited_once_with("/compact")
        provider._client.stream_events.assert_not_called()

    @pytest.mark.asyncio
    async def test_kiro_backend_send_command_with_context(self):
        provider = _build_provider(backend="")
        provider._client.send_command = AsyncMock(return_value="")

        await provider.compact("important context")

        provider._client.send_command.assert_awaited_once()
        sent = provider._client.send_command.await_args.args[0]
        assert sent.startswith("/compact ")
        assert "important context" in sent

    @pytest.mark.asyncio
    async def test_claude_backend_uses_session_prompt(self):
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        provider._client.send_command = AsyncMock(return_value="")
        provider._client.stream_events = MagicMock(
            return_value=_async_iter([AcpEvent(kind="text_chunk", text="x")])
        )

        await provider.compact()

        provider._client.stream_events.assert_called_once_with("/compact")
        provider._client.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_claude_backend_truncates_long_context(self):
        provider = _build_provider(backend=ACP_BACKEND_CLAUDE)
        provider._client.stream_events = MagicMock(return_value=_async_iter([]))

        await provider.compact("a" * 5000)

        sent = provider._client.stream_events.call_args.args[0]
        assert sent.startswith("/compact ")
        # context portion should be capped at 4000 chars
        body = sent.split("\n", 1)[1]
        assert len(body) == 4000


class TestEffortControl:
    """Provider-level effort orchestration: backend branch selection,
    capability gating, live-apply, and clear semantics."""

    def _effort_provider(self, backend: str, model: str) -> AcpProvider:
        provider = _build_provider(backend=backend)
        provider._client._model = model
        provider._client._work_dir = MagicMock()
        provider._client.send_command = AsyncMock()
        provider._client.set_config_option = AsyncMock()
        # Default: the session advertises an 'effort' option (modern adapter).
        provider._client.supports_config_option = MagicMock(return_value=True)
        return provider

    @pytest.mark.asyncio
    async def test_kiro_change_effort_pushes_slash_command_and_overlay(self):
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        with patch("kiro_claw.providers.acp._write_cli_overlay") as wco:
            ok = await provider.change_effort("xhigh")
        assert ok is True
        provider._client.send_command.assert_awaited_once_with("/effort", args={"level": "xhigh"})
        # kiro uses the overlay, never set_config_option
        provider._client.set_config_option.assert_not_awaited()
        wco.assert_called_once()
        assert provider._effort_per_model["claude-opus-4.7"] == "xhigh"

    @pytest.mark.asyncio
    async def test_claude_change_effort_uses_set_config_option(self):
        provider = self._effort_provider(
            backend=ACP_BACKEND_CLAUDE, model="global.anthropic.claude-opus-4-8[1m]"
        )
        ok = await provider.change_effort("high")
        assert ok is True
        provider._client.set_config_option.assert_awaited_once_with("effort", "high")
        # claude does NOT use the kiro /effort slash command
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claude_change_effort_steps_down_when_max_unsupported(self):
        # Adapter rejects "max" for a model whose ceiling is "xhigh"; the push
        # must fall back down the ladder and land "xhigh" rather than failing
        # the whole change (which would reset the session and lose state).
        from kiro_claw.acp.client import AcpError

        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")

        async def _reject_max(config_id, value):
            if value == "max":
                raise AcpError("Invalid value for config option effort: max")

        provider._client.set_config_option = AsyncMock(side_effect=_reject_max)
        ok = await provider.change_effort("max")
        assert ok is True
        calls = [c.args for c in provider._client.set_config_option.await_args_list]
        assert ("effort", "max") in calls  # tried the requested level first
        assert ("effort", "xhigh") in calls  # then stepped down and succeeded
        # The slot override keeps the requested level so a future
        # max-capable model would get it.
        assert provider._effort_per_model["claude-opus-4.7"] == "max"

    @pytest.mark.asyncio
    async def test_claude_change_effort_propagates_non_value_errors(self):
        # A transport/timeout error is NOT a value rejection — it must NOT be
        # swallowed by the ladder; it propagates so the caller rolls back.
        from kiro_claw.acp.client import AcpError

        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._client.set_config_option = AsyncMock(side_effect=AcpError("transport died"))
        with pytest.raises(AcpError, match="transport died"):
            await provider.change_effort("high")

    @pytest.mark.asyncio
    async def test_change_effort_noop_on_incapable_model(self):
        # 'auto' is genuinely effort-incapable (no concrete model selected).
        provider = self._effort_provider(backend="", model="auto")
        ok = await provider.change_effort("high")
        assert ok is False
        provider._client.send_command.assert_not_awaited()
        provider._client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_change_effort_rejects_invalid_level(self):
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        with pytest.raises(ValueError):
            await provider.change_effort("ultra")

    @pytest.mark.asyncio
    async def test_claude_apply_initial_effort_pushes_resolved_level(self):
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        await provider._apply_initial_effort()
        provider._client.set_config_option.assert_awaited_once_with("effort", "max")

    @pytest.mark.asyncio
    async def test_apply_initial_effort_noop_on_kiro_backend(self):
        # kiro gets effort from the spawn-time overlay, not a live push.
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        await provider._apply_initial_effort()
        provider._client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claude_apply_initial_effort_swallows_adapter_error(self):
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        provider._client.set_config_option = AsyncMock(side_effect=RuntimeError("bad"))
        # Must not raise — a rejected effort cannot break session start.
        await provider._apply_initial_effort()

    @pytest.mark.asyncio
    async def test_claude_initial_effort_skips_when_option_unsupported(self):
        # Older claude-agent-acp builds advertise no 'effort' config option;
        # the initial-effort push must be a silent no-op (no set_config_option,
        # no error) rather than spamming 'Unknown config option' every spawn.
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        provider._client.supports_config_option = MagicMock(return_value=False)
        await provider._apply_initial_effort()
        provider._client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claude_change_effort_returns_false_when_option_unsupported(self):
        # change_effort must report unsupported (False) instead of attempting a
        # push that fails with 'Unknown config option' and resets the session.
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._client.supports_config_option = MagicMock(return_value=False)
        ok = await provider.change_effort("high")
        assert ok is False
        provider._client.set_config_option.assert_not_awaited()
        # No poisoned override left behind.
        assert "claude-opus-4.7" not in provider._effort_per_model

    @pytest.mark.asyncio
    async def test_claude_set_effort_swallows_unknown_config_option(self):
        # Defense in depth: even if the capability guard is bypassed (e.g. the
        # option is advertised lazily), an 'Unknown config option' rejection
        # from the adapter must be skipped, not re-raised (which resets).
        from kiro_claw.acp.client import AcpError

        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        # Force the guard open so the ladder runs and hits the adapter error.
        provider._client.supports_config_option = MagicMock(return_value=True)
        provider._client.set_config_option = AsyncMock(
            side_effect=AcpError(
                "JSON-RPC error: {'code': -32603, 'message': 'Internal error', "
                "'data': {'details': 'Unknown config option: effort'}}"
            )
        )
        # Must not raise.
        await provider._set_claude_effort("max")

    @pytest.mark.asyncio
    async def test_kiro_clear_effort_no_default_returns_false_for_reset(self):
        # No workspace default resolves → overlay cleared, nothing pushed live,
        # and clear_effort returns FALSE so the handler resets the session
        # (kiro respawns at the model's built-in default). Returning True here
        # would leave the running session stuck at the old effort.
        provider = self._effort_provider(backend="", model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "high"}
        with patch("kiro_claw.providers.acp._clear_cli_overlay_effort") as cco:
            ok = await provider.clear_effort()
        assert ok is False
        assert "claude-opus-4.7" not in provider._effort_per_model
        cco.assert_called_once()
        provider._client.send_command.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_claude_clear_effort_returns_false_for_reset(self):
        # claude-agent-acp has no "reset to default" config value, so clearing
        # must return False to trigger a session reset; it must NOT push.
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._effort_per_model = {"claude-opus-4.7": "max"}
        ok = await provider.clear_effort()
        assert ok is False
        assert "claude-opus-4.7" not in provider._effort_per_model
        provider._client.set_config_option.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_change_effort_rolls_back_on_push_failure(self):
        # A failed live push must not leave a poisoned override/overlay that
        # would re-push the rejected level on every respawn.
        provider = self._effort_provider(backend=ACP_BACKEND_CLAUDE, model="claude-opus-4.7")
        provider._client.set_config_option = AsyncMock(side_effect=RuntimeError("rejected"))
        with pytest.raises(RuntimeError):
            await provider.change_effort("xhigh")
        # Override rolled back (was previously unset).
        assert "claude-opus-4.7" not in provider._effort_per_model

    def test_supports_effort_reflects_model(self):
        assert self._effort_provider(backend="", model="claude-opus-4.7").supports_effort()
        # 'auto' is genuinely effort-incapable (no concrete model selected). A raw
        # kiro 'claude-haiku-4.5' is also incapable — the haiku guard wins over
        # the registry's Sonnet fold (see test_effort.py).
        assert not self._effort_provider(backend="", model="auto").supports_effort()
        assert not self._effort_provider(backend="", model="claude-haiku-4.5").supports_effort()
