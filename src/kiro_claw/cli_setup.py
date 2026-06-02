"""CLI setup subcommand — interactive credential and config wizard."""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import sys
from importlib.resources import files as _pkg_files
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from kiro_claw.acp.client import KIRO_CLI_BIN
from kiro_claw.aim_agents import (
    install_cc_plugin,
    installed_kiro_packages_missing_from_cc,
)
from kiro_claw.browser.setup import (
    ensure_playwright_installed,
    generate_playwright_config,
    get_extension_token,
    has_playwright_extension,
    is_playwright_installed,
    patch_mcp_extension,
    patch_mcp_headless,
    refresh_storage_state,
)
from kiro_claw.cli_chat import _ensure_default_agent_in_config
from kiro_claw.conductor_skill import generate_conductor_skill
from kiro_claw.config import KiroClawConfig
from kiro_claw.config.loader import (
    _WORKSPACE_DIR_NAME,
    CRED_OWNER_ID,
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    DASHBOARD_PORT,
    _default_workspace_base,
    _workspace_dir_file,
    config_path,
    env_path,
)
from kiro_claw.constants import DATA_WARNING
from kiro_claw.skills import SkillsLoader


def _get_alias() -> str:
    """Return the user's login name (used to name the Slack app)."""
    alias = os.environ.get("USER") or ""
    if not alias:
        try:
            alias = os.getlogin()
        except OSError:
            pass
    if not alias:
        alias = input("  Your username (e.g. johndoe): ").strip()
    if not alias:
        print(
            "❌ Cannot determine username. Set $USER or re-run with "
            "`kiroclaw manifest --alias <alias>`.",
            file=sys.stderr,
        )
        sys.exit(1)
    return alias


