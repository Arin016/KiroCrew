"""Claude Code agent configuration bridge.

Translates KiroClaw's JSON agent config into Claude Code's agent format:
  - Agent Markdown with YAML frontmatter (~/.claude/agents/<name>.md)
  - .mcp.json for MCP server registration (ALL servers, not just kiroclaw's own)

Coexists with kiro agent config (agent.py). Does NOT modify kiro files.

Two consumers read the generated MCP set, and they take it via DIFFERENT
channels:
  - The legacy standalone ``claude`` CLI reads ``kiroclaw.mcp.json`` via the
    ``--mcp-config`` flag (providers/claude_code.py).
  - The default ``claude_code`` backend (claude-agent-acp adapter) does NOT
    read that file. Like kiro-cli, it accepts MCP servers as the ``mcpServers``
    parameter of the ``session/new`` / ``session/load`` JSON-RPC request. The
    spawn path (acp/client.py) reads ``kiroclaw.mcp.json`` and converts it via
    :func:`acp_servers_from_cc_map` into the ACP array passed in those params.

Either way we write ALL discovered servers (user-configured plus the bundled
kiroclaw-core/cron) so CC sees the same merged set the moment the user
switches to the CC provider.

Security deny patterns are installed globally to ~/.claude/settings.json under
permissions.deny rather than being bundled per-agent (which bloats frontmatter
and CLI args). Claude Code applies permissions.deny rules globally to ALL
sessions — see https://docs.anthropic.com/en/docs/claude-code/settings .
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from kiro_claw import model_registry

logger = logging.getLogger(__name__)

CC_AGENTS_DIR = Path.home() / ".claude" / "agents"
CC_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# The literal user-global Claude Code config root. This is the SEED SOURCE for
# the isolated dir (awsCredentialExport/env/model are copied FROM here) and the
# fallback root when isolation is disabled. Never compute the seed source via
# cc_config_root() — that points AT the isolated dir once isolation is on.
_USER_CC_ROOT = Path.home() / ".claude"


def cc_isolation_enabled() -> bool:
    """Whether the spawned claude-agent-acp subprocess gets an isolated config dir.

    Default ON. Set ``KIROCLAW_CC_ISOLATE=0`` (or ``false``/``no``) to disable —
    the subprocess then shares the user's global ``~/.claude`` (inheriting all
    enabled plugins/agents/skills, the token-bloat source). The opt-out exists so
    operators can fall back fast if isolation ever misbehaves in their env.
    """
    return os.environ.get("KIROCLAW_CC_ISOLATE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
    )


def _isolated_cc_config_dir() -> Path:
    """The KiroClaw-owned isolated Claude Code config dir: ``<config_dir>/cc-config``.

    Uses ``config.loader.config_dir()`` (not a hardcoded ``~/.kiroclaw``) so it
    honors ``KIROCLAW_HOME`` — dev instances keep CC config under
    ``.kiroclaw-dev`` rather than polluting the real one. Deterministic and
    recomputable with zero persisted state, which is mandatory: the subagent
    reaper (``subagent_persistence._cleanup_session_files_sync``) prunes CC
    transcripts post-restart with only ``(cwd, sid, provider)`` and no child env,
    so the root must be derivable from config alone.
    """
    # circular import: config.loader imports cc_agent (the _claude_code factory
    # calls cc_config_root/seed_isolated_cc_config), so a module-level import of
    # config.loader here would cycle (cc_agent → config.loader → cc_agent).
    # Deferred to call time to break it.
    from kiro_claw.config.loader import config_dir

    return config_dir() / "cc-config"


def cc_config_root() -> Path:
    """Single source of truth for the spawned CC subprocess's config root.

    Resolution order (must match the value injected into the child env so the
    host-side resume guard and cleanup target the SAME directory the SDK wrote
    transcripts to):

      1. ``CLAUDE_CONFIG_DIR`` env var, if set — test/operator override.
      2. ``<config_dir>/cc-config`` when isolation is enabled (the default).
      3. ``~/.claude`` otherwise (isolation disabled).
    """
    env_override = os.environ.get("CLAUDE_CONFIG_DIR")
    if env_override:
        return Path(env_override)
    if cc_isolation_enabled():
        return _isolated_cc_config_dir()
    return _USER_CC_ROOT


# Default CC model + the `availableModels` allowlist written to the isolated
# config dir. SOURCED FROM THE REGISTRY (model_registry) so there is a single
# source of truth — editing model_registry.json updates the isolated-dir seed,
# the per-session settings.local.json (acp/client.py), and the revert matcher
# together. A guard test (test_model_registry) asserts they stay in sync.
#
# Why the allowlist matters: the claude-agent-acp adapter builds
# session.modelInfos = (SDK model list) ∩ availableModels, then
# resolveModelPreference() can only return an entry IN that set. With the
# default ["opus","sonnet"] the explicit `[1m]` id fuzzy-collapses to
# the `opus` alias (200k). Listing the full versioned ids lets it match `[1m]`
# EXACTLY → the real 1M window. Full Bedrock inference-profile ids only.
_CC_DEFAULT_MODEL = model_registry.to_provider_id(
    model_registry.default("claude_code"), "claude_code"
)
_CC_AVAILABLE_MODELS: list[str] = model_registry.available_models("claude_code")

# ── Translation tables: kiro → Claude Code ──

_KIRO_TO_CC_HOOK_EVENT: dict[str, str] = {
    "preToolUse": "PreToolUse",
    "postToolUse": "PostToolUse",
    "userPromptSubmit": "UserPromptSubmit",
    "agentSpawn": "SessionStart",
    "stop": "Stop",
}

_KIRO_TO_CC_TOOL_NAME: dict[str, str] = {
    "fs_read": "Read",
    "fs_write": "Write",
    "execute_bash": "Bash",
    "shell": "Bash",
    "glob": "Glob",
    "grep": "Grep",
    "code": "Edit",
}

# Regex metacharacters that need escaping when converting globs to regex.
_GLOB_META_ESCAPE = re.compile(r"([.+^${}()|\\[\]])")


def _translate_matcher(glob_pattern: str) -> str:
    """Translate a kiro glob matcher to a CC regex matcher.

    Exact matches (no wildcards) pass through after tool-name rename.
    ``*`` becomes ``.*``, ``?`` becomes ``.``, regex metacharacters are
    escaped.
    """
    if not glob_pattern:
        return ""
    # Check if it is an exact match (no glob characters)
    if "*" not in glob_pattern and "?" not in glob_pattern:
        # Reuse the full tool-name translation so an @server matcher (e.g.
        # "@kiroclaw-core") becomes "mcp__kiroclaw-core" — a bare dict lookup
        # would leave it unchanged and the CC hook would never fire.
        return _translate_tool_name(glob_pattern)
    # Escape regex metacharacters first, then convert glob wildcards
    escaped = _GLOB_META_ESCAPE.sub(r"\\\1", glob_pattern)
    # Convert glob wildcards to regex equivalents
    escaped = escaped.replace("*", ".*")
    escaped = escaped.replace("?", ".")
    return escaped


def _translate_tool_name(kiro_name: str) -> str:
    """Translate a single kiro tool name to CC equivalent.

    For ``@server-name``, returns ``mcp__server-name``.
    For known kiro tools, returns the CC name.
    Unknown tools pass through unchanged (forward-compat).
    """
    if kiro_name.startswith("@"):
        return f"mcp__{kiro_name[1:]}"
    return _KIRO_TO_CC_TOOL_NAME.get(kiro_name, kiro_name)


def _translate_tool_list(kiro_tools: list[str]) -> list[str]:
    """Translate a list of kiro tool names to CC equivalents.

    Drops ``use_aws`` (no CC equivalent).
    """
    result: list[str] = []
    for t in kiro_tools:
        if t == "use_aws":
            continue
        result.append(_translate_tool_name(t))
    return result


def _kiroclaw_bin() -> str:
    """Resolve the ``kiroclaw`` executable for managed MCP server commands.

    Prefers ``agent._resolve_kiroclaw_bin`` — the robust resolver that walks up
    from the package install (venv / site-packages console-script), then PATH,
    validating each candidate is executable. This matters for newly installed
    users whose interactive PATH may not yet expose ``kiroclaw`` when the config
    is generated at gateway boot; a bare ``"kiroclaw"`` command would then fail
    to spawn kiroclaw-core/cron. Imported lazily because ``agent`` imports this
    module (module-level import would cycle). Falls back to ``shutil.which`` if
    the resolver is unavailable.
    """
    try:
        # circular import: agent imports cc_agent (this module), so importing
        # agent at module top would cycle — defer to call time.
        from kiro_claw.agent import _resolve_kiroclaw_bin

        resolved = _resolve_kiroclaw_bin()
        if resolved:
            return resolved
    except Exception:
        logger.debug("_resolve_kiroclaw_bin unavailable; falling back to PATH", exc_info=True)
    return shutil.which("kiroclaw") or "kiroclaw"


# KiroClaw's own stdio MCP servers (name → subcommand args). Single source for
# the CC/ACP paths; mirrors agent._MANAGED_MCP_SERVERS minus url-based entries.
# These are always materialized in stdio form (see kiroclaw_stdio_servers) so a
# stale ``url`` left in an on-disk config can never break core/cron loading.
_KIROCLAW_STDIO_SERVERS: dict[str, list[str]] = {
    "kiroclaw-core": ["mcp-core"],
    "kiroclaw-cron": ["mcp-cron"],
}


def kiroclaw_stdio_servers() -> dict[str, dict[str, Any]]:
    """Canonical stdio entries for kiroclaw-core/cron, keyed by server name.

    Built fresh per call so the resolved ``kiroclaw`` binary path is current.
    Consumed by both the CC config writer (``generate_mcp_json``) and the
    claude-agent-acp spawn reader (``acp/client._claude_acp_mcp_servers``).
    """
    cmd = _kiroclaw_bin()
    return {
        name: {"command": cmd, "args": list(args), "type": "stdio"}
        for name, args in _KIROCLAW_STDIO_SERVERS.items()
    }


def _resolve_prompt_content(prompt: str, fallback_name: str) -> str:
    """Resolve prompt content from a ``file://`` URI or inline string.

    Returns the resolved text, or a default fallback if the prompt is
    empty, points to a missing file, or targets a sensitive path.
    """
    if not prompt:
        return f"You are {fallback_name}, an autonomous AI agent."
    if prompt.startswith("file://"):
        prompt_path = Path(prompt[7:])
        # circular import: hooks transitively reaches cc_agent via config.loader,
        # so import the centralized reader at call time.
        from kiro_claw.hooks import safe_read_file

        # Route through hooks.safe_read_file, which enforces is_sensitive_path()
        # and raises PermissionError on a credential path — keeps the
        # sensitivity check in one place per the security-controls guideline.
        # Missing/unreadable/sensitive all fall back to the default prompt (no
        # secret leaks into the CC markdown).
        if not prompt_path.is_file():
            return f"You are {fallback_name}, an autonomous AI agent."
        try:
            return safe_read_file(str(prompt_path))
        except (OSError, PermissionError):
            logger.warning("Blocked or unreadable file:// prompt: %s", prompt_path)
            return f"You are {fallback_name}, an autonomous AI agent."
    return prompt


def _build_cc_hooks(kiro_hooks: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Translate kiro hook definitions to CC nested hook block shape.

    CC shape: ``{EventName: [{matcher: <regex>, hooks: [{type, command, timeout}]}]}``
    kiro shape: ``{eventName: [{matcher?: str, command: str, timeout?: int}]}``
    """
    cc_hooks: dict[str, list[dict[str, Any]]] = {}
    for kiro_event, cc_event in _KIRO_TO_CC_HOOK_EVENT.items():
        entries = kiro_hooks.get(kiro_event)
        if not entries or not isinstance(entries, list):
            continue
        cc_entries: list[dict[str, Any]] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            command = entry.get("command", "")
            if not command:
                continue
            # Translate matcher from glob to regex
            matcher = _translate_matcher(entry.get("matcher", ""))
            # For agentSpawn → SessionStart, CC uses "startup" as matcher
            if kiro_event == "agentSpawn" and not matcher:
                matcher = "startup"
            timeout = entry.get("timeout", 30)
            hook_def: dict[str, Any] = {
                "type": "command",
                "command": command,
            }
            if timeout != 30:
                hook_def["timeout"] = timeout
            cc_entries.append(
                {
                    "matcher": matcher,
                    "hooks": [hook_def],
                }
            )
        if cc_entries:
            cc_hooks[cc_event] = cc_entries
    return cc_hooks


