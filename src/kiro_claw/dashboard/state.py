"""Dashboard shared state — ChatSlot and DashboardState."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
import time
import traceback
import uuid
from collections.abc import Coroutine
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from aiohttp import web

from kiro_claw.config.loader import DASHBOARD_PORT, config_dir
from kiro_claw.dashboard.side_state import SideState
from kiro_claw.knowledge.store import KnowledgeStore
from kiro_claw.safety_override import safety_override
from kiro_claw.security import redact_credentials, redact_exfiltration_urls
from kiro_claw.sel import sel

if TYPE_CHECKING:
    from kiro_claw.dashboard._types import (  # noqa: F401
        ContextBuilder,
        ConversationLog,
        CronService,
        HistoryConsolidator,
        LessonStore,
        SessionManager,
        SubagentManager,
        TaskRunner,
    )

logger = logging.getLogger(__name__)


def _log_task_exception(task: asyncio.Task[Any]) -> None:
    """Log unhandled exceptions from fire-and-forget tasks.

    Shared by gateway._deliver_result and chat.py queue-drain paths.
    Short-circuits on cancelled tasks (task.exception() would raise CancelledError).
    Exception message is redacted to avoid leaking credentials/URLs to log sinks.
    """
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        try:
            tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
            redacted_tb, _ = redact_credentials(tb)
            redacted_tb, _ = redact_exfiltration_urls(redacted_tb)
            logger.error("Background task failed:\n%s", redacted_tb)
        except Exception as redaction_err:
            # Include the redaction failure class so bugs in the redactor are visible,
            # without logging the raw traceback (which defeats the redaction contract).
            logger.error(
                "Background task failed (redaction error %s): %s",
                type(redaction_err).__name__,
                type(exc).__name__,
            )


# ── Read-only bash command classification ──

_READ_ONLY_BASH_PREFIXES: tuple[str, ...] = (
    "ls",
    "cat",
    "head",
    "tail",
    "find",
    "grep",
    "egrep",
    "fgrep",
    "wc",
    "which",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "diff",
    "pwd",
    "echo",
    "date",
    "whoami",
    "hostname",
    "uname",
    "readlink",
    "realpath",
    "basename",
    "dirname",
    "git status",
    "git log",
    "git diff",
    "git show",
    "git branch",
    "git tag",
    "git remote",
    "git rev-parse",
    "git describe",
    "git ls-files",
    "git ls-tree",
    "git cat-file",
    "git blame",
    "brazil ws show",
    "brazil ws list",
    "brazil workspace show",
    "brazil workspace list",
    "brazil versionset print",
    "brazil versionset show",
    "brazil-path",
    "python --version",
    "python3 --version",
    "node --version",
    "java -version",
    "javac -version",
)

_READ_ONLY_PIPE_RE = re.compile(
    r"^\s*(grep|egrep|fgrep|head|tail|wc|sort|uniq|cut|less|more|cat)\b"
)

# Reject redirections and command substitutions — conservative.
_UNSAFE_SHELL_RE = re.compile(r">|`|\$\(|<\(|(?<!&)&(?!&)")

# Discard-only redirect idioms that are read-only despite containing '>'/'&':
# `2>/dev/null`, `>/dev/null`, `&>/dev/null`, `2>>/dev/null`, and `2>&1`.
# These sink or merge output, never writing a real file, so they must be
# stripped before _UNSAFE_SHELL_RE — otherwise every `find … 2>/dev/null`
# falls through to an interactive prompt. A redirect to any real path
# (e.g. `cmd > out.txt`) still trips _UNSAFE_SHELL_RE and stays unsafe.
# The `(?![\w./-])` guard pins the match to the literal device `/dev/null`:
# without it, `>/dev/nullx` or `>/dev/null/../etc/passwd` would be scrubbed as
# a sink, smuggling a real-file write past the unsafe-shell check.
_DEVNULL_REDIR_RE = re.compile(r"(?:\d*>>?|&>)\s*/dev/null(?![\w./-])|\d*>&\d+")


def _classify_bash(cmd: str) -> str:
    """Single source of truth for read-only bash classification.

    Returns "" when the command is read-only, otherwise a human-readable
    reason it was rejected. :func:`is_read_only_bash` and
    :func:`unsafe_bash_reason` both delegate here so the two can never
    diverge — the invariant "reason is non-empty iff not read-only" holds
    by construction rather than by parallel maintenance. Deny-by-default.
    """
    if not cmd.strip():
        return "empty command"
    # Strip discard-only redirects (output sinks / stderr-merge) before the
    # unsafe-shell check; they are read-only but contain '>' / '&'.
    scrubbed = _DEVNULL_REDIR_RE.sub(" ", cmd)
    if _UNSAFE_SHELL_RE.search(scrubbed):
        return "unsafe shell pattern (redirect, command/process substitution, or backgrounding)"
    parts = re.split(r"\s*(?:&&|\|\||;|\n)\s*", cmd.strip())
    for part in parts:
        if not part.strip():
            continue
        pipe_parts = [p.strip() for p in part.split("|") if p.strip()]
        if not pipe_parts:
            return "unsafe shell pattern"
        first = pipe_parts[0].strip().lower()
        if not (
            first.endswith("--help")
            or first.endswith("--version")
            or any(first == p or first.startswith(p + " ") for p in _READ_ONLY_BASH_PREFIXES)
        ):
            base = first.split()[0] if first.split() else first
            return f"command '{base}' is not on the read-only allowlist"
        for target in pipe_parts[1:]:
            if not _READ_ONLY_PIPE_RE.match(target):
                tgt = target.split()[0] if target.split() else target
                return f"pipe target '{tgt}' is not a read-only filter"
    return ""


def is_read_only_bash(cmd: str) -> bool:
    """Check if a bash command is read-only. Deny-by-default."""
    return _classify_bash(cmd) == ""


def unsafe_bash_reason(cmd: str) -> str:
    """Human-readable reason a bash command failed read-only classification.

    Used to make rejection messages specific ("unsafe shell pattern …")
    instead of the generic adapter default ("User refused permission to run
    tool"). Returns "" when the command IS read-only (no reason to reject on
    safety grounds).
    """
    return _classify_bash(cmd)


# ── Shared helpers ──


def parse_cls_meta(cls_val: str) -> dict | None:
    """Parse a JSON-encoded ``cls`` string into a meta dict.

    Returns the parsed dict (with ``tool_input`` sanitized) or ``None``
    if ``cls_val`` is not valid JSON or not a dict.  Used by both
    ``_prepare_messages`` (HTTP history) and ``_broadcast_chat_message``
    (live WS push) so the frontend sees an identical ``meta`` structure.
    """
    if not cls_val:
        return None
    try:
        meta = json.loads(cls_val)
        if not isinstance(meta, dict):
            return None
    except (json.JSONDecodeError, TypeError):
        return None

    # Defence-in-depth: sanitize LLM-controlled content at every read boundary
    if isinstance(meta.get("tool_input"), str):
        sanitized, _ = redact_exfiltration_urls(meta["tool_input"])
        sanitized, _ = redact_credentials(sanitized)
        meta["tool_input"] = sanitized

    # Normalize: backend stores as request_id, frontend expects approval_id
    if "request_id" in meta and "approval_id" not in meta:
        meta["approval_id"] = meta.pop("request_id")

    return meta


def _mark_permission_resolved(messages: list[dict], request_id: str, decision: str) -> None:
    """Persist a resolved decision into a permission message's cls JSON."""
    for msg in reversed(messages):
        if msg.get("role") == "permission":
            try:
                cls = json.loads(msg.get("cls", "{}"))
                if cls.get("request_id") == request_id:
                    cls["resolved"] = decision
                    msg["cls"] = json.dumps(cls)
                    return
            except (json.JSONDecodeError, TypeError):
                pass


# ── Constants ──


_DEFAULT_PORT = DASHBOARD_PORT
_SSE_INTERVAL_SECS = 5
_NOTIFICATIONS_FILE = "notifications.jsonl"
_MAX_PERSISTED_NOTIFICATIONS = 200
_AUTO_COMPACT_NOTICE = "🔄 Auto-compacted at {pct:.0f}%."
_AUTO_COMPACT_FAILED_NOTICE = (
    "⚠ Auto-compact failed at {pct:.0f}% — will retry after cooldown. "
    "You can run `/compact` manually."
)
_MAX_SLOT_MESSAGES = 10000  # Keep all messages — virtual scrolling handles performance

