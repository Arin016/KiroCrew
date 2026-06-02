"""Mirror ~/.kiro/ configuration to ~/.claude/ for kiro-to-cc provider switching.

Translates kiro agent specs, MCP server config, and skills so users can
switch the KiroClaw provider from ``kiro`` to ``claude_code`` without
losing their customizations.

Usage:
    from kiro_claw.mirror import mirror_kiro_to_cc
    result = mirror_kiro_to_cc(dry_run=True)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Any

from kiro_claw.atomic_write import atomic_write
from kiro_claw.cc_agent import generate_cc_agent_markdown, generate_mcp_json
from kiro_claw.hooks import safe_read_file
from kiro_claw.security import is_sensitive_path

logger = logging.getLogger(__name__)

# Directories are resolved at call time via _kiro_home() / _claude_home()
# so tests can monkeypatch Path.home().

_AIM_SKIP_PREFIXES = ("aim-",)
_AIM_SUBDIR = "aim"

# Kiro → Claude Code tool name mapping (mirrors cc_agent._KIRO_TO_CC_TOOL_NAME).
_KIRO_TO_CC_TOOL: dict[str, str] = {
    "fs_read": "Read",
    "fs_write": "Write",
    "execute_bash": "Bash",
    "shell": "Bash",
    "glob": "Glob",
    "grep": "Grep",
    "code": "Edit",
}

# Subdirectory under .claude/hooks/ where mirrored hook scripts live.
# Using a dedicated subdirectory avoids trampling user-owned hook scripts.
_MIRROR_HOOKS_SUBDIR = ".mirror"

# Sidecar metadata tracking sha256 of mirror-copied scripts.
_MIRROR_META_FILENAME = ".mirror.meta.json"


def _sha256(path: Path) -> str:
    """Compute sha256 hex digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_mirror_meta(cc_hooks_dir: Path) -> dict[str, str]:
    """Read the mirror metadata JSON from the .mirror subdirectory.

    Returns a dict mapping relative script path -> sha256 hex digest.
    """
    meta_path = cc_hooks_dir / _MIRROR_HOOKS_SUBDIR / _MIRROR_META_FILENAME
    try:
        if meta_path.is_file():
            return json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _write_mirror_meta(cc_hooks_dir: Path, meta: dict[str, str]) -> None:
    """Write the mirror metadata JSON to the .mirror subdirectory."""
    mirror_dir = cc_hooks_dir / _MIRROR_HOOKS_SUBDIR
    mirror_dir.mkdir(parents=True, exist_ok=True)
    meta_path = mirror_dir / _MIRROR_META_FILENAME
    atomic_write(meta_path, json.dumps(meta, indent=2) + "\n", fsync=True)


def _is_under_hooks_dir(path: Path, hooks_dir: Path) -> bool:
    """Return True if *path* is strictly under *hooks_dir* (no symlink escape).

    Resolves both paths to detect symlinks that traverse outside the
    hooks directory boundary.
    """
    try:
        resolved_parent = hooks_dir.resolve(strict=True)
    except OSError:
        return False
    try:
        resolved_path = path.resolve(strict=True)
    except OSError:
        return False
    try:
        resolved_path.relative_to(resolved_parent)
        return True
    except ValueError:
        return False


def _extract_hook_commands(agent_data: dict[str, Any]) -> list[str]:
    """Extract all command strings from a kiro agent's hooks block."""
    hooks = agent_data.get("hooks", {})
    if not isinstance(hooks, dict):
        return []
    commands: list[str] = []
    for _event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                cmd = entry.get("command", "")
                if cmd:
                    commands.append(cmd)
    return commands


