"""CLI server lifecycle commands — update, stop, token, logout, status, gateway, run."""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from kiro_claw import __version__
from kiro_claw.config import KiroClawConfig
from kiro_claw.config.loader import _DEFAULT_PORT, _session_work_dir, config_dir, config_path
from kiro_claw.constants import DATA_WARNING
from kiro_claw.context import ContextBuilder
from kiro_claw.dashboard.origin import dashboard_origin, parse_dashboard_url
from kiro_claw.dashboard.token_auth import parse_duration
from kiro_claw.embeddings import OllamaManager, make_sync_embed_fn
from kiro_claw.frontend import build_frontend_sync, ensure_dev_dist_symlink
from kiro_claw.history import ConversationLog, HistoryConsolidator
from kiro_claw.hooks import HookManager, HooksConfig
from kiro_claw.learn import LessonStore
from kiro_claw.memory import MemoryStore
from kiro_claw.sel import sel
from kiro_claw.service import controller as service_controller
from kiro_claw.service import linux as svc_linux
from kiro_claw.service import macos as svc_macos
from kiro_claw.service.common import SERVICE_NAME, Platform, current_platform
from kiro_claw.session import SessionManager
from kiro_claw.skills import SkillsLoader
from kiro_claw.slack.gateway import run_gateway
from kiro_claw.taskrunner import TaskRunner
from kiro_claw.vector_memory import VectorMemoryStore


def resolve_client_port(cli_port: int | None) -> int:
    """Return the dashboard port a *client* CLI command (token/status/logout/stop)
    should talk to.

    Resolution order:

    1. Explicit ``--port`` CLI flag if the user passed one (``cli_port`` is not ``None``).
    2. ``KIROCLAW_PORT`` env var if set to a valid integer.
    3. Port parsed from ``dashboard.url`` in the config file (``~/.kiroclaw/config.json``)
       if present and parseable.
    4. ``_DEFAULT_PORT`` (8765) as the final fallback.

    This matches the server-side ``parse_dashboard_url()`` logic so that
    ``kiroclaw token`` / ``status`` / ``logout`` / ``stop`` all hit the same
    port the gateway is actually bound to when the user has configured a
    non-default ``dashboard.url`` (for example a dev instance on 6777 or an
    alternative prod port like 7778).
    """
    if cli_port is not None:
        return cli_port
    env_port = os.environ.get("KIROCLAW_PORT")
    if env_port:
        try:
            return int(env_port)
        except ValueError:
            # Fall through to config/default — main() validates this early,
            # but guard here too in case the helper is reached via another path.
            pass
    try:
        cfg = KiroClawConfig.load()
        url = cfg.dashboard.url or ""
        if url:
            _, port = parse_dashboard_url(url)
            if port:
                return port
    except Exception:
        # Config load failures must not break client commands — fall through.
        pass
    return _DEFAULT_PORT


def _token(args: argparse.Namespace) -> None:
    """Print a dashboard URL with a fresh auth token."""
    ttl = parse_duration(args.ttl)
    if ttl is None:
        print(f"❌ Invalid TTL: {args.ttl} (use e.g. 1h, 30m)")
        sys.exit(1)

    port = resolve_client_port(args.port)
    secret_path = config_dir() / ".local_secret"
    try:
        secret = secret_path.read_text().strip()
    except FileNotFoundError:
        print("❌ Gateway not running — start it with: kiroclaw gateway")
        sys.exit(1)

    url = f"http://localhost:{port}/api/token/local?ttl={args.ttl}"
    req = urllib.request.Request(url, headers={"X-Local-Secret": secret})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            token = data.get("token", "")
    except Exception as exc:
        print(f"❌ Could not reach gateway on port {port}: {exc}")
        sys.exit(1)

    if not token:
        print("❌ Gateway returned empty token")
        sys.exit(1)
    print(f"http://localhost:{port}?token={token}")
    origin = dashboard_origin(KiroClawConfig.load().dashboard.url)
    if origin and "localhost" not in origin:
        print(f"{origin}/?token={token}")


