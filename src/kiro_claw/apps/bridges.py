"""Registration bridges — wire app resources into KiroClaw's runtime.

When an app is installed or enabled, its agents, skills, and cron jobs need
to be registered with KiroClaw's existing systems.  This module provides
``register_app`` and ``deregister_app`` which handle the namespacing and
symlink/copy operations.

Namespace convention: ``{app_name}/{resource_name}`` to avoid collisions
between apps.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from kiro_claw.apps.cron_sdk import CronSDK
from kiro_claw.apps.manager import app_dir, get_app, get_app_manifest
from kiro_claw.apps.manifest import AppManifest
from kiro_claw.atomic_write import atomic_write
from kiro_claw.config.loader import config_dir
from kiro_claw.sel import sel

logger = logging.getLogger(__name__)

# Where kiro-cli looks for agent definitions
KIRO_AGENTS_DIR = Path.home() / ".kiro" / "agents"

# Where KiroClaw loads skills from
SKILLS_DIR_NAME = "skills"


def _skills_dir() -> Path:
    return config_dir() / SKILLS_DIR_NAME


def _namespace(app_name: str, resource_name: str) -> str:
    """Build a namespaced resource name: ``app_name/resource_name``."""
    return f"{app_name}/{resource_name}"


def _safe_link_name(namespaced: str) -> str:
    """Convert ``app/resource`` to a safe filename for symlinks: ``app--resource``."""
    return namespaced.replace("/", "--")


# ---------------------------------------------------------------------------
# Agent registration
# ---------------------------------------------------------------------------

def _register_agents(app_name: str, manifest: AppManifest, app_root: Path) -> list[str]:
    """Symlink app agent JSONs into ~/.kiro/agents/ with namespaced names.

    Returns list of registered agent names (namespaced).
    """
    registered: list[str] = []
    KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)

    for agent_path_str in manifest.agents:
        agent_path = app_root / agent_path_str
        # Path containment check — reject paths that escape the app root
        if not agent_path.resolve().is_relative_to(app_root.resolve()):
            logger.warning("App %s: agent path escapes app root: %s", app_name, agent_path)
            continue
        if not agent_path.is_file():
            logger.warning("App %s: agent file not found: %s", app_name, agent_path)
            continue

        # Read agent JSON to get the agent name
        try:
            agent_data = json.loads(agent_path.read_text(encoding="utf-8"))
            agent_name = agent_data.get("name", agent_path.stem)
        except (json.JSONDecodeError, OSError):
            agent_name = agent_path.stem

        # Namespaced link name: app-name--agent-name.json
        link_name = _safe_link_name(_namespace(app_name, agent_name)) + ".json"
        link_path = KIRO_AGENTS_DIR / link_name

        # Remove existing link if present
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()

        try:
            os.symlink(str(agent_path), str(link_path))
            registered.append(_namespace(app_name, agent_name))
            logger.info("Registered agent: %s -> %s", link_name, agent_path)
        except OSError as exc:
            logger.warning("Failed to symlink agent %s: %s", link_name, exc)

    return registered


def _deregister_agents(app_name: str) -> int:
    """Remove all agent symlinks for an app from ~/.kiro/agents/."""
    prefix = _safe_link_name(app_name + "/")
    removed = 0
    if not KIRO_AGENTS_DIR.is_dir():
        return 0
    for entry in KIRO_AGENTS_DIR.iterdir():
        if entry.name.startswith(prefix) and entry.name.endswith(".json"):
            try:
                entry.unlink()
                removed += 1
            except OSError:
                pass
    if removed:
        logger.info("Deregistered %d agent(s) for app %s", removed, app_name)
    return removed


# ---------------------------------------------------------------------------
# Skill registration
# ---------------------------------------------------------------------------

_RESERVED_SKILL_DIRS = {"auto"}


def _register_skills(app_name: str, manifest: AppManifest, app_root: Path) -> list[str]:
    """Symlink app skill directories into ~/.kiroclaw/skills/.

    Creates both a namespaced link (``skills/{app_name}/{skill_name}``) and a
    flat link (``skills/{skill_name}``) so the skill scanner finds the skill
    regardless of whether it walks subdirectories or only checks the top level.

    Returns list of registered skill names (namespaced).
    """
    registered: list[str] = []
    skills_root = _skills_dir()
    app_skills_dir = skills_root / app_name
    app_skills_dir.mkdir(parents=True, exist_ok=True)

    for skill_path_str in manifest.skills:
        skill_path = app_root / skill_path_str
        if not skill_path.resolve().is_relative_to(app_root.resolve()):
            logger.warning("App %s: skill path escapes app root: %s", app_name, skill_path)
            continue
        if not skill_path.is_dir():
            logger.warning("App %s: skill directory not found: %s", app_name, skill_path)
            continue

        skill_name = skill_path.name

        # Namespaced link: ~/.kiroclaw/skills/{app_name}/{skill_name}
        link_path = app_skills_dir / skill_name
        if link_path.exists() or link_path.is_symlink():
            if link_path.is_symlink():
                link_path.unlink()
            else:
                shutil.rmtree(link_path)

        # Flat link: ~/.kiroclaw/skills/{skill_name} (for skill scanner)
        if skill_name in _RESERVED_SKILL_DIRS:
            logger.info("App %s: skipping flat link for reserved name %s", app_name, skill_name)
            flat_link = None
        else:
            flat_link = skills_root / skill_name
            if flat_link.exists() or flat_link.is_symlink():
                if flat_link.is_symlink():
                    flat_link.unlink()
                else:
                    logger.info(
                        "App %s: skipping flat link for %s — non-symlink dir exists",
                        app_name, skill_name,
                    )
                    flat_link = None  # type: ignore[assignment]

        try:
            os.symlink(str(skill_path), str(link_path))
            if flat_link is not None:
                os.symlink(str(skill_path), str(flat_link))
            namespaced = _namespace(app_name, skill_name)
            registered.append(namespaced)
            logger.info("Registered skill: %s -> %s", namespaced, skill_path)
        except OSError as exc:
            logger.warning("Failed to symlink skill %s: %s", skill_name, exc)

    if registered:
        sel().log_tool_invocation(
            session_key="", agent="kiroclaw", source="app_bridge",
            tool_name="register_skills", tool_kind="permission_change",
            outcome="completed",
            resources=f"app={app_name} skills={registered}",
        )
    else:
        sel().log_tool_invocation(
            session_key="", agent="kiroclaw", source="app_bridge",
            tool_name="register_skills", tool_kind="permission_change",
            outcome="no_op",
            resources=f"app={app_name} skills=[]",
        )
    return registered


def _deregister_skills(app_name: str) -> int:
    """Remove the app's skill symlinks from ~/.kiroclaw/skills/."""
    skills_root = _skills_dir()
    app_skills_dir = skills_root / app_name
    if not app_skills_dir.exists():
        return 0
    try:
        removed_skills = [item.name for item in app_skills_dir.iterdir() if item.is_symlink()]
        for item in app_skills_dir.iterdir():
            if item.is_symlink():
                if item.name in _RESERVED_SKILL_DIRS:
                    continue
                target = item.resolve()
                flat_link = skills_root / item.name
                if flat_link.is_symlink() and flat_link.resolve() == target:
                    flat_link.unlink()
        shutil.rmtree(app_skills_dir)
        logger.info("Deregistered skills for app %s", app_name)
        sel().log_tool_invocation(
            session_key="", agent="kiroclaw", source="app_bridge",
            tool_name="deregister_skills", tool_kind="permission_change",
            outcome="completed",
            resources=f"app={app_name} skills={removed_skills}",
        )
        return 1
    except OSError:
        sel().log_tool_invocation(
            session_key="", agent="kiroclaw", source="app_bridge",
            tool_name="deregister_skills", tool_kind="permission_change",
            outcome="failed",
            resources=f"app={app_name}",
        )
        return 0


