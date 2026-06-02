"""Shared helpers used across handler submodules."""

from __future__ import annotations

import fnmatch
import json
import logging
import os
from pathlib import Path
from typing import Any

from kiro_claw.dashboard.state import DashboardState
from kiro_claw.security import is_sensitive_path
from kiro_claw.skills import skills_dir

logger = logging.getLogger(__name__)


def _get_memory(state: DashboardState):
    """Get MemoryStore from context_builder, or create standalone."""
    if state.context_builder:
        return state.context_builder.memory
    # Fallback: create standalone MemoryStore
    if not hasattr(state, "_standalone_memory"):
        from kiro_claw.memory import MemoryStore

        mem = MemoryStore()
        mem.init()
        state._standalone_memory = mem  # type: ignore[attr-defined]
    return state._standalone_memory  # type: ignore[attr-defined]


def _get_active_workspace(state: DashboardState) -> str:
    """Return the workspace of the most recently active chat slot, or 'default'."""
    slots = getattr(state, "_slots", {})
    if slots:
        # Pick the slot with the most messages (most active)
        best = max(slots.values(), key=lambda s: s.total_messages, default=None)
        if best and best.workspace and best.workspace != "default":
            return best.workspace
    return "default"


def _get_lessons(state: DashboardState, workspace: str | None = None):
    """Get LessonStore for a workspace. Falls back to global."""
    ws = workspace or _get_active_workspace(state)
    if ws != "default" and state.context_builder:
        return state.context_builder.get_lessons_for(ws)
    return state.lessons


def _get_skills(state: DashboardState):
    """Get SkillsLoader from context_builder, or create standalone."""
    if state.context_builder:
        return state.context_builder.skills
    if not hasattr(state, "_standalone_skills"):
        from kiro_claw.skills import SkillsLoader

        skills = SkillsLoader(install_builtins=False)
        state._standalone_skills = skills  # type: ignore[attr-defined]
    return state._standalone_skills  # type: ignore[attr-defined]


def _resolve_aim_skill_path(name: str) -> Path | None:
    """Find SKILL.md for an AIM skill by name (supports nested paths)."""
    aim_dir = Path.home() / ".aim"
    for pattern in (f"skills/*/{name}/SKILL.md", f"packages/*/skills/*/{name}/SKILL.md"):
        for p in aim_dir.glob(pattern):
            return p
    return None