def _logout(port: int) -> None:
    """Revoke all dashboard sessions by calling the gateway's /api/logout endpoint."""
    secret_path = config_dir() / ".local_secret"
    try:
        secret = secret_path.read_text().strip()
    except FileNotFoundError:
        print("❌ Gateway not running — start it with: kiroclaw gateway")
        sys.exit(1)

    url = f"http://localhost:{port}/api/logout"
    req = urllib.request.Request(
        url,
        method="POST",
        headers={"X-Local-Secret": secret, "Content-Type": "application/json"},
        data=b"{}",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                print("✅ All dashboard sessions revoked.")
            else:
                print(f"❌ Failed to revoke sessions: {data.get('error', 'unknown error')}")
                sys.exit(1)
    except urllib.error.HTTPError as e:
        print(f"❌ Failed to revoke sessions: HTTP {e.code}")
        sys.exit(1)
    except (urllib.error.URLError, OSError):
        print("❌ Gateway not running — start it with: kiroclaw gateway")
        sys.exit(1)


def _stop(cli_port: int | None = None) -> None:
    """Stop a running KiroClaw gateway.

    Accepts the raw CLI ``--port`` value (``None`` when not passed).
    Resolution and service-bypass are both derived from this single input:

    - ``cli_port is None``: user didn't pass ``--port``, so we resolve via
      env/config/default AND try the systemd/launchd service first.
    - ``cli_port is not None``: user explicitly targeted a port, so we
      bypass the service short-circuit and SIGTERM the gateway bound to
      that port directly.
    """
    port = resolve_client_port(cli_port)
    if cli_port is None and service_controller.stop_service():
        sel().log_api_access(
            caller="cli", operation="gateway_stop", outcome="allowed",
            source="cli", resources=f"port={port} via=service",
        )
        print("✅ Stopped kiroclaw service. To remove it: kiroclaw service uninstall")
        return

    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"], text=True
        ).strip()
    except FileNotFoundError:
        sel().log_api_access(
            caller="cli", operation="gateway_stop", outcome="error",
            source="cli", resources=f"port={port} reason=lsof_not_found",
        )
        print("❌ `lsof` not found — cannot look up gateway process. "
              f"Install lsof or use `ss -tlnp | grep {port}` to find the PID manually.")
        sys.exit(1)
    except subprocess.CalledProcessError:
        out = ""

    if not out:
        sel().log_api_access(
            caller="cli", operation="gateway_stop", outcome="no_target",
            source="cli", resources=f"port={port}",
        )
        print(f"No KiroClaw gateway currently running on port {port}.")
        sys.exit(1)

    pids = list(dict.fromkeys(int(p) for p in out.splitlines() if p.strip().isdigit()))

    # Only kill processes that are actually KiroClaw gateways.
    # Note: TOCTOU race exists between this check and os.kill — the PID could be
    # recycled. Acceptable risk for an interactive CLI tool with low blast radius.
    try:
        pids = [p for p in pids if _is_kiroclaw_process(p)]
    except FileNotFoundError:
        sel().log_api_access(
            caller="cli", operation="gateway_stop", outcome="error",
            source="cli", resources=f"port={port} reason=ps_not_found",
        )
        print("❌ `ps` not found — cannot verify gateway process. "
              "Install procps or manually kill the process.")
        sys.exit(1)
    if not pids:
        sel().log_api_access(
            caller="cli", operation="gateway_stop", outcome="no_target",
            source="cli", resources=f"port={port} reason=no_kiroclaw_process",
        )
        print(f"No KiroClaw gateway currently running on port {port}.")
        sys.exit(1)

    sent: set[int] = set()
    denied: list[int] = []
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            sent.add(pid)
        except ProcessLookupError:
            pass
        except PermissionError:
            denied.append(pid)

    # Wait briefly for processes to exit so the port is freed
    if sent:
        for _ in range(10):  # up to 1s
            time.sleep(0.1)
            if all(_pid_exited(p) for p in sent):
                break

    if sent:
        sel().log_api_access(
            caller="cli", operation="gateway_stop", outcome="allowed",
            source="cli", resources=f"pids={sorted(sent)} port={port}",
        )
        print(f"✅ Sent SIGTERM to gateway (pid {', '.join(str(p) for p in sorted(sent))}).")
    if denied:
        sel().log_api_access(
            caller="cli", operation="gateway_stop", outcome="denied",
            source="cli", resources=f"pids={denied} port={port}",
        )
        print(f"❌ No permission to stop pid {', '.join(str(p) for p in denied)} — try: sudo kiroclaw stop")
        sys.exit(1)
    if not sent:
        sel().log_api_access(
            caller="cli", operation="gateway_stop", outcome="no_target",
            source="cli", resources=f"port={port} reason=process_already_exited",
        )
        print(f"No KiroClaw gateway currently running on port {port} (process already exited).")
        sys.exit(1)


