"""Agent discovery — scans ~/.kiro/agents/ for installed agents.

Provides ``list_agents()`` which returns metadata about all installed
agents, including KiroClaw's own agent and any agents shipped by
locally-installed skill packages (agent config files on disk).

Also provides CC (Claude Code) plugin discovery helpers:
- ``list_cc_plugins()`` — installed CC plugin package names (reads disk)
- ``is_cc_plugin_installed()`` — check a single package
- ``install_cc_plugin()`` — no-op in OSS (the optional plugin CLI is absent)
- ``installed_kiro_packages_missing_from_cc()`` — empty in OSS

The optional ``aim`` plugin manager is not part of the public distribution,
so the install/sync helpers degrade gracefully to no-ops when its binary is
absent. ``list_agents()`` remains fully functional — it only reads on-disk
agent config files and has no external-tool dependency.

Each agent is identified by its ``modeId`` — the value passed to
``session/set_mode`` in the ACP protocol to switch the backend's behavior.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kiro_claw.security import is_sensitive_path

logger = logging.getLogger(__name__)

_KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"
_CC_PLUGINS_DIR = Path.home() / ".aim" / "cc-plugins"

_VALID_PACKAGE_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9/_.-]*$")

# AIM packages whose agents are treated as kiroclaw-owned (orange badge, not purple)
_KIROCLAW_AIM_PACKAGES = {"KiroClawAICapabilities"}


@dataclass
class AimAgent:
    """Metadata for an installed kiro-cli agent."""

    name: str
    filename: str
    description: str
    model: str
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    source: str = "builtin"  # "aim" | "kiroclaw" | "builtin"
    package: str = ""  # AIM package name (e.g. "Customer360GenAIContext")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _extract_skills(data: dict[str, Any]) -> list[str]:
    """Extract skill names from builder-mcp args (--skill-name-filter)."""
    bm = data.get("mcpServers", {}).get("builder-mcp", {})
    args = bm.get("args", [])
    skills: list[str] = []
    for i, arg in enumerate(args):
        if arg == "--skill-name-filter" and i + 1 < len(args):
            skills.extend(s.strip() for s in args[i + 1].split(",") if s.strip())
    return skills


def list_agents(agents_dir: Path | None = None) -> list[AimAgent]:
    """Scan ~/.kiro/agents/*.json for all installed agents.

    Returns a list of ``AimAgent`` objects sorted by name. Each agent
    corresponds to a kiro-cli agent config file that can be selected
    via ``session/set_mode`` in the ACP protocol.
    """
    d = agents_dir or _KIRO_AGENTS_DIR
    if not d.is_dir():
        return []

    agents: list[AimAgent] = []
    for f in sorted(d.glob("*.json")):
        # Skip macOS AppleDouble sidecars ("._foo.json"); not JSON.
        if f.name.startswith("._"):
            continue
        # Resolve and gate on sensitive paths before reading: a symlink
        # under ~/.kiro/agents/ could otherwise point at a credential file
        # (e.g. ~/.aws/credentials renamed *.json).
        try:
            resolved = f.resolve(strict=True)
        except OSError:
            continue
        if is_sensitive_path(str(resolved)):
            continue
        try:
            data = json.loads(resolved.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            # Determine source: kiroclaw's own, AIM-installed, or builtin
            # AIM pattern: filename contains the agent name from JSON data
            #   Remote: PackageName-agent-name.json
            #   Local:  local-PackageName-agent-name.json
            agent_name = data.get("name", "")
            stem = f.stem

            # Extract AIM package from filename first (needed for source detection):
            #   Remote: PackageName-agent-name.json
            #   Local:  local-PackageName-agent-name.json
            package = ""
            is_aim_filename = agent_name and stem.endswith(agent_name) and stem != agent_name
            if is_aim_filename:
                pkg_stem = f.stem
                if pkg_stem.startswith("local-"):
                    pkg_stem = pkg_stem[len("local-") :]
                package = pkg_stem[: -(len(agent_name) + 1)]

            # Determine source
            if f.name in ("kiroclaw.json", "kiroclaw-lite.json"):
                source = "kiroclaw"
            elif is_aim_filename:
                source = "kiroclaw" if package in _KIROCLAW_AIM_PACKAGES else "aim"
            else:
                source = "builtin"

            agents.append(
                AimAgent(
                    name=data.get("name", f.stem),
                    filename=f.name,
                    description=data.get("description", ""),
                    model=data.get("model", "auto"),
                    skills=_extract_skills(data),
                    mcp_servers=list(data.get("mcpServers", {}).keys()),
                    source=source,
                    package=package,
                )
            )
        except (OSError, ValueError):
            # ValueError covers json.JSONDecodeError + UnicodeDecodeError
            # so one non-UTF-8 file can't break the whole agent list.
            logger.debug("Skipping invalid agent config: %s", f)
            continue

    # Deduplicate by name — prefer AIM-installed (has package) over fallback
    seen: dict[str, AimAgent] = {}
    for a in agents:
        existing = seen.get(a.name)
        if existing is None:
            seen[a.name] = a
        elif a.package and not existing.package:
            seen[a.name] = a
        elif a.package and existing.package:
            logger.warning(
                "Duplicate agent name '%s' from packages '%s' and '%s'; keeping '%s'",
                a.name,
                existing.package,
                a.package,
                existing.package,
            )
    return list(seen.values())


# ---------------------------------------------------------------------------
# Claude Code plugin discovery and installation
# ---------------------------------------------------------------------------


def list_cc_plugins() -> list[str]:
    """Return package names of installed CC plugins from the AIM marketplace.

    Reads ``~/.aim/cc-plugins/.claude-plugin/marketplace.json`` if it exists.
    Returns an empty list if AIM is not installed or the file is missing.
    """
    marketplace = _CC_PLUGINS_DIR / ".claude-plugin" / "marketplace.json"
    if not marketplace.is_file():
        return []
    try:
        data = json.loads(marketplace.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.debug("Cannot read CC plugins marketplace.json")
        return []
    # marketplace.json is an array of objects with a "packageName" field
    if isinstance(data, list):
        return [
            entry["packageName"]
            for entry in data
            if isinstance(entry, dict) and entry.get("packageName")
        ]
    # Alternate format: dict with "plugins" key
    if isinstance(data, dict):
        plugins = data.get("plugins", [])
        if isinstance(plugins, list):
            return [
                entry["packageName"]
                for entry in plugins
                if isinstance(entry, dict) and entry.get("packageName")
            ]
    return []


def is_cc_plugin_installed(pkg: str) -> bool:
    """Check if a specific AIM package is installed as a CC plugin."""
    return pkg in list_cc_plugins()


def _ensure_standalone_mode() -> bool:
    """No-op in OSS — the optional plugin manager config is not managed here.

    Preserved for API compatibility. Always returns True; writes nothing.
    """
    return True


def install_cc_plugin(pkg: str, *, standalone: bool = True) -> tuple[bool, str]:
    """Install a package as a CC plugin (no-op in this distribution).

    The optional plugin manager used to perform installs is not part of the
    public distribution, so this degrades to a graceful no-op rather than
    shelling out to an absent binary.

    Args:
        pkg: Package name (validated for safety, otherwise unused).
        standalone: Accepted for API compatibility; ignored.

    Returns:
        (success, message) tuple. ``success`` is always False here.
    """
    if not _VALID_PACKAGE_RE.match(pkg) or ".." in pkg:
        return False, f"Invalid package name: {pkg!r}"
    return False, "Plugin install is not available in this distribution"


def _list_kiro_packages() -> set[str]:
    """Return installed plugin-manager package names — empty in OSS.

    The optional plugin manager CLI is absent in the public distribution, so
    there are no externally-tracked packages to enumerate. Returns an empty
    set (no subprocess spawned).
    """
    return set()


def installed_kiro_packages_missing_from_cc() -> list[str]:
    """Return packages installed for the agent backend but missing from CC.

    With no external plugin manager in the public distribution there is
    nothing to diff, so this always returns an empty list.
    """
    return []
