"""KiroClaw CLI — personal AI agent.

Commands:
    kiroclaw chat -m "message"    Send a single message
    kiroclaw chat                 Interactive chat mode
    kiroclaw gateway              Start the KiroClaw server (dashboard + Slack)
    kiroclaw gateway --seed NAME  Populate $KIROCLAW_HOME from fixture NAME, then start the gateway
    kiroclaw status               Show runtime stats
    kiroclaw run TASK.md          Run an autonomous task from a spec file
    kiroclaw update               Update KiroClaw via git fetch + rebuild
    kiroclaw cron list|add|remove Manage scheduled jobs
    kiroclaw spawn run "task"     Spawn a background subagent
    kiroclaw spawn list           List subagents
    kiroclaw learn add|list|remove Save and manage learned corrections
    kiroclaw setup                Interactive credential setup
    kiroclaw doctor               Verify setup
"""

from __future__ import annotations

# Ensure SSL certs are found before any library caches its SSL context.
# The ``kiroclaw`` entry-point (console_scripts) bypasses ``__main__.py``,
# so we must run this here as well.
from kiro_claw._ssl_compat import _ensure_ssl_certs

_ensure_ssl_certs()

import argparse
import asyncio
import importlib
import logging
import os
import shutil
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from kiro_claw import __version__
from kiro_claw.apps.builtins import BUILTIN_NAMES as _BUILTIN_NAMES
from kiro_claw.browser.cli import run_browse
from kiro_claw.config import KiroClawConfig, config_dir
from kiro_claw.config.loader import (
    DASHBOARD_PORT,
)
from kiro_claw.history import ConversationLog, HistoryConsolidator
from kiro_claw.memory import MemoryStore
from kiro_claw.platform import PlatformCompositionError, boot_platform
from kiro_claw.seed import seed_cmd
from kiro_claw.sel import sel
from kiro_claw.session import SessionManager
from kiro_claw.skills import SkillsLoader

logger = logging.getLogger(__name__)

BANNER = r"""
   __  __         _    ___ _
  |  \/  |___ ___| |_ / __| |__ ___ __ __
  | |\/| / -_|_-<| ' \ (__| / _` \ V  V /
  |_|  |_\___/__/|_||_\___|_\__,_|\_/\_/

  🐾 Your personal AI agent
"""

# Markers that uniquely identify the KiroClaw repo root for project-dir
# auto-detection. The project-level ``agents/`` dir was removed when agent
# config was consolidated into ``src/kiro_claw/config/`` (commit bbbc1f6e), so
# ``skills/`` + ``src/kiro_claw/`` is now the stable signature: ``skills/`` is
# editable-at-root and ``src/kiro_claw/`` pins this to the KiroClaw package repo
# (not just any directory that happens to contain a ``skills/`` folder).
_PROJECT_MARKERS = ("skills", "src/kiro_claw")


def _project_dir_file() -> Path:
    """Return the path to the saved project_dir file, respecting KIROCLAW_HOME."""
    return config_dir() / "project_dir"


_MIN_NODE_VERSION = 16