def _build_disallowed_tools(agent_config: dict[str, Any]) -> list[str]:
    """Build CC disallowedTools from a kiro agent's ``deniedCommands`` only.

    The bundled ``_CC_DENY_PATTERNS`` are NOT included — they are installed
    once globally via ``install_cc_global_deny_settings`` so every CC session
    inherits them via ``permissions.deny`` in settings.json. Per-agent
    frontmatter only carries the agent's own added patterns.
    """
    result: list[str] = []
    tools_settings = agent_config.get("toolsSettings", {})
    for tool_key in ("execute_bash", "shell"):
        settings = tools_settings.get(tool_key, {})
        denied = settings.get("deniedCommands", [])
        for cmd in denied:
            pattern = f"Bash({cmd})"
            if pattern not in result:
                result.append(pattern)
    return result


def generate_cc_agent_markdown(
    agent_config: dict[str, Any],
    *,
    prompt_body: str = "",
) -> str:
    """Convert KiroClaw agent JSON to CC agent Markdown with YAML frontmatter.

    Emits full YAML frontmatter including: name, description, tools,
    disallowedTools, model, permissionMode, mcpServers, and hooks.
    The markdown body below frontmatter becomes the system prompt.

    Args:
        agent_config: The kiro agent configuration dict.
        prompt_body: Optional explicit prompt body. If empty, resolved from
            the config's ``prompt`` field (supports ``file://`` URIs).
    """
    name = agent_config.get("name", "kiroclaw")
    description = agent_config.get("description", "")
    model = agent_config.get("model", "")
    permission_mode = agent_config.get("permissionMode", "")

    # Translate tool lists
    raw_tools = agent_config.get("allowedTools", agent_config.get("tools", []))
    tools = _translate_tool_list(raw_tools)
    mcp_servers = list(agent_config.get("mcpServers", {}).keys())

    # Build frontmatter dict (insertion order preserved in yaml.dump)
    frontmatter: dict[str, Any] = {"name": name}
    if description:
        frontmatter["description"] = description
    if model:
        frontmatter["model"] = model
    if permission_mode:
        frontmatter["permissionMode"] = permission_mode
    if tools:
        frontmatter["tools"] = tools
    if mcp_servers:
        frontmatter["mcpServers"] = mcp_servers

    # Build disallowedTools from deniedCommands
    disallowed = _build_disallowed_tools(agent_config)
    if disallowed:
        frontmatter["disallowedTools"] = disallowed

    # Translate hooks
    kiro_hooks = agent_config.get("hooks", {})
    if isinstance(kiro_hooks, dict):
        cc_hooks = _build_cc_hooks(kiro_hooks)
        if cc_hooks:
            frontmatter["hooks"] = cc_hooks

    # Serialize frontmatter as YAML
    yaml_str = yaml.dump(
        frontmatter,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    ).rstrip("\n")

    lines = ["---", yaml_str, "---", ""]

    # Resolve prompt body
    if not prompt_body:
        prompt_body = _resolve_prompt_content(agent_config.get("prompt", ""), name)
    lines.append(prompt_body)
    lines.append("")
    return "\n".join(lines)