# Bare chat-N label matcher used by DashboardState.resolve_slot() for prefix fallback.
# Gates the prefix lookup to prevent broad matches (e.g. bare "chat" binding to any slot).
_CHAT_N_RE = re.compile(r"chat-\d+")

# Cron notification wrapper format — used by handlers.py (create), chat.py (detect), ChatPage.tsx (render)
CRON_NOTIFY_PREFIX = "[Cron notification from "
CRON_NOTIFY_END = "[End of cron notification]"
CRON_NOTIFY_RE = re.compile(rf'^{re.escape(CRON_NOTIFY_PREFIX)}"(.*)"\]')
SUBAGENT_COMPLETION_PREFIX = "[Subagent completion event]"
# Synthetic continuation injected after a recoverable tool refusal (host-gate
# policy deny or the read-only bash gate) ended a turn early. Carries the
# refusal reason back to the model so it can adapt instead of stalling for the
# user. Rendered as an "inject" message (not a user bubble) and never mirrored
# to a linked Slack thread as user input.
REFUSAL_RECOVERY_PREFIX = "[Tool refusal — automatic recovery]"


def build_refusal_recovery_prompt(refusals: list[tuple[str, str]]) -> str:
    """Build the body of an automatic continuation after a recoverable tool refusal.

    When a tool call is refused for a recoverable, system-side reason — a
    host-gate policy deny or the read-only bash safety gate — kiro-cli ends the
    turn early with an attribution-free "tool uses were interrupted" marker. The
    refusal reason is otherwise surfaced only to the dashboard pill and the SEL
    audit log, never to the model, so the agent stalls and waits for the user.

    ``refusals`` is a list of ``(tool_title, reason)`` tuples recorded during the
    turn (already redacted by the caller). The returned text hands those reasons
    back to the model and frames the block as a system policy decision — NOT a
    user cancellation — so the agent can adapt (an allowed alternative, a
    different tool) or stop on its own with a reason. The caller prepends
    :data:`REFUSAL_RECOVERY_PREFIX`. Returns "" if there is nothing to recover.

    Lives here (a leaf module that owns the prefix) rather than in context.py so
    chat_runner can import it at module top without a circular import. There is
    deliberately no retry cap: the model decides when to stop, and the user's
    Stop button remains the hard breaker.
    """
    if not refusals:
        return ""
    lines = [
        "One or more tool calls in your previous turn were blocked by a KiroClaw "
        "safety policy, which ended the turn early. This was NOT a user action — "
        "do not treat it as a cancellation or interruption by the user.",
        "",
        "Blocked:",
    ]
    for title, reason in refusals:
        lines.append(f"  - {title}: {reason}" if reason else f"  - {title}")
    lines += [
        "",
        "Decide how to proceed: use an allowed alternative (for a shell command, "
        "a read-only variant), a different tool, or — if the block is correct and "
        "you genuinely cannot proceed — say so and stop. Otherwise continue the "
        "task where you left off.",
    ]
    return "\n".join(lines)


_OPTIONS_RE = re.compile(r"\[OPTIONS:\s*([^\]]+)\]")


