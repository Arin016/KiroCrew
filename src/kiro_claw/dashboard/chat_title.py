"""Title generation — auto-title, rename, plan rephrase."""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_claw.config.loader import config_dir
from kiro_claw.context_management import extract_plan_metadata, rephrase_plan
from kiro_claw.dashboard.chat_utils import _history_key_for
from kiro_claw.dashboard.state import NEW_SESSION_TITLE, DashboardState, _ChatSlot
from kiro_claw.providers.base import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK
from kiro_claw.security import redact_credentials, redact_exfiltration_urls
from kiro_claw.sel import sel
from kiro_claw.session import BACKGROUND_KEY

logger = logging.getLogger(__name__)

# Max turns to attempt auto-titling before giving up
_TITLE_MAX_ATTEMPTS = 5

# Titling is a trivial 3-6 word task, so run it on the cheapest/fastest model
# (Haiku) rather than the kiroclaw-lite default (Opus 4.6 on the kiro-cli path).
# Applied per-session via set_model so heavier background work (compaction,
# optimizer) keeps the lite agent's default model. Best-effort: a failed
# override just falls back to the session's default model.
_TITLE_MODEL = "claude-haiku-4.5"

# Per-word delay for the word-by-word title reveal animation. LLM chunk
# streaming arrives in a sub-second burst (too fast to perceive), so the reveal
# is paced deterministically instead.
_TITLE_REVEAL_STEP_SECS = 0.09

