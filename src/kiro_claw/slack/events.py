"""Slack Socket Mode event routing.

Sets up the Socket Mode client, dispatches incoming events to the
correct handler:

- ``interactive`` → :mod:`interactions.dispatch`
- ``slash_commands`` → registry-based sub-command routing
- ``member_joined_channel`` → tracking-channel allowlist prompt
- ``app_home_opened`` → publish Home Tab view
- ``message`` / ``app_mention`` → :func:`handler.handle_message`

Also contains the bounded dedup cache (``_SeenCache``) that prevents
processing the same Slack event twice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

import aiohttp
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.socket_mode.websockets import SocketModeClient as WSSocketModeClient
from slack_sdk.web.async_client import AsyncWebClient

from kiro_claw import __version__
from kiro_claw.config.loader import (
    ACTIVATION_MENTION,
    ACTIVATION_OBSERVE,
    ACTIVATION_OFF,
    ACTIVATION_REVIEW,
    KiroClawConfig,
)
from kiro_claw.cron import format_schedule
from kiro_claw.dashboard.handlers import get_update_info
from kiro_claw.dashboard.handlers import sessions as _sessions
from kiro_claw.dashboard.handlers.usage import get_usage_cache
from kiro_claw.dashboard.token_auth import LINK_WINDOW_SECS, MAX_SESSION_TTL_SECS, parse_duration
from kiro_claw.hooks import safe_read_file
from kiro_claw.mcp_discovery import list_servers
from kiro_claw.midway import get_midway_status_line
from kiro_claw.safety_override import safety_override
from kiro_claw.security import (
    redact_credentials,
    redact_exfiltration_urls,
    should_record_observe_history,
)
from kiro_claw.sel import sel
from kiro_claw.skills import SkillsLoader
from kiro_claw.slack.allowlist import prompt_track_channel, send_dashboard_link
from kiro_claw.slack.blocks import (
    build_stopping_blocks,
    channels_modal,
    command_hint_block,
    dashboard_link_block,
    voice_config_modal,
)
from kiro_claw.slack.enterprise import check_message_origin, validate_enterprise
from kiro_claw.slack.files import process_slack_files
from kiro_claw.slack.handler import (
    _YOLO_TTL_SECS,
    APPROVAL_INTERACTIVE,
    handle_message,
    is_allowed_user,
    is_owner,
    is_yolo_mode,
    set_allowed_users,
    set_dashboard_state,
    set_open_channels,
    set_orch_cfg,
    set_owner_id,
    set_tracking_channels,
    set_yolo_mode,
)
from kiro_claw.slack.interactions import dispatch as dispatch_interactive
from kiro_claw.slack.sessions_view import (
    _HOME_TAB_SESSIONS_PER_KIND,
    _SESSION_KIND_DASHBOARD,
    _SESSION_KIND_TASKRUNNER,
    _SESSIONS_DEFAULT_LIMIT,
    _build_sessions_blocks,
    _collect_recent_sessions,
)
from kiro_claw.stats import Stats
from kiro_claw.transcribe import is_available as stt_available
from kiro_claw.transcribe import transcribe_audio

if TYPE_CHECKING:
    from kiro_claw.slack.client import SlackClientOps
    from kiro_claw.slack.gateway import GatewayOrchestrator

logger = logging.getLogger(__name__)

_skills_loader: SkillsLoader | None = None

# Strong references to fire-and-forget asyncio.Tasks scheduled from
# synchronous render paths (e.g. the Home Tab usage-line cold-cache prime).
# Python's event loop only keeps *weak* references to tasks, so without a
# strong reference the task can be garbage-collected mid-execution. Tasks
# remove themselves on completion via ``add_done_callback``.
_bg_tasks: set[asyncio.Task[object]] = set()


def _get_skills_loader() -> SkillsLoader:
    global _skills_loader  # noqa: PLW0603
    if _skills_loader is None:
        _skills_loader = SkillsLoader()
    return _skills_loader


# Suppress noisy Slack SDK WebSocket reconnect errors — these are normal
# idle connection drops that the SDK handles automatically.
# WARNING lets ERROR through (recv failures, reconnect failures) while
# suppressing INFO (session established) and DEBUG (every message/ping).
logging.getLogger("slack_sdk.socket_mode.websockets").setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Dedup cache — bounded LRU to avoid processing duplicate Slack events
# ---------------------------------------------------------------------------

_MAX_SEEN = 5000

# Challenge-and-redirect (force Slack messages through a dashboard session link
# before the agent processes them). DEFAULT OFF: the redirect flow is not yet
# ready for general use, so Slack messages process inline as before. Operators
# can opt in during the rollout with KIROCLAW_ENABLE_CHALLENGE=1. (Mirrors the
# slack.use_tunnel_url default-off pattern — re-enable per-instance when ready.)
_CHALLENGE_REDIRECT_ENABLED = os.environ.get("KIROCLAW_ENABLE_CHALLENGE") == "1"

if _CHALLENGE_REDIRECT_ENABLED:
    logger.info(
        "KIROCLAW_ENABLE_CHALLENGE=1 — challenge-and-redirect is ON; Slack "
        "messages require a dashboard session before reaching the agent."
    )

# prevent GC of fire-and-forget tasks (Python event loop holds weak refs)
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


class SeenCache:
    """Bounded set that remembers the last *maxlen* event IDs."""

    def __init__(self, maxlen: int = _MAX_SEEN):
        self._d: OrderedDict[str, None] = OrderedDict()
        self._maxlen = maxlen

    def check_and_add(self, key: str) -> bool:
        """Return ``True`` if *key* was already seen, else mark it."""
        if key in self._d:
            return True
        self._d[key] = None
        if len(self._d) > self._maxlen:
            self._d.popitem(last=False)
        return False


# ---------------------------------------------------------------------------
# Slash command registry
# ---------------------------------------------------------------------------

# Handler signature: async def handler(orch, caller_id, args, respond) -> None
SlashHandler = Callable[["GatewayOrchestrator", str, str, Callable], Coroutine[Any, Any, None]]

SLASH_REGISTRY: dict[str, tuple[SlashHandler, str]] = {}


def register_slash_command(name: str, handler: SlashHandler, description: str = "") -> None:
    """Register a sub-command for ``/kiroclaw <name>``."""
    SLASH_REGISTRY[name] = (handler, description)


def _build_help_text(cmd_name: str = "kiroclaw") -> str:
    """Build help message listing all registered sub-commands."""
    lines = ["*Available commands:*"]
    for name, (_, desc) in sorted(SLASH_REGISTRY.items()):
        lines.append(f"• `/{cmd_name} {name}` — {desc}" if desc else f"• `/{cmd_name} {name}`")
    lines.append(f"• `/{cmd_name} #channel` — track/untrack channel")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Built-in slash sub-command handlers
# ---------------------------------------------------------------------------


async def _handle_dashboard(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Generate presigned dashboard link and DM to caller."""
    ttl = 3600
    if args:
        parsed = parse_duration(args.split()[0])
        if parsed is None:
            await respond(f"Usage: `/{orch.slack_command} dashboard [<N>h|<N>m]`")
            return
        ttl = parsed

    session_ttl = min(ttl, MAX_SESSION_TTL_SECS)
    assert orch.slack is not None
    url = await send_dashboard_link(orch.slack, caller_id, session_ttl)
    if url:
        blks = dashboard_link_block(url, LINK_WINDOW_SECS // 60, session_ttl // 60)
        await respond("🔗 Dashboard link sent to your DMs.", blocks=blks)
    else:
        await respond("❌ Failed to send dashboard link.")


async def _handle_agent(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Switch agent directly if valid name given, otherwise show selector."""
    from kiro_claw.slack.handler import (  # circular import: handler.py imports events.py for command dispatch, creating runtime circular dependency
        _get_default_agent,
        _resolve_agent_name,
        _set_default_agent,
        is_owner,
    )

    if not is_owner(caller_id):
        await respond("⛔ Only the owner can switch agents.")
        return

    # Direct switch if arg provided
    if args:
        name = args.strip().split()[0]
        if name.lower() in ("off", "default"):
            _set_default_agent("")
            await respond("🔄 Reset to default agent.")
            return
        resolved = _resolve_agent_name(name)
        if resolved:
            _set_default_agent(resolved)
            await respond(f"🔄 Switched to agent: *{resolved}*")
            return
        await respond(f"❌ Unknown agent `{name}`. Pick one below:")

    # Show selector dropdown
    agents_dir = Path.home() / ".kiro" / "agents"
    jsons = sorted(agents_dir.glob("*.json")) if agents_dir.is_dir() else []
    agent_names = sorted(f.stem for f in jsons)
    current = _get_default_agent() or ""

    options = [{"text": {"type": "plain_text", "text": n[:75]}, "value": n} for n in agent_names]
    options.append({"text": {"type": "plain_text", "text": "off (default)"}, "value": "off"})
    initial = next((o for o in options if o["value"] == current), options[-1])

    blks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Current agent:* {current or 'default'}"},
            "accessory": {
                "type": "static_select",
                "action_id": "mc_agent_select",
                "options": options,
                "initial_option": initial,
            },
        },
    ]
    await respond("Select an agent:", blocks=blks)


async def _handle_voice(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Open voice config modal with current TTS settings."""
    from kiro_claw.slack.handler import (
        _vc,  # circular import: handler.py imports events.py for command dispatch
    )

    trigger_id = getattr(orch, "_last_trigger_id", "")
    if not trigger_id:
        await respond("❌ Missing trigger_id — cannot open modal.")
        return

    modal = voice_config_modal(
        tts_enabled=_vc.global_enabled,
        auto_speak=getattr(_vc, "auto_speak", False),
        voice=_vc.default_voice,
        engine=_vc.default_engine,
        speed=_vc.default_rate,
        pitch=_vc.default_pitch,
        aws_profile=_vc.aws_profile,
        region=_vc.region,
    )

    try:
        assert orch.slack is not None
        await orch.slack.views_open(trigger_id=trigger_id, view=modal)
    except Exception:
        logger.exception("Failed to open voice config modal")
        await respond("❌ Failed to open voice settings modal.")


register_slash_command("dashboard", _handle_dashboard, "get a dashboard access link")
register_slash_command("agent", _handle_agent, "switch the active agent")
register_slash_command("voice", _handle_voice, "configure TTS voice settings")


async def _handle_yolo(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Toggle YOLO mode on/off/renew."""
    if not is_owner(caller_id):
        await respond("⛔ Only the owner can toggle YOLO mode.")
        return

    arg = args.strip().lower()
    so = safety_override()

    if arg == "on":
        if so.is_active():
            remaining = so.remaining_secs()
            await respond(f"🟢 YOLO mode is already *ON* ({remaining // 60}min remaining).")
            return
        result = so.activate("slack")
        if not result.active:
            await respond("❌ Failed to activate YOLO mode (audit system unavailable).")
            return
        sel().log_api_access(
            caller=caller_id,
            operation="slack.yolo_mode",
            outcome="allowed",
            source="slack",
            resources="yolo_on",
        )
        if orch.dashboard_state:
            orch.dashboard_state.push_slots_update()
        await respond(
            f"🟢 YOLO mode *ON* (auto-expires in {_YOLO_TTL_SECS // 60}min) — all tools auto-approved."
        )
    elif arg == "off":
        from kiro_claw.slack.handler import (
            disable_yolo,  # circular import: handler.py imports events.py for command dispatch registration
        )

        disable_yolo()
        sel().log_api_access(
            caller=caller_id,
            operation="slack.yolo_mode",
            outcome="allowed",
            source="slack",
            resources="yolo_off",
        )
        if orch.dashboard_state:
            orch.dashboard_state.push_slots_update()
        await respond("🔴 YOLO mode *OFF* — tools require approval.")
    elif arg == "renew":
        renew_result = so.renew("slack")
        if renew_result.renewed:
            sel().log_api_access(
                caller=caller_id,
                operation="slack.yolo_mode",
                outcome="renewed",
                source="slack",
                resources="yolo_renew",
            )
            if orch.dashboard_state:
                orch.dashboard_state.push_slots_update()
            await respond(f"🟢 YOLO mode *renewed* (auto-expires in {renew_result.ttl // 60}min).")
        else:
            await respond("🔴 YOLO mode is not active. Use `on` to activate first.")
    else:
        if so.is_active():
            remaining = so.remaining_secs()
            await respond(
                f"YOLO mode is currently *ON 🟢* ({remaining // 60}min remaining).\nUsage: `/{orch.slack_command} yolo on|off|renew`"
            )
        else:
            await respond(
                f"YOLO mode is currently *OFF 🔴*.\nUsage: `/{orch.slack_command} yolo on|off|renew`"
            )


async def _handle_config(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Open config modal (owner-only) — users and channels."""
    if not is_owner(caller_id):
        await respond("⛔ Only the owner can change config.")
        return

    tracking_ids = list(orch._tracking_channels)

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "⚠️ Multi-user access is disabled for security. Only the owner can interact via Slack.",
            },
        },
        {
            "type": "input",
            "block_id": "channels_block",
            "label": {"type": "plain_text", "text": "Tracked Channels"},
            "element": {
                "type": "multi_channels_select",
                "action_id": "mc_config_channels",
                "placeholder": {"type": "plain_text", "text": "Select channels"},
                **({"initial_channels": tracking_ids} if tracking_ids else {}),
            },
            "optional": True,
        },
    ]

    view = {
        "type": "modal",
        "callback_id": "mc_config_panel",
        "title": {"type": "plain_text", "text": "KiroClaw Config"},
        "submit": {"type": "plain_text", "text": "Save"},
        "close": {"type": "plain_text", "text": "Cancel"},
        "blocks": blocks,
    }

    trigger_id = getattr(orch, "_last_trigger_id", "")
    if not trigger_id:
        await respond("⚠️ Cannot open modal — missing trigger_id.")
        return

    try:
        assert orch.slack is not None
        await orch.slack.views_open(trigger_id=trigger_id, view=view)
    except Exception:
        logger.exception("Failed to open config modal")
        await respond("❌ Failed to open config modal.")