def _manifest(alias: str | None = None, output: str | None = None, url: bool = False) -> None:
    """Render slack-manifest.yaml with the user's alias substituted."""

    alias = alias or _get_alias()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", alias):
        print(
            "❌ Invalid alias — must be alphanumeric, hyphens, or underscores only.",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        template_text = (
            _pkg_files("kiro_claw").joinpath("slack-manifest.yaml").read_text(encoding="utf-8")
        )
    except FileNotFoundError:
        print("❌ Cannot find slack-manifest.yaml", file=sys.stderr)
        sys.exit(1)
    rendered = template_text.replace("{{ALIAS}}", alias)
    if url:
        # Strip comment lines to shorten the URL
        lines = [ln for ln in rendered.splitlines() if not ln.lstrip().startswith("#")]
        encoded = quote("\n".join(lines).strip() + "\n", safe="")
        print("\n🔗 Click to create your Slack app:\n")
        print(f"https://api.slack.com/apps?new_app=1&manifest_yaml={encoded}\n")
    elif output:
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
        print(f"✅ Manifest written to {output} (name: KiroClaw-{alias})")
    else:
        print(rendered)


def _fix_shell_profiles() -> None:
    """Remove stale KiroClaw PATH entries from shell profiles."""
    home = Path.home()
    profiles = [
        home / ".zshrc",
        home / ".bashrc",
        home / ".bash_profile",
        home / ".profile",
    ]
    stale_markers = [
        ".kiroclaw-app",
    ]
    cleaned_profiles: list[str] = []
    for profile in profiles:
        if not profile.is_file():
            continue
        try:
            lines = profile.read_text(encoding="utf-8").splitlines(keepends=True)
            cleaned = []
            removed = False
            for line in lines:
                if any(m in line for m in stale_markers) and "PATH" in line:
                    removed = True
                    continue
                cleaned.append(line)
            if removed:
                profile.write_text("".join(cleaned), encoding="utf-8")
                print(f"  🔧 Cleaned stale KiroClaw PATH from {profile.name}")
                cleaned_profiles.append(profile.name)
        except OSError:
            pass
    if cleaned_profiles:
        sources = " or ".join(f"`source ~/{p}`" for p in cleaned_profiles)
        print(f"  ⚠️  Run {sources} or open a new terminal for PATH changes to take effect.")


def _ensure_prerequisites() -> bool:
    """Report on optional prerequisites resolved from PATH.

    The public build's default agent backend is ``claude-agent-acp`` (public
    npm). This performs no installs and never blocks setup — it only prints
    guidance for optional tooling that is missing from PATH. Always returns
    True so setup proceeds.
    """
    header_printed = False

    def _header() -> None:
        nonlocal header_printed
        if not header_printed:
            print("── Prerequisites ──\n")
            header_printed = True

    # Node is required to run the claude-agent-acp backend and the dashboard build.
    if not shutil.which("node"):
        _header()
        print("  ⚠️  node not found on PATH — install Node.js >= 16 from https://nodejs.org\n")

    # kiro-cli is an OPTIONAL backend. Absence is fine; just note it.
    if not shutil.which(KIRO_CLI_BIN):
        _header()
        print("  ℹ️  kiro-cli not found (optional backend) — the default "
              "claude-agent-acp backend will be used.\n")

    return True


def _setup(agent_only: bool = False, clean: bool = False) -> None:
    """Install agent config and optionally configure credentials."""
    from kiro_claw.agent import install_agent  # circular import: agent imports cli
    from kiro_claw.cli import _project_dir_file  # circular import: cli -> cli_setup -> cli

    print("KiroClaw Setup 🐾\n")
    print(f"  {DATA_WARNING.replace(chr(10), chr(10) + '  ')}\n")

    # Report on optional prerequisites.
    _ensure_prerequisites()

    # 0. Save project dir so kiroclaw works from anywhere
    proj = os.environ.get("KIROCLAW_PROJECT_DIR")
    if proj:
        _project_dir_file().parent.mkdir(parents=True, exist_ok=True)
        _project_dir_file().write_text(proj + "\n", encoding="utf-8")
        print(f"  ✅ Project dir saved: {proj}")

    # 1. Choose workspace directory (skip for agent-only — not relevant)
    if not agent_only:
        _setup_workspace_dir()

    # 2. Install the agent config
    print("Installing agent config...")
    agent_path = install_agent(clean=clean)
    print(f"  ✅ Agent installed: {agent_path}")

    # 2b. Ensure config.json has default KiroClaw agent for fresh installs
    _ensure_default_agent_in_config()

    # 2c. Generate conductor skill if enabled (agent delegation).
    try:
        cfg = KiroClawConfig.load()
        if cfg.agent.conductor_skill:
            generate_conductor_skill(SkillsLoader())
            print("  ✅ Conductor skill generated")
        else:
            # Clean up stale skill if previously enabled then disabled.
            skill_path = SkillsLoader()._dir / "conductor" / "SKILL.md"
            if skill_path.exists():
                skill_path.unlink()
    except Exception as exc:
        print(f"  ⚠️  Conductor skill generation failed: {exc}")

    # 2d. Offer to install missing capability packages for Claude Code (if using CC provider)
    _maybe_sync_cc_plugins()

    if agent_only:
        print("\n🐾 Done! Try: kiroclaw gateway")
        return

    # 3. Slack credentials
    _setup_slack_tokens()

    # 3b. Slash command name
    _setup_slash_command()

    # 4. Timezone
    _setup_timezone()

    # 5. Dashboard URL (remote access)
    _maybe_setup_dashboard_url()

    _maybe_setup_custom_domain()

    # ── Browser (Playwright MCP) ──
    print("\n── Browser (Playwright MCP) ──")

    if is_playwright_installed():
        print("  Playwright MCP already installed")
    else:
        print("  Installing Playwright MCP...")
        try:
            ensure_playwright_installed()
            print("  Playwright MCP installed")
        except Exception as exc:
            print(f"  Playwright install failed: {exc}")
            print("  Browser features will be unavailable until Playwright is installed")

    # Always regenerate config and register proxy in mcp.json (preserve extension mode)
    try:
        generate_playwright_config()
        refresh_storage_state()
        if has_playwright_extension():
            token = get_extension_token()
            if token:
                patch_mcp_extension(token)
            else:
                patch_mcp_headless()
        else:
            patch_mcp_headless()
        print("  Browser proxy registered in mcp.json")
    except Exception:
        pass  # Non-fatal: browser still works without pre-loaded cookies

    print("\n🐾 Done! Try: kiroclaw doctor && kiroclaw gateway")


def _setup_workspace_dir() -> None:
    """Prompt user for workspace directory, falling back to platform default."""
    platform_default = _default_workspace_base() / _WORKSPACE_DIR_NAME
    default = platform_default
    label = "Default"
    if _workspace_dir_file().is_file():
        configured = _workspace_dir_file().read_text(encoding="utf-8").strip()
        if configured:
            default = Path(configured)
            label = "Configured"
    print("── Workspace Directory ──\n")
    print("  LLM sessions and task output are stored in a workspace directory.")
    print(f"  {label}: {default}\n")
    answer = input(f"  Workspace path [{default}]: ").strip()
    chosen = default if answer.lower() in ("", "y", "yes") else Path(answer).expanduser()
    try:
        chosen.mkdir(parents=True, exist_ok=True)
        _workspace_dir_file().parent.mkdir(parents=True, exist_ok=True)
        _workspace_dir_file().write_text(str(chosen) + "\n", encoding="utf-8")
        print(f"  ✅ Workspace: {chosen}\n")
    except OSError as e:
        print(f"  ❌ Cannot create {chosen}: {e}")
        print(f"  Falling back to platform default: {platform_default}\n")


def _setup_slack_tokens() -> None:
    """Prompt for Slack tokens and owner ID, write to config_dir/.env."""
    cred_path = env_path()
    existing: dict[str, str] = {}
    if cred_path.exists():
        for line in cred_path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    print("── Slack Credentials ──\n")
    print("  See SLACK_SETUP.md for how to create a Slack app.\n")

    answer = input("  Configure Slack tokens? [Y/n]: ").strip().lower()
    if answer in ("n", "no"):
        print("  ⏭  Skipped. Slack integration will be disabled.\n")
        return

    def _mask(val: str) -> str:
        return val[:8] + "…" if len(val) > 12 else val

    cur_app = existing.get(CRED_SLACK_APP_TOKEN, "")
    cur_bot = existing.get(CRED_SLACK_BOT_TOKEN, "")
    cur_owner = existing.get(CRED_OWNER_ID, "")

    hint_app = f" [{_mask(cur_app)}]" if cur_app else ""
    hint_bot = f" [{_mask(cur_bot)}]" if cur_bot else ""
    hint_owner = f" [{cur_owner}]" if cur_owner else ""

    app_token = input(f"  App Token (xapp-...){hint_app}: ").strip() or cur_app
    bot_token = input(f"  Bot Token (xoxb-...){hint_bot}: ").strip() or cur_bot
    owner_id = input(f"  Your Slack Member ID{hint_owner}: ").strip() or cur_owner

    if not app_token or not bot_token:
        print("  ⚠️  Missing tokens — Slack integration will be disabled.\n")
        return

    # Preserve any extra keys already in .env
    existing[CRED_SLACK_APP_TOKEN] = app_token
    existing[CRED_SLACK_BOT_TOKEN] = bot_token
    if owner_id:
        existing[CRED_OWNER_ID] = owner_id

    cred_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in existing.items()]
    cred_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    cred_path.chmod(0o600)
    print(f"  ✅ Credentials saved to {cred_path}\n")


