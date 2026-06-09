"""Slack integration — link sessions, handoff, channel listing."""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_claw.config.loader import KiroClawConfig
from kiro_claw.dashboard import state as dashboard_state
from kiro_claw.dashboard.chat_persistence import _save_slot_to_history
from kiro_claw.dashboard.chat_utils import _history_key_for
from kiro_claw.dashboard.state import DashboardState
from kiro_claw.security import redact_and_truncate
from kiro_claw.sel import sel
from kiro_claw.slack.channel_resolver import _CACHE_FILENAME, ChannelNameResolver
from kiro_claw.sync_bridge import handoff_to_slack

logger = logging.getLogger(__name__)


def _get_channel_resolver(state: DashboardState) -> ChannelNameResolver:
    """Lazily construct the shared ChannelNameResolver on first use.

    The cache path is derived from ``dashboard_state.config_dir`` (accessed as a
    module attribute, not a ``from`` import) so it flows through the same seam
    tests patch — isolating the on-disk cache to ``tmp_path`` under test while
    resolving to the real ``~/.kiroclaw`` dir in production.
    """
    if state._channel_resolver is None:
        cache_path = dashboard_state.config_dir() / _CACHE_FILENAME
        state._channel_resolver = ChannelNameResolver(cache_path=cache_path)
    return state._channel_resolver


async def api_chat_slot_slack_link(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{name}/slack-link — link a dashboard session to Slack."""

    state: DashboardState = request.app["state"]
    name = request.match_info.get("name") or request.match_info.get("slot", "")
    slot = state.get_slot(name) or state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    if not state.slack_client:
        return web.json_response({"error": "Slack not connected"}, status=503)
    owner_id = getattr(state, "owner_id", None)
    if not owner_id:
        return web.json_response({"error": "owner not configured"}, status=500)

    session_key = _history_key_for(name)

    # Check if already linked
    existing_ts, existing_chan = state.sessions.get_slack_link(session_key)
    if existing_ts and existing_chan:
        try:
            await state.slack_client.post_message(
                existing_chan, "🔗 Session linked from dashboard — continuing here.", existing_ts
            )
        except Exception:
            pass
        return web.json_response(
            {"ok": True, "already_linked": True, "thread_ts": existing_ts, "channel": existing_chan}
        )

    body = await request.json() if request.content_length else {}
    raw_channel = body.get("channel", "")
    # When the caller supplies an existing thread_ts (challenge-and-redirect
    # auto-link from a Slack thread the user replied in), link to THAT thread
    # rather than posting a new one — this is what makes a thread reply route
    # back to its dashboard session bidirectionally.
    existing_thread = str(body.get("thread_ts", "") or "")
    if not raw_channel or raw_channel == "dm":
        target_channel = await state.slack_client.open_dm(owner_id)
    else:
        target_channel = raw_channel

    if existing_thread:
        thread_ts = existing_thread
    else:
        # redact_and_truncate applies both redact_exfiltration_urls + redact_credentials
        title = redact_and_truncate(slot.title or name, max_chars=200)
        thread_ts = await state.slack_client.post_message(
            target_channel, f"\U0001f9f5 *{title}*\nSession linked from dashboard."
        )
        if not thread_ts:
            return web.json_response({"error": "failed to create thread"}, status=500)

    state.sessions.set_slack_link(session_key, thread_ts, target_channel)
    slot._slack_linked = True
    slot._slack_channel = target_channel
    slot._slack_thread_ts = thread_ts

    # Post last 5 messages as context — only when we created a NEW thread.
    # Linking to an existing thread (challenge-and-redirect) would duplicate
    # messages the thread already contains.
    if not existing_thread:
        for m in slot.messages[-5:]:
            role = m.get("role", "")
            txt = redact_and_truncate(m.get("content") or "", max_chars=2000)
            if role in ("user", "assistant") and txt:
                icon = "\U0001f9d1" if role == "user" else "\U0001f916"
                try:
                    await state.slack_client.post_message(
                        target_channel, f"{icon} {txt}", thread_ts
                    )
                except Exception:
                    pass

    sel().log_api_access(
        caller="dashboard",
        operation="chat.slack_link",
        outcome="success",
        source="dashboard",
        resources=slot.key,
    )
    state.push_slots_update()
    return web.json_response({"ok": True, "thread_ts": thread_ts, "channel": target_channel})


async def api_slack_channels(request: web.Request) -> web.Response:
    """GET /api/slack/channels — list channels the bot can reply in.

    Returns ``[{id, name}]``. Channel names that aren't already stored in
    config are resolved via ``ChannelNameResolver`` (single ``conversations.list``
    call cached for 1h) so the dropdown shows ``# pcn-orchestrator-interest``
    instead of ``# C0AU38Q0E4B``.
    """
    state: DashboardState = request.app["state"]
    cfg = KiroClawConfig.load()
    channels: list[dict] = [{"id": "dm", "name": "Direct Message"}]
    seen: set[str] = set()
    unresolved: list[str] = []  # channel IDs that need name lookup

    for tc in cfg.slack.tracking_channels:
        cid = tc.get("channel_id", "")
        if cid and cid not in seen:
            name = tc.get("name") or ""
            channels.append({"id": cid, "name": name or cid})
            seen.add(cid)
            if not name:
                unresolved.append(cid)
    for cid, cc in cfg.slack_channels.items():
        if cid not in seen and cc.activation in ("always", "mention", "observe"):
            channels.append({"id": cid, "name": cid})  # placeholder — resolved below
            seen.add(cid)
            unresolved.append(cid)

    # Resolve placeholder names via cached Slack API call
    if unresolved and state.slack_client is not None:
        try:
            resolver = _get_channel_resolver(state)
            resolved = await resolver.resolve_many(state.slack_client, unresolved)
            for ch in channels:
                if ch["id"] in unresolved:
                    ch["name"] = resolved.get(ch["id"], ch["id"])
        except Exception:
            # Resolution failure leaves placeholder names in place — non-fatal
            logger.debug("Channel name resolution failed", exc_info=True)

    return web.json_response(channels)


async def api_chat_slot_handoff(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/handoff — hand off session to Slack DM thread."""

    state: DashboardState = request.app["state"]
    name = request.match_info.get("slot") or request.match_info.get("name", "")
    slot = state.get_slot(name) or state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    if not state.slack_client:
        return web.json_response({"error": "Slack not connected"}, status=503)
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=500)

    try:
        _save_slot_to_history(state, slot)
    except Exception:
        pass

    channel = None
    try:
        body = await request.json()
        channel = body.get("channel")
    except Exception:
        pass

    history_key = _history_key_for(slot.key)
    thread_ts = await handoff_to_slack(
        state.slack_client,
        state.owner_id,
        state.conversation_log,
        history_key,
        title=slot.title if slot._titled else "",
        channel=channel,
        sessions=state.sessions,
    )
    if not thread_ts:
        return web.json_response({"error": "handoff failed"}, status=500)

    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_handoff",
        outcome="allowed",
        source="dashboard",
        resources=slot.key,
    )
    return web.json_response({"ok": True, "thread_ts": thread_ts})


async def api_handoff_channels(request: web.Request) -> web.Response:
    """GET /api/handoff-channels — deprecated, use /api/slack/channels instead."""
    return web.json_response({})
