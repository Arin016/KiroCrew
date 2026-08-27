"""Supervise the KiroCrew MCP gateway subprocess.

Lifecycle:

1. :meth:`GatewayManager.start` — if a daemon already answers on the socket,
   compare the target set it reports against the one the agent specs were just
   written for and adopt it only when they agree; an unfit incumbent is asked to
   stand down first (issue #4569). Otherwise spawn ``python -m
   kiro_crew.mcp_gateway.gatewayd``, wait until the unix socket appears, then
   round-trip one ping/pong to confirm the daemon is serving.
2. Background watchdog — detect exit and respawn with exponential backoff.
3. :meth:`GatewayManager.shutdown` — SIGTERM → SIGKILL the daemon on
   KiroCrew shutdown.

Gateway failures are non-fatal for KiroCrew. If the daemon crashes, the
stub's graceful-fallback path exec's the real MCP binary directly, so
sessions keep working — the only loss is the RAM sharing benefit until
the daemon recovers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.config.paths import config_dir
from kiro_crew.env import resolve_krb5_ccname
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.hashing import hash_target_env
from kiro_crew.mcp_gateway.pool import READ_BUFFER_LIMIT_BYTES
from kiro_crew.mcp_gateway.shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS
from kiro_crew.sandbox import _SENSITIVE_ENV_PREFIXES as _SANDBOX_SENSITIVE_ENV_PREFIXES

logger = logging.getLogger(__name__)

# Max time to wait for the gateway's unix socket to appear after spawn.
_SOCKET_READY_TIMEOUT_SECS = 5.0
# Polling interval while waiting for the socket.
_SOCKET_POLL_INTERVAL_SECS = 0.1
# Ping/pong round-trip deadline after the socket appears. The daemon is
# serving if it echoes a pong within this window; otherwise we treat the
# spawn as failed and fall back to per-session MCP.
_PING_TIMEOUT_SECS = 2.0
# Interval between liveness probes in the watchdog. A ping round-trip
# failure (detailed below) promotes the daemon from "alive" to "zombie"
# and triggers the same respawn path we use for crashes. We cannot rely
# on ``proc.wait()`` alone — the accept loop can die silently while the
# Python process stays alive, leaving the socket unreachable.
_LIVENESS_PING_INTERVAL_SECS = 30.0
# Consecutive ping failures tolerated before declaring the daemon a
# zombie. Three gives ~90s of grace — enough for transient chaos-induced
# ping timeouts (stub kill-storms, socket drains, pool-wide eviction
# cycles) to self-heal without the watchdog tripping and killing a
# daemon that would have recovered on its own. Empirically, the 2-fail
# threshold raced with run_chaos.py and produced spurious
# "gatewayd_pid_changed_unexpectedly" during legitimate chaos tests.
_LIVENESS_MAX_CONSECUTIVE_FAILURES = 3
# SIGTERM → SIGKILL grace period on shutdown. DERIVED, never a literal: a
# hand-written 5.0 here was shorter than gatewayd's own 10s drain window, so the
# supervisor SIGKILLed every restart that had attached stubs before the daemon
# could reach ``pool.shutdown_all()``. Sourcing it from the daemon's published
# budget makes that inversion unrepresentable.
_SHUTDOWN_GRACE_SECS = TOTAL_SHUTDOWN_BUDGET_SECS
# Respawn backoff: start here, double up to max.
_RESPAWN_BACKOFF_START_SECS = 1.0
_RESPAWN_BACKOFF_MAX_SECS = 60.0
# How many times start() will re-run assess-then-spawn before giving up. Two,
# because the socket can change hands exactly once under a single start: an
# unfit incumbent yields and another gateway instance on the same machine wins
# the freed lock first. Round two assesses that daemon; a cap is what stops two
# instances trading the socket in a loop, and the give-up path is an ERROR
# rather than a silent success against a daemon that cannot serve the specs.
_ELECTION_ROUNDS = 2
# Lifetime cap on stand-down requests one manager will issue. _ELECTION_ROUNDS
# bounds hand-offs WITHIN a single start(); this bounds them across the whole
# process, which is the case the other cap cannot reach: the watchdog also
# assesses incumbents, on an unbounded respawn loop, so two long-lived gateway
# instances sharing a socket path with divergent target sets would otherwise
# stand each other's daemon down on every respawn, forever. Past the cap this
# manager stops asking and adopts whatever holds the socket, logging the reason
# -- a bounded number of cycles followed by a loud, stable state. Removing the
# oscillation entirely needs an ownership/generation lease so one instance is
# the authorised successor; that is a protocol design and is deliberately not
# invented here.
_MAX_STAND_DOWN_REQUESTS = 3

# Outcomes of assessing the daemon that holds the socket. Plain strings rather
# than an Enum so they read the same in a log line as in a branch.
#: Keep the incumbent (it is fit, or unfit-but-still-serving and unreconcilable).
_ADOPT = "adopt"
#: It released the socket; put our own daemon there.
_SPAWN = "spawn"
#: Neither is safe right now — fail the start rather than report a false ready.
_ABORT = "abort"

# Outcomes of a stand-down request. _DRAINING must stay distinct from _REFUSED:
# a daemon that ACCEPTED has already closed its accept loop and so is not
# adoptable, while one that REFUSED is still serving and is.
_RELEASED = "released"
_DRAINING = "draining"
_REFUSED = "refused"

# Python module invoked as the gateway daemon. Kept as a constant so
# tests can monkey-patch it and the spawn path stays one line.
_GATEWAYD_MODULE = "kiro_crew.mcp_gateway.gatewayd"

# Env-var prefixes scrubbed before spawning the gateway daemon and every
# pooled MCP backend it spawns. Reuse sandbox's canonical all-modes list so
# the AWS/SSH/GPG/credential-helper prefixes stay in sync going forward
# (the previous hand-maintained tuple carried a stale
# "Mirrors sandbox" comment that could silently drift). ``AWS_ACCESS`` is kept
# on top because the daemon is more exposed than a credential_process-backed
# session.
#
# We deliberately do NOT scrub ``sandbox._AGENT_DENIED_ENV_KEYS``
# (SLACK_BOT_TOKEN/APP/USER, KIROCREW_OWNER_ID). config/loader.py seeds those
# into os.environ specifically so TRUSTED children — the gateway, its pooled
# MCP backends, and cron — inherit them (a pooled slack-mcp needs its Slack
# token). The sandbox strips those keys from the LLM *agent* subprocess via
# wrap_argv(); MCP backends sit on the trusted side of that boundary in both
# per-session and pooled topologies, so scrubbing them here would break those
# servers without closing any privilege gap.
_SENSITIVE_ENV_PREFIXES: tuple[str, ...] = (
    "AWS_ACCESS",
    *_SANDBOX_SENSITIVE_ENV_PREFIXES,
)


def is_credential_env_key(key: str) -> bool:
    """Return ``True`` if ``key`` matches :data:`_SENSITIVE_ENV_PREFIXES`.

    The single matching rule behind :func:`_scrub_sensitive_env`, exposed so the
    declared-env forwarding path can refuse to re-introduce a credential key
    that the daemon scrub deliberately removed. Note this list is BROADER than
    ``hashing.ENV_SCRUB_PREFIXES`` (it also covers ``AWS_ACCESS``,
    ``SSH_AUTH_SOCK``, ``GNUPGHOME``, ``GIT_ASKPASS``), so forwarding must
    honour both.
    """
    return any(key.startswith(prefix) for prefix in _SENSITIVE_ENV_PREFIXES)


def _scrub_sensitive_env(env: dict[str, str]) -> dict[str, str]:
    """Return a copy of ``env`` with ``_SENSITIVE_ENV_PREFIXES`` keys removed.

    Called before spawning the gateway daemon so MCP backends the daemon
    spawns do NOT inherit credential env vars. File-level sensitive paths
    (``~/.aws``, ``~/.ssh`` ...) are still reachable — those are protected
    per-session by the kiro-cli sandbox's bind-mounts and the hook layer's
    ``is_sensitive_path()`` check; the gateway daemon does not defeat
    either of those. See ``security.md``.
    """
    return {k: v for k, v in env.items() if not is_credential_env_key(k)}


@dataclass(frozen=True)
class GatewaySpec:
    """Immutable launch parameters for the gateway daemon."""

    socket_path: Path
    idle_timeout_secs: int = 300
    max_backends: int = 64  # keep in sync w/ McpGatewayConfig.max_backends (cover N agents x S servers)
    mcp_target_env: dict[str, str] = None  # type: ignore[assignment]
    prewarm_count: int = 0  # keep in sync w/ McpGatewayConfig.prewarm_count; 0 = disabled

    def __post_init__(self) -> None:
        # dataclass(frozen) + mutable default → use object.__setattr__.
        if self.mcp_target_env is None:
            object.__setattr__(self, "mcp_target_env", {})


class GatewayManager:
    """Supervise a single gateway daemon subprocess."""

    #: Class-level default so the attribute is TOTAL regardless of construction
    #: path. Tests and other call sites build this object via ``__new__``,
    #: bypassing ``__init__``, and an instance-only attribute would raise
    #: AttributeError on every such path the moment adoption reads it.
    _stand_downs_issued: int = 0

    def __init__(self, spec: GatewaySpec) -> None:
        self._spec = spec
        self._process: asyncio.subprocess.Process | None = None
        self._watchdog: asyncio.Task[None] | None = None
        self._stopping = False
        self._adopted = False
        self._stand_downs_issued = 0
        self._lifecycle_lock = asyncio.Lock()

    @property
    def socket_path(self) -> Path:
        return self._spec.socket_path

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def start(self) -> bool:
        """Spawn the daemon and wait for its socket to appear.

        Returns ``True`` on success, ``False`` on any failure. Never raises —
        callers treat a ``False`` return as "fall back to per-session MCP".
        """
        async with self._lifecycle_lock:
            return await self._start_locked()

    async def _start_locked(self) -> bool:
        """Inner start implementation, called under ``_lifecycle_lock``."""
        if self.is_running:
            return True
        # An already-adopted manager already has a live watchdog supervising
        # the incumbent (see the adoption path below). A second start() must
        # not re-enter it and overwrite self._watchdog — that would orphan the
        # first watchdog task (it keeps running but shutdown() can no longer
        # cancel it). is_running is False here because an adopted manager holds
        # no _process, so this explicit guard is required.
        if self._adopted:
            return True

        # Bounded election. One round is not enough because the socket this
        # start is contending for can change hands under it: an unfit incumbent
        # yields, and a DIFFERENT daemon — a second gateway instance on the same
        # machine — can win the freed lock before our own spawn does, leaving us
        # ping-confirming a daemon whose target set we never checked. Round two
        # assesses that daemon the same way round one assessed the first, and
        # the cap is what stops two instances handing the socket back and forth.
        for _attempt in range(_ELECTION_ROUNDS):
            # Singleton adoption: if a healthy daemon already owns the socket
            # (a sibling manager in this process won the spawn race, or a
            # survivor from a prior gateway), adopt it instead of spawning a
            # competitor. gatewayd's flock guard already makes a duplicate spawn
            # a clean no-op, but adopting skips the wasted spawn/exit churn.
            #
            # Liveness is not sufficient on its own. Startup rewrites every agent
            # spec from current config and then arrives here; a survivor of a
            # gateway that died without running its shutdown path answers pong
            # perfectly while serving the target set it was spawned with, which
            # can predate those specs. _adopt_or_stand_down compares the two and
            # gives an unfit incumbent the chance to yield the socket first.
            pong = await self._ping_probe()
            if pong is not None:
                verdict = await self._adopt_or_stand_down(pong)
                if verdict == _ADOPT:
                    return self._adopt_incumbent()
                if verdict == _ABORT:
                    # A draining incumbent: not adoptable (it no longer accepts)
                    # and not replaceable (it still holds the lock). Reporting
                    # ready here would be the laundering this check exists to
                    # avoid, so the start fails and the caller falls back to
                    # per-session MCP.
                    return False

            spawned = await self._spawn_and_confirm()
            if spawned is None:
                return False
            if self._is_fit(spawned):
                if self.is_running:
                    self._watchdog = asyncio.create_task(
                        self._run_watchdog(), name="mcp-gateway-watchdog"
                    )
                    logger.info(
                        "mcp-gateway: started pid=%s socket=%s",
                        self._process.pid if self._process else "?",
                        self._spec.socket_path,
                    )
                    return True
                # Our spawn lost the lock but whoever holds it serves the set we
                # need. Adopt rather than respawn: a fit daemon is a fit daemon
                # regardless of who started it, and leaving _adopted False here
                # would send the watchdog spawning doomed competitors.
                return self._adopt_incumbent()
            # A foreign daemon owns the socket and cannot serve our specs. Drop
            # our exited handle and let the next round assess it as an incumbent.
            logger.warning(
                "mcp-gateway: our spawn on %s lost the election to a daemon that "
                "cannot serve the current target set — re-electing",
                self._spec.socket_path,
            )
            self._process = None
        logger.error(
            "mcp-gateway: could not put a daemon serving the current target set "
            "on %s within %d election rounds; gateway unavailable",
            self._spec.socket_path, _ELECTION_ROUNDS,
        )
        return False

    def _adopt_incumbent(self) -> bool:
        """Mark the daemon on the socket as adopted and supervise it. Always ``True``."""
        self._adopted = True
        self._process = None
        logger.info(
            "mcp-gateway: a healthy daemon already owns %s — adopting "
            "(no spawn)", self._spec.socket_path,
        )
        # Supervise even when adopting: the adopted daemon may be a
        # prior-gateway survivor with no other watchdog in this process.
        # The watchdog's adopted branch re-checks liveness by ping and
        # re-elects (spawns a replacement) if the adopted daemon dies.
        self._watchdog = asyncio.create_task(
            self._run_watchdog(), name="mcp-gateway-watchdog"
        )
        return True

    def _is_fit(self, pong: dict[str, Any]) -> bool:
        """Whether the daemon that sent ``pong`` serves the set we are writing specs for."""
        served = pong.get("targets")
        return isinstance(served, str) and served == self._wanted_target_fingerprint()

    async def _spawn_and_confirm(self) -> dict[str, Any] | None:
        """Spawn a daemon and return the ``pong`` of whoever ends up serving.

        ``None`` means the start has failed outright (spawn raised, shutdown
        intervened, the endpoint never appeared, or nothing answered) and the
        caller must give up — the process handle is already cleaned up.

        A returned pong is NOT proof that the daemon is ours: gatewayd's flock
        guard makes a duplicate spawn exit rc=0 without binding, so on a
        contended socket the answer can come from a foreign daemon. The caller
        decides by fingerprint, which is why this returns the frame rather than a
        bool.
        """
        # Clear any stale socket from a prior crash.
        await self._clear_stale_socket()
        # Owner-only containing directory: the socketsec model calls this the
        # primary access boundary (a 0600 socket alone is insufficient on a
        # shared host), and on Windows it is where the singleton lock file and
        # the out-of-band reap list live since the pipe itself has no entry.
        # Off the event loop: on Windows the owner-only step shells out to
        # icacls with a multi-second timeout, and this runs inside the live
        # gateway's loop (dashboard toggle -> _init_mcp_gateway -> start()),
        # so calling it inline stalls chat turns and the liveness heartbeat.
        # Mirrors the log-file hunk in _spawn_once, which offloads the same helper.
        await asyncio.to_thread(transport.prepare_dir, self._spec.socket_path)

        try:
            await self._spawn_once()
        except Exception:
            logger.exception("mcp-gateway: initial spawn failed")
            return None

        # Re-check after spawn: shutdown() may have been called while we
        # were awaiting _spawn_once(). If so, terminate the freshly-spawned
        # daemon to avoid orphaning it.
        if self._stopping:
            logger.info("mcp-gateway: stopping flag set after spawn — aborting start")
            await self._terminate_process(grace_secs=_SHUTDOWN_GRACE_SECS)
            return None

        ok = await self._wait_for_socket(self._spec.socket_path, _SOCKET_READY_TIMEOUT_SECS)
        if not ok:
            logger.warning(
                "mcp-gateway socket did not appear within %.1fs; gateway unreachable",
                _SOCKET_READY_TIMEOUT_SECS,
            )
            await self._terminate_process(grace_secs=_SHUTDOWN_GRACE_SECS)
            return None

        # One ping/pong round-trip confirms the daemon's accept loop is
        # live before we hand control back to the caller. Without this the
        # socket appearing only proves bind() succeeded; the handler task
        # might still be wiring up when the first stub connects.
        pong = await self._ping_probe()
        if pong is None:
            logger.warning("mcp-gateway ping failed — treating start as failure")
            await self._terminate_process(grace_secs=_SHUTDOWN_GRACE_SECS)
            return None
        return pong

    async def shutdown(self) -> None:
        """Stop the watchdog and terminate the daemon."""
        # Set BEFORE contending for the lock, not inside the locked section. A
        # start() that met an unfit incumbent can hold this lock for the whole
        # stand-down wait (another process's drain budget), and _stopping is the
        # only way to tell it to give up; setting it after acquiring the lock
        # would mean shutdown waits out that drain before it can even say so.
        # Safe to hoist: the flag is monotonic (only ever set on the way down)
        # and every path that reads it already treats it as "abort".
        self._stopping = True
        async with self._lifecycle_lock:
            await self._shutdown_locked()

    async def _shutdown_locked(self) -> None:
        """Inner shutdown implementation, called under ``_lifecycle_lock``."""
        self._stopping = True
        if self._watchdog is not None:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog = None
        # Ownership discipline: an adopted manager owns no daemon (_process
        # is None) and no socket. It MUST NOT terminate a process it didn't
        # spawn or unlink a socket a live foreign daemon (a sibling manager
        # in this process, or a prior-gateway survivor) owns.
        # _clear_stale_socket() is a connect-probe-then-unlink; in the
        # documented false-stale window it would steal a live incumbent's
        # socket — re-introducing the exact socket-theft class the flock
        # guard eliminates. Only the owning (spawning) manager tears down.
        if self._adopted:
            return
        await self._terminate_process(grace_secs=_SHUTDOWN_GRACE_SECS)
        await self._clear_stale_socket()

    async def _spawn_once(self) -> None:
        """Low-level spawn — sets ``self._process``. Raises on failure."""
        # Scrub credential env vars — the gateway daemon and every MCP
        # backend it forks would otherwise inherit AWS secrets, SSH agent
        # sockets, etc.  See ``_scrub_sensitive_env`` docstring for why the
        # sandbox's per-session env scrub is not enough on its own here.
        base_env = _scrub_sensitive_env(dict(os.environ))
        env = {**base_env, **self._spec.mcp_target_env}
        # Repair the Kerberos ccache pointer for pooled MCP backends so a
        # long-lived background daemon's forked children inherit a usable
        # ticket for any credential-gated MCP server.
        resolve_krb5_ccname(env)
        # A background daemon can inherit a minimal PATH (e.g. under
        # systemd-user), so prepend the user-local bin dir where MCP
        # server launchers are commonly installed.
        local_bin = str(Path.home() / ".local" / "bin")
        existing_path = env.get("PATH", "")
        extra_dirs = [p for p in (local_bin,) if p and p not in existing_path.split(os.pathsep)]
        if extra_dirs:
            env["PATH"] = os.pathsep.join([*extra_dirs, existing_path]) if existing_path else os.pathsep.join(extra_dirs)
        argv = [
            sys.executable,
            "-m", _GATEWAYD_MODULE,
            "--socket", str(self._spec.socket_path),
            "--idle-timeout-secs", str(self._spec.idle_timeout_secs),
            "--max-backends", str(self._spec.max_backends),
        ]
        # Only pass --prewarm-count when enabled so the daemon command line
        # stays unchanged (and tests stay byte-identical) in the default case.
        if self._spec.prewarm_count > 0:
            argv += ["--prewarm-count", str(self._spec.prewarm_count)]
        # Credential-rotation drain (seam-routed): the daemon is a separately
        # spawned process that never boots the platform, so the already-booted
        # gateway process resolves the watch paths here and threads each as a
        # repeatable argv flag. The public Default returns [] — no flag, and
        # the daemon command line stays byte-identical to today. Fail-closed
        # via safe_context_call: PlatformCompositionError propagates, any
        # other adapter failure degrades to no watcher.
        for cred_path in self._credential_watch_paths():
            argv += ["--credential-watch-path", str(cred_path)]
        # Capture gatewayd stdout/stderr to the canonical KiroCrew log path.
        # This file persists across restarts so operators can diagnose
        # startup failures and stub rejections without attaching a debugger.
        log_path = self._gatewayd_log_path()
        log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            log_path.parent.chmod(0o700)
        except OSError:
            pass
        # 0600 from creation: this log captures every pooled backend's
        # stdout/stderr, which routinely includes tokens / API keys in error
        # output — never world-readable on a multi-user host. fchmod also
        # tightens a pre-existing looser file.
        _log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        # os.fchmod is a silent no-op on Windows, where mode bits carry no
        # access meaning -- the real carrier is the DACL. restrict_to_owner is
        # the fail-loud owner-only variant and is called by attribute so the
        # hermetic-test stub in conftest can intercept it. It shells out to
        # icacls on Windows, so it runs off the event loop.
        try:
            await asyncio.to_thread(platform_compat.restrict_to_owner, log_path)
        except OSError as exc:
            logger.warning("could not restrict gatewayd log %s: %s", log_path, exc)
        log_fh = os.fdopen(_log_fd, "ab", buffering=0)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                env=env,
                start_new_session=True,
            )
        finally:
            # Subprocess inherits its own copy of the fd, so closing our
            # parent handle is correct on both success and failure paths.
            # try/finally guards the spawn-raises case (ENOENT, EACCES,
            # MemoryError) that would otherwise leak ``log_fh`` until
            # GC — a real risk under a watchdog respawn storm.
            log_fh.close()

    @staticmethod
    def _credential_watch_paths() -> list[Path]:
        """Resolve seam-supplied credential watch paths for the daemon argv.

        Reads ``current_context().identity.credential_watch_paths()`` through
        the fail-closed ``safe_context_call`` helper: a
        ``PlatformCompositionError`` (non-standalone host that failed to
        compose its companion) propagates; any other adapter failure —
        including a pre-method companion adapter missing the v1 addition —
        degrades to ``[]`` (no watcher). The public
        ``DefaultIdentityProvider`` returns ``[]``, so the standalone daemon
        command line is unchanged.
        """
        from kiro_crew.platform import current_context, safe_context_call

        return safe_context_call(
            lambda: list(current_context().identity.credential_watch_paths()),
            fallback=[],
            log_message="identity.credential_watch_paths failed; no watcher",
        )

    @staticmethod
    def _gatewayd_log_path() -> Path:
        """Return the path gatewayd stdout/stderr get redirected to."""
        home = os.environ.get("KIROCREW_HOME")
        base = Path(home) if home else config_dir()
        return base / "logs" / "mcp-gatewayd.stdout"

    async def ping(self) -> bool:
        """Public liveness probe: ``True`` iff the daemon replies pong."""
        return await self._ping_once()

    async def _control_roundtrip(self, frame: dict[str, Any]) -> dict[str, Any] | None:
        """Send one control frame on a fresh connection and return the reply.

        ``None`` on any transport, timeout, decode or non-object reply — every
        caller here treats an unreadable answer as "no answer", never as a
        negative one, so the distinction is not worth propagating. Bounded by
        ``_PING_TIMEOUT_SECS`` at each of connect / drain / read, which is what
        keeps a wedged daemon from stalling gateway startup.

        One implementation for ping, stats and stand-down: all three are the
        same one-shot request/response against the daemon's control surface, and
        three hand-rolled copies of this connect-write-read-close dance is how
        one of them ends up missing a timeout or leaking a writer.
        """
        try:
            reader, writer = await asyncio.wait_for(
                transport.connect(
                    self._spec.socket_path,
                    limit=READ_BUFFER_LIMIT_BYTES,
                ),
                timeout=_PING_TIMEOUT_SECS,
            )
        except (asyncio.TimeoutError, OSError) as exc:
            logger.warning(
                "mcp-gateway %s connect failed: %s", frame.get("type", "control"), exc
            )
            return None
        try:
            writer.write(json.dumps(frame).encode("utf-8") + b"\n")
            await asyncio.wait_for(writer.drain(), timeout=_PING_TIMEOUT_SECS)
            line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=_PING_TIMEOUT_SECS)
            msg = json.loads(line.decode("utf-8"))
            return msg if isinstance(msg, dict) else None
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError,
                asyncio.LimitOverrunError, UnicodeDecodeError, json.JSONDecodeError,
                OSError):
            return None
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _ping_probe(self) -> dict[str, Any] | None:
        """Return the daemon's ``pong`` frame, or ``None`` if it did not answer.

        The payload matters and not just the fact of a reply: ``pong`` carries
        the fingerprint of the target set the answering daemon can serve, which
        is what :meth:`_adopt_or_stand_down` needs to decide whether adopting it
        is safe. :meth:`_ping_once` remains the boolean liveness view for the
        callers that only ask "is it alive".
        """
        msg = await self._control_roundtrip({"type": "ping"})
        if msg is None or msg.get("type") != "pong":
            return None
        return msg

    async def _ping_once(self) -> bool:
        """Return ``True`` iff the daemon replies ``{"type":"pong"}`` within
        ``_PING_TIMEOUT_SECS``. Any transport or parse error → ``False``.
        """
        return await self._ping_probe() is not None

    def _wanted_target_fingerprint(self) -> str:
        """Fingerprint of the target set the daemon we want would serve.

        Computed over the SAME environment :meth:`_spawn_once` would hand a
        fresh daemon — the scrubbed parent environment with
        ``spec.mcp_target_env`` overlaid, in that precedence — so a fresh spawn
        is fingerprint-equal to this value by construction. That equality is
        the whole guarantee: comparing it against a running daemon's reported
        value answers "could this daemon serve the specs we are about to write"
        without asking it about servers one at a time.

        The credential scrub cannot touch the answer (it removes no
        target-prefixed key) but is applied anyway, so this stays a copy of the
        spawn env rather than a lookalike that drifts if either list changes.
        """
        env = {**_scrub_sensitive_env(dict(os.environ)), **self._spec.mcp_target_env}
        return hash_target_env(env)

    async def _adopt_or_stand_down(self, pong: dict[str, Any]) -> str:
        """Decide what to do about the daemon that answered ``pong``.

        Returns :data:`_ADOPT` (keep the incumbent), :data:`_SPAWN` (it released
        the socket, put our own daemon there) or :data:`_ABORT` (neither is safe
        — fail the start).

        Four outcomes, and only one of them is a fail-open:

        * **Fit** → ``_ADOPT``. Its target set matches what we are about to
          write. The ordinary case, including the sibling-manager race that
          adoption was built for, since a sibling in this process computes the
          same fingerprint from the same spec.
        * **Unfit, yields** → ``_SPAWN``. It serves a different set (or cannot
          say what it serves), so every server whose entry moved would be
          rejected terminally for the life of each session that connects. It
          stood down and released the singleton lock.
        * **Unfit, accepted but still draining** → ``_ABORT``. It has already
          closed its accept loop, so adopting it would be WORSE than the
          mismatch this change exists to fix: a daemon that answers ping but
          accepts no new connection turns a partial outage (the changed servers)
          into a total one (every server), while reporting success. Spawning is
          not available either, because it still holds the lock. Fail the start;
          the socket frees itself moments later.
        * **Unfit, will not yield** → ``_ADOPT``, the deliberate fail-open. A
          refusal, or a pre-fingerprint daemon that does not understand the
          frame. It is still accepting connections, so adopting preserves the
          servers that DO work; refusing would leave the socket held by a daemon
          nobody supervises and no working gateway at all. Logged at ERROR: this
          failure used to be silent, and "one MCP server is broken for no
          reason" was all an operator had to go on.

        A daemon that reports no fingerprint is treated as UNFIT, not as fit.
        Unknown is not the same as wrong, but adopting on unknown is what
        reproduces the reported bug on this fix's very first deployment: the
        survivor of a gateway that died without its shutdown path after a package
        upgrade is exactly a daemon too old to report a target set. Asking it to
        stand down costs one bounded round-trip that such a daemon answers by
        closing the connection (it refuses an unrecognised first frame), after
        which the fail-open branch adopts it with the breakage named — strictly
        more informative than adopting it silently, and free in steady state
        because a current daemon always reports.
        """
        served = pong.get("targets")
        wanted = self._wanted_target_fingerprint()
        if isinstance(served, str) and served and served == wanted:
            return _ADOPT
        described = served[:12] if isinstance(served, str) and served else "<unreported>"
        if self._stand_downs_issued >= _MAX_STAND_DOWN_REQUESTS:
            # Oscillation guard. Reached only when this process has already asked
            # _MAX_STAND_DOWN_REQUESTS times, which in practice means another
            # live gateway instance keeps re-winning the socket with a different
            # target set. Stop asking and settle: adopting a serving daemon
            # leaves the servers it CAN serve working, which beats trading the
            # socket back and forth forever.
            logger.error(
                "mcp-gateway: incumbent on %s serves target set %s, not the %s "
                "the specs need, but this gateway has already issued %d "
                "stand-downs — adopting it instead of contending further. "
                "Another gateway instance is likely sharing this socket path "
                "with a different target set; the servers whose launch command "
                "differs will be REJECTED for new sessions.",
                self._spec.socket_path,
                described,
                wanted[:12],
                self._stand_downs_issued,
            )
            return _ADOPT
        logger.warning(
            "mcp-gateway: incumbent on %s serves target set %s but the agent "
            "specs being written need %s — asking it to stand down",
            self._spec.socket_path,
            described,
            wanted[:12],
        )
        outcome = await self._request_stand_down(wanted)
        if outcome == _RELEASED:
            logger.info(
                "mcp-gateway: incumbent stood down and released %s — spawning a "
                "daemon for the current target set",
                self._spec.socket_path,
            )
            return _SPAWN
        if outcome == _DRAINING:
            logger.error(
                "mcp-gateway: incumbent on %s accepted the stand-down but had not "
                "released the socket within %.0fs. It is draining and no longer "
                "accepting connections, so it is NOT adopted — adopting a daemon "
                "that cannot accept would make every server unreachable instead "
                "of the changed ones. Starting without a shared broker; sessions "
                "fall back to per-session MCP and the next start finds it free.",
                self._spec.socket_path,
                _SHUTDOWN_GRACE_SECS,
            )
            return _ABORT
        logger.error(
            "mcp-gateway: incumbent on %s serves target set %s, not the %s the "
            "agent specs were just written for, and refused to release the socket "
            "— adopting it anyway. Every stubbed server whose launch command "
            "changed since that daemon started will be REJECTED for new "
            "sessions until it is stopped and the gateway restarted.",
            self._spec.socket_path,
            described,
            wanted[:12],
        )
        return _ADOPT

    async def _request_stand_down(self, wanted: str) -> str:
        """Ask the incumbent to yield the socket.

        Returns :data:`_RELEASED` (it accepted and the lock is free),
        :data:`_DRAINING` (it accepted but has not finished within the budget) or
        :data:`_REFUSED` (it did not accept, or never answered). The caller must
        keep ``_DRAINING`` distinct from ``_REFUSED``: a daemon that accepted has
        already stopped accepting connections, so it is not adoptable, whereas a
        daemon that refused is still serving and is.

        Voluntary by design. The starting gateway must not take the endpoint
        itself: :meth:`_clear_stale_socket` is a connect-probe-then-unlink, and
        in its documented false-stale window that unlinks a LIVE incumbent's
        socket — the socket-theft class the flock guard exists to prevent. Here
        the incumbent performs its own SIGTERM-equivalent drain and removes its
        own endpoint, so there is no stale-vs-live judgement to get wrong: the
        request only reaches a daemon that just answered on that socket.

        ``wanted`` travels with the request so the daemon can refuse a stand-down
        it has no reason to perform (see ``gatewayd._apply_stand_down``).

        What is waited ON is the singleton lock becoming free, NOT the endpoint
        disappearing, and the difference is load-bearing. A draining daemon stops
        accepting first and releases the lock last, so the endpoint goes away
        while the lock is still held — on Windows for the daemon's whole drain,
        since the kernel drops a pipe name as soon as the last handle closes. A
        replacement spawned in that gap loses the lock, exits rc=0 without
        binding, and ``_wait_for_socket`` then fails the start with no watchdog
        left to retry. The lock is precisely what the replacement must win, so it
        is the only correct readiness signal.

        The wait is bounded by the daemon's own published shutdown budget,
        because that is how long a graceful drain is allowed to take. It also
        gives up as soon as ``_stopping`` is set, so a shutdown racing a start is
        not made to wait out another process's drain.
        """
        reply = await self._control_roundtrip({"type": "stand-down", "want": wanted})
        self._stand_downs_issued += 1
        if reply is None or reply.get("type") != "standing-down":
            logger.warning(
                "mcp-gateway: stand-down request on %s was not accepted (%s)",
                self._spec.socket_path,
                (reply or {}).get("reason") or "no answer",
            )
            return _REFUSED
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _SHUTDOWN_GRACE_SECS
        while loop.time() < deadline:
            if self._stopping:
                logger.info(
                    "mcp-gateway: abandoning the stand-down wait on %s — shutting down",
                    self._spec.socket_path,
                )
                return _DRAINING
            if await asyncio.to_thread(transport.singleton_lock_free, self._spec.socket_path):
                return _RELEASED
            await asyncio.sleep(_SOCKET_POLL_INTERVAL_SECS)
        if await asyncio.to_thread(transport.singleton_lock_free, self._spec.socket_path):
            return _RELEASED
        return _DRAINING

    async def stats(self) -> dict:
        """Return the daemon's pool snapshot, or ``{}`` on any error."""
        msg = await self._control_roundtrip({"type": "stats"})
        return msg if msg is not None and msg.get("type") == "stats" else {}

    async def _run_watchdog(self) -> None:
        """Supervise the daemon: respawn on exit or on liveness failure.

        Watches TWO signals so it also catches the silent-zombie mode
        (accept loop dead, Python process still alive), which watching
        ``proc.wait()`` alone would miss: (1) process exit, and
        (2) a periodic ping round-trip. Whichever fires first wins,
        after which we respawn with exponential backoff.
        """
        backoff = _RESPAWN_BACKOFF_START_SECS
        while not self._stopping:
            proc = self._process
            if proc is None:
                if self._adopted:
                    # Supervise a foreign-owned (adopted) daemon by ping —
                    # we hold no process handle. While it answers, keep
                    # watching; when it dies, surface it and re-elect: drop
                    # adoption and try to become the owner. The flock
                    # arbitrates if a sibling races us; a flock-loser exits
                    # rc=0 and we re-adopt on the next loop.
                    if await self._ping_once():
                        await asyncio.sleep(_LIVENESS_PING_INTERVAL_SECS)
                        continue
                    logger.warning(
                        "mcp-gateway: adopted daemon on %s is gone — "
                        "re-electing (spawning a replacement)",
                        self._spec.socket_path,
                    )
                    self._adopted = False
                    try:
                        await self._clear_stale_socket()
                        await self._spawn_once()
                    except Exception:
                        logger.exception(
                            "mcp-gateway: re-spawn after adopted-daemon "
                            "death failed — will retry"
                        )
                        # Restore adoption so the next iteration re-enters the
                        # adopted branch, re-pings (the daemon is still gone),
                        # and retries the spawn — otherwise _process stays None
                        # with _adopted False and the loop idles forever with
                        # no retry. Escalate backoff (mirroring the main
                        # proc-exit path) so a persistent spawn failure does not
                        # hot-loop at the floor interval.
                        self._adopted = True
                        await asyncio.sleep(backoff)
                        backoff = min(backoff * 2, _RESPAWN_BACKOFF_MAX_SECS)
                    else:
                        # Spawned — reset backoff; the next iteration enters the
                        # wait-race to supervise the fresh process.
                        backoff = _RESPAWN_BACKOFF_START_SECS
                    continue
                # proc is None and we are NOT adopting: we are the owner but
                # our last _spawn_once() raised (e.g. a transient fork()/open()
                # error) and left _process None. Retry the spawn here — without
                # this retry a single spawn failure would leave the watchdog
                # idling forever with no daemon and no retry (permanent wedge).
                try:
                    await self._clear_stale_socket()
                    await self._spawn_once()
                except Exception:
                    logger.exception(
                        "mcp-gateway: owner respawn failed — will retry"
                    )
                    # Escalate backoff (mirroring the main proc-exit path) so a
                    # persistent spawn failure does not hot-loop at the floor.
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _RESPAWN_BACKOFF_MAX_SECS)
                    continue
                # Spawned — reset backoff; next iteration enters the wait-race.
                backoff = _RESPAWN_BACKOFF_START_SECS
                continue

            wait_task = asyncio.ensure_future(proc.wait())
            ping_task = asyncio.ensure_future(self._liveness_probe_loop())

            exit_reason = "unknown"
            rc: Any = None
            try:
                done, _pending = await asyncio.wait(
                    {wait_task, ping_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if wait_task in done:
                    rc = wait_task.result()
                    exit_reason = f"daemon exited rc={rc}"
                elif ping_task in done:
                    # Liveness probe decided the daemon is a zombie.
                    exit_reason = ping_task.result() or "liveness probe failed"
                    # Kill the zombie so respawn gets a clean PID. We use
                    # _terminate_process so the usual SIGTERM→SIGKILL
                    # grace applies — a zombie won't respond to SIGTERM
                    # but SIGKILL always lands.
                    logger.warning(
                        "mcp-gateway: %s — killing zombie pid=%s",
                        exit_reason, proc.pid,
                    )
                    await self._terminate_process(grace_secs=_SHUTDOWN_GRACE_SECS)
                    rc = proc.returncode
            except asyncio.CancelledError:
                for t in (wait_task, ping_task):
                    t.cancel()
                raise
            except Exception:
                logger.exception("mcp-gateway: watchdog race failed")
                exit_reason = "watchdog race exception"
                rc = -1
            finally:
                for t in (wait_task, ping_task):
                    if not t.done():
                        t.cancel()
                        with contextlib.suppress(
                            asyncio.CancelledError, Exception
                        ):
                            await t

            # Clear the handle once we've consumed the exit so a failed
            # respawn below doesn't cause the next iteration to re-wait on
            # the same dead process, re-log the same rc, and double backoff
            # spuriously.
            self._process = None
            if self._stopping:
                return
            logger.warning(
                "mcp-gateway: %s — respawning in %.1fs", exit_reason, backoff,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _RESPAWN_BACKOFF_MAX_SECS)
            if self._stopping:
                return
            # Before respawning, check whether another daemon already owns
            # the socket healthily — a sibling manager may have won the race,
            # or gatewayd's flock guard rejected our last spawn (it exited
            # rc=0 without binding). If so, adopt the incumbent and stand the
            # watchdog down instead of respawn-looping doomed competitors.
            # Same fitness gate as the startup path: the incumbent this loop
            # meets is reached by exactly the same reasoning, so a respawn must
            # not silently accept a target set that startup would have refused.
            #
            # No SEPARATE post-respawn fitness check is needed here, unlike in
            # _start_locked. A respawn that loses the flock exits rc=0, the
            # wait-race below observes that exit, and control returns to THIS
            # gate — so an unfit daemon that won the socket is assessed within
            # one backoff cycle rather than accepted permanently. _start_locked
            # needed its own check because it returns to the caller instead of
            # looping back to a gate.
            pong = await self._ping_probe()
            if pong is not None:
                verdict = await self._adopt_or_stand_down(pong)
                if verdict == _ABORT:
                    # Draining incumbent: neither adoptable nor replaceable yet.
                    # Back off and let the next iteration re-assess rather than
                    # spawning into a lock that is still held.
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, _RESPAWN_BACKOFF_MAX_SECS)
                    continue
                if verdict == _ADOPT:
                    self._adopted = True
                    logger.info(
                        "mcp-gateway: socket %s already served by another daemon "
                        "— adopting; watchdog will supervise it via ping",
                        self._spec.socket_path,
                    )
                    # continue (NOT return): re-enter the loop so the adopted
                    # branch (proc is None and self._adopted) supervises the
                    # incumbent by ping and re-elects if it later dies. Returning
                    # here would terminate the watchdog and leave the adopted
                    # daemon unsupervised.
                    continue
            try:
                await self._clear_stale_socket()
                await self._spawn_once()
            except Exception:
                logger.exception("mcp-gateway: respawn failed — will retry")
                continue
            # Reset backoff after a successful respawn that stays alive
            # for at least 30s.
            await asyncio.sleep(30.0)
            if self._process is not None and self._process.returncode is None:
                backoff = _RESPAWN_BACKOFF_START_SECS

    async def _liveness_probe_loop(self) -> str:
        """Ping the daemon every ``_LIVENESS_PING_INTERVAL_SECS``.

        Returns a human-readable reason string as soon as
        ``_LIVENESS_MAX_CONSECUTIVE_FAILURES`` consecutive ping round-trips
        fail. Never returns normally — either the coroutine is cancelled
        by the outer watchdog race (daemon exited first) or it returns a
        failure reason.
        """
        consecutive_failures = 0
        while True:
            await asyncio.sleep(_LIVENESS_PING_INTERVAL_SECS)
            if self._stopping:
                # Outer loop will notice _stopping and exit; yield a
                # benign reason that gets ignored on stop.
                return "stopping"
            ok = await self._ping_once()
            if ok:
                consecutive_failures = 0
                continue
            consecutive_failures += 1
            logger.warning(
                "mcp-gateway: liveness ping failed (%d/%d consecutive)",
                consecutive_failures, _LIVENESS_MAX_CONSECUTIVE_FAILURES,
            )
            if consecutive_failures >= _LIVENESS_MAX_CONSECUTIVE_FAILURES:
                return (
                    f"zombie detected: {consecutive_failures} consecutive "
                    f"ping failures over "
                    f"{int(consecutive_failures * _LIVENESS_PING_INTERVAL_SECS)}s"
                )

    async def _terminate_process(self, *, grace_secs: float) -> None:
        proc = self._process
        self._process = None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.send_signal(signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=grace_secs)
        except asyncio.TimeoutError:
            logger.warning("mcp-gateway: SIGTERM timeout, escalating to SIGKILL")
            try:
                proc.kill()
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.error("mcp-gateway: SIGKILL also timed out — pid=%s", proc.pid)
            # SIGKILL skips gatewayd's pool.shutdown_all(), so its pooled MCP
            # backends (each a session leader via start_new_session) reparent to
            # init and leak. Reap the pgids gatewayd persisted out-of-band.
            await self._reap_orphaned_backends()

    async def _reap_orphaned_backends(self) -> None:
        """Best-effort tree-kill of pooled backends left orphaned by a SIGKILLed
        gatewayd, read from the ``<socket>.backends`` sidecar the daemon
        maintains. Each recorded pid is a session leader (pid == pgid) on POSIX;
        on Windows there is no process group (spawn's ``start_new_session`` is
        inert there), so the recorded pid is treated as a tree root instead."""
        pidfile = Path(f"{self._spec.socket_path}.backends")
        try:
            raw = pidfile.read_text(encoding="utf-8")
        except OSError:
            return
        for token in raw.split():
            try:
                pid = int(token)
            except ValueError:
                continue
            # platform_compat rather than os.killpg: that name is absent on
            # Windows and the handler below would not catch the AttributeError.
            # Async variant required — this is awaited from
            # _terminate_process, and the Windows branch spawns taskkill with a
            # 5s timeout once per recorded pid, which would stall the loop.
            with contextlib.suppress(
                ProcessLookupError, PermissionError, OSError, ValueError
            ):
                await platform_compat.kill_process_tree_async(pid, platform_compat.SIGKILL)
        with contextlib.suppress(OSError):
            pidfile.unlink()

    async def _clear_stale_socket(self) -> None:
        """Remove an endpoint left behind by a prior crash.

        Delegates to :func:`transport.remove_stale`, which verifies the
        endpoint is not live before removing it (a live one means another
        daemon is bound; leaving it in place lets the bind fail with
        EADDRINUSE, which is the correct user-visible error) and offloads the
        blocking probe so the event loop is never stalled. A no-op on Windows,
        where a named pipe leaves nothing behind to clean up.
        """
        await transport.remove_stale(self._spec.socket_path)

    @staticmethod
    async def _wait_for_socket(path: Path, timeout: float) -> bool:
        """Poll until the endpoint is reachable, or the deadline passes.

        Reachability rather than a directory entry: a Windows named pipe has no
        filesystem presence, so ``transport.endpoint_exists`` probes it. The
        Windows probe blocks briefly, so it runs off the loop.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if await asyncio.to_thread(transport.endpoint_exists, path):
                return True
            await asyncio.sleep(_SOCKET_POLL_INTERVAL_SECS)
        return await asyncio.to_thread(transport.endpoint_exists, path)
