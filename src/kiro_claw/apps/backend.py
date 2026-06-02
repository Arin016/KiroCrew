"""App backend process management — spawn, health check, stop, and proxy config.

When an app declares a ``backend`` section in its manifest, KiroClaw manages
the backend process lifecycle: spawn on enable, health-check, stop on disable.
"""
from __future__ import annotations

import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kiro_claw.apps.manager import app_dir, get_app_manifest
from kiro_claw.apps.registry import minimal_env
from kiro_claw.sandbox import wrap_argv
from kiro_claw.sel import sel

logger = logging.getLogger(__name__)

_MIN_PORT = 9100
_MAX_PORT = 9200
_HEALTH_CHECK_TIMEOUT = 5
_HEALTH_CHECK_RETRIES = 15
_HEALTH_CHECK_INTERVAL = 2.0


# ---------------------------------------------------------------------------
# Port allocation
# ---------------------------------------------------------------------------

_allocated_ports: dict[str, int] = {}  # app_name -> port


def _find_free_port() -> int:
    """Find a free TCP port in the app range."""
    for port in range(_MIN_PORT, _MAX_PORT):
        if port in _allocated_ports.values():
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No free ports in range {_MIN_PORT}-{_MAX_PORT}")


# ---------------------------------------------------------------------------
# Process tracking
# ---------------------------------------------------------------------------

@dataclass
class AppProcess:
    """Tracks a running app backend process."""

    app_name: str = ""
    port: int = 0
    pid: int = 0
    proc: subprocess.Popen | None = field(default=None, repr=False)
    log_fh: Any = field(default=None, repr=False)
    healthy: bool = False
    started_at: float = 0.0
    log_path: str = ""
    adopted_pids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "port": self.port,
            "pid": self.pid,
            "healthy": self.healthy,
            "started_at": self.started_at,
            "log_path": self.log_path,
        }


_processes: dict[str, AppProcess] = {}  # app_name -> AppProcess
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Node.js binary resolution
# ---------------------------------------------------------------------------