_CUSTOM_DOMAIN = "kiroclaw.localhost"


def _detect_system_timezone() -> str:
    """Return IANA tz name from TZ env var or /etc/localtime symlink, or empty string."""
    tz_env = os.environ.get("TZ", "").lstrip(":")
    if tz_env and not tz_env.startswith("/"):
        return tz_env
    try:
        p = Path("/etc/localtime")
        if p.is_symlink():
            target = str(p.resolve())
            if "zoneinfo/" in target:
                return target.split("zoneinfo/", 1)[1]
    except Exception:
        pass
    return ""


def _setup_slash_command() -> None:
    """Prompt for custom slash command name, save to config.json."""
    cfg_file = config_path()
    cfg: dict = {}
    if cfg_file.exists():
        try:
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ⚠️  Could not read {cfg_file}: {exc}")
            return

    print("── Slash Command ──\n")
    current = cfg.get("slack", {}).get("command", "kiroclaw")
    raw = input(f"  Slash command name [{current}]: ").strip()
    if raw:
        raw = raw.lstrip("/").strip()
    if not raw:
        raw = current
    if not all(c.isalnum() or c in "-_" for c in raw):
        print("  ⚠️  Command name should only contain letters, numbers, hyphens, or underscores.")
        raw = current
    if len(raw) > 32:
        print("  ⚠️  Command name too long (max 32 chars).")
        raw = current

    cfg.setdefault("slack", {})["command"] = raw
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    print(f"  ✅ Slash command: /{raw}\n")


