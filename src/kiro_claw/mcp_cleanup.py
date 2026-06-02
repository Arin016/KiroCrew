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
from pathlib import Path

logger = logging.getLogger(__name__)

_KIRO_MCP_JSON = Path.home() / ".kiro" / "settings" / "mcp.json"

# Managed servers whose command is the kiroclaw binary itself.
# Only these are affected by install-method path changes.
KIROCLAW_BIN_MCP_SERVERS = frozenset({"kiroclaw-cron", "kiroclaw-core"})


def clean_stale_managed_mcp() -> list[str]:
    """Remove KiroClaw-binary MCP entries from ``~/.kiro/settings/mcp.json``.

    Legacy cleanup invoked only during explicit migration paths to wipe
    kiroclaw-cron/kiroclaw-core entries left over from an older install
    method that wrote them to the global provider config.  KiroClaw no
    longer writes to the global config under any normal code path, so
    this is NOT called on startup — touching the user's global mcp.json
    unconditionally would violate the "KiroClaw owns only the agent
    file" boundary.

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
    removed = [n for n in KIROCLAW_BIN_MCP_SERVERS if n in servers]
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
