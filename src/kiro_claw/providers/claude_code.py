"""Claude Code provider — communicates with the `claude` CLI.

Supports two connection modes:
  - per_session (default): Long-lived bidirectional process using
    --input-format=stream-json. Process spawns once at start(), messages
    sent as JSON lines to stdin, responses read from stdout. MCP servers
    stay warm across turns. True ACP-style session management.
  - ephemeral: One subprocess per message. No conversation history
    between messages. Simple, stateless.

Protocol (per_session):
  - Process: claude -p --verbose --input-format=stream-json
    --output-format=stream-json --permission-mode=<mode> [flags]
  - Input: {"type":"user","message":{"role":"user","content":"<text>"}}
  - Output: NDJSON events (system/init, assistant, stream_event, result)
  - Session persists in-process; --resume used on crash recovery

Protocol (ephemeral):
  - Process: claude -p - --output-format stream-json --verbose [flags]
  - stdin: raw message text, then close
  - stdout: NDJSON events
  - No session continuity
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from kiro_claw.acp.client import (
    _get_child_pids,
    _get_start_time,
    _kill_escaped_children,
)
from kiro_claw.env import augmented_path
from kiro_claw.providers.base import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    CancelOutcome,
    LLMEvent,
    LLMProvider,
)
from kiro_claw.providers.cleanup import _cleanup_cc_session
from kiro_claw.sandbox import wrap_argv
from kiro_claw.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_claw.sel import sel
from kiro_claw.session_pid import (
    _track_session_pid,
    _untrack_session_pid,
)

logger = logging.getLogger(__name__)

# Model → context window tokens (shared with bedrock provider)
_TOKENS_FILE = Path(__file__).resolve().parent.parent / "model_tokens.json"
_CONTEXT_WINDOWS: dict[str, int] = {}
if _TOKENS_FILE.exists():
    try:
        with open(_TOKENS_FILE) as _f:
            _CONTEXT_WINDOWS = {k: v for k, v in json.load(_f).items() if not k.startswith("_")}
    except (json.JSONDecodeError, OSError):
        pass

_DEFAULT_CONTEXT_WINDOW = 200_000

_CC_MODEL_ALIASES: dict[str, str] = {
    "claude-opus-4.7": "opus",
    "claude-opus-4.6": "opus",
    "claude-sonnet-4.6": "sonnet",
    "claude-opus-4.5": "claude-opus-4-5-20250414",
    "claude-sonnet-4.5": "claude-sonnet-4-5-20250514",
    "claude-sonnet-4.5-1m": "claude-sonnet-4-5-20250514",
    "claude-sonnet-4": "claude-sonnet-4-20250514",
    "claude-haiku-4.5": "claude-haiku-4-5-20241022",
    "auto": "",
}

_CC_VALID_MODELS = [
    {"model_name": "opus", "display_name": "Claude Opus (default)", "description": "Most capable, 1M context, maximum thinking"},
    {"model_name": "sonnet", "display_name": "Claude Sonnet", "description": "Balanced speed and quality"},
    {"model_name": "haiku", "display_name": "Claude Haiku", "description": "Fastest, most efficient"},
    {"model_name": "auto", "display_name": "Auto", "description": "Let Claude Code pick the model"},
]

# Opus 4.8 is enabled on Bedrock and is KiroClaw's default cc_model, but
# claude-agent-acp does not yet advertise it in its session model list — so the
# dashboard injects it explicitly. These are the exact IDs the picker accepts
# (``/model global.anthropic.claude-opus-4-8[1m]``). Remove once the adapter
# lists 4.8 natively.
_CC_OPUS_48_MODELS = [
    {
        "model_name": "global.anthropic.claude-opus-4-8[1m]",
        "display_name": "Opus 4.8 (1M context)",
        "description": "Most capable, 1M context window",
    },
    {
        "model_name": "global.anthropic.claude-opus-4-8",
        "display_name": "Opus 4.8 (200K context)",
        "description": "Most capable, 200K context window",
    },
]

# KiroClaw's resolved default CC model. The adapter, given an empty/unset
# model, falls back to its own ``models[0]`` — which on current claude-agent
# builds is an OLD Opus (4.1). So an empty ``cc_model`` must resolve to this
# explicit ID rather than being passed through as "" (see config.loader
# _claude_code factory and dashboard resolveModel).
_CC_DEFAULT_MODEL = "global.anthropic.claude-opus-4-8[1m]"

# KiroClaw-curated CC model list, shown FIRST in the dashboard dropdown ahead of
# whatever the adapter advertises (its set can be stale — e.g. listing Opus 4.1
# / Sonnet 4.5 on older claude-agent builds). Adapter-advertised models not
# covered here are appended after, de-duped, so nothing is hidden. Keep this
# current as new Bedrock models land.
#
# Ordered most-capable first, mirroring the set the `claude` CLI actually
# offers (Opus 4.8 1M, Opus 4.7 1M, Opus 4.6, Sonnet 4.6 1M).
#
# Model IDs MUST be full Bedrock inference-profile ids (``global.anthropic.…``),
# NOT bare versioned strings like ``claude-opus-4-7``. The bare form is NOT a
# valid Bedrock identifier: when it lands in settings.json ``availableModels``
# the adapter resolves it to itself and passes it straight to Bedrock, which
# rejects it with "400 The provided model identifier is invalid". (It only
# appeared to work before this list existed, because the older
# ``["opus","sonnet"]`` allowlist fuzzy-collapsed it to the valid ``opus``
# alias.) The ``[1m]`` suffix unlocks the 1M context window where the model
# supports it (Opus 4.7/4.8, Sonnet 4.6) — verified live against Bedrock.
#
# Opus 4.6 is intentionally omitted: neither ``global.anthropic.claude-opus-4-6``
# nor its ``[1m]`` form resolves on the shared Bedrock role (400 invalid / no
# modelUsage). The ``opus`` alias already resolves to the current flagship Opus
# at 1M, so a dedicated 4.6 row would only add a broken entry.
_CC_CURATED_MODELS = [
    *_CC_OPUS_48_MODELS,
    {
        "model_name": "global.anthropic.claude-opus-4-7[1m]",
        "display_name": "Opus 4.7 (1M context)",
        "description": "Most capable for complex work, 1M context",
    },
    {
        "model_name": "global.anthropic.claude-sonnet-4-6[1m]",
        "display_name": "Sonnet 4.6 (1M context)",
        "description": "Best for everyday tasks, 1M context",
    },
]


class ClaudeCodeProviderError(Exception):
    """Base exception for CC provider errors."""


class ClaudeCodeConnectionError(ClaudeCodeProviderError):
    """Claude CLI not found or failed to start."""


class ClaudeCodeProvider(LLMProvider):
    """LLMProvider backed by the `claude` CLI.

    Supports two connection modes:
      - per_session (default): Long-lived bidirectional process using
        --input-format=stream-json. Process spawns at start(), persists
        across messages. Instant response, MCP servers stay warm.
      - ephemeral: Fresh subprocess per message. No session continuity.
    """

    def __init__(
        self,
        work_dir: str | Path | None = None,
        model: str | None = None,
        agent: str | None = None,
        connection_mode: str = "per_session",
        permission_mode: str = "bypassPermissions",
        max_turns: int = 0,
        max_budget_usd: float = 0.0,
        bare: bool = False,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        hooks_manager: Any | None = None,
        security_deny_patterns: list[str] | None = None,
        channel_id: str | None = None,
        mcp_config: str | None = None,
        session_key: str | None = None,
        reasoning_effort: str = "",
    ) -> None:
        self._work_dir = Path(work_dir) if work_dir else Path.cwd()
        self._model = model
        self._agent = agent
        self._connection_mode = connection_mode if connection_mode in ("per_session", "ephemeral") else "per_session"
        self._permission_mode = permission_mode
        self._max_turns = max_turns
        self._max_budget_usd = max_budget_usd
        self._bare = bare
        self._allowed_tools = allowed_tools or []
        self._disallowed_tools = disallowed_tools or []
        self._deny_patterns = security_deny_patterns or []
        self._channel_id = channel_id
        self._mcp_config = mcp_config
        self._kiroclaw_session_key = session_key or ""
        # Defense-in-depth: validate at the subprocess boundary (BSC1).
        # Even if upstream callers pass a bad value, it cannot reach --effort.
        # Uses dynamically-reported ACP effort levels when available, with
        # hardcoded fallback for cold-start safety.
        _effort = reasoning_effort or ""
        if _effort:
            # circular import: chat_persistence → dashboard → session → providers.claude_code
            from kiro_claw.dashboard.chat_persistence import get_reasoning_effort_values

            # get_reasoning_effort_values() is the single source of truth: it is
            # seeded from effort.py's EFFORT_VALUES (incl. "xhigh") and only ever
            # widened by ACP-reported levels, so it is always a superset of the
            # static vocabulary — no separate hardcoded allowlist needed.
            if _effort not in get_reasoning_effort_values():
                logger.warning("Rejecting invalid reasoning_effort %r at subprocess boundary", _effort)
                _effort = ""
        self._reasoning_effort = _effort

        self._claude_bin: str | None = None
        self._session_id: str | None = None
        self._resume_sid: str | None = None
        self._last_context_pct: float = 0.0
        self._last_activity: float = 0.0
        self._started: bool = False
        # Long-lived process (per_session mode)
        self._proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None
        self._reader_task: asyncio.Task | None = None
        self._child_track_task: asyncio.Task | None = None
        self._background_tasks: set[asyncio.Task] = set()
        self._reconnect_lock: asyncio.Lock = asyncio.Lock()
        self._last_spawn_time: float = 0.0
        self._event_queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._init_event: asyncio.Event = asyncio.Event()
        self._turn_done: asyncio.Event = asyncio.Event()
        self._turn_in_progress: bool = False
        self._cancel_requested: bool = False
        # Ephemeral mode active subprocess
        self._active_proc: asyncio.subprocess.Process | None = None
        self._sandbox_cleanup: str | None = None
        self._slash_commands: list[str] = []
        self._tools: list[str] = []
        self._mcp_servers: list[dict] = []
        self._context_window_tokens: int = self._resolve_context_window(model)

    @property
    def session_id(self) -> str:
        """Return the Claude Code session ID."""
        return self._session_id if self._session_id else ""

    @property
    def connection_mode(self) -> str:
        return self._connection_mode

    @property
    def cwd(self) -> str:
        """Working directory this provider operates in."""
        return str(self._work_dir)

    async def cleanup_session(self, session_id: str) -> None:
        """Delete Claude Code session files (JSONL transcript + artifacts).

        Deletes:
          - ~/.claude/projects/<encoded-cwd>/<sid>.jsonl
          - ~/.claude/projects/<encoded-cwd>/<sid>/ (subagents + tool-results)
          - ~/.claude/file-history/<sid>/

        Never deletes memory/ (session-shared, not per-session).
        """
        _cleanup_cc_session(self._work_dir, session_id)

    @property
    def was_resumed(self) -> bool:
        """True if this process was started with --resume (has prior history)."""
        return self._resume_sid is not None

    # ── LLMProvider ABC ──

    async def start(self) -> None:
        search_path = augmented_path(os.environ.get("PATH", ""))
        self._claude_bin = shutil.which("claude", path=search_path)
        if not self._claude_bin:
            raise ClaudeCodeConnectionError(
                "claude CLI not found. Install Claude Code (https://www.anthropic.com/claude-code) "
                "or install the claude-agent-acp npm package."
            )
        if self._resume_sid:
            self._session_id = self._resume_sid

        if self._connection_mode == "per_session":
            await self._spawn_persistent_process()

        self._started = True
        self._last_activity = time.monotonic()
        logger.info(
            "ClaudeCodeProvider started: bin=%s model=%s mode=%s session=%s",
            self._claude_bin,
            self._model,
            self._connection_mode,
            self._session_id,
        )

    async def shutdown(self) -> None:
        self._started = False
        if self._connection_mode == "per_session":
            await self._kill_persistent_process()
        else:
            if self._active_proc is not None:
                try:
                    self._active_proc.kill()
                    await self._active_proc.wait()
                except Exception:
                    pass
                self._active_proc = None

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        if not self._started:
            raise ClaudeCodeProviderError("Provider not started — call start() first")

        self._last_activity = time.monotonic()
        message = self._rewrite_file_references(message)

        if self._connection_mode == "per_session":
            async for event in self._stream_persistent(message):
                yield event
        else:
            async for event in self._stream_ephemeral(message):
                yield event

    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> None:
        """No-op — permissions handled by --permission-mode flag."""

    async def reject_tool(self, request_id: str | int) -> None:
        """No-op — permissions handled by --permission-mode/--disallowedTools."""

    def context_usage_pct(self) -> float:
        return self._last_context_pct

    async def stream_command(self, command: str) -> AsyncIterator[LLMEvent]:
        async for event in self.stream(command):
            yield event

    async def compact(self, context: str = "") -> None:
        if not self._started:
            return
        async for _ in self.stream("/compact"):
            pass

    async def wait_for_compaction(self, timeout: float = 120.0) -> dict:
        return {"type": "completed", "summary": "CC auto-compaction"}

    async def cancel(self, *, wait_ack_timeout: float = 0.0) -> CancelOutcome:
        if self._connection_mode == "per_session":
            return await self._cancel_persistent()
        return await self._cancel_ephemeral()

    def is_alive(self) -> bool:
        if self._connection_mode == "per_session":
            return self._started and self._proc is not None and self._proc.returncode is None
        return self._started

    def is_process_alive(self) -> bool:
        if self._connection_mode == "per_session":
            return self._proc is not None and self._proc.returncode is None
        if self._active_proc is not None:
            return self._active_proc.returncode is None
        return self._started

    @property
    def exit_code(self) -> int | None:
        if self._proc is not None:
            return self._proc.returncode
        return None

    def touch_activity(self) -> None:
        self._last_activity = time.monotonic()

    def set_resume_session_id(self, session_id: str) -> None:
        self._resume_sid = session_id

    # ══════════════════════════════════════════════════════════════════════
    # Per-Session Mode (long-lived bidirectional process)
    # ══════════════════════════════════════════════════════════════════════

    async def _spawn_persistent_process(self) -> None:
        """Spawn the long-lived claude process with stream-json I/O."""
        cmd = self._build_persistent_command()
        cmd, self._sandbox_cleanup = wrap_argv(cmd, mode="cc")

        self._init_event.clear()
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._work_dir) if self._work_dir.is_dir() else None,
            env=self._build_env(),
            limit=10 * 1024 * 1024,
            start_new_session=True,  # setsid: enables killpg for full tree cleanup
        )
        self._proc_pid = self._proc.pid
        self._last_spawn_time = time.monotonic()
        _track_session_pid(self._proc.pid)
        self._child_pids: dict[int, int | None] = {}
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._proc))
        self._reader_task = asyncio.create_task(self._stdout_reader_loop())
        # Schedule child discovery after CC has time to spawn MCP servers
        self._child_track_task = asyncio.create_task(self._discover_children())
        # Note: init event fires after first message is sent (CC defers init
        # until first input with --input-format=stream-json). We wait for it
        # in _stream_persistent() instead of here.

    async def _discover_children(self) -> None:
        """Discover and track child processes spawned by Claude Code (builder-mcp, etc.).

        Mirrors Kiro/ACP's two-phase approach:
        1. Early scan (0.3s) — catches fast-forking children
        2. Post-init scans (5s, 15s, 30s) — catches MCP servers spawned after
           the first message triggers CC initialization

        CC defers process spawning until the first user message (unlike Kiro
        which spawns MCP servers at start()), so we scan multiple times.
        """
        for delay in (0.3, 5, 15, 30):
            await asyncio.sleep(delay)
            if not self._proc or self._proc.returncode is not None:
                return
            try:
                descendants = _get_child_pids(self._proc.pid)
                if not descendants and delay < 1:
                    # Retry once like Kiro does for early scan
                    await asyncio.sleep(0.5)
                    descendants = _get_child_pids(self._proc.pid)
                new_count = 0
                for p in descendants:
                    if p not in self._child_pids:
                        self._child_pids[p] = _get_start_time(p)
                        new_count += 1
                # Children are tracked in-memory only and killed via
                # _kill_escaped_children on shutdown; no separate pid file.
                if self._child_pids and new_count:
                    logger.debug(
                        "CC child tracking: %d total (%d new) descendants of PID %d",
                        len(self._child_pids), new_count, self._proc.pid,
                    )
            except Exception:
                logger.debug("CC child discovery failed", exc_info=True)

    async def _kill_persistent_process(self) -> None:
        """Gracefully shut down the long-lived process and its entire tree."""
        if self._child_track_task:
            self._child_track_task.cancel()
            try:
                await self._child_track_task
            except (asyncio.CancelledError, Exception):
                pass
            self._child_track_task = None

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reader_task = None

        if self._stderr_task:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass
            self._stderr_task = None

        if self._proc and self._proc.returncode is None:
            pgid = None
            try:
                pgid = os.getpgid(self._proc.pid)
            except (OSError, ProcessLookupError):
                pass

            # CC does NOT exit on stdin close in stream-json mode.
            # Send SIGTERM first for graceful shutdown.
            try:
                self._proc.terminate()
            except (OSError, ProcessLookupError):
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                # SIGTERM didn't work — kill entire process group
                if pgid and pgid != os.getpgid(0):
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except (OSError, ProcessLookupError):
                        pass
                else:
                    self._proc.kill()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    pass

            # Defense-in-depth: even when SIGTERM exited the parent
            # cleanly, MCP server children sometimes survive because
            # claude-code closes their stdin asynchronously and the MCP
            # binaries don't always exit on stdin close.  Reaping the
            # whole process group post-exit catches those stragglers
            # without affecting healthy sessions (we just spawned with
            # start_new_session=True so this group is ours alone).
            if pgid and pgid != os.getpgid(0):
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass  # already empty

        self._proc = None

        # Kill escaped children (builder-mcp, otelcol-contrib) that survived
        # killpg because they set their own PGID.
        # Fresh scan + merge with stored snapshot (same as Kiro/ACP).
        if hasattr(self, '_child_pids'):
            if hasattr(self, '_proc_pid'):
                fresh = _get_child_pids(self._proc_pid)
                for p in fresh:
                    if p not in self._child_pids:
                        self._child_pids[p] = _get_start_time(p)
            if self._child_pids:
                _kill_escaped_children(self._child_pids)
                logger.debug("CC shutdown: killed %d escaped children", len(self._child_pids))
                self._child_pids = {}

        # Untrack session PID
        if hasattr(self, '_proc_pid'):
            _untrack_session_pid(self._proc_pid)

        if self._sandbox_cleanup:
            try:
                os.remove(self._sandbox_cleanup)
            except OSError:
                pass
            self._sandbox_cleanup = None

    async def _reconnect(self) -> None:
        """Restart the persistent process after crash."""
        logger.warning("CC process died, reconnecting (session=%s)...", self._session_id)
        await self._kill_persistent_process()
        # Replace queue so any stale puts from old reader's finally block
        # land on a discarded instance instead of polluting the new session.
        self._event_queue = asyncio.Queue()
        if self._session_id:
            self._resume_sid = self._session_id
        await self._spawn_persistent_process()

    _EAGER_RECONNECT_MIN_INTERVAL = 5.0

    async def _eager_reconnect(self) -> None:
        """Background reconnect triggered by reader loop on unexpected death.

        Restores MCP servers immediately so the user doesn't see a prolonged
        "tools disconnected" state.  Serialized via _reconnect_lock to prevent
        racing with _stream_persistent's own reconnect path.
        Rate-limited to avoid tight crash loops when the binary is broken.
        """
        if time.monotonic() - self._last_spawn_time < self._EAGER_RECONNECT_MIN_INTERVAL:
            logger.warning(
                "Skipping eager reconnect: process died too quickly (session=%s)",
                self._session_id,
            )
            return
        async with self._reconnect_lock:
            if self._proc and self._proc.returncode is None:
                return  # already reconnected by the other path
            try:
                await self._reconnect()
                logger.info("Eager reconnect succeeded (session=%s)", self._session_id)
            except Exception:
                logger.warning("Eager reconnect failed (session=%s)", self._session_id, exc_info=True)

    async def _stream_persistent(self, message: str) -> AsyncIterator[LLMEvent]:
        """Send message to long-lived process and yield response events."""
        if not self._proc or self._proc.returncode is not None:
            async with self._reconnect_lock:
                if not self._proc or self._proc.returncode is not None:
                    await self._reconnect()

        self._turn_done.clear()
        self._cancel_requested = False
        # Drain any stale events from previous turns
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

        # Send message as stream-json input
        msg_json = json.dumps({"type": "user", "message": {"role": "user", "content": message}})
        self._turn_in_progress = True
        try:
            self._proc.stdin.write((msg_json + "\n").encode("utf-8"))  # type: ignore[union-attr]
            await self._proc.stdin.drain()  # type: ignore[union-attr]
        except (BrokenPipeError, ConnectionResetError, OSError):
            async with self._reconnect_lock:
                if not self._proc or self._proc.returncode is not None:
                    await self._reconnect()
            try:
                self._proc.stdin.write((msg_json + "\n").encode("utf-8"))  # type: ignore[union-attr]
                await self._proc.stdin.drain()  # type: ignore[union-attr]
            except (BrokenPipeError, ConnectionResetError, OSError):
                self._turn_in_progress = False
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="error: reconnect failed")
                return

        # Track whether we've streamed partial content (to skip duplicate
        # full-message assistant events that repeat the same text)
        streamed_partial = False

        # Yield events until result.
        # Timeout matches Claude Code's API_TIMEOUT_MS default (600s / 10 min).
        # CC will emit 'error: response timeout' before this fires in normal
        # operation; this is a fallback for frozen processes.
        while True:
            try:
                data = await asyncio.wait_for(self._event_queue.get(), timeout=600.0)
            except asyncio.TimeoutError:
                self._turn_in_progress = False
                if self._proc and self._proc.returncode is None:
                    self._proc.kill()
                    await self._proc.wait()
                async with self._reconnect_lock:
                    if not self._proc or self._proc.returncode is not None:
                        await self._reconnect()
                yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="error: response timeout")
                return
            except asyncio.CancelledError:
                raise

            if data is None:
                self._turn_in_progress = False
                if self._cancel_requested:
                    self._cancel_requested = False
                    yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="cancelled")
                else:
                    yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="error: process died")
                return

            event_type = data.get("type")

            if event_type == "stream_event":
                event = self._parse_event(data)
                if event is not None:
                    streamed_partial = True
                    yield event
            elif event_type == "assistant":
                # Always yield tool_call/tool_result; only suppress duplicate
                # text/thinking when we already streamed partials for them.
                for ev in self._parse_assistant_events(data, skip_text=streamed_partial):
                    yield ev
            elif event_type == "result":
                event = self._parse_result_event(data)
                yield event
                return
            else:
                event = self._parse_event(data)
                if event is not None:
                    yield event
                    if event.kind == EVENT_COMPLETE:
                        return

    async def _stdout_reader_loop(self) -> None:
        """Background task: read NDJSON from long-lived process stdout."""
        assert self._proc and self._proc.stdout
        cancelled = False
        try:
            async for raw_line in self._proc.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                self._last_activity = time.monotonic()
                event_type = data.get("type")

                if event_type == "system":
                    self._handle_system_event(data)
                    continue

                await self._event_queue.put(data)
        except asyncio.CancelledError:
            cancelled = True
        except Exception as exc:
            logger.debug("CC reader loop ended: %s", exc)
        finally:
            try:
                await self._event_queue.put(None)
            except Exception:
                pass
            # Eager reconnect: if process died (not cancelled by us), spawn
            # a fresh CC process immediately so MCP servers come back without
            # waiting for the next user message.
            if not cancelled and self._proc and self._proc.returncode is not None:
                task = asyncio.create_task(self._eager_reconnect())
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)

    def _handle_system_event(self, data: dict) -> None:
        """Handle system events (init, etc.) from the persistent process."""
        subtype = data.get("subtype")
        if subtype == "init":
            sid = data.get("session_id")
            if sid:
                self._session_id = sid
            model = data.get("model", "")
            if model and not self._model:
                self._model = model
                self._context_window_tokens = self._resolve_context_window(model)
            self._slash_commands = data.get("slash_commands", [])
            self._tools = data.get("tools", [])
            self._mcp_servers = data.get("mcp_servers", [])
            self._init_event.set()

    async def _cancel_persistent(self) -> CancelOutcome:
        """Cancel in-flight turn on persistent process via SIGINT."""
        if self._proc is None or self._proc.returncode is not None:
            return "no_turn"
        if not self._turn_in_progress:
            return "no_turn"
        self._cancel_requested = True
        try:
            # SIGINT to process only (not group) — CC handles it cooperatively
            self._proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(self._turn_done.wait(), timeout=5.0)
                return "acked"
            except asyncio.TimeoutError:
                # Process didn't respond to SIGINT — force kill and reconnect
                await self._kill_persistent_process()
                return "acked"
        except Exception:
            return "error"

    def _build_persistent_command(self) -> list[str]:
        """Build command for long-lived bidirectional stream-json process."""
        cmd = [
            self._claude_bin or "claude",
            "-p",
            "--verbose",
            "--input-format=stream-json",
            "--output-format=stream-json",
            "--include-partial-messages",
        ]

        if self._bare:
            cmd.append("--bare")

        resolved_model = self._resolve_model_flag()
        if resolved_model:
            cmd.extend(["--model", resolved_model])

        if self._reasoning_effort:
            cmd.extend(["--effort", self._reasoning_effort])
        elif not resolved_model or "opus" in resolved_model:
            cmd.extend(["--effort", "max"])

        if self._agent:
            cmd.extend(["--agent", self._agent])

        if self._resume_sid:
            cmd.extend(["--resume", self._resume_sid])

        if self._permission_mode:
            cmd.extend(["--permission-mode", self._permission_mode])

        if self._max_budget_usd > 0:
            cmd.extend(["--max-budget-usd", str(self._max_budget_usd)])

        if self._allowed_tools:
            cmd.extend(["--allowedTools", ",".join(self._allowed_tools)])

        if self._disallowed_tools or self._deny_patterns:
            all_denied = list(dict.fromkeys(self._disallowed_tools + self._deny_patterns))
            cmd.extend(["--disallowedTools", ",".join(all_denied)])

        if self._mcp_config:
            cmd.extend(["--mcp-config", self._mcp_config])

        cmd.extend(["--settings", '{"autoMemoryEnabled": false}'])

        return cmd

    # ══════════════════════════════════════════════════════════════════════
    # Ephemeral Mode (one subprocess per message)
    # ══════════════════════════════════════════════════════════════════════

    async def _stream_ephemeral(self, message: str) -> AsyncIterator[LLMEvent]:
        """Spawn a fresh subprocess for a single message."""
        cmd = self._build_ephemeral_command()
        cmd, sandbox_cleanup = wrap_argv(cmd, mode="cc")

        stderr_task = None
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._work_dir) if self._work_dir.is_dir() else None,
                env=self._build_env(),
                limit=10 * 1024 * 1024,
            )
            proc.stdin.write(message.encode("utf-8"))  # type: ignore[union-attr]
            await proc.stdin.drain()  # type: ignore[union-attr]
            proc.stdin.close()  # type: ignore[union-attr]
            self._active_proc = proc

            stderr_task = asyncio.create_task(self._drain_stderr(proc))

            async for event in self._read_events_from_proc(proc):
                yield event

            await proc.wait()

        except asyncio.CancelledError:
            if proc and proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        except Exception as exc:
            logger.error("CC ephemeral stream error: %s", exc, exc_info=True)
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="error: internal provider error")
        finally:
            if stderr_task is not None:
                stderr_task.cancel()
                try:
                    await stderr_task
                except (asyncio.CancelledError, Exception):
                    pass
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
            self._active_proc = None
            if sandbox_cleanup:
                try:
                    os.remove(sandbox_cleanup)
                except OSError:
                    pass

    async def _cancel_ephemeral(self) -> CancelOutcome:
        proc = self._active_proc
        if proc is not None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5.0)
                self._active_proc = None
                return "acked"
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                self._active_proc = None
                return "acked"
            except Exception:
                return "error"
        return "no_turn"

    def _build_ephemeral_command(self) -> list[str]:
        """Build command for a single-message subprocess."""
        cmd = [
            self._claude_bin or "claude",
            "-p",
            "-",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]

        if self._bare:
            cmd.append("--bare")

        resolved_model = self._resolve_model_flag()
        if resolved_model:
            cmd.extend(["--model", resolved_model])

        if self._reasoning_effort:
            cmd.extend(["--effort", self._reasoning_effort])
        elif not resolved_model or "opus" in resolved_model:
            cmd.extend(["--effort", "max"])

        if self._agent:
            cmd.extend(["--agent", self._agent])

        if self._permission_mode:
            cmd.extend(["--permission-mode", self._permission_mode])

        if self._max_turns > 0:
            cmd.extend(["--max-turns", str(self._max_turns)])

        if self._max_budget_usd > 0:
            cmd.extend(["--max-budget-usd", str(self._max_budget_usd)])

        if self._allowed_tools:
            cmd.extend(["--allowedTools", ",".join(self._allowed_tools)])

        if self._disallowed_tools or self._deny_patterns:
            all_denied = list(dict.fromkeys(self._disallowed_tools + self._deny_patterns))
            cmd.extend(["--disallowedTools", ",".join(all_denied)])

        if self._mcp_config:
            cmd.extend(["--mcp-config", self._mcp_config])

        cmd.extend(["--settings", '{"autoMemoryEnabled": false}'])

        return cmd

    # ══════════════════════════════════════════════════════════════════════
    # Shared Event Parsing
    # ══════════════════════════════════════════════════════════════════════

    def _parse_event(self, data: dict) -> LLMEvent | None:
        """Parse a single NDJSON event dict into an LLMEvent (or None to skip)."""
        event_type = data.get("type")

        if event_type == "stream_event":
            inner = data.get("event", {})
            inner_type = inner.get("type")
            if inner_type == "content_block_delta":
                delta = inner.get("delta", {})
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    raw = delta.get("text", "")
                    if raw:
                        cleaned, _ = redact_exfiltration_urls(raw)
                        cleaned, _ = redact_credentials(cleaned)
                        return LLMEvent(kind=EVENT_TEXT_CHUNK, text=cleaned, context_usage_pct=self._last_context_pct)
                elif delta_type == "thinking_delta":
                    raw = delta.get("thinking", "")
                    if raw:
                        cleaned, _ = redact_exfiltration_urls(raw)
                        cleaned, _ = redact_credentials(cleaned)
                        return LLMEvent(kind=EVENT_THINKING_CHUNK, text=cleaned, context_usage_pct=self._last_context_pct)
            return None

        if event_type == "assistant":
            events = self._parse_assistant_events(data)
            return events[0] if events else None

        if event_type == "result":
            return self._parse_result_event(data)

        return None

    def _parse_assistant_events(self, data: dict, skip_text: bool = False) -> list[LLMEvent]:
        """Parse an assistant event — returns all block events.

        When skip_text=True, text/thinking blocks are suppressed (already
        streamed via stream_event deltas) but tool_call/tool_result are
        always emitted so tool activity is visible in the UI.
        """
        msg = data.get("message", {})
        content_blocks = msg.get("content", [])

        # Per-turn context usage from assistant event (authoritative source).
        # result event reports cumulative totals across all internal turns —
        # this is the per-turn value that reflects actual context window fill.
        usage = msg.get("usage", {})
        if usage:
            input_tokens = usage.get("input_tokens", 0) or 0
            cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
            cache_read = usage.get("cache_read_input_tokens", 0) or 0
            total_ctx = input_tokens + cache_creation + cache_read
            if total_ctx > 0 and self._context_window_tokens > 0:
                self._last_context_pct = min(
                    (total_ctx / self._context_window_tokens) * 100, 99.0
                )

        events: list[LLMEvent] = []
        for block in content_blocks:
            block_type = block.get("type")
            if block_type == "thinking":
                if not skip_text:
                    raw = block.get("thinking", "")
                    if raw:
                        cleaned, _ = redact_exfiltration_urls(raw)
                        cleaned, _ = redact_credentials(cleaned)
                        events.append(LLMEvent(kind=EVENT_THINKING_CHUNK, text=cleaned, context_usage_pct=self._last_context_pct))
            elif block_type == "text":
                if not skip_text:
                    raw = block.get("text", "")
                    if raw:
                        cleaned, _ = redact_exfiltration_urls(raw)
                        cleaned, _ = redact_credentials(cleaned)
                        events.append(LLMEvent(kind=EVENT_TEXT_CHUNK, text=cleaned, context_usage_pct=self._last_context_pct))
            elif block_type == "tool_use":
                tid = block.get("id", "")
                name = block.get("name", "")
                inp = block.get("input", {})
                raw_input = json.dumps(inp) if inp else ""
                cleaned_input, _ = redact_exfiltration_urls(raw_input)
                cleaned_input, _ = redact_credentials(cleaned_input)
                sel().log_tool_invocation(
                    session_key=self._session_id or "",
                    agent="claude_code",
                    source="cc_provider",
                    tool_name=name,
                    tool_kind=name,
                    outcome=f"auto_approved:{self._permission_mode}",
                    request_id=tid,
                )
                events.append(LLMEvent(
                    kind=EVENT_TOOL_CALL,
                    tool_call_id=tid,
                    title=name,
                    tool_kind=name,
                    tool_input=cleaned_input,
                ))
            elif block_type == "tool_result":
                tid = block.get("tool_use_id", "")
                output = block.get("content", "")
                if not isinstance(output, str):
                    output = json.dumps(output)
                cleaned, _ = redact_exfiltration_urls(output)
                cleaned, _ = redact_credentials(cleaned)
                events.append(LLMEvent(kind=EVENT_TOOL_RESULT, tool_call_id=tid, tool_output=cleaned))

        return events

    def _parse_result_event(self, data: dict) -> LLMEvent:
        """Parse a result event (turn complete)."""
        stop = data.get("stop_reason", "end_turn") or "end_turn"
        sid = data.get("session_id")
        if sid:
            self._session_id = sid
        usage = data.get("usage", {})
        input_tokens = usage.get("input_tokens", 0)
        output_tokens = usage.get("output_tokens", 0)
        cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)
        cache_read_tokens = usage.get("cache_read_input_tokens", 0)
        cost_usd = data.get("total_cost_usd", 0.0) or 0.0
        num_turns = data.get("num_turns", 0) or 0
        duration_ms = data.get("duration_ms", 0) or 0
        # Update context window size from modelUsage (authoritative).
        # Do NOT compute context % here — result event usage is cumulative
        # across all internal turns within one invocation. Per-turn % is
        # set from "assistant" events in _parse_assistant_events().
        model_usage = data.get("modelUsage", {})
        for _model_id, mu in model_usage.items():
            cw = mu.get("contextWindow", 0)
            if cw > 0:
                self._context_window_tokens = cw
            break

        self._turn_done.set()
        self._turn_in_progress = False
        return LLMEvent(
            kind=EVENT_COMPLETE,
            stop_reason=stop,
            context_usage_pct=self._last_context_pct,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_tokens=cache_creation_tokens,
            cache_read_tokens=cache_read_tokens,
            cost_usd=cost_usd,
            num_turns=num_turns,
            duration_ms=duration_ms,
        )

    async def _read_events_from_proc(self, proc: asyncio.subprocess.Process) -> AsyncIterator[LLMEvent]:
        """Parse NDJSON from a short-lived process stdout (ephemeral mode)."""
        assert proc.stdout is not None
        _streamed_partial = False

        async for raw_line in proc.stdout:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            self._last_activity = time.monotonic()
            event_type = data.get("type")

            if event_type == "system":
                self._handle_system_event(data)
                continue

            if event_type == "stream_event":
                event = self._parse_event(data)
                if event:
                    _streamed_partial = True
                    yield event
                continue

            if event_type == "assistant":
                for ev in self._parse_assistant_events(data, skip_text=_streamed_partial):
                    yield ev
                continue

            if event_type == "result":
                event = self._parse_result_event(data)
                yield event
                return

        yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="error: process ended without result")

    # ══════════════════════════════════════════════════════════════════════
    # Shared Helpers
    # ══════════════════════════════════════════════════════════════════════

    def _resolve_model_flag(self) -> str:
        """Resolve model name to CC CLI flag value."""
        if not self._model:
            return ""
        if self._model in _CC_MODEL_ALIASES:
            return _CC_MODEL_ALIASES[self._model]
        if "claude" in self._model.lower() or self._model in ("opus", "sonnet", "haiku"):
            return self._model
        logger.warning("Model %r not supported by CC, using default", self._model)
        return ""

    def _build_env(self) -> dict[str, str]:
        """Build environment for the subprocess."""
        env = dict(os.environ)
        if self._channel_id:
            env["KIROCLAW_CHANNEL_ID"] = self._channel_id
        if self._kiroclaw_session_key:
            env["KIROCLAW_SESSION_KEY"] = self._kiroclaw_session_key
        return env

    _IMAGE_EXT = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
    _FILE_TOKEN_RE = re.compile(r"\[attached_file \d+\] (/\S+)")
    _IMG_MD_RE = re.compile(r"!\[image\]\((/[^)]+)\)")

    @staticmethod
    def _rewrite_file_references(message: str) -> str:
        """Rewrite image/file references into Read tool instructions for CC."""
        img_paths: list[str] = []
        for m in ClaudeCodeProvider._IMG_MD_RE.finditer(message):
            p = m.group(1)
            if Path(p).suffix.lower() in ClaudeCodeProvider._IMAGE_EXT:
                img_paths.append(p)
        message = ClaudeCodeProvider._IMG_MD_RE.sub(
            lambda m: f"[Image attached: {m.group(1)}]", message
        )
        file_paths: list[str] = []
        for m in ClaudeCodeProvider._FILE_TOKEN_RE.finditer(message):
            file_paths.append(m.group(1))
        message = ClaudeCodeProvider._FILE_TOKEN_RE.sub(
            lambda m: f"[File attached: {m.group(1)}]", message
        )
        img_paths = [p for p in img_paths if not is_sensitive_path(p)]
        file_paths = [p for p in file_paths if not is_sensitive_path(p)]
        if img_paths or file_paths:
            all_paths = img_paths + file_paths
            message += (
                "\n\n[SYSTEM: The user attached files. "
                "Use the Read tool to view them: "
                + ", ".join(all_paths) + "]"
            )
        return message

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
        """Read and discard stderr to prevent pipe buffer deadlock."""
        assert proc.stderr is not None
        try:
            async for raw_line in proc.stderr:
                line = raw_line.decode("utf-8", errors="replace").rstrip()
                if line:
                    logger.debug("claude stderr: %s", line)
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    @staticmethod
    def _resolve_context_window(model: str | None) -> int:
        if not model:
            return _DEFAULT_CONTEXT_WINDOW
        if model in _CONTEXT_WINDOWS:
            return _CONTEXT_WINDOWS[model]
        model_lower = model.lower()
        for key, tokens in sorted(_CONTEXT_WINDOWS.items(), key=lambda kv: len(kv[0]), reverse=True):
            if model_lower in key.lower() or key.lower() in model_lower:
                return tokens
        return _DEFAULT_CONTEXT_WINDOW