def install_cc_agent(agent_config: dict[str, Any], agent_name: str = "kiroclaw") -> Path:
    """Write agent markdown to ~/.claude/agents/<name>.md."""
    CC_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    md_content = generate_cc_agent_markdown(agent_config)
    path = CC_AGENTS_DIR / f"{agent_name}.md"
    path.write_text(md_content, encoding="utf-8")
    logger.info("Installed CC agent config: %s", path)
    return path


def generate_mcp_json(
    agent_config: dict[str, Any],
    *,
    settings_allow: list[str] | None = None,
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Generate .mcp.json content with ALL MCP servers from agent config.

    Unlike the kiro ACP path where servers are passed in session/new params,
    Claude Code reads servers from .mcp.json. This function translates ALL
    servers from agent_config (built by build_agent_config()) into CC format:
      - kiroclaw-core, kiroclaw-cron (the bundled servers)
      - User-configured servers from ~/.kiroclaw/mcp.json (or ~/.kiro/...)
      - Auto-discovered servers from mcp_discovery

    Handles kiro-only fields:
      - ``disabled: true`` → entry omitted entirely
      - ``autoApprove: [tool, ...]`` → appended to ``settings_allow`` as
        ``mcp__<server>__<tool>`` strings
      - ``disabledTools: [tool, ...]`` → returned as agent-level
        ``disallowedTools`` in the format ``mcp__<server>__<tool>``

    Args:
        agent_config: The kiro agent configuration dict.
        settings_allow: Optional pre-existing allow list to append to.

    Returns:
        Tuple of (mcp_json_dict, settings_allow_list, disallowed_tools_list).
    """
    mcp_servers = agent_config.get("mcpServers", {})
    cc_servers: dict[str, Any] = {}
    allow_list: list[str] = list(settings_allow or [])
    disallowed_list: list[str] = []

    for name, spec in mcp_servers.items():
        if not spec or not isinstance(spec, dict):
            continue

        # Skip disabled entries
        if spec.get("disabled", False):
            logger.debug("Skipping disabled MCP server: %s", name)
            continue

        # Collect autoApprove → settings_allow
        auto_approve = spec.get("autoApprove", [])
        if auto_approve and isinstance(auto_approve, list):
            for tool in auto_approve:
                perm = f"mcp__{name}__{tool}"
                if perm not in allow_list:
                    allow_list.append(perm)

        # Collect disabledTools → agent-level disallowedTools
        disabled_tools = spec.get("disabledTools", [])
        if disabled_tools and isinstance(disabled_tools, list):
            for tool in disabled_tools:
                entry_name = f"mcp__{name}__{tool}"
                if entry_name not in disallowed_list:
                    disallowed_list.append(entry_name)

        # Handle remote/URL-based MCP servers
        url = spec.get("url", "")
        if url:
            entry: dict[str, Any] = {"url": url}
            headers = spec.get("headers")
            if headers and isinstance(headers, dict):
                entry["headers"] = dict(headers)
            cc_servers[name] = entry
            continue

        cmd = spec.get("command", "")
        # Handle command_fn pattern (used by managed servers)
        if not cmd and "command_fn" in spec:
            try:
                cmd = spec["command_fn"]()
            except Exception:
                cmd = ""
        if not cmd:
            # kiroclaw-core/cron are re-asserted in stdio form below regardless,
            # so a missing command here is only fatal for other servers.
            if name not in _KIROCLAW_STDIO_SERVERS:
                logger.warning("Skipping MCP server %s: no command resolved", name)
            continue

        entry = {
            "command": cmd,
            "args": list(spec.get("args", [])),
            "type": "stdio",
        }
        env = spec.get("env")
        if env and isinstance(env, dict):
            entry["env"] = dict(env)
        cc_servers[name] = entry

    # Ensure kiroclaw's own servers are always present AND in stdio form.
    # A stale ``url`` (e.g. an abandoned gateway HTTP-MCP endpoint left in the
    # kiro source) would otherwise survive the loop above as a url-only entry
    # that resolves to nothing — overwrite unconditionally with the canonical
    # stdio command so kiroclaw-core/cron always load.
    cc_servers.update(kiroclaw_stdio_servers())

    return {"mcpServers": cc_servers}, allow_list, disallowed_list


def acp_servers_from_cc_map(cc_servers: dict[str, Any]) -> list[dict[str, Any]]:
    """Reshape a CC ``.mcp.json`` server map into the ACP ``session/new`` array.

    claude-agent-acp (the default ``claude_code`` backend) does NOT read
    ``~/.claude/agents/kiroclaw.mcp.json`` — that file is only consumed by the
    legacy standalone ``claude`` CLI via ``--mcp-config``. The ACP adapter
    instead accepts MCP servers as the ``mcpServers`` parameter of the
    ``session/new`` (and ``session/load``) JSON-RPC request, exactly like
    kiro-cli, and merges them with the user's own ``~/.claude.json`` servers
    (see ``acp-agent.js`` ``createSession``).

    Input is the CC map shape produced by :func:`generate_mcp_json`
    (``{name: {command/url, args, env, type}}``); output is the ACP array:

      - stdio:  ``{"name", "command", "args", "env": [{"name","value"}], "type": "stdio"}``
      - http:   ``{"name", "type": "http"|"sse", "url", "headers": [{"name","value"}]}``

    The adapter treats a server as remote only when ``type`` is ``"http"`` or
    ``"sse"``; a url-bearing entry with no type would be misrouted to stdio, so
    url servers are emitted with an explicit ``type`` (defaulting to ``http``).

    The adapter's zod schema (``zMcpServerStdio.env``,
    ``zMcpServerHttp.headers``, ``zMcpServerSse.headers``) types these as
    ``z.array(...)`` — *required*, not optional. Omitting them on a server with
    no env/headers fails ``session/new`` with ``-32602 Invalid params``
    (``expected array, received undefined``), so they are always emitted, empty
    list when there is nothing to pass.

    Args:
        cc_servers: The ``mcpServers`` map from a CC ``.mcp.json`` document.

    Returns:
        A list of ACP server descriptors suitable for ``session/new`` params.
        Empty list when the map has no usable servers.
    """
    servers: list[dict[str, Any]] = []
    for name, spec in cc_servers.items():
        if not isinstance(spec, dict):
            continue
        url = spec.get("url", "")
        if url:
            stype = spec.get("type") or "http"
            if stype not in ("http", "sse"):
                stype = "http"
            headers = spec.get("headers")
            header_list = (
                [{"name": k, "value": str(v)} for k, v in headers.items()]
                if isinstance(headers, dict)
                else []
            )
            entry: dict[str, Any] = {
                "name": name,
                "type": stype,
                "url": url,
                "headers": header_list,
            }
            servers.append(entry)
            continue
        cmd = spec.get("command", "")
        if not cmd:
            continue
        env = spec.get("env")
        env_list = (
            [{"name": k, "value": str(v)} for k, v in env.items()] if isinstance(env, dict) else []
        )
        entry = {
            "name": name,
            "command": cmd,
            "args": list(spec.get("args", [])),
            "env": env_list,
            "type": "stdio",
        }
        servers.append(entry)
    return servers


def build_acp_mcp_servers(agent_config: dict[str, Any]) -> list[dict[str, Any]]:
    """kiro agent config → ACP ``session/new`` mcpServers array (full pipeline).

    Runs :func:`generate_mcp_json` (normalizes every server, guarantees
    kiroclaw-core/cron as stdio) then reshapes via
    :func:`acp_servers_from_cc_map`. Used where a kiro agent config is in
    hand; the spawn path reads the already-materialized ``kiroclaw.mcp.json``
    and calls :func:`acp_servers_from_cc_map` directly instead.
    """
    mcp_data, _allow, _disallowed = generate_mcp_json(agent_config)
    return acp_servers_from_cc_map(mcp_data.get("mcpServers", {}))


# ── Security: CC deny patterns ──

# Patterns enforced via --disallowedTools CLI arg per CC invocation.
#
# With selective-mount sandbox (.aws/ hidden, only .aws/config exposed),
# most file-read patterns are unnecessary — the files don't exist in the
# mount namespace. Remaining patterns cover: CLI credential extraction
# (in-memory creds), IMDS, env var exposure, destructive ops, and
# defense-in-depth Read/Grep/Edit blocks on credential paths.
#
# The 42 "suspicious bash" patterns (audit-only in kiro-cli) are NOT included
# here because CC has no audit-only mode — they require an interactive gate
# that doesn't exist yet. Tracked as a known security gap.
_CC_DENY_PATTERNS: list[str] = [
    # ===== AWS CLI credential extraction (in-memory creds) =====
    "Bash(aws configure get*)",
    "Bash(aws configure export-credentials*)",
    "Bash(aws sts get-session-token*)",
    "Bash(aws sts assume-role*)",
    # Python SDK credential extraction — .aws/config exposes credential_process
    # so boto3/botocore can resolve live creds even without .aws/credentials.
    "Bash(*python*boto3*get_credentials*)",
    "Bash(*python*botocore*credentials*)",
    # ===== IMDS credential theft =====
    "Bash(curl *169.254.169.254*)",
    "Bash(wget *169.254.169.254*)",
    # ===== Environment variable exposure =====
    "Bash(echo $AWS_SECRET*)",
    "Bash(echo $AWS_SESSION*)",
    "Bash(echo $AWS_ACCESS*)",
    "Bash(printenv AWS*)",
    "Bash(env | grep AWS*)",
    "Bash(export AWS_SECRET*)",
    "Bash(export AWS_ACCESS*)",
    "Bash(*curl*$AWS_SECRET*)",
    "Bash(*curl*$AWS_ACCESS*)",
    "Bash(*curl*$AWS_SESSION*)",
    # ===== Data exfiltration =====
    "Bash(aws s3 cp * s3://*)",
    "Bash(aws s3 mv * s3://*)",
    "Bash(aws s3 sync * s3://*)",
    "Bash(*kiroclaw*token*)",
    # ===== Destructive operations =====
    "Bash(rm -rf /)",
    "Bash(rm -rf ~*)",
    "Bash(rm -rf /*)",
    "Bash(dd if=*)",
    "Bash(mkfs*)",
    "Bash(chmod 777*)",
    "Bash(chmod */usr/*)",
    "Bash(chmod */etc/*)",
    "Bash(chmod */sbin/*)",
    "Bash(chmod */boot/*)",
    "Bash(chmod */lib/*)",
    "Bash(chmod */lib64/*)",
    "Bash(chown */usr/*)",
    "Bash(chown */etc/*)",
    "Bash(chown */sbin/*)",
    "Bash(chown */boot/*)",
    "Bash(chown */lib/*)",
    "Bash(chown */lib64/*)",
    "Bash(git push*)",
    "Bash(git reset --hard*)",
    "Bash(aws * delete-*)",
    "Bash(aws * terminate-*)",
    "Bash(aws iam create-access-key*)",
    "Bash(aws kms schedule-key-deletion*)",
    "Bash(aws cloudformation update-termination-protection*)",
    "Bash(aws s3 rb*)",
    "Bash(aws s3 rm*)",
    "Bash(kubectl delete namespace*)",
    "Bash(cdk destroy*)",
    "Bash(terraform destroy*)",
    "Bash(pulumi destroy*)",
    "Bash(*DROP TABLE*)",
    "Bash(*DROP DATABASE*)",
    "Bash(*TRUNCATE TABLE*)",
    # ===== Pipe execution / reverse shell =====
    "Bash(curl *| bash*)",
    "Bash(curl *| sh*)",
    "Bash(wget *| bash*)",
    "Bash(nc -e*)",
    "Bash(ncat -e*)",
    # ===== Bash credential reads on EXPOSED dirs =====
    # .ssh is exposed in CC mode (git-over-SSH); block key reads.
    "Bash(cat ~/.ssh/*)",
    "Bash(cat */.ssh/*)",
    "Bash(head ~/.ssh/*)",
    "Bash(tail ~/.ssh/*)",
    "Bash(*base64*~/.ssh/*)",
    "Bash(cp ~/.ssh/*)",
    # .aws is hidden by sandbox but defense-in-depth if sandbox fails.
    "Bash(cat ~/.aws/*)",
    "Bash(cat */.aws/*)",
    "Bash(head ~/.aws/*)",
    "Bash(tail ~/.aws/*)",
    "Bash(*base64*~/.aws/*)",
    "Bash(cp ~/.aws/*)",
    # ===== CC tool-level blocks on credential paths =====
    "Read(~/.aws/*)",
    "Read(*/.aws/*)",
    "Read(~/.ssh/*)",
    "Read(*/.ssh/*)",
    "Read(~/.kube/*)",
    "Read(*/.kube/*)",
    "Read(~/.gnupg/*)",
    "Read(*/.gnupg/*)",
    "Read(~/.docker/config.json)",
    "Read(~/.netrc)",
    "Read(~/.git-credentials)",
    "Read(~/.npmrc)",
    "Read(~/.pypirc)",
    "Read(~/.kiroclaw/.env)",
    "Grep(~/.aws/*)",
    "Grep(*/.aws/*)",
    "Grep(~/.ssh/*)",
    "Grep(*/.ssh/*)",
    "Edit(~/.aws/*)",
    "Edit(*/.aws/*)",
    "Edit(~/.ssh/*)",
    "Edit(*/.ssh/*)",
]


# Benign canary commands. Any ``Bash(<glob>)`` deny rule that matches one of
# these is over-broad: it would hard-block ordinary work. KiroClaw's own scoped
# patterns (``Bash(git push*)``, ``Bash(*DROP TABLE*)``, …) match none of these.
_CC_DENY_CANARIES: tuple[str, ...] = (
    "ls",
    "ls -la",
    "git status",
    "cat file.txt",
    "echo hi",
    "python3 script.py",
    "pwd",
)


def find_overbroad_cc_deny_rules(settings: Any) -> list[str]:
    """Return ``Bash(...)`` deny rules in *settings* that block benign commands.

    Claude Code's native permission engine reads ``permissions.deny`` from the
    user-global ``~/.claude/settings.json`` and project ``.claude/settings.json``
    and blocks matching tools BEFORE KiroClaw's ``canUseTool`` host gate runs —
    a rule like ``Bash(*)`` / ``Bash(git *)`` therefore aborts ordinary commands
    with a cryptic "Tool use aborted" and no approval prompt, upstream of and
    invisible to KiroClaw. KiroClaw cannot override these (they are the user's
    own config), so the best we can do is detect and surface them.

    A rule is "over-broad" when its ``Bash(<glob>)`` body matches any benign
    canary command (case-insensitive fnmatch). KiroClaw's bundled scoped
    patterns match no canary and are never reported. Non-``Bash(...)`` rules
    (``Read()``/``Edit()``/bare tool names) are ignored — they don't gate bash.

    Defensive against malformed input: a non-dict ``settings`` or non-list
    ``deny`` yields ``[]`` rather than raising.
    """
    if not isinstance(settings, dict):
        return []
    perms = settings.get("permissions")
    if not isinstance(perms, dict):
        return []
    deny = perms.get("deny")
    if not isinstance(deny, list):
        return []
    flagged: list[str] = []
    for rule in deny:
        if not isinstance(rule, str):
            continue
        m = re.match(r"^Bash\((.*)\)$", rule.strip())
        if not m:
            continue
        glob = m.group(1).lower()
        if any(fnmatch.fnmatch(c, glob) for c in _CC_DENY_CANARIES):
            flagged.append(rule)
    return flagged


def _atomic_settings_write(path: Path, data: dict[str, Any], mode: int | None = None) -> None:
    """Write JSON settings atomically via tmp+rename with fsync.

    Preserves the existing file mode (defaulting to 0o644 for a new file) unless
    ``mode`` is given, in which case the file is created with exactly that mode
    from the start (no brief default-umask window). The randomly-named tmp file
    is unlinked on any error, so no ``.tmp`` is ever orphaned.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if mode is None:
                try:
                    mode = stat.S_IMODE(path.stat().st_mode)
                except FileNotFoundError:
                    mode = 0o644
            os.fchmod(f.fileno(), mode)
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, str(path))
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _apply_deny_and_marker(data: dict[str, Any]) -> None:
    """Layer the security deny patterns + managed marker onto ``data``.

    Used for the USER-global ``~/.claude/settings.json``. Writes ONLY the deny
    patterns (a safety control, kept regardless of isolation) and a marker so a
    later revert is precise. Does NOT write model keys (``availableModels`` /
    ``model``) — those are injected via the KiroClaw-owned per-session
    ``<work_dir>/.claude/settings.local.json`` (see acp/client.py
    ``_write_claude_local_settings``) so we never mutate the operator's model
    config in their real ~/.claude.
      - ``permissions.deny`` ← _CC_DENY_PATTERNS (applies to ALL sessions).
      - ``_kiroclaw_managed`` ← records the keys KiroClaw owns here.
    """
    permissions = data.setdefault("permissions", {})
    permissions["deny"] = list(_CC_DENY_PATTERNS)
    managed = data.get("_kiroclaw_managed")
    keys = set(managed) if isinstance(managed, list) else set()
    keys.add("permissions.deny")
    data["_kiroclaw_managed"] = sorted(keys)