def _is_kiroclaw_process(pid: int) -> bool:
    """Return True if *pid* looks like a KiroClaw gateway process."""
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "args="], text=True
        ).strip().lower()
        # Match both the installed entrypoint (``kiroclaw gateway``) and the
        # source/module invocation (``python -m kiro_claw gateway``). The
        # module form appears in ps as ``kiro_claw gateway`` (underscore +
        # space), which the entrypoint-only checks would otherwise miss, so
        # ``kiroclaw stop`` could not stop a dev/source-run gateway.
        return ("kiro_claw.gateway" in out or "kiro_claw.dashboard" in out
                or "kiro_claw gateway" in out or "-m kiro_claw" in out
                or "kiroclaw gateway" in out or "kiroclaw start" in out)
    except subprocess.CalledProcessError:
        return False


def _pid_exited(pid: int) -> bool:
    """Return True if *pid* no longer exists."""
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # still alive, just can't signal


def _spawn_detached_gateway() -> int:
    """Spawn a detached ``kiroclaw gateway`` so the calling shell returns.

    Used by :func:`_restart` when no platform service is active. The
    new process:

    - Detaches via ``start_new_session=True`` (own session + process
      group), so closing the calling terminal does not SIGHUP it.
    - Drops stdin to ``/dev/null`` and redirects stdout/stderr to
      ``~/.kiroclaw/gateway.log`` (same file the existing ``logs``
      command tails for foreground gateways), so the user has one
      place to look regardless of how the gateway was started.
    - Resolves ``kiroclaw`` via ``shutil.which`` first, falling back
      to ``sys.executable -m kiro_claw`` so editable/source-tree dev
      installs also work without a global ``kiroclaw`` symlink.
    - Closes all inherited file descriptors so it does not pin sockets
      or pipes from the parent CLI process.

    Returns the new PID.
    """
    log_path = config_dir() / "gateway.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    # Open in append mode so successive restarts accumulate history in
    # one log file. The fd is owned by the child after Popen returns.
    log_fh = open(log_path, "a", encoding="utf-8")  # noqa: SIM115

    bin_path = shutil.which("kiroclaw")
    if bin_path:
        argv: list[str] = [bin_path, "gateway"]
    else:
        # Source-tree/editable-install fallback: run the module directly.
        # This also covers the case where the wrapper script is not on PATH
        # (e.g. running from an unactivated checkout).
        argv = [sys.executable, "-m", "kiro_claw", "gateway"]

    proc = subprocess.Popen(  # noqa: S603 — argv is built from trusted sources
        argv,
        stdin=subprocess.DEVNULL,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        close_fds=True,
        cwd=str(Path.home()),
    )
    return proc.pid


