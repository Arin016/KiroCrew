"""Process tracking and orphan cleanup for kiro-cli sessions.

Manages PID files (``kiro_pids.txt`` and ``kiro_session_pids.txt``) that
track spawned kiro-cli processes.  Provides startup cleanup, periodic
sweeping, and per-process track/untrack operations.

See ``session.py`` module docstring for the full Process Sweep Architecture.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path

# fcntl via flock_compat: POSIX-only, shimmed to a no-op on Windows so the
# CLI (and thus `kiroclaw cloud` on Windows) can import this module. The
# gateway/cron machinery that actually locks runs only on macOS/Linux.
from kiro_claw import flock_compat as fcntl
from kiro_claw.config.paths import config_dir
from kiro_claw.providers.base import LLMProvider

logger = logging.getLogger(__name__)

_PID_FILE = "kiro_pids.txt"
_SESSION_PID_FILE = "kiro_session_pids.txt"


def _pid_file_path() -> Path:
    return config_dir() / _PID_FILE


def _session_pid_file_path() -> Path:
    return config_dir() / _SESSION_PID_FILE


@contextmanager
def _session_pid_file_lock():  # type: ignore[no-untyped-def]
    """Exclusive file lock for session PID file operations."""
    lock_path = _session_pid_file_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def _track_session_pid(pid: int) -> None:
    """Append a kiro-cli PID to the session tracking file (dedup).

    Entries are written as ``<gateway_pid>:<child_pid>`` so each gateway
    instance can identify and sweep only its own children.
    """
    entry = f"{os.getpid()}:{pid}"
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = set(path.read_text(encoding="utf-8").split())
            if entry in existing:
                return
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{entry}\n")


@contextmanager
def _pid_file_lock():  # type: ignore[no-untyped-def]
    """Exclusive file lock for all PID file read-modify-write operations."""
    lock_path = _pid_file_path().with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w") as lock_fd:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)


def _is_managed_agent_process(pid: int) -> bool:
    """Check if a PID belongs to an agent process managed by KiroClaw (guards against PID recycling)."""
    try:
        if sys.platform == "linux":
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            return b"kiro-cli" in cmdline or b"claude" in cmdline
        # macOS: use ps
        out = subprocess.check_output(
            ["ps", "-o", "command=", "-p", str(pid)],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        return b"kiro-cli" in out or b"claude" in out
    except Exception:
        return False


def _collect_active_pids(sessions: "dict") -> tuple[set[int], bool]:
    """Extract PIDs from live sessions. Returns ``(pids, ok)``.

    If any session's PID is not an int or extraction fails,
    returns ``(partial_set, False)`` — caller should skip the sweep.
    """
    pids: set[int] = _protected_pids()  # shared _bg / subagent runtimes shielded from the sweep
    for sess in sessions.values():
        # ACP provider: long-lived process PID via client._pid
        client = getattr(sess.provider, "client", None)
        if client is not None:
            try:
                pid = client._pid  # type: ignore[attr-defined]
                if not isinstance(pid, int):
                    logger.warning(
                        "PID for session is not an int (%r) — skipping orphan sweep this cycle", pid
                    )
                    return pids, False
                pids.add(pid)
            except Exception:
                logger.warning("Failed to read PID for session — skipping orphan sweep this cycle")
                return pids, False
        # CC provider: protect long-lived process PID (per_session mode)
        cc_proc = getattr(sess.provider, "_proc", None)
        if cc_proc is not None and cc_proc.returncode is None:
            pids.add(cc_proc.pid)
        # CC provider: protect in-flight subprocess PID (ephemeral mode)
        active_proc = getattr(sess.provider, "_active_proc", None)
        if active_proc is not None and active_proc.returncode is None:
            pids.add(active_proc.pid)
    return pids, True


def _kill_pid_tree(pid: int) -> tuple[int, bool]:
    """Kill *pid* and its descendant kiro-cli processes (bottom-up).

    Returns ``(total_killed, root_killed)`` so callers can distinguish
    whether the root process itself was sent SIGKILL.
    """
    if pid <= 0:
        return 0, False
    killed = 0
    root_killed = False
    try:
        # circular import: session_pid → acp.client → session → session_pid
        from kiro_claw.acp.client import _get_child_pids

        children = _get_child_pids(pid)
        for cpid in reversed(children):
            if cpid <= 0 or not _is_managed_agent_process(cpid):
                continue
            try:
                os.kill(cpid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError, OSError):
                pass
    except Exception:
        logger.debug("Error killing children of PID %s", pid, exc_info=True)
    if not _is_managed_agent_process(pid):
        return killed, root_killed
    try:
        os.kill(pid, signal.SIGKILL)
        killed += 1
        root_killed = True
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return killed, root_killed


def _write_back_pid_file(killed_or_dead: set[str]) -> None:
    """Remove *killed_or_dead* entries from the session PID file."""
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        if path.exists():
            current = path.read_text(encoding="utf-8").splitlines()
            keep = [
                entry for entry in current
                if entry.strip() and entry.strip() not in killed_or_dead
            ]
            path.write_text(
                ("\n".join(keep) + "\n") if keep else "",
                encoding="utf-8",
            )


def _sweep_pid_entries(
    lines: list[str],
    *,
    should_skip_tagged: "Callable[[int, int], bool]",
    should_skip_bare: "Callable[[int], bool]",
    is_managed: "Callable[[int], bool] | None" = None,
    dry_run: bool = False,
) -> tuple[int, set[str], list[int]]:
    """Shared per-entry sweep logic for startup and periodic cleanup.

    Parses each line, applies caller-provided skip predicates, probes
    liveness, and either kills orphaned kiro-cli processes or collects
    them as candidates (when *dry_run* is True).

    Returns:
        ``(killed_count, killed_or_dead_entries, candidates)`` where
        *candidates* is non-empty only when ``dry_run=True``.
    """
    killed = 0
    killed_or_dead: set[str] = set()
    candidates: list[int] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            if ":" in stripped:
                parts = stripped.split(":", 1)
                try:
                    gw_pid = int(parts[0])
                    pid = int(parts[1])
                except ValueError:
                    killed_or_dead.add(stripped)
                    continue
                if gw_pid <= 0 or pid <= 0:
                    killed_or_dead.add(stripped)
                    continue
                if should_skip_tagged(gw_pid, pid):
                    continue
            else:
                try:
                    pid = int(stripped)
                except ValueError:
                    killed_or_dead.add(stripped)
                    continue
                if pid <= 0:
                    killed_or_dead.add(stripped)
                    continue
                if should_skip_bare(pid):
                    continue
            # Probe liveness
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                killed_or_dead.add(stripped)
                continue
            except PermissionError:
                logger.debug("No permission to signal PID %s — skipping", pid)
                continue
            except OSError:
                continue
            # Managed check (periodic only)
            if is_managed is not None and is_managed(pid):
                continue
            if not _is_managed_agent_process(pid):
                killed_or_dead.add(stripped)
                continue
            if dry_run:
                candidates.append(pid)
                continue
            total_killed, root_killed = _kill_pid_tree(pid)
            killed += total_killed
            if root_killed:
                killed_or_dead.add(stripped)
            else:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    killed_or_dead.add(stripped)
                except (PermissionError, OSError):
                    pass
        except Exception:
            logger.debug("Error processing PID entry %s", stripped, exc_info=True)
    return killed, killed_or_dead, candidates


def _periodic_pid_sweep(my_gw_pid: int, active_pids: set[int]) -> tuple[set[str], list[int]]:
    """Phase 1: identify orphan candidates in a thread (no killing).

    Returns ``(killed_or_dead, candidates)`` where *killed_or_dead* are
    entries to prune (dead/invalid) and *candidates* are PIDs that appear
    orphaned and should be killed — but the final kill decision is made
    back on the event loop where ``self._sessions`` is authoritative.
    """
    path = _session_pid_file_path()
    if not path.exists():
        return set(), []
    lock_path = path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = open(lock_path, "w")
    except OSError:
        return set(), []
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return set(), []
        try:
            lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        lock_fd.close()

    if not lines:
        return set(), []

    _, killed_or_dead, candidates = _sweep_pid_entries(
        lines,
        should_skip_tagged=lambda gw, _p: gw != my_gw_pid,
        should_skip_bare=lambda _p: True,
        is_managed=lambda p: p in active_pids,
        dry_run=True,
    )
    return killed_or_dead, candidates


def _kill_confirmed_and_writeback(
    my_gw_pid: int, confirmed: list[int], killed_or_dead: set[str]
) -> int:
    """Phase 2b: kill confirmed orphans and write back PID file (sync, thread-safe)."""
    orphan_killed = 0
    for pid in confirmed:
        total, root = _kill_pid_tree(pid)
        orphan_killed += total
        if root:
            killed_or_dead.add(f"{my_gw_pid}:{pid}")
        else:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                killed_or_dead.add(f"{my_gw_pid}:{pid}")
            except (PermissionError, OSError):
                pass
    if killed_or_dead:
        _write_back_pid_file(killed_or_dead)
    return orphan_killed


def _sync_kill_provider(provider: LLMProvider) -> None:
    """Synchronously kill a provider's process.

    Used during CancelledError handling where async shutdown is unreliable
    (asyncio.shield + await raises CancelledError immediately, leaving
    shutdown fire-and-forget).  Falls back to SIGKILL if SIGTERM fails.
    """
    # ACP provider: long-lived process via client._pid
    client = getattr(provider, "_client", None)
    pid = getattr(client, "_pid", None) if client else None
    # CC provider: long-lived process via _proc.pid or ephemeral via _active_proc.pid
    if pid is None:
        proc = getattr(provider, "_proc", None)
        if proc is not None and proc.returncode is None:
            pid = proc.pid
    if pid is None:
        proc = getattr(provider, "_active_proc", None)
        if proc is not None and proc.returncode is None:
            pid = proc.pid
    if pid is None:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return  # already dead
        except OSError:
            return
        if sig == signal.SIGTERM:
            # Brief wait for graceful exit before escalating
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                return
    logger.warning("_sync_kill_provider: killed PID %d for leaked provider", pid)


_libproc: ctypes.CDLL | None = None


def _get_ppid_libproc(pid: int) -> int:
    """Get parent PID via libproc (macOS) - no entitlement required."""
    global _libproc
    if _libproc is None:
        path = ctypes.util.find_library("proc")
        if path is None:
            raise OSError("libproc not found")
        _libproc = ctypes.CDLL(path)
        _libproc.proc_pidinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
            ctypes.c_void_p, ctypes.c_int,
        ]
        _libproc.proc_pidinfo.restype = ctypes.c_int
    PROC_PIDTBSDINFO = 3  # noqa: N806 — macOS kernel constant
    buf = ctypes.create_string_buffer(136)
    ret = _libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buf, 136)
    if ret <= 0:
        return -1
    return struct.unpack_from("<I", buf.raw, 16)[0]


def _cleanup_orphaned_mcp_servers() -> int:
    """Kill tracked child PIDs whose parent kiro-cli session is dead.

    Child entries are stored as ``child_pid:parent_pid`` in ``kiro_pids.txt``.
    A child is orphaned when its parent PID is no longer alive.  Bare PID
    lines (sandbox root PIDs) are pruned when the process is confirmed dead.

    Zero false positives: we only kill PIDs we tracked, and only when the
    specific parent session that spawned them is confirmed dead.
    """
    path = _pid_file_path()
    if not path.exists():
        return 0

    # Hold the lock for the entire read-kill-write cycle so that a concurrent
    # _untrack_child_pids (clean shutdown) cannot remove an entry between our
    # read and our kill decision.  os.kill is non-blocking so lock duration is
    # negligible.
    with _pid_file_lock():
        lines = path.read_text(encoding="utf-8").splitlines()
        killed = 0
        lines_to_remove: set[str] = set()

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if ":" not in stripped:
                # Bare PID (sandbox root). Prune if dead.
                try:
                    os.kill(int(stripped), 0)
                except ProcessLookupError:
                    lines_to_remove.add(stripped)
                except (ValueError, PermissionError, OSError):
                    pass
                continue
            parts = stripped.split(":", 1)
            try:
                child_pid = int(parts[0])
                parent_pid = int(parts[1])
            except (ValueError, IndexError):
                continue

            # Is the child still alive?
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                lines_to_remove.add(stripped)  # confirmed dead — prune
                continue
            except (PermissionError, OSError):
                continue  # alive (different user) or unknown — leave alone

            # Is the parent session still alive?
            try:
                os.kill(parent_pid, 0)
            except ProcessLookupError:
                # Parent confirmed dead → child is orphaned — kill it.
                # Guard against PID reuse: if the child was truly ours, its
                # PPid should be 1 (reparented to init) since the parent died.
                # A reused PID would have a different PPid.
                try:
                    if sys.platform == "linux":
                        ppid_line = Path(f"/proc/{child_pid}/status").read_text()
                        for ln in ppid_line.splitlines():
                            if ln.startswith("PPid:"):
                                actual_ppid = int(ln.split()[1])
                                break
                        else:
                            actual_ppid = -1
                    else:
                        # Use libproc directly - ps requires
                        # com.apple.system-task-ports.read entitlement
                        # on macOS 26+ which kiro-cli lacks.
                        actual_ppid = _get_ppid_libproc(child_pid)
                except Exception:
                    actual_ppid = -1  # can't read - process died between checks
                if actual_ppid not in (1, parent_pid):
                    # PID was reused by an unrelated process — just prune
                    lines_to_remove.add(stripped)
                    continue
                try:
                    os.kill(child_pid, signal.SIGKILL)
                    killed += 1
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                lines_to_remove.add(stripped)
            except (PermissionError, OSError):
                continue  # cannot confirm death — leave child alone

        if lines_to_remove:
            kept = [ln for ln in lines if ln.strip() not in lines_to_remove]
            path.write_text(
                "\n".join(kept) + "\n" if kept else "",
                encoding="utf-8",
            )

    return killed


def cleanup_orphaned_sessions() -> None:
    """Kill leftover kiro-cli processes from a previous gateway run.

    Reads ``kiro_session_pids.txt`` (written at spawn time), validates each
    PID still belongs to a kiro-cli process (guards against PID recycling),
    kills descendants bottom-up, then truncates the file.

    Runs at gateway startup before any new sessions are created, so the file
    contains only PIDs from the previous run.

    Also sweeps orphaned MCP server processes via ``_cleanup_orphaned_mcp_servers``
    which uses the separate ``kiro_pids.txt`` (child:parent format).

    Additionally cleans up:
    - Stale ``session_pid_*.txt`` files for processes that no longer exist.
    - Empty directories under ``sessions/`` left by subagents that produced
      no output before timing out.
    """
    # Step 1: Read file under lock (fast I/O only)
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        lines: list[str] = path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    # Step 2: Process outside lock (slow: os.kill, _get_child_pids, SIGKILL)
    def _skip_tagged(gw_pid: int, _pid: int) -> bool:
        """Skip if owning gateway is still alive."""
        try:
            os.kill(gw_pid, 0)
            return True  # gateway alive — preserve
        except ProcessLookupError:
            return False  # gateway dead — orphan
        except (PermissionError, OSError):
            return True  # can't tell — preserve

    killed, killed_or_dead, _ = _sweep_pid_entries(
        lines,
        should_skip_tagged=_skip_tagged,
        should_skip_bare=lambda _pid: False,  # startup processes all entries
    )

    # Step 3: Re-read and write under lock — only remove handled entries,
    # preserving entries for alive gateways and un-signalable processes.
    if killed_or_dead:
        _write_back_pid_file(killed_or_dead)

    if killed:
        logger.info("Cleaned up %d orphaned kiro-cli processes", killed)

    # Second pass: sweep MCP servers that escaped process-group kill
    mcp_killed = _cleanup_orphaned_mcp_servers()
    if mcp_killed:
        logger.info("Cleaned up %d orphaned MCP server processes", mcp_killed)

    # Third pass: remove stale session_pid_*.txt files for dead processes
    stale_pid_files = 0
    for pid_file in config_dir().glob("session_pid_*.txt"):
        try:
            pid = int(pid_file.stem.removeprefix("session_pid_"))
            os.kill(pid, 0)  # raises ProcessLookupError if dead
        except ProcessLookupError:
            pid_file.unlink(missing_ok=True)
            stale_pid_files += 1
        except ValueError:
            # Malformed filename (e.g. MagicMock leak) -- safe to delete
            logger.debug("Removing malformed pid file: %s", pid_file.name)
            try:
                pid_file.unlink(missing_ok=True)
                stale_pid_files += 1
            except OSError:
                logger.debug("Could not remove malformed pid file: %s", pid_file.name)
        except (PermissionError, OSError):
            pass  # alive but different user, or unexpected
    if stale_pid_files:
        logger.info("Cleaned up %d stale session PID files", stale_pid_files)

    # Fourth pass: remove empty session workspace dirs (orphaned subagent dirs)
    sessions_dir = config_dir() / "sessions"
    empty_dirs = 0
    if sessions_dir.exists():
        for d in sessions_dir.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                try:
                    d.rmdir()
                    empty_dirs += 1
                except OSError:
                    pass  # directory became non-empty or was already removed
    if empty_dirs:
        logger.info("Cleaned up %d empty session workspace dirs", empty_dirs)


def _track_pid(pid: int) -> None:
    """Append a PID to the tracking file."""
    with _pid_file_lock():
        path = _pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{pid}\n")


def _track_child_pids(pids: Mapping[int, object], parent_pid: int = 0) -> None:
    """Append descendant PIDs to the tracking file as ``child:parent`` pairs."""
    if not pids:
        return
    with _pid_file_lock():
        path = _pid_file_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = set(path.read_text(encoding="utf-8").splitlines()) if path.exists() else set()
        with open(path, "a", encoding="utf-8") as f:
            for pid in pids:
                entry = f"{pid}:{parent_pid}"
                if entry not in existing:
                    f.write(f"{entry}\n")
                    existing.add(entry)


def _untrack_child_pids(pids: Mapping[int, object]) -> None:
    """Remove descendant PIDs from the tracking file."""
    if not pids:
        return
    to_remove = {str(p) for p in pids}
    with _pid_file_lock():
        path = _pid_file_path()
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [
            ln for ln in lines if ":" not in ln.strip() or ln.strip().split(":")[0] not in to_remove
        ]
        path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def _untrack_pid(pid: int) -> None:
    """Remove a PID from the tracking file."""
    with _pid_file_lock():
        path = _pid_file_path()
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [ln for ln in lines if ln.strip() != str(pid)]
        path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


def _untrack_session_pid(pid: int) -> None:
    """Remove this gateway's ``<gw_pid>:<pid>`` entry from the session PID
    tracking file.  Called on clean provider shutdown so the periodic
    orphan sweep doesn't race against legitimate still-running kiro-cli
    processes whose in-memory session entry has transiently gone away
    (e.g. during compaction/reset/replace)."""
    entry = f"{os.getpid()}:{pid}"
    with _session_pid_file_lock():
        path = _session_pid_file_path()
        if not path.exists():
            return
        lines = path.read_text(encoding="utf-8").splitlines()
        lines = [ln for ln in lines if ln.strip() != entry]
        path.write_text("\n".join(lines) + "\n" if lines else "", encoding="utf-8")


# ── Sweep-protected PIDs ──────────────────────────────────────────────────
# Live agent-process PIDs tracked in the PID file but NOT registered as
# SessionMap sessions (e.g. app-managed worker pools / shared ACP runtimes).
# The periodic orphan sweep consults _protected_pids() to avoid killing them.
_PROTECTED_PIDS: set[int] = set()
_PROTECTED_LOCK = threading.Lock()


def register_protected_pid(pid: int) -> None:
    """Shield a live agent-process PID from the periodic orphan sweep.

    For app-managed worker pools whose processes are tracked in the PID file but
    not registered as SessionMap sessions. Pair every call with
    ``unregister_protected_pid`` on worker shutdown/replacement."""
    if isinstance(pid, int) and pid > 0:
        with _PROTECTED_LOCK:
            _PROTECTED_PIDS.add(pid)


def unregister_protected_pid(pid: int) -> None:
    """Drop a PID from the sweep-protected set (worker shut down / replaced)."""
    with _PROTECTED_LOCK:
        _PROTECTED_PIDS.discard(pid)


def _protected_pids() -> set[int]:
    with _PROTECTED_LOCK:
        return set(_PROTECTED_PIDS)


# ── Untracked orphan MCP sweep (defense-in-depth, Mesh-1870) ──────────
# Catches KiroClaw-spawned MCP subtrees that escaped PID-file tracking.
# Split into find + kill so the caller can re-verify active PIDs between phases.

_ORPHAN_SWEEP_MAX_KILLS = 30
_ORPHAN_MIN_AGE_SECONDS = 120  # Never reap processes younger than this

# Entrypoints that positively identify a KiroClaw-spawned MCP/worker process.
# Each marker MUST be unique to a process KiroClaw itself launches — the sweep
# SIGKILLs any user-owned orphan that matches, so a marker naming a server the
# core does not spawn would reap an unrelated process. The upstream MeshClaw
# reaper also lists ``builder-mcp`` (the Amazon-internal server it manages), but
# this public fork never spawns ``builder-mcp`` (the CPP companion contributes
# it, not the core), so that marker is deliberately omitted here.
_MCP_ENTRYPOINT_MARKERS = (
    b"kiroclaw_sandbox_",       # sandbox wrapper script (session-spawned)
    b"kiro_claw.mcp_gateway.stub",  # gateway pool worker (not gatewayd itself)
)

# Gateway/CLI entrypoints — these are peer gateways, never orphan MCP targets.
# Checked BEFORE _MCP_ENTRYPOINT_MARKERS to prevent prefix overlap.
_GATEWAY_MARKERS = (
    b"kiro_claw.mcp_gateway.gatewayd",
    b"kiro_claw.cli",
    b"kiro_claw.__main__",
)


def _our_orphan_pids() -> list[int]:
    """PIDs owned by current user whose parent is init (pid 1) or systemd --user."""
    my_uid = os.getuid()
    # An orphaned process reparents to init (pid 1) or the nearest subreaper
    # (systemd --user), never back to its original launcher. We deliberately do
    # NOT include the gateway's launcher ppid: doing so would widen the
    # candidate set to the launcher's other live children (peer processes from
    # the same shell/tmux/supervisor), adding wrong-kill surface with no
    # orphan-reaping benefit.
    accepted_ppids: set[int] = {1}
    try:
        if sys.platform == "linux":
            # Two /proc passes: pass 1 detects systemd --user subreaper PIDs,
            # pass 2 classifies orphans (needs the complete subreaper set
            # before any child can be matched against accepted_ppids).
            result: list[int] = []
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if entry.stat().st_uid != my_uid:
                        continue
                    pid = int(entry.name)
                    # Detect systemd --user (user-session subreaper)
                    try:
                        if (entry / "comm").read_text().strip() == "systemd":
                            accepted_ppids.add(pid)
                    except OSError:
                        pass
                except (OSError, ValueError):
                    continue
            # Second pass now that accepted_ppids is complete
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    if entry.stat().st_uid != my_uid:
                        continue
                    pid = int(entry.name)
                    for ln in (entry / "status").read_text().splitlines():
                        if ln.startswith("PPid:"):
                            parts = ln.split(maxsplit=1)
                            if len(parts) < 2:
                                break
                            if int(parts[1]) in accepted_ppids:
                                result.append(pid)
                            break
                except (OSError, ValueError, IndexError):
                    pass
            return result
        else:
            result = []
            out = subprocess.check_output(
                ["ps", "-o", "pid=,ppid=", "-U", str(my_uid)],
                stderr=subprocess.DEVNULL, timeout=5,
            )
            for ln in out.decode().splitlines():
                parts = ln.split()
                if len(parts) == 2 and parts[0].isdigit():
                    pid, ppid = int(parts[0]), int(parts[1])
                    if ppid in accepted_ppids:
                        result.append(pid)
            return result
    except Exception:
        logger.warning("_our_orphan_pids failed", exc_info=True)
    return []


def _is_orphan_mcp(cmdline: bytes) -> bool:
    """True if cmdline matches a KiroClaw MCP entrypoint (not a peer gateway)."""
    # Exclude peer gateways — they're not orphan MCP targets
    if any(marker in cmdline for marker in _GATEWAY_MARKERS):
        return False
    # Parse argv: null-separated on Linux, space-separated on macOS ps output
    args = cmdline.split(b"\x00")
    if len(args) == 1:
        args = cmdline.split(b" ")
    argv0 = args[0].rsplit(b"/", 1)[-1]
    # A sandbox/worker script exec'd directly via its shebang puts the script
    # (not a python interpreter) in argv0 — match the marker there too so such
    # orphans aren't missed.
    if any(marker in argv0 for marker in _MCP_ENTRYPOINT_MARKERS):
        return True
    # Otherwise require python interpreter + known entrypoint in remaining args
    if b"python" not in argv0:
        return False
    return any(
        any(marker in a for marker in _MCP_ENTRYPOINT_MARKERS)
        for a in args[1:]
    )


def find_orphan_mcp_candidates(active_pids: set[int]) -> list[int]:
    """Scan process table for orphaned MCP processes not in any active set.

    Returns candidate PIDs. Caller should re-verify against fresh active PIDs
    before killing (two-phase pattern to eliminate races).
    """
    candidates: list[int] = []
    my_pid = os.getpid()
    now = time.time()

    for pid in _our_orphan_pids():
        if pid == my_pid or pid in active_pids:
            continue
        try:
            if sys.platform == "linux":
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
                # Use /proc/pid/stat field 22 (starttime in clock ticks) for
                # canonical process age — immune to /proc mtime heuristic issues.
                pid_age = _linux_pid_age(pid, now)
            else:
                # Single ps call fetches both age and command (two -o flags
                # avoid the BSD header-label comma ambiguity). etime is
                # whitespace-free, so split(None, 1) cleanly separates the
                # two fields.
                ps_out = subprocess.check_output(
                    ["ps", "-o", "etime=", "-o", "command=", "-p", str(pid)],
                    stderr=subprocess.DEVNULL, timeout=2,
                )
                fields = ps_out.split(None, 1)
                pid_age = _parse_etime(
                    fields[0].decode() if fields else ""
                )
                cmdline = fields[1] if len(fields) > 1 else b""
        except Exception:
            logger.debug(
                "Orphan candidate probe failed for pid %s", pid,
                exc_info=True,
            )
            continue
        if pid_age < _ORPHAN_MIN_AGE_SECONDS:
            continue
        if not _is_orphan_mcp(cmdline):
            continue
        candidates.append(pid)

    return candidates


def _linux_pid_age(pid: int, now: float) -> float:
    """Process age in seconds using /proc/pid/stat starttime (canonical)."""
    try:
        stat_data = Path(f"/proc/{pid}/stat").read_text()
        # Field 22 is starttime (after comm which may contain spaces/parens)
        close_paren = stat_data.rfind(")")
        fields = stat_data[close_paren + 2:].split()
        starttime_ticks = int(fields[19])  # field 22 is index 19 after state
        clk_tck = os.sysconf("SC_CLK_TCK")
        uptime = float(Path("/proc/uptime").read_text().split()[0])
        boot_time = now - uptime
        start_seconds = boot_time + (starttime_ticks / clk_tck)
        return now - start_seconds
    except (OSError, ValueError, IndexError):
        return 0.0  # Cannot determine age — min-age guard will skip


def _parse_etime(etime: str) -> float:
    """Parse ps etime format [[DD-]HH:]MM:SS into seconds."""
    try:
        days = 0
        if "-" in etime:
            day_part, etime = etime.split("-", 1)
            days = int(day_part)
        parts = etime.split(":")
        if len(parts) == 3:
            return days * 86400 + int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        elif len(parts) == 2:
            return days * 86400 + int(parts[0]) * 60 + int(parts[1])
    except (ValueError, IndexError):
        pass
    return 0.0


def kill_orphan_mcps(pids: list[int]) -> int:
    """Kill confirmed orphan MCP processes. Uses killpg if isolated, else direct kill.

    Re-verifies cmdline immediately before kill to mitigate PID-reuse TOCTOU.
    """
    my_pgid = os.getpgrp()
    my_pid = os.getpid()
    killed = 0
    for pid in pids:
        if killed >= _ORPHAN_SWEEP_MAX_KILLS:
            break
        if pid == my_pid:
            continue
        try:
            # Re-verify identity right before kill (TOCTOU mitigation):
            # PID may have been recycled between find and kill phases.
            if sys.platform == "linux":
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            else:
                cmdline = subprocess.check_output(
                    ["ps", "-o", "command=", "-p", str(pid)],
                    stderr=subprocess.DEVNULL, timeout=2,
                )
            if not _is_orphan_mcp(cmdline):
                continue
            pgid = os.getpgid(pid)
            if pgid == pid and pgid != my_pgid and pgid > 1:
                os.killpg(pgid, signal.SIGKILL)
                killed += 1
                _sel_orphan_kill(pid, pgid, cmdline, "killpg")
            else:
                # Candidate already passed UID + orphan-ppid + positive MCP
                # marker + two-phase active-PID re-verify + cmdline re-check.
                # Direct os.kill of the confirmed-orphan PID only — NOT a tree
                # walk. _kill_pid_tree is gated by kiro-cli/claude markers that
                # MCP processes don't carry. If this orphan shares a pgid (not
                # its own group leader) and has children, those children that
                # carry an MCP marker are reclaimed on a subsequent sweep; any
                # without a marker were never sweep candidates to begin with.
                os.kill(pid, signal.SIGKILL)
                killed += 1
                _sel_orphan_kill(pid, pgid, cmdline, "kill")
        except (
            ProcessLookupError,
            PermissionError,
            OSError,
            subprocess.SubprocessError,
        ) as exc:
            try:
                # Lazy import: session_pid is imported early by acp.runtime, so
                # a module-level `from kiro_claw.sel import sel` would be circular.
                from kiro_claw.sel import sel
                sel().log_tool_invocation(
                    session_key="gateway",
                    agent="kiroclaw",
                    source="background",
                    tool_name="orphan_mcp_sweep",
                    tool_kind="process_kill",
                    outcome="failed",
                    resources=f"pid={pid}",
                    metadata={"error": str(exc)},
                )
            except Exception:
                logger.debug(
                    "SEL orphan-kill audit failed", exc_info=True
                )
    if killed:
        logger.warning(
            "Orphan MCP sweep: killed %d untracked process(es)", killed
        )
    return killed


def _sel_orphan_kill(
    pid: int, pgid: int, cmdline: bytes, method: str
) -> None:
    """Emit SEL audit event for an orphan MCP kill."""
    try:
        # Lazy import to avoid a circular import (see kill_orphan_mcps).
        from kiro_claw.sel import sel
        sel().log_tool_invocation(
            session_key="gateway",
            agent="kiroclaw",
            source="background",
            tool_name="orphan_mcp_sweep",
            tool_kind="process_kill",
            outcome="completed",
            resources=f"pid={pid} pgid={pgid} method={method}",
            metadata={
                "cmdline": cmdline[:200].decode(
                    "utf-8", errors="replace"
                ),
            },
        )
    except Exception:
        logger.debug("SEL orphan-kill audit failed", exc_info=True)