def _apply_deny_and_models_for_isolated(data: dict[str, Any]) -> None:
    """Layer deny + marker + model allowlist + default onto the KiroClaw-OWNED
    isolated config dir (allowed by the file-scope decision — it is not the
    user's ~/.claude). Mirrors the old ``_apply_deny_and_models`` so a spawn
    still resolves the 1M window even if the per-session ``settings.local.json``
    is somehow absent.
      - ``permissions.deny`` ← _CC_DENY_PATTERNS.
      - ``availableModels`` ← _CC_AVAILABLE_MODELS (unlocks the real 1M window).
      - ``model`` ← _CC_DEFAULT_MODEL only when unset (never clobber).
    """
    _apply_deny_and_marker(data)
    data["availableModels"] = list(_CC_AVAILABLE_MODELS)
    if not data.get("model"):
        data["model"] = _CC_DEFAULT_MODEL


def revert_user_model_settings(
    target_path: Path | None = None, dry_run: bool = False, require_marker: bool = False
) -> bool:
    """Remove KiroClaw-written MODEL keys from the user's ~/.claude/settings.json.

    Earlier KiroClaw versions wrote ``availableModels`` + ``model`` into the
    user's real ``~/.claude/settings.json``. Model config now lives in the
    KiroClaw-owned per-session ``settings.local.json``, so this un-pollutes the
    user file. Removal is value-matched against the historical constants:
      - ``availableModels``: removed iff it exactly equals :data:`_CC_AVAILABLE_MODELS`.
      - ``model``: removed iff it equals :data:`_CC_DEFAULT_MODEL`.
      - ``permissions.deny`` and the ``_kiroclaw_managed`` marker: always kept.

    ``require_marker`` controls the safety/coverage tradeoff (value-match alone
    cannot tell a value KiroClaw wrote from the identical value an operator chose):
      - ``True`` (the unattended BOOT path): only act if ``_kiroclaw_managed``
        records the model key, so we NEVER delete a value the operator set
        themselves. Old pollution has no such marker, so boot leaves it for the
        explicit command — boot's job is just to avoid clobbering.
      - ``False`` (the explicit ``kiroclaw cc revert-settings`` CLI): value-match
        with no marker requirement, so it cleans legacy pollution the operator
        asked to remove.

    Returns True if a change was (or, in ``dry_run``, would be) made. Atomic
    write; preserves all other keys; idempotent; never creates the file.
    """
    settings_path = target_path or CC_SETTINGS_PATH
    if not settings_path.is_file():
        return False
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    if not isinstance(data, dict):
        return False
    managed = data.get("_kiroclaw_managed")
    managed_keys = set(managed) if isinstance(managed, list) else set()
    changed = False
    if data.get("availableModels") == list(_CC_AVAILABLE_MODELS) and (
        not require_marker or "availableModels" in managed_keys
    ):
        if not dry_run:
            data.pop("availableModels", None)
        changed = True
    if data.get("model") == _CC_DEFAULT_MODEL and (not require_marker or "model" in managed_keys):
        if not dry_run:
            data.pop("model", None)
        changed = True
    if changed and not dry_run:
        _atomic_settings_write(settings_path, data)
        logger.info("Reverted KiroClaw model keys from %s", settings_path)
    return changed


