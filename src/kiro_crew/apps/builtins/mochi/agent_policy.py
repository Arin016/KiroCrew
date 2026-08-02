"""Mochi's MCP reach policy — what its agents may and may not call.

kiro-cli loads every server in the GLOBAL ``~/.kiro/settings/mcp.json`` into an
agent regardless of that agent's own config; there is no "off" switch. The only
way to keep a server out of an agent's reach is to re-declare it in the agent's
own ``mcpServers`` with its tools disabled. The original standalone Mochi did
exactly this with hand-maintained tool lists per server.

This module produces the same effect from live data instead of hardcoded lists:

* ``mochi.extraMcpServers`` (what the user turned on in Settings -> MCP) becomes
  the GRANT list, with per-server ``autoApprove`` / ``disabledTools`` and
  per-agent scoping (``chat`` -> the foreground agent, ``bg`` -> the background
  one).
* every other ambient server becomes a NEUTRALIZE entry carrying its real tool
  names, discovered from the MCP probe cache.

**Fail-closed on unknown tools.** A neutralize entry with an empty tool list
would be worse than useless — it declares the server and disables nothing, i.e.
it reads like a deny but behaves like an allow. When a server's tools are not
known yet, it is recorded under ``pendingNeutralize`` (auditable, and retried
next time the policy is rebuilt) instead of being emitted as a hollow deny.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.apps.builtins.mochi.soul_loader import rendered_bg_prompt_path, rendered_prompt_path
from kiro_crew.atomic_write import atomic_write

logger = logging.getLogger(__name__)

#: Filename the framework reads from the app's data dir. Must match
#: ``kiro_crew.apps.bridges.AGENT_MCP_POLICY_FILE``.
POLICY_FILENAME = "agent_mcp_policy.json"

#: The two agents this app ships. ``chat`` / ``bg`` are the audience labels the
#: user-facing settings use; these are the real agent names they map to.
CHAT_AGENT = "mochi"
BG_AGENT = "mochi-bg"
_AUDIENCE_TO_AGENT = {"chat": CHAT_AGENT, "bg": BG_AGENT}

#: Never neutralize the app's own server: it is the pet's whole reason to exist,
#: and it is declared by the manifest rather than by the user.
_OWN_SERVER_PREFIX = "mochi"


def policy_path(data_dir: Path) -> Path:
    return data_dir / POLICY_FILENAME


def _normalise_entries(raw: Any) -> list[dict[str, Any]]:
    """Accept both wire shapes for ``extraMcpServers``.

    Older settings stored plain strings ("just enable this server"); the current
    UI stores objects with per-server policy. Both must keep working — a user's
    stored settings are not migrated on read.
    """
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if isinstance(item, str) and item:
            out.append({"name": item})
        elif isinstance(item, dict) and isinstance(item.get("name"), str):
            out.append(item)
    return out


def _ambient_servers() -> dict[str, list[str]]:
    """Ambient server name -> known tool names, from the MCP probe cache.

    Returns ``{}`` rather than raising: a policy that cannot be built must not
    take the app down, and the caller treats an empty map as "nothing to
    neutralize this round".
    """
    try:
        from kiro_crew.mcp_discovery import list_servers
    except Exception as exc:  # noqa: BLE001 — optional dependency of policy build
        logger.warning("Mochi policy: MCP discovery unavailable: %s", exc)
        return {}
    try:
        servers = list_servers()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Mochi policy: cannot list MCP servers: %s", exc)
        return {}
    return {s.name: list(getattr(s, "tools", []) or []) for s in servers}


def build_policy(settings: dict[str, Any], data_dir: Path | None = None) -> dict[str, Any]:
    """Compute the policy document from Mochi's settings + live MCP discovery.

    When *data_dir* is given, each agent also gets its system prompt pinned to the
    rendered prompt file in that directory. The prompt is GENERATED (it carries the
    user's pet name and the persona of the chosen appearance), so the path can only
    be stated at runtime — the packaged agent template cannot name it.
    """
    entries = _normalise_entries(settings.get("extraMcpServers"))
    ambient = _ambient_servers()

    granted: dict[str, dict[str, dict[str, Any]]] = {CHAT_AGENT: {}, BG_AGENT: {}}
    for entry in entries:
        name = entry["name"]
        audiences = entry.get("agents") or ["chat"]
        spec = {
            "autoApprove": list(entry.get("autoApprove") or []),
            "disabledTools": list(entry.get("disabledTools") or []),
        }
        for audience in audiences:
            agent = _AUDIENCE_TO_AGENT.get(str(audience))
            if agent is not None:
                granted[agent][name] = dict(spec)

    agents: dict[str, dict[str, Any]] = {}
    for agent, servers in granted.items():
        neutralize: dict[str, list[str]] = {}
        pending: list[str] = []
        for name, tools in ambient.items():
            if name in servers:
                continue
            if name == _OWN_SERVER_PREFIX or name.startswith(_OWN_SERVER_PREFIX + ":"):
                continue
            if tools:
                neutralize[name] = tools
            else:
                pending.append(name)
        agents[agent] = {
            "servers": servers,
            "neutralize": neutralize,
            # Audit trail, not enforcement: an empty disabledTools list would
            # read as a deny while behaving as an allow, so these are recorded
            # instead of emitted. Rebuilt (and usually resolved) once the
            # server has been probed.
            "pendingNeutralize": sorted(pending),
        }

    if data_dir is not None:
        # Per-agent, NOT one shared document. The background agent is a spawned
        # subagent with a different tool set and a different output contract;
        # pointing it at the chat prompt told it to spawn subagents, save
        # lessons, and reply in plain text — none of which it can do.
        prompts = {
            CHAT_AGENT: rendered_prompt_path(data_dir),
            BG_AGENT: rendered_bg_prompt_path(data_dir),
        }
        for agent in agents:
            path = prompts.get(agent)
            if path is not None:
                agents[agent]["prompt"] = f"file://{path}"

    return {"version": 1, "agents": agents}


def write_policy(data_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Persist the policy where the framework's agent materializer reads it."""
    policy = build_policy(settings, data_dir)
    atomic_write(policy_path(data_dir), json.dumps(policy, indent=2) + "\n")
    return policy


def apply_policy(data_dir: Path, settings: dict[str, Any]) -> dict[str, Any]:
    """Write the policy and re-materialize the app's agent configs.

    Best-effort by design: a failure here must not fail the settings save the
    user just made, so it is logged and the policy still lands on disk (the
    gateway's startup reconcile picks it up on the next boot).
    """
    policy = write_policy(data_dir, settings)
    try:
        from kiro_crew.apps.bridges import refresh_app_agents

        refreshed = refresh_app_agents("mochi")
        logger.info("Mochi MCP policy applied to %d agent config(s)", len(refreshed))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Mochi MCP policy written but agents not refreshed: %s", exc)
    return policy