register_slash_command("yolo", _handle_yolo, "toggle YOLO mode (auto-approve tools)")
register_slash_command("config", _handle_config, "manage users and channels (owner-only)")


async def _handle_allowlist_cmd(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Multi-user access disabled — user management is blocked."""
    await respond(
        "⛔ Multi-user access is disabled for security. Only the owner can use KiroClaw via Slack."
    )


def _get_agent_names() -> list[str]:
    """Return sorted list of installed agent names from ~/.kiro/agents/.

    Reads each agent JSON's ``name`` field via ``hooks.safe_read_file`` so
    symlinks into sensitive paths (e.g. ``~/.aws/credentials``) are blocked
    by ``is_sensitive_path()``. Falls back to the filename stem when the
    file cannot be read safely or the JSON does not carry a usable name.

    When a read is blocked by ``is_sensitive_path()``, a SEL audit event
    (``sensitive_path_blocked``) is emitted so the attempt is observable.
    """
    agents_dir = Path.home() / ".kiro" / "agents"
    if not agents_dir.is_dir():
        return []
    names = []
    for f in agents_dir.glob("*.json"):
        try:
            data = json.loads(safe_read_file(str(f)))
            name = data.get("name") if isinstance(data, dict) else None
        except PermissionError as exc:
            # Symlink or resolved path landed in a sensitive location — audit it.
            try:
                sel().log_api_access(
                    caller="system",
                    operation="sensitive_path_blocked",
                    outcome="denied",
                    source="slack.events._get_agent_names",
                    resources=str(f),
                    error=str(exc),
                )
            except Exception:
                logger.debug(
                    "Failed to emit SEL audit event for blocked agent read: %s",
                    f,
                    exc_info=True,
                )
            name = None
        except (json.JSONDecodeError, OSError):
            name = None
        names.append(name or f.stem)
    return sorted(names)


async def _handle_channel_cmd(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Open modal showing tracked channels with per-channel activation mode."""
    if not is_owner(caller_id):
        await respond("⛔ Only the owner can manage tracked channels.")
        return

    current_ids = sorted(orch._tracking_channels)
    channels = [
        {
            "channel_id": cid,
            "activation": orch._cfg.channel_config(cid).activation,
            "agent": orch._cfg.channel_config(cid).agent,
        }
        for cid in current_ids
    ]
    agent_names = _get_agent_names()
    modal = channels_modal(channels, agent_names=agent_names)

    trigger_id = getattr(orch, "_last_trigger_id", "")
    if not trigger_id:
        await respond("⚠️ Cannot open modal — missing trigger_id.")
        return
    try:
        assert orch.slack is not None
        await orch.slack.views_open(trigger_id=trigger_id, view=modal)
    except Exception:
        logger.exception("Failed to open channels modal")
        await respond("❌ Failed to open channels modal.")


register_slash_command("users", _handle_allowlist_cmd, "manage allowed users")
register_slash_command("channels", _handle_channel_cmd, "manage tracked channels")


async def _handle_sessions(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """List last 10 sessions as task_card blocks with resume buttons."""
    # Deny-by-default authorization gate (defense-in-depth).
    #
    # Session JSONLs contain prior conversation contents — only owner /
    # explicitly-allowed users may read them. The dispatcher in
    # ``_handle_slash`` already filters slash commands by allowlist, so in
    # production this branch is unreachable today. The check is still
    # required by the AUTOSDE security-controls rule (deny-by-default) and
    # protects against future refactors that bypass the dispatcher gate.
    # The ``sessions`` keyword path applies the same check at handler.py
    # before delegating to ``_handle_sessions_command``.
    if not (is_owner(caller_id) or is_allowed_user(caller_id)):
        sel().log_api_access(
            caller=caller_id,
            operation="slack.sessions_slash_data_access",
            outcome="denied",
            source="slack",
            resources=args or "",
            error="unauthorized caller",
        )
        await respond("_Permission denied._")
        return

    # Wrap the collector so a transient OSError still produces a SEL audit
    # entry. Without this, an IO failure would skip the audit entirely and
    # the access attempt would be invisible to the security pipeline. The
    # Home Tab error-path (in ``_publish_home_tab``) follows the same
    # pattern: capture the exception, redact-then-truncate the message,
    # and emit an ``error=`` audit field.
    try:
        rows = _collect_recent_sessions(
            orch.sessions if orch is not None else None,
            limit=_SESSIONS_DEFAULT_LIMIT,
        )
    except Exception as exc:
        # Redact-then-truncate: redact() first so credential / exfil
        # patterns aren't split mid-string by the truncation step. The
        # SEL on-disk file is not internally redacted (sel.py only
        # redacts on forward), so this is defense-in-depth for the
        # AUTOSDE security-controls "never trust output" rule applied
        # to exception messages from third-party libraries.
        redacted_exc, _ = redact_exfiltration_urls(str(exc))
        redacted_exc, _ = redact_credentials(redacted_exc)
        sel().log_api_access(
            caller=caller_id,
            operation="slack.sessions_slash_data_access",
            outcome="error",
            source="slack",
            resources="0 sessions read (collector failed)",
            error=redacted_exc[:200],
        )
        logger.exception("slash sessions: collector failed for caller %s", caller_id)
        await respond("_Sessions unavailable._")
        return

    sel().log_api_access(
        caller=caller_id,
        operation="slack.sessions_slash_data_access",
        outcome="allowed",
        source="slack",
        resources=f"{len(rows)} sessions read",
    )

    if not rows:
        await respond("_No recent sessions._")
        return

    blocks = _build_sessions_blocks(rows)
    await respond("\U0001f4cb Recent sessions:", blocks=blocks)


register_slash_command("sessions", _handle_sessions, "list recent sessions")


async def _handle_status(
    orch: GatewayOrchestrator, caller_id: str, args: str, respond: Callable
) -> None:
    """Show runtime stats summary."""
    mw_line = await get_midway_status_line(prefix=" · midway")
    await respond(Stats().summary() + mw_line)


register_slash_command("status", _handle_status, "show runtime stats")


# ---------------------------------------------------------------------------
# Socket Mode setup
# ---------------------------------------------------------------------------


def init_socket_mode(orch: GatewayOrchestrator, seen: SeenCache) -> None:
    """Wire up the Socket Mode client and attach the event listener.

    Does nothing when Slack is disabled (missing tokens or no allowed
    users).  Mutates ``orch._socket_client`` in place.
    """
    if not orch._slack_enabled:
        return

    if not orch._owner_id:
        logger.error("KIROCLAW_OWNER_ID is not set — Slack disabled for security")
        orch._slack_enabled = False
        orch.slack = None
        return

    # Invariant: _allowed_users contains only the owner (multi-user disabled)
    assert orch._owner_id, "owner_id must be set"

    # Share owner-only allowlist and tracking channels with handler modules
    set_allowed_users(orch._allowed_users)
    set_tracking_channels(orch._tracking_channels)
    set_open_channels(orch._open_channels)
    set_owner_id(orch._owner_id)
    if orch._cfg.agent.yolo:
        set_yolo_mode(True)
    set_orch_cfg(orch._cfg)
    if orch.dashboard_state:
        set_dashboard_state(orch.dashboard_state)

    # ── Enterprise Grid workspace validation ──
    # Blocks data exfiltration via personal/external Slack workspaces.
    extra_ids = orch._cfg.slack_enterprise_ids
    if not validate_enterprise(orch._bot_token, extra_ids=extra_ids):
        logger.error("Slack workspace failed enterprise validation — Slack disabled")
        orch._slack_enabled = False
        orch.slack = None
        return

    web_client = AsyncWebClient(token=orch._bot_token)
    orch._socket_client = WSSocketModeClient(
        app_token=orch._app_token,
        web_client=web_client,
    )

    async def _on_event(client: WSSocketModeClient, req: SocketModeRequest) -> None:
        # Always ack immediately so Slack doesn't retry
        try:
            await client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            logger.debug("Failed to ack event (WebSocket not ready), skipping")
            return

        if req.type == "interactive":
            t = asyncio.create_task(dispatch_interactive(req.payload or {}))
            orch._handler_tasks.add(t)
            t.add_done_callback(orch._handler_tasks.discard)
            return

        if req.type == "slash_commands":
            payload = req.payload or {}
            # Handle /tk directly (TaskKeeper quick-note logging)
            if payload.get("command") == "/tk":
                t = asyncio.create_task(_handle_tk_note(orch, payload))
                orch._handler_tasks.add(t)
                t.add_done_callback(orch._handler_tasks.discard)
                return
            t = asyncio.create_task(_handle_slash(orch, payload))
            orch._handler_tasks.add(t)
            t.add_done_callback(orch._handler_tasks.discard)
            return

        if req.type != "events_api":
            return

        event = (req.payload or {}).get("event", {})
        event_type = event.get("type")

        # ── Tracking-channel join → allowlist prompt ──
        if event_type == "member_joined_channel":
            _maybe_prompt_owner(orch, event)
            return

        # ── Home Tab ──
        if event_type == "app_home_opened":
            user = event.get("user")
            if event.get("tab") == "home" and user:
                if is_allowed_user(user):
                    sel().log_api_access(
                        caller=user,
                        operation="slack.home_tab",
                        outcome="allowed",
                        source="slack",
                    )
                    task = asyncio.ensure_future(_publish_home_tab(orch, user))
                    _background_tasks.add(task)
                    task.add_done_callback(_background_tasks.discard)
                else:
                    sel().log_api_access(
                        caller=user,
                        operation="slack.home_tab",
                        outcome="denied",
                        source="slack",
                        error="unauthorized sender",
                    )
            return

        # ── Messages and mentions ──
        if event_type not in ("message", "app_mention"):
            return
        _bot_id = event.get("bot_id")
        _subtype = event.get("subtype")
        # ── message_deleted: cancel queued or in-flight messages ──
        if _subtype == "message_deleted":
            await _handle_message_deleted(orch, event)
            return
        if _subtype and _subtype != "file_share":
            return
        if _bot_id:  # pragma: no cover — socket mode callback, tested via integration
            sel().log_api_access(
                caller=_bot_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                error="untrusted_bot",
            )
            return

        # Enterprise Grid: envelope team_id is the *bot's* workspace;
        # event["team"] may be the *sender's* workspace in shared channels.
        # Always prefer envelope to prevent cross-workspace bypass.
        outer_team = (req.payload or {}).get("team_id", "")
        if outer_team:
            event["team"] = outer_team
        elif not event.get("team"):
            logger.warning(
                "Enterprise Grid: no team_id from event or envelope " "(sender=%s) — rejecting",
                event.get("user", "unknown"),
            )
            sel().log_api_access(
                caller=event.get("user", "unknown"),
                operation="slack.message",
                outcome="denied",
                source="slack",
                error="missing_team_id",
            )
            return

        await _route_message(
            orch,
            event,
            seen,
            is_mention=(event_type == "app_mention"),
            from_trusted_bot=False,
        )

    orch._socket_client.socket_mode_request_listeners.append(_on_event)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Home Tab
# ---------------------------------------------------------------------------


def _build_usage_status_line() -> str:
    """Render the kiro-cli plan-usage status line for the Home Tab.

    Reads the cached ``/usage`` snapshot maintained by
    :mod:`kiro_claw.dashboard.handlers.sessions`. When the cache is cold,
    fires a fire-and-forget background refresh so the next tab render has
    data, and returns a loading placeholder.

    Format matches neighbouring status lines — single mrkdwn line starting
    with ``*Plan usage:*``. Two render modes:

    - **Under plan:**  ``*Plan usage:* 4.3 / 10 credits · $0.85 · resets 2026-06-01``
    - **Over plan:**   ``*Plan usage:* ⚠️ 12249 / 10000 credits (over plan) · $89.95 · resets 2026-06-01``

    The final string is run through :func:`redact_credentials` and
    :func:`redact_exfiltration_urls` before being returned, since the
    ``resets`` / ``plan`` fields are raw strings parsed from a
    subprocess's stdout.
    """
    try:
        cache = get_usage_cache() or {}
        used = cache.get("credits_used")
        plan = cache.get("credits_plan")
        covered = cache.get("credits_covered")
        cost = cache.get("cost_usd")
        resets = cache.get("resets")

        if used is None or plan is None:
            # Cache cold — kick off bg fetch, render placeholder.
            try:
                if not getattr(_sessions, "_usage_fetching", False):
                    loop = asyncio.get_running_loop()
                    # Strong-ref the task so the event loop's weak-ref
                    # GC can't collect it mid-execution; discard on done.
                    task = loop.create_task(_sessions._fetch_usage_bg())
                    _bg_tasks.add(task)
                    task.add_done_callback(_bg_tasks.discard)
            except Exception:
                logger.debug("usage line: bg prime skipped", exc_info=True)
            return "*Plan usage:* _loading… reopen the tab in a moment_"

        try:
            used_f = float(used)
            plan_f = float(plan)
        except (TypeError, ValueError):
            return "*Plan usage:* _unavailable_"

        parts: list[str] = []
        if covered is not None:
            try:
                if float(covered) >= plan_f - 0.01:
                    total = plan_f + used_f
                    parts.append(f"⚠️ {total:.0f} / {plan_f:.0f} credits (over plan)")
                else:
                    parts.append(f"{used_f:.2f} / {plan_f:.0f} credits")
            except (TypeError, ValueError):
                parts.append(f"{used_f:.2f} / {plan_f:.0f} credits")
        else:
            parts.append(f"{used_f:.2f} / {plan_f:.0f} credits")

        if cost is not None:
            try:
                parts.append(f"${float(cost):.2f}")
            except (TypeError, ValueError):
                pass
        if isinstance(resets, str) and resets:
            parts.append(f"resets {resets}")
        line = "*Plan usage:* " + " · ".join(parts)
        # Defence-in-depth: `resets` / `plan` fields are raw subprocess
        # output, not LLM-generated, but apply the same redaction pipeline
        # used for every other Slack-bound string to be safe.
        line, _ = redact_exfiltration_urls(line)
        line, _ = redact_credentials(line)
        return line
    except Exception:
        logger.debug("usage line: render failed", exc_info=True)
        return "*Plan usage:* _unavailable_"


async def _publish_home_tab(orch: GatewayOrchestrator, user_id: str) -> None:
    """Build and publish the Block Kit Home Tab view."""
    try:
        blocks: list[dict] = []

        # ── Data Handling Reminder ──
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        ":warning: *Do not enter sensitive or confidential data"
                        " into KiroClaw.* Follow your organization's data handling"
                        " policy when using this tool."
                    ),
                },
            }
        )
        blocks.append({"type": "divider"})

        # ── Status ──
        yolo = is_yolo_mode()
        blocks.append(
            {"type": "header", "text": {"type": "plain_text", "text": "🐾 KiroClaw Status"}}
        )
        status_lines = [
            "*Gateway:* ✅ Online",
            f"*YOLO mode:* {'🟢 ON' if yolo else '🔴 OFF'}",
        ]
        if orch.sessions is not None:
            status_lines.append(f"*Active sessions:* {orch.sessions.count}")
        status_lines.append(f"*Uptime:* {Stats().uptime_str()}")
        status_lines.append(await get_midway_status_line())
        status_lines.append(_build_usage_status_line())
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(status_lines)}}
        )
        blocks.append({"type": "divider"})

        # ── Capabilities ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "🔌 Capabilities"}})
        try:
            servers = list_servers()
            skills = _get_skills_loader().list_skills()
            cap_lines: list[str] = []
            if servers:
                names = ", ".join(s.name for s in servers)
                raw = f"*MCP Integrations ({len(servers)}):* {names}"
                cap_lines.append(redact_credentials(redact_exfiltration_urls(raw)[0])[0])
            if skills:
                names = ", ".join(s["name"] for s in skills)
                raw = f"*Skills ({len(skills)}):* {names}"
                cap_lines.append(redact_credentials(redact_exfiltration_urls(raw)[0])[0])
            if not cap_lines:
                cap_lines.append("_No MCP servers or skills configured._")
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(cap_lines)}}
            )
        except Exception:
            logger.error("Failed to load capabilities for home tab", exc_info=True)
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_Capabilities unavailable._"},
                }
            )
        blocks.append({"type": "divider"})

        # ── Cron Jobs ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "⏰ Cron Jobs"}})
        if orch.cron_svc is not None:
            jobs = orch.cron_svc.list_jobs(include_disabled=True)
            if jobs:
                try:
                    _tz = KiroClawConfig.load().timezone
                except Exception:
                    _tz = ""
                if not _tz and orch.slack is not None:
                    try:
                        profile = await orch.slack.get_user_profile(user_id)
                        _tz = profile.get("timezone", "")
                    except Exception:
                        _tz = ""
                lines = []
                for j in jobs[:15]:
                    status = "✅" if j.enabled else "⏸️"
                    sched = format_schedule(j.schedule, tz_name=_tz)
                    raw = f"{status} *{j.name}* — `{sched}`"
                    lines.append(redact_credentials(redact_exfiltration_urls(raw)[0])[0])
                if len(jobs) > 15:
                    lines.append(f"_…and {len(jobs) - 15} more_")
                blocks.append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}}
                )
            else:
                blocks.append(
                    {"type": "section", "text": {"type": "mrkdwn", "text": "_No cron jobs._"}}
                )
        else:
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_Cron service unavailable._"},
                }
            )
        blocks.append({"type": "divider"})

        # ── Sessions (main chat + autopilot/task runner) ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "🧵 Sessions"}})
        # Deny-by-default authorization gate (defense-in-depth).
        #
        # Session JSONLs contain prior conversation contents. The dispatcher
        # already filters ``app_home_opened`` events via ``is_allowed_user``
        # at events.py before calling _publish_home_tab, so in production
        # this branch is unreachable today. The check is still required by
        # the AUTOSDE security-controls rule (deny-by-default) and protects
        # against future refactors that bypass the dispatcher gate. Mirrors
        # the slash-command pattern at events._handle_sessions.
        if not (is_owner(user_id) or is_allowed_user(user_id)):
            sel().log_api_access(
                caller=user_id,
                operation="slack.home_tab_sessions_data_access",
                outcome="denied",
                source="slack",
                resources="home_tab",
                error="unauthorized caller",
            )
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": "_Sessions unavailable._"},
                }
            )
            blocks.append({"type": "divider"})
            # Skip ahead to the next section (lessons), bypassing the
            # try block below which would otherwise read sessions.
        else:
            try:
                sess_mgr = orch.sessions
                # Read per-kind cap from config (default 5).
                try:
                    per_kind = orch._cfg.slack.home_tab_sessions_per_kind
                    if not isinstance(per_kind, int) or per_kind < 1:
                        per_kind = _HOME_TAB_SESSIONS_PER_KIND
                except (AttributeError, TypeError):
                    per_kind = _HOME_TAB_SESSIONS_PER_KIND
                # Single directory scan for both kinds; partition + cap in memory.
                all_rows = _collect_recent_sessions(
                    sess_mgr,
                    limit=per_kind * 10,
                    kind=(_SESSION_KIND_DASHBOARD, _SESSION_KIND_TASKRUNNER),
                )
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.home_tab_sessions_data_access",
                    outcome="allowed",
                    source="slack",
                    resources=f"{len(all_rows)} sessions read",
                )
                dashboard_rows = [r for r in all_rows if r["kind"] == _SESSION_KIND_DASHBOARD][
                    :per_kind
                ]
                taskrunner_rows = [r for r in all_rows if r["kind"] == _SESSION_KIND_TASKRUNNER][
                    :per_kind
                ]
                if dashboard_rows or taskrunner_rows:
                    if dashboard_rows:
                        blocks.append(
                            {
                                "type": "context",
                                "elements": [{"type": "mrkdwn", "text": "*Main chat*"}],
                            }
                        )
                        blocks.extend(_build_sessions_blocks(dashboard_rows, for_home_tab=True))
                    if taskrunner_rows:
                        if dashboard_rows:
                            blocks.append({"type": "divider"})
                        blocks.append(
                            {
                                "type": "context",
                                "elements": [
                                    {"type": "mrkdwn", "text": "*Autopilot / task runner*"}
                                ],
                            }
                        )
                        blocks.extend(_build_sessions_blocks(taskrunner_rows, for_home_tab=True))
                else:
                    blocks.append(
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "_No recent sessions._"},
                        }
                    )
            except Exception as exc:
                # Redact-then-truncate the exception message before writing
                # it to SEL ``error=``, mirroring the slash and keyword
                # error-path patterns. SEL forwards externally redact, but
                # the on-disk audit file is not internally redacted, so this
                # is defense-in-depth for the AUTOSDE security-controls
                # "never trust output" rule applied to exception messages.
                redacted_exc, _ = redact_exfiltration_urls(str(exc))
                redacted_exc, _ = redact_credentials(redacted_exc)
                logger.exception("home_tab sessions: collector failed for user %s", user_id)
                # SEL audit must record the access attempt even when the collector
                # raises, so a failure mode can't silently bypass the audit trail.
                # The success-path audit at the top of the try is skipped on
                # exception; this is the only audit that fires in that case.
                try:
                    sel().log_api_access(
                        caller=user_id,
                        operation="slack.home_tab_sessions_data_access",
                        outcome="error",
                        source="slack",
                        resources="0 sessions read (collector failed)",
                        error=redacted_exc[:200],
                    )
                except Exception:
                    logger.exception("Failed to emit SEL audit for home tab sessions error")
                blocks.append(
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "_Sessions unavailable._"},
                    }
                )
            blocks.append({"type": "divider"})

        # ── Recent Lessons ──
        blocks.append(
            {"type": "header", "text": {"type": "plain_text", "text": "📚 Recent Lessons"}}
        )
        lesson_lines: list[str] = []
        total_lessons = 0
        vs_ok = False
        # Primary: read from vector store (where learn_add writes).
        vs = getattr(orch, "vector_memory", None)
        if vs is not None and callable(getattr(vs, "get_lessons", None)):
            try:
                all_vs = vs.get_lessons()
            except Exception:
                all_vs = None
                logger.debug("Vector store lesson read failed, trying JSONL", exc_info=True)
            if isinstance(all_vs, list):
                total_lessons = len(all_vs)
                # get_lessons() returns ORDER BY updated_at DESC (most recent first).
                for entry in all_vs[:5]:
                    try:
                        parsed = json.loads(entry["value_json"])
                        rule = (
                            parsed.get("rule", str(parsed))
                            if isinstance(parsed, dict)
                            else str(parsed)
                        )
                        lesson_lines.append(
                            f"• {redact_credentials(redact_exfiltration_urls(rule)[0])[0][:100]}"
                        )
                    except Exception:
                        logger.debug("Skipping malformed lesson entry", exc_info=True)
                vs_ok = True
        # Fallback: legacy JSONL store.
        if not vs_ok and orch.ctx_builder is not None:
            all_lessons = orch.ctx_builder.lessons.load_all()
            total_lessons = len(all_lessons)
            for le in all_lessons[-5:]:
                lesson_lines.append(
                    f"• {redact_credentials(redact_exfiltration_urls(le.rule)[0])[0][:100]}"
                )
        if lesson_lines:
            if total_lessons > 5:
                lesson_lines.append(f"_…and {total_lessons - 5} more_")
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lesson_lines)}}
            )
        elif not vs_ok and orch.ctx_builder is None:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "_Lessons unavailable._"}}
            )
        else:
            blocks.append(
                {"type": "section", "text": {"type": "mrkdwn", "text": "_No lessons yet._"}}
            )
        blocks.append({"type": "divider"})

        # ── Commands ──
        blocks.append({"type": "header", "text": {"type": "plain_text", "text": "⌨️ Commands"}})
        _sc = f"/{orch.slack_command}"
        for name, (_, desc) in sorted(SLASH_REGISTRY.items()):
            blocks.append(command_hint_block(f"{_sc} {name}", desc))
        blocks.append(command_hint_block(f"{_sc} #channel", "track/untrack channel"))

        # ── Version ──
        version_text = f"📦 KiroClaw v{__version__}"
        update_info = get_update_info()
        remote_ver = update_info.get("remote_version")
        if update_info.get("available") and remote_ver is not None:
            version_text += f"  •  🆕 v{remote_ver} available — open Dashboard to update"
        version_text = redact_credentials(redact_exfiltration_urls(version_text)[0])[0]
        blocks.append({"type": "divider"})
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": version_text}]})

        view = {"type": "home", "blocks": blocks}

        if orch.slack is not None:
            await orch.slack.views_publish(user_id=user_id, view=view)
        else:
            logger.warning("Cannot publish home tab — Slack client is None")

    except Exception:
        logger.error("Failed to publish home tab for %s", user_id, exc_info=True)
        # Attempt fallback error view
        try:
            if orch.slack is not None:
                fallback = {
                    "type": "home",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {
                                "type": "mrkdwn",
                                "text": "⚠️ Failed to load Home Tab. Try again later.",
                            },
                        }
                    ],
                }
                await orch.slack.views_publish(user_id=user_id, view=fallback)
        except Exception:
            logger.debug("Fallback home tab also failed", exc_info=True)