# ---------------------------------------------------------------------------
# Cron registration (deferred — writes a manifest for the CronService)
# ---------------------------------------------------------------------------

_CRON_MANIFEST_NAME = "app-crons.json"


def _app_crons_path(app_name: str) -> Path:
    """Path to the app's cron manifest within its install directory."""
    return app_dir(app_name) / _CRON_MANIFEST_NAME


def _register_crons(app_name: str, manifest: AppManifest) -> list[str]:
    """Write app cron definitions to a manifest file for later CronService pickup.

    The actual CronService registration happens at enable time via
    ``register_app_crons_with_service()``.  This just persists the
    definitions so they survive restarts.

    Returns list of namespaced cron names.
    """
    if not manifest.crons:
        return []

    cron_defs: list[dict[str, Any]] = []
    registered: list[str] = []
    for cron in manifest.crons:
        namespaced = _namespace(app_name, cron.name)
        cron_defs.append({
            "name": namespaced,
            "every": cron.every,
            "cron_expr": cron.cron_expr,
            "agent": cron.agent,
            "message": cron.message,
            "app": app_name,
            "agent_sequence": cron.agent_sequence,
            "env": cron.env,
            "persistent_session": cron.persistent_session,
            "silent": cron.silent,
        })
        registered.append(namespaced)

    path = _app_crons_path(app_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cron_defs, indent=2), encoding="utf-8")
    logger.info("Wrote %d cron definition(s) for app %s", len(cron_defs), app_name)
    return registered