def _redact(text: str) -> str:
    """Sanitise LLM output before surfacing to dashboard."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _parse_options(text: str) -> list[str]:
    """Extract pipe-separated choices from the LAST [OPTIONS: A | B | C] in text."""
    matches = _OPTIONS_RE.findall(text)
    if not matches:
        return []
    parts = [p.strip() for p in matches[-1].split("|")]
    return [p for p in parts if p]


VALID_MEMORY_MODES = ("persistent", "incognito", "temporary")


class _ChatSlot:
    """Independent chat session that runs server-side."""

    __slots__ = (
        "key",
        "title",
        "agent",
        "model",
        "reasoning_effort",
        "mode",
        "workspace",
        "project",
        "created_at",
        "messages",
        "total_messages",
        "task",
        "event",
        "_pending",
        "_queue",
        "_approval_futures",
        "_trust",
        "_trust_reads",
        "_trusted_patterns",
        "_titled",
        "_taskkeeper_task_id",
        "_resumed_count",
        "_on_message",
        "_has_reader",
        "_stop_state",
        "_stop_event_id",
        "_dirty",
        "_orch_tracker",
        "_auto_run",
        "_recovery_chat_triggered",
        "_stage_titles",
        "_stage_descriptions",
        "_plan_goal",
        "_slack_linked",
        "_slack_channel",
        "_slack_thread_ts",
        "folder_id",
        "_folder_changed",
        "pinned",
        "tags",
        "_pending_subagent_failures",
        "_recovery_retrigger_count",
        "_prompt_busy_retries",
        "_acp_pipe_death_retries",
        "_empty_response_retries",
        "_batch_rejected",
        "color_index",
        "color_theme",
        "memory_mode",
        "_ephemeral",
        "_pending_context",
        "_app",
        "_pending_variants",
        "_lock",
        "forked_from",
        "_fork_lock",
        "_tab_id",
        "_disk_older_count",
        "_disk_window_len",
        "_frozen_prefix_cache",
        "_pending_rewrite",
        "_file_changes",
        "linked_session_key",
        "_browse_mode",
        "_side",
    )

    def __init__(
        self,
        key: str,
        title: str = "",
        agent: str = "",
        workspace: str = "default",
        model: str = "",
        mode: str = "",
        memory_mode: str = "persistent",
        ephemeral: bool = False,
    ) -> None:
        self.key = key
        self.title = title or key
        self.agent = agent
        self.model = model
        # Reasoning effort: "" = provider default, else one of low/medium/high/max.
        # Currently consumed by Claude Code (--effort flag); ACP/OpenCode wired later.
        self.reasoning_effort: str = ""
        # "" = default chat, "orchestrator" = orchestrated chat
        self.mode = mode
        self.workspace = workspace
        self.project: str = ""
        self.created_at: str = datetime.now(timezone.utc).isoformat()
        self.messages: list[dict[str, Any]] = []
        self.total_messages: int = 0  # lifetime count (survives trimming)
        self.task: asyncio.Task | None = None  # type: ignore[type-arg]
        self.event = asyncio.Event()
        self._pending: list[dict[str, str]] = []
        self._queue: list[dict[str, str]] = []  # [{"id": uuid, "content": str}, ...]
        self._approval_futures: dict[str, asyncio.Future[str]] = {}  # type: ignore[type-arg]
        self._trust: bool = False  # auto-approve tools for this slot
        self._trust_reads: bool = False  # auto-approve read-only bash commands
        self._trusted_patterns: set[str] = set()  # session-scoped fnmatch globs
        self._titled: bool = False  # True once a title has been assigned
        self._taskkeeper_task_id: int | None = None  # Set when slot was spawned from a TaskKeeper task; pins taskkeeper_complete to that task only
        self._resumed_count: int = 0  # messages loaded from history on resume
        # Callback for broadcasting messages via global SSE
        self._on_message: object | None = None  # Callable[[str, dict], None] | None
        self._has_reader: bool = False  # True when HTTP SSE stream is draining
        self._stop_state: str = "idle"  # 'idle' | 'soft_pending' | 'killing'
        self._stop_event_id: str | None = None  # transcript message id for in-flight stop
        self._dirty: bool = False  # True when messages changed since last flush
        self._orch_tracker: Any = None  # OrchestrationTracker, set by gateway
        self._auto_run: bool = False  # "Go All" — skip stage gates
        self._recovery_chat_triggered: bool = False  # guard against concurrent failure recovery
        self._stage_titles: list[str] = []  # stage titles extracted from plan
        self._stage_descriptions: list[list[str]] = []  # bullet points per stage
        self._plan_goal: str = ""  # goal from 📋 Plan for: header
        self._slack_linked: bool = False  # True when linked to a Slack thread
        self._slack_channel: str = ""
        self._slack_thread_ts: str = ""
        self.folder_id: str = ""  # project folder assignment
        self._folder_changed: bool = False  # re-inject [FOLDER] breadcrumb next turn after move
        self.pinned: bool = False  # pinned to top of sidebar
        self.tags: list[str] = []  # assigned tag ids (see DashboardState._tags)
        self._pending_subagent_failures: list[str] = []
        self._recovery_retrigger_count: int = 0
        self._prompt_busy_retries: int = 0
        self._acp_pipe_death_retries: int = 0
        self._empty_response_retries: int = 0
        self._batch_rejected: bool = False
        self.color_index: int | None = None
        self.color_theme: str = ""
        if memory_mode not in VALID_MEMORY_MODES:
            raise ValueError(f"invalid memory_mode {memory_mode!r}, must be one of {VALID_MEMORY_MODES}")
        self.memory_mode: str = memory_mode
        self._ephemeral: bool = ephemeral  # Incognito mode: no memory writes
        self._pending_context: list[dict[str, Any]] = []
        self._app: str = ""  # App identity tag (App Kit §5.2)
        # Regenerate feature: variants pending attachment to next finalized assistant message
        self._pending_variants: list[dict] = []
        self._lock = asyncio.Lock()
        self.forked_from: str | None = None  # parent slot key if this is a fork
        self._fork_lock: asyncio.Lock = asyncio.Lock()  # serialises concurrent forks on this slot
        self._tab_id: str = ""  # permanent tab identity for cross-restart session chaining
        self._disk_older_count: int = 0  # count of disk messages OLDER than in-memory window (stable, set at restore/resume)
        # Count of in-memory window messages the LAST save persisted to disk
        # (the on-disk window region). Trimming may only fold a leading window
        # message into the frozen prefix once it is known to be on disk; this
        # watermark is what makes the #8 trim credit safe. It is NOT a fragile
        # "what to append" counter — saves always re-serialize the WHOLE window.
        self._disk_window_len: int = 0
        # Cached frozen-prefix bytes for the append-safe save model.
        # The session file is FROZEN-PREFIX (the first _disk_older_count on-disk
        # message lines, OLDER than the in-memory window) + a fresh re-serialize
        # of the whole window. The prefix is never rewritten, so a restart that
        # loaded only a recent window can no longer destroy older history. This
        # caches the prefix bytes keyed by (path-mtime, _disk_older_count) so a
        # 5s flush is O(window), not O(file). See chat_persistence._save_*.
        self._frozen_prefix_cache: tuple[float, int, str] | None = None
        # Set by rewind/regenerate after they TRUNCATE the window. While set,
        # _save_slot_to_history takes the archive-safe rewrite path so the
        # dropped tail is archived — even if the inline rewrite save failed
        # (#3): the next 5s flush then retries the rewrite instead of silently
        # overwriting (the default save skips archiving). Cleared on a
        # successful rewrite save.
        self._pending_rewrite: bool = False
        self._file_changes: list[dict[str, str]] = []  # [{path, content}] before-snapshots accumulated per turn for file-chip diffs
        self.linked_session_key: str = ""  # when set, _run_chat uses this as session key
        self._browse_mode: bool = False  # per-turn: True when user explicitly enables browser
        self._side: SideState | None = None

    @property
    def _plan_stage_count(self) -> int:
        return len(self._stage_titles)

    @property
    def _stopping(self) -> bool:
        return self._stop_state != "idle"

    @_stopping.setter
    def _stopping(self, value: bool) -> None:
        self._stop_state = "soft_pending" if value else "idle"

    def append(self, role: str, content: str, cls: str = "", ts: str = "", *, broadcast: bool = True, meta: dict | None = None) -> None:
        msg: dict[str, Any] = {
            "role": role,
            "content": content,
            "cls": cls,
            "ts": ts or datetime.now(timezone.utc).isoformat(),
        }
        if meta:
            msg["meta"] = meta
        self.messages.append(msg)
        self.total_messages += 1
        self._dirty = True
        self._pending.append(msg)
        self.event.set()
        # Broadcast via global SSE when no HTTP stream reader is active
        # Skip: chunk (too noisy), done (internal), user (frontend adds optimistically)
        if broadcast and self._on_message and role not in ("chunk", "done", "user") and not self._has_reader:
            self._on_message(self.key, msg)  # type: ignore[operator]
        # Trim old messages to bound memory usage
        if len(self.messages) > _MAX_SLOT_MESSAGES:
            excess = len(self.messages) - _MAX_SLOT_MESSAGES
            del self.messages[:excess]
            self._resumed_count = max(0, self._resumed_count - excess)
            # A trimmed leading window message may only join the frozen prefix
            # once it is actually on disk (#8). Credit _disk_older_count only
            # for the persisted portion; the unpersisted overflow (should not
            # happen between 5s flushes) is logged rather than silently counted
            # as on-disk, which would have stranded those turns.
            persisted_trim = min(excess, self._disk_window_len)
            self._disk_older_count += persisted_trim
            self._disk_window_len = max(0, self._disk_window_len - excess)
            if persisted_trim < excess:
                logger.warning(
                    "Slot %s trimmed %d messages not yet flushed to disk; "
                    "they will not be recoverable from history",
                    self.key, excess - persisted_trim,
                )
            # The frozen prefix grew → its cached bytes are stale.
            self._frozen_prefix_cache = None

    def drain(self) -> list[dict[str, str]]:
        """Return and clear pending messages."""
        out = self._pending[:]
        self._pending.clear()
        self.event.clear()
        return out

    def mark_permission_resolved(self, approval_id: str, decision: str = "approved") -> None:
        """Update stored permission message cls JSON with resolved flag."""
        for m in self.messages:
            if m.get("role") == "permission":
                try:
                    cls_data = json.loads(m.get("cls", ""))
                    if isinstance(cls_data, dict) and cls_data.get("request_id") == approval_id:
                        cls_data["resolved"] = decision
                        m["cls"] = json.dumps(cls_data)
                        return
                except (json.JSONDecodeError, TypeError):
                    pass

    def update_message(
        self,
        ts: str,
        *,
        content: str | None = None,
        meta: dict | None = None,
    ) -> dict | None:
        """Replace fields on a previously-appended message identified by ts.

        ``meta`` replaces the whole meta dict (so callers can also remove keys);
        pass ``None`` to leave it untouched. Returns the mutated message or None.
        """
        if not ts:
            return None
        for m in self.messages:
            if m.get("ts") == ts:
                if content is not None:
                    m["content"] = content
                if meta is not None:
                    m["meta"] = meta
                self._dirty = True
                return m
        return None

    # ── Queue helpers (dict-based queue items) ──

    def queue_append(self, content: str) -> str:
        """Append a message to the queue. Returns the generated queue ID."""
        qid = uuid.uuid4().hex[:12]
        self._queue.append({"id": qid, "content": content})
        return qid

    def queue_insert(self, index: int, content: str) -> str:
        """Insert a message at a specific queue position. Returns the queue ID."""
        qid = uuid.uuid4().hex[:12]
        self._queue.insert(index, {"id": qid, "content": content})
        return qid

    def queue_pop(self, index: int = 0) -> dict[str, str]:
        """Pop a queue item by index. Returns {"id": ..., "content": ...}."""
        return self._queue.pop(index)

    def queue_remove_by_id(self, queue_id: str) -> str | None:
        """Remove a queue item by ID. Returns the content or None if not found."""
        for i, item in enumerate(self._queue):
            if item["id"] == queue_id:
                del self._queue[i]
                return item["content"]
        return None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def queue_depth(self) -> int:
        """Number of prompts currently queued behind the active turn."""
        return len(self._queue)

    @property
    def is_restricted(self) -> bool:
        """True when memory writes (consolidation, lessons) are blocked."""
        return self.memory_mode != "persistent"

    @property
    def blocks_reads(self) -> bool:
        """True when memory-context injection into this session is blocked."""
        return self.memory_mode == "temporary"

    def enqueue_or_run_prompt(
        self,
        prompt: str,
        run_chat_coro: Callable[
            [DashboardState, _ChatSlot, str], Coroutine[Any, Any, None]
        ],
        state: DashboardState,
    ) -> bool:
        """Queue *prompt* if busy, otherwise start an agent turn.

        Encapsulates the queue-vs-run decision so callers don't need to
        touch ``_queue``, ``task``, or ``_background_tasks`` directly.
        Always registers :func:`_log_task_exception` to prevent silent failures.

        Returns ``True`` if the prompt started an agent turn, ``False`` if
        it was queued. Lets callers gate UI-visible side-effects (notifications,
        SSE pushes) on whether the prompt actually ran.

        Concurrency: the check (``self.running``) and mutation (``self.task = ...``)
        run synchronously on the asyncio event loop with no ``await`` between them,
        so two concurrent callers targeting the same slot cannot both observe
        ``running == False`` within a single loop iteration.
        """
        if self.running:
            self.queue_append(prompt)
            return False
        self.append("user", prompt, "msg msg-u")
        task = asyncio.create_task(run_chat_coro(state, self, prompt))
        self.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
        task.add_done_callback(_log_task_exception)
        return True

    def to_dict(self) -> dict:
        last_ts = self.messages[-1].get("ts", "") if self.messages else ""
        # Single reverse scan for last_msg, options, and last_activity_ts.
        last_msg = ""
        has_options = False
        options: list[str] = []
        prompt_preview = ""
        last_conv_role = ""
        last_activity_ts = ""
        found_conv = False
        for m in reversed(self.messages):
            role = m.get("role")
            # Capture last_activity_ts from the most recent actionable message
            if not last_activity_ts and role in ("tool_call", "tool_result", "assistant"):
                last_activity_ts = m.get("ts") or ""
            # Capture last conversational message (once)
            if not found_conv and role in ("user", "assistant"):
                txt = m.get("content") or ""
                if txt:
                    found_conv = True
                    last_conv_role = role
                    redacted = _redact(txt)
                    last_msg = (redacted[:80] + "…") if len(redacted) > 80 else redacted
                    if role == "assistant":
                        options = _parse_options(txt)
                        has_options = bool(options)
                        if has_options:
                            stripped = _redact(_OPTIONS_RE.sub("", txt).strip())
                            prompt_preview = (
                                stripped[:240] + "…" if len(stripped) > 240 else stripped
                            )
            if found_conv and last_activity_ts:
                break
        pending_approval = any(not f.done() for f in self._approval_futures.values())
        # waiting_for_input: turn ended (not running), no options, no approval,
        # and the last conversational message is from the assistant (not user).
        waiting_for_input = (
            not self.running
            and not has_options
            and not pending_approval
            and bool(self.messages)
            and last_conv_role == "assistant"
        )
        # If an approval is pending, surface the tool metadata from the most
        # recent unresolved permission message so the Board can show inline
        # Approve/Trust/Reject buttons without a second API call.
        #
        # LANE ASSIGNMENT NOTE: The frontend's inferLane() uses the boolean
        # `pending_approval` field (not `pending_approval_info`) to assign
        # sessions to the "Needs Approval" lane. `pending_approval_info` is
        # supplementary UI metadata (tool name, input, kind) for rendering
        # inline action buttons — it does NOT drive lane placement.
        pending_approval_info: dict[str, str] | None = None
        if pending_approval:
            for m in reversed(self.messages):
                if m.get("role") != "permission":
                    continue
                meta = parse_cls_meta(m.get("cls") or "") or {}
                if meta.get("resolved"):
                    continue
                pending_approval_info = {
                    "tool": _redact(m.get("content") or ""),
                    "tool_input": _redact(meta.get("tool_input", "")),
                    "tool_kind": _redact(meta.get("tool_kind", "")),
                    "request_id": _redact(meta.get("approval_id", meta.get("request_id", ""))),
                }
                break
        return {
            "key": self.key,
            "title": _redact(self.title) if self.title else self.title,
            "agent": self.agent,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "mode": self.mode,
            # Forward-compat alias of `mode` for the frontend's surface
            # registry. Today every slot's surface is identical to its mode
            # (default chat -> "", autopilot -> "orchestrator"), but emitting
            # a distinct field lets a future backend split the two — e.g. a
            # mode that introduces new behavior without claiming its own nav
            # destination, or two modes that share a destination — without
            # another wire-format change. The frontend reads
            # `slot.surface ?? slot.mode` for back-compat.
            "surface": self.mode,
            "workspace": self.workspace,
            "project": self.project,
            "messages": len(self.messages),
            "running": self.running,
            "stopping": self._stopping,
            "pending_approval": pending_approval,
            "pending_approval_info": pending_approval_info,
            "last_activity_ts": last_activity_ts,
            "waiting_for_input": waiting_for_input,
            "stop_state": self._stop_state,
            "created": self.created_at,
            "last_ts": last_ts,
            "last_message": last_msg,
            "has_options": has_options,
            "options": [_redact(o) for o in options],
            "prompt_preview": prompt_preview,
            "trust": self._trust,
            "trust_reads": self._trust_reads,
            "trusted_patterns_count": len(self._trusted_patterns),
            "slack_linked": self._slack_linked,
            "slack_channel": self._slack_channel,
            "slack_thread_ts": self._slack_thread_ts,
            "folder_id": self.folder_id,
            "pinned": self.pinned,
            "tags": list(self.tags),
            "color_index": self.color_index,
            "color_theme": self.color_theme,
            "memory_mode": self.memory_mode,
            "forked_from": self.forked_from,
            "linked_session_key": self.linked_session_key,
            "app": self._app,
        }


class DashboardState:
    """Shared state injected into all handlers via ``app["state"]``."""

    def __init__(
        self,
        sessions: SessionManager,
        crons: CronService,
        lessons: LessonStore,
        start_time: float,
        subagents: SubagentManager | None = None,
        context_builder: ContextBuilder | None = None,
        conversation_log: ConversationLog | None = None,
        consolidator: HistoryConsolidator | None = None,
        task_runner: TaskRunner | None = None,
        slack_client: Any = None,
        owner_id: str = "",
    ):
        self.sessions = sessions
        self.crons = crons
        self.lessons = lessons
        self.start_time = start_time
        self.subagents = subagents
        self.channel_manager: Any = None  # lazy-init in server.py
        self.tunnel_manager: Any = None  # lazy-init in server.py (TunnelManager)
        self.instances_manager: Any = None  # lazy-init in server.py (SshTunnelManager)
        self.instances_registry: Any = None  # lazy-init in server.py (InstancesRegistry)
        # Secretary subsystem removed; kept as permanent None for apps/routes.py
        # builtin-service restart lookup (getattr-based, no-op when None).
        self._secretary_restart: Any = None  # restart callback (always None — service removed)
        self.context_builder = context_builder
        self.conversation_log = conversation_log
        self.consolidator = consolidator
        self.task_runner = task_runner
        self.slack_client = slack_client
        self.owner_id = owner_id
        self._owner_hash: str | None = None
        self.messages_received = 0
        # Broadcast: each SSE client gets its own queue; _notify_event wakes all
        self._sse_queues: list[asyncio.Queue[dict[str, Any]]] = []
        self._notify_event = asyncio.Event()
        self._notification_log: list[dict[str, Any]] = _load_notifications()
        self._unread_count: int = 0
        self._slots: dict[str, _ChatSlot] = {}
        self._slack_to_slot: dict[str, str] = {}  # Slack session_key → slot name
        self._slot_counter = 0
        self._folders: list[dict[str, Any]] = []  # project folder definitions
        # Tag vocabulary: list of {id, name, color, order}. User-managed.
        self._tags: list[dict[str, Any]] = []
        # Sidebar columns — flat list of {id, name, tag_ids, mode, order, include_untagged}
        self._tag_boards: list[dict[str, Any]] = []
        self._background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        self.no_crons: bool = False  # --no-crons flag: cron execution disabled
        self._hook_store: Any = None  # Lazy-init ScriptHookStore
        # Task refine state (background LLM spec generation)
        self._refine_status: str = "idle"  # idle, running, done, error, cancelled
        self._refine_text: str = ""
        self._refine_error: str = ""
        self._terminal_sessions: dict[str, Any] = {}  # PTY sessions for CLI panel
        self._terminal_reaper: asyncio.Task | None = None  # type: ignore[type-arg]
        self._loop_heartbeat: asyncio.Task | None = None  # type: ignore[type-arg]

        # Knowledge Library
        self._knowledge_store: "KnowledgeStore | None" = None  # Lazy-initialized on first access
        self._knowledge_watcher: asyncio.Task | None = None  # type: ignore[type-arg]
        # Slack channel name resolver (lazy-initialized on first /api/slack/channels hit)
        self._channel_resolver: Any = None
        self._refine_input: str = ""
        self._refine_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._refine_session_key: str = ""
        # slack_client is set via constructor param above; gateway may override later
        self._refine_answer_future: asyncio.Future | None = None  # type: ignore[type-arg]
        # WebSocket clients (multiplexed real-time connection)
        self._ws_clients: list[web.WebSocketResponse] = []
        self._ws_log_subscribers: set[web.WebSocketResponse] = set()
        self._ws_subagent_subscribers: set[web.WebSocketResponse] = set()
        # Pending tool approvals: id → asyncio.Future[bool]
        self._pending_approvals: dict[str, dict] = {}
        self._approval_futures: dict[str, asyncio.Future] = {}  # type: ignore[type-arg]
        self._flush_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # Update progress tracking (shared across all connected clients)
        self._update_progress: dict[str, str] | None = None  # {step, detail}
        # Restricted (incognito/temporary): session keys with memory writes disabled
        self._restricted_keys: set[str] = set()
        # Ephemeral: session keys with no memory writes at all
        self._ephemeral_keys: set[str] = set()
        # Per-project file index registry (shared across slots)
        from kiro_claw.dashboard.file_index import FileIndexRegistry
        self.file_indexes = FileIndexRegistry()

    def wire_session_compact_callback(self) -> None:
        """Register the dashboard's compaction callback on the session manager."""

        async def _on_compacted(
            key: str, pct: float, *, success: bool
        ) -> None:
            if not key.startswith("dashboard:"):
                return
            slot_key = key[len("dashboard:"):]
            slot = self.get_slot(slot_key)
            if slot is None:
                return
            template = _AUTO_COMPACT_NOTICE if success else _AUTO_COMPACT_FAILED_NOTICE
            message = template.format(pct=pct)
            try:
                # Tag kind="compaction" so this proactive auto-compact notice
                # (fired at session.autocompact_pct) is skipped by the dashboard's
                # follow-up [OPTIONS:] backward scan — same invariant as
                # chat_utils._append_compaction_notice. meta.kind covers history
                # reload; slot.append carries the meta on the live broadcast too.
                # (Routing through the chat_utils chokepoint would create a
                # state<->chat_utils import cycle; the notice is a hardcoded
                # template with no LLM content, so its redaction pass is moot.)
                slot.append(
                    "assistant", message, "msg msg-a", meta={"kind": "compaction"}
                )
            except Exception:
                logging.getLogger(__name__).exception(
                    "Failed to append compact notice to slot %s", slot_key
                )
            if success:
                # Reset the context bar — successful compact dropped usage.
                try:
                    self.broadcast_ws(
                        "context_usage", {"slot": slot_key, "pct": 0.0}
                    )
                except Exception:
                    logging.getLogger(__name__).exception(
                        "Failed to broadcast context_usage for slot %s", slot_key
                    )

        self.sessions.set_compact_callback(_on_compacted)

    def _count_lessons(self) -> int:
        """Count lessons from JSONL store + vector store (if enabled)."""
        count = len(self.lessons.load_all())
        if self.context_builder:
            vs = self.context_builder.memory.vector_store
            if vs:
                count += len(vs.get_lessons())
        return count

    def status_snapshot(
        self,
        *,
        cron_jobs: int | None = None,
        lessons: int | None = None,
        update_available: bool = False,
    ) -> dict[str, Any]:
        """Core status fields shared by /api/status, SSE, and WebSocket pushes."""
        uptime = int(time.time() - self.start_time)
        return {
            "uptime": _fmt_duration(uptime),
            "start_time": self.start_time,
            "sessions": self.sessions.count,
            "messages": self.messages_received,
            "cron_jobs": cron_jobs if cron_jobs is not None else len(self.crons.list_jobs()),
            "lessons": lessons if lessons is not None else self._count_lessons(),
            "subagents": self.subagents.count if self.subagents else 0,
            "update_available": update_available,
            "no_crons": self.no_crons,
        }

    _APPROVAL_TIMEOUT = 7200  # 2 hours — triggers pause (not skip/fail) via deny path
    # Background sources (cron, heartbeat, taskrunner) have no human responder, so
    # waiting the full human window would burn 2h on every unattended approval. They
    # wait only this short window and then deny-fast, letting the turn proceed/fail
    # rather than hang.
    _BACKGROUND_APPROVAL_TIMEOUT_SECS = 180  # 3 minutes — deny-fast for unattended runs
    _FLUSH_INTERVAL = 5  # seconds between dirty-slot flushes

    _log = logging.getLogger(__name__)

    @property
    def knowledge_store(self):  # type: ignore[override]
        """Lazy-init KnowledgeStore on first access."""
        if self._knowledge_store is None:
            db_dir = os.path.join(str(config_dir()), "workspace", "knowledge")
            os.makedirs(db_dir, exist_ok=True)
            self._knowledge_store = KnowledgeStore(os.path.join(db_dir, "knowledge.db"))
        return self._knowledge_store

    def enable_yolo(self, *, from_config: bool = False) -> None:
        """Activate safety override (delegates to safety_override module)."""
        source = "config" if from_config else "dashboard"
        safety_override().activate(source)

    def disable_yolo(self) -> None:
        """Deactivate safety override (delegates to safety_override module)."""
        safety_override().deactivate("dashboard")

    def is_yolo_active(self) -> bool:
        """Return whether safety override is active (delegates to safety_override module)."""
        return safety_override().is_active()

    @property
    def _yolo(self) -> bool:
        """Backward-compat property for code reading _yolo directly."""
        return safety_override().is_active()

    @_yolo.setter
    def _yolo(self, value: bool) -> None:
        """Backward-compat setter for tests that assign state._yolo = True/False."""
        if value:
            safety_override().activate("dashboard")
        else:
            safety_override().deactivate("dashboard")

    async def request_approval(
        self,
        approval_id: str,
        source: str,
        tool: str,
        *,
        tool_input: str = "",
        tool_purpose: str = "",
        slot: str = "",
        is_background: bool = False,
    ) -> bool:
        """Request interactive approval. Returns True if approved, False if rejected/timeout.

        ``is_background`` marks an unattended source (cron, heartbeat, taskrunner)
        with no human responder. Those wait only ``_BACKGROUND_APPROVAL_TIMEOUT_SECS``
        and then deny-fast, instead of burning the full 2h human window.
        """
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[bool] = loop.create_future()
        self._approval_futures[approval_id] = fut

        # Sanitize LLM-sourced fields before broadcasting to dashboard clients
        safe_tool, _ = redact_exfiltration_urls(tool)
        safe_tool, _ = redact_credentials(safe_tool)
        safe_input, _ = redact_exfiltration_urls(tool_input)
        safe_input, _ = redact_credentials(safe_input)
        safe_purpose, _ = redact_exfiltration_urls(tool_purpose)
        safe_purpose, _ = redact_credentials(safe_purpose)

        self._pending_approvals[approval_id] = {
            "id": approval_id,
            "source": source,
            "tool": safe_tool,
            "tool_input": safe_input,
            "tool_purpose": safe_purpose,
            "slot": slot,
            "ts": time.time(),
        }
        self.broadcast_ws("approval", self._pending_approvals[approval_id])
        # Background sources have no human present — deny-fast on a short window
        # instead of pausing for the full 2h human window.
        timeout = (
            self._BACKGROUND_APPROVAL_TIMEOUT_SECS if is_background else self._APPROVAL_TIMEOUT
        )
        try:
            # Timeout triggers deny → which pauses the run (not skip/fail) for
            # interactive sources. This prevents indefinite hangs if notifications
            # are lost or user disconnects, while still allowing the user to resume
            # later. The run pauses gracefully rather than silently proceeding or
            # permanently failing.
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            # Deny-by-default on shutdown/cancellation
            return False
        finally:
            self._pending_approvals.pop(approval_id, None)
            self._approval_futures.pop(approval_id, None)

    def _audit_and_broadcast_approval(self, session_key: str, approval_id: str, approved: bool) -> None:
        """Emit SEL audit event and broadcast WS notification for an approval decision."""
        try:
            sel().log_tool_invocation(
                session_key=session_key,
                tool_name="approval_decision",
                outcome="approved" if approved else "rejected",
                request_id=approval_id,
                source="dashboard",
            )
        except Exception:
            self._log.warning("SEL audit failed for approval resolution", exc_info=True)
        try:
            self.broadcast_ws("approval_resolved", {"id": approval_id, "approved": approved})
        except Exception:
            self._log.warning("WS broadcast failed for approval resolution", exc_info=True)

    def resolve_approval(self, approval_id: str, approved: bool) -> bool:
        """Resolve a pending approval. Returns False if not found.

        State-level futures receive ``bool`` (consumed by gateway, which converts to str).
        Slot-level futures receive ``str`` ("approved"/"rejected", consumed by channel.py).
        """
        decision = "approved" if approved else "rejected"
        fut = self._approval_futures.get(approval_id)
        if fut and not fut.done():
            fut.set_result(approved)
            self._audit_and_broadcast_approval("state", approval_id, approved)
            return True
        # Also check slot-level approval futures (chat tool approvals)
        for slot in self._slots.values():
            fut = slot._approval_futures.get(approval_id)
            if fut and not fut.done():
                fut.set_result(decision)
                _mark_permission_resolved(slot.messages, approval_id, decision)
                self._audit_and_broadcast_approval(slot.key, approval_id, approved)
                self.push_slots_update()
                return True
        return False

    def start_flush_loop(self) -> None:
        """Start background loop that flushes dirty slots to disk every 5s."""
        if self._flush_task is None:
            self._flush_task = asyncio.ensure_future(self._flush_loop())

    async def _flush_loop(self) -> None:
        """Periodically save dirty slots so a crash loses at most 5s of chat."""
        from kiro_claw import shutdown_event

        while not shutdown_event.is_set():
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=self._FLUSH_INTERVAL)
                return
            except asyncio.TimeoutError:
                pass
            await asyncio.get_running_loop().run_in_executor(None, self._flush_dirty_slots)

    def _flush_dirty_slots(self) -> None:
        """Write any slot with new messages to its JSONL file."""
        if not self.conversation_log:
            return
        from kiro_claw.dashboard.chat import _save_slot_to_history

        for slot in list(self._slots.values()):
            if not slot._dirty or not slot.messages:
                continue
            try:
                _save_slot_to_history(self, slot)
                slot._dirty = False
            except Exception:
                logger.warning("Flush failed for slot %s", slot.key, exc_info=True)

    def notify(self, kind: str, title: str, body: str, *, meta: dict | None = None) -> None:
        """Push a notification to ALL connected SSE clients and persist to disk."""
        note: dict[str, Any] = {
            "kind": kind,
            "title": title,
            "body": body,
            "ts": datetime.now(tz=timezone.utc).isoformat(),
        }
        if meta:
            note.update(meta)
        self._notification_log.append(note)
        self._unread_count += 1
        self._broadcast(note)
        _persist_notification(note)

    def register_sse(self) -> asyncio.Queue[dict[str, Any]]:
        """Register a new SSE client and return its dedicated queue."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=100)
        self._sse_queues.append(q)
        return q

    def unregister_sse(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Remove an SSE client queue on disconnect."""
        try:
            self._sse_queues.remove(q)
        except ValueError:
            pass

    def mark_notifications_read(self) -> None:
        """Reset unread counter (called when client opens notification panel)."""
        self._unread_count = 0

    def delete_notification(self, ts: str) -> bool:
        """Remove a single notification by timestamp and persist to disk."""
        before = len(self._notification_log)
        self._notification_log = [n for n in self._notification_log if n.get("ts") != ts]
        removed = len(self._notification_log) < before
        if removed:
            _rewrite_notifications(self._notification_log)
        return removed

    def ack_notification(self, ts: str) -> bool:
        """Mark a notification as acknowledged and persist."""
        for n in self._notification_log:
            if n.get("ts") == ts:
                n["acked"] = True
                _rewrite_notifications(self._notification_log)
                self.broadcast_ws("notification_ack", {"ts": ts})
                return True
        return False

    def unack_notification(self, ts: str) -> bool:
        """Mark a notification as unread and persist."""
        for n in self._notification_log:
            if n.get("ts") == ts:
                n["acked"] = False
                _rewrite_notifications(self._notification_log)
                self.broadcast_ws("notification_unack", {"ts": ts})
                return True
        return False

    def clear_notifications(self) -> None:
        """Remove all notifications from memory and disk."""
        self._notification_log.clear()
        self._unread_count = 0
        path = _notifications_path()
        try:
            if path.exists():
                path.write_text("", encoding="utf-8")
        except Exception:
            logger.debug("Failed to clear notifications file", exc_info=True)

    def get_slot(self, name: str) -> _ChatSlot | None:
        """Look up a slot by name without creating it. Returns None if absent."""
        return self._slots.get(name)

    def has_slot(self, name: str) -> bool:
        """Check if a slot exists by name."""
        return name in self._slots

    def get_linked_slot(self, session_key: str) -> "_ChatSlot | None":
        """Look up a dashboard slot linked to a Slack thread. Cleans up stale mappings."""
        slot_key = self._slack_to_slot.get(session_key)
        if not slot_key:
            return None
        slot = self._slots.get(slot_key)
        if not slot or not slot._slack_linked or slot._slack_thread_ts != session_key:
            self._slack_to_slot.pop(session_key, None)
            return None
        return slot

    def resolve_slot(self, name: str) -> _ChatSlot | None:
        """Like :meth:`get_slot`, but also resolves bare ``chat-N`` labels.

        Falls back to a prefix match so ``chat-2`` resolves to
        ``chat-2-<timestamp>`` when no exact match exists. The fallback is
        gated to names matching ``chat-\\d+`` to prevent broad-prefix
        collisions (e.g. a bare ``chat`` binding to any ``chat-*`` slot).

        Tie-break: when multiple slots share the same ``chat-N-`` prefix
        (e.g. after a resume creates a second timestamped slot), returns
        the first slot in dict iteration order. Under normal operation
        that's also the oldest slot, but callers should not rely on it
        after ad-hoc removals and re-adds. In practice only one active
        slot per chat-N label exists at a time.

        Use this from trusted delivery paths (heartbeat, cron) where the
        caller wants short-label addressing. Do NOT use from HTTP handlers
        that pass the resolved name to key-derivation functions
        (e.g. ``_history_key_for``) — those require the full slot key.
        """
        slot = self._slots.get(name)
        if slot is not None:
            return slot
        if not _CHAT_N_RE.fullmatch(name):
            return None
        prefix = name + "-"
        for key, s in self._slots.items():
            if key.startswith(prefix):
                return s
        return None

    def link_slack(self, slot_name: str, thread_ts: str, channel_id: str) -> None:
        """Update a slot's Slack link state and persist to SessionStore."""
        slot = self._slots.get(slot_name)
        if not slot:
            return
        # Remove stale mapping if slot was previously linked to a different thread
        old_ts = slot._slack_thread_ts
        if old_ts and old_ts != thread_ts:
            self._slack_to_slot.pop(old_ts, None)
        # Clear persisted link of old slot if this thread was previously owned by another slot
        old_owner = self._slack_to_slot.get(thread_ts)
        if old_owner and old_owner != slot_name:
            old_slot = self._slots.get(old_owner)
            if old_slot:
                old_slot._slack_linked = False
                old_slot._slack_thread_ts = ""
                old_slot._slack_channel = ""
            if self.sessions:
                from kiro_claw.dashboard.chat import _history_key_for

                self.sessions.set_slack_link(_history_key_for(old_owner), "", "")
        slot._slack_linked = True
        slot._slack_channel = channel_id
        slot._slack_thread_ts = thread_ts
        self._slack_to_slot[thread_ts] = slot_name
        # Persist so link survives gateway restarts
        if self.sessions:
            from kiro_claw.dashboard.chat import _history_key_for

            self.sessions.set_slack_link(_history_key_for(slot_name), thread_ts, channel_id)
        self.push_slots_update()

    def get_or_create_slot(
        self,
        name: str | None = None,
        agent: str = "",
        workspace: str = "default",
        model: str = "",
        mode: str = "",
        memory_mode: str | None = None,
        ephemeral: bool | None = None,
        app: str = "",
    ) -> _ChatSlot:
        """Return existing slot or create a new one."""
        if name and name in self._slots:
            existing = self._slots[name]
            if memory_mode is not None and memory_mode != existing.memory_mode:
                raise ValueError(
                    f"Slot {name!r} already exists with memory_mode={existing.memory_mode!r}"
                )
            return existing
        if not name:
            import time

            self._slot_counter += 1
            ts = int(time.time())
            name = f"chat-{self._slot_counter}-{ts}"
        slot = _ChatSlot(name, agent=agent, workspace=workspace, model=model, mode=mode, memory_mode=memory_mode or "persistent")
        slot._tab_id = uuid.uuid4().hex[:12]
        slot._on_message = self._broadcast_chat_message
        slot._app = app
        if memory_mode and memory_mode != "persistent":
            self._restricted_keys.add(f"dashboard:{name}")
        if ephemeral:
            self._ephemeral_keys.add(f"dashboard:{name}")
        # Check if this session is already linked to a Slack thread
        try:
            if self.sessions:
                from kiro_claw.dashboard.chat import _history_key_for

                _ts, _ch = self.sessions.get_slack_link(_history_key_for(name))
                slot._slack_linked = _ts is not None
                if _ts and _ch:
                    slot._slack_channel = _ch
                    slot._slack_thread_ts = _ts
        except Exception:
            pass
        self._slots[name] = slot
        self.push_slots_update()
        return slot

    def _broadcast_chat_message(self, slot_key: str, msg: dict) -> None:
        """Push a chat message to all SSE clients via the global stream."""
        payload: dict[str, Any] = {
            "_type": "chat_message",
            "slot": slot_key,
            "role": msg.get("role", ""),
            "content": msg.get("content", ""),
            "ts": msg.get("ts", ""),
        }
        # Include cls for backward compatibility
        cls_val = msg.get("cls", "")
        if cls_val:
            payload["cls"] = cls_val
            # Parse cls as JSON to send structured meta field for new frontend
            meta = parse_cls_meta(cls_val)
            if meta is not None:
                payload["meta"] = meta
        # Also include direct meta (e.g. tool_call_id on tool messages)
        direct_meta = msg.get("meta")
        if direct_meta and isinstance(direct_meta, dict):
            payload["meta"] = {**(payload.get("meta") or {}), **direct_meta}
        self._broadcast(payload)

    # ── Folder persistence ──

    _FOLDERS_FILE = "folders.json"
    _TAGS_FILE = "tags.json"
    _TAG_BOARDS_FILE = "tag_boards.json"

    # Seed vocabulary created on first run when tags.json is missing or empty.
    # status=True tags are mutually-exclusive workflow states. Drag-between-columns
    # strips all status tags from a card and applies the destination column's
    # status tag. Non-status tags survive the drag.
    _DEFAULT_TAGS: list[dict[str, Any]] = [
        {"id": "planned", "name": "Planned", "color": "#6b7280", "order": 0, "status": True},
        {"id": "todo", "name": "ToDo", "color": "#3b82f6", "order": 1, "status": True},
        {"id": "implementation", "name": "Implementation", "color": "#8b5cf6", "order": 2, "status": True},
        {"id": "review", "name": "Review", "color": "#f59e0b", "order": 3, "status": True},
        {"id": "done", "name": "Done", "color": "#10b981", "order": 4, "status": True},
    ]

    def load_folders(self) -> None:
        """Load folder definitions from disk."""
        path = config_dir() / self._FOLDERS_FILE
        try:
            if path.exists():
                self._folders = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            logger.warning("Failed to load folders", exc_info=True)

    def save_folders(self) -> None:
        """Persist folder definitions to disk (atomic write)."""
        path = config_dir() / self._FOLDERS_FILE
        self._atomic_write_json(path, self._folders)

    def folder_breadcrumb(self, folder_id: str, sep: str = " › ") -> str:
        """Render a folder's ancestry root→leaf as a breadcrumb string.

        Walks the ``parent_id`` chain up to the root, then joins names with
        *sep*. Cycle-safe (a visited set bounds the walk) and tolerant of
        dangling ``parent_id`` references. Returns "" for an empty or unknown
        folder id.
        """
        if not folder_id:
            return ""
        by_id = {f["id"]: f for f in self._folders}
        names: list[str] = []
        seen: set[str] = set()
        fid = folder_id
        while fid and fid in by_id and fid not in seen:
            seen.add(fid)
            folder = by_id[fid]
            names.append(str(folder.get("name", "")))
            fid = str(folder.get("parent_id") or "")
        names.reverse()
        return sep.join(n for n in names if n)

    def load_tags(self) -> None:
        """Load tag vocabulary and sidebar columns from disk; seed defaults if missing.

        Only seed when ``tags.json`` does not exist. An explicitly-empty file
        is left as-is (so a user who deletes every tag stays at zero tags
        across restarts), and a parse failure is left untouched (so a
        transient I/O error never silently overwrites saved data).
        """
        tags_path = config_dir() / self._TAGS_FILE
        file_existed = tags_path.exists()
        try:
            if file_existed:
                raw = json.loads(tags_path.read_text(encoding='utf-8'))
                if isinstance(raw, list):
                    self._tags = [t for t in raw if isinstance(t, dict) and t.get("id")]
        except Exception:
            logger.warning("Failed to load tags", exc_info=True)
            # Treat a parse error like a present file: do not re-seed.
            file_existed = True
        # Back-fill the status flag for legacy tags saved before the field existed.
        # The 5 seed ids are canonical status tags; everything else defaults to False.
        seed_ids = {t["id"] for t in self._DEFAULT_TAGS}
        mutated = False
        for t in self._tags:
            if "status" not in t:
                t["status"] = t.get("id") in seed_ids
                mutated = True
        if not file_existed and not self._tags:
            # Fresh install (no tags.json on disk) — seed the default vocabulary.
            self._tags = [dict(t) for t in self._DEFAULT_TAGS]
            mutated = True
        if mutated:
            self.save_tags()

        # Column layout: flat list of {id, name, tag_ids, mode, order}.
        # Empty list = single implicit "all sessions" column (legacy UX).
        columns_path = config_dir() / self._TAG_BOARDS_FILE
        try:
            if columns_path.exists():
                raw = json.loads(columns_path.read_text(encoding='utf-8'))
                if isinstance(raw, list):
                    self._tag_boards = [c for c in raw if isinstance(c, dict) and c.get("id")]
        except Exception:
            logger.warning("Failed to load sidebar columns", exc_info=True)

    def save_tags(self) -> None:
        """Persist tag vocabulary to disk (atomic write)."""
        self._atomic_write_json(config_dir() / self._TAGS_FILE, self._tags)

    def save_tag_boards(self) -> None:
        """Persist sidebar column layout to disk (atomic write)."""
        self._atomic_write_json(config_dir() / self._TAG_BOARDS_FILE, self._tag_boards)

    @staticmethod
    def _atomic_write_json(path: Path, data: Any) -> None:
        """Atomic JSON write used by folder/tag persistence helpers."""
        try:
            payload = json.dumps(data).encode()
            fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                try:
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, str(path))
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception:
            logger.warning("Failed to write %s", path.name, exc_info=True)

    def push_slots_update(self) -> None:
        """Push current slot list to all SSE clients (instant UI update)."""
        yolo_active = self.is_yolo_active()  # expire first if needed
        slots_data = [s.to_dict() for s in self._slots.values()]
        mgr = getattr(self, "channel_manager", None)
        ch_trusted = bool(mgr and any(ch.trusted for ch in mgr._channels.values()))
        self._broadcast(
            {
                "_type": "slots",
                "_slots_list": slots_data,
                "_yolo": yolo_active,
                "slots": json.dumps(slots_data),
                "channelTrusted": ch_trusted,
            }
        )

    def push_slot_title(self, key: str, title: str) -> None:
        """Push a targeted title update for a single slot.

        Also pushes a full slots update so the sidebar reflects the new
        title without callers needing to do both.
        """
        self._broadcast({"_type": "slot_title", "key": key, "title": title})
        self.push_slots_update()

    def push_refresh(self, *kinds: str) -> None:
        """Push a lightweight refresh hint for specific data types.

        The frontend receives ``event: refresh`` with ``data: kind1,kind2``
        and fetches fresh data only for those types.  This replaces blind
        polling — the server tells the client *when* to refresh, not the
        client guessing on a timer.

        Supported kinds: ``crons``, ``lessons``, ``agents``, ``history``,
        ``taskrunner``.
        """
        self._broadcast({"_type": "refresh", "kinds": ",".join(kinds)})

    def push_update_progress(self, step: str, detail: str = "") -> None:
        """Broadcast an update progress event to all connected clients.

        ``step`` is a short machine-readable phase name (e.g. ``pulling``,
        ``syncing``, ``building``, ``installing``, ``restarting``, ``failed``).
        ``detail`` is an optional human-readable message.
        """
        self._update_progress = {"step": step, "detail": detail}
        self._broadcast(
            {
                "_type": "update_progress",
                "step": step,
                "detail": detail,
            }
        )

    def clear_update_progress(self) -> None:
        """Reset update progress (e.g. after cancel or completion)."""
        self._update_progress = None

    def _broadcast(self, note: dict[str, Any]) -> None:
        """Send a message to all connected SSE and WS clients."""
        for q in self._sse_queues:
            try:
                q.put_nowait(note)
            except asyncio.QueueFull:
                pass
        self._notify_event.set()
        # WS broadcast — translate internal _type to WS message format
        if self._ws_clients:
            msg_type = note.get("_type", "notification")
            if msg_type == "slots":
                slots_list = note.get("_slots_list") or json.loads(note["slots"])
                ws_msg = json.dumps(
                    {
                        "type": "slots",
                        "data": slots_list,
                        "yolo": note.get("_yolo", False),
                        "channelTrusted": note.get("channelTrusted", False),
                    }
                )
            elif msg_type == "slot_title":
                ws_msg = json.dumps(
                    {"type": "slot_title", "data": {"key": note["key"], "title": note["title"]}}
                )
            elif msg_type == "refresh":
                ws_msg = json.dumps(
                    {"type": "refresh", "data": {"kinds": note["kinds"].split(",")}}
                )
            elif msg_type == "update_progress":
                ws_msg = json.dumps(
                    {
                        "type": "update_progress",
                        "data": {"step": note["step"], "detail": note.get("detail", "")},
                    }
                )
            elif msg_type == "chat_message":
                chat_data: dict[str, Any] = {
                    "slot": note["slot"],
                    "role": note["role"],
                    "content": note["content"],
                    "ts": note.get("ts", ""),
                }
                # Include cls for messages with metadata (e.g. permission with tool_input)
                if note.get("cls"):
                    chat_data["cls"] = note["cls"]
                if note.get("meta"):
                    chat_data["meta"] = note["meta"]
                ws_msg = json.dumps({"type": "chat_message", "data": chat_data})
            else:
                ws_msg = json.dumps({"type": "notification", "data": note})
            self._send_ws_all(ws_msg)

    def _spawn_ws_send(self, ws: web.WebSocketResponse, msg: str) -> None:
        """Fire-and-forget a WS send while retaining a strong task reference.

        ``asyncio.ensure_future(...)`` without keeping the returned task lets the
        event loop hold only a weak reference, so the task can be garbage-collected
        mid-send — silently dropping the websocket message (a lost dashboard update).
        Track it in ``_background_tasks`` (the existing pattern in this module) and
        discard on completion so the reference is held for the task's lifetime.
        """
        task = asyncio.ensure_future(ws.send_str(msg))
        self._background_tasks.add(task)
        task.add_done_callback(self._on_ws_send_done)

    def _on_ws_send_done(self, task: asyncio.Task) -> None:
        """Discard the finished WS-send task and surface any failure.

        A failed ``ws.send_str`` (e.g. ``ConnectionResetError`` when a client
        disconnects mid-send) is otherwise swallowed silently — the task stores the
        exception, nobody reads it, and it's GC'd with the task — leaving operators
        blind to send failures under burst load. Log at DEBUG since peer disconnects
        are routine and expected, not errors.
        """
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.debug("WS send failed (client likely disconnected): %s", exc)

    def _send_ws_all(self, msg: str) -> None:
        """Send a pre-serialized JSON string to all WS clients."""
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._ws_clients):
            if ws.closed:
                dead.append(ws)
                continue
            try:
                self._spawn_ws_send(ws, msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._remove_ws(ws)

    def broadcast_ws(self, msg_type: str, data: object) -> None:
        """Send a typed message to all WS clients (not SSE)."""
        if not self._ws_clients:
            return
        msg = json.dumps({"type": msg_type, "data": data})
        self._send_ws_all(msg)

    def broadcast_browser_event(self, event_type: str, data: dict) -> None:
        """Broadcast a browser activity event to all connected WS clients.

        Redacts string values to prevent credential leakage.
        """
        safe_data: dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, str):
                v, _ = redact_credentials(v)
                v, _ = redact_exfiltration_urls(v)
            safe_data[k] = v
        payload: dict[str, Any] = {"type": "browser_event", "event": event_type, "ts": time.time()}
        for k, v in safe_data.items():
            if k not in ("type", "event", "ts"):
                payload[k] = v
        self.broadcast_ws("browser_event", payload)

    def register_ws(self, ws: web.WebSocketResponse) -> None:
        """Register a new WebSocket client."""
        self._ws_clients.append(ws)

    def unregister_ws(self, ws: web.WebSocketResponse) -> None:
        """Remove a WebSocket client on disconnect."""
        self._remove_ws(ws)

    def _remove_ws(self, ws: web.WebSocketResponse) -> None:
        """Remove a WS client from all subscriber lists."""
        try:
            self._ws_clients.remove(ws)
        except ValueError:
            pass
        self._ws_log_subscribers.discard(ws)
        self._ws_subagent_subscribers.discard(ws)

    def subscribe_logs(self, ws: web.WebSocketResponse) -> None:
        """Subscribe a WS client to log events."""
        self._ws_log_subscribers.add(ws)

    def unsubscribe_logs(self, ws: web.WebSocketResponse) -> None:
        """Unsubscribe a WS client from log events."""
        self._ws_log_subscribers.discard(ws)

    def subscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        self._ws_subagent_subscribers.add(ws)

    def unsubscribe_subagents(self, ws: web.WebSocketResponse) -> None:
        self._ws_subagent_subscribers.discard(ws)

    def broadcast_ws_subagent_subscribers(self, msg_type: str, data: object) -> None:
        """Send to subagent-subscribed clients only (for heavy chunk data)."""
        if not self._ws_subagent_subscribers:
            return
        msg = json.dumps({"type": msg_type, "data": data})
        dead: list[web.WebSocketResponse] = []
        for ws in list(self._ws_subagent_subscribers):
            if ws.closed:
                dead.append(ws)
                continue
            try:
                self._spawn_ws_send(ws, msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._remove_ws(ws)

    async def close_all_ws(self) -> None:
        """Close all WebSocket connections (called on shutdown)."""
        if self._flush_task:
            self._flush_task.cancel()
            self._flush_task = None
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:
                pass
        self._ws_clients.clear()
        self._ws_log_subscribers.clear()
        self._ws_subagent_subscribers.clear()


# ── Notification persistence ──


def _notifications_path() -> Path:
    """Path to the notifications JSONL file."""
    return config_dir() / _NOTIFICATIONS_FILE


def _load_notifications() -> list[dict[str, Any]]:
    """Load persisted notifications from disk (newest last)."""
    path = _notifications_path()
    if not path.exists():
        return []
    try:
        entries: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        # Keep only the most recent N
        return entries[-_MAX_PERSISTED_NOTIFICATIONS:]
    except Exception:
        logger.debug("Failed to load notifications", exc_info=True)
        return []


def _persist_notification(note: dict[str, str]) -> None:
    """Append a single notification to the JSONL file on disk."""
    path = _notifications_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(note) + "\n")
        # Trim if file grows too large (keep last N lines)
        _maybe_trim_notifications(path)
    except Exception:
        logger.debug("Failed to persist notification", exc_info=True)


def _rewrite_notifications(notifications: list[dict[str, str]]) -> None:
    """Rewrite the entire notifications file from the in-memory list."""
    path = _notifications_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(n) + "\n" for n in notifications[-_MAX_PERSISTED_NOTIFICATIONS:]]
        path.write_text("".join(lines), encoding="utf-8")
    except Exception:
        logger.debug("Failed to rewrite notifications file", exc_info=True)


def _maybe_trim_notifications(path: Path) -> None:
    """Trim the notifications file if it exceeds 2x the max."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if len(lines) <= _MAX_PERSISTED_NOTIFICATIONS * 2:
            return
        kept = lines[-_MAX_PERSISTED_NOTIFICATIONS:]
        path.write_text("".join(kept), encoding="utf-8")
    except Exception:
        pass


def _fmt_duration(secs: int) -> str:
    """Format seconds as human-readable duration."""
    h, rem = divmod(secs, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m" if h > 0 else f"{m}m {s}s"
