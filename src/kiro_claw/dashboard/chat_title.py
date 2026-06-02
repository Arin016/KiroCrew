"""Title generation — auto-title, rename, plan rephrase."""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_claw.config.loader import config_dir
from kiro_claw.context_management import extract_plan_metadata, rephrase_plan
from kiro_claw.dashboard.chat_utils import _history_key_for
from kiro_claw.dashboard.state import DashboardState, _ChatSlot
from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_claw.security import redact_credentials, redact_exfiltration_urls
from kiro_claw.sel import sel
from kiro_claw.session import BACKGROUND_KEY

logger = logging.getLogger(__name__)

# Max turns to attempt auto-titling before giving up
_TITLE_MAX_ATTEMPTS = 5

_TITLE_PROMPT_TEMPLATE = (
    "You are a session naming agent. Given the conversation below, decide if the topic "
    "is clear enough to name.\n\n"
    "If YES: reply with ONLY a short title (3-6 words). No quotes, no punctuation.\n"
    "If NO (too vague, just greetings, or unclear topic): reply with exactly SKIP\n\n"
    "{transcript}"
)


def _build_title_prompt(messages: list[dict[str, str]]) -> str | None:
    """Build a title generation prompt from conversation messages."""
    lines: list[str] = []
    for m in messages[:10]:
        role = m.get("role", "")
        content = m.get("content", "")
        if role in ("user", "assistant") and content:
            lines.append(f"{role}: {content[:200]}")
    if not lines:
        return None
    return _TITLE_PROMPT_TEMPLATE.format(transcript="\n".join(lines))


def _reset_auto_run_for_new_plan(slot: "_ChatSlot") -> None:
    """Clear auto-run state so a new plan requires fresh user approval."""
    session_dir = config_dir() / "sessions" / slot.key
    if session_dir.exists():
        for f in session_dir.glob("stage_*_result.md"):
            try:
                f.unlink()
            except OSError:
                pass
    slot._orch_tracker = None
    slot._auto_run = False


def _extract_and_redact_plan_metadata(text: str) -> tuple[list[str], str, list[list[str]]]:
    """Extract stage titles, goal, and descriptions from plan text, redacted."""
    titles, goal, descriptions = extract_plan_metadata(text)
    titles = [redact_credentials(redact_exfiltration_urls(t)[0])[0] for t in titles]
    if goal:
        goal = redact_credentials(redact_exfiltration_urls(goal)[0])[0]
    descriptions = [
        [redact_credentials(redact_exfiltration_urls(d)[0])[0] for d in stage_descs]
        for stage_descs in descriptions
    ]
    return titles, goal, descriptions


async def _rephrase_plan_lite(
    state: DashboardState,
    text: str,
    issues: list[str],
    *,
    might_not_be_plan: bool = False,
) -> str | None:
    """Rephrase a plan using the cheap background session (kiroclaw-lite)."""

    try:
        bg, _new, _resumed = await state.sessions.get_or_create(BACKGROUND_KEY)
    except Exception:
        logger.warning("Failed to get background session for plan rephrase", exc_info=True)
        return None
    try:
        result = await rephrase_plan(text, issues, bg, might_not_be_plan=might_not_be_plan)
    finally:
        state.sessions.release(BACKGROUND_KEY)
        # Recycle the shared BG session if it's accumulated too much context.
        # Without this, repeated dashboard plan-rephrases bloat the kiro-cli
        # child until a mid-stream recycle eventually kills an in-flight call,
        # blocking every chat queued behind the BG session for minutes.
        await state.sessions.recycle_background()
    if result:
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)
    return result