def _setup_timezone() -> None:
    """Auto-detect timezone and save to config.json."""
    cfg_file = config_path()

    # Check if already configured
    data: dict = {}
    if cfg_file.exists():
        try:
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"  ⚠️  Could not read {cfg_file}: {exc}")
            return
    current = data.get("timezone", "")

    # Auto-detect from system
    detected = _detect_system_timezone()

    print("── Timezone ──\n")
    if current:
        print(f"  Current: {current}")
        answer = input(f"  Timezone [{current}]: ").strip()
        if not answer:
            print(f"  ✅ Keeping: {current}\n")
            return
        tz_val = answer
    elif detected:
        print(f"  Detected: {detected}")
        answer = input(f"  Timezone [{detected}]: ").strip()
        tz_val = answer or detected
    else:
        tz_val = input("  IANA timezone (e.g. America/Los_Angeles): ").strip()
        if not tz_val:
            print("  ⏭  Skipped. Cron schedules will show UTC.\n")
            return

    # Validate with retry
    abbrev_to_iana: dict[str, str] = {
        "PST": "America/Los_Angeles", "PDT": "America/Los_Angeles",
        "MST": "America/Denver", "MDT": "America/Denver",
        "CST": "America/Chicago", "CDT": "America/Chicago",
        "EST": "America/New_York", "EDT": "America/New_York",
        "GMT": "Etc/GMT", "BST": "Europe/London",
        "CET": "Europe/Berlin", "CEST": "Europe/Berlin",
        "IST": "Asia/Kolkata", "JST": "Asia/Tokyo",
        "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
        "NZST": "Pacific/Auckland", "NZDT": "Pacific/Auckland",
    }
    max_retries = 3
    for attempt in range(max_retries):
        try:
            ZoneInfo(tz_val)
            break  # valid
        except (KeyError, Exception):
            suggestion = abbrev_to_iana.get(tz_val.upper())
            if suggestion:
                print(f"  ❌ '{tz_val}' is an abbreviation, not an IANA timezone.")
                print(f"     Did you mean: {suggestion}?")
            else:
                print(f"  ❌ Unknown timezone '{tz_val}'.")
                print("     Use IANA format, e.g. America/Los_Angeles, Europe/London")
            if attempt < max_retries - 1:
                tz_val = input("  Timezone: ").strip()
                if not tz_val:
                    print("  ⏭  Skipped.\n")
                    return
            else:
                print("  ⏭  Skipped after too many attempts.\n")
                return

    data["timezone"] = tz_val
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cfg_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"  ✅ Timezone saved: {tz_val}\n")


