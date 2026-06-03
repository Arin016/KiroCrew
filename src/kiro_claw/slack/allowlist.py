"""Slack user allowlist and tracking-channel management.

Handles two owner-approval workflows:

1. **User allowlist** — when a user joins a tracked channel
   (``member_joined_channel``) or is nominated via ``/kiroclaw @user``,
   the owner gets a DM with Allow / Deny buttons.
2. **Tracking channel** — ``/kiroclaw #channel`` sends an Add / Ignore
   prompt to the owner.  Approved channels are persisted to config.json.

Both flows share the same config persistence helpers so changes survive
gateway restarts.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from kiro_claw.config import KiroClawConfig
from kiro_claw.config.loader import config_path
from kiro_claw.dashboard.origin import (
    dashboard_origin,
    devspaces_proxy_url,
    is_local_only,
    parse_dashboard_url,
    resolve_dashboard_host,
)
from kiro_claw.dashboard.token_auth import LINK_WINDOW_SECS, MAX_SESSION_TTL_SECS, generate_token
from kiro_claw.sel import sel
from kiro_claw.slack.handler import is_allowed_user, is_tracked_channel
from kiro_claw.tunnel import get_tunnel_url

if TYPE_CHECKING:
    from kiro_claw.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

# Block Kit action IDs shared with the interaction router
ACTION_ALLOWLIST_APPROVE = "allowlist_approve"
ACTION_ALLOWLIST_DENY = "allowlist_deny"
ACTION_TRACK_APPROVE = "track_channel_approve"
ACTION_TRACK_DENY = "track_channel_deny"


# ---------------------------------------------------------------------------
# Owner prompts — builds the Allow/Deny DMs
# ---------------------------------------------------------------------------


async def _send_prompt(
    slack: SlackClientOps,
    owner_id: str,
    text: str,
    approve_label: str,
    deny_label: str,
    approve_action: str,
    deny_action: str,
    value: str,
    fallback: str,
) -> None:
    """Build a two-button Slack prompt and DM it to the owner."""
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": text}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": approve_label},
                    "style": "primary",
                    "action_id": approve_action,
                    "value": value,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": deny_label},
                    "style": "danger",
                    "action_id": deny_action,
                    "value": value,
                },
            ],
        },
    ]
    try:
        dm = await slack.open_dm(owner_id)
        await slack.post_blocks(dm, blocks, fallback)
    except Exception:
        logger.exception("Failed to send prompt: %s", fallback)


async def prompt_allowlist(
    slack: SlackClientOps,
    owner_id: str,
    user_id: str,
    channel_id: str = "",
) -> None:
    """Send an Allow / Deny prompt to the owner for *user_id*.

    When the user is already allowed (manual ``/kiroclaw @user`` recall),
    the prompt offers Keep / Remove instead of Allow / Deny.  Automatic
    channel-join prompts are silently skipped for already-allowed users.
    """
    if not user_id:
        return

    already = is_allowed_user(user_id)

    # Automatic channel-join → skip if already allowed
    if channel_id and already:
        return

    logger.info("allowlist prompt: user=%s channel=%s already=%s", user_id, channel_id, already)

    if already:
        text = f"<@{user_id}> is currently on the allowlist.\nKeep or remove?"
        approve_label = "✅ Keep"
        deny_label = "🚫 Remove"
    elif channel_id:
        text = f"👋 <@{user_id}> just joined <#{channel_id}>.\nAdd to allowlist?"
        approve_label = "✅ Allow"
        deny_label = "🚫 Deny"
    else:
        text = f"👋 <@{user_id}> — allowlist requested.\nAdd to allowlist?"
        approve_label = "✅ Allow"
        deny_label = "🚫 Deny"

    await _send_prompt(
        slack,
        owner_id,
        text,
        approve_label,
        deny_label,
        ACTION_ALLOWLIST_APPROVE,
        ACTION_ALLOWLIST_DENY,
        f"{user_id}:",
        "Allowlist prompt",
    )


async def prompt_track_channel(
    slack: SlackClientOps,
    owner_id: str,
    channel_id: str,
    channel_name: str = "",
) -> None:
    """Send a Track / Ignore prompt to the owner for *channel_id*.

    When the channel is already tracked the prompt offers to keep or
    remove it instead of add/ignore.
    """
    if not channel_id:
        return

    already = is_tracked_channel(channel_id)
    logger.info(
        "track channel prompt: channel=%s (%s) already=%s",
        channel_id,
        channel_name,
        already,
    )

    if already:
        text = f"📡 <#{channel_id}> is currently tracked.\nKeep tracking or remove?"
        approve_label = "✅ Keep"
        deny_label = "🚫 Remove"
    else:
        text = f"📡 Track <#{channel_id}> for new member allowlist prompts?"
        approve_label = "✅ Track"
        deny_label = "🚫 Ignore"

    await _send_prompt(
        slack,
        owner_id,
        text,
        approve_label,
        deny_label,
        ACTION_TRACK_APPROVE,
        ACTION_TRACK_DENY,
        f"{channel_id}:{channel_name}",
        "Track channel — prompt",
    )


# ---------------------------------------------------------------------------
# Tunnel URL resolution
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dashboard presigned link — always sent via DM, never in a channel
# ---------------------------------------------------------------------------


async def send_dashboard_link(
    slack: SlackClientOps,
    user_id: str,
    ttl: int = 3600,
) -> str:
    """Generate a presigned dashboard URL and DM it to *user_id*.

    Returns the generated URL (for logging), or an empty string on failure.
    The link is always sent as a DM to prevent token leakage in channels.

    The URL must be clicked within 5 minutes. Once opened, the session
    cookie lasts for *ttl* seconds (capped at 6 hours).
    """
    session_ttl = min(ttl, MAX_SESSION_TTL_SECS)
    cfg = KiroClawConfig.load()
    configured_host, port = parse_dashboard_url(cfg.dashboard.url)
    local_only = is_local_only(configured_host, True)
    host = resolve_dashboard_host(local_only, configured_host)

    token = generate_token(user_id, session_ttl)

    # Tunnel URL is only used when explicitly opted in via slack.use_tunnel_url
    # (default false until tunnel mechanism is scaled for general use).
    tunnel_url = get_tunnel_url() if cfg.slack.use_tunnel_url else ""
    if tunnel_url:
        url = f"{tunnel_url}/?token={token}"
    else:
        origin = dashboard_origin(cfg.dashboard.url)
        url = f"{origin}/?token={token}" if origin else f"http://{host}:{port}/?token={token}"

    # DevSpaces/AgentSpaces: also provide proxy URL
    proxy_line = ""
    proxy = devspaces_proxy_url(port)
    if proxy:
        proxy_line = f"\n🔗 <{proxy}/?token={token}|Open via DevSpaces Proxy>"

    link_mins = LINK_WINDOW_SECS // 60
    session_mins = session_ttl // 60
    try:
        dm = await slack.open_dm(user_id)
        await slack.post_message(
            dm,
            f"🔗 <{url}|Open Dashboard>{proxy_line}\n"
            f"⏱ Click within {link_mins}m · session lasts {session_mins}m",
        )
        sel().log_api_access(
            caller=user_id,
            operation="slack.dashboard_token",
            outcome="ok",
            resources=f"ttl={session_ttl}",
        )
    except Exception:
        try:
            sel().log_api_access(
                caller=user_id,
                operation="slack.dashboard_token",
                outcome="error",
                resources=f"ttl={session_ttl}",
            )
        except Exception:
            pass
        logger.exception("Failed to DM dashboard link to %s", user_id)
        return ""

    return url


async def send_channel_challenge(
    slack: SlackClientOps,
    channel_id: str,
    user_id: str,
    prompt_text: str,
    ttl: int = 3600,
    *,
    thread_ts: str = "",
    session_key: str = "",
) -> str:
    """Send an ephemeral challenge-and-redirect to the user in a channel.

    Instead of processing the prompt inline, generates a presigned dashboard
    URL with the prompt HMAC-signed inside the token payload, then sends it
    as an ephemeral message visible only to the target user.

    The prompt is NOT included as a separate query parameter — the frontend
    extracts it from the validated token payload to prevent tampering.

    The link must be clicked within ``LINK_WINDOW_SECS`` (the ``exp`` claim),
    but the resulting session lasts *ttl* seconds (the ``session_exp`` claim,
    capped at ``MAX_SESSION_TTL_SECS``). These are independent: passing the
    link window as the session TTL would expire the session the instant the
    click window closes.

    *thread_ts* and *session_key* carry Slack thread context into the signed
    token so the dashboard reconnects to the correct session instead of always
    spawning a fresh one. When *session_key* is set (an existing session is
    already linked to this thread), the dashboard reopens that session. When
    only *thread_ts*/*channel_id* are set, the dashboard auto-links the newly
    created session back to the originating Slack thread so agent responses
    flow into it.

    Returns the generated URL, or empty string on failure.
    """
    cfg = KiroClawConfig.load()
    configured_host, port = parse_dashboard_url(cfg.dashboard.url)
    local_only = is_local_only(configured_host, True)
    host = resolve_dashboard_host(local_only, configured_host)

    # Generate token with prompt included in HMAC signature.
    # session_ttl governs session_exp; the 5-min link window (exp) is applied
    # separately inside generate_token via LINK_WINDOW_SECS.
    session_ttl = min(ttl, MAX_SESSION_TTL_SECS)
    extra: dict[str, str] = {}
    if channel_id:
        extra["channel"] = channel_id
    if thread_ts:
        extra["thread_ts"] = thread_ts
    if session_key:
        extra["session_key"] = session_key
    token = generate_token(user_id, session_ttl, prompt=prompt_text, extra=extra)

    # Build URL — tunnel only when slack.use_tunnel_url is explicitly enabled
    # (KiroClaw keeps the tunnel mechanism opt-in). Prompt is only inside the
    # signed token, not as a separate query param (prevents tampering).
    tunnel_url = get_tunnel_url() if cfg.slack.use_tunnel_url else ""
    if tunnel_url:
        url = f"{tunnel_url}/?token={token}"
    else:
        origin = dashboard_origin(cfg.dashboard.url)
        base = origin if origin else f"http://{host}:{port}"
        url = f"{base}/?token={token}"

    link_mins = LINK_WINDOW_SECS // 60
    text = f"🔐 <{url}|Open a session to continue>\n" f"_(Link expires in {link_mins} minutes)_"

    try:
        # DMs: regular message (already private; ephemeral has rendering issues).
        # Channels: ephemeral without thread_ts (visible only to target user;
        # thread_ts omitted because Slack only shows ephemeral thread replies
        # if an active thread already exists).
        if channel_id.startswith("D"):
            await slack.post_message(channel_id, text)
        else:
            await slack.post_ephemeral(channel_id, user_id, text)
        sel().log_api_access(
            caller=user_id,
            operation="slack.channel_challenge",
            outcome="ok",
            resources=f"channel={channel_id}",
        )
    except Exception:
        logger.exception("Failed to post channel challenge in %s", channel_id)
        sel().log_api_access(
            caller=user_id,
            operation="slack.channel_challenge",
            outcome="error",
            resources=f"channel={channel_id}",
        )
        return ""

    return url


# ---------------------------------------------------------------------------
# Config persistence
# ---------------------------------------------------------------------------


def _read_config() -> dict:
    """Read config.json, returning {} on any error.

    Always re-reads from disk so manual edits are respected.
    """
    cp = config_path()
    try:
        return json.loads(cp.read_text(encoding="utf-8")) if cp.exists() else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_config(data: dict) -> None:
    """Write *data* back to config.json atomically (write tmp + rename)."""
    cp = config_path()
    from kiro_claw.atomic_write import atomic_write

    atomic_write(cp, json.dumps(data, indent=4) + "\n")


def _update_config_list(
    section_key: str,
    id_field: str,
    target_id: str,
    *,
    remove: bool = False,
    name: str = "",
) -> None:
    """Add or remove an entry in a ``config.json → slack.<section_key>`` list.

    Each entry is a dict with at least *id_field*.  Idempotent — adding
    a duplicate or removing a missing entry is a no-op.  Always re-reads
    the file first so manual edits aren't clobbered.
    """
    data = _read_config()
    slack_cfg = data.setdefault("slack", {})
    entries: list[dict] = slack_cfg.setdefault(section_key, [])

    if remove:
        filtered = [e for e in entries if e.get(id_field) != target_id]
        if len(filtered) == len(entries):
            return  # wasn't there — no-op
        slack_cfg[section_key] = filtered
        _write_config(data)
        logger.info("Removed %s=%s from config slack.%s", id_field, target_id, section_key)
    else:
        if any(e.get(id_field) == target_id for e in entries):
            return  # already present — no-op
        entry: dict[str, str] = {id_field: target_id}
        if name:
            entry["name"] = name
        entries.append(entry)
        _write_config(data)
        logger.info("Added %s=%s (%s) to config slack.%s", id_field, target_id, name, section_key)


def persist_allowed_user(user_id: str, name: str = "", *, remove: bool = False) -> None:
    """Add or remove *user_id* in ``config.json → slack.allowed_users``."""
    _update_config_list("allowed_users", "slack_id", user_id, remove=remove, name=name)


def persist_tracking_channel(channel_id: str, name: str = "", *, remove: bool = False) -> None:
    """Add or remove *channel_id* in ``config.json → slack.tracking_channels``."""
    _update_config_list("tracking_channels", "channel_id", channel_id, remove=remove, name=name)