def _deregister_crons(app_name: str) -> int:
    """Remove the app's cron manifest."""
    path = _app_crons_path(app_name)
    if path.is_file():
        path.unlink()
        logger.info("Removed cron manifest for app %s", app_name)
        return 1
    return 0


def load_app_cron_defs(app_name: str) -> list[dict[str, Any]]:
    """Load persisted cron definitions for an app (used by CronService bridge)."""
    path = _app_crons_path(app_name)
    if not path.is_file():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def register_app_crons_with_service(app_name: str, cron_service: Any) -> list[str]:
    """Promote persisted app cron defs into the running CronService.

    Reads ``app-crons.json`` and registers each job via :class:`CronSDK`,
    which tags ownership as ``created_by="app:{app_name}"``.

    Idempotent — jobs already present (by name) are skipped.
    """
    if cron_service is None:
        return []

    defs = load_app_cron_defs(app_name)
    if not defs:
        return []

    sdk = CronSDK(app_name, cron_service)
    existing_names = {j.name for j in sdk.list_jobs()}

    newly_registered: list[str] = []
    for d in defs:
        name = d.get("name", "")
        if not name or name in existing_names:
            continue
        try:
            sdk.add_job(
                name=name,
                message=d.get("message", ""),
                every_secs=d.get("every"),  # JSON "every" → Python "every_secs"
                cron_expr=d.get("cron_expr"),
                agent=d.get("agent") or "",
                agent_sequence=d.get("agent_sequence") or None,
                env=d.get("env") or None,
                persistent_session=d.get("persistent_session", False),
                silent=bool(d.get("silent", False)),
            )
            newly_registered.append(name)
        except Exception as exc:
            logger.warning(
                "App %s: failed to register cron %r (%s): %s",
                app_name, name, type(exc).__name__, exc,
            )
            sel().log_api_access(
                caller="app_bridge",
                operation="app_cron_add_job",
                outcome="failed",
                resources=f"app={app_name} cron={name}",
                error=str(exc),
            )

    if newly_registered:
        logger.info(
            "App %s: registered %d cron job(s) with scheduler: %s",
            app_name, len(newly_registered), ", ".join(newly_registered),
        )
    return newly_registered


def deregister_app_crons_from_service(app_name: str, cron_service: Any) -> int:
    """Remove app-owned cron jobs from the running CronService.

    Mirrors :func:`register_app_crons_with_service`. Uses :class:`CronSDK`,
    which only removes jobs tagged ``created_by="app:{app_name}"`` — other
    apps' jobs are unaffected.

    Idempotent — safe to call when no jobs are registered (returns ``0``).
    Returns the number of jobs removed.
    """
    if cron_service is None:
        return 0
    sdk = CronSDK(app_name, cron_service)
    try:
        return sdk.remove_all()
    except Exception as exc:
        logger.warning(
            "App %s: failed to remove crons from scheduler (%s): %s",
            app_name, type(exc).__name__, exc,
        )
        sel().log_api_access(
            caller="app_bridge",
            operation="app_crons_deregister",
            outcome="failed",
            resources=app_name,
            error=str(exc),
        )
        return 0


# ---------------------------------------------------------------------------
# MCP server registration
# ---------------------------------------------------------------------------

_MCP_JSON_PATH = Path.home() / ".kiro" / "settings" / "mcp.json"