def _maybe_setup_dashboard_url() -> None:
    """Prompt for dashboard.url when running on a remote host with Slack configured."""

    cfg_file = config_path()
    cfg = KiroClawConfig.load()
    creds = cfg.load_credentials()
    has_slack = bool(creds.get("SLACK_APP_TOKEN") and creds.get("SLACK_BOT_TOKEN"))

    if not has_slack:
        return  # No Slack → local-only, no URL needed

    # Detect if this looks like a remote host
    try:
        ip = socket.gethostbyname(socket.gethostname())
        is_remote = not ip.startswith("127.")
    except OSError:
        is_remote = False

    if not is_remote and not cfg.dashboard.url:
        return  # Localhost machine with no existing URL config — skip

    current = cfg.dashboard.url
    hostname = socket.gethostname()

    print("── Dashboard URL (remote access) ──\n")
    if is_remote:
        print(f"  This host ({hostname}) appears to be a remote machine.")
        print("  Setting a dashboard URL enables direct browser access with token auth.")
        print("  Leave blank for localhost-only (SSH tunnel required).\n")
    else:
        print("  Configure a custom dashboard URL for remote access.")
        print("  Leave blank for localhost-only.\n")

    hint = f" [{current}]" if current else ""
    answer = input(f"  Dashboard URL (e.g. http://{hostname}:{DASHBOARD_PORT}){hint}: ").strip()

    if answer == "" and current:
        print(f"  ✅ Keeping: {current}\n")
        return
    if answer == "" and not current:
        print("  ⏭  Skipped. Dashboard will bind to localhost only.\n")
        return

    # Persist to config.json
    try:
        data: dict = {}
        if cfg_file.exists():
            data = json.loads(cfg_file.read_text(encoding="utf-8"))
        dashboard = data.setdefault("dashboard", {})
        dashboard["url"] = answer
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        cfg_file.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        print(f"  ✅ Dashboard URL saved: {answer}")
        print("  Token auth will be required for all requests.\n")
    except Exception as e:
        print(f"  ❌ Failed to save: {e}\n")


def _maybe_setup_custom_domain() -> None:
    """Inform user about kiroclaw.localhost and clean up legacy mesh.claw from /etc/hosts."""
    print("\n── Custom Domain ──\n")
    print(f"  Dashboard available at http://{_CUSTOM_DOMAIN}:{DASHBOARD_PORT}")
    print("  (*.localhost resolves to 127.0.0.1 per RFC 6761 — no /etc/hosts edit needed)\n")

    # Advise removal of legacy "mesh.claw" entry from /etc/hosts if present
    try:
        if "mesh.claw" in Path("/etc/hosts").read_text(encoding="utf-8"):
            print("  ⚠  Legacy mesh.claw entry found in /etc/hosts.")
            print(
                "  To remove it: sudo grep -v 'mesh\\.claw' /etc/hosts > /tmp/hosts.clean"
                " && sudo mv /tmp/hosts.clean /etc/hosts\n"
            )
    except Exception:
        pass


def _maybe_sync_cc_plugins() -> None:
    """Prompt user to install missing capability packages for Claude Code.

    Only activates when the configured provider is ``claude_code``.
    Does NOT auto-install — prompts the user for opt-in. In the public build
    the package source is optional, so this is typically a no-op.
    """
    try:
        cfg = KiroClawConfig.load()
    except Exception:
        return
    if cfg.agent.provider != "claude_code":
        return

    try:
        missing = installed_kiro_packages_missing_from_cc()
    except Exception:
        return

    if not missing:
        return

    print("\n── Claude Code Capability Parity ──\n")
    print(f"  Found {len(missing)} capability package(s) not yet installed for Claude Code:")
    for pkg in missing:
        print(f"    - {pkg}")

    try:
        answer = input(f"\n  Install {len(missing)} package(s) for Claude Code? [Y/n] ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Skipped.")
        return

    if answer.lower() in ("n", "no"):
        print("  Skipped. Run `kiroclaw aim sync-cc` later to install.")
        return

    successes = 0
    for pkg in missing:
        print(f"  Installing {pkg}...", end=" ", flush=True)
        ok, msg = install_cc_plugin(pkg, standalone=True)
        if ok:
            print("done")
            successes += 1
        else:
            print(f"FAILED: {msg}")

    print(f"  ✅ {successes}/{len(missing)} packages installed for Claude Code")
