"""Test harness for spawning isolated KiroClaw gateways.

Companion to the ``--test-mode`` / ``--json-ready`` / ``--port`` / ``--approval``
CLI flags on ``kiroclaw gateway``. Provides a context manager that spins up
an isolated, headless gateway from the current workspace's source tree (not
the system-installed ``kiroclaw``), reads the ``KIROCLAW_READY:{...}`` line
off stdout, and tears down cleanly on exit.

Transport-agnostic: ``GatewayHandle`` exposes the URL plus a few metadata
fields. The caller chooses the driver (Playwright via DSO Frontend MCP is
the recommended one; plain HTTP for backend-only smoke tests is fine but
not the recommended path).

Usage:

    from kiro_claw.testing.harness import spawn_feature_gateway

    with spawn_feature_gateway() as handle:
        # drive Playwright / urllib / etc against handle.url
        ...
    # subprocess and tmp KIROCLAW_HOME are gone by here
"""

from __future__ import annotations

import contextlib
import json
import os
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Iterator, Optional

# 60 s default — config init + MCP probe + dashboard bind takes meaningful
# time on slow machines. Plan suggested 30 s; 60 s gives headroom without
# masking real hangs. Override via ``KIROCLAW_HARNESS_READY_TIMEOUT``.
DEFAULT_READY_TIMEOUT = 60.0

# How long to wait between SIGTERM and SIGKILL during teardown. Gateway's
# graceful-shutdown budget is 10 s internally; 5 s here is enough for the
# common case (no pending tool calls, no hung MCP servers) and bounds the
# pytest teardown latency for tests that exercise multiple invocations.
TERMINATE_GRACE_SECONDS = 5.0

# Sentinel prefix the gateway prints to stdout once the dashboard is bound.
# Owned by ``slack/gateway.py``; if you change it there, update here too.
READY_PREFIX = "KIROCLAW_READY:"


@dataclass(frozen=True)
class GatewayHandle:
    """Handle on a spawned gateway.

    Intentionally minimal — exposes the URL plus a few metadata fields and
    leaves all I/O to the caller. Adding HTTP / WebSocket / MCP helpers
    here would couple every consumer to a specific driver; keeping it
    transport-agnostic lets each test pick its own (Playwright, plain
    axios, urllib, websockets — whatever fits).

    Attributes:
        url: Authenticated dashboard URL with token query param. Safe to
            ``urllib.request.urlopen`` directly or feed to a browser via
            Playwright.
        port: OS-assigned ephemeral port the dashboard is bound to.
        token: Session token embedded in ``url``. Exposed separately for
            clients that build their own URLs (e.g. WebSocket connectors).
        home: Path to the throwaway ``KIROCLAW_HOME`` directory the gateway
            is using. Useful for tests that need to inspect on-disk state
            (sessions, memory, lessons) after exercising the gateway.
        proc: Underlying ``subprocess.Popen`` handle. Most tests should
            never touch this; the context manager owns its lifecycle.
    """

    url: str
    port: int
    token: str
    home: Path
    proc: subprocess.Popen[bytes]


class GatewaySpawnError(RuntimeError):
    """Raised when the harness can't spin up a working gateway.

    Wraps the underlying cause (timeout, early exit, bad fixture name)
    with the subprocess's stderr so failures produce useful diagnostics
    instead of a bare ``TimeoutError`` from a buried ``readline()``.
    """


def _resolve_workspace_src() -> Path:
    """Locate the in-repo ``src/`` so PYTHONPATH points at feature-branch code.

    Walks up from this file's location
    (``<pkg>/src/kiro_claw/testing/harness.py``) to find the package's
    ``src/`` directory. We deliberately avoid using the system-installed
    ``kiroclaw`` for two reasons:

    1. We're testing the *current* code, not whatever the developer has
       on PATH. A stale Toolbox install would silently mask regressions.
    2. The harness needs to work before the composable-CLI CR merges —
       i.e. when the system install doesn't yet have ``--test-mode``.
    """
    here = Path(__file__).resolve()
    # <pkg>/src/kiro_claw/testing/harness.py -> <pkg>/src
    src = here.parent.parent.parent
    if not (src / "kiro_claw" / "__init__.py").exists():
        raise GatewaySpawnError(
            f"Could not locate kiro_claw package at {src}. "
            f"Harness expects harness.py at <pkg>/src/kiro_claw/testing/, "
            f"with the source tree at <pkg>/src/kiro_claw/."
        )
    return src