def _resolve_nvm_path(binary_name: str) -> str | None:
    """Resolve a binary via nvm, returning its full path or None.

    Sources ~/.nvm/nvm.sh to find the nvm-managed node path, then resolves
    the requested binary relative to that directory.
    """
    nvm_dir = os.environ.get("NVM_DIR", os.path.expanduser("~/.nvm"))
    nvm_sh = os.path.join(nvm_dir, "nvm.sh")
    if not os.path.isfile(nvm_sh):
        return None
    try:
        result = subprocess.run(
            ["bash", "-c", f'source "{nvm_sh}" --no-use && nvm which current'],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            nvm_node = result.stdout.strip()
            target = os.path.join(os.path.dirname(nvm_node), binary_name)
            if os.path.isfile(target):
                return target
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _find_node_binary() -> str | None:
    """Find a usable node binary.

    Search order:
    1. nvm-managed node (via ~/.nvm/nvm.sh)
    2. System PATH
    """
    nvm_path = _resolve_nvm_path("node")
    if nvm_path:
        return nvm_path
    return shutil.which("node")


def _find_npm_binary() -> str | None:
    """Find npm binary, same search order as node."""
    nvm_path = _resolve_nvm_path("npm")
    if nvm_path:
        return nvm_path
    return shutil.which("npm")


def _is_asgi_entry(entry: Any) -> bool:
    """Heuristic: check if a Python entry point looks like an ASGI app."""
    try:
        content = entry.read_text(encoding="utf-8", errors="replace")
        return "FastAPI(" in content and "uvicorn" in content.lower()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start_app_backend(app_name: str) -> AppProcess | None:
    """Start an app's backend process if it declares one.

    Returns the AppProcess on success, None if no backend declared.
    """
    manifest = get_app_manifest(app_name)
    if not manifest or not manifest.backend.entryPoint:
        return None

    with _lock:
        if app_name in _processes:
            existing = _processes[app_name]
            if existing.proc and existing.proc.poll() is None:
                logger.info("App %s backend already running (pid %d)", app_name, existing.pid)
                return existing

    root = app_dir(app_name)
    entry_point = manifest.backend.entryPoint
    # Module-style entry point (e.g. "kiro_claw.apps.builtins.<name>"):
    # used by built-in apps that live inside the KiroClaw package itself.
    # Heuristics:
    #   - no path separator,
    #   - no script-file extension (.py/.js/.ts/.mjs/.cjs/.sh) — those are
    #     paths, not module dotted-names,
    #   - has a dot (i.e. is a dotted module path),
    #   - and no file with that literal name exists under the app root.
    is_module_entry = (
        "/" not in entry_point
        and not entry_point.endswith((".py", ".js", ".ts", ".mjs", ".cjs", ".sh"))
        and "." in entry_point
        and not (root / entry_point).exists()
    )
    if is_module_entry:
        entry = None  # sentinel; no file path for module-style entries
    else:
        entry = root / entry_point
        if not entry.is_file():
            logger.error("App %s backend entry point not found: %s", app_name, entry)
            return None

    # Resolve port
    port_str = manifest.backend.port
    if port_str == "auto":
        port = _find_free_port()
    else:
        try:
            port = int(port_str)
            if not (_MIN_PORT <= port <= _MAX_PORT):
                logger.error(
                    "App %s: port %d outside allowed range %d-%d",
                    app_name, port, _MIN_PORT, _MAX_PORT,
                )
                return None
        except ValueError:
            port = _find_free_port()

    # Prepare log directory (needed early for adopt path)
    log_dir = root / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "backend.log"

    # Check if the port is already in use by a healthy instance
    if port_str != "auto":
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(1)
                s.connect(("127.0.0.1", port))
            # Port occupied — probe health endpoint before giving up
            healthy = False
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}{manifest.backend.healthCheck}",
                    method="GET",
                )
                with urllib.request.urlopen(req, timeout=3) as resp:
                    healthy = resp.status < 400
            except (urllib.error.URLError, OSError):
                pass

            if healthy:
                try:
                    sel().log_api_access(
                        caller="gateway", operation="app_backend_adopt",
                        outcome="adopted", resources=f"{app_name} port={port}",
                    )
                except Exception as exc:
                    logger.debug("SEL audit failed for app %s backend adopt: %s", app_name, exc)
                # Record PIDs listening on this port at adoption time
                adopted_pids: list[int] = []
                try:
                    lsof_result = subprocess.run(
                        ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if lsof_result.returncode == 0 and lsof_result.stdout.strip():
                        for pid_str in lsof_result.stdout.strip().split("\n"):
                            try:
                                adopted_pids.append(int(pid_str.strip()))
                            except ValueError:
                                pass
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if not adopted_pids:
                    logger.warning(
                        "App %s: cannot record PIDs on port %d (lsof unavailable?) — skipping adoption",
                        app_name, port,
                    )
                    return None
                logger.info("App %s: healthy instance already on port %d — adopting (pids=%s)", app_name, port, adopted_pids)
                ap = AppProcess(
                    app_name=app_name, port=port, pid=0, proc=None,
                    healthy=True, started_at=time.time(), log_path=str(log_path),
                    adopted_pids=adopted_pids,
                )
                with _lock:
                    _processes[app_name] = ap
                    _allocated_ports[app_name] = port
                return ap
            else:
                try:
                    sel().log_api_access(
                        caller="gateway", operation="app_backend_spawn",
                        outcome="rejected_port_unhealthy",
                        resources=f"{app_name} port={port}",
                    )
                except Exception as exc:
                    logger.debug("SEL audit failed for app %s port rejection: %s", app_name, exc)
                logger.warning(
                    "App %s: port %d occupied by unhealthy process — "
                    "kill it manually then retry", app_name, port,
                )
                return None
        except OSError:
            pass  # port is free — proceed to spawn

    # Install Python dependencies into a per-app venv (isolated from KiroClaw runtime)
    req_file = root / "requirements.txt"
    if req_file.is_file():
        venv_dir = root / ".venv"
        _env = minimal_env()  # don't leak secrets to pip/venv subprocesses
        try:
            if not venv_dir.exists():
                venv_cmd, _ = wrap_argv(
                    ["python3", "-m", "venv", str(venv_dir)], mode="standard"
                )
                subprocess.run(
                    venv_cmd,
                    check=True, capture_output=True, timeout=60, env=_env,
                )
            pip_bin = str(venv_dir / "bin" / "pip")
            pip_cmd, _ = wrap_argv(
                [pip_bin, "install", "--quiet", "--disable-pip-version-check",
                 "-r", str(req_file)], mode="standard"
            )
            subprocess.run(
                pip_cmd,
                capture_output=True, timeout=60, env=_env,
            )
        except Exception as exc:
            logger.warning("Failed to install deps for app %s: %s", app_name, exc)

    # Spawn process — use manifest backend type if available, fall back to heuristic
    env = minimal_env(PORT=str(port), KIROCLAW_APP_NAME=app_name)
    entry_str = str(entry) if entry else entry_point

    # Prefer explicit backend type from manifest over content sniffing
    backend_type = manifest.backend.type if manifest.backend else ""

    # --- Node.js backend ---
    # Note: module-style entry points (entry is None) are always Python
    # builtin apps and never declare a Node.js backend, so this branch is
    # safe to evaluate before the module-style branch below.
    if entry is not None and (backend_type == "node" or (
        not backend_type and entry_str.endswith((".js", ".mjs", ".cjs"))
    )):
        node_bin = _find_node_binary()
        if not node_bin:
            logger.error(
                "App %s declares a Node.js backend but no node binary found. "
                "Searched: nvm, PATH.",
                app_name,
            )
            return None
        cmd = [node_bin, entry_str]
        cwd = str(root)
        # Pass PORT as env var — Node.js apps typically read process.env.PORT
        env["NODE_ENV"] = "production"

        # Install npm dependencies if package.json exists and node_modules is missing
        pkg_json = root / "package.json"
        node_modules = root / "node_modules"
        if pkg_json.is_file() and not node_modules.is_dir():
            npm_bin = _find_npm_binary()
            if npm_bin:
                logger.info("Installing npm deps for app %s", app_name)
                try:
                    sel().log_api_access(
                        caller="gateway", operation="app_backend_npm_install",
                        outcome="started", resources=f"{app_name}",
                    )
                except Exception as exc:
                    logger.debug("SEL audit failed for npm install %s: %s", app_name, exc)
                try:
                    sandboxed_npm, _ = wrap_argv(
                        [npm_bin, "install", "--production", "--no-audit", "--no-fund"],
                        mode="standard",
                    )
                    subprocess.run(
                        sandboxed_npm,
                        cwd=str(root), env=env, capture_output=True, timeout=120,
                    )
                except Exception as exc:
                    logger.warning("Failed to install npm deps for app %s: %s", app_name, exc)

    # --- Module-style Python builtin (e.g. kiro_claw.apps.builtins.<name>) ---
    # Module-style entries have no file path — invoke via `python -m <module>`.
    # Run under the gateway's own python interpreter (sys.executable) so the
    # module path resolves against the gateway's installed packages, with
    # cwd at the KiroClaw source root so relative imports inside the module
    # work without venv setup.
    elif entry is None:
        python_bin = sys.executable
        cmd = [python_bin, "-m", entry_point]
        cwd = str(Path(__file__).resolve().parent.parent.parent)

    # --- ASGI (Python) backend ---
    elif backend_type == "asgi" or (
        not backend_type and _is_asgi_entry(entry)
    ):
        venv_python = str(root / ".venv" / "bin" / "python3")
        python_bin = venv_python if (root / ".venv" / "bin" / "python3").is_file() else "python3"
        # Derive the module path for uvicorn (e.g. backend.app:app)
        rel = entry.relative_to(root)
        parts = list(rel.parts)
        if len(parts) > 2 and parts[0] == "src":
            cwd = str(root / "src")
            module_path = ".".join(parts[1:]).removesuffix(".py")
        else:
            cwd = str(root)
            module_path = ".".join(parts).removesuffix(".py")
        cmd = [
            python_bin, "-m", "uvicorn",
            f"{module_path}:app",
            "--host", "127.0.0.1",
            "--port", str(port),
            "--log-level", "warning",
        ]

    # --- Plain Python backend (default) ---
    else:
        venv_python = str(root / ".venv" / "bin" / "python3")
        python_bin = venv_python if (root / ".venv" / "bin" / "python3").is_file() else "python3"
        cmd = [python_bin, entry_str]
        cwd = str(root)

    # Apply OS-level sandbox to app backend process
    sandboxed_cmd, cleanup_path = wrap_argv(cmd, mode="standard")

    logger.info(
        "Spawning app %s backend: %s", app_name, " ".join(sandboxed_cmd),
    )
    try:
        sel().log_api_access(
            caller="gateway", operation="app_backend_spawn",
            outcome="started", resources=f"{app_name} port={port}",
        )
    except Exception as exc:
        logger.debug("SEL audit failed for app %s backend spawn: %s", app_name, exc)

    try:
        log_fh = open(log_path, "w")
        try:
            proc = subprocess.Popen(
                sandboxed_cmd,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        except OSError:
            log_fh.close()
            raise
    except OSError as exc:
        logger.error("Failed to start app %s backend: %s", app_name, exc)
        return None

    ap = AppProcess(
        app_name=app_name,
        port=port,
        pid=proc.pid,
        proc=proc,
        log_fh=log_fh,
        healthy=False,
        started_at=time.time(),
        log_path=str(log_path),
    )

    with _lock:
        _processes[app_name] = ap
        _allocated_ports[app_name] = port

    logger.info("Started app %s backend on port %d (pid %d)", app_name, port, proc.pid)

    # Health check in background
    threading.Thread(
        target=_health_check_loop,
        args=(app_name, port, manifest.backend.healthCheck),
        daemon=True,
    ).start()

    return ap


def _wait_for_pids(pids: list[int], timeout: float = 2.0) -> None:
    """Poll until all PIDs have exited or timeout is reached.

    Uses short sleeps (0.1s) to avoid blocking the thread for the full
    timeout duration when processes exit quickly.
    """
    deadline = time.monotonic() + timeout
    remaining = list(pids)
    while remaining and time.monotonic() < deadline:
        still_alive: list[int] = []
        for pid in remaining:
            try:
                os.kill(pid, 0)
                still_alive.append(pid)
            except (ProcessLookupError, OSError):
                pass
        remaining = still_alive
        if remaining:
            time.sleep(0.1)


def stop_app_backend(app_name: str) -> bool:
    """Stop an app's backend process."""
    with _lock:
        ap = _processes.pop(app_name, None)
        _allocated_ports.pop(app_name, None)

    if not ap:
        return False

    if ap.proc and ap.proc.poll() is None:
        try:
            sel().log_api_access(
                caller="gateway", operation="app_backend_stop",
                outcome="sigterm", resources=f"{app_name} pid={ap.proc.pid}",
            )
        except Exception as exc:
            logger.debug("SEL audit failed for app_backend_stop %s: %s", app_name, exc)
        try:
            os.killpg(os.getpgid(ap.proc.pid), signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass
        try:
            ap.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(ap.proc.pid), signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass
            try:
                sel().log_api_access(
                    caller="gateway", operation="app_backend_stop",
                    outcome="sigkill_escalation",
                    resources=f"{app_name} pid={ap.proc.pid}",
                )
            except Exception as exc:
                logger.debug("SEL audit failed for sigkill_escalation %s: %s", app_name, exc)
    elif not ap.proc and ap.port:
        # Adopted process (proc=None) — kill only PIDs we recorded at adoption
        if not ap.adopted_pids:
            logger.warning(
                "Cannot stop adopted backend for %s on port %s: no recorded PIDs — "
                "refusing to kill unknown processes",
                app_name, ap.port,
            )
            try:
                sel().log_api_access(
                    caller="gateway", operation="app_backend_stop_adopted",
                    outcome="rejected_no_pids",
                    resources=f"{app_name} port={ap.port}",
                )
            except Exception as exc:
                logger.debug("SEL audit failed for rejected_no_pids %s: %s", app_name, exc)
            # Restore tracking so a retry is possible after re-adoption
            with _lock:
                _processes.setdefault(app_name, ap)
                if ap.port:
                    _allocated_ports.setdefault(app_name, ap.port)
            return False
        try:
            target_pids: set[int] = set(ap.adopted_pids)

            # Verify adopted PIDs still belong to this port (guards against
            # PID recycling between adoption and stop).
            try:
                lsof_result = subprocess.run(
                    ["lsof", "-ti", f":{ap.port}", "-sTCP:LISTEN"],
                    capture_output=True, text=True, timeout=5,
                )
                if lsof_result.returncode == 0 and lsof_result.stdout.strip():
                    current_pids: set[int] = set()
                    for pid_str in lsof_result.stdout.strip().split("\n"):
                        try:
                            current_pids.add(int(pid_str.strip()))
                        except ValueError:
                            pass
                    # Only kill PIDs that are both adopted AND still on this port
                    target_pids = target_pids & current_pids
            except (OSError, subprocess.TimeoutExpired):
                # lsof unavailable at stop time — proceed with adopted PIDs
                # (they were validated at adoption time)
                pass

            pids: list[int] = []
            for pid in target_pids:
                if pid <= 0:
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                    pids.append(pid)
                except (ProcessLookupError, OSError):
                    pass
            try:
                sel().log_api_access(
                    caller="gateway", operation="app_backend_stop_adopted",
                    outcome="sigterm",
                    resources=f"{app_name} port={ap.port} pids={pids}",
                )
            except Exception as exc:
                logger.debug("SEL log_api_access failed for app_backend_stop_adopted: %s", exc)
            # Wait for graceful shutdown (non-blocking poll)
            _wait_for_pids(pids, timeout=2.0)
            # Escalate to SIGKILL if still alive
            escalated: list[int] = []
            for pid in pids:
                try:
                    os.kill(pid, 0)  # check if still alive
                    os.kill(pid, signal.SIGKILL)
                    escalated.append(pid)
                except (ProcessLookupError, OSError):
                    pass
            if escalated:
                try:
                    sel().log_api_access(
                        caller="gateway", operation="app_backend_stop_adopted",
                        outcome="sigkill_escalation",
                        resources=f"{app_name} port={ap.port} pids={escalated}",
                    )
                except Exception as exc:
                    logger.debug("SEL log_api_access failed for app_backend_stop_adopted sigkill: %s", exc)
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            logger.warning(
                "Failed to stop adopted backend for %s on port %s: %s",
                app_name, ap.port, exc,
            )
            # Restore tracking so a retry is possible
            with _lock:
                _processes.setdefault(app_name, ap)
                if ap.port:
                    _allocated_ports.setdefault(app_name, ap.port)
            return False

    if ap.proc:
        logger.info("Stopped app %s backend (pid %d)", app_name, ap.pid)
    else:
        logger.info("Stopped adopted app %s backend on port %s", app_name, ap.port)
    if ap.log_fh:
        try:
            ap.log_fh.close()
        except OSError:
            pass
    return True


def get_app_process(app_name: str) -> AppProcess | None:
    """Get the process info for a running app backend."""
    with _lock:
        return _processes.get(app_name)


def list_app_processes() -> list[dict[str, Any]]:
    """List all running app backend processes."""
    with _lock:
        return [ap.to_dict() for ap in _processes.values()]


def get_app_backend_port(app_name: str) -> int | None:
    """Get the port for a running app backend (used by reverse proxy)."""
    with _lock:
        ap = _processes.get(app_name)
        return ap.port if ap and ap.healthy else None


# ---------------------------------------------------------------------------
# Health checking
# ---------------------------------------------------------------------------

def _health_check_loop(app_name: str, port: int, health_path: str) -> None:
    """Poll the health endpoint until it responds or we give up."""
    url = f"http://127.0.0.1:{port}{health_path}"
    for attempt in range(_HEALTH_CHECK_RETRIES):
        time.sleep(_HEALTH_CHECK_INTERVAL)
        with _lock:
            if app_name not in _processes:
                return  # stopped while we were checking
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=_HEALTH_CHECK_TIMEOUT) as resp:
                if resp.status < 400:
                    with _lock:
                        if app_name in _processes:
                            _processes[app_name].healthy = True
                    logger.info(
                        "App %s backend healthy (port %d, attempt %d)",
                        app_name, port, attempt + 1,
                    )
                    return
        except (urllib.error.URLError, OSError):
            pass

    logger.warning(
        "App %s backend failed health check after %d attempts",
        app_name, _HEALTH_CHECK_RETRIES,
    )


# ---------------------------------------------------------------------------
# Gateway startup — start backends for all enabled apps
# ---------------------------------------------------------------------------

def start_enabled_app_backends() -> list[str]:
    """Start backends for all enabled apps that declare one.

    Called during gateway startup to restore app backends.
    Returns list of app names that were started.
    """
    from kiro_claw.apps.manager import list_apps

    started: list[str] = []
    for app_info in list_apps():
        if not app_info.get("enabled"):
            continue
        name = app_info.get("name", "")
        manifest = app_info.get("manifest", {})
        if not manifest.get("backend", {}).get("entryPoint"):
            continue
        ap = start_app_backend(name)
        if ap:
            started.append(name)
            logger.info("Auto-started backend for app %s on port %d", name, ap.port)
    return started