def _copy_hook_scripts(
    commands: list[str],
    *,
    kiro_root: Path,
    cc_root: Path,
    dry_run: bool = False,
) -> dict[str, str]:
    """Copy .kiro/hooks/<x> paths referenced in commands to .claude/hooks/.mirror/<x>.

    Returns a mapping {old_path: new_path} so the caller can rewrite commands.

    Path traversal defense: refuses any token that resolves to a real path
    outside ``kiro_root / "hooks"``. Does not follow symlinks out.
    Mode bits are preserved via shutil.copy2.
    """
    kiro_hooks_dir = kiro_root / "hooks"
    cc_hooks_dir = cc_root / "hooks"
    mirror_dir = cc_hooks_dir / _MIRROR_HOOKS_SUBDIR

    rename_map: dict[str, str] = {}

    # Build both the absolute and tilde-prefixed forms for matching
    kiro_hooks_abs = str(kiro_hooks_dir)
    kiro_hooks_tilde = str(Path("~") / ".kiro" / "hooks")

    # Read existing mirror metadata
    meta = _read_mirror_meta(cc_hooks_dir) if not dry_run else {}
    updated_meta = dict(meta)

    for command in commands:
        tokens = command.split()
        for token in tokens:
            if token.startswith("-"):
                continue

            # Detect if token references a path under the kiro hooks dir
            expanded = os.path.expanduser(token)

            is_kiro_hook = False
            if expanded.startswith(kiro_hooks_abs + "/") or expanded == kiro_hooks_abs:
                is_kiro_hook = True
            elif token.startswith(kiro_hooks_tilde + "/") or token == kiro_hooks_tilde:
                is_kiro_hook = True

            if not is_kiro_hook:
                continue

            token_path = Path(expanded)

            # Path traversal defense
            if kiro_hooks_dir.exists() and token_path.exists():
                if not _is_under_hooks_dir(token_path, kiro_hooks_dir):
                    logger.warning(
                        "mirror_hooks: refusing token %r — resolves outside hooks dir",
                        token,
                    )
                    continue

            # Compute relative path within hooks directory
            try:
                rel = token_path.relative_to(kiro_hooks_dir)
            except ValueError:
                continue

            dest_path = mirror_dir / rel

            # Build new path string preserving ~ prefix if original used it
            if token.startswith("~"):
                new_token = str(
                    Path("~") / ".claude" / "hooks" / _MIRROR_HOOKS_SUBDIR / rel
                )
            else:
                new_token = str(cc_hooks_dir / _MIRROR_HOOKS_SUBDIR / rel)

            if token in rename_map:
                continue

            if dry_run:
                # Preview only — report the intended rewrite without touching disk.
                rename_map[token] = new_token
                continue

            # Only rewrite the agent markdown to the mirrored path if the source
            # script actually exists and gets copied. Inserting into rename_map
            # unconditionally would make _rewrite_hook_commands point the hook at
            # a .claude/hooks/.mirror/ file that was never created — a broken
            # hook at runtime.
            if not token_path.is_file():
                logger.warning(
                    "mirror_hooks: source %s does not exist; leaving hook path unrewritten",
                    token_path,
                )
                continue

            # Defense-in-depth: never read a sensitive credential path. The
            # _is_under_hooks_dir resolve-check above already rejects symlinks
            # that escape ~/.kiro/hooks/, but that check is skipped when the
            # hooks dir is absent — so re-assert here right before we open the
            # file (mirrors the agent-prompt guard in _resolve_prompt at :302).
            if is_sensitive_path(str(token_path)):
                logger.warning(
                    "mirror_hooks: refusing token %r — resolves to a sensitive path",
                    token,
                )
                continue

            rename_map[token] = new_token
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            src_hash = _sha256(token_path)
            rel_key = str(rel)

            # Copy with mode preservation (atomic via copy2)
            shutil.copy2(str(token_path), str(dest_path))
            updated_meta[rel_key] = src_hash

    # Write updated metadata
    if not dry_run and updated_meta != meta:
        _write_mirror_meta(cc_hooks_dir, updated_meta)

    return rename_map


def _rewrite_hook_commands(
    md_content: str,
    rename_map: dict[str, str],
) -> str:
    """Rewrite hook command paths in agent markdown content.

    Uses str.replace for each old->new path mapping. Paths are absolute
    or ~/...-prefixed so partial-token collision is not a concern.
    """
    for old_path, new_path in rename_map.items():
        md_content = md_content.replace(old_path, new_path)
    return md_content


def _kiro_home() -> Path:
    """Return ~/.kiro/ path."""
    return Path.home() / ".kiro"


def _claude_home() -> Path:
    """Return ~/.claude/ path."""
    return Path.home() / ".claude"


def _is_aim_managed(path: Path) -> bool:
    """Return True if the agent file is AIM-managed and should be skipped."""
    if path.name.startswith(tuple(_AIM_SKIP_PREFIXES)):
        return True
    # Check if inside aim/ subdirectory
    try:
        rel = path.relative_to(path.parent.parent)
        parts = rel.parts
        if len(parts) > 1 and parts[0] == _AIM_SUBDIR:
            return True
    except ValueError:
        pass
    # Also check if any parent (excluding the agents root) is "aim"
    for parent in path.parents:
        if parent.name == _AIM_SUBDIR:
            return True
    return False


def _should_skip_agent(data: dict[str, Any]) -> bool:
    """Return True if the agent JSON has markers indicating internal use."""
    return bool(data.get("_kiroclaw_internal"))


