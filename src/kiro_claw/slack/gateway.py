"""Slack Socket Mode gateway orchestrator for KiroClaw.

Manages the lifecycle of all runtime services: session manager, cron
scheduler, context builder, heartbeat, subagents, task runner, dashboard,
and the Slack Socket Mode connection.

Event routing, interactive button handling, and allowlist management
live in sibling modules:

- ``events``        — Socket Mode event dispatch + dedup
- ``interactions``  — Block Kit button routing
- ``allowlist``     — tracking-channel join prompts + config persistence
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from aiohttp import web
from slack_sdk.errors import SlackApiError
from slack_sdk.socket_mode.websockets import SocketModeClient as WSSocketModeClient

import kiro_claw
from kiro_claw import shutdown_event
from kiro_claw.acp.client import AcpError, AcpProcessDied
from kiro_claw.autonudge import (
    AutoNudgeService,
    NudgeLoop,
)
from kiro_claw.autonudge import enabled as autonudge_enabled
from kiro_claw.channel_history import ChannelHistory
from kiro_claw.config import KiroClawConfig
from kiro_claw.config.loader import (
    CRED_OWNER_ID,
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    _session_work_dir,
    config_dir,
)
from kiro_claw.constants import CHAT_TURN_TIMEOUT, DATA_WARNING
from kiro_claw.context import ContextBuilder
from kiro_claw.cron import CronJob, CronService, build_cron_session_context
from kiro_claw.cron_script import resolve_script_path, run_command_sandboxed, run_script_sandboxed
from kiro_claw.dashboard import start_dashboard
from kiro_claw.dashboard.chat_runner import _run_chat
from kiro_claw.dashboard.cron_inject import inject_cron_result_to_dashboard
from kiro_claw.dashboard.handlers import MAX_PROMPT_BYTES
from kiro_claw.dashboard.handlers.autonudge import render_nudge_message
from kiro_claw.dashboard.handlers.messaging import _rehydrate_slot_from_history
from kiro_claw.dashboard.origin import (
    build_dashboard_url,
    format_dashboard_urls,
    is_local_only,
    parse_dashboard_url,
    resolve_dashboard_host,
)
from kiro_claw.dashboard.state import DashboardState
from kiro_claw.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token
from kiro_claw.embeddings import OllamaManager, _validate_url, make_sync_embed_fn
from kiro_claw.frontend import build_frontend_async
from kiro_claw.heartbeat import (
    HEARTBEAT_TASK_TIMEOUT_SECS,
    HeartbeatService,
    is_keep_response,
    strip_keep_sentinel,
)
from kiro_claw.history import ConversationLog, HistoryConsolidator
from kiro_claw.hooks import HookManager, HooksConfig
from kiro_claw.learn import LessonStore
from kiro_claw.llm_helpers import PromptBusyExhaustedError, ToolApprovalPolicy, stream_and_collect
from kiro_claw.memory import MemoryStore
from kiro_claw.providers.base import LLMEvent
from kiro_claw.safety_override import safety_override
from kiro_claw.security import redact_credentials, redact_exfiltration_urls
from kiro_claw.sel import sel
from kiro_claw.session import BACKGROUND_KEY, SessionManager
from kiro_claw.skills import SkillsLoader
from kiro_claw.slack.client import RealSlackClient
from kiro_claw.slack.format import build_cron_ack_block, split_message, to_slack_mrkdwn
from kiro_claw.subagent import (
    INJECTION_TIMEOUT,
    SubagentInfo,
    SubagentManager,
    ToolApprovalCallback,
)
from kiro_claw.taskrunner import TaskRunner

if TYPE_CHECKING:
    from kiro_claw.dashboard.state import _ChatSlot
    from kiro_claw.task_models import Task

logger = logging.getLogger(__name__)

# Full chat turn timeout — tool calls, multi-step reasoning, spawning.
# More generous than INJECTION_TIMEOUT (120s) which only covers stream_and_collect.

# Max retries for injecting subagent results into parent sessions.
_MAX_INJECT_ATTEMPTS = 2

# Approval sources that run UNATTENDED (no human responder). These deny-fast on a
# short window instead of burning the full 2h human-approval window. Subagent
# approvals are NOT background: they route to the dashboard where the spawning
# human is present (via the parent slot), so they keep the long interactive window.
_BACKGROUND_APPROVAL_SOURCES = frozenset({"cron", "heartbeat", "taskrunner", ""})

# Slack Block Kit section.text hard limit is 3000 chars.
# We split cron output at this boundary so each chunk fits in a section block.
_CRON_MSG_LIMIT = 3000

# Volatile patterns stripped before hashing cron results for dedup.
_VOLATILE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"  # ISO timestamps
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",  # UUIDs
    re.IGNORECASE,
)
_EPOCH_RE = re.compile(r"\b\d{10,13}\b")
_EPOCH_WINDOW_SECS = 300  # strip epoch values within ±5 min of now
_SUCCESS_REMINDER_SECS = 86400  # post "still succeeding w/ same result" reminder every 24h
_FAILURE_REMINDER_SECS = 3600  # re-alert still-failing cron every 1h (louder than success dedup)


# Tool-name prefixes treated as read-only by the --approval reads flag.
# Matched against the leading verb token of an event.title (e.g. "Read foo.txt"
# -> "read"). Conservative list — anything not on it falls through to the
# standard approval flow.
_READ_ONLY_TOOL_PREFIXES = (
    "read",
    "list",
    "get",
    "search",
    "find",
    "describe",
    "show",
    "view",
    "fetch",
    "query",
    "grep",
    "ls",
    "cat",
    "head",
    "tail",
)

# Tokens that disqualify a tool from auto-approval even if its leading
# verb is in _READ_ONLY_TOOL_PREFIXES. After splitting the title on
# whitespace/punctuation/underscore/dash, any resulting token that exactly
# matches one of these entries causes rejection. Catches compound names
# a third-party MCP author might pick (e.g. read_or_write, find_and_replace,
# get_or_create) where the read prefix masks a write capability. Fail
# closed on ambiguity.
_WRITE_INDICATORS = (
    "write",
    "delete",
    "create",
    "destroy",
    "remove",
    "update",
    "modify",
    "replace",
    "set",
    "put",
    "post",
    "exec",
    "execute",
    "run",
    "rm",
    "rmdir",
    "drop",
    "patch",
    "send",
    "publish",
    "save",
    "edit",
    "kill",
    "terminate",
)


def _is_read_only_tool(event_title: str) -> bool:
    """Return True if event_title looks like a read-only tool invocation.

    Used by --approval reads to auto-approve a conservative set of read
    verbs while still gating writes. Two-stage check:

    1. Leading token (before any whitespace/punctuation) must be in
       _READ_ONLY_TOOL_PREFIXES.
    2. After splitting the title on whitespace/punctuation/underscore/dash,
       no resulting token may exactly match one in _WRITE_INDICATORS — catches
       compound names like read_or_write, find_and_replace, get_or_create.
       Exact token equality, not substring containment: ``setter`` does not
       match ``set``.

    Fails closed on ambiguity.
    """
    if not event_title:
        return False
    lowered = event_title.strip().lower()
    if not lowered:
        return False
    # Tokenize on whitespace, underscores, dashes, and common punctuation
    # so compound names like read_or_write break into ["read", "or", "write"].
    tokens = [t for t in re.split(r"[\s_\-:()/.,]+", lowered) if t]
    if not tokens:
        return False
    leading = tokens[0]
    if leading not in _READ_ONLY_TOOL_PREFIXES:
        return False
    # Reject if any token (other than the leading verb itself) is a known
    # write indicator. Catches read_or_write, find_and_replace, etc.
    if any(token in _WRITE_INDICATORS for token in tokens):
        return False
    return True


def _result_hash(text: str) -> str:
    """Normalize volatile data and return a 16-hex-char SHA-256 prefix.

    Strips ISO timestamps, UUIDs, and any 10–13 digit number that looks
    like an epoch timestamp (within ±5 minutes of now).  Non-epoch numeric
    IDs (account IDs, build IDs) are likely preserved because they would
    likely fall outside the time window.

    Truncated to 64 bits — sufficient for 1:1 comparison against a single
    previous hash (collision probability ~1/2^64 per comparison).
    """
    now = time.time()
    lo = now - _EPOCH_WINDOW_SECS
    hi = now + _EPOCH_WINDOW_SECS

    def _strip_epoch(m: re.Match) -> str:
        v = int(m.group())
        # 13 digits → millis, convert to seconds for comparison
        ts = v / 1000 if v > 9_999_999_999 else v
        return "" if lo <= ts <= hi else m.group()

    text = _VOLATILE_RE.sub("", text)
    text = _EPOCH_RE.sub(_strip_epoch, text)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class GatewayOrchestrator:
    """Manages the lifecycle of all gateway services.

    Responsibilities are intentionally narrow — event routing and
    interactive handling are delegated to :mod:`events` and
    :mod:`interactions` respectively.
    """

    def __init__(
        self, cfg: KiroClawConfig, *, no_dashboard: bool = False, no_crons: bool = False, no_open: bool = False,
        port_override: str | None = None,
        json_ready: bool = False,
        approval_mode: str | None = None,
    ) -> None:
        # NOTE: test_heartbeat_prompt_deliver.py creates instances via __new__
        # (bypassing __init__). Update that fixture if new attributes are added.
        self._cfg = cfg
        self._no_dashboard = no_dashboard
        self._no_crons = no_crons
        self._no_open = no_open
        self._port_override = port_override
        self._json_ready = json_ready
        self._approval_mode = approval_mode
        creds = cfg.load_credentials()
        self._app_token = creds.get(CRED_SLACK_APP_TOKEN, "")
        self._bot_token = creds.get(CRED_SLACK_BOT_TOKEN, "")
        self._owner_id = creds.get(CRED_OWNER_ID, "")
        # Multi-user access is disabled — only owner is authorized.
        # Prune stale allowed_users entries from config and warn.
        stale = {u["slack_id"] for u in cfg.slack.allowed_users} - (
            {self._owner_id} if self._owner_id else set()
        )
        if stale:
            logger.warning(
                "Pruning %d stale allowlist entries (multi-user disabled): %s",
                len(stale),
                stale,
            )
        self._allowed_users: set[str] = {self._owner_id} if self._owner_id else set()
        self._tracking_channels: set[str] = {
            c["channel_id"] for c in cfg.slack.tracking_channels if c.get("channel_id")
        }
        self._open_channels: set[str] = set(cfg.slack.open_channels)
        self._slack_enabled = bool(self._app_token and self._bot_token)
        self.slack_command = cfg.slack.command

        # Services (initialized in start())
        self.slack: RealSlackClient | None = None
        self.sessions: SessionManager | None = None
        self.ctx_builder: ContextBuilder | None = None
        self.conv_log: ConversationLog | None = None
        self.consolidator: HistoryConsolidator | None = None
        self.cron_svc: CronService | None = None
        self.heartbeat_svc: HeartbeatService | None = None
        # Secretary runtime service removed (Amazon-internal). Attribute stays
        # as an inert None so other modules referencing it degrade gracefully.
        self.secretary_svc: object | None = None
        self.subagent_mgr: SubagentManager | None = None
        self._cron_injecting: dict[str, int] = {}  # parent_key → pending injection count
        self._running_script_ids: set[str] = set()  # job IDs with in-flight script/command execution
        self.task_runner: TaskRunner | None = None
        self.channel_history: ChannelHistory | None = None
        self.dashboard_state: DashboardState | None = None
        self._background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks
        self._dashboard_runner: web.AppRunner | None = None
        self._handler_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        self._session_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._pending_queue: dict[str, list] = {}
        self._socket_client: WSSocketModeClient | None = None
        self._ollama_manager: object | None = None  # OllamaManager (lazy import)

    # ------------------------------------------------------------------
    # Tool approval callback (shared by cron, heartbeat, subagent, task)
    # ------------------------------------------------------------------

    def _interactive_approval(
        self, source: str, slot_resolver: Callable[[str], str] | None = None
    ) -> ToolApprovalCallback:
        """Return an approval callback that races dashboard vs Slack DM.

        Uses the same rich Block Kit message as the main-agent approval flow
        so users see full command text, security redactions, and Trust-session
        controls for background agents too.
        """

        is_background = source in _BACKGROUND_APPROVAL_SOURCES

        async def _approve(event: LLMEvent, parent_session_key: str = "") -> bool:
            # Resolve slot: use explicit slot, or try to find from active dashboard slot
            # Heuristic fallback: picks first running slot (dict insertion order). Not guaranteed
            # to be the correct slot for subagents, but explicit slot param is the primary path.
            resolved_slot = ""
            if not resolved_slot and self.dashboard_state and self.dashboard_state._slots:
                # Heuristic: pick first running slot (insertion order)
                for k in self.dashboard_state._slots:
                    if self.dashboard_state._slots[k].running:
                        resolved_slot = k.removeprefix("dashboard:")
                        break

            # Per-source auto-approve (e.g. cron, taskrunner, subagent)
            if source in self._cfg.hooks.get("auto_approve_sources", []):
                logger.info("Auto-approving tool %s from source %s", event.title, source)
                return True

            # CLI --approval flag override (composable test mode).
            # 'yolo' auto-approves all; 'reads' auto-approves read-only tools;
            # 'interactive' falls through to the standard flow.
            if self._approval_mode in ("yolo", "reads"):
                approve = self._approval_mode == "yolo" or (
                    self._approval_mode == "reads" and _is_read_only_tool(event.title or "")
                )
                if approve:
                    # Emit a SEL audit event so the audit trail records WHICH
                    # mode auto-approved the tool. Downstream sites already
                    # log the invocation itself; this captures the decision.
                    try:
                        _safe = redact_exfiltration_urls(redact_credentials(event.title or "")[0])[0]
                        sel().log_api_access(
                            caller=f"cli:approval={self._approval_mode}",
                            operation=f"{source}.cli_approval_auto_approve",
                            outcome="ok",
                            resources=_safe,
                        )
                    except Exception:
                        logger.warning("SEL audit failed for cli --approval auto-approve", exc_info=True)
                    return True

            # Check both YOLO sources: Slack handler (!yolo on) and dashboard UI
            if safety_override().is_active():
                return True

            if self.dashboard_state:
                # Check if the parent slot is trusted (not all slots).
                # Use slot_resolver or resolved_slot to find the parent;
                # only fall back to all-slots check when neither exists.
                # When slot_resolver exists but returns falsy, we do NOT
                # fall back to the heuristic -- if the explicit resolver
                # can't find the parent, guessing would widen trust scope.

                def _sel_log(**kw: str) -> None:
                    try:
                        sel().log_api_access(**kw)
                    except Exception:
                        logger.warning("SEL audit failed for trust check", exc_info=True)

                _safe_title = redact_exfiltration_urls(redact_credentials(event.title)[0])[0]

                if slot_resolver:
                    try:
                        _parent_slot_key = slot_resolver(str(event.request_id))
                    except Exception:
                        logger.warning("slot_resolver failed for %s", event.request_id, exc_info=True)
                        _parent_slot_key = None
                elif resolved_slot:
                    _parent_slot_key = resolved_slot
                else:
                    _parent_slot_key = None

                if _parent_slot_key:
                    _ps = (self.dashboard_state._slots or {}).get(_parent_slot_key)
                    if _ps and _ps._trust:
                        _sel_log(
                            caller=f"slot:{_parent_slot_key}",
                            operation=f"{source}.scoped_trust_auto_approve",
                            outcome="ok",
                            resources=_safe_title,
                        )
                        return True
                    elif _ps:
                        _sel_log(
                            caller=f"slot:{_parent_slot_key}",
                            operation=f"{source}.scoped_trust_not_trusted",
                            outcome="not_auto_approved",
                            resources=_safe_title,
                        )
                    else:
                        _sel_log(
                            caller=f"slot:{_parent_slot_key}",
                            operation=f"{source}.scoped_trust_slot_not_found",
                            outcome="not_auto_approved",
                            resources=_safe_title,
                        )
                elif not slot_resolver and not resolved_slot:
                    # No resolver available at all -- fall back to all-slots
                    slots = self.dashboard_state._slots
                    if slots and all(s._trust for s in slots.values()):
                        _sel_log(
                            caller=f"source:{source}",
                            operation=f"{source}.all_slots_trust_auto_approve",
                            outcome="ok",
                            resources=_safe_title,
                        )
                        return True
                    else:
                        _sel_log(
                            caller=f"source:{source}",
                            operation=f"{source}.all_slots_trust_not_trusted",
                            outcome="not_auto_approved",
                            resources=_safe_title,
                        )
                else:
                    # Resolver existed but failed -- fall through to interactive approval
                    _sel_log(
                        caller=f"source:{source}",
                        operation=f"{source}.scoped_trust_fallthrough",
                        outcome="not_auto_approved",
                        resources=_safe_title,
                    )

            request_id = str(event.request_id)

            # Post approval buttons to Slack DM if available
            if self.slack and self._owner_id:
                try:
                    # Resolve parent thread context for threaded approval prompts
                    thread_ts: str | None = None
                    channel: str | None = None
                    if parent_session_key and self.sessions:
                        channel = self.sessions.get_channel(parent_session_key)
                        thread_ts = self.sessions.get_thread(parent_session_key)
                        if not thread_ts and channel:
                            # Slack ts format: "{epoch_seconds}.{microseconds}" — pure digits + one dot
                            if re.fullmatch(r"\d+\.\d+", parent_session_key):
                                thread_ts = parent_session_key
                    is_dm = not channel
                    if not channel:
                        channel = await self.slack.open_dm(self._owner_id)
                        thread_ts = None
                    from kiro_claw.slack.handler import (
                        _build_approval_blocks,
                        _pending_approvals,
                        _PendingApproval,
                    )

                    blocks = _build_approval_blocks(event, is_dm=is_dm, source=source)
                    title_safe, _ = redact_exfiltration_urls(event.title)
                    title_safe, _ = redact_credentials(title_safe)
                    fallback = f"🔐 [{source}] Approve: {title_safe}?"
                    approval_ts = await self.slack.post_blocks(
                        channel, blocks, fallback, thread_ts  # type: ignore[arg-type]
                    )

                    # Create a pending approval that the interactive handler can resolve.
                    # Use a dummy provider — the actual approve/reject is handled by
                    # returning True/False from this callback.
                    pending = _PendingApproval(
                        provider=None,  # type: ignore[arg-type]
                        request_id=request_id,
                        session_key=parent_session_key,
                    )
                    key = f"{channel}:{approval_ts}"
                    _pending_approvals[key] = pending

                    # Also request via dashboard if available
                    dashboard_future = None
                    if self.dashboard_state:
                        dashboard_future = asyncio.ensure_future(
                            self.dashboard_state.request_approval(
                                request_id,
                                source,
                                event.title,
                                tool_input=event.tool_input,
                                tool_purpose=event.tool_purpose,
                                slot=slot_resolver(request_id) if slot_resolver else resolved_slot,
                                is_background=is_background,
                            )
                        )

                        # When dashboard resolves, also resolve the Slack future
                        def _on_dashboard_done(fut: asyncio.Future) -> None:  # type: ignore[type-arg]
                            if fut.cancelled() or fut.exception():
                                return
                            result = "approved" if fut.result() else "rejected"
                            if not pending.future.done():
                                pending.future.set_result(result)

                        dashboard_future.add_done_callback(_on_dashboard_done)

                    # Wait for either Slack or dashboard approval. Background
                    # sources (no human present) deny-fast on a short window
                    # instead of burning the full 2h human window.
                    approval_timeout = (
                        DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS
                        if is_background
                        else DashboardState._APPROVAL_TIMEOUT
                    )
                    try:
                        outcome = await asyncio.wait_for(
                            pending.future, timeout=approval_timeout
                        )
                    except asyncio.TimeoutError:
                        outcome = "rejected"
                    finally:
                        _pending_approvals.pop(key, None)
                        # Resolve dashboard approval if Slack responded first
                        if self.dashboard_state:
                            self.dashboard_state.resolve_approval(request_id, outcome == "approved")
                        if dashboard_future and not dashboard_future.done():
                            dashboard_future.cancel()

                    # Clean up Slack message
                    try:
                        status = "✅ Approved" if outcome == "approved" else "🚫 Rejected"
                        await self.slack.update_message(
                            channel, approval_ts, text=f"🔐 *{title_safe}* — {status}"
                        )
                    except Exception:
                        pass

                    return outcome == "approved"
                except Exception:
                    logger.debug("Slack approval failed, falling back to dashboard", exc_info=True)

            # Fallback: dashboard only
            if self.dashboard_state:
                return await self.dashboard_state.request_approval(
                    request_id,
                    source,
                    event.title,
                    tool_input=event.tool_input,
                    tool_purpose=event.tool_purpose,
                    slot=slot_resolver(request_id) if slot_resolver else resolved_slot,
                    is_background=is_background,
                )
            return True  # no UI → auto-approve

        return _approve

    # Required packages that must be importable (import_name, pip_spec).
    # pip_spec may include version constraints matching setup.cfg.
    _REQUIRED_DEPS = [
        ("snowballstemmer", "snowballstemmer>=1.0"),
        # PyYAML (import name ``yaml``) is imported by cc_agent on every CLI
        # path. It installs cleanly from public PyPI, so list it here as a
        # backstop: if it is ever missing (e.g. a partial install), the startup
        # self-heal repairs it instead of every command crashing at import.
        ("yaml", "PyYAML>=6,<7"),
    ]

    @staticmethod
    def _is_brazil_install(proj: str) -> bool:
        """Return True if *proj* was installed via Brazil, False for venv/pip."""
        method_file = Path(proj) / ".install-method"
        if method_file.is_file():
            return method_file.read_text().strip() == "brazil"
        return bool(
            shutil.which("brazil-build")
            and (Path(proj).parent.parent / ".brazil").is_dir()
        )

    def _check_missing_deps(self) -> None:
        """Auto-repair missing pip deps for venv installs.

        After auto-update, old code may have pulled new source via git reset
        but skipped ``pip install``. This catches the gap on next startup.
        """
        missing = [
            pip for mod, pip in self._REQUIRED_DEPS
            if importlib.util.find_spec(mod) is None
        ]
        if not missing:
            return

        proj = os.environ.get("KIROCLAW_PROJECT_DIR", "")
        if not proj or self._is_brazil_install(proj):
            return

        logger.warning("Missing deps %s — installing directly", missing)
        print(f"🐾 Installing missing dependencies: {', '.join(missing)}")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--quiet", *missing],
            cwd=proj,
            capture_output=True,
            timeout=300,
        )
        if result.returncode == 0:
            # Invalidate import caches so the new packages are found
            importlib.invalidate_caches()
            print("✅ Dependencies installed")
        else:
            print("❌ pip install failed — run manually: kiroclaw update")
            logger.error("Dep repair failed: %s", result.stderr.decode(errors="replace")[:500])

    # ------------------------------------------------------------------
    # Service initialisation
    # ------------------------------------------------------------------

    def _init_services(self) -> None:
        """Initialize memory, skills, hooks, context, history, sessions."""
        if not self._slack_enabled:
            logger.info("Starting in dashboard-only mode (no Slack credentials)")

        # Check kiro-cli version (--agent requires >= 1.26)
        try:
            result = subprocess.run(["kiro-cli", "--version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                # e.g. "kiro-cli 1.25.0" -> (1, 25, 0)
                parts = result.stdout.strip().split()[-1].split(".")
                major, minor = int(parts[0]), int(parts[1])
                if (major, minor) < (1, 26):
                    print(
                        f"⚠️  kiro-cli {major}.{minor} is outdated (1.26+ required). "
                        "Update kiro-cli, or use the default claude-agent-acp backend."
                    )
        except Exception:
            pass

        # Auto-repair missing pip deps (handles chicken-and-egg after auto-update)
        try:
            self._check_missing_deps()
        except Exception:
            logger.warning("Dep check failed", exc_info=True)

        # Auto-install agent config so MCP servers are always up to date
        try:
            from kiro_claw.agent import rebuild_agent_config  # circular import

            path = rebuild_agent_config()
            logger.info("Agent config installed: %s", path)

            # Deliver shim + one-time stale-MCP purge automatically — the
            # desktop app launches the gateway but never runs `kiroclaw setup`.
            from kiro_claw.agent import run_first_run_setup  # circular import

            run_first_run_setup()
        except Exception:
            logger.warning("Agent config install failed", exc_info=True)

        self.slack = RealSlackClient(self._bot_token) if self._slack_enabled else None
        factory = self._cfg.create_provider_factory()

        # Memory, skills, hooks, lessons
        memory = MemoryStore()
        memory.init()

        # Vector memory (structured semantic store)
        from kiro_claw.vector_memory import VectorMemoryStore

        self.vector_memory = VectorMemoryStore(
            confidence_threshold=self._cfg.memory.semantic_confidence_threshold,
            extra_prefixes=self._cfg.memory.semantic_keys or None,
            episodic_limit=self._cfg.memory.episodic_max_results,
            embedding_dim=self._cfg.memory.embedding_dim,
        )
        self.vector_memory.init()
        memory.vector_store = self.vector_memory

        skills = SkillsLoader()
        hooks = HookManager(HooksConfig.from_dict(self._cfg.hooks))
        lessons = LessonStore()
        self.ctx_builder = ContextBuilder(
            memory=memory,
            skills=skills,
            hooks=hooks,
            lessons=lessons,
            bot_name=self._cfg.agent.bot_name,
        )

        # Conversation history
        self.conv_log = ConversationLog()
        self.conv_log.init()
        self.ctx_builder.conversation_log = self.conv_log

        # Session manager
        self.sessions = SessionManager(
            self._cfg, provider_factory=factory
        )  # type: ignore[arg-type]

        # History consolidator
        self.consolidator = HistoryConsolidator(
            log=self.conv_log,
            memory=memory,
            sessions=self.sessions,
            lesson_store=lessons,
            history_idle_secs=self._cfg.memory.history_idle_hours * 3600,
            vector_store=self.vector_memory,
            migrated=self._cfg.memory.migrated,
            skills_loader=skills,
            auto_skills_enabled=self._cfg.skills.auto_create_from_sessions,
            auto_refine_enabled=self._cfg.skills.auto_refine_on_deviation,
            auto_min_tool_calls=self._cfg.skills.auto_min_tool_calls,
            auto_similarity_threshold=self._cfg.skills.auto_similarity_threshold,
        )

        # Trigger skill extraction when sessions expire (idle/orphan)
        self.sessions.on_session_expire = self.consolidator.consolidate_session

        # Channel history buffer
        self.channel_history = ChannelHistory(
            observe_max_entries=self._cfg.observe_max_messages,
            observe_ttl_secs=int(self._cfg.observe_ttl_hours * 3600),
            history_dir=config_dir() / "history",
        )
        self.ctx_builder.channel_history = self.channel_history

        # Register observe-mode channels for deeper history buffer
        from kiro_claw.config.loader import ACTIVATION_OBSERVE

        for ch_id, ch_cfg in self._cfg.slack_channels.items():
            if ch_cfg.activation == ACTIVATION_OBSERVE:
                self.channel_history.set_observe(ch_id)

        # FTS index
        indexed = memory.rebuild_index()
        logger.info("FTS index built: %d files", indexed)

    async def _open_dm_with_retry(self, user_id: str, job_name: str, max_attempts: int = 3) -> str | None:
        """Retry open_dm to handle transient Slack API errors."""
        if self.slack is None:
            return None
        for attempt in range(1, max_attempts + 1):
            try:
                return await self.slack.open_dm(user_id)
            except (SlackApiError, ConnectionError, TimeoutError) as exc:
                # Retry on rate-limits (429), server errors (5xx), and network errors;
                # re-raise immediately on non-retryable client errors (4xx except 429).
                retryable = (
                    not isinstance(exc, SlackApiError)
                    or exc.response.status_code == 429
                    or exc.response.status_code >= 500
                )
                if not retryable:
                    raise
                if attempt < max_attempts:
                    logger.warning(
                        "Cron '%s': open_dm attempt %d/%d failed, retrying in %ds",
                        job_name, attempt, max_attempts, attempt,
                        exc_info=True,
                    )
                    await asyncio.sleep(attempt)
                else:
                    raise
        return None  # unreachable but satisfies type checker

    async def _init_cron(self) -> None:
        """Initialize and start the cron service."""

        async def _deliver_script_result(job: CronJob, message: str, *, remove: bool = False) -> None:
            """Deliver a script cron result to the originating session. Optionally remove the job."""
            delivered = False
            try:
                if message and not job.silent and self.dashboard_state and job.session_key:
                    slot_key = job.session_key.removeprefix("dashboard:")
                    slot = self.dashboard_state.get_slot(slot_key)
                    if slot is None:
                        slot = _rehydrate_slot_from_history(self.dashboard_state, slot_key)
                    label = redact_exfiltration_urls(redact_credentials(job.name)[0])[0]
                    if slot:
                        wrapped = f'[Cron notification: "{label}"]\n{message}\n[/Cron notification]'
                        inject_cls = json.dumps({"cronLabel": label})
                        if slot.running:
                            qid = slot.queue_append(wrapped)
                            _cls = json.loads(inject_cls)
                            _cls["queue_id"] = qid
                            slot.append("queued", wrapped, json.dumps(_cls))
                        else:
                            slot.append("inject", wrapped, inject_cls)
                            task = asyncio.create_task(_run_chat(self.dashboard_state, slot, wrapped))
                            slot.task = task
                            self.dashboard_state._background_tasks.add(task)
                            task.add_done_callback(self.dashboard_state._background_tasks.discard)
                        self.dashboard_state.push_slots_update()
                    else:
                        self.dashboard_state.notify("cron", f"⚡ {label}", message, meta={"job_id": job.id})
                elif message and not job.silent and self.dashboard_state:
                    label = redact_exfiltration_urls(redact_credentials(job.name)[0])[0]
                    self.dashboard_state.notify("cron", f"⚡ {label}", message, meta={"job_id": job.id})
                delivered = True
            except Exception as notify_exc:
                logger.warning("Cron '%s' delivery failed: %s", job.name, notify_exc)
            if remove and delivered and self.cron_svc:
                self.cron_svc.remove_job(job.id)

        async def _cron_callback(job: CronJob) -> str | None:
            # Mesh-1026: helper picks stable vs ephemeral session key and
            # decides whether to prepend last_result, based on job.persistent_session.
            session_key, msg = build_cron_session_context(job)

            # ── Concurrent execution guard ──
            if (job.script or job.command) and job.id in self._running_script_ids:
                logger.info("Cron '%s': previous execution still running, skipping", job.name)
                return None

            # ── Command mode: direct shell execution (sandboxed) ──
            if job.command:
                self._running_script_ids.add(job.id)
                try:
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}", tool_name="cron_command_exec",
                            tool_kind="cron_command", outcome="invoked",
                        )
                    except Exception:
                        logger.debug("SEL logging failed in cron command invoked path", exc_info=True)
                    cmd_timeout = job.timeout or 300
                    result = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(
                            None, run_command_sandboxed, job.command, cmd_timeout
                        ),
                        timeout=cmd_timeout + 5,
                    )
                    output = result.get("output", "")
                    if not output.strip():
                        if result.get("status") == "ok":
                            job.last_status = "ok"
                            job.last_error = ""
                            job.consecutive_failures = 0
                        else:
                            job.last_status = "error"
                            job.last_error = f"non-ok status with no output (status={result.get('status')})"
                            job.consecutive_failures += 1
                            if job.consecutive_failures >= 5:
                                job.enabled = False
                        return None  # no output = no delivery
                    job.last_result = redact_exfiltration_urls(redact_credentials(output)[0])[0]
                    job.last_error = ""
                    if result.get("status") == "ok":
                        job.last_status = "ok"
                        job.consecutive_failures = 0
                    else:
                        job.last_status = "error"
                        job.last_error = f"command failed (exit_code={result.get('exit_code')})"
                        job.consecutive_failures += 1
                        if job.consecutive_failures >= 5:
                            job.enabled = False
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}", tool_name="cron_command_exec",
                            tool_kind="cron_command", outcome=job.last_status,
                        )
                    except Exception:
                        logger.debug("SEL logging failed in cron command result path", exc_info=True)
                    return job.last_result
                except asyncio.TimeoutError:
                    job.last_error = f"timeout ({cmd_timeout + 5}s)"
                    job.last_status = "error"
                    job.consecutive_failures += 1
                    if job.consecutive_failures >= 5:
                        job.enabled = False
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}", tool_name="cron_command_exec",
                            tool_kind="cron_command", outcome="timeout",
                        )
                    except Exception:
                        logger.debug("SEL logging failed in cron command timeout path", exc_info=True)
                    return None
                except Exception as exc:
                    logger.exception("Command cron '%s' failed: %s", job.name, exc)
                    err_str = redact_exfiltration_urls(redact_credentials(str(exc))[0])[0]
                    job.last_error = err_str[:200]
                    job.last_status = "error"
                    job.consecutive_failures += 1
                    if job.consecutive_failures >= 5:
                        job.enabled = False
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}", tool_name="cron_command_exec",
                            tool_kind="cron_command", outcome="error",
                        )
                    except Exception:
                        logger.debug("SEL logging failed in cron command error path", exc_info=True)
                    return None
                finally:
                    self._running_script_ids.discard(job.id)

            # ── Code-based script execution (deterministic, no LLM) ──
            if job.script:
                self._running_script_ids.add(job.id)
                try:
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}", tool_name=job.script,
                            tool_kind="cron_script", outcome="invoked",
                        )
                    except Exception:
                        logger.debug("SEL logging failed in cron script invoked path", exc_info=True)
                    # Validate path before spawning subprocess
                    resolve_script_path(job.script)
                    # Run in sandboxed subprocess via wrap_argv()
                    script_timeout = job.timeout or 30
                    result = await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(
                            None, run_script_sandboxed, job.script, job.id, job.message, script_timeout
                        ),
                        timeout=script_timeout + 5,
                    )
                    status = result.get("status", "error")
                    if status == "ok":
                        job.last_result = "ok"
                        job.last_error = ""
                        job.last_status = "ok"
                        job.consecutive_failures = 0
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}", tool_name=job.script,
                                tool_kind="cron_script", outcome="ok",
                            )
                        except Exception:
                            logger.debug("SEL logging failed in cron script ok path", exc_info=True)
                        return "ok"
                    elif status == "skip":
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}", tool_name=job.script,
                                tool_kind="cron_script", outcome="skip",
                            )
                        except Exception:
                            logger.debug("SEL logging failed in cron script skip path", exc_info=True)
                        return None
                    elif status == "done":
                        msg = result.get("message", "")
                        job.last_result = redact_exfiltration_urls(redact_credentials(msg)[0])[0] if msg else ""
                        job.last_error = ""
                        job.last_status = "ok"
                        job.consecutive_failures = 0
                        # Deliver Done message and remove job
                        await _deliver_script_result(job, job.last_result, remove=True)
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}", tool_name=job.script,
                                tool_kind="cron_script", outcome="done",
                            )
                        except Exception:
                            logger.debug("SEL logging failed in cron script done path", exc_info=True)
                        return job.last_result or "done"
                    elif status == "report":
                        msg = result.get("message", "")
                        job.last_result = redact_exfiltration_urls(redact_credentials(msg)[0])[0] if msg else ""
                        job.last_error = ""
                        job.last_status = "ok"
                        job.consecutive_failures = 0
                        # Deliver Report message (keep job running)
                        await _deliver_script_result(job, job.last_result)
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}", tool_name=job.script,
                                tool_kind="cron_script", outcome="report",
                            )
                        except Exception:
                            logger.debug("SEL logging failed in cron script report path", exc_info=True)
                        return job.last_result or "report"
                    else:
                        err = result.get("error", "unknown error")
                        raise RuntimeError(err)
                except asyncio.TimeoutError:
                    logger.warning("Script cron '%s' timed out after %ds", job.name, script_timeout + 5)
                    job.last_error = f"timeout ({script_timeout + 5}s)"
                    job.last_status = "error"
                    job.consecutive_failures += 1
                    if job.consecutive_failures >= 5:
                        job.enabled = False
                        logger.warning("Script cron '%s' auto-paused after %d consecutive errors", job.name, job.consecutive_failures)
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}", tool_name=job.script,
                            tool_kind="cron_script", outcome="error", error=f"timeout ({script_timeout + 5}s)",
                        )
                    except Exception:
                        logger.debug("SEL logging failed in cron script timeout path", exc_info=True)
                    return None
                except Exception as exc:
                    logger.exception("Script cron '%s' failed: %s", job.name, exc)
                    err_str = redact_exfiltration_urls(redact_credentials(str(exc))[0])[0]
                    job.last_error = err_str
                    job.last_status = "error"
                    job.consecutive_failures += 1
                    if job.consecutive_failures >= 5:
                        job.enabled = False
                        logger.warning("Script cron '%s' auto-paused after %d consecutive errors", job.name, job.consecutive_failures)
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}", tool_name=job.script,
                            tool_kind="cron_script", outcome="error", error=err_str,
                        )
                    except Exception:
                        logger.debug("SEL logging failed in cron script error path", exc_info=True)
                    return None
                finally:
                    self._running_script_ids.discard(job.id)

            # ── Sequential agent execution (Mimir integration) ──
            # When agent_sequence has multiple agents, run them sequentially
            # with per-agent session keys and per-job env vars.
            agents = job.agent_sequence if job.agent_sequence else []
            if len(agents) > 1:
                assert self.sessions is not None
                assert self.ctx_builder is not None
                result_text = "_No response._"
                for agent in agents:
                    agent_session_key = f"cron:{job.id}:{agent}"
                    if self.cron_svc is not None:
                        self.cron_svc.register_active_session_key(job.id, agent_session_key)
                    _acq = False
                    try:
                        client, is_new, _resumed = await self.sessions.get_or_create(
                            agent_session_key,
                            agent=agent,
                            channel_id=job.channel,
                            approval_policy=job.approval_mode,
                            extra_env=job.env or None,
                        )
                        _acq = True
                        full_message, _ = self.ctx_builder.build_message(
                            msg, True, interactive=False, agent=agent
                        )
                        result_text = await stream_and_collect(
                            client,
                            full_message,
                            approval_policy=(
                                ToolApprovalPolicy.AUTO_APPROVE
                                if job.approval_mode == "auto"
                                else ToolApprovalPolicy.HOOK_BASED
                            ),
                            hooks=self.ctx_builder.hooks,
                            on_tool_approval=(
                                None if job.approval_mode == "auto"
                                else self._interactive_approval("cron")
                            ),
                        )
                        if not result_text:
                            result_text = "_No response._"
                        logger.info(
                            "Cron '%s': agent '%s' completed", job.name, agent
                        )
                    finally:
                        if _acq:
                            self.sessions.release(agent_session_key)
                            await self.sessions.reset(agent_session_key)
                            if self.cron_svc is not None:
                                self.cron_svc.clear_active_session_key(job.id)
                job.last_result = result_text
                return result_text

            # ── Single-agent path (existing behavior) ──
            # Tell the reaper which key to target if this run hangs.
            if self.cron_svc is not None:
                self.cron_svc.register_active_session_key(job.id, session_key)

            _acquired = False
            try:
                assert self.sessions is not None
                assert self.ctx_builder is not None
                client, is_new, _resumed = await self.sessions.get_or_create(
                    session_key,
                    agent=job.agent_id or None,
                    channel_id=job.channel,
                    approval_policy=job.approval_mode,
                    extra_env=job.env or None,
                )
                _acquired = True
                if job.acked_items:
                    msg += (
                        "\n\n[User has seen and acknowledged ALL of the following — "
                        "do NOT repeat the same content]\n"
                        + "\n".join(f"- {a}" for a in job.acked_items)
                    )
                _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                full_message, _ = self.ctx_builder.build_message(
                    msg, True, interactive=False, agent=job.agent_id or None,
                    provider_type=_provider,
                )

                result_text = await stream_and_collect(
                    client,
                    full_message,
                    approval_policy=(
                        ToolApprovalPolicy.AUTO_APPROVE
                        if job.approval_mode == "auto"
                        else ToolApprovalPolicy.HOOK_BASED
                    ),
                    hooks=self.ctx_builder.hooks,
                    on_tool_approval=(
                        None if job.approval_mode == "auto" else self._interactive_approval("cron")
                    ),
                )

                if not result_text:
                    result_text = "_No response._"

                job.last_result = result_text

                # ── Error deduplication ──
                # Suppress Slack for repeated identical results to avoid spam.
                rh = _result_hash(result_text)

                # Clear failure dedup on any success, regardless of whether
                # the success result itself is a dup. A successful run means
                # the job recovered — next failure should always alert fresh.
                job.last_failure_hash = ""
                job.last_failure_at = 0.0
                job.consecutive_failures = 0

                if rh == job.last_posted_hash:
                    job.consecutive_dupes += 1
                    # Time-based reminder: re-post after 24h so persistent identical
                    # results don't go unnoticed indefinitely.
                    if time.time() - job.last_posted_at >= _SUCCESS_REMINDER_SECS:
                        # NB: consecutive_dupes is captured here before the reset
                        # at the post-delivery state update further below.
                        result_text = (
                            f"⚠️ Cron '{job.name}' has produced the same result"
                            f" {job.consecutive_dupes} times in a row:\n\n{result_text}"
                        )
                    else:
                        logger.info(
                            "Cron '%s': duplicate result #%d — suppressing Slack",
                            job.name,
                            job.consecutive_dupes,
                        )
                        if self.dashboard_state:
                            redacted_for_dash, _ = redact_exfiltration_urls(result_text)
                            redacted_for_dash, _ = redact_credentials(redacted_for_dash)
                            title = f"🔇 Cron: {job.name} (dup #{job.consecutive_dupes})"
                            title, _ = redact_exfiltration_urls(title)
                            title, _ = redact_credentials(title)
                            self.dashboard_state.notify(
                                "cron",
                                title,
                                redacted_for_dash,
                                meta={"job_id": job.id},
                            )

                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_dedup_suppress",
                            outcome="suppressed",
                            downstream_service="none",
                        )
                        # Still inject into dashboard slot even when Slack is suppressed
                        if self.dashboard_state and job.persistent_session and self.dashboard_state.has_slot(f"cron-{job.id}"):
                            inject_cron_result_to_dashboard(self.dashboard_state, job, result_text)
                        return result_text

                if job.silent:
                    logger.info("Cron job '%s' silent — suppressing auto-delivery", job.name)

                    sel().log_tool_invocation(
                        session_key=f"cron:{job.id}",
                        tool_name="cron_silent_suppress",
                        outcome="suppressed",
                        downstream_service="none",
                    )
                    # Still inject into dashboard slot even when silent
                    if self.dashboard_state and job.persistent_session and self.dashboard_state.has_slot(f"cron-{job.id}"):
                        inject_cron_result_to_dashboard(self.dashboard_state, job, result_text)
                    return result_text

                if self.dashboard_state:
                    # Inject into slot BEFORE notification so has_slot() is true for notify_meta
                    if job.persistent_session:
                        history = await asyncio.to_thread(
                            self.dashboard_state.conversation_log.read_messages, f"cron:{job.id}"
                        ) if self.dashboard_state.conversation_log else []
                        inject_cron_result_to_dashboard(self.dashboard_state, job, result_text, history=history)
                    redacted_for_dash, _ = redact_exfiltration_urls(result_text)
                    redacted_for_dash, _ = redact_credentials(redacted_for_dash)
                    safe_name, _ = redact_exfiltration_urls(job.name)
                    safe_name, _ = redact_credentials(safe_name)
                    notify_meta: dict[str, str] = {"job_id": job.id}
                    if job.persistent_session and self.dashboard_state.has_slot(f"cron-{job.id}"):
                        notify_meta["slot"] = f"cron-{job.id}"
                    self.dashboard_state.notify(
                        "cron",
                        f"Cron: {safe_name}",
                        redacted_for_dash,
                        meta=notify_meta,
                    )
                if self.slack:
                    try:
                        # Retry only open_dm (transient Slack API errors).
                        # Delivery (post_blocks/post_message) is NOT retried to avoid duplicates.
                        channel = job.channel
                        if not channel and (job.created_by or self._owner_id):
                            channel = await self._open_dm_with_retry(
                                job.created_by or self._owner_id, job.name
                            )
                        if channel:
                            redacted, _ = redact_exfiltration_urls(result_text)
                            redacted, _ = redact_credentials(redacted)
                            post_text = f"⏰ *Cron: {job.name}*\n\n{to_slack_mrkdwn(redacted)}"
                            parts = split_message(post_text, limit=_CRON_MSG_LIMIT)
                            # First part as Block Kit message with ack button
                            blocks: list[dict] = [
                                {
                                    "type": "section",
                                    "text": {"type": "mrkdwn", "text": parts[0]},
                                },
                            ] + build_cron_ack_block(job.id)
                            parent_ts = await self.slack.post_blocks(
                                channel, blocks, parts[0], job.thread_ts
                            )
                            thread_root = job.thread_ts or parent_ts
                            # Store thread_ts so subagents can route replies here
                            if thread_root and self.sessions:
                                await self.sessions.set_thread(session_key, thread_root)
                                await self.sessions.set_channel(session_key, channel)
                            # Overflow parts as threaded follow-up messages
                            for part in parts[1:]:
                                await self.slack.post_message(channel, part, thread_root)
                            # Dedup state: only advance after confirmed delivery.
                            job.last_posted_hash = rh
                            job.consecutive_dupes = 0
                            job.last_posted_at = time.time()
                        else:
                            logger.warning("Cron '%s': no channel resolved, skipping notification", job.name)
                    except Exception as slack_exc:
                        logger.error(
                            "Cron job '%s': Slack delivery failed (job succeeded)",
                            job.name,
                            exc_info=True,
                        )
                        if self.dashboard_state:
                            exc_msg, _ = redact_exfiltration_urls(str(slack_exc))
                            exc_msg, _ = redact_credentials(exc_msg)
                            self.dashboard_state.notify(
                                "cron",
                                f"Cron: {job.name}",
                                f"⚠️ Job completed but Slack delivery failed: {exc_msg}",
                                meta={"job_id": job.id},
                            )
                # Session cleanup happens in finally block
                return result_text
            except Exception as exc:
                # Attempt one retry for ACP process death before any dedup / alert.
                exc_msg = str(exc).lower()
                if (
                    isinstance(exc, AcpError)
                    and ("not running" in exc_msg or "process exited" in exc_msg)
                    and not getattr(job, "_acp_retried", False)
                    and self.sessions is not None
                ):
                    logger.warning(
                        "Cron '%s': ACP process died, resetting session and retrying",
                        job.name,
                    )
                    job._acp_retried = True  # type: ignore[attr-defined]
                    try:
                        if _acquired:
                            self.sessions.release(session_key)
                            _acquired = False
                        await self.sessions.reset(session_key)
                        return await _cron_callback(job)
                    except Exception:
                        pass  # retry failed — fall through to dedup + alert
                    finally:
                        job._acp_retried = False  # type: ignore[attr-defined]
                logger.exception("Cron job '%s' failed", job.name)
                # During an in-flight ACP retry (inner recursive _cron_callback
                # call), suppress all notify/slack/dedup work — the outer
                # invocation is authoritative and will handle notification
                # for the retry's final failure. Without this guard, the
                # inner call emits its own dashboard notify + Slack alert
                # and advances dedup state, duplicating the outer handler.
                if getattr(job, "_acp_retried", False):
                    raise
                # ── Failure dedup: suppress repeated identical crash notifications ──
                exc_summary = f"{type(exc).__name__}: {exc}"
                exc_summary, _ = redact_exfiltration_urls(exc_summary)
                exc_summary, _ = redact_credentials(exc_summary)
                fh = _result_hash(exc_summary)
                is_dup = fh == job.last_failure_hash
                if is_dup and time.time() - job.last_failure_at < _FAILURE_REMINDER_SECS:
                    job.consecutive_failures += 1
                    logger.info(
                        "Cron '%s': duplicate failure #%d — suppressing Slack",
                        job.name,
                        job.consecutive_failures,
                    )
                    # Dashboard notify is best-effort — never mask the original
                    # exception if notification itself fails.
                    try:
                        if self.dashboard_state:
                            title = f"🔇 Cron: {job.name} (dup failure #{job.consecutive_failures})"
                            title, _ = redact_exfiltration_urls(title)
                            title, _ = redact_credentials(title)
                            self.dashboard_state.notify(
                                "cron",
                                title,
                                f"❌ Job failed (suppressed — same error):\n{exc_summary}",
                                meta={"job_id": job.id, "failure_hash": fh},
                            )
                    except Exception:
                        logger.debug("Dashboard notify failed in cron failure suppress path", exc_info=True)
                    # SEL logging is best-effort — never mask the original
                    # exception if audit logging itself fails.
                    try:

                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_failure_dedup_suppress",
                            outcome="suppressed",
                            downstream_service="none",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron failure suppress path",
                            exc_info=True,
                        )
                    raise
                # First failure (or fresh failure after reminder window) — alert.
                # Dashboard notify is best-effort — never mask the original
                # exception if notification itself fails.
                try:
                    if self.dashboard_state:
                        alert_title = f"Cron: {job.name}"
                        alert_title, _ = redact_exfiltration_urls(alert_title)
                        alert_title, _ = redact_credentials(alert_title)
                        self.dashboard_state.notify("cron", alert_title, "❌ Job failed")
                except Exception:
                    logger.debug("Dashboard notify failed in cron failure alert path", exc_info=True)
                # Compute the count this alert represents (including itself) so
                # the re-alert message can call out persistence.
                new_count = job.consecutive_failures + 1 if is_dup else 1
                if is_dup:
                    fail_msg = (
                        f"⏰ *Cron: {job.name}* ❌ _Job still failing"
                        f" ({new_count} consecutive identical failures)"
                        f" — check logs._"
                    )
                else:
                    fail_msg = f"⏰ *Cron: {job.name}* ❌ _Job failed — check logs._"
                slack_failed = False  # track real delivery exceptions only
                if self.slack:

                    try:
                        channel = job.channel
                        if not channel and (job.created_by or self._owner_id):
                            channel = await self._open_dm_with_retry(
                                job.created_by or self._owner_id, job.name
                            )
                        if channel:
                            fail_msg, _ = redact_exfiltration_urls(fail_msg)
                            fail_msg, _ = redact_credentials(fail_msg)
                            await self.slack.post_message(channel, fail_msg)
                        else:
                            logger.warning("Cron '%s': no channel resolved for error notification", job.name)

                    except Exception:
                        slack_failed = True
                        logger.error(
                            "Cron job '%s': Slack failure-notification delivery failed",
                            job.name,
                            exc_info=True,
                        )
                # Advance dedup state unless Slack delivery raised. "No channel
                # available" is treated as a skip (not a failure), so dedup still
                # advances — otherwise every identical failure re-notifies the
                # dashboard, which is what dedup is supposed to prevent.
                if not slack_failed:
                    job.last_failure_hash = fh
                    job.last_failure_at = time.time()
                    job.consecutive_failures = new_count
                    # SEL logging is best-effort — never mask the original
                    # exception if audit logging itself fails.
                    try:

                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_failure_alert",
                            outcome="alerted",
                            downstream_service="slack" if self.slack else "none",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron failure alert path",
                            exc_info=True,
                        )
                raise
            finally:
                assert self.sessions is not None
                if _acquired:
                    self.sessions.release(session_key)
                    # Defer session reset if subagents are still running or
                    # mid-injection — _subagent_done will reset after the last one.
                    has_pending = self.subagent_mgr and any(
                        a.parent_session_key == session_key for a in self.subagent_mgr.running
                    )
                    has_injecting = self._cron_injecting.get(session_key, 0) > 0
                    if has_pending or has_injecting:
                        logger.info("Cron '%s': deferring reset, subagents pending", job.name)
                        # Mesh-1026: leave the active-session registration in place so
                        # the reaper can still target the ephemeral key if the deferred
                        # reset hangs. _subagent_done will clear it after the real reset.
                    else:
                        await self.sessions.reset(session_key)
                        # Mesh-1026: reset done → reaper no longer needs this key.
                        if self.cron_svc is not None:
                            self.cron_svc.clear_active_session_key(job.id)
                # Restore per-job env vars (single-agent path) — now handled via extra_env passthrough

        self.cron_svc = CronService(base_dir=config_dir(), on_job=_cron_callback)
        if self.dashboard_state:
            self.cron_svc.set_refresh_callback(self.dashboard_state.push_refresh)
        if self._no_crons:
            logger.info("Cron scheduler disabled (--no-crons)")
        else:
            await self.cron_svc.start()
            if self.sessions:
                self.cron_svc.start_reaper(self.sessions)
            else:
                logger.warning("Cron reaper not started: sessions not available")

    async def _init_heartbeat(self) -> None:
        """Initialize and start the heartbeat service."""
        memory = self.ctx_builder.memory if self.ctx_builder else MemoryStore()

        async def _heartbeat_task(task_text: str, deliver: str) -> str | None:
            assert self.sessions is not None
            assert self.ctx_builder is not None
            session_key = BACKGROUND_KEY
            _acquired = False
            try:
                client, is_new, _resumed = await self.sessions.get_or_create(session_key)
                _acquired = True
                full_message, _ = self.ctx_builder.build_message(task_text, is_new)

                # A heartbeat turn runs unattended. Bound it with a hard deadline
                # (mirrors cron's _execute_with_timeout) so a single non-allowlisted
                # tool approval — which blocks on the human-approval wait with no
                # human present — cannot freeze the whole heartbeat subsystem.
                result_text = await asyncio.wait_for(
                    stream_and_collect(
                        client,
                        full_message,
                        approval_policy=ToolApprovalPolicy.HOOK_BASED,
                        hooks=self.ctx_builder.hooks,
                        on_tool_approval=self._interactive_approval("heartbeat"),
                    ),
                    timeout=HEARTBEAT_TASK_TIMEOUT_SECS,
                )

                if not result_text:
                    result_text = "_No response._"
            except asyncio.TimeoutError:
                # Tear down the in-flight turn so the underlying claude-agent-acp
                # process/turn doesn't linger holding the background session.
                # Do this BEFORE the finally releases the session.
                logger.warning(
                    "Heartbeat task timed out after %ds, resetting session: %s",
                    HEARTBEAT_TASK_TIMEOUT_SECS,
                    task_text[:80],
                )
                try:
                    await self.sessions.reset(session_key)
                except Exception:
                    logger.warning("Heartbeat: session reset after timeout failed", exc_info=True)
                # Produce a graceful incomplete result rather than crashing the loop.
                result_text = (
                    f"_Heartbeat task timed out after {HEARTBEAT_TASK_TIMEOUT_SECS}s "
                    "and was cancelled._"
                )
            except Exception:
                logger.exception("Heartbeat task failed: %s", task_text[:80])
                raise
            finally:
                if _acquired:
                    self.sessions.release(session_key)
                    await self.sessions.recycle_background()

            result_safe, _ = redact_exfiltration_urls(result_text)
            result_safe, _ = redact_credentials(result_safe)
            display_text = strip_keep_sentinel(result_safe)
            # Only notify when task is complete — suppress delivery for
            # incomplete tasks (HEARTBEAT_KEEP) to avoid spamming every cycle.
            if is_keep_response(result_safe):
                logger.info("Heartbeat task incomplete, suppressing delivery: %s", task_text[:80])
            else:
                task_safe, _ = redact_exfiltration_urls(task_text[:100])
                task_safe, _ = redact_credentials(task_safe)
                await self._deliver_result(
                    "💓 Heartbeat",
                    task_safe,
                    display_text,
                    deliver,
                )
            return result_safe

        self.heartbeat_svc = HeartbeatService(
            memory=memory,
            on_task=_heartbeat_task,
            consolidator=self.consolidator,
        )
        await self.heartbeat_svc.start()

    async def _init_autonudge(self) -> None:
        """Initialize and start the auto-nudge service (feature-flagged)."""
        if not autonudge_enabled():
            logger.info("AutoNudge disabled via feature flag")
            return

        async def _fire(loop: NudgeLoop) -> bool:
            """Inject nudge message into the bound chat slot.

            Returns True if the nudge was actually dispatched, False if skipped
            (slot missing, dashboard not ready, or turn still active). The
            service uses this to avoid counting skipped cycles toward
            max_cycles.
            """
            # Guard (not assert): stripped under -O; also _init_autonudge() can
            # run before _init_dashboard(), and _init_dashboard is skipped
            # entirely in --no-dashboard mode. Mirrors _observer's guard below.
            if self.dashboard_state is None:
                logger.warning(
                    "AutoNudge: dashboard not ready — skipping fire for loop %s", loop.id
                )
                return False
            slot = self.dashboard_state._slots.get(loop.slot_key)
            if slot is None:
                logger.warning("AutoNudge: slot %s missing — removing loop %s", loop.slot_key, loop.id)
                await self.autonudge_svc.remove(loop.id)  # type: ignore[union-attr]
                return False
            msg = render_nudge_message(loop.message, loop.stop_sentinel_path)
            tagged = f"[auto-nudge cycle {loop.cycle_count + 1}]\n{msg}"
            from kiro_claw.dashboard.chat import (
                _run_chat,  # circular import: gateway -> dashboard.chat -> gateway (chat dispatch references GatewayOrchestrator)
            )

            if slot.running:
                # Turn still active — drop this nudge. Next idle-timer tick will
                # schedule again once the turn ends. Queueing would stack
                # identical 3KB+ nudges and blow up the context window.
                # Returning False keeps cycle_count accurate (only delivered
                # nudges count toward max_cycles).
                logger.info(
                    "AutoNudge skip: slot %s is running (loop %s cycle %d)",
                    slot.key,
                    loop.id,
                    loop.cycle_count,
                )
                return False
            # Show nudge as a distinct "nudge" role message in the slot history.
            slot.append("nudge", tagged, "msg msg-nudge")
            task = asyncio.create_task(_run_chat(self.dashboard_state, slot, tagged))
            # Mirror dashboard /api/chat/send path so slot.running == True and sidebar
            # shows the "turn active" three-dots indicator immediately.
            slot.task = task
            self.dashboard_state._background_tasks.add(task)
            task.add_done_callback(self.dashboard_state._background_tasks.discard)
            self._session_tasks[slot.key] = task
            self.dashboard_state.push_slots_update()
            return True

        def _observer(event: str, loop: NudgeLoop | None) -> None:
            if self.dashboard_state and loop is not None:
                self.dashboard_state.broadcast_ws(
                    "autonudge_state",
                    {
                        "event": event,
                        "slot": loop.slot_key,
                        "loop": {
                            "id": loop.id,
                            "slot_key": loop.slot_key,
                            "message": loop.message,
                            "idle_secs": loop.idle_secs,
                            "max_cycles": loop.max_cycles,
                            "cycle_count": loop.cycle_count,
                            "active": loop.active,
                            "last_fire_ts": loop.last_fire_ts,
                        },
                    },
                )

        self.autonudge_svc = AutoNudgeService(base_dir=config_dir(), on_fire=_fire)
        self.autonudge_svc.subscribe(_observer)
        await self.autonudge_svc.start()

    @staticmethod
    def _notif_meta(parent_key: str | None) -> dict[str, str] | None:
        """Build notification meta with slot or slack_link for jump-to-source."""
        if not parent_key:
            return None
        if parent_key.startswith("dashboard:"):
            return {"slot": parent_key.removeprefix("dashboard:")}
        if ":" in parent_key and not parent_key.startswith(("cron:", "subagent:", "hook:")):
            chan, ts = parent_key.split(":", 1)
            return {
                "slack_link": f"https://amzn-aws.slack.com/archives/{chan}/p{ts.replace('.', '')}"
            }
        return None

    async def _deliver_result(
        self,
        title: str,
        task_summary: str,
        result_text: str,
        deliver: str,
    ) -> None:
        """Route a background result to the right surface.

        ``deliver`` values:
        - ``prompt:dashboard:<slot>`` → send as user prompt to dashboard slot (triggers agent turn)
        - ``dashboard:<slot>`` → inject into existing dashboard chat slot
        - ``dashboard``        → create new dashboard chat slot
        - ``slack:<chan>:<ts>`` → reply to Slack thread
        - ``slack``            → new Slack DM only (no dashboard notification)
        - ``silent``           → log only
        - ``""`` (empty)       → Slack DM (if available) + dashboard notification
        """
        result_text, _ = redact_exfiltration_urls(result_text)
        result_text, _ = redact_credentials(result_text)
        task_summary, _ = redact_exfiltration_urls(task_summary)
        task_summary, _ = redact_credentials(task_summary)
        title, _ = redact_exfiltration_urls(title)
        title, _ = redact_credentials(title)
        body = f"{task_summary}\n\n{result_text}"

        # ── silent: log only ──
        if deliver == "silent":
            logger.info("%s (silent): %s", title, task_summary)
            return

        # ── prompt:dashboard:<slot> → send as user prompt to slot (triggers agent turn) ──
        if deliver.startswith("prompt:dashboard:"):
            slot_name = deliver.removeprefix("prompt:dashboard:")
            if not slot_name:
                logger.debug("Heartbeat prompt:dashboard: missing slot name, skipping")
                return
            if self.dashboard_state:
                slot = self.dashboard_state.resolve_slot(slot_name)
                if slot:
                    # Truncate the variable-size *content* separately so the title/prefix
                    # can never be sliced at a multi-byte boundary. errors='ignore'
                    # (not 'replace') keeps the final byte size <= limit — U+FFFD
                    # would be 3 bytes and push past the cap.
                    prefix = f"{title}\n\n"
                    prefix_bytes = len(prefix.encode("utf-8"))
                    content_budget = max(0, MAX_PROMPT_BYTES - prefix_bytes)
                    content_bytes = result_text.encode("utf-8")
                    if len(content_bytes) > content_budget:
                        truncated = content_bytes[:content_budget].decode(
                            "utf-8", errors="ignore"
                        )
                        logger.warning(
                            "Heartbeat prompt truncated to %d bytes for slot %s",
                            MAX_PROMPT_BYTES,
                            slot_name,
                        )
                        prompt = prefix + truncated
                    else:
                        prompt = prefix + result_text
                    # Lazy import avoids circular dependency (chat → gateway)
                    from kiro_claw.dashboard.chat import _run_chat

                    sel().log_api_access(
                        caller="heartbeat",
                        operation="heartbeat_prompt_deliver",
                        outcome="approved",
                        source="gateway",
                        resources=f"requested={slot_name},resolved={slot.key}",
                    )
                    ran = slot.enqueue_or_run_prompt(
                        prompt, _run_chat, self.dashboard_state
                    )
                    if ran:
                        # Only push UI updates when the prompt actually started —
                        # queued prompts produce no visible change until dequeued.
                        self.dashboard_state.push_slots_update()
                        self.dashboard_state.notify(
                            "heartbeat", title, body, meta={"slot": slot.key}
                        )
                    else:
                        logger.info(
                            "Heartbeat prompt queued for busy slot %s (queue depth=%d)",
                            slot.key,
                            slot.queue_depth,
                        )
                else:
                    sel().log_api_access(
                        caller="heartbeat",
                        operation="heartbeat_prompt_deliver",
                        outcome="not_found",
                        source="gateway",
                        resources=f"requested={slot_name}",
                    )
                    logger.warning("Heartbeat prompt target slot %s not found", slot_name)
            else:
                logger.debug("prompt:dashboard:%s ignored — no dashboard_state", slot_name)
            return

        # ── dashboard:<slot> → inject into specific slot ──
        if deliver.startswith("dashboard:"):
            slot_name = deliver.removeprefix("dashboard:")
            if self.dashboard_state:
                slot = self.dashboard_state.resolve_slot(slot_name)
                if slot:
                    sel().log_api_access(
                        caller="heartbeat",
                        operation="heartbeat_inject_deliver",
                        outcome="approved",
                        source="gateway",
                        resources=f"requested={slot_name},resolved={slot.key}",
                    )
                    slot.append("assistant", f"{title}\n\n{result_text}", "msg msg-a")
                    self.dashboard_state.push_slots_update()
                    self.dashboard_state.notify(
                        "heartbeat", title, body, meta={"slot": slot.key}
                    )
                else:
                    sel().log_api_access(
                        caller="heartbeat",
                        operation="heartbeat_inject_deliver",
                        outcome="not_found",
                        source="gateway",
                        resources=f"requested={slot_name}",
                    )
                    logger.warning("Heartbeat deliver target slot %s not found", slot_name)
            else:
                logger.debug("dashboard:%s ignored — no dashboard_state", slot_name)
            return

        # ── dashboard (no slot) → new slot ──
        if deliver == "dashboard":
            if self.dashboard_state:
                slot = self.dashboard_state.get_or_create_slot()
                slot.append("assistant", f"{title}\n\n{result_text}", "msg msg-a")
                self.dashboard_state.push_slots_update()
                self.dashboard_state.notify("heartbeat", title, body, meta={"slot": slot.key})
            return

        # ── slack (no thread) → new Slack DM only ──
        if deliver == "slack":
            if self.slack and self._owner_id:
                try:
                    channel = await self.slack.open_dm(self._owner_id)
                    if channel:
                        post = f"💓 *{title}*\n\n{to_slack_mrkdwn(result_text)}"
                        await self.slack.post_message(channel, post)
                except Exception:
                    logger.exception("Heartbeat Slack delivery failed")
            return

        # ── slack:<channel>:<thread_ts> → reply to thread ──
        if deliver.startswith("slack:"):
            parts = deliver.split(":", 2)
            try:
                if self.slack and len(parts) == 3:
                    chan, ts = parts[1], parts[2]
                    post = f"💓 *{title}*\n\n{to_slack_mrkdwn(result_text)}"
                    await self.slack.post_message(chan, post, ts)
                elif self.slack and self._owner_id:
                    chan = await self.slack.open_dm(self._owner_id)
                    if chan:
                        post = f"💓 *{title}*\n\n{to_slack_mrkdwn(result_text)}"
                        await self.slack.post_message(chan, post)
            except Exception:
                logger.exception("Heartbeat Slack delivery failed")
            if self.dashboard_state:
                self.dashboard_state.notify("heartbeat", title, body)
            return

        # ── default: Slack DM + dashboard notification ──
        if self.slack and self._owner_id:
            try:
                channel = await self.slack.open_dm(self._owner_id)
                if channel:
                    post = f"💓 *{title}*\n\n{to_slack_mrkdwn(result_text)}"
                    await self.slack.post_message(channel, post)
            except Exception:
                logger.exception("Heartbeat Slack delivery failed")
        if self.dashboard_state:
            self.dashboard_state.notify("heartbeat", title, body)

    def _init_mcp_discovery(self) -> None:
        """Log configured MCP servers at startup.

        The actual config merge is handled by rebuild_agent_config() which
        runs earlier in __init__. This just logs what's configured for
        debugging visibility.
        """
        try:
            from kiro_claw.mcp_discovery import list_servers  # circular import

            servers = list_servers()
            if servers:
                srv_names = [s.name for s in servers]
                logger.info("Configured MCP servers: %s", ", ".join(srv_names))
            else:
                logger.info("No MCP servers configured")
        except Exception:
            logger.debug("MCP server listing failed", exc_info=True)

    def _init_subagents(self) -> None:
        """Initialize the subagent manager."""

        async def _broadcast_subagent_status(info: SubagentInfo, event: str) -> None:
            """Broadcast subagent status change via WS for per-slot tracking."""
            if not self.dashboard_state:
                return
            try:
                slot = info.parent_session_key.removeprefix("dashboard:")
                agents = (
                    self.subagent_mgr.running_agents_for(info.parent_session_key)
                    if self.subagent_mgr
                    else []
                )
                running = len(agents)
                payload = {
                    "running": running,
                    "id": info.id,
                    "event": event,
                    "slot": slot,
                    "agents": agents,
                }
                logger.info(
                    "📡 subagent_status WS: event=%s slot=%s running=%d agents=%d",
                    event,
                    slot,
                    running,
                    len(agents),
                )
                self.dashboard_state.broadcast_ws("subagent_status", payload)
            except Exception:
                logger.info("Failed to broadcast subagent %s status", info.id, exc_info=True)

        def _retrigger_recovery(slot: "_ChatSlot", parent_key: str) -> None:
            """Drain queued failures into a new recovery _run_chat turn.

            Called from _on_done callbacks after resetting the guard, so
            failures that arrived while the previous recovery was running
            get processed without waiting for user input.
            """
            if slot._recovery_chat_triggered or not slot._pending_subagent_failures:
                return
            if not self.dashboard_state:
                return
            _max_retrigger = 3
            if slot._recovery_retrigger_count >= _max_retrigger:
                logger.warning(
                    "Recovery retrigger cap (%d) reached for %s, dropping %d queued failures",
                    _max_retrigger, parent_key, len(slot._pending_subagent_failures),
                )
                slot._pending_subagent_failures.clear()
                return
            slot._recovery_retrigger_count += 1
            slot._recovery_chat_triggered = True
            from kiro_claw.dashboard.chat import _run_chat

            failures = slot._pending_subagent_failures[:]
            slot._pending_subagent_failures.clear()
            msg = "\n\n".join(failures)
            msg, _ = redact_exfiltration_urls(msg)
            msg, _ = redact_credentials(msg)
            slot.append("user", msg, "msg msg-u auto-go")
            logger.info(
                "Re-triggering recovery _run_chat for %s (%d queued failures)",
                parent_key, len(failures),
            )

            def _done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
                if t.cancelled():
                    logger.warning("Re-triggered recovery cancelled for %s", parent_key)
                    slot._recovery_chat_triggered = False
                    return
                elif t.exception():
                    logger.error(
                        "Re-triggered recovery failed for %s",
                        parent_key, exc_info=t.exception(),
                    )
                slot._recovery_chat_triggered = False
                if slot._pending_subagent_failures:
                    _retrigger_recovery(slot, parent_key)

            _task = asyncio.create_task(
                asyncio.wait_for(
                    _run_chat(self.dashboard_state, slot, msg),
                    timeout=CHAT_TURN_TIMEOUT,
                ),
            )
            slot.task = _task
            self._background_tasks.add(_task)
            _task.add_done_callback(self._background_tasks.discard)
            _task.add_done_callback(_done)

        async def _subagent_done(info: SubagentInfo) -> None:
            async def _inject_with_retry(
                client,
                msg: str,
                parent_key: str,
                label: str,
            ) -> str | None:
                """Retry stream_and_collect up to 3 times on AcpError.

                Cancels any orphaned prompt between attempts so the next
                retry doesn't hit 'Prompt already in progress'.
                """
                for attempt in range(3):
                    try:
                        return await stream_and_collect(client, msg)
                    except PromptBusyExhaustedError:
                        # Provider is dead after exhausting prompt-busy retries.
                        # Reset session + notify, same as TimeoutError path.
                        logger.error(
                            "Subagent %s: provider dead after prompt-busy retries (%s)",
                            info.id,
                            label,
                        )
                        try:
                            assert self.sessions is not None
                            await self.sessions.reset(parent_key)
                        except Exception:
                            logger.debug(
                                "Failed to reset %s after busy exhaustion",
                                parent_key,
                                exc_info=True,
                            )
                        if self.subagent_mgr:
                            self.subagent_mgr.notify_injection_failed(
                                info,
                                reason="provider dead after prompt-busy retries",
                            )
                        return None
                    except AcpProcessDied:
                        logger.warning(
                            "Subagent %s: ACP process died during %s injection",
                            info.id,
                            label,
                        )
                        try:
                            assert self.sessions is not None
                            await self.sessions.reset(parent_key)
                        except Exception:
                            logger.debug(
                                "Failed to reset %s after process death",
                                parent_key,
                                exc_info=True,
                            )
                        if self.subagent_mgr:
                            self.subagent_mgr.notify_injection_failed(
                                info,
                                reason="ACP process died",
                            )
                        return None
                    except AcpError:
                        if attempt == 2:
                            raise
                        logger.warning(
                            "Subagent %s %s injection attempt %d failed, retrying",
                            info.id,
                            label,
                            attempt + 1,
                        )
                        try:
                            assert self.sessions is not None
                            await self.sessions.cancel_current(parent_key)
                        except Exception:
                            logger.debug(
                                "Failed to cancel parent prompt for %s",
                                info.id,
                                exc_info=True,
                            )
                        await asyncio.sleep(2**attempt)
                return None  # unreachable, but satisfies type checker

            await _broadcast_subagent_status(info, "done")
            status = "failed" if info.error else "completed"
            emoji = "❌" if info.error else "✅"
            title = f"Subagent `{info.id}` {emoji}"

            # ── Orchestration guard: track failures (only in orchestrator mode) ──
            parent_key = info.parent_session_key
            guard_msg = ""
            try:
                _is_orchestrator = False
                _slot = None
                if self.dashboard_state and parent_key.startswith("dashboard:"):
                    _slot = self.dashboard_state.get_slot(parent_key.removeprefix("dashboard:"))
                    _is_orchestrator = (
                        _slot is not None and getattr(_slot, "mode", "") == "orchestrator"
                    )
                if _slot is not None and _is_orchestrator:
                    from kiro_claw.context_management import (
                        MAX_STAGE_ESCALATIONS,
                        MAX_STAGE_ROUNDS,
                        OrchestrationTracker,
                    )

                    if not getattr(_slot, "_orch_tracker", None):
                        _slot._orch_tracker = OrchestrationTracker()
                    tracker = _slot._orch_tracker
                    if tracker.stopped:
                        logger.info("Orchestration stopped, ignoring subagent result %s", info.id)
                        return
                    task_key = info.task[:80]
                    if info.error:
                        if tracker.record_failure(task_key):
                            guard_msg = (
                                f"\n\n⚠️ [SYSTEM] Task '{task_key}' has failed "
                                f"{tracker.failure_count(task_key)} times. "
                                "You MUST ask the user for guidance before retrying."
                            )
                    else:
                        tracker.record_success(task_key)
                    # Track spawn rounds — count each completed batch as a round
                    pending = (
                        self.subagent_mgr.running_agents_for(parent_key)
                        if self.subagent_mgr
                        else []
                    )
                    if not pending:
                        # All agents done → one round completed
                        stage = tracker.current_stage
                        if tracker.record_round(stage):
                            if tracker.is_force_failed(stage):
                                guard_msg += (
                                    f"\n\n🛑 [SYSTEM] Stage {stage} has failed after "
                                    f"{MAX_STAGE_ESCALATIONS} escalations ({tracker.round_count(stage)} rounds). "
                                    "You MUST stop this stage and report the failure to the user. "
                                    "Do NOT retry or spawn more agents."
                                )
                            else:
                                guard_msg += (
                                    f"\n\n⚠️ [SYSTEM] Stage {stage} has used "
                                    f"{tracker.round_count(stage)}/{MAX_STAGE_ROUNDS} spawn rounds. "
                                    "You MUST ask the user for guidance before spawning more."
                                )
            except Exception:
                logger.warning("Orchestration guard failed for %s", info.id, exc_info=True)
            # Chat mode: pass info.result through (subagent.py already applied
            # agent.completion_keep + completion_keep_chars).
            # Orchestrator mode: 200-word summary + disk path so LLM can read full file.
            result_path = info.result_path or ""
            if info.error:
                detail = f"Error: {info.error}"
            elif _is_orchestrator and result_path:
                from kiro_claw.context_management import RESULT_SUMMARY_WORDS

                words = (info.result or "").split()
                half = RESULT_SUMMARY_WORDS // 2
                if len(words) <= RESULT_SUMMARY_WORDS:
                    summary = " ".join(words)
                else:
                    summary = (
                        " ".join(words[:half])
                        + "\n[...middle truncated...]\n"
                        + " ".join(words[-half:])
                    )
                import os

                size = ""
                try:
                    size = f", {os.path.getsize(result_path)} bytes"
                except OSError:
                    pass
                detail = f"Full result: {result_path} ({size.lstrip(', ')})\nSummary (first+last {half} words):\n{summary}\n\nUse the read tool on the path above if the summary is not enough."
            else:
                detail = info.result or "_No response._"
            detail, _ = redact_exfiltration_urls(detail)
            detail, _ = redact_credentials(detail)
            task_text, _ = redact_exfiltration_urls(info.task)
            task_text, _ = redact_credentials(task_text)
            task_text = task_text[:100]
            body = f"{task_text}\n\n{detail}"
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)

            announce = (
                f"[Subagent completion event]\n"
                f"Agent `{info.id}`"
                f"{f' ({info.agent})' if info.agent else ''}"
                f" {status} {emoji}\n"
                f"Task: {task_text}\n\n"
                f"{detail}"
                f"{guard_msg}"
            )

            parent_key = info.parent_session_key

            # ── Route completion back to the originating session ──
            # Dashboard → dashboard only (no Slack)
            # Slack → Slack thread + dashboard notification
            # Cron/no parent → dashboard notification only

            if parent_key.startswith("dashboard:") and self.dashboard_state:
                # Dashboard session — route subagent result through _run_chat
                # for full streaming, tool call visibility, and proper lifecycle.
                _slot_name = parent_key.removeprefix("dashboard:")
                _injection_slot = self.dashboard_state.get_slot(_slot_name)

                # Redact LLM-generated output before any external surface
                announce, _ = redact_exfiltration_urls(announce)
                announce, _ = redact_credentials(announce)
                body, _ = redact_exfiltration_urls(body)
                body, _ = redact_credentials(body)

                if _injection_slot:

                    if _injection_slot.running:
                        # Slot is busy — wait for current turn to finish,
                        # then inject. No visible queue card.
                        _current = _injection_slot.task
                        if _current is not None:
                            try:
                                await asyncio.wait_for(
                                    asyncio.shield(_current),
                                    timeout=INJECTION_TIMEOUT,
                                )
                            except asyncio.TimeoutError:
                                pass  # Timed out waiting — slot still busy, will be queued below
                            except asyncio.CancelledError:
                                raise  # Don't swallow cancellation of this coroutine
                            except Exception:
                                pass  # Task failed — slot is now idle

                        # Re-check: another injection may have claimed the slot
                        # during the await above.
                        if _injection_slot.running:
                            logger.info(
                                "Subagent %s: slot %s claimed by another injection, queuing",
                                info.id, _slot_name,
                            )
                            # Bounded by CHAT_TURN_TIMEOUT (~600s): _run_chat's
                            # finally block drains slot._queue on any exit path.
                            _injection_slot.queue_append(announce)
                            self.dashboard_state.push_slots_update()
                            logger.info("Subagent %s → queued in %s", info.id, _slot_name)
                            return

                    # Slot is idle — start _run_chat.
                    _task = asyncio.create_task(
                        asyncio.wait_for(
                            _run_chat(self.dashboard_state, _injection_slot, announce),
                            timeout=CHAT_TURN_TIMEOUT,
                        )
                    )
                    _injection_slot.task = _task
                    self.dashboard_state._background_tasks.add(_task)
                    _task.add_done_callback(
                        self.dashboard_state._background_tasks.discard
                    )

                    def _on_inject_done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
                        if _injection_slot.task is t:
                            _injection_slot.task = None
                        if not t.cancelled() and t.exception():
                            logger.error("Subagent injection _run_chat failed: %s", t.exception())
                            if self.subagent_mgr:
                                _reason = str(t.exception())
                                _reason, _ = redact_exfiltration_urls(_reason)
                                _reason, _ = redact_credentials(_reason)
                                self.subagent_mgr.notify_injection_failed(
                                    info, reason=_reason,
                                )

                    _task.add_done_callback(_on_inject_done)
                    self.dashboard_state.push_slots_update()
                    logger.info("Subagent %s → _run_chat in %s", info.id, _slot_name)
                else:
                    logger.info(
                        "Subagent %s: parent slot %s gone, notification only",
                        info.id, _slot_name,
                    )
                    # Only notify when slot is gone — active slots already show
                    # results in the Activity panel and chat.
                    self.dashboard_state.notify(
                        "subagent",
                        title,
                        body,
                        meta=self._notif_meta(parent_key),
                    )
                return

            if parent_key and not parent_key.startswith(("cron:", "subagent:")):
                # Slack session — inject silently into ACP session (no visible Slack message).
                # Retry up to _MAX_INJECT_ATTEMPTS times on timeout.
                assert self.sessions is not None
                _injected = False
                _slack_failure_reasons: list[str] = []
                _sleep_before_retry = False
                for _attempt in range(1, _MAX_INJECT_ATTEMPTS + 1):
                    if _sleep_before_retry:
                        await asyncio.sleep(2)
                        _sleep_before_retry = False
                    _acquired = False
                    _footer_client = None
                    try:
                        logger.debug(
                            "Subagent %s: Slack injection attempt %d/%d into %s",
                            info.id, _attempt, _MAX_INJECT_ATTEMPTS, parent_key,
                        )
                        client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
                        _acquired = True
                        _footer_client = client
                        _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                        if self.ctx_builder:
                            msg, _ = self.ctx_builder.build_message(announce, is_new, parent_key, provider_type=_provider)
                        else:
                            msg = announce
                        response = await asyncio.wait_for(
                            _inject_with_retry(client, msg, parent_key, "Slack"),
                            timeout=INJECTION_TIMEOUT,
                        )
                        _injected = True  # LLM processed result; Slack posting is best-effort

                        # Post only the LLM's synthesized response to Slack
                        try:
                            if response and self.slack and self._owner_id:
                                channel = (
                                    self.sessions.get_channel(parent_key) if self.sessions else None
                                ) or await self.slack.open_dm(self._owner_id)
                                if channel:
                                    reply_text, _ = redact_exfiltration_urls(to_slack_mrkdwn(response))
                                    reply_text, _ = redact_credentials(reply_text)
                                    for part in split_message(reply_text):
                                        await self.slack.post_message(channel, part, parent_key)
                                    try:
                                        from kiro_claw.slack.handler import build_timing_footer

                                        elapsed = info.elapsed if info.elapsed > 0 else (time.monotonic() - info.started)
                                        footer_blocks, footer_text = build_timing_footer(
                                            elapsed, _footer_client,
                                        )
                                        await self.slack.post_blocks(
                                            channel, footer_blocks, footer_text, parent_key,
                                        )
                                    except Exception:
                                        logger.debug("Failed to post timing footer for %s", parent_key, exc_info=True)
                        except Exception:
                            logger.exception(
                                "Subagent %s: Slack posting failed (injection succeeded)", info.id,
                            )
                        logger.info("Subagent %s → Slack session %s", info.id, parent_key)
                        break
                    except asyncio.TimeoutError:
                        _slack_failure_reasons.append(
                            f"attempt {_attempt} timed out after {int(INJECTION_TIMEOUT)}s"
                        )
                        logger.warning(
                            "Subagent %s: Slack injection attempt %d/%d timed out after %.0fs",
                            info.id, _attempt, _MAX_INJECT_ATTEMPTS, INJECTION_TIMEOUT,
                        )
                        if _acquired:
                            try:
                                await self.sessions.reset(parent_key)
                            except Exception:
                                logger.debug(
                                    "Failed to reset %s after Slack injection timeout",
                                    parent_key, exc_info=True,
                                )
                        if _attempt < _MAX_INJECT_ATTEMPTS:
                            _sleep_before_retry = True
                    except Exception as exc:
                        _slack_failure_reasons.append(f"attempt {_attempt} failed: {exc}")
                        logger.exception("Subagent %s Slack injection failed", info.id)
                        break
                    finally:
                        if _acquired:
                            try:
                                await self.sessions.cancel_current(parent_key)
                            except Exception:
                                logger.debug(
                                    "Failed to cancel parent prompt for %s",
                                    info.id, exc_info=True,
                                )
                            try:
                                self.sessions.release(parent_key)
                            except Exception:
                                logger.exception("Failed to release session %s", parent_key)

                if not _injected:
                    _last_failure_reason = "; ".join(_slack_failure_reasons)
                    _last_failure_reason, _ = redact_exfiltration_urls(_last_failure_reason)
                    _last_failure_reason, _ = redact_credentials(_last_failure_reason)
                    logger.error(
                        "Subagent %s: all %d Slack injection attempts failed: %s",
                        info.id, _MAX_INJECT_ATTEMPTS, _last_failure_reason,
                    )
                    if self.subagent_mgr:
                        self.subagent_mgr.notify_injection_failed(
                            info, reason=_last_failure_reason,
                        )
                # Dashboard notification
                if self.dashboard_state:
                    self.dashboard_state.notify(
                        "subagent",
                        title,
                        body,
                        meta=self._notif_meta(parent_key),
                    )
                return

            # Cron parent — inject result back into the cron session.
            # Track pending injections to avoid resetting the session while
            # other subagents are queued behind the per-session semaphore.
            if parent_key.startswith("cron:"):
                self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 0) + 1
                assert self.sessions is not None
                acquired = False
                cron_response: str | None = None
                try:
                    client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
                    acquired = True
                    _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                    if self.ctx_builder:
                        msg, _ = self.ctx_builder.build_message(announce, is_new, parent_key, provider_type=_provider)
                    else:
                        msg = announce
                    cron_response = await asyncio.wait_for(
                        _inject_with_retry(client, msg, parent_key, "cron"),
                        timeout=INJECTION_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "Subagent %s: cron injection timed out after %.0fs",
                        info.id,
                        INJECTION_TIMEOUT,
                    )
                    try:
                        await self.sessions.reset(parent_key)
                    except Exception:
                        logger.debug(
                            "Failed to reset %s after cron injection timeout",
                            parent_key,
                            exc_info=True,
                        )
                    if self.subagent_mgr:
                        self.subagent_mgr.notify_injection_failed(
                            info,
                            reason=f"injection timed out after {int(INJECTION_TIMEOUT)}s",
                        )
                except Exception:
                    logger.exception("Subagent %s cron injection failed", info.id)
                finally:
                    if acquired:
                        try:
                            await self.sessions.cancel_current(parent_key)
                        except Exception:
                            logger.debug(
                                "Failed to cancel parent prompt for cron %s", info.id, exc_info=True
                            )
                        try:
                            self.sessions.release(parent_key)
                        except Exception:
                            logger.exception("Failed to release session %s", parent_key)
                    self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 1) - 1
                    if self._cron_injecting[parent_key] <= 0:
                        self._cron_injecting.pop(parent_key, None)
                if cron_response:
                    cron_response, _ = redact_exfiltration_urls(cron_response)
                    cron_response, _ = redact_credentials(cron_response)
                    body = f"{body}\n\n{cron_response}"
                    logger.info("Subagent %s → cron session %s", info.id, parent_key)
                # Reset only when no subagents running AND no injections pending
                still_running = self.subagent_mgr and any(
                    a.parent_session_key == parent_key and a.id != info.id
                    for a in self.subagent_mgr.running
                )
                still_injecting = self._cron_injecting.get(parent_key, 0) > 0
                if not still_running and not still_injecting:
                    try:
                        await self.sessions.reset(parent_key)
                        logger.info(
                            "Cron session %s: last subagent done, session reset", parent_key
                        )
                        # Mesh-1026: reset succeeded → reaper no longer needs the
                        # registered ephemeral key. Clear inside try so a failed
                        # reset leaves the key registered (ephemeral session may
                        # still be alive — reaper must be able to target it).
                        # parent_key is "cron:{job_id}" (persistent) or
                        # "cron:{job_id}:{run_id}" (ephemeral); job_id is the
                        # second colon-separated segment in both cases.
                        cron_svc = getattr(self, "cron_svc", None)
                        if cron_svc is not None:
                            parts = parent_key.split(":", 2)
                            if len(parts) >= 2:
                                cron_svc.clear_active_session_key(parts[1])
                    except Exception:
                        logger.exception(
                            "Cron session %s: reset failed after last subagent", parent_key
                        )

            # Dashboard notification
            if self.dashboard_state and not info.silent:
                self.dashboard_state.notify(
                    "subagent",
                    title,
                    body,
                    meta=self._notif_meta(parent_key),
                )
            if not parent_key.startswith("cron:"):
                logger.info("Subagent %s → notification only (parent=%s)", info.id, parent_key)

        assert self.sessions is not None
        assert self.ctx_builder is not None

        def _is_yolo() -> bool:
            return safety_override().is_active()

        def _spawn_slot_resolver(request_id: str) -> str:
            """Resolve slot from spawn request_id (spawn:{agent_id})."""
            agent_id = request_id.removeprefix("spawn:")
            info = self.subagent_mgr.get(agent_id) if self.subagent_mgr is not None else None
            slot = (
                info.parent_session_key.removeprefix("dashboard:")
                if info and info.parent_session_key
                else ""
            )
            logger.info(
                "_spawn_slot_resolver: rid=%s agent_id=%s info=%s slot=%s",
                request_id,
                agent_id,
                info is not None,
                slot,
            )
            return slot

        _approve_subagent = self._interactive_approval(
            "subagent", slot_resolver=_spawn_slot_resolver
        )

        async def _spawn_approve(
            request_id: str, description: str, parent_session_key: str = ""
        ) -> bool:
            event = LLMEvent(kind="permission_request", request_id=request_id, title=description)
            return await _approve_subagent(event, parent_session_key)

        async def _subagent_event(etype: str, info: SubagentInfo, extra: dict) -> None:
            if not self.dashboard_state:
                return
            slot_name = info.parent_session_key.removeprefix("dashboard:")
            base = {"id": info.id, "slot": slot_name}
            if etype == "subagent_injection_failed":
                # Show error in UI + queue for LLM context on next turn.
                slot = self.dashboard_state.get_slot(slot_name)
                if slot:
                    task_preview, _ = redact_exfiltration_urls((info.task or "")[:100])
                    task_preview, _ = redact_credentials(task_preview)
                    error_text, _ = redact_exfiltration_urls(extra.get("error", "timed out"))
                    error_text, _ = redact_credentials(error_text)
                    slot.append(
                        "assistant",
                        f"[Subagent completion event]\n"
                        f"Agent `{info.id}` ❌\n"
                        f"Task: {task_preview}\n\n"
                        f"Error: {error_text}\n"
                        f"⚠️ Result delivery timed out — the subagent finished but "
                        f"its result could not be injected into this session.",
                        "msg msg-a",
                    )
                    # Queue failure for LLM context drain
                    failure_msg = extra.get("failure_msg", "")
                    if failure_msg:
                        failure_msg, _ = redact_exfiltration_urls(failure_msg)
                        failure_msg, _ = redact_credentials(failure_msg)
                        slot._pending_subagent_failures.append(failure_msg)
                    self.dashboard_state.push_slots_update()
                    logger.warning(
                        "Injected timeout error for subagent %s into slot %s", info.id, slot_name
                    )
                self.dashboard_state.broadcast_ws(etype, {**base, **extra})
            elif etype == "subagent_chunk":
                # Heavy data — only to subscribed clients
                self.dashboard_state.broadcast_ws_subagent_subscribers(etype, {**base, **extra})
            else:
                # Lightweight status events — broadcast to all
                self.dashboard_state.broadcast_ws(etype, {**base, **extra})

        self.subagent_mgr = SubagentManager(
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
            on_done=_subagent_done,
            max_concurrent=self._cfg.agent.max_subagents,
            default_turn_limit=self._cfg.agent.subagent_max_turns,
            default_timeout=self._cfg.agent.subagent_timeout_secs,
            on_tool_approval=_approve_subagent,
            on_spawn_approval=_spawn_approve,
            is_yolo=_is_yolo,
            on_event=_subagent_event,
            completion_keep=self._cfg.agent.completion_keep,
            completion_keep_chars=self._cfg.agent.completion_keep_chars,
        )
        self.subagent_mgr.start_reaper()

    def _init_task_runner(self) -> None:
        """Initialize the task runner."""

        async def _task_notify(title: str, body: str, task_id: str = "") -> None:
            if self.dashboard_state:
                body, _ = redact_exfiltration_urls(body)
                body, _ = redact_credentials(body)
                title, _ = redact_exfiltration_urls(title)
                title, _ = redact_credentials(title)
                meta = {"task_id": task_id} if task_id else None
                self.dashboard_state.notify("taskrunner", title, body, meta=meta)
                self.dashboard_state.push_refresh("taskrunner")
            # Send approval-related notifications to Slack DM so user knows even when away.
            # Match on specific title patterns from task_executor, not broad keywords
            # (avoids false positives like "Investigating gateway error").
            if "requires approval" in title.lower() or "denied" in title.lower():
                try:
                    if self.slack and self._owner_id:
                        safe_t = redact_credentials(redact_exfiltration_urls(title)[0])[0]
                        safe_b = redact_credentials(redact_exfiltration_urls(body)[0])[0]
                        ch = await self.slack.open_dm(self._owner_id)
                        if ch:
                            await self.slack.post_message(ch, f"*{safe_t}*\n{safe_b}")
                except Exception as exc:
                    logger.warning("Failed to send approval notification to Slack DM: %s", exc)

        assert self.sessions is not None
        self.task_runner = TaskRunner(
            sessions=self.sessions,
            context_builder=self.ctx_builder,
            on_notify=_task_notify,
            work_dir=_session_work_dir("taskrunner:main"),
            conversation_log=self.conv_log,
            consolidator=self.consolidator,
            lesson_store=LessonStore(),
            max_parallel_steps=self._cfg.taskrunner.max_parallel_steps,
        )
        self.task_runner._on_tool_approval = self._interactive_approval("taskrunner")

        # Task-level approval handler: blocks until user approves via dashboard UI
        async def _task_approval(task: "Task") -> bool:
            if not self.dashboard_state:
                logger.warning("No dashboard state — denying task %d approval", task.index)
                sel().log_api_access(
                    caller="taskrunner",
                    operation="task.force_approval",
                    outcome="denied",
                    source="gateway",
                    resources=f"task-{task.index}",
                    error="no dashboard state available",
                )
                return False
            clean_title, _ = redact_exfiltration_urls(task.title or "")
            clean_title, _ = redact_credentials(clean_title)
            approval_id = f"task-gate-{task.index}-{uuid.uuid4().hex[:8]}"
            result = await self.dashboard_state.request_approval(
                approval_id=approval_id,
                source="taskrunner",
                tool=f"Task {task.index}: {clean_title}",
                tool_purpose="Task requires manual approval before execution",
            )
            sel().log_api_access(
                caller="taskrunner",
                operation="task.force_approval",
                outcome="approved" if result else "denied",
                source="dashboard",
                resources=f"task-{task.index}",
            )
            return result

        self.task_runner._on_approval = _task_approval

    async def _init_dashboard(self) -> None:
        """Start the dashboard web server."""
        assert self.sessions is not None
        assert self.cron_svc is not None

        configured_host, dashboard_port = parse_dashboard_url(self._cfg.dashboard.url)
        # --port override (literal int or "auto" for ephemeral)
        if self._port_override == "auto":
            dashboard_port = 0
        elif self._port_override is not None:
            dashboard_port = int(self._port_override)
        self._dashboard_port = dashboard_port
        self._configured_host = configured_host
        self._local_only = is_local_only(configured_host, self._slack_enabled)
        self._dashboard_runner, self.dashboard_state = await start_dashboard(
            sessions=self.sessions,
            crons=self.cron_svc,
            lessons=LessonStore(),
            port=dashboard_port,
            subagents=self.subagent_mgr,
            context_builder=self.ctx_builder,
            conversation_log=self.conv_log,
            consolidator=self.consolidator,
            task_runner=self.task_runner,
            slack_connected=self._slack_enabled,
            local_only=self._local_only,
            configured_host=configured_host,
            dashboard_url=self._cfg.dashboard.url,
            slack_client=self.slack,
            owner_id=self._owner_id,
        )
        # When --port auto was requested, read the OS-assigned ephemeral port
        # back from the runner so subsequent URL building and the READY line
        # use the real bound port.
        if dashboard_port == 0 and self._dashboard_runner is not None:
            addresses = self._dashboard_runner.addresses
            if addresses:
                self._dashboard_port = addresses[0][1]
        if self.slack and self.dashboard_state:
            self.dashboard_state.slack_client = self.slack
        if self.dashboard_state:
            self.dashboard_state.no_crons = self._no_crons  # dashboard mode

    async def _init_api_server(self) -> None:
        """Start a minimal API-only HTTP server for MCP tool transport."""
        from kiro_claw.dashboard import start_api_server

        assert self.sessions is not None
        assert self.cron_svc is not None
        configured_host, dashboard_port = parse_dashboard_url(self._cfg.dashboard.url)
        # --port override (literal int or "auto" for ephemeral)
        if self._port_override == "auto":
            dashboard_port = 0
        elif self._port_override is not None:
            dashboard_port = int(self._port_override)
        self._dashboard_port = dashboard_port
        self._configured_host = configured_host
        self._local_only = is_local_only(configured_host, self._slack_enabled)
        self._dashboard_runner, self.dashboard_state = await start_api_server(
            sessions=self.sessions,
            crons=self.cron_svc,
            lessons=LessonStore(),
            port=dashboard_port,
            subagents=self.subagent_mgr,
            task_runner=self.task_runner,
            slack_client=self.slack,
            owner_id=self._owner_id,
        )
        if dashboard_port == 0 and self._dashboard_runner is not None:
            addresses = self._dashboard_runner.addresses
            if addresses:
                self._dashboard_port = addresses[0][1]
        if self.dashboard_state:
            self.dashboard_state.no_crons = self._no_crons  # API-only mode

    async def _start_ollama(self) -> None:
        """Start Ollama server, pull model, and set embed_fn on vector memory.

        Wires embed_fn_factory unconditionally so that if Ollama is unavailable
        at gateway boot but starts later, _try_embed() will lazily rebind on
        next memory write — no gateway restart required.
        """
        # Capture config locals so the factory closure stays stable.
        embed_url = self._cfg.memory.embedding_url
        embed_model = self._cfg.memory.embedding_model
        embed_timeout = self._cfg.memory.embedding_timeout_secs
        embed_auth = self._cfg.memory.embedding_auth

        if not self._cfg.memory.embedding_managed:
            # External Ollama (e.g. SSH-forwarded or API Gateway). Reuse
            # _validate_url so the safety gate, credential-in-URL check, and
            # audit logging stay consistent with the managed EmbeddingClient path.
            try:
                _validate_url(
                    embed_url,
                    allow_remote=self._cfg.memory.allow_remote_embedding,
                )
            except ValueError as e:
                logger.error(
                    "External embedding URL %s rejected: %s — embeddings disabled",
                    embed_url,
                    e,
                )
                return
            # SEL audit: external network endpoint authorized for embedding data.
            # _validate_url already emits SEL on rejection paths; this matches
            # the success-path audit requirement for security-relevant config.
            sel().log_tool_invocation(
                session_key="gateway_startup",
                tool_name="_start_ollama_unmanaged",
                outcome="external_endpoint_configured",
                resources=embed_url,
                metadata={"auth": embed_auth},
            )
            print("🐾 Using external Ollama at", embed_url)
            # Wire factory for lazy rebind on external Ollama too — covers
            # transient SSH tunnel / API Gateway outages mid-session.
            self.vector_memory.embed_fn_factory = lambda: make_sync_embed_fn(
                embed_url,
                timeout=embed_timeout,
                model=embed_model,
                auth=embed_auth,
            )
            self.vector_memory.embed_fn = make_sync_embed_fn(
                embed_url,
                timeout=embed_timeout,
                model=embed_model,
                auth=embed_auth,
            )
            logger.info("External Ollama configured, embeddings enabled (unmanaged)")
            return

        mgr = OllamaManager(embed_url, model=embed_model)
        self._ollama_manager = mgr
        print("🐾 Starting Ollama embedding server…")
        # Wire factory before ensure_running() so lazy rebind survives boot-time failure.
        self.vector_memory.embed_fn_factory = lambda: make_sync_embed_fn(
            embed_url,
            timeout=embed_timeout,
            model=embed_model,
            auth=embed_auth,
        )
        if await mgr.ensure_running():
            self.vector_memory.embed_fn = make_sync_embed_fn(
                embed_url,
                timeout=embed_timeout,
                model=embed_model,
                auth=embed_auth,
            )
            logger.info("Ollama server started, embeddings enabled")
        else:
            logger.warning(
                "Ollama not ready at boot — embeddings will lazily reconnect when available"
            )

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """Graceful cleanup of all services."""
        # Save all active chat slots to history before shutdown
        if self.dashboard_state:
            from kiro_claw.dashboard.chat import save_all_slots_to_history

            save_all_slots_to_history(self.dashboard_state)
            self.dashboard_state.file_indexes.stop_all()

        # Cancel in-flight handler tasks
        for t in list(self._handler_tasks):
            t.cancel()
        if self._handler_tasks:
            await asyncio.gather(*self._handler_tasks, return_exceptions=True)

        # Stop services
        if self.cron_svc:
            await self.cron_svc.stop()
        if self.heartbeat_svc:
            self.heartbeat_svc.stop()

        # Kill all ACP processes and close connections
        cleanup_tasks: list = []
        if self.subagent_mgr:
            cleanup_tasks.append(self.subagent_mgr.cancel_all())
        if self.sessions:
            cleanup_tasks.append(self.sessions.close_all())
        if self._dashboard_runner:
            # Close WS connections first so handlers exit promptly
            if self.dashboard_state:
                await self.dashboard_state.close_all_ws()
            cleanup_tasks.append(self._dashboard_runner.cleanup())
        if self._socket_client:
            cleanup_tasks.append(asyncio.wait_for(self._socket_client.close(), timeout=1.0))
        # Stop Ollama server if we started it
        if self._ollama_manager is not None:
            if isinstance(self._ollama_manager, OllamaManager):
                cleanup_tasks.append(self._ollama_manager.stop())

        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Auto-update
    # ------------------------------------------------------------------

    async def _check_for_updates(self) -> None:
        """Blocking update check — auto-applies if enabled, otherwise notifies."""
        try:
            from kiro_claw.dashboard.handlers import _do_update_check, _update_info

            await _do_update_check()
            if _update_info.get("available"):
                logger.info("Updates available from remote")
                from kiro_claw.config import KiroClawConfig

                cfg = KiroClawConfig.load()
                if cfg.auto_update:
                    logger.info("Auto-update enabled — applying update")
                    await self._auto_apply_update()
                elif self.dashboard_state:
                    self.dashboard_state.push_refresh("update_available")
            else:
                print("🐾 Already on latest version")
        except Exception:
            logger.debug("Update check failed", exc_info=True)

    async def _auto_apply_update(self) -> None:
        """Auto-apply: fetch, reset to remote, rebuild frontend, pip install, restart.

        Uses ``git fetch`` + ``git reset --hard`` instead of ``git pull``
        so local tracked-file edits never cause merge conflicts.
        Untracked files (task specs, notes) are untouched by reset.

        The public OSS flow is the same one used by ``kiroclaw update`` and the
        dashboard update endpoint: git reset to origin → build + stage the
        in-tree ``website/`` frontend → ``pip install -e .`` → ``os.execv``
        restart. The optional ``kiro-cli`` backend is updated only when present.
        """
        proj = os.environ.get("KIROCLAW_PROJECT_DIR", "")
        if not proj:
            return
        try:
            # Detect current branch
            branch_proc = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            branch_out, _ = await asyncio.wait_for(branch_proc.communicate(), timeout=10)
            if branch_proc.returncode != 0:
                logger.error("Auto-update: could not determine current branch")
                return
            branch = branch_out.strip().decode() if branch_out else ""
            if not branch or branch == "HEAD":
                branch = "mainline"

            # Only auto-update on mainline — beta/feature branches need manual update
            if branch != "mainline":
                logger.debug("Auto-update: skipping — on branch %s, not mainline", branch)
                return

            if self.dashboard_state:
                self.dashboard_state.push_update_progress("pulling", "Fetching latest changes…")

            fetch = await asyncio.create_subprocess_exec(
                "git",
                "fetch",
                "origin",
                branch,
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(fetch.communicate(), timeout=60)

            if fetch.returncode != 0:
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                return

            # Check if there are actually new commits
            diff_proc = await asyncio.create_subprocess_exec(
                "git",
                "diff",
                "HEAD",
                f"origin/{branch}",
                "--quiet",
                cwd=proj,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(diff_proc.wait(), timeout=10)
            if diff_proc.returncode == 0:
                # No diff — already up to date
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                return

            # Warn if local tracked-file edits will be discarded
            status_proc = await asyncio.create_subprocess_exec(
                "git",
                "status",
                "--porcelain",
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            status_out, _ = await asyncio.wait_for(status_proc.communicate(), timeout=10)
            if status_out and status_out.strip():
                tracked = [
                    ln
                    for ln in status_out.decode(errors="replace").splitlines()
                    if not ln.startswith("??")
                ]
                if tracked:
                    logger.warning("Auto-update: discarding local tracked-file changes in %s", proj)

            # Hard reset to remote — discards local tracked-file edits,
            # untracked files (task specs, notes) are preserved.
            reset = await asyncio.create_subprocess_exec(
                "git",
                "reset",
                "--hard",
                f"origin/{branch}",
                cwd=proj,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(reset.wait(), timeout=10)
            if reset.returncode != 0:
                logger.error("Auto-update: git reset --hard failed (rc=%d)", reset.returncode)
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                return
            logger.info("Auto-update: reset to origin/%s, rebuilding", branch)

            # Update the optional kiro-cli backend if present.
            if shutil.which("kiro-cli"):
                try:
                    kiro_update = await asyncio.create_subprocess_exec(
                        "kiro-cli",
                        "update",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(kiro_update.wait(), timeout=120)
                except Exception:
                    logger.debug("Auto-update: kiro-cli update failed (non-fatal)")

            # Build + stage the in-tree website/ frontend so the dashboard
            # serves the latest bundle. Graceful no-op if no website/ or npm.
            if self.dashboard_state:
                self.dashboard_state.push_update_progress("building", "Building frontend…")
            await build_frontend_async(
                proj,
                push_progress=self.dashboard_state.push_update_progress if self.dashboard_state else None,
            )

            if self.dashboard_state:
                self.dashboard_state.push_update_progress("building", "Rebuilding package…")
            # pip install -e . picks up new Python deps / entry points.
            pip_install = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "pip",
                "install",
                "-e",
                ".",
                "--quiet",
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            pip_out, pip_err = await asyncio.wait_for(
                pip_install.communicate(), timeout=400
            )
            if pip_install.returncode != 0:
                err_text = pip_err.decode(errors="replace")[:500]
                logger.error(
                    "Auto-update: pip install failed (rc=%d): %s",
                    pip_install.returncode,
                    err_text,
                )
                if self.dashboard_state:
                    self.dashboard_state.push_update_progress(
                        "building",
                        "Dependency install hit an error — repairing core deps…",
                    )
                # The source tree is already on the new version (git reset ran
                # first), so booting without the core deps crashes every
                # command (e.g. cc_agent's `import yaml`). Install the core
                # public deps directly so the gateway still boots and can
                # self-heal — this can't fully fail the way `pip install -e .`
                # can, because these resolve from public PyPI with no
                # internal-index dependency.
                core_deps = [pip for _mod, pip in self._REQUIRED_DEPS]
                fallback = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install", "--quiet", *core_deps,
                    cwd=proj,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _fb_out, fb_err = await asyncio.wait_for(
                    fallback.communicate(), timeout=300
                )
                if fallback.returncode == 0:
                    logger.info(
                        "Auto-update: core deps repaired after pip failure (%s)",
                        ", ".join(core_deps),
                    )
                else:
                    logger.error(
                        "Auto-update: core dep repair also failed (rc=%d): %s",
                        fallback.returncode,
                        fb_err.decode(errors="replace")[:300],
                    )

            logger.info("Auto-update: rebuild complete, restarting")
            # Re-read version from rebuilt package
            importlib.reload(kiro_claw)
            new_ver = kiro_claw.__version__
            print(f"🐾 New version {new_ver} available — auto-updating and restarting…")
            if self.dashboard_state:
                self.dashboard_state.push_update_progress("restarting", "Restarting server…")
                from kiro_claw.dashboard.chat import save_all_slots_to_history

                save_all_slots_to_history(self.dashboard_state)
            if self.sessions:
                await self.sessions.close_all()
            # Use -m kiro_claw rather than sys.argv[0] so the restart resolves
            # the freshly reinstalled entry point regardless of how the
            # original process was launched.
            os.execv(sys.executable, [sys.executable, "-m", "kiro_claw"] + sys.argv[1:])
        except Exception:
            logger.warning("Auto-update failed", exc_info=True)

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Start all services and block until shutdown signal."""
        # Raise FD limit — each kiro-cli session uses ~6 FDs (3 pipes)
        # plus MCP server subprocesses. Default macOS limit (256) is too low.
        import resource

        try:
            soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
            target = min(hard, 10240)
            if soft < target:
                resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
                logger.info("Raised FD limit: %d → %d", soft, target)
        except Exception:
            pass

        # Clean up orphaned kiro-cli processes from previous runs
        from kiro_claw.session import cleanup_orphaned_sessions

        cleanup_orphaned_sessions()

        # ── Initialise all services ──
        from kiro_claw.slack.events import SeenCache, init_socket_mode
        from kiro_claw.slack.interactions import init as init_interactions

        seen = SeenCache()
        self._init_services()

        # Start Ollama server if embeddings are configured
        if self._cfg.memory.embedding_provider == "ollama":
            await self._start_ollama()

        await self._init_cron()
        await self._init_heartbeat()
        self._init_mcp_discovery()
        self._init_subagents()
        self._init_task_runner()
        if not self._no_dashboard:
            await self._init_dashboard()
        else:
            await self._init_api_server()

        # Emit machine-readable READY line for test harnesses (--json-ready).
        # Printed BEFORE bg_session and other startup chatter so the harness
        # can read it deterministically with a single readline() in the
        # KIROCLAW_READY: prefix matcher.
        if self._json_ready:
            ready_token = generate_token("local-startup", ttl_seconds=MAX_SESSION_TTL_SECS)
            ready_payload = {
                "port": self._dashboard_port,
                "token": ready_token,
                "pid": os.getpid(),
                "home": os.environ.get("KIROCLAW_HOME", str(Path.home() / ".kiroclaw")),
            }
            print(f"KIROCLAW_READY:{json.dumps(ready_payload)}", flush=True)

        # AutoNudge must run after dashboard init — _fire callback dereferences
        # self.dashboard_state. In --no-dashboard mode the guard inside _fire
        # early-returns so persisted loops are harmless until a dashboard
        # process takes over.
        await self._init_autonudge()

        # Wire up event routing and interactive handlers
        init_interactions(self)
        init_socket_mode(self, seen)

        # Check for updates before printing URLs
        print("🐾 Checking for updates…")
        await self._check_for_updates()

        # ── Signal handlers ──
        loop = asyncio.get_running_loop()
        _shutting_down = False

        def _on_signal(*_args: object) -> None:
            nonlocal _shutting_down
            if _shutting_down:
                print("\n🐾 Force exit!")
                cleanup_orphaned_sessions()
                os._exit(0)
            _shutting_down = True
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal)
            except (RuntimeError, ValueError):
                # Not in main thread (e.g. pytest-xdist worker) — skip.
                pass

        # Wait for MCP probe to finish before warming sessions —
        # kiro-cli reads MCP config at spawn time, so sessions must
        # start AFTER the probe has synced all servers to mcp.json.
        from kiro_claw.dashboard.handlers import _bg_mcp_probe

        print("🐾 Probing MCP servers…")
        try:
            from kiro_claw.config.loader import KiroClawConfig as _Cfg
            _probe_t = _Cfg.load().dashboard.mcp_probe_timeout_secs + 15
        except Exception:
            _probe_t = 30  # fallback: original default (15 + 15)
        try:
            await asyncio.wait_for(_bg_mcp_probe(), timeout=_probe_t)
        except asyncio.TimeoutError:
            print("🐾 MCP probe timed out — continuing without full probe")

        # ── Start background session and print URLs ──
        _has_slack = self._socket_client is not None

        async def _start_bg_session() -> None:
            try:
                assert self.sessions is not None
                await self.sessions.start_pool(blocking=False)
                logger.info("Background session starting")
            except Exception:
                logger.warning("Background session start failed", exc_info=True)

            if not self._no_dashboard:
                host = resolve_dashboard_host(self._local_only, self._configured_host)
                base_url = f"http://{host}:{self._dashboard_port}"
                startup_token = generate_token("local-startup", ttl_seconds=MAX_SESSION_TTL_SECS)
                dashboard_url = build_dashboard_url(
                    base_url, startup_token, local_only=self._local_only
                )
                for line in format_dashboard_urls(
                    dashboard_url,
                    port=self._dashboard_port,
                    local_only=self._local_only,
                    has_custom_host=bool(self._configured_host),
                ):
                    print(line)

                # Auto-open dashboard — skip on headless remote sessions
                _is_ssh = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))
                _has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
                _skip_open = _is_ssh and not _has_display and sys.platform != "darwin"
                if self._no_open or not self._cfg.dashboard.auto_open_browser:
                    pass  # suppressed via --no-open flag or config
                elif _skip_open:
                    print("🐾 Headless remote session — skipping browser auto-open")
                else:
                    import webbrowser

                    webbrowser.open(dashboard_url)
            if _has_slack:
                print("🐾 KiroClaw gateway connected to Slack")

        asyncio.create_task(_start_bg_session())
        print("🐾 KiroClaw gateway starting…")
        print(f"\n{DATA_WARNING}\n")

        if self._socket_client:
            await self._socket_client.connect()

        # Block until shutdown
        await shutdown_event.wait()
        print("🐾 Shutting down…")

        try:
            await asyncio.wait_for(self._shutdown(), timeout=10.0)
        except (asyncio.TimeoutError, Exception):
            logger.warning("Graceful shutdown timed out — force exiting")

        print("🐾 Goodbye!")
        # Kill any kiro-cli processes that survived graceful shutdown
        cleanup_orphaned_sessions()
        os._exit(0)


async def run_gateway(
    cfg: KiroClawConfig,
    *,
    no_dashboard: bool = False,
    no_crons: bool = False,
    no_open: bool = False,
    port_override: str | None = None,
    json_ready: bool = False,
    approval_mode: str | None = None,
) -> None:
    """Start the Slack Socket Mode gateway (blocks until shutdown).

    If Slack credentials are missing, starts in **dashboard-only** mode:
    all services (chat, cron, subagents, task runner) are available via
    the web dashboard, but Slack connectivity is disabled.
    """
    orchestrator = GatewayOrchestrator(
        cfg,
        no_dashboard=no_dashboard,
        no_crons=no_crons,
        no_open=no_open,
        port_override=port_override,
        json_ready=json_ready,
        approval_mode=approval_mode,
    )
    await orchestrator.run()