async def _list_aim_skills() -> list[dict[str, Any]]:
    """List skills from AIM using ``aim skills list`` CLI."""
    import asyncio
    import re

    try:
        proc = await asyncio.create_subprocess_exec(
            "aim", "skills", "list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            return []
    except FileNotFoundError:
        return []
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        return []

    result: list[dict[str, Any]] = []
    pkg = ""
    for line in stdout.decode(errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Package header: non-indented line (no leading spaces)
        if not line.startswith(" ") and not line.startswith("Installed"):
            pkg = stripped
            continue
        # Description line (check BEFORE skill line — both match `word: text`)
        dm = re.match(r"^\s+Description:\s+(.+)", line)
        if dm and result and result[-1]["package"] == pkg:
            result[-1]["description"] = dm.group(1)
            continue
        # Skill line: "  name [vX.Y.Z]: display-name" or "  name: display-name"
        m = re.match(r"^\s+(\S+)(?:\s+\[v[\d.]+\])?:\s+(.+)", line)
        if m:
            name = m.group(1)
            resolved = _resolve_aim_skill_path(name)
            result.append(
                {
                    "key": f"aim/{name}",
                    "name": name,
                    "description": "",
                    "path": str(resolved) if resolved else "",
                    "dir": str(resolved.parent) if resolved else "",
                    "always": False,
                    "source": "aim",
                    "package": pkg,
                }
            )

    return result


# ── Kiro-cli native skills (~/.kiro/skills/, <project>/.kiro/skills/) ──


# Maximum SKILL.md content we'll read just to extract frontmatter description.
_KIRO_SKILL_FRONTMATTER_BYTES = 4096


def _kiro_skill_roots(project_dir: Path | None = None) -> list[tuple[str, Path]]:
    """Return ``(label, path)`` pairs for the open-standard skill locations.

    label is one of: ``kiro-user``, ``kiro-workspace``.  Used as the
    ``source`` field on listed skills so the UI can show provenance.
    """
    out: list[tuple[str, Path]] = []
    user_dir = Path.home() / ".kiro" / "skills"
    if user_dir.is_dir() and not is_sensitive_path(str(user_dir)):
        out.append(("kiro-user", user_dir))
    if project_dir:
        ws_dir = project_dir / ".kiro" / "skills"
        if ws_dir.is_dir() and not is_sensitive_path(str(ws_dir)):
            out.append(("kiro-workspace", ws_dir))
    return out


def _parse_skill_description(skill_md: Path) -> tuple[str, bool]:
    """Cheap frontmatter parse — return (description, always)."""
    # Gate on the resolved target before reading: a SKILL.md inside an
    # otherwise-trusted skills root may itself be a symlink to a sensitive
    # credential file (e.g. ~/.kiro/skills/evil/SKILL.md → ~/.aws/credentials).
    # Checking the root dir is not enough — individual files must be checked.
    try:
        resolved_md = skill_md.resolve(strict=True)
    except OSError:
        return "", False
    if is_sensitive_path(str(resolved_md)):
        return "", False
    try:
        with resolved_md.open("rb") as f:
            head = f.read(_KIRO_SKILL_FRONTMATTER_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return "", False
    if not head.startswith("---"):
        return "", False
    end = head.find("\n---", 3)
    if end < 0:
        return "", False
    desc = ""
    always = False
    for line in head[3:end].splitlines():
        line = line.strip()
        if line.startswith("description:"):
            desc = line.split(":", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("always:"):
            val = line.split(":", 1)[1].strip().lower()
            always = val == "true"
    return desc, always


def list_kiro_skills(project_dir: Path | None = None) -> list[dict[str, Any]]:
    """List skills from kiro-cli's open-standard locations.

    Each entry has the same shape as a SkillsLoader entry plus a
    ``source`` of ``kiro-user`` or ``kiro-workspace``.  Read-only —
    edits are not routed back here (kiro-cli owns these directories).
    """
    out: list[dict[str, Any]] = []
    for source, root in _kiro_skill_roots(project_dir):
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name.startswith("."):
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            desc, always = _parse_skill_description(skill_md)
            out.append({
                "key": f"{source}/{entry.name}",
                "name": entry.name,
                "description": desc,
                "path": str(skill_md),
                "dir": str(entry),
                "always": always,
                "source": source,
            })
    return out


# ── loaded_by_agents resolution ──


def _agent_dirs() -> list[Path]:
    """Return existing agent JSON directories (global + workspace)."""
    out: list[Path] = []
    user = Path.home() / ".kiro" / "agents"
    if user.is_dir():
        out.append(user)
    return out


def _expand_resource_uri(uri: str, agent_path: Path) -> str | None:
    """Strip ``skill://`` prefix and resolve ``~`` and workspace-relative paths.

    kiro-cli accepts both ``skill://~/.kiro/skills/*/SKILL.md`` (global)
    and ``skill://.kiro/skills/*/SKILL.md`` (workspace, relative to the
    process cwd at session start).  We resolve workspace-relative globs
    relative to the agent file's directory's ancestor chain — best-effort
    since we don't know the exact cwd kiro-cli will use.

    Returns a glob pattern usable with fnmatch, or None if not a skill URI.
    """
    if not uri.startswith("skill://"):
        return None
    raw = uri[len("skill://") :]
    if raw.startswith("~/"):
        raw = str(Path.home() / raw[2:])
    elif raw.startswith("/"):
        pass  # absolute
    else:
        # Workspace-relative (e.g. ``.kiro/skills/*/SKILL.md``): resolve
        # against the project root.  For the typical layout
        # ``<project>/.kiro/agents/foo.json`` the project root is three
        # levels up (foo.json → agents → .kiro → <project>); appending the
        # ``.kiro/...``-prefixed glob then yields ``<project>/.kiro/...``.
        # Going only two levels up would double the ``.kiro`` segment.
        # Best-effort — the cwd kiro-cli uses at session start may differ.
        candidate = agent_path.parent.parent.parent / raw
        raw = str(candidate)
    return raw


def _agent_loads_skill(agent_json: dict[str, Any], agent_path: Path, skill_md: Path) -> bool:
    """Return True if *agent_json*'s ``resources`` would load *skill_md*."""
    resources = agent_json.get("resources") or []
    if not isinstance(resources, list):
        return False
    target = str(skill_md)
    for res in resources:
        if not isinstance(res, str):
            continue
        glob = _expand_resource_uri(res, agent_path)
        if glob and fnmatch.fnmatch(target, glob):
            return True
    return False


def _resolve_loaded_by_agents(skill_md: Path) -> list[str]:
    """Return list of agent names whose ``resources`` glob matches *skill_md*.

    Best-effort: parses every agent JSON, ignores ones that fail to parse.
    Empty list means no agent loads this skill via ``skill://`` URIs (it
    may still be loaded via KiroClaw text-injection or an external MCP server).
    """
    out: list[str] = []
    for agents_dir in _agent_dirs():
        for agent_path in sorted(agents_dir.glob("*.json")):
            # Resolve and gate on sensitive paths before reading — a symlink
            # under ~/.kiro/agents/ could otherwise point at a credential
            # file (e.g. ~/.aws/credentials renamed *.json).  All file reads
            # must honor is_sensitive_path().
            try:
                resolved = agent_path.resolve(strict=True)
            except OSError:
                continue
            if is_sensitive_path(str(resolved)):
                continue
            try:
                data = json.loads(resolved.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            if _agent_loads_skill(data, agent_path, skill_md):
                name = data.get("name") or agent_path.stem
                out.append(str(name))
    return out


# ── Skill directory browser (tree + file content) ──


# Hard caps to keep the API responsive and bounded.
SKILL_TREE_MAX_ENTRIES = 500
SKILL_FILE_MAX_BYTES = 1_048_576  # 1 MiB


def _resolve_skill_root(name: str, state: DashboardState) -> Path | None:
    """Return the absolute skill directory for *name*, or None.

    Accepts the same nested-name scheme used by the existing skill API:
    - ``foo`` → ``~/.kiroclaw/skills/foo``
    - ``utils/tiny-url`` → ``~/.kiroclaw/skills/utils/tiny-url``
    - ``aim/<skill>`` → resolved via _list_aim_skills() lookup
    - ``kiro-user/<skill>`` → ``~/.kiro/skills/<skill>``
    - ``kiro-workspace/<skill>`` → ``<project>/.kiro/skills/<skill>``

    The returned path is always under one of the allowed roots — paths
    that try to escape via ``..`` or symlinks are rejected.
    """
    if not name or ".." in name or name.startswith("/"):
        return None
    if name.startswith("kiro-user/"):
        rel = name[len("kiro-user/") :]
        root = Path.home() / ".kiro" / "skills"
    elif name.startswith("kiro-workspace/"):
        rel = name[len("kiro-workspace/") :]
        # We cannot reliably resolve project dir here — gate this to
        # paths under any active slot's project_dir.
        proj: Path | None = None
        for slot in getattr(state, "_slots", {}).values():
            pd = getattr(slot, "project_dir", None)
            if pd:
                proj = Path(pd)
                break
        if proj is None:
            return None
        root = proj / ".kiro" / "skills"
    elif name.startswith("aim/"):
        # Locate via existing helper (sync version).
        aim_name = name[len("aim/") :]
        path = _resolve_aim_skill_path(aim_name)
        if not path:
            return None
        candidate = path.parent
        if is_sensitive_path(str(candidate)):
            return None
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        # Re-check the *resolved* target — a symlink within the AIM path could
        # point at a sensitive location that the unresolved check missed
        # (consistent with the kiroclaw/kiro branches below).
        if is_sensitive_path(str(resolved)):
            return None
        return resolved
    else:
        # ``kiroclaw`` skills live under the active config home, which honors
        # KIROCLAW_HOME (e.g. isolated dev gateways).  Hardcoding
        # ``~/.kiroclaw`` here would 404 every skill in a KIROCLAW_HOME-isolated
        # deployment even though SkillsLoader (the GET /api/skills source)
        # resolves them correctly.
        rel = name
        root = skills_dir()
    if not rel or ".." in rel:
        return None
    candidate = root / rel
    if not candidate.is_dir():
        return None
    if is_sensitive_path(str(candidate)):
        return None
    try:
        resolved = candidate.resolve(strict=True)
    except OSError:
        return None
    # Containment + symlink policy.  Skills can be nested under category
    # directories (``utils/multi-badger`` → ``<root>/utils/multi-badger``),
    # and a skill directory itself may be a symlink (AIM ``--local`` installs
    # symlink ``~/.kiro/skills/<name>`` to ``~/.agents/skills/<name>``).  We
    # therefore require the candidate's *parent* directory to resolve to a
    # location at or under the trusted root — which permits the leaf to be a
    # symlink while still rejecting a symlinked *intermediate* directory that
    # would let ``a/b`` escape the tree.  The resolved target is then checked
    # against the sensitive-path list as a final guard.
    try:
        parent_resolved = candidate.parent.resolve(strict=True)
        root_resolved = root.resolve(strict=True)
    except OSError:
        return None
    if parent_resolved != root_resolved and root_resolved not in parent_resolved.parents:
        return None
    if is_sensitive_path(str(resolved)):
        return None
    return resolved


def list_skill_tree(skill_root: Path) -> list[dict[str, Any]]:
    """Return a flat list of files under *skill_root*, capped at SKILL_TREE_MAX_ENTRIES.

    Each entry: ``{path: relative-from-root, type: "file"|"dir", size: int}``.
    Sensitive paths are filtered out.  Symlinks are resolved; entries whose
    real path escapes *skill_root* are omitted.
    """
    out: list[dict[str, Any]] = []
    skipped = 0
    for dirpath, dirnames, filenames in os.walk(skill_root, followlinks=False):
        # Stable order — reproducible across runs / tests.
        dirnames.sort()
        filenames.sort()
        for d in list(dirnames):
            full = Path(dirpath) / d
            if is_sensitive_path(str(full)):
                dirnames.remove(d)
                continue
            rel = full.relative_to(skill_root).as_posix()
            out.append({"path": rel, "type": "dir", "size": 0})
            if len(out) >= SKILL_TREE_MAX_ENTRIES:
                return out
        for f in filenames:
            full = Path(dirpath) / f
            if is_sensitive_path(str(full)):
                skipped += 1
                continue
            try:
                if full.is_symlink():
                    real = full.resolve(strict=True)
                    real.relative_to(skill_root.resolve(strict=True))
                    if is_sensitive_path(str(real)):
                        skipped += 1
                        continue
                stat = full.stat()
            except (OSError, ValueError):
                skipped += 1
                continue
            rel = full.relative_to(skill_root).as_posix()
            out.append({"path": rel, "type": "file", "size": int(stat.st_size)})
            if len(out) >= SKILL_TREE_MAX_ENTRIES:
                return out
    return out


def read_skill_file(skill_root: Path, rel_path: str) -> tuple[str, str | None]:
    """Read ``skill_root/rel_path`` with safety + size guards.

    Returns ``(content, error)``.  ``error`` is non-empty when access is
    denied, the file is too big, or it doesn't exist.
    """
    if not rel_path or ".." in rel_path.split("/") or rel_path.startswith("/"):
        return "", "invalid path"
    target = skill_root / rel_path
    try:
        resolved = target.resolve(strict=True)
        skill_resolved = skill_root.resolve(strict=True)
        resolved.relative_to(skill_resolved)
    except (OSError, ValueError):
        return "", "not found"
    if is_sensitive_path(str(resolved)):
        return "", "access denied"
    if not resolved.is_file():
        return "", "not a file"
    try:
        size = resolved.stat().st_size
    except OSError:
        return "", "stat failed"
    if size > SKILL_FILE_MAX_BYTES:
        return "", f"file too large ({size} bytes; cap {SKILL_FILE_MAX_BYTES})"
    try:
        return resolved.read_text(encoding="utf-8", errors="replace"), None
    except OSError:
        return "", "read failed"


def _is_restricted_session(state: DashboardState, request: "Any") -> bool:
    """Check if request comes from an ephemeral (incognito) or temporary (guest) session.

    Reads X-Session-Key header (set by browser and MCP subprocesses).
    Returns True if the session should be blocked from memory operations.
    """
    sk = request.headers.get("X-Session-Key", "")
    if not sk:
        return False
    if sk == "dashboard:ui":
        return False
    if sk in state._restricted_keys:
        return True
    slot_name = sk.split(":", 1)[-1] if ":" in sk else sk
    slot = state._slots.get(slot_name)
    if slot and slot.is_restricted:
        return True
    if sk.startswith("slack:"):
        from kiro_claw.slack.handler import is_thread_incognito, is_thread_temporary

        if is_thread_temporary(sk) or is_thread_incognito(sk):
            return True
    return False


def _blocks_reads_session(state: DashboardState, request: "Any") -> bool:
    """Check if request comes from a temporary session that blocks memory reads."""
    sk = request.headers.get("X-Session-Key", "")
    if not sk or sk == "dashboard:ui":
        return False
    slot_name = sk.split(":", 1)[-1] if ":" in sk else sk
    slot = state._slots.get(slot_name)
    if slot and slot.blocks_reads:
        return True
    if sk.startswith("slack:"):
        from kiro_claw.slack.handler import is_thread_temporary

        if is_thread_temporary(sk):
            return True
    return False


def _session_has_persisted_history(slot_name: str) -> bool:
    """Return True iff the slot has a JSONL file in ~/.kiroclaw/sessions/.

    This is a positive signal that the session was previously established
    as non-ephemeral: ephemeral (incognito/temporary) sessions never write
    to disk, so a persisted JSONL can only come from a real user session.

    Used by ``api_lessons_create`` to distinguish between:

    * A legitimate MCP subprocess whose in-memory slot was evicted by the
      idle-sweep loop (``session.py``'s 30-minute timeout). The subprocess
      still holds the original ``KIROCLAW_SESSION_KEY`` env var, so it
      keeps sending the same ``X-Session-Key``, but ``state._slots`` has
      moved on. Without this check such calls return HTTP 400 ``unknown
      session`` even though the user is actively typing in the thread.

    * A forged or stale key from a context that never had a real session
      backing it — which should continue to be rejected.

    Only checks existence, not contents. Authentication of the caller is
    still enforced by the ``X-Internal-Secret`` middleware upstream; this
    check only governs the *ephemeral vs non-ephemeral* distinction.
    """
    if (
        not slot_name
        or "/" in slot_name
        or "\\" in slot_name
        or "\x00" in slot_name
        or slot_name.startswith(".")
    ):
        # Defence-in-depth against path traversal; ``KIROCLAW_SESSION_KEY``
        # normally has no path separators, but ``X-Session-Key`` is
        # attacker-controlled in principle even behind the secret
        # middleware. Reject forward slash (Linux/macOS) and backslash
        # (Windows) path separators, null bytes that can truncate C-level
        # path parsing, and leading dots that could target hidden
        # per-directory files outside the intended session namespace.
        return False
    sess_dir = Path.home() / ".kiroclaw" / "sessions"
    if not sess_dir.exists():
        return False
    # Match the resolution order used by slack/interactions.py when
    # linking Slack threads to existing sessions: bare stem first, then
    # the ``dashboard_`` prefix fallback for dashboard slots.
    if (sess_dir / f"{slot_name}.jsonl").exists():
        return True
    if not slot_name.startswith("dashboard_") and (
        sess_dir / f"dashboard_{slot_name}.jsonl"
    ).exists():
        return True
    return False
