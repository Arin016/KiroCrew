"""Shared LLM interaction helpers — stream collection, JSON parsing, history saving.

Eliminates duplicate code across gateway, handler, dashboard, taskrunner,
subagent, and history modules.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import TYPE_CHECKING

from kiro_claw.acp.client import AcpError
from kiro_claw.hooks import fire_tool_hooks, get_global_hook_store
from kiro_claw.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    LLMEvent,
    LLMProvider,
)
from kiro_claw.sel import sel as _sel

_PROMPT_BUSY_RETRIES = 2
_PROMPT_BUSY_DELAY = 1.5  # seconds between retries

# Transient backend (Bedrock 5xx / throttle / stream-reset) retry budget. These
# are server-side hiccups where the credential is VALID — retry helps, re-auth
# does not. Kept separate from the prompt-busy budget above.
_TRANSIENT_RETRIES = 3
_TRANSIENT_DELAY = 2.0  # base seconds; exponential backoff + jitter

# Per-process RNG for retry jitter, auto-seeded from os.urandom at import. The
# entropy seed spreads jitter uniformly *across* processes/machines — so a
# fleet-wide transient (e.g. several gateways hitting the same backend 5xx at the
# same minute) doesn't retry in lockstep and re-thunder the recovering backend.
# Auto-seeding (rather than os.getpid()) is container-safe: under Docker/ECS/K8s
# the gateway is commonly PID 1, so a PID seed would be identical fleet-wide and
# collapse the spread. Tests that need determinism patch asyncio.sleep (so the
# jitter value is never observed) or reseed _JITTER_RNG in a fixture.
_JITTER_RNG = random.Random()

# Substrings (lowercased) that mark a RETRYABLE transient backend failure.
# Matched against the formatted AcpError message (see acp.client._format_acp_error).
# Auth/validation markers are deliberately ABSENT so those fail fast — a retry
# cannot fix an expired token or a bad request, and silently retrying them would
# only delay the correct "re-auth"/"fix the request" signal to the operator.
_TRANSIENT_MARKERS = (
    "internal server error",
    "internal error: api error",
    "serviceunavailable",
    "service unavailable",
    "throttl",                 # ThrottlingException + "Bedrock is throttling"
    "toomanyrequests",
    "servicequotaexceeded",
    "modelstreamerror",
    "connection reset",
    "connectionreset",
    "is unavailable on bedrock",  # capacity/region rollout (formatted message)
)


def _is_transient_acp_error(msg: str) -> bool:
    """True iff an AcpError message looks like a retryable transient backend
    failure. Auth failures are explicitly excluded (they need re-auth, not retry)."""
    low = msg.lower()
    if (
        "authentication failed" in low
        or "accessdenied" in low
        or "expiredtoken" in low
        or "unrecognizedclient" in low
        or "invalidsignature" in low
    ):
        return False
    return any(m in low for m in _TRANSIENT_MARKERS)


class PromptBusyExhaustedError(Exception):
    """Provider was shut down after prompt-busy retries were exhausted."""


if TYPE_CHECKING:
    from kiro_claw.history import ConversationLog
    from kiro_claw.hooks import HookManager

logger = logging.getLogger(__name__)


# ── Tool Approval Policies ──


class ToolApprovalPolicy(Enum):
    """How to handle tool permission requests during streaming."""

    AUTO_APPROVE = "auto_approve"
    REJECT_ALL = "reject_all"
    HOOK_BASED = "hook_based"


# Callback type for custom tool approval logic
OnPermissionCallback = Callable[[LLMEvent], Awaitable[bool]]


# ── Stream and Collect ──


async def stream_and_collect(
    provider: LLMProvider,
    message: str,
    *,
    approval_policy: ToolApprovalPolicy = ToolApprovalPolicy.AUTO_APPROVE,
    hooks: HookManager | None = None,
    on_chunk: Callable[[str], None] | None = None,
    on_tool_approval: Callable[[LLMEvent], Awaitable[bool]] | None = None,
    retry_transient: bool = True,
) -> str:
    """Stream a message through an LLM provider and collect the full response.

    This is the core pattern used by cron, heartbeat, subagent, consolidator,
    taskrunner, and title generation.

    Args:
        provider: The LLM provider to stream through.
        message: The prompt to send.
        approval_policy: How to handle tool permission requests.
        hooks: HookManager for HOOK_BASED approval policy.
        on_chunk: Optional callback invoked with each text chunk (for progress).
        on_tool_approval: Optional async callback for interactive approval.
        retry_transient: When True (default), transient backend errors are
            retried in-place with bounded backoff. Set False from callers that
            already own an outer transient-retry loop, so the inner arm doesn't
            compound their attempts (retry-layer amplification).

    Returns:
        The complete response text.
    """
    transient_attempts = 0
    attempt = 0
    while True:
        result_text = ""
        try:
            async for event in provider.stream(message):
                if event.kind == EVENT_TEXT_CHUNK:
                    result_text += event.text
                    if on_chunk:
                        on_chunk(event.text)
                elif event.kind == EVENT_PERMISSION_REQUEST:
                    approved = await _resolve_permission(
                        provider, event, approval_policy, hooks, on_tool_approval
                    )
                    if not approved:
                        continue
                elif event.kind == EVENT_TOOL_CALL:
                    # Fire PreToolUse hooks for auto-approved tools (informational only)
                    _sel().log_tool_invocation(
                        session_key="",
                        source="llm_helpers",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                    )
                    await fire_tool_hooks(
                        get_global_hook_store(), event.title, event.tool_input,
                    )
                elif event.kind == EVENT_COMPLETE:
                    break
            return result_text
        except AcpError as exc:
            msg = str(exc)
            busy = "already in progress" in msg

            # ── Case 1: prompt-busy (provider mid-turn) — cancel + retry. ──
            if busy:
                if attempt >= _PROMPT_BUSY_RETRIES:
                    # Provider is permanently stuck — kill it so the next
                    # get_or_create cold-starts a fresh process.
                    logger.warning(
                        "Prompt busy after %d retries, shutting down provider", _PROMPT_BUSY_RETRIES
                    )
                    try:
                        await provider.shutdown()
                    except Exception:
                        logger.debug("Provider shutdown after busy retries failed", exc_info=True)
                    raise PromptBusyExhaustedError(msg) from exc
                logger.warning(
                    "Prompt busy (attempt %d/%d), cancelling and retrying: %s",
                    attempt + 1, _PROMPT_BUSY_RETRIES, exc,
                )
                try:
                    await provider.cancel()
                except Exception:
                    logger.debug("Cancel before retry failed", exc_info=True)
                await asyncio.sleep(_PROMPT_BUSY_DELAY * (2**attempt))
                attempt += 1
                continue

            # ── Case 2: transient backend (Bedrock 5xx / throttle / stream) ──
            # Credential is valid; the server hiccupped. Retry with exponential
            # backoff + jitter. Distinct budget from prompt-busy.
            #
            # Guards:
            #   - retry_transient: callers that own an outer transient loop pass
            #     False so the inner arm doesn't compound their attempts.
            #   - `not result_text`: only retry if NO tokens have streamed yet.
            #     A partial response must not be retried — the re-run would
            #     duplicate the already-emitted output (Mesh-1157).
            if (
                retry_transient
                and not result_text
                and _is_transient_acp_error(msg)
                and transient_attempts < _TRANSIENT_RETRIES
            ):
                transient_attempts += 1
                # Exponential backoff with per-process jitter (see _JITTER_RNG):
                # deterministic within a process for tests, uniform across the
                # fleet so co-located peers don't retry in lockstep.
                base = _TRANSIENT_DELAY * (2 ** (transient_attempts - 1))
                jitter = _JITTER_RNG.random() * 0.25 * base
                delay = base + jitter
                logger.warning(
                    "Transient backend error (attempt %d/%d), retrying in %.1fs: %s",
                    transient_attempts, _TRANSIENT_RETRIES, delay, exc,
                )
                await asyncio.sleep(delay)
                continue

            # ── Case 3: fatal (auth, validation, exhausted retries) — propagate. ──
            raise


async def stream_and_collect_json(
    provider: LLMProvider,
    message: str,
    *,
    approval_policy: ToolApprovalPolicy = ToolApprovalPolicy.AUTO_APPROVE,
    hooks: HookManager | None = None,
) -> dict | None:
    """Stream a message and parse the response as JSON.

    Combines ``stream_and_collect`` with ``parse_llm_json``.
    Returns parsed dict or None on failure.
    """
    text = await stream_and_collect(provider, message, approval_policy=approval_policy, hooks=hooks)
    return parse_llm_json(text)


async def _resolve_permission(
    provider: LLMProvider,
    event: LLMEvent,
    policy: ToolApprovalPolicy,
    hooks: HookManager | None,
    on_tool_approval: Callable[[LLMEvent], Awaitable[bool]] | None = None,
    session_key: str = "",
    agent: str = "",
) -> bool:
    """Resolve a tool permission request. Returns True if approved."""
    from kiro_claw.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
    from kiro_claw.sel import sel

    def _log(outcome: str, **extra):
        sel().log_tool_invocation(
            session_key=session_key,
            agent=agent,
            tool_name=event.title,
            tool_kind=event.tool_kind,
            outcome=outcome,
            request_id=event.request_id,
            **extra,
        )

    if policy == ToolApprovalPolicy.REJECT_ALL:
        await provider.reject_tool(event.request_id)
        _log("rejected", metadata={"reason": "reject_all_policy"})
        return False

    if policy == ToolApprovalPolicy.HOOK_BASED and hooks:
        tool_result = hooks.on_tool_call(event.title)
        if tool_result.action == TOOL_DENY:
            await provider.reject_tool(event.request_id)
            _log("denied", error=tool_result.reason)
            return False
        if tool_result.action == TOOL_AUTO_APPROVE:
            await provider.approve_tool(event.request_id)
            _log("auto_approved", metadata={"reason": "hook_auto_approve"})
            return True

    # Interactive approval if callback provided
    if on_tool_approval:
        approved = await on_tool_approval(event)
        if not approved:
            await provider.reject_tool(event.request_id)
            _log("rejected", metadata={"reason": "interactive_rejected"})
            return False

    # Default: auto-approve
    await provider.approve_tool(event.request_id)
    _log("auto_approved")
    return True


# ── JSON Parsing ──


_JSON_DECODER = json.JSONDecoder()


def _extract_json_of_type(text: str, expected_type: type) -> dict | list | None:
    """Extract the first top-level JSON value of *expected_type* embedded in prose.

    Scans successive ``{`` (dict) or ``[`` (list) offsets and uses the stdlib
    ``raw_decode`` to parse a complete JSON value at each — this validates the
    full JSON grammar and correctly handles nesting and string escapes. Returns
    the first value that matches *expected_type*, or None.

    Scanning successive offsets (rather than committing to the first delimiter)
    is what makes this robust to a stray structural brace in the prose preamble
    (e.g. ``"use {placeholder}: {\\"a\\": 1}"``). Only TOP-LEVEL matches count: a
    ``{`` nested inside an earlier-starting ``[ ... ]`` is consumed by that
    array's decode, so a dict request never digs a nested object out of a
    surrounding array.
    """
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Only attempt a decode at a JSON container start. Scanning BOTH
        # delimiters in positional order (not just the expected one) is what
        # prevents digging a nested object out of a surrounding array: a
        # leading "[ ... ]" is decoded as a list, found to be the wrong type,
        # and skipped past in full — so a dict request on "[1, {\\"a\\":2}]"
        # returns None rather than the inner {"a":2}.
        if ch not in "{[":
            i += 1
            continue
        try:
            data, end = _JSON_DECODER.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(data, expected_type):
            return data  # type: ignore[return-value]
        # Valid JSON of the wrong type — skip past its full extent.
        i = end
    return None


def _parse_llm(text: str, expected_type: type) -> dict | list | None:
    """Parse JSON from LLM output, tolerating fences and surrounding prose.

    Background turns (e.g. memory consolidation) run on a shared lite session.
    On the Claude Code backend that session is not tool/persona-scoped the way
    kiro's no-tools lite agent is, so the model may wrap the JSON in prose. To
    keep consolidation from silently no-opping, fall back to extracting the
    first top-level JSON value of the expected type when a strict parse fails.
    """
    text = text.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        data = json.loads(text)
        if isinstance(data, expected_type):
            return data  # type: ignore[return-value]
        return None
    except json.JSONDecodeError:
        # Fallback: extract the first top-level JSON value of the expected type
        # embedded in prose (scans successive delimiters, validates via stdlib).
        result = _extract_json_of_type(text, expected_type)
        if result is None:
            logger.debug("Failed to parse LLM JSON: %.200s", text)
        return result


def parse_llm_json(text: str) -> dict | None:
    """Parse JSON dict from LLM output, stripping markdown fences if present."""
    return _parse_llm(text, dict)  # type: ignore[return-value]


def parse_llm_json_list(text: str) -> list | None:
    """Parse a JSON array from LLM output, stripping markdown fences."""
    return _parse_llm(text, list)  # type: ignore[return-value]


# ── Conversation History Helpers ──


def save_conversation_turn(
    log: ConversationLog,
    key: str,
    user_text: str,
    assistant_text: str,
    source_thread: str | None = None,
    source_user: str | None = None,
) -> None:
    """Save a user+assistant conversation turn to the history log.

    Consolidates the repeated pattern of appending user and assistant
    messages with provenance tracking.
    """
    log.append(
        key,
        "user",
        user_text,
        source_thread=source_thread,
        source_user=source_user,
    )
    if assistant_text:
        log.append(
            key,
            "assistant",
            assistant_text,
            source_thread=source_thread,
            source_user=source_user,
        )