@contextmanager
def _mcp_lock(*, exclusive: bool = True) -> Iterator[None]:
    """Acquire a lock on mcp.json for the duration of the block.

    Uses a single ``.lock`` sidecar file for both shared and exclusive
    locks so that readers and writers coordinate properly.
    """
    lock_path = _MCP_JSON_PATH.with_suffix(".lock")
    _MCP_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch(exist_ok=True)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    with open(lock_path, "r") as lf:
        fcntl.flock(lf, mode)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _read_mcp_json_unlocked() -> dict[str, Any]:
    """Read mcp.json without acquiring a lock (caller must hold lock)."""
    if not _MCP_JSON_PATH.is_file():
        return {}
    try:
        return json.loads(_MCP_JSON_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to read mcp.json: %s", exc)
        return {}


def _write_mcp_json_unlocked(data: dict[str, Any]) -> None:
    """Write mcp.json without acquiring a lock (caller must hold lock)."""
    _MCP_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(_MCP_JSON_PATH, json.dumps(data, indent=2) + "\n")


def _read_mcp_json() -> dict[str, Any]:
    """Read mcp.json with a shared lock."""
    with _mcp_lock(exclusive=False):
        return _read_mcp_json_unlocked()


def _register_mcp_servers(app_name: str, manifest: AppManifest) -> list[str]:
    """Register app-provided MCP servers into global mcp.json.

    Uses ``{app_name}:{server_name}`` namespace to avoid collisions.
    """
    if not manifest.mcpServers:
        return []
    registered: list[str] = []
    with _mcp_lock():
        mcp_data = _read_mcp_json_unlocked()
        servers = mcp_data.setdefault("mcpServers", {})
        for server_name, server_config in manifest.mcpServers.items():
            namespaced = f"{app_name}:{server_name}"
            servers[namespaced] = server_config
            registered.append(namespaced)
        _write_mcp_json_unlocked(mcp_data)
    logger.info("Registered %d MCP server(s) for app %s", len(registered), app_name)
    return registered


def _deregister_mcp_servers(app_name: str) -> int:
    """Remove app MCP servers from global mcp.json."""
    prefix = f"{app_name}:"
    with _mcp_lock():
        mcp_data = _read_mcp_json_unlocked()
        servers = mcp_data.get("mcpServers", {})
        to_remove = [k for k in servers if k.startswith(prefix)]
        for k in to_remove:
            del servers[k]
        if to_remove:
            _write_mcp_json_unlocked(mcp_data)
    if to_remove:
        logger.info("Deregistered %d MCP server(s) for app %s", len(to_remove), app_name)
    return len(to_remove)


# ---------------------------------------------------------------------------
# Top-level register / deregister
# ---------------------------------------------------------------------------

@dataclass
class RegistrationResult:
    """Summary of what was registered/deregistered for an app."""

    agents: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    crons: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agents": self.agents,
            "skills": self.skills,
            "crons": self.crons,
            "mcp_servers": self.mcp_servers,
            "errors": self.errors,
        }


def register_app(app_name: str) -> RegistrationResult:
    """Register all resources for an installed app.

    Reads the app's manifest from its install directory and creates
    symlinks/manifests for agents, skills, crons, and MCP servers.

    Apps with ``resources="app"`` manage their own resource registration
    (agents, skills, MCP servers via SDK).  Bridge registration is skipped
    entirely to avoid creating duplicates that confuse kiro-cli.
    """
    result = RegistrationResult()
    manifest = get_app_manifest(app_name)
    if not manifest:
        result.errors.append(f"app {app_name!r} not found or has invalid manifest")
        return result

    # Self-managed apps handle their own registration — skip all bridge work.
    info = get_app(app_name)
    if info and info.get("resources") == "app":
        logger.debug(
            "Skipping bridge registration for %s (resources=app)", app_name,
        )
        return result

    app_root = app_dir(app_name)

    try:
        result.agents = _register_agents(app_name, manifest, app_root)
    except Exception as exc:
        result.errors.append(f"agent registration failed: {exc}")

    try:
        result.skills = _register_skills(app_name, manifest, app_root)
    except Exception as exc:
        result.errors.append(f"skill registration failed: {exc}")

    try:
        result.crons = _register_crons(app_name, manifest)
    except Exception as exc:
        result.errors.append(f"cron registration failed: {exc}")

    try:
        result.mcp_servers = _register_mcp_servers(app_name, manifest)
    except Exception as exc:
        result.errors.append(f"MCP server registration failed: {exc}")

    logger.info(
        "Registered app %s: %d agents, %d skills, %d crons, %d mcp, %d errors",
        app_name, len(result.agents), len(result.skills),
        len(result.crons), len(result.mcp_servers), len(result.errors),
    )
    return result


def deregister_app(app_name: str) -> RegistrationResult:
    """Deregister all resources for an app.

    Removes symlinks and cron manifests.  Does not remove the app directory.
    """
    result = RegistrationResult()

    try:
        n = _deregister_agents(app_name)
        result.agents = [f"removed {n} agent(s)"]
    except Exception as exc:
        result.errors.append(f"agent deregistration failed: {exc}")

    try:
        _deregister_skills(app_name)
        result.skills = ["removed"]
    except Exception as exc:
        result.errors.append(f"skill deregistration failed: {exc}")

    try:
        _deregister_crons(app_name)
        result.crons = ["removed"]
    except Exception as exc:
        result.errors.append(f"cron deregistration failed: {exc}")

    try:
        n = _deregister_mcp_servers(app_name)
        result.mcp_servers = [f"removed {n} MCP server(s)"]
    except Exception as exc:
        result.errors.append(f"MCP server deregistration failed: {exc}")

    logger.info("Deregistered app %s", app_name)
    return result