def _ensure_node(proj_dir: str = "") -> bool:
    """Run ensure-node.sh to guarantee Node >= 16. Returns True if node is OK."""
    script = None
    env_dir = os.environ.get("KIROCLAW_PROJECT_DIR")
    for candidate in [
        Path(proj_dir) / "ensure-node.sh" if proj_dir else None,
        Path(env_dir) / "ensure-node.sh" if env_dir else None,
        Path(__file__).resolve().parent.parent.parent / "ensure-node.sh",
    ]:
        if candidate and candidate.is_file():
            script = candidate
            break
    if not script:
        return _node_ok()
    try:
        result = subprocess.run(
            ["bash", str(script)],
            timeout=120,
            capture_output=False,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return _node_ok()


def _node_ok() -> bool:
    """Check if node >= MIN_NODE_VERSION is available."""
    node = shutil.which("node")
    if not node:
        return False
    try:
        node_ver = subprocess.run(
            ["node", "-v"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        major = int(node_ver.stdout.strip().lstrip("v").split(".")[0])
        return major >= _MIN_NODE_VERSION
    except Exception:
        return False


def _detect_project_dir() -> str | None:
    """Find the project root containing agents/ and skills/.

    Search order:
    1. Walk up from CWD
    2. Read saved path from config_dir()/project_dir (respects KIROCLAW_HOME)
    """
    cur = Path.cwd().resolve()
    for d in (cur, *cur.parents):
        if all((d / m).is_dir() for m in _PROJECT_MARKERS):
            return str(d)
    pdf = _project_dir_file()
    if pdf.is_file():
        saved = pdf.read_text(encoding="utf-8").strip()
        p = Path(saved)
        if p.is_dir() and all((p / m).is_dir() for m in _PROJECT_MARKERS):
            return saved
    return None


def _resolve_gateway_args(args: argparse.Namespace) -> dict:
    """Resolve the kwargs for `_gateway()` from parsed CLI args.

    Expands the `--test-mode` bundle (with explicit-flag-wins override
    semantics) and enforces the `--approval yolo` safety rail. On rail
    violation, prints a message to stderr and calls `sys.exit(2)`.
    Returned dict is safe to splat directly into `_gateway()`.
    """
    port = getattr(args, "port", None)
    json_ready = getattr(args, "json_ready", False)
    approval = getattr(args, "approval", None)
    no_open = getattr(args, "no_open", False)
    if getattr(args, "test_mode", False):
        # Bundle defaults; explicit flags above take precedence (they are
        # already populated in the locals when the user passed them).
        if port is None:
            port = "auto"
        if approval is None:
            approval = "reads"
        json_ready = True
        no_open = True

    # Validate --port at parse time so a typo (e.g. `--port AUTO`, `--port abc`,
    # `--port 99999`) fails fast with a clear message instead of crashing
    # mid-startup at `int(self._port_override)` after services are partially
    # initialized.
    if port is not None:
        if str(port).lower() == "auto":
            port = "auto"  # canonicalize for downstream comparisons
        else:
            try:
                port_int = int(port)
            except ValueError:
                print(
                    f"🐾 --port must be an integer or 'auto', got {port!r}.",
                    file=sys.stderr,
                )
                sys.exit(2)
            if not 1 <= port_int <= 65535:
                print(
                    f"🐾 --port {port_int} out of range (1..65535).",
                    file=sys.stderr,
                )
                sys.exit(2)
            port = str(port_int)

    if approval == "yolo":
        home_env = os.environ.get("KIROCLAW_HOME", "")
        if not home_env:
            print(
                "🐾 --approval yolo refused: KIROCLAW_HOME must be explicitly set "
                "to an isolated path (not the default ~/.kiroclaw).",
                file=sys.stderr,
            )
            sys.exit(2)
        try:
            home_resolved = Path(home_env).expanduser().resolve()
            main_home = (Path.home() / ".kiroclaw").resolve()
        except OSError as exc:
            print(
                f"🐾 --approval yolo refused: failed to resolve KIROCLAW_HOME: {exc}",
                file=sys.stderr,
            )
            sys.exit(2)
        if home_resolved == main_home:
            print(
                "🐾 --approval yolo refused: KIROCLAW_HOME resolves to the main "
                f"gateway home ({main_home}). Set KIROCLAW_HOME to an isolated "
                "path before re-running.",
                file=sys.stderr,
            )
            sys.exit(2)

    return {
        "no_dashboard": getattr(args, "slack_only", False),
        "no_crons": getattr(args, "no_crons", False),
        "no_open": no_open,
        "port_override": port,
        "json_ready": json_ready,
        "approval_mode": approval,
    }


def _consolidate_cmd(args) -> None:
    """Force history consolidation (and auto-skill extraction) for sessions."""

    cfg = KiroClawConfig.load()
    conv_log = ConversationLog()
    conv_log.init()

    session_key = args.session_key
    consolidate_all = getattr(args, "consolidate_all", False)

    if not session_key and not consolidate_all:
        # List path — lightweight, no heavy machinery needed
        found = []
        for f in conv_log._dir.glob("*.jsonl"):
            key = f.stem
            count = conv_log.unconsolidated_count(key)
            if count > 0:
                found.append((key, count))
        if not found:
            print("No sessions with unconsolidated messages.")
            return
        print(f"Sessions with unconsolidated messages ({len(found)}):\n")
        for key, count in sorted(found, key=lambda x: -x[1]):
            print(f"  {key}  ({count} messages)")
        print("\nRun with a session key or --all to consolidate.")
        return

    # Heavy machinery only for actual consolidation
    mem = MemoryStore()
    mem.init()
    skills = SkillsLoader()
    sessions = SessionManager(cfg, provider_factory=cfg.create_provider_factory())

    # vector_store omitted: skill dedup uses SkillsLoader.find_similar() (Jaccard),
    # not vector_store. vector_store is for episodic memory embeddings only.
    consolidator = HistoryConsolidator(
        log=conv_log,
        memory=mem,
        sessions=sessions,
        skills_loader=skills,
        auto_skills_enabled=cfg.skills.auto_create_from_sessions,
        auto_refine_enabled=cfg.skills.auto_refine_on_deviation,
        auto_min_tool_calls=cfg.skills.auto_min_tool_calls,
        auto_similarity_threshold=cfg.skills.auto_similarity_threshold,
    )

    async def _run(keys: list[str]) -> None:
        for key in keys:
            try:
                sel().log_api_access(
                    caller="cli",
                    operation="consolidate",
                    outcome="allowed",
                    source="cli",
                    resources=key,
                )
                count = conv_log.unconsolidated_count(key)
                if count < 1:
                    print(f"  {key}: no unconsolidated messages, skipping")
                    continue
                print(f"  {key}: consolidating {count} messages...")
                await consolidator.consolidate_now(key)
                print(f"  {key}: done ✓")
            except Exception:
                logger.debug("consolidate (or SEL) failed for %s", key, exc_info=True)

    if consolidate_all:
        keys = [
            f.stem
            for f in conv_log._dir.glob("*.jsonl")
            if conv_log.unconsolidated_count(f.stem) > 0
        ]
        if not keys:
            print("No sessions with unconsolidated messages.")
            return
        print(f"Consolidating {len(keys)} session(s)...")
        asyncio.run(_run(keys))
    else:
        print(f"Consolidating session: {session_key}")
        asyncio.run(_run([session_key]))

    print("\nDone. Check ~/.kiroclaw/skills/auto/ for new skills.")


def main() -> None:
    """Entry point — parse args and dispatch to the appropriate subcommand."""
    # Validate KIROCLAW_PORT early — fail fast before anything else loads.
    _raw_port = os.environ.get("KIROCLAW_PORT")
    if _raw_port is not None:
        try:
            int(_raw_port)
        except ValueError:
            print(
                f"❌ KIROCLAW_PORT={_raw_port!r} is not a valid integer.\n"
                f"   Unset it or provide a numeric port (e.g. KIROCLAW_PORT=6777).",
                file=sys.stderr,
            )
            sys.exit(1)

    if not os.environ.get("KIROCLAW_PROJECT_DIR"):
        detected = _detect_project_dir()
        if detected:
            os.environ["KIROCLAW_PROJECT_DIR"] = detected

    parser = argparse.ArgumentParser(
        prog="kiroclaw",
        description="KiroClaw — personal AI agent",
    )
    parser.add_argument("--version", action="version", version=f"kiroclaw {__version__}")
    parser.add_argument(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help="Increase log verbosity (-v INFO, -vv DEBUG)",
    )

    sub = parser.add_subparsers(dest="command")

    # Helper for commands with examples
    _fmt = argparse.RawDescriptionHelpFormatter

    # chat
    chat_parser = sub.add_parser(
        "chat",
        help="Chat with the agent",
        epilog="""
Examples:
  kiroclaw chat                      # Interactive mode
  kiroclaw chat -m 'check my CRs'    # Single message
  kiroclaw chat --model claude-opus  # Use specific model
""",
        formatter_class=_fmt,
    )
    chat_parser.add_argument("-m", "--message", help="Single message (non-interactive)")
    chat_parser.add_argument("--model", help="Model to use (default: from config)")
    chat_parser.add_argument("--agent", help="Agent to use (default: from config)")
    chat_parser.add_argument("--tui", action="store_true", help="Launch TUI instead of REPL")

    # tui
    tui_parser = sub.add_parser("tui", help="Launch Terminal UI")
    tui_parser.add_argument("--yolo", action="store_true", help="Auto-approve all tools")
    tui_parser.add_argument("--port", type=int, help="Gateway port (default: from config)")
    tui_parser.add_argument("--session", help="Resume a specific session")
    tui_parser.add_argument(
        "--workspace", help="Workspace name (auto-detected from CWD if omitted)"
    )
    tui_parser.add_argument("--agent", help="Start with a specific agent")
    tui_parser.add_argument("--home", help="KIROCLAW_HOME override (e.g. ~/.kiroclaw-dev)")

    # doctor
    sub.add_parser("doctor", help="Verify KiroClaw setup")

    # gateway
    gw_parser = sub.add_parser("gateway", help="Start the KiroClaw server (dashboard + Slack)")
    gw_parser.add_argument(
        "--slack-only",
        action="store_true",
        help="Slack-only mode — skip dashboard web server and SSH tunnel instructions",
    )
    gw_parser.add_argument(
        "--no-crons",
        action="store_true",
        help="Skip cron scheduler — use when another instance handles cron execution",
    )
    gw_parser.add_argument(
        "--seed",
        metavar="FIXTURE",
        help=(
            "Seed $KIROCLAW_HOME from the named fixture BEFORE starting the "
            "gateway (dev tool). Fixture must exist under "
            "src/kiro_claw/tests_fixtures/. The gateway then runs normally "
            "against the populated $KIROCLAW_HOME. Refuses when "
            "$KIROCLAW_HOME is the main gateway home (~/.kiroclaw) or "
            "when the target is non-empty (use --seed-replace to wipe + re-seed)."
        ),
    )
    gw_parser.add_argument(
        "--seed-replace",
        action="store_true",
        help=(
            "When used with --seed, wipe $KIROCLAW_HOME (rmtree) before "
            "copying the fixture. Ignored without --seed. Does NOT "
            "override the main-gateway-home rail — ~/.kiroclaw is refused "
            "regardless."
        ),
    )
    gw_parser.add_argument(
        "--no-open",
        action="store_true",
        help="Do not auto-open the dashboard URL in the default browser on startup",
    )
    gw_parser.add_argument(
        "--port",
        metavar="PORT",
        help=(
            "Override the dashboard port. Pass an integer (e.g. --port 9999) "
            "for a fixed port, or --port auto to bind to an ephemeral port "
            "(OS-assigned). When omitted, falls back to the value in config "
            "(dashboard.url)."
        ),
    )
    gw_parser.add_argument(
        "--json-ready",
        action="store_true",
        help=(
            "Print a single line `KIROCLAW_READY:{...}` to stdout once the "
            "dashboard is bound. Payload includes port, token, pid, and "
            "KIROCLAW_HOME. Used by test harnesses to discover the bound "
            "ephemeral port and authenticate without polling. NOTE: the "
            "token grants gateway access for up to 20 hours — treat the "
            "READY line as sensitive and do not commit captured stdout to "
            "shared logs."
        ),
    )
    gw_parser.add_argument(
        "--approval",
        choices=["reads", "yolo", "interactive"],
        help=(
            "Default approval mode for tool invocations. 'reads' auto-approves "
            "read-only tools (read/list/get/search/* prefixes); 'yolo' "
            "auto-approves all tools (refused unless KIROCLAW_HOME is "
            "explicitly set to a non-default location); 'interactive' uses "
            "the standard Slack/dashboard prompt flow. When omitted, current "
            "interactive behavior is preserved."
        ),
    )
    gw_parser.add_argument(
        "--test-mode",
        action="store_true",
        help=(
            "Convenience alias for --port auto --no-open --json-ready "
            "--approval reads. An explicit --port or --approval value "
            "overrides the bundle's default (e.g. --test-mode --approval "
            "yolo uses yolo). The boolean flags --no-open and --json-ready "
            "are forced on by --test-mode and cannot be opted out of."
        ),
    )

    # setup
    setup_parser = sub.add_parser("setup", help="Install agent config and configure credentials")
    setup_parser.add_argument(
        "--agent-only",
        action="store_true",
        help="Only install the agent config, skip credential prompts",
    )
    setup_parser.add_argument(
        "--electron-only",
        action="store_true",
        help="Only install the KiroClaw desktop app (macOS), skip other setup",
    )
    setup_parser.add_argument(
        "--clean",
        action="store_true",
        help="Fresh install — don't merge MCP servers/tools from existing config",
    )

    # manifest
    manifest_parser = sub.add_parser("manifest", help="Generate Slack manifest with your alias")
    manifest_parser.add_argument("--alias", help="Override alias (default: auto-detect)")
    manifest_parser.add_argument("-o", "--output", help="Output file (default: stdout)")
    manifest_parser.add_argument(
        "--url",
        action="store_true",
        help="Print a one-click Slack app creation URL",
    )

    # cron
    cron_parser = sub.add_parser(
        "cron",
        help="Manage scheduled jobs",
        epilog="""
Examples:
  kiroclaw cron list
  kiroclaw cron add 'daily-status' 'show status' --every 86400
  kiroclaw cron add 'weekday-9am' 'check tickets' --cron '0 9 * * MON-FRI' --approval-mode auto
  kiroclaw cron add 'c360-check' 'check pipeline' --every 600 --agent customer360-code-agent
  kiroclaw cron update <job-id> --approval-mode auto
  kiroclaw cron update <job-id> --agent oncall-agent
  kiroclaw cron remove <job-id>
""",
        formatter_class=_fmt,
    )
    cron_sub = cron_parser.add_subparsers(dest="cron_action")
    cron_sub.add_parser("list", help="List cron jobs")
    cron_add = cron_sub.add_parser("add", help="Add a cron job")
    cron_add.add_argument("name", help="Job name")
    cron_add.add_argument("message", help="Message to send to agent")
    cron_add.add_argument("--every", type=int, help="Interval in seconds")
    cron_add.add_argument(
        "--cron", dest="cron_expr", help='Cron expression (e.g. "0 9 * * MON-FRI")'
    )
    cron_add.add_argument("--channel", help="Slack channel ID to post results to")
    cron_add.add_argument(
        "--agent",
        dest="agent",
        default="",
        help="Agent name for this job (e.g. 'customer360-code-agent'). "
        "Empty or omitted uses the default kiroclaw agent.",
    )
    cron_add.add_argument(
        "--silent",
        action="store_true",
        help="Suppress auto-delivery; agent controls notifications",
    )
    cron_add.add_argument(
        "--approval-mode",
        dest="approval_mode",
        choices=["auto"],
        default="",
        help='Tool approval mode ("auto" to auto-approve all tools)',
    )
    cron_update = cron_sub.add_parser("update", help="Update a cron job")
    cron_update.add_argument("job_id", help="Job ID to update")
    cron_update.add_argument("--name", help="New job name")
    cron_update.add_argument("--message", help="New message")
    cron_update.add_argument("--every", type=int, dest="every_secs", help="New interval in seconds")
    cron_update.add_argument("--cron", dest="cron_expr", help="New cron expression")
    cron_update.add_argument("--channel", help="New channel ID")
    cron_update.add_argument(
        "--agent",
        dest="agent",
        default=None,
        help="New agent name (empty string resets to default kiroclaw agent)",
    )
    cron_update.add_argument(
        "--approval-mode",
        dest="approval_mode",
        choices=["auto", "default"],
        default=None,
        help='Tool approval mode ("auto" to auto-approve, "default" to reset)',
    )
    cron_rm = cron_sub.add_parser("remove", help="Remove a cron job")
    cron_rm.add_argument("job_id", help="Job ID to remove")
    cron_pause = cron_sub.add_parser("pause", help="Pause a cron job")
    cron_pause.add_argument("job_id", help="Job ID to pause")
    cron_resume = cron_sub.add_parser("resume", help="Resume a cron job")
    cron_resume.add_argument("job_id", help="Job ID to resume")
    cron_trigger = cron_sub.add_parser("trigger", help="Trigger a cron job immediately")
    cron_trigger.add_argument("job_id", help="Job ID to trigger")

    cron_preview = cron_sub.add_parser(
        "preview",
        help="Run a script cron locally with real MCP tools; notifications are captured and printed instead of delivered",
    )
    cron_preview.add_argument(
        "script", help="Script path in module:function format (e.g. ~/.kiroclaw/crons/my.py:run)"
    )
    cron_preview.add_argument("--message", "-m", default="", help="ctx.message value")
    cron_preview.add_argument(
        "--env", "-e", action="append", metavar="K=V", help="Extra env vars (repeatable)"
    )

    # spawn
    spawn_parser = sub.add_parser(
        "spawn",
        help="Manage background subagents",
        epilog="""
Examples:
  kiroclaw spawn run 'check my open CRs'        # Wait for result
  kiroclaw spawn run --async 'analyze logs'     # Fire-and-forget
  kiroclaw spawn list                           # Show active subagents
""",
        formatter_class=_fmt,
    )
    spawn_sub = spawn_parser.add_subparsers(dest="spawn_action")
    spawn_run = spawn_sub.add_parser("run", help="Spawn a subagent")
    spawn_run.add_argument("task", help="Task for the subagent")
    spawn_run.add_argument(
        "--async",
        dest="fire_and_forget",
        action="store_true",
        help="Fire-and-forget (don't wait for result)",
    )
    spawn_sub.add_parser("list", help="List subagents")
    spawn_parser.add_argument("--port", type=int, default=DASHBOARD_PORT, help="Dashboard port")

    # run (autonomous task runner)
    run_parser = sub.add_parser(
        "run",
        help="Run an autonomous task from a spec file",
        epilog="""
Examples:
  kiroclaw run TASK.md                  # Run task with auto-resume
  kiroclaw run TASK.md --fresh          # Start from scratch
  kiroclaw run TASK.md --no-test        # Skip test verification
  kiroclaw run TASK.md --timeout 3600   # 1 hour timeout
""",
        formatter_class=_fmt,
    )
    run_parser.add_argument("spec", help="Path to the spec/task file (e.g. TASK.md)")
    run_parser.add_argument(
        "--name",
        default="",
        help="Human-readable task name (auto-derived from spec if omitted)",
    )
    run_parser.add_argument(
        "--no-test",
        action="store_true",
        help="Skip running build/test verification after each step",
    )
    run_parser.add_argument(
        "--fresh",
        action="store_true",
        help="Ignore checkpoint, start task from scratch",
    )
    run_parser.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="Global timeout in seconds (0 = no limit)",
    )
    run_parser.add_argument(
        "--port", type=int, default=DASHBOARD_PORT, help="Dashboard port for status"
    )

    # update
    # snapshot / restore
    snap_parser = sub.add_parser("snapshot", help="Create a portable backup of KiroClaw state")
    snap_parser.add_argument("output_dir", nargs="?", default=None)
    snap_parser.add_argument("--keep", type=int, default=7, help="Keep N most recent snapshots")
    snap_parser.add_argument(
        "--list", action="store_true", dest="list_snapshots", help="List existing snapshots"
    )

    rest_parser = sub.add_parser("restore", help="Restore KiroClaw state from a snapshot")
    rest_parser.add_argument("snapshot", nargs="?", help="Path to snapshot .tar.gz")
    rest_parser.add_argument("--mode", choices=("replace", "merge"))
    rest_parser.add_argument("--dry-run", action="store_true")
    rest_parser.add_argument("--components", help="Comma-separated components to restore")
    rest_parser.add_argument("--list-components", action="store_true")
    rest_parser.add_argument(
        "--force", action="store_true", help="Restore even if gateway is running"
    )

    # security
    sec_parser = sub.add_parser("security", help="Security audit and deny list")

    # eval (benchmark harness)
    eval_parser = sub.add_parser(
        "eval",
        help="Run multi-session evaluation scenarios",
        epilog="""
Examples:
  kiroclaw eval                         # smoke test (~30s)
  kiroclaw eval memory_recall_basic     # specific scenario
  kiroclaw eval --all                   # all scenarios (slow)
""",
        formatter_class=_fmt,
    )
    eval_parser.add_argument(
        "scenarios",
        nargs="*",
        default=[],
        help="Scenario names to run (without extension). Default: smoke_test",
    )
    eval_parser.add_argument(
        "--all", action="store_true", dest="all_scenarios", help="Run all scenarios"
    )
    eval_parser.add_argument("--judge", action="store_true", help="Enable LLM judge scoring")

    sec_sub = sec_parser.add_subparsers(dest="sec_action")
    sec_sub.add_parser("audit", help="Scan conversation history for suspicious tool usage")
    sec_sub.add_parser("deny-list", help="Show active deny patterns")
    sel_parser = sec_sub.add_parser("events", help="Show recent security event log entries")
    sel_parser.add_argument("-n", "--limit", type=int, default=20, help="Number of entries")
    sec_sub.add_parser("verify", help="Verify security event log HMAC integrity")

    # policy — governance model inspection (read-only; MCP-safe)
    policy_parser = sub.add_parser(
        "policy", help="Inspect the governance security policy + profiles"
    )
    policy_sub = policy_parser.add_subparsers(dest="policy_action")
    policy_sub.add_parser("show", help="Show the effective enterprise security policy")
    policy_sub.add_parser("validate", help="Validate the policy + all profiles (load-check)")
    explain_parser = policy_sub.add_parser(
        "explain", help="Explain a tool/scope decision for a surface"
    )
    explain_parser.add_argument("scope", help="Governed scope, e.g. 'commands' or 'mcp'")
    explain_parser.add_argument("item", help="The item to evaluate, e.g. 'git push origin'")
    explain_parser.add_argument(
        "--session-key", default="cli_chat", help="Surface session key (default: cli_chat)"
    )
    explain_parser.add_argument("--agent", default="", help="Agent name (optional)")
    explain_parser.add_argument("--app", default="", help="App slug (optional)")
    profile_show = policy_sub.add_parser("profile", help="Show a profile by name")
    profile_show.add_argument("name", help="Profile file stem (without .json)")

    sub.add_parser("update", help="Update KiroClaw to the latest version")

    # stop
    stop_parser = sub.add_parser("stop", help="Stop a running KiroClaw gateway")
    stop_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Dashboard port (default: resolved from KIROCLAW_PORT env or "
            "dashboard.url config). When passed explicitly, bypasses the "
            "systemd/launchd service short-circuit and SIGTERMs the gateway "
            "bound to that port — use this for parallel dev gateways on a "
            "non-default port."
        ),
    )

    # restart — service-aware: restarts the systemd/launchd service if active,
    # otherwise SIGTERMs the foreground gateway and respawns it detached so the
    # shell returns immediately. Mirrors `stop`.
    restart_parser = sub.add_parser(
        "restart", help="Restart a running KiroClaw gateway (service-aware)"
    )
    restart_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help=(
            "Dashboard port (default: resolved from KIROCLAW_PORT env or "
            "dashboard.url config). When passed explicitly, bypasses the "
            "systemd/launchd service short-circuit and restarts the gateway "
            "bound to that port — use this for parallel dev gateways on a "
            "non-default port."
        ),
    )

    # service — install/uninstall/status as a system-level systemd unit (Linux,
    # /etc/systemd/system/, requires sudo) or launchd LaunchAgent (macOS,
    # ~/Library/LaunchAgents/, no sudo) so the gateway survives SSH disconnect,
    # auto-restarts on crash, and auto-starts on boot.
    svc_parser = sub.add_parser(
        "service",
        help="Manage the KiroClaw gateway as a system service (requires sudo on Linux)",
    )
    svc_sub = svc_parser.add_subparsers(dest="service_action")
    svc_sub.add_parser("install", help="Install and start the gateway service (sudo on Linux)")
    svc_sub.add_parser("uninstall", help="Stop and remove the gateway service (sudo on Linux)")
    svc_sub.add_parser("status", help="Show service status (systemctl/launchctl)")

    # logs — tail the gateway log. Reads from the systemd journal when running
    # as a service on Linux, the launchd stdout file on macOS, or the
    # foreground gateway log file otherwise.
    logs_parser = sub.add_parser("logs", help="Show gateway logs")
    logs_parser.add_argument(
        "-f", "--follow", action="store_true", help="Follow log output (live tail)"
    )
    logs_parser.add_argument(
        "-n", "--lines", type=int, default=100, help="Number of lines to show (default: 100)"
    )

    # token
    token_parser = sub.add_parser("token", help="Print a dashboard access URL with auth token")

    # logout
    logout_parser = sub.add_parser("logout", help="Revoke all active dashboard sessions")
    logout_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: resolved from KIROCLAW_PORT env or dashboard.url config)",
    )
    token_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: resolved from KIROCLAW_PORT env or dashboard.url config)",
    )
    token_parser.add_argument("--ttl", default="20h", help="Token TTL, e.g. 1h, 30m (default: 20h)")

    # status
    status_parser = sub.add_parser("status", help="Show runtime stats")
    status_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Dashboard port (default: resolved from KIROCLAW_PORT env or dashboard.url config)",
    )

    # mcp-cron (MCP server — spawned by the agent backend, not user-facing)
    sub.add_parser("mcp-cron", help=argparse.SUPPRESS)

    # consolidate
    consolidate_parser = sub.add_parser(
        "consolidate",
        help="Force history consolidation (triggers auto-skill extraction)",
    )
    consolidate_parser.add_argument(
        "session_key",
        nargs="?",
        default=None,
        help="Session key to consolidate (omit to list pending sessions)",
    )
    consolidate_parser.add_argument(
        "--all",
        action="store_true",
        dest="consolidate_all",
        help="Consolidate all sessions with unconsolidated messages",
    )

    # mcp-core (MCP server — spawned by the agent backend, not user-facing)
    sub.add_parser("mcp-core", help=argparse.SUPPRESS)

    # Builtin app MCP servers (spawned by the agent backend, not user-facing)
    for _bname in _BUILTIN_NAMES:
        sub.add_parser(f"mcp-{_bname}", help=argparse.SUPPRESS)

    # mcp-playwright-proxy (MCP proxy — compresses accessibility tree responses)
    proxy_parser = sub.add_parser("mcp-playwright-proxy", help=argparse.SUPPRESS)
    proxy_parser.add_argument("proxy_args", nargs=argparse.REMAINDER)

    # browse — auth management for Playwright MCP browsing
    browse_parser = sub.add_parser(
        "browse",
        help="Setup for Playwright MCP browsing",
        epilog="""
Examples:
  kiroclaw browse setup                        # Install Playwright + browsers
  kiroclaw browse auth health                  # Check auth status
""",
        formatter_class=_fmt,
    )
    browse_parser.add_argument(
        "browse_args",
        nargs=argparse.REMAINDER,
        help="browse sub-command and its arguments",
    )

    # learn
    learn_parser = sub.add_parser(
        "learn",
        help="Save or manage learned corrections",
        epilog="""
Examples:
  kiroclaw learn list
  kiroclaw learn add 'use snake_case for variables' --category tool
  kiroclaw learn remove 'snake_case'
""",
        formatter_class=_fmt,
    )
    learn_sub = learn_parser.add_subparsers(dest="learn_action")
    learn_add = learn_sub.add_parser("add", help="Save a lesson")
    learn_add.add_argument("rule", help="The rule or correction to remember")
    learn_add.add_argument(
        "--category",
        choices=["tool", "preference", "knowledge"],
        default="knowledge",
        help="Lesson category (default: knowledge)",
    )
    learn_add.add_argument("--negative", help="What NOT to do (optional)")
    learn_sub.add_parser("list", help="List all lessons")
    learn_rm = learn_sub.add_parser("remove", help="Remove lessons matching a substring")
    learn_rm.add_argument("query", help="Substring to match against lesson rules")

    # artifact
    art_parser = sub.add_parser(
        "artifact",
        help="Manage saved artifacts (LLM-generated UI)",
        epilog="""
Examples:
  kiroclaw artifact list
  kiroclaw artifact list --tag op --kind widget
  kiroclaw artifact save --name "CR Queue" --content-file widget.html --tags ops,cr
  cat widget.html | kiroclaw artifact save --name "Pipeline Health"
  kiroclaw artifact show cr-queue
  kiroclaw artifact show cr-queue --version 2
  kiroclaw artifact show cr-queue --meta
  kiroclaw artifact update cr-queue --content-file widget.html
  kiroclaw artifact versions cr-queue
  kiroclaw artifact delete cr-queue
""",
        formatter_class=_fmt,
    )
    art_sub = art_parser.add_subparsers(dest="artifact_action")

    art_list = art_sub.add_parser("list", help="List saved artifacts")
    art_list.add_argument("--tag", help="Filter by tag")
    art_list.add_argument(
        "--kind",
        choices=["widget", "html", "markdown", "svg", "json", "text"],
        help="Filter by kind",
    )
    art_list.add_argument("-q", "--q", help="Substring filter on artifact name")

    art_show = art_sub.add_parser("show", help="Print an artifact's content")
    art_show.add_argument("slug", help="Artifact slug")
    art_show.add_argument("--version", type=int, help="Specific version (default: current)")
    art_show.add_argument(
        "--meta",
        action="store_true",
        help="Print metadata as JSON instead of the content body",
    )

    art_save = art_sub.add_parser("save", help="Save a new artifact")
    art_save.add_argument("--name", required=True, help="Human-readable name")
    art_save.add_argument(
        "--kind",
        choices=["widget", "html", "markdown", "svg", "json", "text"],
        default="widget",
        help="Artifact kind (default: widget)",
    )
    art_save.add_argument("--content", help="Inline content")
    art_save.add_argument("--content-file", help="Path to file containing the content")
    art_save.add_argument("--description", default="", help="Short description")
    art_save.add_argument("--tags", help="Comma-separated tag list")

    art_update = art_sub.add_parser("update", help="Update an artifact in place")
    art_update.add_argument("slug", help="Artifact slug to update")
    art_update.add_argument("--content", help="Inline new content")
    art_update.add_argument("--content-file", help="Path to file containing new content")
    art_update.add_argument("--name", help="New name (rename)")
    art_update.add_argument("--description", help="New description")
    art_update.add_argument("--tags", help="Replacement tag list (comma-separated)")

    art_del = art_sub.add_parser("delete", help="Delete an artifact and all its versions")
    art_del.add_argument("slug", help="Artifact slug to delete")

    art_ver = art_sub.add_parser("versions", help="List the version numbers for an artifact")
    art_ver.add_argument("slug", help="Artifact slug")

    # Memory
    mem_parser = sub.add_parser("memory", help="Manage vector memory system")
    mem_sub = mem_parser.add_subparsers(dest="mem_action")
    mem_sub.add_parser("list", help="Show semantic memory entries")
    mem_search = mem_sub.add_parser("search", help="Search episodic memories")
    mem_search.add_argument("query", help="Search query text")
    mem_sub.add_parser("stats", help="Show memory statistics")
    mem_sub.add_parser("audit", help="Scan memory for suspicious content")
    mem_export = mem_sub.add_parser("export", help="Export all memory to JSON")
    mem_export.add_argument("--output", "-o", help="Output file (default: stdout)")
    mem_sub.add_parser("migrate", help="Migrate legacy markdown memory to vector store")
    mem_import = mem_sub.add_parser("import", help="Import memory from JSON file")
    mem_import.add_argument("file", help="Path to JSON file (export format)")

    # agent
    agent_parser = sub.add_parser("agent", help="Manage KiroClaw agent definitions")
    agent_sub = agent_parser.add_subparsers(dest="agent_action")
    agent_sub.add_parser("list", help="List KiroClaw agents")
    agent_create = agent_sub.add_parser("create", help="Create a KiroClaw agent")
    agent_create.add_argument("--name", required=True, help="Agent name")
    agent_create.add_argument("--kiro-agent", default="kiroclaw", help="Kiro agent name")
    agent_create.add_argument("--workspace", default="default", help="Workspace name")
    agent_create.add_argument("--memory-store", default="default", help="Memory store name")
    agent_update = agent_sub.add_parser("update", help="Update a KiroClaw agent")
    agent_update.add_argument("name", help="Agent name to update")
    agent_update.add_argument("--kiro-agent", help="New kiro agent name")
    agent_update.add_argument("--workspace", help="New workspace name")
    agent_update.add_argument("--memory-store", help="New memory store name")
    agent_delete = agent_sub.add_parser("delete", help="Delete a KiroClaw agent")
    agent_delete.add_argument("name", help="Agent name to delete")

    # workspace
    ws_parser = sub.add_parser("workspace", help="Manage workspace definitions")
    ws_sub = ws_parser.add_subparsers(dest="workspace_action")
    ws_sub.add_parser("list", help="List workspaces")
    ws_create = ws_sub.add_parser("create", help="Create a workspace")
    ws_create.add_argument("--name", required=True, help="Workspace name")
    ws_create.add_argument("--dir", default=None, help="Workspace directory path")
    ws_create.add_argument("--copy-from", help="Copy dir from an existing workspace")
    ws_update = ws_sub.add_parser("update", help="Update a workspace")
    ws_update.add_argument("name", help="Workspace name to update")
    ws_update.add_argument("--dir", help="New directory path")
    ws_delete = ws_sub.add_parser("delete", help="Delete a workspace")
    ws_delete.add_argument("name", help="Workspace name to delete")

    # app
    app_parser = sub.add_parser(
        "app",
        help="Manage KiroClaw apps",
        epilog="""
Examples:
  kiroclaw app install /path/to/oncall-watchtower
  kiroclaw app list
  kiroclaw app enable oncall-watchtower
  kiroclaw app disable oncall-watchtower
  kiroclaw app info oncall-watchtower
  kiroclaw app uninstall oncall-watchtower
""",
        formatter_class=_fmt,
    )
    app_sub = app_parser.add_subparsers(dest="app_action")
    app_install = app_sub.add_parser("install", help="Install an app from a local directory")
    app_install.add_argument("source", help="Path to app directory containing app.json")
    app_sub.add_parser("list", help="List installed apps")
    app_enable = app_sub.add_parser("enable", help="Enable an installed app")
    app_enable.add_argument("name", help="App name to enable")
    app_disable = app_sub.add_parser("disable", help="Disable an installed app")
    app_disable.add_argument("name", help="App name to disable")
    app_uninstall = app_sub.add_parser("uninstall", help="Uninstall an app")
    app_uninstall.add_argument("name", help="App name to uninstall")
    app_uninstall.add_argument(
        "--keep-data", action="store_true", help="Preserve app data directory"
    )
    app_info = app_sub.add_parser("info", help="Show app details")
    app_info.add_argument("name", help="App name")
    app_init = app_sub.add_parser("init", help="Scaffold a new app")
    app_init.add_argument("name", help="App name (kebab-case)")
    app_init.add_argument("--dir", default=".", help="Output directory (default: current)")
    app_init.add_argument("--backend", action="store_true", help="Include backend stub")
    app_init.add_argument("--ui", action="store_true", help="Include UI frontend (ESM + Vite)")
    app_init.add_argument("--cron", action="store_true", help="Include sample cron job")

    # config
    cfg_parser = sub.add_parser(
        "config",
        help="Get or set configuration values",
        epilog="""
Examples:
  kiroclaw config get                   # Show all config
  kiroclaw config get agent.provider    # Get a specific value
  kiroclaw config set dashboard.url http://localhost:5476
  kiroclaw config edit                  # Open in $EDITOR

The dashboard port is set with the KIROCLAW_PORT env var, not a config key.
""",
        formatter_class=_fmt,
    )
    cfg_sub = cfg_parser.add_subparsers(dest="config_action")
    cfg_get = cfg_sub.add_parser("get", help="Get a config value (or all if no key)")
    cfg_get.add_argument("key", nargs="?", help="Dot-separated key (e.g. agent.provider)")
    cfg_set = cfg_sub.add_parser("set", help="Set a config value")
    cfg_set.add_argument("key", nargs="?", help="Dot-separated key (e.g. agent.provider)")
    cfg_set.add_argument("value", nargs="?", help="Value to set")
    cfg_set.add_argument("--file", "-f", dest="file", help="Load full config from a JSON file")
    cfg_set.add_argument(
        "--local",
        action="store_true",
        help="Save to config.local.json (persists across upgrades)",
    )
    cfg_sub.add_parser("edit", help="Open config in $EDITOR")

    # aim
    aim_parser = sub.add_parser(
        "aim",
        help="Manage capability-package parity across providers",
        epilog="""
Examples:
  kiroclaw aim sync-cc        # Install capability packages for Claude Code
""",
        formatter_class=_fmt,
    )
    aim_sub = aim_parser.add_subparsers(dest="aim_action")
    aim_sub.add_parser("sync-cc", help="Install capability packages as CC plugins")

    if len(sys.argv) > 1 and sys.argv[1] == "mcp-playwright-proxy":
        from kiro_claw.mcp_playwright_proxy import run_proxy

        run_proxy(sys.argv[2:])
        return

    args = parser.parse_args()

    # ``gateway --seed <fixture>`` populates $KIROCLAW_HOME from a hand-authored
    # fixture BEFORE the gateway starts — lets a dev spin up a pre-populated
    # server in one command. We run the seed here (post parse_args, but BEFORE
    # ``KiroClawConfig.load()`` and the file-log handler attach at line ~603):
    # both of those call ``config_dir()`` which ``mkdir``s $KIROCLAW_HOME, which
    # would pre-populate the target and break ``shutil.copytree``'s
    # empty-target-only contract in Phase 1.A.  If seed fails, exit with the
    # seed's own exit code instead of continuing into the gateway — running
    # the gateway against a half-seeded or wrong-state $KIROCLAW_HOME would be
    # worse than a clean failure.
    #
    # ``is not None`` (not truthiness): argparse assigns ``""`` when the user
    # explicitly passes ``--seed ""``, and ``""`` is falsy. A truthiness check
    # would silently start the gateway without seeding — exactly the silent
    # wrong-state startup the rest of this block is set up to avoid.
    # ``_resolve_fixture("")`` has an explicit rail for this case.
    if args.command == "gateway" and getattr(args, "seed", None) is not None:
        _rc = seed_cmd(args)
        if _rc != 0:
            sys.exit(_rc)

    if args.verbose >= 2:
        level = logging.DEBUG
    elif args.verbose >= 1:
        level = logging.INFO
    else:
        level = logging.WARNING
    logging.basicConfig(
        level=logging.WARNING,  # third-party libs stay quiet
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    # KiroClaw loggers: --verbose CLI flag takes precedence, otherwise
    # fall back to the persistent log_level from config.
    if args.verbose == 0:
        try:
            _cfg = KiroClawConfig.load()
            _persisted = _cfg.agent.log_level.upper()
            level = getattr(logging, _persisted, logging.WARNING)
        except Exception:
            pass  # config missing or corrupt — keep default WARNING
    logging.getLogger("kiro_claw").setLevel(level)

    # Persistent file log — respects the configured log_level
    _log_file = config_dir() / "gateway.log"
    _fh = RotatingFileHandler(_log_file, maxBytes=2 * 1024 * 1024, backupCount=3)
    _fh.setLevel(level)
    _fh.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S")
    )
    logging.getLogger("kiro_claw").addHandler(_fh)

    # No subcommand given (`kiroclaw` with no args) — show banner + help and exit.
    # Without this guard, the `args.command.startswith("mcp-")` branch later
    # in the dispatch chain raises AttributeError on None.
    if args.command is None:
        print(BANNER)
        parser.print_help()
        return

    # ── Platform context boot (CPP seam) ──
    # Resolve + install the PlatformContext ONCE, before any subcommand spins up
    # services.  Standalone (no companion, no KIROCLAW_PROFILE) composes the
    # all-defaults context, so behavior is identical to today; ``boot_platform``
    # is idempotent so a later ``run_gateway`` boot is a no-op.  Failure to
    # compose a non-standalone profile is fail-closed (raises), but a standalone
    # boot never raises — keep the call defensive so a corrupt config cannot
    # break the CLI for the standalone edition.
    #
    # ``doctor`` is exempt from the fail-closed re-raise: it is the read-only
    # triage command whose whole job is to diagnose a broken setup (including a
    # failed composition), so it must RUN rather than abort with a traceback —
    # otherwise the one command that could explain the failure is also bricked
    # by it.  It does no agent/credential work, so running it without an
    # installed context is safe; _doctor() reports the composition failure.
    try:
        boot_platform(KiroClawConfig.load())
    except Exception as exc:
        # Fail-closed: a non-standalone profile that cannot compose (companion
        # missing/rejected/version-mismatched) MUST NOT silently downgrade to
        # open-source defaults — re-raise so the CLI aborts instead of running
        # mcp-core/mcp-cron/etc. with no security overlay or credential
        # redaction. PluginAdmissionError subclasses PlatformCompositionError.
        if isinstance(exc, PlatformCompositionError):
            if args.command == "doctor":
                logging.getLogger("kiro_claw").debug(
                    "platform composition failed; doctor will report it", exc_info=True
                )
                _platform_boot_error = exc
            else:
                raise
        else:
            # Standalone boot never raises; only genuinely-unexpected errors
            # reach here, and the standalone edition must not break on a corrupt
            # config.
            logging.getLogger("kiro_claw").debug("platform boot deferred", exc_info=True)
            _platform_boot_error = None
    else:
        _platform_boot_error = None

    if args.command == "chat":
        if getattr(args, "tui", False):
            _tui(args)
        else:
            cfg = KiroClawConfig.load()
            if cfg.to_dict().get("dashboard", {}).get("default_mode") == "tui":
                _tui(args)
            else:
                print("Tip: Try `kiroclaw tui` for the new terminal UI experience")
                asyncio.run(_chat(args.message, args.model, agent=getattr(args, "agent", None)))
    elif args.command == "tui":
        _tui(args)
    elif args.command == "gateway":
        gw_kwargs = _resolve_gateway_args(args)
        asyncio.run(_gateway(**gw_kwargs))
    elif args.command == "setup":
        _setup(
            agent_only=getattr(args, "agent_only", False),
            electron_only=getattr(args, "electron_only", False),
            clean=getattr(args, "clean", False),
        )
    elif args.command == "doctor":
        _doctor(platform_boot_error=_platform_boot_error)
    elif args.command == "manifest":
        _manifest(
            alias=getattr(args, "alias", None),
            output=getattr(args, "output", None),
            url=getattr(args, "url", False),
        )
    elif args.command == "cron":
        _cron(args)
    elif args.command == "spawn":
        _spawn(args)
    elif args.command == "run":
        asyncio.run(_run_task(args))
    elif args.command == "learn":
        _learn(args)
    elif args.command == "artifact":
        _artifact(args)
    elif args.command == "memory":
        _memory_cmd(args)
    elif args.command == "mcp-cron":
        from kiro_claw.mcp_cron import run_mcp_server as run_mcp_cron_server

        run_mcp_cron_server()
    elif args.command == "mcp-core":
        from kiro_claw.mcp_core import run_mcp_core_server

        run_mcp_core_server()
    elif args.command.startswith("mcp-") and args.command[4:] in _BUILTIN_NAMES:
        _mod = importlib.import_module(f"kiro_claw.apps.builtins.{args.command[4:]}.mcp_server")
        _mod.run_mcp_server()
    elif args.command == "browse":
        run_browse(getattr(args, "browse_args", []))
    elif args.command == "eval":
        asyncio.run(_run_eval(args))
    elif args.command == "security":
        _security(args)
    elif args.command == "policy":
        from kiro_claw.cli_commands import _policy

        _policy(args)
    elif args.command == "update":
        _update()
    elif args.command == "stop":
        _stop(args.port)
    elif args.command == "restart":
        _restart(args.port)
    elif args.command == "service":
        sys.exit(_service_cmd(args))
    elif args.command == "logs":
        _logs_cmd(args)
    elif args.command == "token":
        _token(args)
    elif args.command == "logout":
        _logout(resolve_client_port(args.port))
    elif args.command == "status":
        _status(args)
    elif args.command == "consolidate":
        _consolidate_cmd(args)
    elif args.command == "config":
        _config_cmd(args)
    elif args.command == "snapshot":
        from kiro_claw.snapshot import snapshot_main

        rc = snapshot_main(parsed=args)
        if rc:
            raise SystemExit(rc)
    elif args.command == "restore":
        from kiro_claw.snapshot import restore_main

        rc = restore_main(parsed=args)
        if rc:
            raise SystemExit(rc)
    elif args.command == "agent":
        _handle_agent(args)
    elif args.command == "workspace":
        _handle_workspace(args)
    elif args.command == "app":
        _handle_app(args)
    elif args.command == "aim":
        _handle_aim(args)
    else:
        print(BANNER)
        parser.print_help()


# ── Config ──


from kiro_claw.cli_chat import _chat, _tui  # noqa: E402
from kiro_claw.cli_commands import (  # noqa: E402
    _artifact,
    _cron,
    _handle_agent,
    _handle_aim,
    _handle_app,
    _handle_workspace,
    _learn,
    _memory_cmd,
    _run_eval,
    _security,
    _spawn,
)
from kiro_claw.cli_config import _config_cmd  # noqa: E402
from kiro_claw.cli_doctor import _doctor  # noqa: E402
from kiro_claw.cli_server import (  # noqa: E402
    _gateway,
    _logout,
    _logs_cmd,
    _restart,
    _run_task,
    _service_cmd,
    _status,
    _stop,
    _token,
    _update,
    resolve_client_port,
)
from kiro_claw.cli_setup import (  # noqa: E402, F401
    _fix_shell_profiles,
    _manifest,
    _setup,
)
