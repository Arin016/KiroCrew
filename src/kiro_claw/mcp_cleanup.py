"""Shared MCP config cleanup utilities.

KiroClaw does NOT write KiroClaw-managed MCP servers to the user's global
provider MCP config (``~/.kiro/settings/mcp.json``) during normal
operation — the KiroClaw agent file is authoritative, and provider
globals are user-owned.  Remaining helpers here clean up stale
kiroclaw-binary entries left over from older install methods.

Extracted from agent.py so both agent.py and cli.py can import at the
top level without circular dependencies (agent.py imports cli.py).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_KIRO_MCP_JSON = Path.home() / ".kiro" / "settings" / "mcp.json"

# Managed servers whose command is the kiroclaw binary itself.
# Only these are affected by install-method path changes.
KIROCLAW_BIN_MCP_SERVERS = frozenset({"kiroclaw-cron", "kiroclaw-core"})

# MeshClaw was the predecessor of KiroClaw. The rename left these managed
# server entries — pointing at now-dead MeshClaw build paths — behind in the
# user's global provider config. They are unambiguously stale and safe to purge.
PREDECESSOR_BIN_MCP_SERVERS = frozenset({"meshclaw-cron", "meshclaw-core"})

# Every managed-binary server name KiroClaw is responsible for removing from
# the user's global mcp.json (KiroClaw never legitimately writes these there).
STALE_MANAGED_MCP_SERVERS = KIROCLAW_BIN_MCP_SERVERS | PREDECESSOR_BIN_MCP_SERVERS


def _invokes_meshclaw(spec: object) -> bool:
    """True if a server spec's command is the dead MeshClaw predecessor binary.

    Catches stale entries the rename left behind whose *name* isn't in the
    managed set — e.g. a leftover ``npm:@playwright/mcp`` proxy pointing at an
    old MeshClaw runtime (``.../MeshClaw/.../bin/meshclaw``). Keyed on the
    command basename so it matches both bare ``meshclaw`` and absolute paths,
    and never matches a genuine playwright server (which runs ``npx``/``node``).
    """
    if not isinstance(spec, dict):
        return False
    cmd = spec.get("command", "")
    return isinstance(cmd, str) and bool(cmd) and os.path.basename(cmd) == "meshclaw"


def clean_stale_managed_mcp() -> list[str]:
    """Remove stale managed-binary MCP entries from ``~/.kiro/settings/mcp.json``.

    Runs from explicit setup (``kiroclaw setup``) and once on first gateway
    start (marker-guarded by ``run_first_run_setup``) — never on every startup,
    which would violate the "KiroClaw owns only the agent file" boundary.

    Removes two classes of stale entry left in the user's global provider
    config; genuine user-installed servers are never touched:

    * **By name** — ``kiroclaw-cron`` / ``kiroclaw-core`` (written there by an
      older install method; KiroClaw now keeps these in the agent file) and the
      predecessor ``meshclaw-cron`` / ``meshclaw-core``.
    * **By command** — any server whose command is the dead MeshClaw predecessor
      binary (basename ``meshclaw``), e.g. a leftover ``npm:@playwright/mcp``
      proxy entry pointing at an old MeshClaw runtime.

    Returns names of removed servers (empty list on no-op or error).
    """
    if not _KIRO_MCP_JSON.is_file():
        return []
    try:
        data = json.loads(_KIRO_MCP_JSON.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    servers = data.get("mcpServers", {})
    if not isinstance(servers, dict):
        return []
    removed = sorted(
        name
        for name, spec in servers.items()
        if name in STALE_MANAGED_MCP_SERVERS or _invokes_meshclaw(spec)
    )
    if not removed:
        return []
    for name in removed:
        del servers[name]
    try:
        _KIRO_MCP_JSON.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info("Removed stale managed MCP entries from kiro mcp.json: %s", removed)
    except OSError:
        logger.debug("Could not clean kiro mcp.json", exc_info=True)
        return []
    return removed
