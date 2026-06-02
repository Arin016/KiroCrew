"""Tests for the llm_helpers module — shared LLM interaction utilities."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_claw.acp.client import AcpError
from kiro_claw.llm_helpers import (
    PromptBusyExhaustedError,
    ToolApprovalPolicy,
    parse_llm_json,
    parse_llm_json_list,
    save_conversation_turn,
    stream_and_collect,
)
from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent


class TestParseLlmJson:
    def test_valid_json(self) -> None:
        assert parse_llm_json('{"key": "value"}') == {"key": "value"}

    def test_json_with_fences(self) -> None:
        text = '```json\n{"key": "value"}\n```'
        assert parse_llm_json(text) == {"key": "value"}

    def test_json_with_plain_fences(self) -> None:
        text = '```\n{"key": "value"}\n```'
        assert parse_llm_json(text) == {"key": "value"}

    def test_empty_string(self) -> None:
        assert parse_llm_json("") is None

    def test_whitespace_only(self) -> None:
        assert parse_llm_json("   \n  ") is None

    def test_invalid_json(self) -> None:
        assert parse_llm_json("not json") is None

    def test_returns_none_for_list(self) -> None:
        assert parse_llm_json("[1, 2, 3]") is None

    def test_returns_none_for_string(self) -> None:
        assert parse_llm_json('"just a string"') is None

    def test_nested_fences(self) -> None:
        text = '```json\n{"code": "```"}\n```'
        # Should handle gracefully — the inner ``` gets split
        result = parse_llm_json(text)
        # May or may not parse, but should not raise
        assert result is None or isinstance(result, dict)

    def test_whitespace_around_json(self) -> None:
        text = '  \n  {"a": 1}  \n  '
        assert parse_llm_json(text) == {"a": 1}


class TestParseLlmJsonList:
    def test_valid_list(self) -> None:
        assert parse_llm_json_list('[{"title": "a"}]') == [{"title": "a"}]

    def test_list_with_fences(self) -> None:
        text = '```json\n[{"title": "a"}]\n```'
        assert parse_llm_json_list(text) == [{"title": "a"}]

    def test_empty_string(self) -> None:
        assert parse_llm_json_list("") is None

    def test_returns_none_for_dict(self) -> None:
        assert parse_llm_json_list('{"key": "value"}') is None

    def test_invalid_json(self) -> None:
        assert parse_llm_json_list("not json") is None


class TestSaveConversationTurn:
    def test_saves_user_and_assistant(self) -> None:
        log = MagicMock()
        save_conversation_turn(log, "key1", "hello", "world")
        assert log.append.call_count == 2
        log.append.assert_any_call("key1", "user", "hello", source_thread=None, source_user=None)
        log.append.assert_any_call(
            "key1", "assistant", "world", source_thread=None, source_user=None
        )

    def test_saves_with_provenance(self) -> None:
        log = MagicMock()
        save_conversation_turn(log, "key1", "hello", "world", source_thread="t1", source_user="u1")
        log.append.assert_any_call("key1", "user", "hello", source_thread="t1", source_user="u1")
        log.append.assert_any_call(
            "key1", "assistant", "world", source_thread="t1", source_user="u1"
        )

    def test_skips_empty_assistant(self) -> None:
        log = MagicMock()
        save_conversation_turn(log, "key1", "hello", "")
        assert log.append.call_count == 1
        log.append.assert_called_once_with(
            "key1", "user", "hello", source_thread=None, source_user=None
        )


class TestToolApprovalPolicy:
    def test_enum_values(self) -> None:
        assert ToolApprovalPolicy.AUTO_APPROVE.value == "auto_approve"
        assert ToolApprovalPolicy.REJECT_ALL.value == "reject_all"
        assert ToolApprovalPolicy.HOOK_BASED.value == "hook_based"


# ── Prompt-busy retry tests ──


def _make_provider(events=None, error=None):
    """Create a mock LLMProvider that yields events or raises."""
    provider = AsyncMock()
    provider.cancel = AsyncMock()
    provider.shutdown = AsyncMock()

    async def _stream(msg):
        if error:
            raise error
        for e in events or []:
            yield e

    provider.stream = _stream
    return provider


class TestStreamAndCollectPromptBusy:
    @pytest.mark.asyncio
    async def test_retries_on_prompt_busy_then_succeeds(self) -> None:
        """First call raises 'already in progress', second succeeds."""
        call_count = 0

        async def _stream(msg):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise AcpError("Prompt error: {'data': 'Prompt already in progress'}")
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok")
            yield LLMEvent(kind=EVENT_COMPLETE)

        provider = AsyncMock()
        provider.cancel = AsyncMock()
        provider.shutdown = AsyncMock()
        provider.stream = _stream

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await stream_and_collect(provider, "test")

        assert result == "ok"
        assert call_count == 2
        provider.cancel.assert_awaited_once()
        provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_shuts_down_provider_after_retries_exhausted(self) -> None:
        """After all retries fail, provider.shutdown() is called."""
        provider = _make_provider(error=AcpError("already in progress"))

        with patch("asyncio.sleep", new_callable=AsyncMock), pytest.raises(
            PromptBusyExhaustedError
        ):
            await stream_and_collect(provider, "test")

        provider.shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_non_busy_error_raises_immediately(self) -> None:
        """Non-busy AcpError is not retried."""
        provider = _make_provider(error=AcpError("some other error"))

        with pytest.raises(AcpError, match="some other error"):
            await stream_and_collect(provider, "test")

        provider.cancel.assert_not_awaited()
        provider.shutdown.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_normal_stream_no_retry(self) -> None:
        """Normal stream completes without retry."""
        provider = _make_provider(
            events=[
                LLMEvent(kind=EVENT_TEXT_CHUNK, text="hello"),
                LLMEvent(kind=EVENT_COMPLETE),
            ]
        )

        result = await stream_and_collect(provider, "test")

        assert result == "hello"
        provider.cancel.assert_not_awaited()