def _restart(cli_port: int | None = None) -> None:
    """Restart a running KiroClaw gateway.

    Service-aware, mirroring :func:`_stop`:

    1. If a systemd/launchd service is active AND the caller did not
       explicitly request a specific port, ask the platform to restart
       it (``systemctl restart`` / ``launchctl unload + load``).
    2. Otherwise, SIGTERM the foreground gateway via the existing
       lsof+SIGTERM path used by ``kiroclaw stop``, then spawn a
       detached replacement.

    When ``cli_port is not None`` (user passed ``--port N``), branch (1) is
    bypassed: the systemd unit name is not bound to a specific port, so
    short-circuiting through it would target the wrong gateway.
    """
    port = resolve_client_port(cli_port)
    if cli_port is None and service_controller.restart_service():
        sel().log_api_access(
            caller="cli", operation="gateway_restart", outcome="allowed",
            source="cli", resources=f"port={port} via=service",
        )
        print("✅ Restarted kiroclaw service.")
        return

    # No service active — bounce the foreground gateway and detach a fresh one.
    # Reuse _stop() for the SIGTERM path so behavior stays in sync if _stop
    # ever gains new safety checks. _stop() exits the process with sys.exit(1)
    # when no gateway is running, which is wrong for restart: a user running
    # `kiroclaw restart` after the gateway crashed should still get a fresh
    # gateway. Detect that case up-front instead of letting _stop() exit.
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"], text=True
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        out = ""
    if out:
        # TOCTOU: the gateway can exit between the lsof check above and
        # _stop()'s own lookup. _stop() raises SystemExit(1) when it
        # finds nothing — for restart that's the wrong behavior. Swallow
        # SystemExit so we always proceed to spawn a fresh gateway. The
        # user asked for a restart; an exit-before-spawn here would
        # leave them with no running gateway at all.
        try:
            _stop(cli_port)
        except SystemExit:
            pass

    pid = _spawn_detached_gateway()
    sel().log_api_access(
        caller="cli", operation="gateway_restart", outcome="allowed",
        source="cli", resources=f"port={port} via=fork pid={pid}",
    )
    print(f"✅ Started detached gateway (pid {pid}). Logs: kiroclaw logs -f")