def _wait_for_ready_line(
    proc: subprocess.Popen[bytes],
    *,
    timeout: float,
    stderr_buffer: list[bytes],
) -> dict[str, Any]:
    """Read stdout until we see ``KIROCLAW_READY:{...}`` or hit the timeout.

    Uses ``selectors`` rather than ``stdout.readline()`` so the deadline is
    enforced even when the subprocess is alive but silent. ``readline()``
    is a blocking call on a pipe — a gateway that's stuck on a network
    call or deadlocked internally without writing to stdout and without
    exiting would otherwise hang the harness indefinitely, contradicting
    the documented 60s timeout guarantee. The selector poll caps the wait
    per iteration so the deadline check fires at least every 0.5 s.

    Surfaces stderr (populated by the caller's drain thread) on timeout
    or early subprocess exit. Raises ``GatewaySpawnError`` on timeout,
    early exit, or malformed payload. Returns the parsed READY-line dict.
    """
    deadline = time.monotonic() + timeout
    stdout = proc.stdout
    if stdout is None:  # defensive — Popen always wires stdout when PIPE
        raise GatewaySpawnError("subprocess stdout is not piped")

    sel = selectors.DefaultSelector()
    sel.register(stdout, selectors.EVENT_READ)
    buf = b""
    try:
        while True:
            if proc.poll() is not None:
                stderr_text = b"".join(stderr_buffer).decode("utf-8", errors="replace")
                raise GatewaySpawnError(
                    f"gateway subprocess exited with code {proc.returncode} "
                    f"before emitting {READY_PREFIX} line.\n"
                    f"--- stderr (last) ---\n{stderr_text[-4000:]}"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stderr_text = b"".join(stderr_buffer).decode("utf-8", errors="replace")
                raise GatewaySpawnError(
                    f"gateway did not emit {READY_PREFIX} line within "
                    f"{timeout:.1f}s. Override with KIROCLAW_HARNESS_READY_TIMEOUT.\n"
                    f"--- stderr (last) ---\n{stderr_text[-4000:]}"
                )
            # Cap each select() at 0.5 s so the deadline + poll() checks
            # above run frequently even if the subprocess goes silent.
            ready = sel.select(timeout=min(remaining, 0.5))
            if not ready:
                continue  # poll interval elapsed — re-check deadline & proc
            chunk = stdout.read1(4096)  # type: ignore[attr-defined]
            if not chunk:
                # EOF on stdout. Without unregistering, the selector keeps
                # reporting EOF as ready (EOF is permanently readable),
                # ``read1`` keeps returning b"", and the loop spins at
                # 100% CPU until the deadline fires. Unregister so the
                # next ``sel.select()`` has nothing to poll and sleeps
                # for its 0.5s timeout instead — the deadline + poll()
                # checks at the top of the loop handle the rest.
                sel.unregister(stdout)
                continue
            buf += chunk
            while b"\n" in buf:
                line_bytes, buf = buf.split(b"\n", 1)
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if line.startswith(READY_PREFIX):
                    try:
                        payload = json.loads(line[len(READY_PREFIX) :])
                    except json.JSONDecodeError as exc:
                        raise GatewaySpawnError(
                            f"malformed {READY_PREFIX} line: {line!r} ({exc})"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise GatewaySpawnError(
                            f"{READY_PREFIX} payload was {type(payload).__name__}, "
                            f"expected dict: {line!r}"
                        )
                    # Validate required keys so the caller can rely on
                    # ``payload["port"]`` / ``payload["token"]`` without
                    # leaking a ``KeyError`` past the
                    # always-``GatewaySpawnError`` contract documented on
                    # ``spawn_feature_gateway``. A gateway version drift
                    # that drops one of these is exactly the kind of
                    # protocol-shift the harness should surface clearly.
                    for required_key in ("port", "token"):
                        if required_key not in payload:
                            raise GatewaySpawnError(
                                f"{READY_PREFIX} payload missing required key "
                                f"{required_key!r}: {line!r}"
                            )
                    return payload
    finally:
        sel.close()


def _drain_stderr(stderr: IO[bytes], buffer: list[bytes]) -> None:
    """Continuously read from stderr into ``buffer``.

    The gateway emits ~30+ WARNING lines on startup (config-loader meta-key
    spam, MCP probe failures for unconfigured servers). They all go to
    stderr, so without a drainer the pipe fills, the subprocess blocks on
    write, and we deadlock. Buffer the contents so we can surface them
    on failure.
    """
    while True:
        chunk = stderr.read1(4096)  # type: ignore[attr-defined]
        if not chunk:
            return
        buffer.append(chunk)


def _terminate_process_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the whole process group, fall back to SIGKILL on timeout.

    The gateway spawns child processes (MCP servers, kiro-cli sessions,
    Ollama, secretary). ``proc.terminate()`` only signals the parent;
    children can outlive it and hold the ephemeral port or cache files
    open. Process-group kill (enabled by ``start_new_session=True`` at
    spawn time) sweeps the whole tree.
    """
    if proc.poll() is not None:
        return  # already exited
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        # Race: process exited between poll() and killpg(), or we lack
        # permission to signal the group. Fall back to direct terminate.
        proc.terminate()

    try:
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=TERMINATE_GRACE_SECONDS)


@contextlib.contextmanager
def spawn_feature_gateway(
    fixture: str = "minimal",
    approval: str = "reads",
    *,
    crons: bool = False,
    timeout: Optional[float] = None,
) -> Iterator[GatewayHandle]:
    """Spin up an isolated gateway from the current workspace checkout.

    Args:
        fixture: Named fixture (``empty`` / ``minimal`` / ``rich``) passed
            through to ``kiroclaw gateway --seed`` so the tmp KIROCLAW_HOME
            is populated atomically with gateway startup. A bad name causes
            the gateway to exit before READY; the readline loop surfaces
            seed's stderr in a ``GatewaySpawnError``.
        approval: Approval mode to pass through ``--approval``. ``"reads"``
            (default) auto-approves a conservative set of read verbs;
            ``"yolo"`` auto-approves everything but requires an isolated
            ``KIROCLAW_HOME`` (the harness always provides one); pass
            ``"interactive"`` only if the test will drive the approval
            UI itself.
        crons: When ``False`` (default) the harness passes ``--no-crons``
            to suppress all scheduled jobs — the safe default since stray
            cron fires can pollute unrelated tests' state. Set ``True`` to
            keep cron scheduling enabled (e.g. tests that exercise
            ``cron_add`` end-to-end and need the scheduler thread alive).
        timeout: Override the ready-line timeout in seconds. Falls back to
            ``KIROCLAW_HARNESS_READY_TIMEOUT`` env var, then to
            ``DEFAULT_READY_TIMEOUT``.

    Yields:
        ``GatewayHandle`` once the gateway has bound its dashboard port
        and emitted the ``KIROCLAW_READY:{...}`` line. The handle is only
        valid inside the ``with`` block; on exit the subprocess is
        terminated and ``handle.home`` is removed.

    Raises:
        ``GatewaySpawnError`` if the gateway exits before READY, doesn't
        emit READY within the timeout, or can't seed the fixture.
    """
    src = _resolve_workspace_src()
    home = Path(tempfile.mkdtemp(prefix="kiroclaw-harness-"))
    # Outer try/finally so ``home`` is always cleaned up, even if
    # ``subprocess.Popen`` raises before we reach the inner block (bad
    # ``sys.executable``, fd exhaustion, fork failure, ...).
    try:
        if timeout is None:
            env_timeout = os.environ.get("KIROCLAW_HARNESS_READY_TIMEOUT")
            timeout = float(env_timeout) if env_timeout else DEFAULT_READY_TIMEOUT

        env = {
            **os.environ,
            "PYTHONPATH": str(src) + os.pathsep + os.environ.get("PYTHONPATH", ""),
            "KIROCLAW_HOME": str(home),
            # Force unbuffered Python so we see READY without waiting for the
            # next flush. ``--json-ready`` already calls ``flush=True`` on the
            # READY print itself, but other prints leading up to it (e.g.
            # "Created default config") would block-buffer when stdout is a
            # pipe and could mask early failures.
            "PYTHONUNBUFFERED": "1",
        }

        cmd = [
            sys.executable,
            "-m",
            "kiro_claw",
            "gateway",
            "--test-mode",
            # ``--seed`` populates the (empty) tmp KIROCLAW_HOME from the named
            # fixture (empty / minimal / rich) before binding the dashboard.
            # Atomic with the gateway start: a bad fixture name → gateway exits
            # with seed's exit code before READY, which the readline loop below
            # surfaces as a GatewaySpawnError with stderr.
            "--seed",
            fixture,
            "--approval",
            approval,
        ]
        if not crons:
            # Suppress scheduled jobs by default — a stray cron firing during
            # an unrelated test is a hard-to-diagnose source of flakes. Tests
            # that specifically exercise the cron path opt back in via
            # ``crons=True``.
            cmd.append("--no-crons")
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # New session = new process group. Lets us SIGTERM/SIGKILL the
            # whole tree on teardown so child MCP servers / kiro-cli sessions
            # don't outlive their parent and hold ports open.
            start_new_session=True,
        )

        # Drain stderr asynchronously into a list so the buffer can't fill
        # and deadlock the subprocess. Using a daemon thread is fine: it
        # exits when the process closes its stderr.
        import threading

        stderr_buffer: list[bytes] = []
        if proc.stderr is not None:
            drainer = threading.Thread(
                target=_drain_stderr, args=(proc.stderr, stderr_buffer), daemon=True
            )
            drainer.start()

        try:
            ready = _wait_for_ready_line(proc, timeout=timeout, stderr_buffer=stderr_buffer)
            port = int(ready["port"])
            token = str(ready["token"])
            url = f"http://localhost:{port}/?token={token}"
            handle = GatewayHandle(url=url, port=port, token=token, home=home, proc=proc)
            yield handle
        finally:
            _terminate_process_group(proc)
    finally:
        # Clean up tmp home regardless of how we exited. ``ignore_errors``
        # because pytest cleanup races with nested file handles on macOS
        # and the test harness shouldn't fail teardown on stale FDs.
        shutil.rmtree(home, ignore_errors=True)