def _resolve_prompt(agent_data: dict[str, Any], agents_dir: Path) -> dict[str, Any]:
    """Resolve file:// prompt URIs relative to the agents directory.

    Returns a copy of agent_data with the prompt field resolved to its
    content if it was a file:// URI pointing to a readable file.

    Refuses to read sensitive credential paths (~/.aws, ~/.ssh, ~/.gnupg, etc.)
    so a malicious or misconfigured agent JSON cannot exfiltrate secrets into
    the generated CC agent markdown.
    """
    prompt = agent_data.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.startswith("file://"):
        return agent_data

    uri_path = prompt[7:]
    # Resolve relative paths against the agents directory
    p = Path(uri_path)
    if not p.is_absolute():
        p = agents_dir / p
    p = p.resolve()

    if not p.is_file():
        return agent_data
    # Route through the centralized reader (hooks.safe_read_file), which
    # enforces is_sensitive_path() and raises PermissionError on a credential
    # path — keeps the sensitivity check in one place per the security-controls
    # guideline. A blocked or unreadable URI leaves the prompt unresolved so no
    # secret is inlined into the generated CC agent markdown.
    try:
        resolved = safe_read_file(str(p))
    except (OSError, PermissionError):
        logger.warning("Blocked or unreadable file:// URI: %s", p)
        return agent_data
    out = dict(agent_data)
    out["prompt"] = resolved
    return out


def _translate_auto_approve(entries: list[str]) -> list[str]:
    """Translate kiro autoApprove patterns to Claude Code permissions.allow format.

    Kiro uses tool name patterns (e.g. "mcp__kiroclaw-core__*") or bare
    kiro tool names (e.g. "fs_read").  Claude Code expects CC-native names
    (e.g. "Read") and ``mcp__<server>__<tool>`` glob patterns.

    Glob/regex patterns (containing ``*`` or ``?``) pass through unchanged
    since CC accepts them as-is.  Bare kiro tool names are translated via
    the canonical tool name mapping.
    """
    result: list[str] = []
    for entry in entries:
        if not entry or not isinstance(entry, str):
            continue
        # Patterns with wildcards pass through unchanged
        if "*" in entry or "?" in entry:
            result.append(entry)
        elif entry.startswith("@"):
            # @server-name → mcp__server-name
            result.append(f"mcp__{entry[1:]}")
        else:
            # Look up in the kiro→CC translation table; unknown names pass through
            result.append(_KIRO_TO_CC_TOOL.get(entry, entry))
    return result


def _merge_mcp_servers(
    existing_cc_json: dict[str, Any],
    new_servers: dict[str, Any],
) -> dict[str, Any]:
    """Merge new MCP servers into existing .claude.json without clobbering.

    Existing entries are preserved; new entries are added only if the key
    does not already exist.
    """
    merged = dict(existing_cc_json)
    existing_servers = merged.get("mcpServers", {})
    for name, spec in new_servers.get("mcpServers", {}).items():
        if name not in existing_servers:
            existing_servers[name] = spec
    merged["mcpServers"] = existing_servers
    return merged


def _merge_permissions(
    existing_settings: dict[str, Any],
    new_allow: list[str],
) -> dict[str, Any]:
    """Merge new permission entries into settings.local.json."""
    merged = dict(existing_settings)
    perms = merged.get("permissions", {})
    if not isinstance(perms, dict):
        perms = {}
    allow = perms.get("allow", [])
    if not isinstance(allow, list):
        allow = []
    # Add only entries not already present
    existing_set = set(allow)
    for entry in new_allow:
        if entry not in existing_set:
            allow.append(entry)
            existing_set.add(entry)
    perms["allow"] = allow
    merged["permissions"] = perms
    return merged


def _read_json_safe(path: Path) -> dict[str, Any] | None:
    """Read a JSON file, returning None on any error."""
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.debug("Failed to read %s: %s", path, exc)
    return None