# ---------------------------------------------------------------------------
# /tk quick-note handler (TaskKeeper integration)
# ---------------------------------------------------------------------------

_TK_NOTES_FILE = Path.home() / ".taskkeeper" / "quick-notes.json"
_TK_MAX_NOTES = 500
_tk_notes_lock: asyncio.Lock | None = None


async def _handle_tk_note(orch: GatewayOrchestrator, payload: dict) -> None:
    """Handle /tk slash command: log a quick note to TaskKeeper's quick-notes.json."""
    global _tk_notes_lock
    if _tk_notes_lock is None:
        _tk_notes_lock = asyncio.Lock()

    text = (payload.get("text") or "").strip()
    caller_id = payload.get("user_id", "")
    response_url = payload.get("response_url", "")
    channel_id = payload.get("channel_id", "")
    channel_name = payload.get("channel_name", "")

    async def _respond(msg: str) -> None:
        if not response_url:
            return
        try:
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json={"response_type": "ephemeral", "text": msg})
        except Exception:
            logger.debug("/tk response_url failed", exc_info=True)

    if not caller_id or not is_allowed_user(caller_id):
        sel().log_api_access(
            caller=caller_id,
            operation="slack.tk_note",
            outcome="denied",
            source="slack",
            error="unauthorized sender",
        )
        await _respond("⛔ You are not authorized to use this command.")
        return

    if not text:
        sel().log_api_access(
            caller=caller_id,
            operation="slack.tk_note",
            outcome="rejected",
            source="slack",
            error="empty input",
        )
        await _respond("Usage: `/tk <note>` — log a quick action item for TaskKeeper.")
        return

    # Extract optional permalink (last URL in text)
    url_match = re.search(r"(https://\S+)$", text)
    permalink = url_match.group(1) if url_match else None
    clean_text = text[: url_match.start()].strip() if url_match else text

    if not clean_text:
        clean_text = text
        permalink = None

    # Atomic write to quick-notes.json
    notes_dir = _TK_NOTES_FILE.parent
    notes_dir.mkdir(parents=True, exist_ok=True)

    def _write_note() -> None:
        notes: list = []
        if _TK_NOTES_FILE.exists():
            try:
                data = json.loads(_TK_NOTES_FILE.read_text())
                notes = data if isinstance(data, list) else []
            except (ValueError, OSError):
                notes = []
        notes.append(
            {
                "text": clean_text,
                "permalink": permalink,
                "user_id": caller_id,
                "channel_id": channel_id,
                "channel": channel_name,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
        # Cap file size: keep most recent notes
        if len(notes) > _TK_MAX_NOTES:
            notes = notes[-_TK_MAX_NOTES:]
        tmp_path = _TK_NOTES_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(notes, indent=2))
        os.replace(tmp_path, _TK_NOTES_FILE)

    async with _tk_notes_lock:
        await asyncio.to_thread(_write_note)

    sel().log_api_access(
        caller=caller_id,
        operation="slack.tk_note",
        outcome="allowed",
        source="slack",
        resources=clean_text[:100],
    )
    logger.info("/tk logged: %r (channel=%s)", clean_text, channel_name)

    # Respond via response_url
    resp_text = f'✅ Logged: "{clean_text}"'
    if permalink:
        resp_text += f"\n🔗 {permalink}"
    resp_text += "\n_Will appear in your next `check slack`._"
    await _respond(resp_text)


# Slash command handler
# ---------------------------------------------------------------------------


async def _handle_slash(orch: GatewayOrchestrator, payload: dict) -> None:
    """Route ``/kiroclaw <sub-command>`` via :data:`SLASH_REGISTRY`.

    Falls back to @user / #channel mention handling, then help text.
    """
    cmd = payload.get("command", "")
    cmd_text = payload.get("text", "").strip()
    caller_id = payload.get("user_id", "")
    response_url = payload.get("response_url", "")
    logger.info("Slash command: %s %s (caller=%s)", cmd, cmd_text, caller_id)

    async def _respond(text: str, blocks: list[dict] | None = None) -> None:
        if not response_url:
            return
        try:
            body: dict = {"text": text, "response_type": "ephemeral"}
            if blocks:
                body["blocks"] = blocks
            async with aiohttp.ClientSession() as sess:
                await sess.post(response_url, json=body)
        except Exception:
            logger.debug("slash response_url failed", exc_info=True)

    slash_command = f"/{orch.slack_command}"
    if cmd != slash_command:
        return

    # Deny-by-default — only allowed users can invoke slash commands
    if not is_allowed_user(caller_id):
        sel().log_api_access(
            caller=caller_id,
            operation="slack.slash_command",
            outcome="denied",
            source="slack",
            resources=cmd_text,
            error="unauthorized sender",
        )
        asyncio.create_task(_respond("⛔ You are not authorized to use this command."))
        return

    sel().log_api_access(
        caller=caller_id,
        operation="slack.slash_command",
        outcome="allowed",
        source="slack",
        resources=cmd_text,
    )

    if not (orch.slack and orch._owner_id):
        asyncio.create_task(_respond("⚠️ Owner not configured."))
        return

    # Parse sub-command and args
    parts = cmd_text.split(maxsplit=1)
    sub_cmd = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    # Registry lookup
    entry = SLASH_REGISTRY.get(sub_cmd)
    if entry is not None:
        handler, _ = entry
        # Stash trigger_id so modal-opening handlers can use it
        orch._last_trigger_id = payload.get("trigger_id", "")  # type: ignore[attr-defined]
        asyncio.create_task(handler(orch, caller_id, args, _respond))
        return

    # Fallback: @user mention — multi-user access disabled for security
    user_match = re.search(r"<@([A-Z0-9]+)(?:\|([^>]+))?>", cmd_text)
    if user_match:
        asyncio.create_task(
            _respond("⛔ Multi-user access is disabled. Only the owner can use KiroClaw via Slack.")
        )
        return

    # Fallback: #channel mention — Slack sends <#C1234|name> or <#C1234>
    channel_match = re.search(r"<#([A-Z0-9]+)(?:\|([^>]*))?>", cmd_text)
    if channel_match:
        channel_id = channel_match.group(1)
        channel_name = channel_match.group(2) or "Secret"
        asyncio.create_task(
            prompt_track_channel(orch.slack, orch._owner_id, channel_id, channel_name)
        )
        asyncio.create_task(_respond(f"📨 Track request sent for #{channel_name or channel_id}."))
        return

    # Unknown sub-command → help
    asyncio.create_task(_respond(_build_help_text(orch.slack_command)))


# ---------------------------------------------------------------------------
# Tracking-channel join
# ---------------------------------------------------------------------------


def _maybe_prompt_owner(orch: GatewayOrchestrator, event: dict) -> None:
    """Multi-user access disabled — channel-join allowlist prompts are blocked."""
    return


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Audio transcription helper
# ---------------------------------------------------------------------------

_AUDIO_MIMETYPES = {"audio/", "video/webm"}


async def _transcribe_with_reaction(
    slack_client: "SlackClientOps",
    channel: str,
    msg_ts: str,
    orch: "GatewayOrchestrator",
    files: list[dict],
) -> list[str]:
    """Transcribe audio files with a reaction indicator for user feedback."""
    _stt_reaction_added = False
    try:
        await slack_client.add_reaction(channel, msg_ts, "studio_microphone")
        _stt_reaction_added = True
    except Exception:
        logger.debug("Failed to add STT reaction", exc_info=True)

    try:
        transcripts = await _transcribe_files(orch, files)
    finally:
        if _stt_reaction_added:
            try:
                await slack_client.remove_reaction(
                    channel,
                    msg_ts,
                    "studio_microphone",
                )
            except Exception:
                logger.debug("Failed to remove STT reaction", exc_info=True)
    return transcripts


async def _transcribe_files(orch: "GatewayOrchestrator", files: list[dict]) -> list[str]:
    """Download and transcribe audio files, return list of transcription strings."""
    results: list[str] = []
    for f in files:
        mimetype = f.get("mimetype", "")
        if not any(mimetype.startswith(prefix) for prefix in _AUDIO_MIMETYPES):
            continue
        url = f.get("url_private_download") or f.get("url_private", "")
        if not url:
            continue
        dest: str | None = None
        try:
            raw_ft = re.sub(r"[^a-zA-Z0-9]", "", f.get("filetype", "webm"))
            suffix = "." + (raw_ft or "webm")
            fd, dest = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            assert orch.slack is not None
            assert dest is not None
            await orch.slack.download_file(url, dest)
            sel().log_api_access(
                caller="stt",
                operation="slack.download_file",
                outcome="success",
                source="transcribe",
                resources=f.get("name", "?"),
            )
            transcript = await transcribe_audio(dest)
            sel().log_api_access(
                caller="stt",
                operation="whisper.transcribe",
                outcome="success" if transcript else "empty",
                source="transcribe",
                resources=f.get("name", "?"),
            )
            if transcript:
                results.append(transcript)
                logger.info("Transcribed voice memo: %d chars", len(transcript))
            else:
                logger.warning("Transcription returned empty for %s", f.get("name", "?"))
        except Exception:
            logger.exception("Failed to transcribe file %s", f.get("name", "?"))
            sel().log_api_access(
                caller="stt",
                operation="whisper.transcribe",
                outcome="error",
                source="transcribe",
                resources=f.get("name", "?"),
                error="transcription_failed",
            )
        finally:
            if dest:
                try:
                    os.unlink(dest)
                except OSError:
                    pass
    return results


# ---------------------------------------------------------------------------
# Message routing
# ---------------------------------------------------------------------------


async def _handle_message_deleted(orch: GatewayOrchestrator, event: dict) -> None:
    """Handle message_deleted subtype — cancel queued or in-flight messages."""
    deleted_ts = event.get("deleted_ts")
    _del_thread_ts = event.get("previous_message", {}).get("thread_ts")
    _del_channel = event.get("channel", "")
    _del_user = event.get("previous_message", {}).get("user", "")
    if deleted_ts and _del_channel and is_allowed_user(_del_user):
        _del_session_key = _del_thread_ts or deleted_ts
        was_queued = False
        if orch.sessions:
            was_queued = orch.sessions.cancel_queued(_del_session_key, deleted_ts)
        if not was_queued:
            _pq = orch._pending_queue.get(_del_session_key, [])
            _filtered = [item for item in _pq if item[0] != deleted_ts]
            if len(_filtered) < len(_pq):
                was_queued = True
                if _filtered:
                    orch._pending_queue[_del_session_key] = _filtered
                else:
                    orch._pending_queue.pop(_del_session_key, None)
        if was_queued:
            logger.info(
                "message_deleted: ts=%s session=%s queued=%s",
                deleted_ts,
                _del_session_key,
                was_queued,
            )
        sel().log_api_access(
            caller=event.get("previous_message", {}).get("user", "unknown"),
            operation="slack.message_deleted",
            outcome="allowed",
            source="slack",
            resources=f"ts={deleted_ts} session={_del_session_key} queued={was_queued}",
        )


async def _dispatch_queued(
    orch: GatewayOrchestrator,
    session_key: str,
    msg_ts: str,
    text: str,
    kwargs: dict,
) -> None:
    """Dispatch a queued message — remove ⏳ reaction and call handle_message."""
    channel = kwargs.get("channel", "")
    thread_ts = kwargs.get("thread_ts")
    if orch.slack:
        try:
            await orch.slack.remove_reaction(channel, msg_ts, "hourglass_flowing_sand")
        except Exception:
            pass
    await handle_message(
        orch.slack,  # type: ignore[arg-type]
        orch.sessions,  # type: ignore[arg-type]
        channel,
        text,
        thread_ts,
        msg_ts,
        kwargs.get("sender_id", ""),
        team_id=kwargs.get("team_id", ""),
        approval_mode=APPROVAL_INTERACTIVE,
        context_builder=orch.ctx_builder,
        cron_service=orch.cron_svc,
        conversation_log=orch.conv_log,
        consolidator=orch.consolidator,
        subagent_manager=orch.subagent_mgr,
        task_runner=orch.task_runner,
        channel_agent=kwargs.get("agent_override"),
        user_display_name=kwargs.get("user_display_name"),
    )


async def _route_message(
    orch: GatewayOrchestrator,
    event: dict,
    seen: SeenCache,
    is_mention: bool = False,
    from_trusted_bot: bool = False,
) -> None:
    """Validate, dedup, check activation mode, and dispatch an incoming Slack message."""
    sender_id = event.get("user", "") or (event.get("bot_id", "") if from_trusted_bot else "")
    channel = event.get("channel", "")
    text = event.get("text", "")
    thread_ts = event.get("thread_ts")
    msg_ts = event.get("ts", "")
    team_id = event.get("team", "")
    files = event.get("files", [])

    logger.debug("Stream debug: team_id=%s user_id=%s channel=%s", team_id, sender_id, channel)

    if not sender_id or not channel or (not text and not files):
        return

    # ── Enterprise origin check: reject messages from swapped tokens ──
    if not check_message_origin(team_id):
        logger.error("Message rejected: team_id=%s does not match validated workspace", team_id)
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="denied",
            source="slack",
            resources=f"team_id={team_id} channel={channel}",
            error="enterprise_origin_mismatch",
        )
        return

    # ── Workspace routing cache for org-wide installs ──
    # Slack Web API calls (chat.postMessage, chat.startStream, etc.) need
    # team_id when the bot is org-wide installed; record the channel→team
    # mapping so outbound posts on this channel route to the correct
    # workspace and avoid ``team_access_not_granted``.
    record_team = getattr(orch.slack, "record_channel_team", None)
    if record_team and team_id:
        record_team(channel, team_id)

    # ── Access control: record authorization decision early for SEL audit ──
    # The ephemeral rejection is deferred until after activation checks so
    # users in observe/mention channels aren't spammed, but the SEL event
    # is always emitted to preserve the audit trail.
    _user_authorized = is_allowed_user(sender_id)
    if _user_authorized:
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="allowed",
            source="slack",
        )
    else:
        logger.warning("Ignoring message from unauthorized user %s", sender_id)
        sel().log_api_access(
            caller=sender_id,
            operation="slack.message",
            outcome="denied",
            source="slack",
            error="unauthorized sender",
        )

    # ── Channel activation mode (checked BEFORE ephemeral & dedup) ──
    # When activation=mention, Slack sends both a `message` and an
    # `app_mention` event for the same msg_ts.  We must skip the plain
    # `message` event *without* marking it as seen so the subsequent
    # `app_mention` event is still processed.
    ch_cfg = orch._cfg.channel_config(channel)
    activation = ch_cfg.activation

    if activation == ACTIVATION_OFF:
        # Allow !channel commands through so the owner can re-enable the channel.
        # Text may start with "<@BOTID> " when @mentioned, so strip that first.
        _stripped = text.lstrip()
        if _stripped.startswith("<@"):
            end = _stripped.find(">")
            if end != -1:
                _stripped = _stripped[end + 1 :].lstrip()
        if not _stripped.startswith("!channel"):
            logger.debug("Channel %s activation=off — ignoring message", channel)
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                resources=channel,
                error="activation=off",
            )
            return

    # Resolve sender's Slack display name so the LLM uses the actual
    # profile name instead of guessing from memory. Cached on channel
    # history (for history context) and passed to handle_message.
    _sender_display: str | None = None
    if orch.channel_history:
        _sender_display = orch.channel_history._user_names.get(sender_id)
    if not _sender_display and orch.slack and hasattr(orch.slack, "get_user_info"):
        try:
            info = await orch.slack.get_user_info(sender_id)
            _sender_display = info.get("real_name") or sender_id
            if orch.channel_history:
                orch.channel_history.set_user_name(sender_id, _sender_display)
        except Exception:
            logger.debug("Failed to resolve display name for %s", sender_id, exc_info=True)

    # Fallback: if display name is still the raw Slack ID, resolve from
    # allowed_users config (works even without Slack users:read scope).
    if (not _sender_display or _sender_display == sender_id) and hasattr(orch, "_cfg"):
        for u in getattr(orch._cfg.slack, "allowed_users", []):
            if u.get("slack_id") == sender_id and u.get("name"):
                _sender_display = u["name"]
                if orch.channel_history:
                    orch.channel_history.set_user_name(sender_id, u["name"])
                break

    # Observe mode: record history from authorized users only.
    # Previously recorded all channel traffic, but non-owner messages
    # could influence LLM context (Shepherd bdd39e84).
    if activation == ACTIVATION_OBSERVE:
        if should_record_observe_history(orch.channel_history, _user_authorized):
            assert orch.channel_history is not None  # narrowed by helper
            orch.channel_history.push(channel, sender_id, text, thread_ts=thread_ts, msg_ts=msg_ts)
        if not is_mention:
            in_active_thread = (
                ch_cfg.thread_follow
                and thread_ts
                and orch.sessions
                and (
                    orch.sessions.has_session(thread_ts)
                    or orch.sessions.get_session_for_thread(thread_ts)
                    or (orch.conv_log and orch.conv_log.has_log(thread_ts))
                )
            )
            if not in_active_thread:
                sel().log_api_access(
                    caller=sender_id,
                    operation="slack.message",
                    outcome="denied",
                    source="slack",
                    resources=channel,
                    error="activation=observe, no mention or active thread",
                )
                return

    if activation in (ACTIVATION_MENTION, ACTIVATION_REVIEW) and not is_mention:
        # In mention/review mode: ignore messages without @mention UNLESS the
        # message is a reply in a thread where the bot already has an active
        # session (i.e., the bot was previously @mentioned in that thread).
        # When thread_follow=false, always require @mention even in active threads.
        in_active_thread = (
            ch_cfg.thread_follow
            and thread_ts
            and orch.sessions
            and (
                orch.sessions.has_session(thread_ts)
                or orch.sessions.get_session_for_thread(thread_ts)
                or (orch.conv_log and orch.conv_log.has_log(thread_ts))
            )
        )
        if not in_active_thread:
            sel().log_api_access(
                caller=sender_id,
                operation="slack.message",
                outcome="denied",
                source="slack",
                resources=channel,
                error=f"activation={activation}, no mention or active thread",
            )
            return

    # ── Access control: send ephemeral rejection ──
    # Only reached for messages the bot would actually respond to,
    # preventing notification spam in observe/mention channels.
    if not _user_authorized:
        if orch.slack:
            try:
                await orch.slack.post_ephemeral(
                    channel,
                    sender_id,
                    "⛔ You are not authorized to use this bot. "
                    "Ask the owner to add you to the allowlist.",
                )
            except Exception:
                logger.debug("Failed to send ephemeral rejection", exc_info=True)
        return

    # Dedup AFTER activation check — prevents the plain `message` event
    # from poisoning the cache before the `app_mention` event arrives.
    if seen.check_and_add(msg_ts):
        return

    # ── Transcribe audio files (voice memos) ──
    # Placed after dedup + auth to avoid expensive work on duplicate events
    # or unauthorized users.
    _image_temp_paths: list[str] = []
    _had_voice_input = False
    if files and orch.slack and _user_authorized:
        if stt_available():
            transcripts = await _transcribe_with_reaction(
                orch.slack,
                channel,
                msg_ts,
                orch,
                files,
            )
            if transcripts:
                raw = "\n".join(transcripts)
                raw, _ = redact_exfiltration_urls(raw)
                raw, _ = redact_credentials(raw)
                prefix = f"[Voice memo transcription]\n{raw}\n[End of transcription]"
                text = f"{prefix}\n\n{text}" if text else prefix
                _had_voice_input = True

        # ── Process non-audio files (images, text, etc.) ──
        image_paths, text_blocks = await process_slack_files(orch, files)
        _image_temp_paths = image_paths

        # Inject image paths so AcpClient._send_prompt() inlines them as base64
        if image_paths:
            paths_text = "\n".join(image_paths)
            text = f"{text}\n{paths_text}" if text else paths_text

        # Inject text file contents
        if text_blocks:
            blocks_text = "\n\n".join(text_blocks)
            text = f"{text}\n\n{blocks_text}" if text else blocks_text

    # Bail out if we still have no text after attempting transcription
    if not text:
        # Clean up any downloaded image temp files
        for p in _image_temp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass
        return

    def _cleanup_image_temps() -> None:
        for p in _image_temp_paths:
            try:
                os.unlink(p)
            except OSError:
                pass

    # Record messages in channel history buffer (observe channels already
    # pushed above, so skip them here to avoid duplicates).
    if activation != ACTIVATION_OBSERVE:
        if orch.channel_history is None:
            logger.error("channel_history not initialised — skipping history push")
        else:
            orch.channel_history.push(channel, sender_id, text, thread_ts=thread_ts, msg_ts=msg_ts)

    # Strip the leading bot @mention so the LLM sees clean text.
    # app_mention events always start with "<@BOTID> ..." — just slice past the first ">".
    clean_text = text
    if is_mention and text.startswith("<@"):
        end = text.find(">")
        if end != -1:
            clean_text = text[end + 1 :].lstrip()
    if not clean_text:
        _cleanup_image_temps()
        return

    # ── !stop: intercept BEFORE handle_message to bypass session semaphore ──
    if clean_text.strip().lower() == "!stop":
        if not (is_owner(sender_id) or is_allowed_user(sender_id)):
            sel().log_api_access(
                caller=sender_id,
                operation="slack.stop_command",
                outcome="denied",
                source="slack",
                resources="!stop",
                error="unauthorized sender",
            )
            if orch.slack:
                await orch.slack.post_message(channel, "⛔ Not authorized.", thread_ts or msg_ts)
            return
        if not orch.sessions:
            sel().log_tool_invocation(
                session_key=thread_ts or msg_ts,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome="no_session",
                metadata={"user": sender_id, "channel": channel},
            )
            if orch.slack:
                await orch.slack.post_message(channel, "Nothing running.", thread_ts or msg_ts)
            return
        session_key = thread_ts or msg_ts
        has_session = orch.sessions.has_session(session_key)
        active_task = orch._session_tasks.pop(session_key, None)
        if has_session or active_task:
            orch.sessions.clear_queue(session_key)
            orch._pending_queue.pop(session_key, None)

            # Post ephemeral "Stopping…" block with Kill Now button
            if orch.slack:
                await orch.slack.post_ephemeral(
                    channel,
                    sender_id,
                    "Stopping…",
                    blocks=build_stopping_blocks(session_key),
                    thread_ts=session_key,
                )

            async def _on_soft() -> None:
                if orch.slack:
                    await orch.slack.post_message(channel, "⏹ Execution stopped.", session_key)

            async def _on_hard() -> None:
                if orch.slack:
                    await orch.slack.post_message(
                        channel, "⛔ Execution stopped — session reset.", session_key
                    )

            outcome = await orch.sessions.stop_turn(session_key, on_soft=_on_soft, on_hard=_on_hard)
            if active_task and not active_task.done():
                active_task.cancel()
            # If stop_turn returned "idle" (no active turn), neither callback
            # fired — dismiss the stale "Stopping…" ephemeral explicitly.
            if outcome == "idle" and orch.slack:
                await orch.slack.post_message(channel, "Nothing running.", session_key)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome=outcome,
                metadata={"user": sender_id, "channel": channel},
            )
        else:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome="no_session",
                metadata={"user": sender_id, "channel": channel},
            )
            if orch.slack:
                await orch.slack.post_message(channel, "Nothing running.", thread_ts or msg_ts)
        return

    # Per-channel agent override
    agent_override = ch_cfg.agent or None

    # ── Security: challenge-and-redirect ──
    # ALL messages that would reach the agent require dashboard session
    # verification first. Deterministic commands (!dashboard, !stop, status,
    # etc.) are handled earlier in the flow and never reach this point.
    if _CHALLENGE_REDIRECT_ENABLED:
        from kiro_claw.slack.allowlist import (
            send_channel_challenge,  # circular import: allowlist imports handler.is_allowed_user
        )

        # Resolve the thread this challenge should bind to. If the message is
        # already in a thread, use that; otherwise root a new thread at the
        # triggering message itself (its ts) so a fresh @mention becomes the
        # thread the dashboard session mirrors into. This is what lets dashboard
        # turns (user + agent) flow back into the originating Slack thread.
        challenge_thread_ts = thread_ts or msg_ts or ""

        # If that thread is already linked to a dashboard session, carry its
        # session key so the challenge reopens the SAME session instead of
        # spawning a disconnected one. Otherwise the dashboard auto-links the
        # new session to challenge_thread_ts.
        linked_session_key = ""
        if challenge_thread_ts and orch.sessions:
            linked_session_key = orch.sessions.get_session_for_thread(challenge_thread_ts) or ""

        url = await send_channel_challenge(
            orch.slack,  # type: ignore[arg-type]
            channel,
            sender_id,
            clean_text,
            thread_ts=challenge_thread_ts,
            session_key=linked_session_key,
        )
        if url:
            # Ack the message with a lock emoji so the user knows it was received
            if not channel.startswith("D") and orch.slack:
                try:
                    await orch.slack.add_reaction(channel, msg_ts, "lock")
                except Exception:
                    pass
            sel().log_api_access(
                caller=sender_id,
                operation="slack.channel_challenge",
                outcome="redirected",
                source="slack",
                resources=f"channel={channel}",
            )
        else:
            # Deny-by-default: do NOT fall through to inline processing.
            # This is a security control — silently processing inline on
            # failure would bypass dashboard auth enforcement.
            sel().log_api_access(
                caller=sender_id,
                operation="slack.channel_challenge",
                outcome="challenge_failed",
                source="slack",
                resources=f"channel={channel}",
            )
            if orch.slack:
                try:
                    fallback = (
                        "⚠️ Couldn't open a dashboard session right now. "
                        "Please try again later or open the dashboard directly."
                    )
                    if channel.startswith("D"):
                        await orch.slack.post_message(channel, fallback)
                    else:
                        await orch.slack.post_ephemeral(channel, sender_id, fallback)
                except Exception:
                    pass
        _cleanup_image_temps()
        return

    logger.info(
        "Message from %s in %s (activation=%s): %s", sender_id, channel, activation, text[:80]
    )

    # ── Queue check: if session is busy, enqueue instead of blocking ──
    session_key = thread_ts or msg_ts
    _task_busy = session_key in orch._session_tasks
    if _task_busy:
        # A task is already running for this session key.  Try the session-level
        # queue first (semaphore-based); fall back to an orchestrator-level
        # pre-session queue when the session object doesn't exist yet.
        _queued = orch.sessions and orch.sessions.enqueue(
            session_key,
            msg_ts,
            clean_text,
            force=True,
            channel=channel,
            thread_ts=thread_ts,
            sender_id=sender_id,
            team_id=team_id,
            agent_override=agent_override,
            user_display_name=_sender_display,
        )
        if not _queued:
            # Session object not created yet — stash on orch._pending_queue
            orch._pending_queue.setdefault(session_key, []).append(
                (
                    msg_ts,
                    clean_text,
                    dict(
                        channel=channel,
                        thread_ts=thread_ts,
                        sender_id=sender_id,
                        team_id=team_id,
                        agent_override=agent_override,
                        user_display_name=_sender_display,
                    ),
                )
            )
        logger.info(
            "Message %s queued for busy session %s (session_obj=%s)", msg_ts, session_key, _queued
        )
        if orch.slack:
            try:
                await orch.slack.add_reaction(channel, msg_ts, "hourglass_flowing_sand")
            except Exception:
                logger.debug("Failed to add queue reaction", exc_info=True)
        _cleanup_image_temps()
        return
    elif orch.sessions and orch.sessions.enqueue(
        session_key,
        msg_ts,
        clean_text,
        channel=channel,
        thread_ts=thread_ts,
        sender_id=sender_id,
        team_id=team_id,
        agent_override=agent_override,
        user_display_name=_sender_display,
    ):
        logger.info("Message %s queued for busy session %s", msg_ts, session_key)
        if orch.slack:
            try:
                await orch.slack.add_reaction(channel, msg_ts, "hourglass_flowing_sand")
            except Exception:
                logger.debug("Failed to add queue reaction", exc_info=True)
        _cleanup_image_temps()
        return

    try:
        t = asyncio.create_task(
            handle_message(
                orch.slack,  # type: ignore[arg-type]
                orch.sessions,  # type: ignore[arg-type]
                channel,
                clean_text,
                thread_ts,
                msg_ts,
                sender_id,
                team_id=team_id,
                approval_mode=APPROVAL_INTERACTIVE,
                context_builder=orch.ctx_builder,
                cron_service=orch.cron_svc,
                conversation_log=orch.conv_log,
                consolidator=orch.consolidator,
                subagent_manager=orch.subagent_mgr,
                task_runner=orch.task_runner,
                channel_agent=agent_override,
                user_display_name=_sender_display,
                from_trusted_bot=from_trusted_bot,
                channel_activation=activation,
                had_voice_input=_had_voice_input,
            )
        )
    except Exception:
        logger.exception("Failed to create handle_message task")
        _cleanup_image_temps()
        return

    orch._session_tasks[session_key] = t

    def _on_done(task: asyncio.Task) -> None:  # type: ignore[type-arg]
        orch._handler_tasks.discard(task)
        if orch._session_tasks.get(session_key) is task:
            del orch._session_tasks[session_key]
        _cleanup_image_temps()
        # Drain queue: only if no other task took over this session
        try:
            if session_key not in orch._session_tasks and orch.sessions:
                _next = orch.sessions.dequeue(session_key)
                # Fall back to orchestrator-level pending queue (pre-session messages)
                if not _next:
                    _pq = orch._pending_queue.get(session_key)
                    if _pq:
                        _next = _pq.pop(0)
                        if not _pq:
                            del orch._pending_queue[session_key]
                if _next:
                    _q_ts, _q_text, _q_kw = _next
                    _q_t = asyncio.ensure_future(
                        _dispatch_queued(orch, session_key, _q_ts, _q_text, _q_kw)
                    )
                    orch._session_tasks[session_key] = _q_t
                    orch._handler_tasks.add(_q_t)
                    _q_t.add_done_callback(_on_done)
        except Exception:
            logger.exception("_on_done drain failed for %s", session_key)

    orch._handler_tasks.add(t)
    t.add_done_callback(_on_done)
