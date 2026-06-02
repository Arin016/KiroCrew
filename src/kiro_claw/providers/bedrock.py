"""Bedrock provider — direct Bedrock converse_stream() API, text-only (no tools)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from kiro_claw.providers.base import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    CancelOutcome,
    LLMEvent,
    LLMProvider,
)

logger = logging.getLogger(__name__)

# Max conversation history entries before trimming oldest
_MAX_HISTORY = 50

# Model → context window tokens (loaded from shared JSON)
_TOKENS_FILE = Path(__file__).resolve().parent.parent / "model_tokens.json"
_CONTEXT_WINDOWS: dict[str, int] = {}
if _TOKENS_FILE.exists():
    with open(_TOKENS_FILE) as f:
        _CONTEXT_WINDOWS = {k: v for k, v in json.load(f).items() if not k.startswith("_")}

# Default model
DEFAULT_BEDROCK_MODEL = "anthropic.claude-sonnet-4-20250514"


class BedrockProvider(LLMProvider):
    """LLMProvider backed by Amazon Bedrock converse_stream() API.

    Text-only — no tool execution. Conversation history managed in-memory.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_BEDROCK_MODEL,
        region: str = "us-west-2",
        system_prompt: str | None = None,
    ) -> None:
        self._model_id = model_id
        self._region = region
        self._system_prompt = system_prompt
        self._history: list[dict] = []
        self._client = None  # type: ignore[assignment]
        self._last_context_pct: float = 0.0

    async def start(self) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise RuntimeError(
                "BedrockProvider requires boto3 and valid AWS credentials. "
                "Install the optional AWS extra (e.g. `pip install boto3`) and configure "
                "AWS credentials to use the Bedrock backend."
            ) from exc

        self._client = boto3.client("bedrock-runtime", region_name=self._region)
        logger.info("Bedrock provider ready: model=%s region=%s", self._model_id, self._region)

    async def shutdown(self) -> None:
        self._history.clear()
        self._client = None

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        if not self._client:
            raise RuntimeError("Bedrock provider not started")

        self._history.append({"role": "user", "content": [{"text": message}]})

        # Trim history if too long
        if len(self._history) > _MAX_HISTORY:
            self._history = self._history[-_MAX_HISTORY:]

        kwargs: dict = {
            "modelId": self._model_id,
            "messages": self._history,
        }
        if self._system_prompt:
            kwargs["system"] = [{"text": self._system_prompt}]

        response = self._client.converse_stream(**kwargs)

        assistant_text = ""
        for event in response.get("stream", []):
            if "contentBlockDelta" in event:
                delta = event["contentBlockDelta"].get("delta", {})
                text = delta.get("text", "")
                if text:
                    assistant_text += text
                    yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=text)
            elif "metadata" in event:
                usage = event["metadata"].get("usage", {})
                input_tokens = usage.get("inputTokens", 0)
                output_tokens = usage.get("outputTokens", 0)
                total = input_tokens + output_tokens
                # Context usage estimate based on model's context window
                if total > 0:
                    ctx = _CONTEXT_WINDOWS.get(self._model_id, 200_000)
                    self._last_context_pct = (input_tokens / ctx) * 100

        # Track assistant response in history
        if assistant_text:
            self._history.append({"role": "assistant", "content": [{"text": assistant_text}]})

        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> None:
        pass  # No tools in Bedrock text-only mode

    async def reject_tool(self, request_id: str | int) -> None:
        pass  # No tools in Bedrock text-only mode

    def context_usage_pct(self) -> float:
        return self._last_context_pct

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        """Bedrock has no in-flight turn state."""
        return "no_turn"