async def _generate_title_via_kiro(state: DashboardState, messages: list[dict[str, str]]) -> str:
    """Generate a title using the shared background kiro-cli session."""

    prompt = _build_title_prompt(messages)
    if not prompt:
        logger.debug("Title generation skipped — no usable messages")
        return ""

    logger.debug("Title generation prompt (%d chars): %s", len(prompt), prompt[:120])
    client, _is_new, _resumed = await state.sessions.get_or_create(BACKGROUND_KEY)
    text = ""
    try:
        async for event in client.stream(prompt):
            if event.kind == EVENT_TEXT_CHUNK:
                text += event.text
            elif event.kind == EVENT_PERMISSION_REQUEST:
                await client.reject_tool(event.request_id)
            elif event.kind == EVENT_COMPLETE:
                break
    finally:
        state.sessions.release(BACKGROUND_KEY)
        # Recycle the shared BG session if it's accumulated too much context.
        # Without this, every auto-title appends to the kiro-cli child's
        # internal history; after ~N titles the session bloats past 70-90%
        # context and the next call gets killed mid-stream by a recycle
        # triggered elsewhere, also blocking every chat queued on the BG.
        await state.sessions.recycle_background()
    title = text.strip().strip('"').strip("'").strip(".")
    if not title or title.upper() == "SKIP":
        logger.info("Title generation returned SKIP/empty — topic not clear yet")
        return ""
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    logger.info("Title generated: %r", title[:80])
    return title[:80]


def _persist_title(state: DashboardState, slot: _ChatSlot) -> None:
    """Save the slot title to the conversation history file."""

    if state.conversation_log:
        history_key = _history_key_for(slot.key)
        try:
            state.conversation_log.set_title(history_key, slot.title)
            logger.debug("Persisted title %r for slot %s", slot.title, slot.key)
        except Exception:
            logger.debug("Failed to persist title for slot %s", slot.key)


async def _maybe_auto_title(state: DashboardState, slot: _ChatSlot) -> None:
    """Background task: attempt to auto-title a slot after a response completes."""
    if slot._titled:
        return
    if slot.blocks_reads:
        return
    user_count = sum(1 for m in slot.messages if m.get("role") == "user")
    if user_count < 1 or user_count > _TITLE_MAX_ATTEMPTS:
        if user_count > _TITLE_MAX_ATTEMPTS and not slot._titled:
            first_user = next((m["content"] for m in slot.messages if m.get("role") == "user"), "")
            slot.title = first_user[:60] or slot.key
            slot._titled = True
            _persist_title(state, slot)
            state.push_slot_title(slot.key, slot.title)
        return
    logger.info("Auto-title: attempting for slot %s (turn %d)", slot.key, user_count)
    try:
        title = await _generate_title_via_kiro(state, slot.messages)
        logger.info("Auto-title: kiro returned %r for slot %s", title, slot.key)
        if title:
            slot.title = title
            slot._titled = True
            _persist_title(state, slot)
            state.push_slot_title(slot.key, title)
    except Exception:
        logger.warning("Auto-title failed for slot %s", slot.key, exc_info=True)


async def api_chat_slot_generate_title(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/generate-title — manually trigger title generation."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    logger.info("Manual title generation requested for slot %s", name)
    try:
        title = await _generate_title_via_kiro(state, slot.messages)
    except Exception:
        logger.debug("Title generation failed for slot %s", name, exc_info=True)
        user_msgs = [m for m in slot.messages if m.get("role") == "user"]
        title = user_msgs[0].get("content", "")[:60] if user_msgs else ""

    if title:
        slot.title = title
        slot._titled = True
        _persist_title(state, slot)
        state.push_slot_title(slot.key, title)

    return web.json_response({"ok": True, "title": title})


async def api_chat_slot_rename(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/title — rename a chat session."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON"}, status=400)
    title = body.get("title", "").strip()[:200]
    if not title:
        return web.json_response({"error": "title required"}, status=400)
    slot.title = title
    slot._titled = True
    _persist_title(state, slot)
    state.push_slot_title(slot.key, title)
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_rename",
        outcome="allowed",
        source="dashboard",
        resources=slot.key,
    )
    return web.json_response({"ok": True, "title": title})