def _safe_copytree(src: Path, dst: Path) -> None:
    """Copy a directory tree, skipping any entry whose resolved path is sensitive.

    ``shutil.copytree`` would follow a nested symlink (e.g. ``creds ->
    ~/.aws/credentials``) and exfiltrate it into ~/.claude/skills/. Walk the
    tree and apply ``is_sensitive_path`` per entry (mirrors the top-level guard
    in the skill-mirror loop), so a sensitive file anywhere in the subtree is
    never read or copied.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.rglob("*")):
        if is_sensitive_path(str(child.resolve())):
            logger.warning("mirror skill: refusing %s in subtree — sensitive path", child)
            continue
        rel = child.relative_to(src)
        dest_child = dst / rel
        if child.is_dir():
            dest_child.mkdir(parents=True, exist_ok=True)
        elif child.is_file():
            dest_child.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, dest_child)


def _rename_allowed_tools_in_skill(content: str) -> str:
    """Rename kiro-specific frontmatter keys in SKILL.md for CC compatibility.

    The open Agent Skills standard uses 'allowedTools' but some kiro skills
    use 'allowed-tools'. Normalize to what CC expects.
    """
    # Simple line-level replacement within frontmatter
    if not content.startswith("---"):
        return content
    match = re.match(r"^(---\n)(.*?\n)(---)", content, re.DOTALL)
    if not match:
        return content
    prefix = match.group(1)
    fm_body = match.group(2)
    suffix = match.group(3)
    rest = content[match.end():]
    # Replace 'allowed-tools:' with 'allowedTools:'
    fm_body = re.sub(r"^allowed-tools:", "allowedTools:", fm_body, flags=re.MULTILINE)
    return prefix + fm_body + suffix + rest


def mirror_kiro_to_cc(*, dry_run: bool = False, force: bool = False) -> dict[str, Any]:
    """Mirror ~/.kiro to ~/.claude. Returns summary of actions taken.

    Returns:
        {
            "agents": [{"name": ..., "action": "mirrored"|"skipped"|"skipped_aim"|...}],
            "mcp": [{"name": ..., "action": ...}],
            "skills": [{"name": ..., "action": ...}],
            "errors": [{"source": ..., "error": ...}],
        }
    """
    kiro = _kiro_home()
    claude = _claude_home()

    result: dict[str, Any] = {
        "agents": [],
        "mcp": [],
        "skills": [],
        "errors": [],
    }

    # ── 1. Mirror agents ──
    agents_dir = kiro / "agents"
    if agents_dir.is_dir():
        cc_agents_dir = claude / "agents"
        if not dry_run:
            cc_agents_dir.mkdir(parents=True, exist_ok=True)

        for agent_file in sorted(agents_dir.rglob("*.json")):
            name = agent_file.stem

            # Skip externally-managed agent specs (``aim-`` prefix or ``aim/``
            # subdir) — they are owned by an external manager, not by KiroClaw.
            if _is_aim_managed(agent_file):
                result["agents"].append({
                    "name": name,
                    "action": "skipped_aim",
                    "hint": "Externally-managed agent spec — not mirrored.",
                })
                continue

            # Read and parse
            try:
                data = json.loads(agent_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                result["errors"].append({"source": str(agent_file), "error": str(exc)})
                continue

            # Skip internal agents
            if _should_skip_agent(data):
                result["agents"].append({"name": name, "action": "skipped_internal"})
                continue

            # Resolve file:// prompt to inline content for the CC agent body.
            data = _resolve_prompt(data, agents_dir)
            resolved_prompt = data.get("prompt", "")

            # Generate CC agent markdown with the resolved prompt body.
            # Pass the resolved content explicitly so cc_agent does not
            # attempt a second file:// resolution with a stale relative path.
            md_content = generate_cc_agent_markdown(
                data, prompt_body=resolved_prompt if resolved_prompt else ""
            )

            # Copy hook scripts from .kiro/hooks/ to .claude/hooks/.mirror/
            # and rewrite command paths in the generated markdown.
            hook_commands = _extract_hook_commands(data)
            if hook_commands:
                rename_map = _copy_hook_scripts(
                    hook_commands,
                    kiro_root=kiro,
                    cc_root=claude,
                    dry_run=dry_run,
                )
                if rename_map:
                    md_content = _rewrite_hook_commands(md_content, rename_map)

            dest = cc_agents_dir / f"{name}.md"
            if dest.exists() and not force:
                result["agents"].append({"name": name, "action": "skipped_exists"})
                continue

            if not dry_run:
                atomic_write(dest, md_content, fsync=True)

            result["agents"].append({"name": name, "action": "mirrored"})
    else:
        result["agents"].append({"name": "(none)", "action": "no_agents_dir"})

    # ── 2. Mirror MCP servers ──
    mcp_json_path = kiro / "settings" / "mcp.json"
    if mcp_json_path.is_file():
        try:
            mcp_data = json.loads(mcp_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            result["errors"].append({"source": str(mcp_json_path), "error": str(exc)})
            mcp_data = None

        if mcp_data and isinstance(mcp_data, dict):
            # Filter out disabled entries
            servers = mcp_data.get("mcpServers", mcp_data)
            if not isinstance(servers, dict):
                servers = {}
            active_servers: dict[str, Any] = {}
            for srv_name, srv_spec in servers.items():
                if isinstance(srv_spec, dict) and srv_spec.get("disabled"):
                    result["mcp"].append({"name": srv_name, "action": "skipped_disabled"})
                    continue
                active_servers[srv_name] = srv_spec

            if active_servers:
                # Generate CC MCP config via the bridge
                cc_mcp, _, _ = generate_mcp_json({"mcpServers": active_servers})

                # Merge into ~/.claude.json (user-level MCP config — Claude Code
                # reads `mcpServers` from this file at the user scope; project
                # scope uses .mcp.json at repo root).
                cc_json_path = Path.home() / ".claude.json"
                existing = _read_json_safe(cc_json_path) or {}

                merged = _merge_mcp_servers(existing, cc_mcp)

                for srv_name in active_servers:
                    if srv_name in (existing.get("mcpServers") or {}):
                        result["mcp"].append({"name": srv_name, "action": "skipped_exists"})
                    else:
                        result["mcp"].append({"name": srv_name, "action": "mirrored"})

                if not dry_run:
                    atomic_write(cc_json_path, json.dumps(merged, indent=2) + "\n", fsync=True)

            # Handle autoApprove → permissions.allow
            auto_approve = mcp_data.get("autoApprove", [])
            if isinstance(auto_approve, list) and auto_approve:
                translated = _translate_auto_approve(auto_approve)
                if translated:
                    settings_path = claude / "settings.local.json"
                    existing_settings = _read_json_safe(settings_path) or {}
                    merged_settings = _merge_permissions(existing_settings, translated)

                    if not dry_run:
                        settings_path.parent.mkdir(parents=True, exist_ok=True)
                        atomic_write(
                            settings_path,
                            json.dumps(merged_settings, indent=2) + "\n",
                            fsync=True,
                        )

                    result["mcp"].append({
                        "name": "autoApprove",
                        "action": "mirrored_permissions",
                        "count": len(translated),
                    })
    else:
        result["mcp"].append({"name": "(none)", "action": "no_mcp_config"})

    # ── 3. Mirror skills ──
    # Skills live in ~/.kiro/skills/ or the project skills dir.
    # KiroClaw stores them in config_dir()/skills/ — check both.
    kiro_skills = kiro / "skills"
    if kiro_skills.is_dir():
        cc_skills_dir = claude / "skills"
        if not dry_run:
            cc_skills_dir.mkdir(parents=True, exist_ok=True)

        for dirpath, _dirs, files in os.walk(kiro_skills):
            if "SKILL.md" not in files:
                continue
            skill_file = Path(dirpath) / "SKILL.md"
            try:
                rel = skill_file.parent.relative_to(kiro_skills)
            except ValueError:
                continue
            skill_name = str(rel).replace("\\", "/")

            dest_dir = cc_skills_dir / rel
            dest_file = dest_dir / "SKILL.md"

            if dest_file.exists() and not force:
                result["skills"].append({"name": skill_name, "action": "skipped_exists"})
                continue

            if not dry_run:
                dest_dir.mkdir(parents=True, exist_ok=True)
                # Copy the entire skill directory (may include scripts/assets).
                # Only SKILL.md needs text transformation; everything else is
                # copied bytewise so binary assets (images, compiled files)
                # don't crash on UTF-8 decode.
                src_dir = skill_file.parent
                for item in src_dir.iterdir():
                    dest_item = dest_dir / item.name
                    # Defense-in-depth: a symlink under ~/.kiro/skills/ pointing
                    # at a credential path (e.g. SKILL.md -> ~/.aws/credentials)
                    # would otherwise exfiltrate secrets into ~/.claude/skills/.
                    # Skip any item whose resolved path is sensitive (mirrors the
                    # _copy_hook_scripts guard).
                    if is_sensitive_path(str(item.resolve())):
                        logger.warning(
                            "mirror skill: refusing %s — resolves to a sensitive path",
                            item,
                        )
                        continue
                    if item.is_file():
                        if item.name == "SKILL.md":
                            content = _rename_allowed_tools_in_skill(
                                safe_read_file(str(item))
                            )
                            atomic_write(dest_item, content)
                        else:
                            shutil.copy2(item, dest_item)
                    elif item.is_dir():
                        if dest_item.exists():
                            shutil.rmtree(dest_item)
                        # Per-entry sensitivity check on the whole subtree —
                        # plain shutil.copytree would follow a nested symlink to
                        # a credential path.
                        _safe_copytree(item, dest_item)

            result["skills"].append({"name": skill_name, "action": "mirrored"})
    else:
        result["skills"].append({"name": "(none)", "action": "no_skills_dir"})

    return result
