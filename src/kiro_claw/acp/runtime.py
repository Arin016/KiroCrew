"""ACP Runtime for multiplexed kiro-cli sessions.

Single-reader demux architecture: one AcpRuntime owns the subprocess and a
reader task that routes frames by sessionId to per-session queues. Each
``AcpSessionHandle`` (in ``session_handle.py``) owns one sessionId + queue and
provides the prompt/cancel/approve/reject API.

The per-session handle, the runtime protocol it depends on, and the runtime
exceptions live in ``session_handle.py`` (the lower layer); they are re-exported
here so ``from kiro_claw.acp.runtime import AcpSessionHandle`` (and the
exceptions) keeps working for existing callers and tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any

from kiro_claw.acp._dispatch import (
    build_session_new_params,
    set_mode_params,
)
from kiro_claw.acp.client import _NOT_LOGGED_IN_RE, _get_start_time
from kiro_claw.acp.session_handle import (
    AcpRuntimeDead,
    AcpRuntimeError,
    AcpRuntimeProtocol,
    AcpSessionHandle,
)
from kiro_claw.acp.types import (
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SET_MODE,
    JsonRpcMessage,
    JsonRpcRequest,
)
from kiro_claw.env import augmented_path, resolve_krb5_ccname
from kiro_claw.sandbox import wrap_argv
from kiro_claw.session_pid import (
    _track_pid,
    _track_session_pid,
    _untrack_pid,
    _untrack_session_pid,
    register_protected_pid,
    unregister_protected_pid,
)

logger = logging.getLogger(__name__)

__all__ = [
    "AcpRuntime",
    "AcpRuntimeError",
    "AcpRuntimeDead",
    "AcpRuntimeProtocol",
    "AcpSessionHandle",
]


# ── AcpRuntime ──

_STDOUT_BUFFER_LIMIT = 10 * 1024 * 1024  # 10MB
_INIT_TIMEOUT = 30.0
_REQUEST_TIMEOUT = 30.0

KIRO_CLI_BIN = "kiro-cli"
KIRO_CLI_SUBCMD = "acp"
CLIENT_NAME = "kiroclaw"
CLIENT_VERSION = "0.1.2"
PROTOCOL_VERSION = "2025-08-22"


def _resolve_kiro_bin() -> str | None:
    """Find kiro-cli on PATH."""
    return shutil.which(KIRO_CLI_BIN)


class AcpRuntime:
    """Owns one kiro-cli acp subprocess with single-reader demux.

    The _reader_task is the ONLY coroutine that reads from stdout.
    It routes frames by:
      - 'id' field in _pending_requests → resolve Future (for send_and_await)
      - 'id' field in _routed_requests → put in session queue (for prompt responses)
      - params.sessionId → _session_queues[sessionId].put(msg)
      - no sessionId → broadcast to all session queues
    """

    def __init__(
        self,
        work_dir: str | Path | None = None,
        agent: str = CLIENT_NAME,
        sandbox_mode: str = "auto",
        extra_env: dict[str, str] | None = None,
        mcp_gateway_overlay: str | Path | None = None,
        mcp_gateway_settings_mcp_json: str | Path | None = None,
        mcp_gateway_socket: str | Path | None = None,
    ):
        self._work_dir = Path(work_dir) if work_dir else Path.home() / ".kiroclaw" / "workspace"
        self._agent = agent
        self._sandbox_mode = sandbox_mode
        self._extra_env = extra_env or {}
        self._mcp_gateway_overlay = str(mcp_gateway_overlay) if mcp_gateway_overlay else None
        self._mcp_gateway_settings_mcp_json = (
            str(mcp_gateway_settings_mcp_json) if mcp_gateway_settings_mcp_json else None
        )
        self._mcp_gateway_socket = str(mcp_gateway_socket) if mcp_gateway_socket else None
        self._sandbox_cleanup: str | None = None

        # Process state
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        self._start_time: int | None = None
        self._child_pids: dict[int, int | None] = {}

        # Single reader task — the ONLY coroutine that reads stdout
        self._reader_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]

        # Demux routing
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        # Maps req_id → sessionId for responses that should be routed to a session queue
        # (e.g. session/prompt response signals turn completion and must reach the session)
        self._routed_requests: dict[int, str] = {}
        self._session_queues: dict[str, asyncio.Queue[JsonRpcMessage | None]] = {}
        self._next_id = 1
        self._initialized = False
        # Whether kiro-cli advertised session/load support in its initialize
        # response. Mirrors AcpClient._can_load_session — load_session() guards
        # on it so we never issue session/load against a backend that lacks it.
        self._can_load_session = False
        self._dead = False
        self._last_activity: float = 0.0
        self._stderr_lines: list[str] = []

    @property
    def pid(self) -> int | None:
        return self._pid

    def is_alive(self) -> bool:
        """True if the underlying process exists and has not exited."""
        return (
            self._process is not None
            and self._process.returncode is None
            and not self._dead
        )

    # ── Lifecycle ──

    async def spawn(self) -> None:
        """Start the kiro-cli acp subprocess and complete protocol handshake."""
        if self._process is not None:
            raise AcpRuntimeError("Runtime already spawned")

        self._work_dir.mkdir(parents=True, exist_ok=True)

        kiro_bin = _resolve_kiro_bin()
        if not kiro_bin:
            raise AcpRuntimeError(f"{KIRO_CLI_BIN} not found in PATH")

        argv: list[str] = [kiro_bin, KIRO_CLI_SUBCMD, "--agent", self._agent]

        # OSS sandbox.wrap_argv takes (argv, mode) only — the MCP-gateway
        # overlay / gateway-socket / strip-python-env options are an internal
        # feature not present in kiro_claw's sandbox, so (matching AcpClient) we
        # apply only the sandbox mode here. The _mcp_gateway_* attrs are retained
        # for constructor/caller compatibility but are inert under this sandbox.
        argv, self._sandbox_cleanup = wrap_argv(argv, mode=self._sandbox_mode)

        env = {**os.environ}
        if self._extra_env:
            env.update(self._extra_env)

        env["PATH"] = augmented_path(env.get("PATH", ""))
        resolve_krb5_ccname(env)

        self._process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._work_dir),
            limit=_STDOUT_BUFFER_LIMIT,
            start_new_session=True,
            env=env,
        )
        self._pid = self._process.pid
        self._start_time = _get_start_time(self._pid)
        self._last_activity = time.monotonic()
        logger.info("AcpRuntime spawned kiro-cli acp (PID %d)", self._pid)

        # Track the PID for orphan cleanup (mirrors AcpClient._spawn). Without
        # this, a kiro-cli process leaked by a gateway crash/restart is never
        # recorded in kiro_session_pids.txt, so startup cleanup can't reap it.
        # A LIVE runtime is already protected during the periodic sweep because
        # AcpSessionProvider._pid feeds _collect_active_pids — this only closes
        # the cross-restart leak.
        try:
            _track_pid(self._pid)
            _track_session_pid(self._pid)
            # Shield this shared runtime's PID from the periodic orphan sweep.
            # _bg_runtime and companion subagent runtimes are held only in
            # SessionManager instance attributes (not registered sessions /
            # warm-pool providers), so _collect_active_pids would otherwise
            # classify them as orphans and SIGKILL them mid-use.
            register_protected_pid(self._pid)
        except Exception:
            logger.debug("AcpRuntime: PID tracking failed for %s", self._pid, exc_info=True)

        # Everything after the subprocess exists must be guarded: if reader
        # startup or the initialize handshake fails (kiro-cli hang / auth stall),
        # the process, its reader/stderr tasks, its PID-file entries AND its
        # _PROTECTED_PIDS shield would all leak. kill() reaps them (and
        # unregisters the protected PID via _mark_dead) before we re-raise.
        # BaseException so CancelledError during the 30s handshake also cleans up.
        try:
            # Start stderr drain
            if self._process.stderr:
                self._stderr_task = asyncio.ensure_future(self._drain_stderr())

            # Start the single reader task — owns stdout exclusively
            self._reader_task = asyncio.ensure_future(self._reader_loop())

            # Protocol handshake
            init_resp = await self._send_and_await(
                "initialize",
                {
                    "clientName": CLIENT_NAME,
                    "clientVersion": CLIENT_VERSION,
                    "protocolVersion": PROTOCOL_VERSION,
                },
            )
            self._can_load_session = bool(
                init_resp.get("agentCapabilities", {}).get("loadSession", False)
            )
            self._initialized = True
            logger.info("AcpRuntime initialized (PID %d)", self._pid)
        except BaseException:
            try:
                await self.kill()
            except Exception:
                logger.debug(
                    "AcpRuntime: cleanup kill after failed spawn/handshake failed", exc_info=True
                )
            raise

    async def kill(self) -> None:
        """Kill the subprocess and clean up all state."""
        # Fail pending futures + poison session queues FIRST. _mark_dead sets
        # self._dead internally; doing it up front (before teardown) ensures any
        # waiters learn the runtime died. Calling it after setting _dead=True
        # would hit its early-return guard and skip all cleanup.
        self._mark_dead("killed")

        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._process:
            pid = self._process.pid
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(pid), signal.SIGKILL)
                except (OSError, ProcessLookupError):
                    pass
            self._process = None
            logger.info("AcpRuntime killed (PID %d)", pid)

            # Untrack the PID so the orphan sweep doesn't chase a dead entry
            # (mirrors AcpClient._reset_state). Best-effort — a leftover entry
            # is only pruned lazily otherwise.
            try:
                _untrack_pid(pid)
                _untrack_session_pid(pid)
                unregister_protected_pid(pid)
            except Exception:
                logger.debug("AcpRuntime: PID untracking failed for %s", pid, exc_info=True)

        if self._sandbox_cleanup:
            try:
                os.remove(self._sandbox_cleanup)
            except OSError:
                pass

    # ── Reader Task (single owner of stdout) ──

    async def _reader_loop(self) -> None:
        """Single reader task — owns stdout exclusively. Routes frames by type.

        Routing:
          1. Response with id in _pending_requests → resolve Future
          2. Response with id in _routed_requests → put in session queue
          3. Notification with params.sessionId → session queue
          4. No sessionId → broadcast to all queues
        """
        assert self._process and self._process.stdout
        stdout = self._process.stdout

        try:
            while True:
                try:
                    line = await stdout.readline()
                except (ValueError, asyncio.LimitOverrunError) as exc:
                    logger.error("stdout buffer overrun: %s", exc)
                    self._mark_dead(f"stdout overrun: {exc}")
                    return

                if not line:
                    rc = self._process.returncode if self._process else "?"
                    self._mark_dead(f"process exited (rc={rc})")
                    return

                self._last_activity = time.monotonic()

                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("non-JSON stdout line: %s", line[:200])
                    continue

                # Valid JSON is not necessarily a JSON-RPC object: a bare scalar
                # or array (e.g. `123`, `"foo"`, `[1,2]`, `true`, `null`) would
                # make JsonRpcMessage.from_dict -> data.get(...) raise
                # AttributeError, crashing this single-owner reader and tearing
                # down EVERY multiplexed session. Skip anything that isn't an
                # object so one stray line can't kill the demux.
                if not isinstance(data, dict):
                    logger.debug("non-object JSON stdout line: %s", line[:200])
                    continue

                msg = JsonRpcMessage.from_dict(data)

                # Route responses
                if msg.id is not None and (msg.result is not None or msg.error is not None):
                    req_id = msg.id if isinstance(msg.id, int) else int(msg.id)

                    # Check awaited requests first (init, session/new, set_mode)
                    future = self._pending_requests.pop(req_id, None)
                    if future and not future.done():
                        if msg.error:
                            future.set_exception(AcpRuntimeError(f"RPC error: {msg.error}"))
                        else:
                            future.set_result(msg.result or {})
                        continue

                    # Check routed requests (prompt response → session queue)
                    session_id = self._routed_requests.pop(req_id, None)
                    if session_id and session_id in self._session_queues:
                        await self._session_queues[session_id].put(msg)
                        continue

                    logger.debug("Unmatched response id=%d", req_id)
                    continue

                # Route notifications by sessionId
                session_id = (msg.params or {}).get("sessionId")
                if session_id:
                    # A frame tagged with a sessionId belongs to exactly one
                    # session. Route to it if registered; otherwise DROP it.
                    # Broadcasting a known-but-unregistered session's frame to
                    # every other session would be cross-talk.
                    queue = self._session_queues.get(session_id)
                    if queue is not None:
                        await queue.put(msg)
                    else:
                        logger.debug(
                            "Dropping frame for unknown session %s (method=%s)",
                            session_id, msg.method,
                        )
                    continue

                # No sessionId → genuinely global notification; broadcast to all.
                if self._session_queues:
                    # Snapshot: `await queue.put` yields, and a concurrent
                    # unregister_session() could pop mid-iteration otherwise.
                    for queue in list(self._session_queues.values()):
                        await queue.put(msg)
                else:
                    logger.debug("Unrouted msg (no sessions): method=%s", msg.method)

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("Reader loop crashed: %s", exc, exc_info=True)
            self._mark_dead(f"reader crash: {exc}")

    def saw_not_logged_in(self) -> bool:
        """True if kiro-cli's 'not logged in' auth-failure appeared on stderr.

        Lets callers translate a runtime death into AcpAuthRequired (an
        actionable login prompt) instead of a generic process-death error —
        parity with AcpClient, which inspects stderr the same way.
        """
        return any(_NOT_LOGGED_IN_RE.search(line) for line in self._stderr_lines)

    def _mark_dead(self, reason: str) -> None:
        """Mark runtime dead, fail all pending requests, poison all session queues."""
        if self._dead:
            return
        self._dead = True
        # Release the sweep-protection shield on ANY death path (EOF, rc!=0,
        # stdout overrun, reader crash, broken pipe) — not just kill(). Otherwise
        # the dead PID lingers in _PROTECTED_PIDS forever and, after PID reuse,
        # could shield a genuinely-orphaned process from the orphan sweep.
        if self._pid:
            try:
                unregister_protected_pid(self._pid)
            except Exception:
                logger.debug(
                    "AcpRuntime: unregister protected pid failed for %s", self._pid, exc_info=True
                )
        # Diagnostic context: process returncode + tail of captured stderr so
        # operators can tell an OOM/crash from a clean exit without DEBUG logs.
        rc = self._process.returncode if self._process else None
        tail = " | ".join(self._stderr_lines[-5:]) if self._stderr_lines else "<none>"
        logger.warning(
            "AcpRuntime dead (PID %s): %s [returncode=%s] stderr_tail: %s",
            self._pid,
            reason,
            rc,
            tail,
        )

        exc = AcpRuntimeDead(reason)
        for future in self._pending_requests.values():
            if not future.done():
                future.set_exception(exc)
        self._pending_requests.clear()
        # Also drop routed-request correlations: on death no reader will pop
        # them, and if a session is never destroyed the entry would otherwise
        # linger. unregister_session() also sweeps these per-session; this is
        # belt-and-suspenders for the process-death-before-response case.
        self._routed_requests.clear()

        for queue in list(self._session_queues.values()):
            try:
                queue.put_nowait(None)  # poison sentinel
            except asyncio.QueueFull:
                pass

    # ── Protocol Interface (used by AcpSessionHandle) ──

    async def send_request(self, method: str, params: dict[str, Any]) -> int:
        """Send a JSON-RPC request and return the request id.

        The response will be routed to the session's queue (via _routed_requests)
        so AcpSessionHandle can detect turn completion. For requests that need
        an immediate response (init, session/new), use _send_and_await instead.
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        req_id = self._next_id
        self._next_id += 1

        # Register for session routing so the response goes to the right queue
        session_id = params.get("sessionId")
        if session_id and session_id in self._session_queues:
            self._routed_requests[req_id] = session_id

        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = json.dumps(req.to_dict()) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._routed_requests.pop(req_id, None)
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()
        return req_id

    async def send_notification(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response expected).

        Unlike send_request, this does NOT allocate an id or register routing,
        so it leaves no _routed_requests entry to leak when the server (per the
        ACP spec) sends no response back (e.g. session/cancel).
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        data = json.dumps(msg) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()

    async def send_response(self, request_id: str | int, result: dict[str, Any]) -> None:
        """Send a JSON-RPC response (for server→client requests like permission)."""
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "id": request_id, "result": result}
        data = json.dumps(msg) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    async def send_error(self, request_id: str | int, code: int, message: str) -> None:
        """Send a JSON-RPC error response."""
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        msg = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        data = json.dumps(msg) + "\n"

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

    def unregister_session(self, session_id: str) -> None:
        """Unregister a session queue (called by AcpSessionHandle.destroy)."""
        self._session_queues.pop(session_id, None)
        # Clean up any pending routed requests for this session
        stale = [k for k, v in self._routed_requests.items() if v == session_id]
        for k in stale:
            del self._routed_requests[k]
        logger.debug("Removed session %s", session_id)

    # Alias for backward compat
    remove_session = unregister_session

    # ── Session Management ──

    async def create_session(
        self,
        cwd: str | Path | None = None,
        agent: str | None = None,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> AcpSessionHandle:
        """Create a new ACP session on this runtime. Returns a session handle."""
        if not self._initialized:
            raise AcpRuntimeError("Runtime not initialized — call spawn() first")

        params = build_session_new_params(
            cwd if cwd else self._work_dir,
            mcp_servers=mcp_servers,
        )

        resp = await self._send_and_await(METHOD_SESSION_NEW, params)

        session_id = resp.get("sessionId")
        if not session_id:
            raise AcpRuntimeError(f"session/new did not return sessionId: {resp}")

        # Register session queue
        queue: asyncio.Queue[JsonRpcMessage | None] = asyncio.Queue()
        self._session_queues[session_id] = queue

        handle = AcpSessionHandle(
            session_id=session_id,
            queue=queue,
            runtime=self,
        )

        # Populate state from session/new response (configOptions, available models)
        handle.store_session_config(resp)

        # Set agent mode if specified. If set_mode raises, no handle is returned
        # to the caller, so unregister the queue we just registered above —
        # otherwise the reader loop keeps routing frames to an abandoned queue
        # indefinitely. Mirrors the same cleanup in load_session().
        if agent:
            try:
                await self._send_and_await(
                    METHOD_SET_MODE,
                    set_mode_params(session_id, agent),
                )
            except Exception:
                self.unregister_session(session_id)
                raise

        # Drain MCP-server-init / oauth / config notifications before the first
        # prompt so they don't race into the first turn (parity with
        # AcpClient._drain_notifications). Best-effort, bounded (~1s).
        await handle.drain_init()

        logger.info("Created session %s on runtime PID %d", session_id, self._pid or 0)
        return handle

    async def load_session(
        self,
        session_file: str,
        resume_sid: str,
        cwd: str | Path | None = None,
        agent: str | None = None,
    ) -> AcpSessionHandle:
        """Resume a prior session via session/load — mirrors AcpClient.

        Unlike create_session()+handle.load(), this issues session/load
        DIRECTLY (no session/new first), using the ORIGINAL sid as sessionId
        and passing cwd + mcpServers:[] + the full transcript path, exactly as
        AcpClient._initialize_session does. This avoids the double-session
        footgun (fresh session/new context replayed on top of the loaded
        transcript) that produced stopReason='refusal'. Raises on failure so
        the caller can fall back to create_session().
        """
        if not self._initialized:
            raise AcpRuntimeError("Runtime not initialized — call spawn() first")
        if not self._can_load_session:
            raise AcpRuntimeError("Backend does not advertise session/load support")

        load_params = {
            "sessionId": resume_sid,
            "cwd": str(cwd if cwd else self._work_dir),
            "mcpServers": [],  # kiro-cli gets its servers via --agent
            "_meta": {"_kiro.dev/session_file": session_file},
        }
        resp = await self._send_and_await(METHOD_SESSION_LOAD, load_params)

        # A genuine resume echoes "modes" in the response (same signal AcpClient
        # keys on). Anything else means load did not actually restore state.
        if "modes" not in resp:
            raise AcpRuntimeError(
                f"session/load did not resume session {resume_sid}: {resp}"
            )

        # Register the queue AFTER _send_and_await returns. During session/load
        # kiro-cli replays the full prior transcript on stdout; without a
        # registered queue those replay frames hit the "unknown session -> drop"
        # path in the reader loop and are silently discarded. Only frames
        # arriving AFTER this point (from future prompt() calls) reach the queue.
        # The load response itself routes via _pending_requests, not the session
        # queue, so this reorder is safe.
        queue: asyncio.Queue[JsonRpcMessage | None] = asyncio.Queue()
        self._session_queues[resume_sid] = queue

        handle = AcpSessionHandle(
            session_id=resume_sid,
            queue=queue,
            runtime=self,
        )
        handle.store_session_config(resp)

        # Activate the agent (mirrors AcpClient step 4 — set_mode applies to a
        # resumed session too, not just fresh ones). If set_mode raises, the
        # caller falls back to create_session() (a fresh sid + its own queue),
        # so unregister this resume_sid queue first — otherwise the reader keeps
        # routing late transcript-replay frames to an abandoned queue.
        if agent:
            try:
                await self._send_and_await(
                    METHOD_SET_MODE,
                    set_mode_params(resume_sid, agent),
                )
            except Exception:
                self.unregister_session(resume_sid)
                raise

        # Drain MCP-init / oauth / config notifications before the first prompt
        # (parity with AcpClient). Transcript-replay frames were already dropped
        # before the queue was registered above, so only genuine init frames
        # remain to drain here.
        await handle.drain_init()

        logger.info("Resumed session %s on runtime PID %d", resume_sid, self._pid or 0)
        return handle

    # ── Internal Helpers ──

    async def _send_and_await(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and await the response via _pending_requests.

        Used for control-plane requests (initialize, session/new, set_mode)
        where we need the response immediately rather than routing it to a
        session queue.
        """
        if not self._process or not self._process.stdin:
            raise AcpRuntimeDead("process not running")
        if self._dead:
            raise AcpRuntimeDead("runtime is dead")

        req_id = self._next_id
        self._next_id += 1

        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = json.dumps(req.to_dict()) + "\n"

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_requests[req_id] = future

        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._pending_requests.pop(req_id, None)
            self._mark_dead(f"pipe broken: {exc}")
            raise AcpRuntimeDead(f"pipe broken: {exc}") from exc

        self._last_activity = time.monotonic()

        try:
            return await asyncio.wait_for(future, timeout=_REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending_requests.pop(req_id, None)
            raise AcpRuntimeError(f"Request {method} timed out")

    async def _drain_stderr(self) -> None:
        """Drain stderr to prevent subprocess blocking."""
        assert self._process and self._process.stderr
        stderr = self._process.stderr
        try:
            while True:
                line = await stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text:
                    self._stderr_lines.append(text)
                    if len(self._stderr_lines) > 20:
                        self._stderr_lines = self._stderr_lines[-20:]
                    logger.debug("stderr: %s", text[:200])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # An overlong stderr line (ValueError / LimitOverrunError from
            # readline when no newline fits the buffer) or a low-level read
            # error must not kill this task with an unhandled exception. Log and
            # exit the drain cleanly rather than leaving a dead task behind.
            logger.debug("stderr drain task exiting on error: %s", exc, exc_info=True)