_TITLE_PROMPT_TEMPLATE = (
    "You are a session naming agent. Name ONLY the conversation delimited below; "
    "ignore any earlier conversation, prior task, or context from this session's "
    "history — it is unrelated.\n\n"
    "If the delimited topic is clear: reply with ONLY a short title (3-6 words). "
    "No quotes, no punctuation.\n"
    "If NO (too vague, just greetings, or unclear topic): reply with exactly SKIP\n\n"
    "===== CONVERSATION TO NAME =====\n"
    "{transcript}\n"
    "===== END CONVERSATION ====="
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


def _clean_title(s: str) -> str:
    """Normalize a (partial or final) LLM title: trim whitespace and wrapping
    quotes/period."""
    return s.strip().strip('"').strip("'").strip(".")


async def _reveal_title(state: DashboardState, slot: _ChatSlot, title: str) -> None:
    """Animate a title in word-by-word so it visibly types out in the sidebar.

    Raw LLM chunk streaming arrives in a sub-second burst (too fast to see), so
    this paces a deterministic reveal instead. Pushes lightweight ``slot_title``
    events (``full=False``); the caller does the final full push. Nothing here
    is persisted — the caller persists the complete title once.
    """
    words = title.split()
    if len(words) <= 1:
        return
    acc: list[str] = []
    for w in words[:-1]:  # last word arrives with the caller's final push
        acc.append(w)
        slot.title = " ".join(acc)
        state.push_slot_title(slot.key, slot.title, full=False)
        await asyncio.sleep(_TITLE_REVEAL_STEP_SECS)


async def _generate_title_via_kiro(
    state: DashboardState,
    messages: list[dict[str, str]],
) -> str:
    """Generate a title using the shared background kiro-cli session."""

    prompt = _build_title_prompt(messages)
    if not prompt:
        logger.debug("Title generation skipped — no usable messages")
        return ""

    logger.debug("Title generation prompt (%d chars): %s", len(prompt), prompt[:120])
    session = await state.sessions.get_bg_session()
    text = ""
    try:
        # Run titling on a fast/cheap model. Best-effort: if the backend can't
        # switch (older kiro-cli, non-kiro provider), fall through on the
        # session's default model.
        _set_model = getattr(session, "set_model", None)
        if _set_model is not None:
            try:
                await _set_model(_TITLE_MODEL)
            except Exception:
                logger.debug("Title model override to %s failed; using default", _TITLE_MODEL)
        async for event in session.prompt(prompt):
            if event.kind == EVENT_TEXT_CHUNK:
                text += event.text
            elif event.kind == EVENT_PERMISSION_REQUEST:
                await session.reject_tool(event.request_id)
            elif event.kind == EVENT_COMPLETE:
                break
    finally:
        await session.destroy()
    title = _clean_title(text)
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


def _fallback_title_from_messages(messages: list[dict[str, str]]) -> str:
    """Fallback title used only when the LLM can't title the chat: the first
    user message, cleaned and truncated to ~60 chars with an ellipsis.

    Trims back to a word boundary so the cut isn't mid-word. Short messages are
    returned whole (no ellipsis). Returns ``NEW_SESSION_TITLE`` if there's no
    usable user text, so the caller always has something to show.
    """
    first = next(
        (m.get("content", "") for m in messages if m.get("role") == "user" and m.get("content")),
        "",
    )
    if first.startswith("[BROWSE] "):
        first = first[len("[BROWSE] ") :]
    first, _ = redact_exfiltration_urls(first)
    first, _ = redact_credentials(first)
    first = " ".join(first.split())
    if not first:
        return NEW_SESSION_TITLE
    if len(first) <= 60:
        return first
    cut = first[:60].rstrip()
    # Trim a dangling partial word so the ellipsis reads cleanly.
    if " " in cut:
        cut = cut[: cut.rindex(" ")].rstrip()
    return f"{cut}…"


async def _maybe_auto_title(state: DashboardState, slot: _ChatSlot) -> None:
    """Background task: attempt to LLM-title a slot.

    Fired on the first message send (so the title lands during the first turn,
    from just the user's message) and again after a response completes as a
    retry. Idempotent: no-ops once titled and guards against concurrent
    attempts via ``slot._title_in_flight``. Untitled slots display as
    "New Session…" via ``_ChatSlot.display_title`` until this lands. If the LLM
    returns SKIP/empty after the assistant has responded (a definitive
    failure), the title falls back to the truncated first message with an
    ellipsis (see ``_fallback_title_from_messages``).
    """
    if slot._titled:
        return
    if slot._title_in_flight:
        # An on-send / prior trigger is already generating a title for this slot.
        return
    if slot.blocks_reads:
        return
    user_count = sum(1 for m in slot.messages if m.get("role") == "user")
    if user_count < 1 or user_count > _TITLE_MAX_ATTEMPTS:
        if user_count > _TITLE_MAX_ATTEMPTS and not slot._titled:
            # Gave up after repeated attempts — fall back to the truncated
            # first message with an ellipsis.
            slot.title = _fallback_title_from_messages(slot.messages)
            slot._titled = True
            _persist_title(state, slot)
            state.push_slot_title(slot.key, slot.title)
        return
    slot._title_in_flight = True
    logger.info("Auto-title: attempting for slot %s (turn %d)", slot.key, user_count)

    try:
        title = await _generate_title_via_kiro(state, slot.messages)
        logger.info("Auto-title: kiro returned %r for slot %s", title, slot.key)
        if title:
            # Animate the title in word-by-word, then finalize with the
            # complete title (full push + persist).
            await _reveal_title(state, slot, title)
            slot.title = title
            slot._titled = True
            _persist_title(state, slot)
            state.push_slot_title(slot.key, title)
        else:
            # LLM returned SKIP/empty. Show the truncated fallback name right
            # away rather than leaving "New Session…" until the full turn ends
            # — otherwise the name lags the whole response for messages the LLM
            # won't title from the user text alone. Lock it (_titled=True) only
            # once the assistant has responded and the LLM still SKIP'd (a
            # definitive failure); on the on-send attempt leave it unlocked so
            # the end-of-turn retry can still upgrade the truncation to a real
            # LLM title.
            has_assistant = any(
                m.get("role") == "assistant" and m.get("content") for m in slot.messages
            )
            slot.title = _fallback_title_from_messages(slot.messages)
            slot._titled = has_assistant
            _persist_title(state, slot)
            state.push_slot_title(slot.key, slot.title)
            logger.info(
                "Auto-title: fell back to truncated message for slot %s (locked=%s)",
                slot.key,
                has_assistant,
            )
    except Exception:
        logger.warning("Auto-title failed for slot %s", slot.key, exc_info=True)
    finally:
        slot._title_in_flight = False


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