def _update() -> None:
    """Update KiroClaw via git fetch + reset --hard + rebuild."""
    print("🐾 Updating KiroClaw…\n")

    proj = os.environ.get("KIROCLAW_PROJECT_DIR", "")
    if not proj:
        print("❌ KIROCLAW_PROJECT_DIR not set — cannot locate source tree")
        print("   Run from the project directory or run `kiroclaw setup` first.")
        sys.exit(1)

    proj_path = Path(proj)
    if not (proj_path / ".git").is_dir():
        print(f"❌ No git repo at {proj}")
        sys.exit(1)

    print(f"  📂 {proj}")

    # Detect current branch
    branch_result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if branch_result.returncode != 0:
        print("❌ Could not determine current branch")
        sys.exit(1)
    branch = branch_result.stdout.strip() or "mainline"
    if branch == "HEAD":
        branch = "mainline"

    # Fetch + reset --hard: no merge conflicts, untracked files preserved
    print("  ⬇️  git fetch…")
    result = subprocess.run(
        ["git", "fetch", "origin", branch],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        print(f"  ❌ git fetch failed:\n{result.stderr.strip()}")
        sys.exit(1)

    # Check if there are new commits
    diff_result = subprocess.run(
        ["git", "diff", "HEAD", f"origin/{branch}", "--quiet"],
        cwd=proj,
        capture_output=True,
        timeout=10,
    )
    if diff_result.returncode == 0:
        print("\n✅ Already up to date!")
        return

    # Warn about local tracked-file changes before discarding
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=10,
    )
    tracked_changes = [
        line for line in status.stdout.strip().splitlines() if not line.startswith("??")
    ]
    if tracked_changes:
        print("  ⚠️  Local tracked-file changes will be discarded:")
        for line in tracked_changes[:10]:
            print(f"      {line}")
        resp = input("  Continue? [y/N] ").strip().lower()
        if resp != "y":
            print("  Aborted.")
            sys.exit(0)

    print(f"  🔄 git reset --hard origin/{branch}…")
    result = subprocess.run(
        ["git", "reset", "--hard", f"origin/{branch}"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        print(f"  ❌ git reset failed:\n{result.stderr.strip()}")
        sys.exit(1)

    # Update the optional kiro-cli backend if present.
    if shutil.which("kiro-cli"):
        print("  🔄 kiro-cli update")
        subprocess.run(["kiro-cli", "update"], capture_output=True, timeout=120)

    # Ensure Node.js >= 16 for frontend builds
    from kiro_claw.cli import _ensure_node  # circular import: cli -> cli_server -> cli

    print("  🔄 Checking Node.js…")
    _ensure_node(proj)

    # Build the dashboard frontend assets (npm), then reinstall the package.
    build_frontend_sync(proj_path)

    print("  🔨 pip install -e .")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
        cwd=proj,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  ❌ Install failed:\n{result.stderr.strip()}")
        sys.exit(1)

    print("\n✅ KiroClaw updated!")
    print(f"\n{DATA_WARNING}\n")

    # Re-install agent config so new denied commands take effect.
    # Run as subprocess since the current process has old code loaded.
    print("  🔒 Refreshing agent config…")
    r = subprocess.run(
        [sys.executable, "-m", "kiro_claw", "setup", "--agent-only"],
        cwd=proj,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode == 0:
        print("  ✅ Agent config refreshed (deniedCommands + hooks updated)")
    else:
        print("  ⚠️  Agent config refresh failed — run: kiroclaw setup --agent-only")


def _status(args: argparse.Namespace) -> None:
    """Query the running gateway for stats, or print offline message."""
    port = resolve_client_port(getattr(args, "port", None))
    url = f"http://127.0.0.1:{port}/api/status"
    try:
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print("KiroClaw gateway is running (token auth enabled).")
            print("  For detailed stats, see the Overview page in the dashboard.")
        else:
            print(f"KiroClaw gateway is running but returned HTTP {e.code}.")
        return
    except (urllib.error.URLError, OSError):
        print("KiroClaw gateway is not running.")
        print("  Start it with: kiroclaw gateway")
        return
    except Exception:
        print("KiroClaw gateway is running but returned an unexpected response.")
        return

    print(f"KiroClaw v{__version__} 🐾\n")
    print(f"  Uptime:      {data.get('uptime', '—')}")
    print(f"  Sessions:    {data.get('sessions', 0)}")
    print(f"  Messages:    {data.get('messages', 0)}")
    print(f"  Tool calls:  {data.get('tool_calls', 0)}")
    print(f"  Subagents:   {data.get('subagents', 0)}")
    print(f"  Cron jobs:   {data.get('crons', 0)}")
    print(f"  Lessons:     {data.get('lessons', 0)}")


async def _gateway(
    *,
    no_dashboard: bool = False,
    no_crons: bool = False,
    no_open: bool = False,
    port_override: str | None = None,
    json_ready: bool = False,
    approval_mode: str | None = None,
) -> None:
    """Load config and start the Slack Socket Mode gateway."""
    # Ensure Node >= 16 so frontend builds work (avoids legacy fallback).
    from kiro_claw.cli import _ensure_node, _node_ok  # circular import: cli -> cli_server -> cli

    if not _node_ok():
        _ensure_node()

    # Resolve the dashboard's React build. Skipped in slack-only mode since no
    # dashboard will be served. When the prebuilt dist/ is missing the gateway
    # falls back to the legacy dashboard.html — build the frontend to restore
    # the full dashboard.
    if not no_dashboard and ensure_dev_dist_symlink() is None:
        logging.getLogger(__name__).warning(
            "Dashboard dist/ not found — serving legacy dashboard.html. "
            "Run `npm ci && npm run build` in the website/ directory to build "
            "the full dashboard."
        )

    if not config_path().exists():
        cfg = KiroClawConfig()
        cfg.save()
        print(f"🐾 Created default config: {config_path()}")

    cfg = KiroClawConfig.load()
    await run_gateway(
        cfg,
        no_dashboard=no_dashboard,
        no_crons=no_crons,
        no_open=no_open,
        port_override=port_override,
        json_ready=json_ready,
        approval_mode=approval_mode,
    )


async def _run_task(args: argparse.Namespace) -> None:
    """Execute a spec file autonomously via TaskRunner."""

    spec_path = Path(args.spec).resolve()
    if not spec_path.exists():
        print(f"❌ Spec file not found: {spec_path}", file=sys.stderr)
        sys.exit(1)

    cfg = KiroClawConfig.load()
    factory = cfg.create_provider_factory()
    sessions = SessionManager(cfg, provider_factory=factory)  # type: ignore[arg-type]

    auto_test = not getattr(args, "no_test", False)
    fresh = getattr(args, "fresh", False)
    timeout = float(getattr(args, "timeout", 0))

    # Initialize history + lessons for learning and memory formation
    memory = MemoryStore()
    memory.init()

    # Vector memory (structured semantic store)

    vector_memory = VectorMemoryStore(
        confidence_threshold=cfg.memory.semantic_confidence_threshold,
        extra_prefixes=cfg.memory.semantic_keys or None,
        episodic_limit=cfg.memory.episodic_max_results,
        embedding_dim=cfg.memory.embedding_dim,
    )
    vector_memory.init()
    if cfg.memory.embedding_provider == "ollama":
        _ollama_mgr = OllamaManager(cfg.memory.embedding_url, model=cfg.memory.embedding_model)
        vector_memory._ollama_manager = _ollama_mgr
        # Wire factory FIRST so lazy rebind works even if ensure_running() fails now
        # but Ollama becomes available later (e.g., container started slowly).
        _embed_url = cfg.memory.embedding_url
        _embed_model = cfg.memory.embedding_model
        vector_memory.embed_fn_factory = lambda: make_sync_embed_fn(_embed_url, model=_embed_model)
        if await _ollama_mgr.ensure_running():
            vector_memory.embed_fn = make_sync_embed_fn(_embed_url, model=_embed_model)
        else:
            print(
                "⚠️  Ollama not ready at boot — embeddings will lazily reconnect when available",
                file=sys.stderr,
            )
    memory.vector_store = vector_memory

    conv_log = ConversationLog()
    conv_log.init()
    lessons = LessonStore()
    skills = SkillsLoader()
    consolidator = HistoryConsolidator(
        log=conv_log,
        memory=memory,
        sessions=sessions,
        lesson_store=lessons,
        history_idle_secs=cfg.memory.history_idle_hours * 3600,
        skills_loader=skills,
        auto_skills_enabled=cfg.skills.auto_create_from_sessions,
        auto_refine_enabled=cfg.skills.auto_refine_on_deviation,
        auto_min_tool_calls=cfg.skills.auto_min_tool_calls,
        auto_similarity_threshold=cfg.skills.auto_similarity_threshold,
    )

    async def _cli_notify(title: str, body: str, task_id: str = "") -> None:
        print(f"\n{title}")
        if body:
            print(f"  {body}")

    hooks = HookManager(HooksConfig.from_dict(cfg.hooks))
    ctx = ContextBuilder(memory=memory, skills=skills, hooks=hooks, lessons=lessons, bot_name=cfg.agent.bot_name)

    runner = TaskRunner(
        sessions=sessions,
        context_builder=ctx,
        auto_test=auto_test,
        on_notify=_cli_notify,
        work_dir=_session_work_dir("taskrunner:main"),
        conversation_log=conv_log,
        consolidator=consolidator,
        lesson_store=lessons,
        fresh=fresh,
        global_timeout=timeout,
    )

    # Pre-warm session pool (background session for lesson extraction)
    await sessions.start_pool()

    if fresh:
        print(f"🐾 Running spec (fresh): {spec_path}")
    else:
        print(f"🐾 Running spec: {spec_path}")
    task_name = getattr(args, "name", "")
    result = await runner.run(spec_path, name=task_name)

    label = result.name or result.task_id
    if result.status == "completed":
        print(f"\n✅ Task completed — {label} ({len(result.tasks)} steps)")
    elif result.status == "failed":
        print(f"\n❌ Task failed ({label}): {result.error}", file=sys.stderr)
        sys.exit(1)
    elif result.status == "cancelled":
        print("\n⚠️  Task cancelled")
        sys.exit(1)

    await sessions.close_all()


def _service_cmd(args: argparse.Namespace) -> int:
    """Dispatch ``kiroclaw service {install,uninstall,status}``.

    Wraps :mod:`kiro_claw.service.controller` so that platform detection
    and the underlying systemctl/launchctl calls live there. The CLI
    layer only handles argument parsing, audit logging, and exit codes.
    """
    action = getattr(args, "service_action", None)
    if action == "install":
        rc = service_controller.install_service()
        sel().log_api_access(
            caller="cli", operation="service_install",
            outcome="allowed" if rc == 0 else "error",
            source="cli", resources=f"rc={rc}",
        )
        return rc
    if action == "uninstall":
        rc = service_controller.uninstall_service()
        sel().log_api_access(
            caller="cli", operation="service_uninstall",
            outcome="allowed" if rc == 0 else "error",
            source="cli", resources=f"rc={rc}",
        )
        return rc
    if action == "status":
        rc = service_controller.service_status()
        sel().log_api_access(
            caller="cli", operation="service_status",
            outcome="allowed" if rc == 0 else "error",
            source="cli", resources=f"rc={rc}",
        )
        return rc
    print("Usage: kiroclaw service {install|uninstall|status}", file=sys.stderr)
    return 2


def _logs_cmd(args: argparse.Namespace) -> None:
    """Tail gateway logs from the most appropriate source.

    Order of preference:
      1. systemd journal (if the system service is installed on Linux)
      2. launchd stdout file (macOS)
      3. ``~/.kiroclaw/gateway.log`` (foreground gateway)
    """
    follow = bool(getattr(args, "follow", False))
    lines = int(getattr(args, "lines", 100) or 100)
    plat = current_platform()
    unit = f"{SERVICE_NAME}.service"

    # Audit before any os.execvp branch — the exec replaces this process
    # so a post-exec audit call would never run.
    sel().log_api_access(
        caller="cli",
        operation="logs",
        outcome="allowed",
        source="cli",
        resources=f"follow={follow} lines={lines} platform={plat.value}",
    )

    if plat == Platform.SYSTEMD and svc_linux.UNIT_PATH.exists():
        # Try journalctl unprivileged first — it works if the user is in
        # the `systemd-journal` or `adm` group. Only fall back to sudo
        # journalctl if the unprivileged probe returns no rows. Without
        # this fall-through, `kiroclaw logs` would hang on hosts without
        # passwordless sudo, which is a surprising failure mode for a
        # read-only log-viewer.
        base = ["journalctl", "--no-pager", "-u", unit, "-n", str(lines)]
        probe = subprocess.run(
            ["journalctl", "-u", unit, "-n", "1", "--no-pager"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            if follow:
                base.append("-f")
            os.execvp("journalctl", base)
        # Refuse to invoke sudo without a TTY: in non-interactive
        # contexts (cron, piped scripts, systemd ExecStartPre) the sudo
        # password prompt would block forever with no way to cancel.
        if not sys.stdin.isatty():
            print(
                "🐾 Insufficient permissions to read the journal without sudo, "
                "and stdin is not a TTY so sudo can't prompt.\n"
                "   Add your user to the `systemd-journal` or `adm` group, or run:\n"
                f"   sudo journalctl -u {unit} -f",
                file=sys.stderr,
            )
            sys.exit(1)
        # Fall back to sudo journalctl. `--no-pager` prevents the pager
        # (`less`) from taking over after exec, which behaves badly in
        # piped/non-interactive contexts.
        sudo_cmd = ["sudo", *base]
        if follow:
            sudo_cmd.append("-f")
        os.execvp("sudo", sudo_cmd)

    if plat == Platform.LAUNCHD and svc_macos.STDOUT_LOG.exists():
        cmd = ["tail", "-n", str(lines)]
        if follow:
            cmd.append("-f")
        cmd.append(str(svc_macos.STDOUT_LOG))
        os.execvp("tail", cmd)

    fallback = config_dir() / "gateway.log"
    if not fallback.exists():
        print(
            "🐾 No gateway logs found. Either install the service "
            "(`kiroclaw service install`) or start the gateway "
            "(`kiroclaw gateway`).",
            file=sys.stderr,
        )
        sys.exit(1)
    cmd = ["tail", "-n", str(lines)]
    if follow:
        cmd.append("-f")
    cmd.append(str(fallback))
    os.execvp("tail", cmd)
