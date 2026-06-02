"""CLI doctor subcommand — verify KiroClaw setup and diagnose issues."""

from __future__ import annotations

import json
import os
import platform as _plat
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from kiro_claw import __version__ as _mc_version
from kiro_claw.acp.client import KIRO_CLI_BIN
from kiro_claw.agent import AGENT_FILENAME, KIRO_AGENTS_DIR
from kiro_claw.config import KiroClawConfig
from kiro_claw.config.loader import config_dir
from kiro_claw.constants import OLLAMA_DOCKER_CONTAINER
from kiro_claw.dashboard.origin import (
    is_local_only,
    machine_hostname,
    parse_dashboard_url,
)
from kiro_claw.slack.enterprise import validate_enterprise
from kiro_claw.transcribe import _find_whisper, ensure_ffmpeg_in_path

_MIN_NODE_VERSION = 16

# Default agent backend is the public claude-agent-acp; kiro-cli stays an
# OPTIONAL backend. Resolve it via PATH only and report gracefully when absent.
_CLAUDE_ACP_BIN = "claude-agent-acp"


def _detect_docker_ollama() -> str | None:
    """Return display string if Ollama Docker container exists, else None."""
    docker = shutil.which("docker")
    if not docker:
        return None
    try:
        result = subprocess.run(
            [docker, "inspect", "--format", "{{.State.Status}}", OLLAMA_DOCKER_CONTAINER],
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = result.stdout.strip()
        if result.returncode == 0 and status in ("running", "exited", "paused", "created"):
            return f"Docker ({OLLAMA_DOCKER_CONTAINER}) [{status}]"
    except (subprocess.TimeoutExpired, subprocess.SubprocessError, OSError):
        pass
    return None


def _doctor_ollama_install(issues: list[str]) -> None:
    """Diagnose why Ollama auto-install would fail."""

    system = _plat.system()

    if system == "Darwin":
        brew = shutil.which("brew")
        if brew:
            result = subprocess.run(
                [brew, "info", "ollama"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode == 0:
                print(
                    "  brew info:   ✅ formula available" " (enable will run: brew install ollama)"
                )
            else:
                err = result.stderr.strip()[:200]
                print(f"  brew info:   ❌ {err}")
                issues.append("ollama (brew cannot resolve formula)")
        else:
            print("  brew:        ⚠️  not found" " — enable will try direct download")
        print("               Install: brew install ollama")
    elif system == "Linux":
        brew = shutil.which("brew")
        brew_formula_ok = False
        if brew:
            try:
                result = subprocess.run(
                    [brew, "info", "ollama"],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                brew_formula_ok = result.returncode == 0
            except subprocess.TimeoutExpired:
                print("  brew:        ⚠️  timed out checking formula")
            else:
                if brew_formula_ok:
                    print(
                        "  brew:        ✅ (enable will run: brew install ollama)"
                    )
                else:
                    print("  brew:        ⚠️  formula not available; will try curl fallback")
        if not brew_formula_ok:
            curl = shutil.which("curl")
            if curl:
                print(
                    "  curl:        ✅ (enable will run:"
                    " curl -fsSL https://ollama.com/install.sh | sh)"
                )
            else:
                print("  curl:        ❌ not found — auto-install will fail")
                issues.append("ollama (curl missing)")
        if brew_formula_ok:
            print("               Install: brew install ollama")
        else:
            print("               Install: curl -fsSL https://ollama.com/install.sh | sh")
    else:
        print(f"  platform:    ❌ auto-install unsupported on {system}")
        issues.append("ollama (unsupported platform)")


def _doctor() -> None:
    """Verify KiroClaw setup — check dependencies, config, credentials, connectivity."""

    print("KiroClaw Doctor 🐾\n")
    issues: list[str] = []

    # ── Dependencies ──
    print("Dependencies")
    # Default backend is the public claude-agent-acp (npm). kiro-cli is an
    # OPTIONAL backend — report gracefully when either is absent.
    claude_acp = shutil.which(_CLAUDE_ACP_BIN)
    if claude_acp:
        print(f"  claude-acp:  ✅ {claude_acp}")
    else:
        print("  claude-acp:  ⏭  not found (default backend)")
        print("               Install: npm i -g @agentclientprotocol/claude-agent-acp")

    kiro = shutil.which(KIRO_CLI_BIN)
    if kiro:
        print(f"  kiro-cli:    ✅ {kiro} (optional backend)")
        # Check login status — best-effort, never a hard failure
        try:
            r = subprocess.run(
                [KIRO_CLI_BIN, "whoami"],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode == 0:
                print("  kiro login:  ✅")
            else:
                print("  kiro login:  ⏹ not logged in (optional: kiro-cli login)")
        except Exception:
            print("  kiro login:  ⚠️  could not check")
    else:
        print("  kiro-cli:    ⏭  not configured (optional backend)")

    git = shutil.which("git")
    if git:
        print(f"  git:         ✅ {git}")
    else:
        print("  git:         ❌ not found (needed for kiroclaw update)")
        issues.append("git")

    node = shutil.which("node")
    if node:
        try:
            node_ver_result = subprocess.run(
                ["node", "-v"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            major = int(node_ver_result.stdout.strip().lstrip("v").split(".")[0])
            if major >= _MIN_NODE_VERSION:
                print(f"  node:        ✅ {node} (v{major})")
            else:
                print(
                    f"  node:        ⚠️  v{major} < {_MIN_NODE_VERSION} (frontend needs Node {_MIN_NODE_VERSION}+)"
                )
                print("               Fix: install Node.js >= 16")
        except Exception:
            print(f"  node:        ✅ {node}")
    else:
        print(f"  node:        ⚠️  not found (frontend needs Node {_MIN_NODE_VERSION}+)")
        print("               Fix: install Node.js >= 16")

    # venv detection — used by the runtime section below
    venv_py = Path(__file__).resolve().parents[2] / ".venv" / "bin" / "python3"
    is_venv_install = venv_py.is_file()

    # ── Project ──
    print("\nProject")
    proj = os.environ.get("KIROCLAW_PROJECT_DIR", "")
    stale_project = False
    if not proj:
        # Check saved project_dir file
        saved_proj = config_dir() / "project_dir"
        if saved_proj.is_file():
            saved = saved_proj.read_text(encoding="utf-8").strip()
            if saved and Path(saved).is_dir():
                proj = saved
            else:
                print(f"  project dir: ❌ stale — points to deleted {saved}")
                print(f"               Fix: rm {config_dir() / 'project_dir'}")
                issues.append("stale project_dir")
                stale_project = True
    if proj and Path(proj).is_dir():
        print(f"  project dir: ✅ {proj}")
        git_dir = Path(proj) / ".git"
        if git_dir.is_dir():
            print("  git repo:    ✅")
        else:
            print("  git repo:    ⚠️  not a git repo")
    elif not stale_project:
        print("  project dir: ⚠️  not set (run kiroclaw setup from project root)")

    # ── Agent config ──
    print("\nAgent")
    agent_path = KIRO_AGENTS_DIR / AGENT_FILENAME
    if agent_path.exists():
        print(f"  config:      ✅ {agent_path}")
    else:
        print("  config:      ❌ not found (run kiroclaw setup)")
        issues.append("agent config")

    # ── Config ──
    print("\nConfiguration")
    cfg_dir = config_dir()
    cfg = KiroClawConfig.load()
    if cfg_dir.exists():
        print(f"  config dir:  ✅ {cfg_dir}")
    else:
        print(f"  config dir:  📁 {cfg_dir} (will be created)")
    print(f"  provider:    {cfg.agent.provider}")
    print(f"  model:       {cfg.agent.model}")
    print(f"  approval:    {cfg.agent.approval_mode}")
    _host: str = ""
    _port: int | None = None
    try:
        _host, _port = parse_dashboard_url(cfg.dashboard.url)
    except Exception:
        print("  dashboard:   ⚠️  cannot parse dashboard URL from config")
        issues.append("dashboard URL misconfigured")
    _display_host = _host or "localhost"
    if _port:
        print(f"  dashboard:   http://{_display_host}:{_port}")

    # Dashboard auth mode
    creds = cfg.load_credentials()
    _has_slack = bool(creds.get("SLACK_APP_TOKEN") and creds.get("SLACK_BOT_TOKEN"))
    _local = is_local_only(_host, _has_slack)
    if _local:
        print("  bind:        127.0.0.1 (local-only, SSH tunnel for remote)")
        print("  auth:        loopback trusted (no token required)")
    else:
        print("  bind:        0.0.0.0 (all interfaces)")
        print("  auth:        ✅ token auth required (via !dashboard)")
        if not _has_slack:
            print("  auth:        ⚠️  Slack not configured — token generation unavailable")
            issues.append("dashboard auth: remote bind without Slack")

    # ── MCP Tools ──
    print("\nMCP Tools")
    if agent_path.exists():

        try:
            agent_data = json.loads(agent_path.read_text(encoding="utf-8"))
        except Exception:
            agent_data = {}
        tools = agent_data.get("tools", [])
        allowed = agent_data.get("allowedTools", [])
        mcps = agent_data.get("mcpServers", {})
        mcp_fixed = False
        mcp_cmd_fixed = False
        for ref in ("@kiroclaw-cron", "@kiroclaw-core"):
            name = ref[1:]
            in_tools = ref in tools
            in_allowed = ref in allowed
            in_servers = name in mcps
            if in_tools and in_allowed and in_servers:
                cmd = mcps[name].get("command", "")
                exists = Path(cmd).is_file() if cmd else False
                if exists:
                    print(f"  {ref}: ✅")
                else:
                    resolved = shutil.which("kiroclaw")
                    if resolved:
                        mcps[name]["command"] = resolved
                        mcp_cmd_fixed = True
                        print(f"  {ref}: 🔧 fixed stale path: {cmd} → {resolved}")
                    else:
                        print(f"  {ref}: ❌ binary not found: {cmd}")
                        issues.append(f"{ref} binary")
            else:
                missing: list[str] = []
                if not in_servers:
                    missing.append("mcpServers")
                if not in_tools:
                    missing.append("tools")
                if not in_allowed:
                    missing.append("allowedTools")
                print(f"  {ref}: ❌ missing from {', '.join(missing)}")
                issues.append(f"{ref} config")
                # Auto-fix
                if not in_tools:
                    tools.append(ref)
                if not in_allowed:
                    allowed.append(ref)
                mcp_fixed = True
        if mcp_fixed or mcp_cmd_fixed:
            agent_data["tools"] = tools
            agent_data["allowedTools"] = allowed
            agent_path.write_text(json.dumps(agent_data, indent=2) + "\n", encoding="utf-8")
            if mcp_fixed:
                print("  → Auto-fixed tools/allowedTools in kiroclaw.json")
                issues = [i for i in issues if "config" not in i]
            if mcp_cmd_fixed:
                print("  → Auto-fixed stale binary path(s) in kiroclaw.json")

    # ── Python Runtime ──
    print("\nRuntime")
    # Prefer venv install (pip install -e); otherwise verify the running Python.
    if is_venv_install:
        try:
            py_result = subprocess.run(
                [str(venv_py), "--version"], capture_output=True, text=True, timeout=5
            )
            py_result.check_returncode()
            ver = py_result.stdout.strip()
            print(f"  python:      ✅ {venv_py} ({ver})")
        except Exception as exc:
            print(f"  python:      ❌ venv python broken: {exc}")
            issues.append("venv python")
        else:
            try:
                subprocess.run(
                    [str(venv_py), "-c", "import websockets, slack_sdk, aiohttp"],
                    capture_output=True, timeout=5,
                ).check_returncode()
                print("  deps:        ✅ websockets, slack_sdk, aiohttp available")
            except Exception:
                print("  deps:        ❌ missing modules (websockets/slack_sdk/aiohttp)")
                issues.append("python deps")
    else:
        print(f"  python:      ✅ {sys.executable} ({sys.version.split()[0]})")
        print(f"  kiro_claw:   ✅ {_mc_version}")
        try:
            import aiohttp  # noqa: F401
            import slack_sdk  # noqa: F401
            import websockets  # noqa: F401

            print("  deps:        ✅ websockets, slack_sdk, aiohttp available")
        except ImportError:
            print("  deps:        ❌ missing modules (websockets/slack_sdk/aiohttp)")
            print("               Fix: pip install -e .")
            issues.append("python deps")

    # ── Ollama / Vector Memory ──
    print("\nVector Memory (Ollama)")

    ollama = shutil.which("ollama") or _detect_docker_ollama()
    if ollama:
        print(f"  ollama:      ✅ {ollama}")
    else:
        print("  ollama:      ⏹ not installed (optional — vector memory)")
        _doctor_ollama_install(issues)

    if ollama:
        try:
            with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=2) as resp:
                print("  server:      ✅ running")
        except Exception:
            print("  server:      ⏹ not running (will auto-start on enable)")

    # Check embedding config
    if cfg.memory.embedding_provider == "ollama":
        print("  embeddings:  ✅ enabled")
    else:
        print("  embeddings:  ⏹ disabled (enable from dashboard → Overview → Memory)")

    # ── Speech-to-Text (optional) ──
    print("\nSpeech-to-Text")
    stt_active = cfg.stt.enabled
    needs_whisper = stt_active and cfg.stt.provider == "whisper"
    needs_ffmpeg = stt_active  # both providers use ffmpeg

    if not stt_active:
        print("  status:      ⏹ disabled (enable from dashboard → Overview → Slack)")
    else:
        print(f"  provider:    ✅ {cfg.stt.provider}")

    whisper_bin = _find_whisper(cfg.stt.whisper_path)
    if whisper_bin:
        print(f"  whisper:     ✅ {whisper_bin}")
    elif needs_whisper:
        print("  whisper:     ❌ not found")
        print("               Fix: brew install openai-whisper")
        issues.append("whisper")
    else:
        print("  whisper:     ⏭  not installed (not needed)")

    ensure_ffmpeg_in_path()
    ffmpeg_bin = shutil.which("ffmpeg")
    if ffmpeg_bin:
        print(f"  ffmpeg:      ✅ {ffmpeg_bin}")
    elif needs_ffmpeg:
        print("  ffmpeg:      ❌ not found")
        print("               Fix: brew install ffmpeg")
        issues.append("ffmpeg")
    else:
        print("  ffmpeg:      ⏭  not installed (not needed)")

    # Cloud transcription (AWS Transcribe) is an OPTIONAL feature requiring
    # user-provided AWS credentials and the `amazon-transcribe`/`boto3` extras.
    # It is never a hard failure on a standard install — report gracefully.
    if stt_active and cfg.stt.provider == "transcribe":
        try:
            import amazon_transcribe.client  # noqa: F401

            print("  transcribe:  ✅ amazon_transcribe importable (optional)")
        except ImportError:
            print("  transcribe:  ⏹ optional cloud STT not installed")
            print("               Install: pip install 'kiro-claw[voice]'")

        try:
            import boto3  # noqa: F401

            print("  boto3:       ✅ importable (optional)")
        except ImportError:
            print("  boto3:       ⏹ optional AWS SDK not installed")
            print("               Install: pip install 'kiro-claw[aws]'")

    # ── Slack (optional) ──
    print("\nSlack Integration")
    creds = cfg.load_credentials()
    has_slack = bool(creds.get("SLACK_APP_TOKEN") and creds.get("SLACK_BOT_TOKEN"))
    if has_slack:
        has_owner = bool(creds.get("KIROCLAW_OWNER_ID"))
        print("  tokens:      ✅ configured")
        if has_owner:
            print(f"  owner:       ✅ {creds['KIROCLAW_OWNER_ID']}")
        else:
            print("  owner:       ⚠️  KIROCLAW_OWNER_ID not set")

        # Optional workspace allowlist validation (default-open unless the user
        # configured slack.allowed_enterprise_ids).
        bot_token = creds.get("SLACK_BOT_TOKEN", "")
        if bot_token:
            extra_ids = cfg.slack_enterprise_ids
            if validate_enterprise(bot_token, extra_ids=extra_ids):
                print("  workspace:   ✅ allowed")
            else:
                print("  workspace:   ❌ not in configured workspace allowlist")
                print("               The gateway will refuse to connect.")
                issues.append("slack workspace: not in allowlist")
    else:
        print("  status:      ⏭  not configured (dashboard-only mode)")
        print("  setup:       run 'kiroclaw setup' to add Slack tokens")

    # ── Connectivity ──
    print("\nConnectivity")
    if kiro:
        kiro_result = subprocess.run(
            [KIRO_CLI_BIN, "--version"], capture_output=True, text=True, timeout=5
        )
        if kiro_result.returncode == 0:
            ver = kiro_result.stdout.strip() or kiro_result.stderr.strip()
            print(f"  kiro-cli:    ✅ {ver}")
        else:
            print("  kiro-cli:    ⚠️  exits with error (optional backend)")
    else:
        print("  kiro-cli:    ⏭  skipped (not installed)")

    # Check if gateway is running — connect to 127.0.0.1 (loopback)
    # to avoid DNS resolution issues with the configured hostname.
    # Any HTTP response (even 401/403 from token auth) means the gateway is up.
    is_remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))

    if _port:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{_port}/api/status")
            with urllib.request.urlopen(req, timeout=2) as resp:
                data = json.loads(resp.read())
            print(f"  gateway:     ✅ running (uptime {data.get('uptime', '?')})")
        except urllib.error.HTTPError as he:
            # 401/403 means gateway is running but requires token auth
            if he.code in (401, 403):
                print("  gateway:     ✅ running (token auth enabled)")
            else:
                print(f"  gateway:     ⚠️  HTTP {he.code}")
        except (urllib.error.URLError, OSError):
            print("  gateway:     ⏹  not running")
        except Exception:
            print("  gateway:     ⚠️  running but returned unexpected response")

        # SSH tunnel hint for remote hosts
        if is_remote:
            mh = machine_hostname() or "this-host"
            print("\n  💡 Remote access: Run on your LOCAL machine:")
            print(f"     ssh -NL {_port}:localhost:{_port} {mh}")
            print("     Then run: kiroclaw token")

    # Verify token auth is enforced on non-loopback (security check)
    if _port and not _local:
        if not _host:
            issues.append("cannot verify dashboard auth (host unknown)")
        else:
            try:
                ext_req = urllib.request.Request(f"http://{_host}:{_port}/api/status")
                try:
                    with urllib.request.urlopen(ext_req, timeout=2) as resp:
                        # 200 without token = auth is NOT enforced
                        print("  auth check:  ❌ external access allowed without token!")
                        issues.append("dashboard auth: no token required on external interface")
                except urllib.error.HTTPError as he:
                    if he.code in (401, 403):
                        print("  auth check:  ✅ token required on external interface")
                    else:
                        print(f"  auth check:  ⚠️  HTTP {he.code}")
            except Exception:
                print("  auth check:  ⏭  could not reach external interface")

    # ── Summary ──
    print()
    if issues:
        print(f"❌ Fix these issues: {', '.join(issues)}")
        sys.exit(1)
    else:
        print("✅ KiroClaw is ready!")