def install_cc_global_deny_settings(target_path: Path | None = None) -> Path:
    """Write CC settings.json: global security deny patterns + managed marker.

    Writes ONLY the security control to the user-global ``~/.claude`` (no per-agent
    frontmatter / CLI args needed):
      - ``permissions.deny`` ← _CC_DENY_PATTERNS (applies to ALL sessions).
      - ``_kiroclaw_managed`` ← records ``permissions.deny`` as KiroClaw-owned so
        a later ``revert_user_model_settings`` is precise.
    Model config (``availableModels`` / ``model``) is intentionally NOT written
    here — it is injected via the KiroClaw-owned per-session
    ``<work_dir>/.claude/settings.local.json`` (acp/client.py), so KiroClaw never
    mutates the operator's model config in their real ~/.claude. The isolated dir
    still gets the model allowlist via :func:`seed_isolated_cc_config` (a
    KiroClaw-owned dir).
    Atomic write preserves all other keys. Idempotent.

    Default target is ``CC_SETTINGS_PATH`` (the user-global ``~/.claude``).
    """
    settings_path = target_path or CC_SETTINGS_PATH
    existing: dict[str, Any] = {}
    if settings_path.is_file():
        try:
            existing = json.loads(settings_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            logger.warning(
                "Could not parse existing settings at %s; will overwrite permissions.deny",
                settings_path,
            )
    _apply_deny_and_marker(existing)
    _atomic_settings_write(settings_path, existing)
    logger.info(
        "Installed %d deny patterns to %s (model config NOT written to user file)",
        len(_CC_DENY_PATTERNS),
        settings_path,
    )
    return settings_path


# Keys copied verbatim from the user's ~/.claude/settings.json into the isolated
# config dir — the credential/model/env state the spawned CC genuinely needs.
# Everything NOT listed here (and not re-asserted by install_cc_global_deny_settings)
# is dropped, which is the whole point: enabledPlugins/extraKnownMarketplaces/etc.
# never reach the isolated session.
#
# NOTE: effortLevel is intentionally NOT stripped. KiroClaw only pushes effort via
# session/set_config_option when an override is configured; with no override the
# session reads effortLevel straight from settings.json. Stripping it would
# silently downgrade effort below the user's configured level (e.g. xhigh), so
# the user's effortLevel is preserved into the isolated config.
_CC_SEED_STRIP_KEYS: tuple[str, ...] = (
    "enabledPlugins",  # user-installed CC plugins → duplicate MCP servers + agents + skills
    "extraKnownMarketplaces",  # plugin marketplace registration
    "enabledMcpjsonServers",  # absent on this host today; pop is a safe no-op
    "disabledMcpjsonServers",
    "statusLine",  # cosmetic; irrelevant to a headless subprocess
    "theme",  # cosmetic
)


def _settings_has_aws_cred_export(path: Path) -> bool:
    """Whether ``path`` parses to settings JSON containing ``awsCredentialExport``.

    Used by the seed mtime guard to honor the boot-ordering safety: the early
    return is only correct once the seeded file already carries the
    credential-refresh command (or the source has none to copy). Any parse/read
    error returns False so the caller errs toward a full re-seed.
    """
    if not path.is_file():
        return False
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return False
    return isinstance(parsed, dict) and "awsCredentialExport" in parsed


def seed_isolated_cc_config(root: Path | None = None) -> Path:
    """Seed the isolated CC config dir's settings.json (creds kept, plugins stripped).

    Copies the user's literal ``~/.claude/settings.json`` into
    ``<root>/settings.json`` with the plugin/marketplace/cosmetic keys
    (:data:`_CC_SEED_STRIP_KEYS`) removed, then layers KiroClaw's deny patterns +
    1M model allowlist (:func:`_apply_deny_and_models_for_isolated`) onto the
    in-memory dict before a SINGLE atomic write. This drops the token bloat
    (plugins/agents/skills) while preserving:

      - ``awsCredentialExport`` — consumed by the native ``claude`` binary to
        refresh Bedrock creds. Copied verbatim; dropping it is what broke auth
        when we tried ``settingSources: []``.
      - ``availableModels`` / ``model`` — the 1M window; re-asserted with the
        full ``[1m]`` ids by ``_apply_deny_and_models_for_isolated``.
      - ``effortLevel`` — the user's configured reasoning effort (see
        :data:`_CC_SEED_STRIP_KEYS`).
      - ``env`` (e.g. ``AWS_REGION``) and top-level ``permissions`` (minus
        ``defaultMode``/``allow``/``ask``, see below).

    The seeded file is written 0o600: it carries ``awsCredentialExport`` (a
    credential-refresh command), and ``_atomic_settings_write`` would otherwise
    default a brand-new file to 0o644 (group/world readable).

    The seed SOURCE is always the literal :data:`_USER_CC_ROOT` file — never
    :func:`cc_config_root`, which under isolation points at the dir being seeded.

    Boot ordering safety: the boot ``repair_agent_configs`` pass writes
    deny+models here BEFORE any spawn copies ``awsCredentialExport``, so the
    mtime early-return only fires once the seeded file already carries the
    creds (or the source has none) — otherwise it does the full re-seed so the
    creds are always present.
    """
    root = root or cc_config_root()

    # Data-loss guard: if the isolation root ever resolves to the operator's real
    # ~/.claude (e.g. CLAUDE_CONFIG_DIR=$HOME/.claude in the gateway env), seeding
    # would strip and OVERWRITE their genuine settings.json — destroying plugins.
    # Skip entirely in that case.
    try:
        if root.resolve() == _USER_CC_ROOT.resolve():
            logger.warning(
                "CC isolation root resolves to the user ~/.claude; skipping seed "
                "to avoid destroying user config"
            )
            return root
    except OSError:
        # Fail CLOSED: if we cannot positively confirm the root is distinct from
        # the operator's real ~/.claude, do NOT proceed with the destructive
        # strip-and-overwrite seed — skip instead. Proceeding could overwrite the
        # user's genuine settings.json (data loss).
        logger.warning(
            "Could not resolve CC isolation root %s; skipping seed to avoid " "potential data loss",
            root,
            exc_info=True,
        )
        return root

    source = _USER_CC_ROOT / "settings.json"
    seeded = root / "settings.json"

    # mtime guard: the full re-seed runs on EVERY spawn (warm-pool/subagent/
    # secretary/cron) doing fsync'd writes + reads + json parses. Boot already
    # seeds; the source only changes on user edit. Skip the re-seed when the
    # seeded file is newer-or-equal than the source AND the documented cred
    # ordering is satisfied: the seeded file already carries awsCredentialExport
    # (or the source has none to copy). Any error → fall through to full seed.
    try:
        if seeded.is_file():
            seeded_mtime = seeded.stat().st_mtime
            source_mtime = source.stat().st_mtime if source.is_file() else 0.0
            if seeded_mtime >= source_mtime:
                source_has_creds = _settings_has_aws_cred_export(source)
                if (not source_has_creds) or _settings_has_aws_cred_export(seeded):
                    return root
    except OSError:
        logger.debug("CC seed mtime guard check failed; doing full seed", exc_info=True)

    data: dict[str, Any] = {}
    if source.is_file():
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (ValueError, OSError):
            logger.warning("Could not parse %s for CC isolation seed; seeding bare", source)
            data = {}

    for key in _CC_SEED_STRIP_KEYS:
        data.pop(key, None)

    # Drop permissions.defaultMode/allow/ask so EVERY tool routes through the
    # adapter's canUseTool host gate (hooks.on_tool_call → reject_tool). An
    # inherited defaultMode 'dontAsk', or any inherited allow/ask wildcard (e.g.
    # Bash(*)/Edit(*)/mcp__*), is auto-approved by CC's native permission engine
    # WITHOUT calling canUseTool — silently bypassing KiroClaw's deny/approve
    # gate. Keep only deny (re-asserted below with the KiroClaw deny patterns).
    perms = data.get("permissions")
    if isinstance(perms, dict):
        perms = dict(perms)
        perms.pop("defaultMode", None)
        perms.pop("allow", None)
        perms.pop("ask", None)
        data["permissions"] = perms

    # Layer deny + marker + 1M availableModels + default model onto the in-memory
    # dict, then write ONCE. This is the KiroClaw-OWNED isolated dir (not the
    # user's ~/.claude), so writing the model allowlist here is allowed and keeps
    # the 1M window working even if per-session settings.local.json is absent.
    _apply_deny_and_models_for_isolated(data)

    root.mkdir(parents=True, exist_ok=True)
    # The seed carries awsCredentialExport; create it 0o600 AT WRITE TIME (via the
    # atomic writer's mode= param, which fchmods the tmp file before the rename)
    # so the credential-bearing file is never briefly group/world-readable at the
    # 0o644 default — a post-write os.chmod would leave that race window open.
    # Only the isolated seed forces this; _atomic_settings_write's other callers
    # keep their existing (mode-preserving) behavior.
    _atomic_settings_write(seeded, data, mode=0o600)
    logger.info("Seeded isolated CC config at %s (plugins stripped, creds kept)", root)
    return root
