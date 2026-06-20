"""ACP client — JSON-RPC 2.0 over stdio with `kiro-cli acp` or `claude-agent-acp`.

Protocol (ACP JSON-RPC 2.0):
  initialize → session/new → session/set_mode → session/set_model → session/prompt
  (claude backend: skips set_mode and uses session/set_config_option for model)

Agent selection: ``session/set_mode`` with ``modeId`` activates the agent
config (prompt, tools, resources).  MCP servers are passed explicitly
in ``session/new`` via the ``mcpServers`` parameter.

Permission flow:
  ← session/request_permission (server→client REQUEST with uuid id)
  → {result: {outcome: {outcome: "selected", optionId: "allow_once"}}}
"""

from __future__ import annotations

import asyncio
import base64
import difflib
import json
import logging
import os
import re
import shutil
import signal
import subprocess as subprocess_mod
import sys
import time
from collections import deque
from contextlib import aclosing
from pathlib import Path
from typing import AsyncGenerator, AsyncIterator

from kiro_claw.acp.types import (
    ACP_BACKEND_CLAUDE,
    EVENT_AGENT_SWITCHED,
    EVENT_CLEAR_STATUS,
    EVENT_COMPACTION_STATUS,
    EVENT_COMPLETE,
    EVENT_MCP_OAUTH_REQUEST,
    EVENT_MCP_SERVER_INIT_FAILURE,
    EVENT_MCP_SERVER_INITIALIZED,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    KNOWN_SESSION_UPDATES,
    METHOD_AGENT_SWITCHED,
    METHOD_CANCEL,
    METHOD_CLEAR_STATUS,
    METHOD_COMMANDS_EXECUTE,
    METHOD_COMPACTION_STATUS,
    METHOD_INITIALIZE,
    METHOD_KIRO_SESSION_UPDATE,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_METADATA,
    METHOD_PROMPT,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_UPDATE,
    METHOD_SET_MODE,
    METHOD_SET_MODEL,
    METHOD_SUBAGENT_LIST_UPDATE,
    OPTION_ALLOW_ALWAYS,
    OPTION_ALLOW_ONCE,
    OUTCOME_CANCELLED,
    OUTCOME_SELECTED,
    STOP_REASON_END_TURN,
    UPDATE_AGENT_MESSAGE_CHUNK,
    UPDATE_AGENT_THOUGHT_CHUNK,
    UPDATE_CONFIG_OPTION,
    UPDATE_TOOL_CALL,
    UPDATE_USAGE,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
    JsonRpcRequest,
)
from kiro_claw.env import augmented_path, resolve_krb5_ccname
from kiro_claw.sandbox import wrap_argv
from kiro_claw.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

CLIENT_NAME = "kiroclaw"
CLIENT_VERSION = "0.1.2"
# kiro-cli uses a date-stamped protocol; claude-agent-acp follows the
# upstream ACP SDK (numeric integer, currently 1).  See acp.types.
PROTOCOL_VERSION = "2025-08-22"
PROTOCOL_VERSION_CLAUDE = 1
DEFAULT_MODEL = "auto"

KIRO_CLI_BIN = "kiro-cli"
KIRO_CLI_SUBCMD = "acp"

CLAUDE_ACP_BIN = "claude-agent-acp"
# Claude Code CLI binary.  The claude-agent-acp adapter delegates the actual
# model turn to @anthropic-ai/claude-agent-sdk, which needs a per-platform
# native Claude binary (~250 MB each).  Those ship as npm optionalDependencies
# that a plain ``npm i -g @agentclientprotocol/claude-agent-acp`` may omit, so
# the SDK can fail session/new with "Claude native binary not found for
# <platform>".  The SDK does NOT auto-discover a `claude` on PATH — it only
# looks for that bundled native package — so the host having Claude Code
# installed is not enough; we point the adapter at it explicitly via
# CLAUDE_CODE_EXECUTABLE (the env var the adapter forwards to the SDK as
# pathToClaudeCodeExecutable).  ``augmented_path()`` includes the common Node
# install locations (mise/nvm/fnm/volta shims, npm global bin), so this
# resolves with no user action when Claude Code is on PATH; otherwise the
# adapter surfaces its own native-binary error.
CLAUDE_CODE_BIN = "claude"
# npm package that provides the claude-agent-acp binary.  Install it publicly
# with ``npm i -g @agentclientprotocol/claude-agent-acp`` (or add it as a
# project dependency); resolution also accepts a copy under a project-local
# ``node_modules`` so no global install is strictly required.
CLAUDE_ACP_NPM_PKG = "@agentclientprotocol/claude-agent-acp"
# Entry script relative to the installed package directory (its package.json
# "bin" field).  Used to locate a copy under a project ``node_modules``.
_CLAUDE_ACP_PKG_ENTRY = Path(CLAUDE_ACP_NPM_PKG) / "dist" / "index.js"
# A direct runtime dependency of the adapter that npm hoists flat into the
# same node_modules root.  Its presence is a cheap completeness check: a
# copy missing it would crash at import with
# ``ERR_MODULE_NOT_FOUND: @agentclientprotocol/sdk``, so we reject such an
# incomplete root and fall through to the next candidate.
_CLAUDE_ACP_DEP_MARKER = Path("@agentclientprotocol") / "sdk"

# High-frequency, content-free adapter stderr diagnostics that _drain_stderr()
# drops instead of forwarding as per-line WARNINGs.  The driving case is the
# claude-agent-acp "Unexpected case: {...thinking_tokens...}" line.  Mechanism
# (confirmed by reading the vendored adapter, dist/acp-agent.js): claude-code
# emits a `system` message with subtype `thinking_tokens`, but the adapter's
# `switch (message.subtype)` enumerates ~18 known subtypes (init, status,
# compact_boundary, memory_recall, api_retry, ...) and routes anything else to
# `default: unreachable(message)`, which does `logger.error("Unexpected case:
# " + JSON.stringify(message))` to stderr — one line per token delta.  Measured
# at ~10 lines/sec during active thinking (one line per 2-4 thinking tokens; the
# payload is only estimated_tokens/_delta/uuid/session_id — no response content,
# so dropping them loses nothing).
#
# This is a forward-compat gap in the adapter, NOT new behavior in a specific
# claude-code build: the `thinking_tokens` event is present in both 2.1.165.357
# and 2.1.168.358 (verified by string-matching both bundled `claude` binaries —
# identical occurrences), so it predates the .168 update that drew attention to
# it.  Exactly when it began appearing in our logs is unconfirmed.  The cleaner
# long-term fix is upstream (add a `thinking_tokens` case to the adapter / bump
# the vendored version); this filter is the version-agnostic stopgap that also
# absorbs the next unenumerated subtype's flood.
#
# Why drop rather than just downgrade the level:
#   1. Log hygiene — gateway.log uses a RotatingFileHandler(maxBytes=2MB,
#      backupCount=3) (see cli.py), so a sustained burst rolls genuine
#      diagnostics out of the retained 8MB window.
#   2. Event-loop load — the file handler is a plain *synchronous* handler and
#      _drain_stderr runs as a task on the gateway event loop, so each forwarded
#      line costs a synchronous file write + two regex redaction passes on the
#      same loop that streams responses.  Per-session the cost is small; it
#      compounds across concurrent thinking sessions.
# (This is a log-volume / event-loop-load reduction, NOT a fix for any
# turn-stall or "agent not responding" symptom — no such causal link was
# established.)
#
# Match on a stable substring (not the full JSON) so the filter survives field
# changes, and keep the tuple NARROW so a genuine error line is never silently
# swallowed.
_SUPPRESSED_STDERR_MARKERS = ("thinking_tokens",)
# Minimum seconds between throttled debug summaries of the suppressed-line count,
# so the suppression itself stays observable without re-introducing a flood.
_SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS = 60.0


def _is_safe_oauth_url(url: str) -> bool:
    """Reject anything that isn't http(s) — `<a href>` will execute javascript:/data:."""
    if not url:
        return False
    lower = url.lower()
    return lower.startswith("https://") or lower.startswith("http://")


def _resolve_kiro_bin() -> str | None:
    """Find the kiro-cli binary, or None when it is not installed.

    kiro-cli is the agent backend for the public build. Honours an explicit
    ``KIROCLAW_KIRO_BIN`` override first (only when it points at an existing
    executable; ignored otherwise), then resolves from PATH (augmented with the
    usual local bin dirs so a non-login gateway still finds a user install) and
    returns ``None`` rather than raising when it is absent, so the caller can
    surface a clear "install kiro-cli" error instead of crashing.
    """
    env_bin = os.environ.get("KIROCLAW_KIRO_BIN")
    if env_bin and Path(env_bin).is_file() and os.access(env_bin, os.X_OK):
        return env_bin
    search_path = augmented_path(os.environ.get("PATH", ""))
    return shutil.which(KIRO_CLI_BIN, path=search_path)


def _mise_which(tool: str) -> str | None:
    """Ask mise for the resolved path of *tool*.

    Respects MISE_DATA_DIR, global config, and .mise.toml — works
    regardless of how the user configured their mise installation.
    Returns None if mise isn't installed or the tool isn't registered.
    """
    mise_bin = shutil.which("mise")
    if not mise_bin:
        return None
    try:
        result = subprocess_mod.run(
            [mise_bin, "which", tool],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            path = result.stdout.strip()
            if Path(path).is_file():
                return path
    except (subprocess_mod.TimeoutExpired, OSError):
        pass
    return None


def _mise_node_installs_dir() -> Path:
    """Canonical path to mise's Node installs directory."""
    return Path.home() / ".local" / "share" / "mise" / "installs" / "node"


def _resolve_node_for_script(script_path: str) -> str | None:
    """Derive the correct node binary for a script installed under mise.

    If *script_path* lives under ``~/.local/share/mise/installs/node/<ver>/``,
    return the co-located ``bin/node``.  This avoids reliance on shim
    resolution which requires mise global config and a cooperative cwd.

    Resolves both $HOME and the script path to real paths to handle
    symlinked home directories (e.g. /home/user -> /local/home/user).
    """
    resolved = Path(script_path).resolve()
    mise_installs = _mise_node_installs_dir().resolve()
    try:
        rel = resolved.relative_to(mise_installs)
        version_dir = mise_installs / rel.parts[0]
        node_bin = version_dir / "bin" / "node"
        if node_bin.is_file() and os.access(node_bin, os.X_OK):
            return str(node_bin)
    except (ValueError, IndexError):
        pass
    return None


_UNRESOLVED: object = object()  # sentinel for "not yet resolved"
_claude_acp_argv_cache: list[str] | None | object = _UNRESOLVED


def _vendored_claude_acp_roots(pkg_dir: Path | None = None) -> list[Path]:
    """Directories that may contain a project-local ``node_modules`` copy of
    the claude-agent-acp adapter.

    A project-local install (``npm i @agentclientprotocol/claude-agent-acp`` in
    the repo, or a copy bundled next to the installed package) lets the gateway
    run without a global npm install — useful in non-login launchd/systemd
    contexts with a minimal PATH.  Resolution still falls back to global / PATH
    installs in ``_resolve_claude_acp_bin``; these roots are just preferred.

    *pkg_dir* (the installed ``kiro_claw`` package directory) defaults to this
    module's location; it is a parameter so tests can inject a fake layout.
    """
    roots: list[Path] = []

    # 1. Bundled alongside the installed package (optional vendored copy).
    if pkg_dir is None:
        pkg_dir = Path(__file__).resolve().parent.parent  # .../kiro_claw
    roots.append(pkg_dir / "_vendor" / "node_modules")

    # 2. Explicit project dir (KIROCLAW_PROJECT_DIR points at the repo root):
    #    its ``node_modules`` from a local ``npm install``.
    proj = os.environ.get("KIROCLAW_PROJECT_DIR", "")
    if proj:
        roots.append(Path(proj) / "node_modules")

    return roots


def _resolve_vendored_claude_acp(pkg_dir: Path | None = None) -> str | None:
    """Return the path to a vendored claude-agent-acp entry script, or None.

    Looks for ``<root>/@agentclientprotocol/claude-agent-acp/dist/index.js``
    under each candidate ``node_modules`` root.  Returns the first existing
    entry script (a plain Node script — the caller wraps it with ``node``).

    A root is accepted only when the adapter's hoisted dependency marker
    (``@agentclientprotocol/sdk``) is also present, so an incomplete vendored
    copy (entry script but missing deps) is skipped in favour of a complete
    one rather than picked and crashed at ESM import time.
    """
    for root in _vendored_claude_acp_roots(pkg_dir):
        entry = root / _CLAUDE_ACP_PKG_ENTRY
        if entry.is_file() and (root / _CLAUDE_ACP_DEP_MARKER).is_dir():
            return str(entry)
    return None


def _resolve_claude_acp_bin() -> list[str] | None:
    """Find the claude-agent-acp Node entry script and return argv.

    Returns a list suitable for subprocess argv (e.g. ``["node", "script.js"]``
    or ``["/path/to/binary"]``).  Explicitly resolves the node binary to
    avoid relying on ``#!/usr/bin/env node`` shebang resolution which fails
    in non-interactive daemon contexts (mise shims require cwd with
    .mise.toml or a working global config).

    Resolution order:
      1. ``CLAUDE_AGENT_ACP_BIN`` env var (explicit override; need not be
         executable — non-executable scripts are auto-wrapped with node).
      2. Project-local ``node_modules`` copy (from ``npm install`` in the repo
         or a copy bundled next to the package) — no global install required.
      3. ``mise which claude-agent-acp`` (respects all mise config).
      4. Direct glob under mise installs (fallback if mise exec fails).
      5. Augmented PATH (includes mise shims, nvm, fnm, volta, npm -g).
    """
    candidates: list[str] = []

    override = os.environ.get("CLAUDE_AGENT_ACP_BIN")
    if override and Path(override).is_file():
        candidates.append(override)

    # Project-local node_modules copy.  Preferred over PATH-based resolution
    # because it needs no global install and works in non-login gateway
    # contexts (launchd/systemd) with a minimal PATH.
    vendored = _resolve_vendored_claude_acp()
    if vendored:
        candidates.append(vendored)

    # Preferred: ask mise directly — respects MISE_DATA_DIR, global config,
    # and .mise.toml regardless of the user's installation layout.
    mise_resolved = _mise_which(CLAUDE_ACP_BIN)
    if mise_resolved:
        candidates.append(mise_resolved)

    # Fallback: search mise installs directory directly (handles case where
    # `mise which` fails due to missing global config in daemon context).
    mise_installs = _mise_node_installs_dir()
    if mise_installs.is_dir():
        for bin_path in sorted(mise_installs.glob("*/bin/" + CLAUDE_ACP_BIN), reverse=True):
            if bin_path.is_file():
                candidates.append(str(bin_path))
                break

    # Also search augmented PATH (includes mise shims) as fallback.
    # Covers nvm, fnm, volta, and plain `npm i -g` installations.
    search_path = augmented_path(os.environ.get("PATH", ""))
    on_path = shutil.which(CLAUDE_ACP_BIN, path=search_path)
    if on_path:
        candidates.append(on_path)

    for script in candidates:
        resolved = str(Path(script).resolve())
        node = _resolve_node_for_script(resolved)
        if node:
            return [node, resolved]
        if os.access(script, os.X_OK):
            return [script]
        node_on_path = shutil.which("node", path=search_path)
        if node_on_path:
            return [node_on_path, resolved]

    return None


def _resolve_claude_code_executable() -> str | None:
    """Find the Claude Code CLI binary for CLAUDE_CODE_EXECUTABLE.

    The claude-agent-acp adapter forwards this env var to
    @anthropic-ai/claude-agent-sdk as ``pathToClaudeCodeExecutable``, letting
    the SDK use an existing ``claude`` install instead of the per-platform
    native binary package (~250 MB) that a plain npm install may omit.  The SDK
    does not search PATH itself, so this resolution is required even when the
    host has Claude Code installed.

    Resolution order:
      1. ``CLAUDE_CODE_EXECUTABLE`` env var (explicit override; honoured as-is).
      2. ``mise which claude`` (respects MISE_DATA_DIR and all mise config).
      3. Augmented PATH (``env.augmented_path`` — includes mise/nvm/fnm/volta
         shims and the npm global bin), so a non-login launchd/systemd gateway
         still finds an installed ``claude``.

    Returns the resolved path, or ``None`` when no ``claude`` is found.
    """
    override = os.environ.get("CLAUDE_CODE_EXECUTABLE")
    if override and Path(override).is_file():
        return override

    mise_resolved = _mise_which(CLAUDE_CODE_BIN)
    if mise_resolved:
        return mise_resolved

    search_path = augmented_path(os.environ.get("PATH", ""))
    return shutil.which(CLAUDE_CODE_BIN, path=search_path)


def _resolve_ssh_auth_sock(env: dict[str, str]) -> None:
    """Ensure SSH_AUTH_SOCK points to a live agent socket.

    The gateway's inherited value may be stale after an ssh-agent restart.
    Re-discovers the current agent socket without spawning a login shell.

    - macOS: launchd listener path changes on reboot
    - Linux: ssh-agent sockets live under /tmp/ssh-*/agent.*
    """
    import glob
    import stat
    import sys

    current = env.get("SSH_AUTH_SOCK", "")
    if current and os.path.exists(current):
        return  # already valid

    if sys.platform == "darwin":
        patterns = ["/tmp/com.apple.launchd.*/Listeners"]
    else:
        uid = os.getuid()
        patterns = [
            "/tmp/ssh-*/agent.*",
            f"/run/user/{uid}/ssh-agent.socket",
            f"/run/user/{uid}/keyring/ssh",
        ]

    for pattern in patterns:
        candidates = [p for p in glob.glob(pattern) if stat.S_ISSOCK(os.stat(p).st_mode)]
        if candidates:
            best = max(candidates, key=lambda p: os.path.getmtime(p))
            env["SSH_AUTH_SOCK"] = best
            logger.debug("Resolved SSH_AUTH_SOCK → %s", best)
            return


# Subprocess stdout buffer — kiro-cli can send large JSON-RPC lines (tool outputs)
_STDOUT_BUFFER_LIMIT = 10 * 1024 * 1024  # 10MB

# Max consecutive empty reads before checking if process is alive
_MAX_CONSECUTIVE_EMPTY = 5

# Emitted by kiro-cli as a plain agent_message_chunk when its built-in, non-overridable
# security filter cancels every tool use in an assistant turn (e.g. shell commands
# containing "credentials").  After this text kiro-cli returns to an idle state waiting
# for the next user prompt and NEVER sends a ``complete`` response for the in-flight
# ``session/prompt`` — so without special handling KiroClaw waits the full 2h timeout.
# Treating this chunk as end-of-turn unblocks the caller; the text itself is still
# yielded so the user/agent sees what happened.  We use an exact (stripped) match so
# the detection does not fire if the model merely quotes the marker string in prose.
_TOOL_INTERRUPTED_MARKER = "Tool uses were interrupted, waiting for the next user prompt"


def _is_tool_interrupted_marker(chunk: str) -> bool:
    """Exact match against the kiro-cli security-filter interrupt marker."""
    return chunk.strip() == _TOOL_INTERRUPTED_MARKER


# Timeouts for session initialization steps
_INIT_TIMEOUT = 240.0  # 4 min — MCP servers can be slow to initialize
# set_mode/set_model: fire-and-forget.  kiro-cli accepts these commands
# but usually never sends a JSON-RPC response — MCP servers load
# asynchronously.  Any late responses land in _buffer and are harmlessly
# skipped by _process_message() during the next prompt read loop.
_DRAIN_DURATION = 1.0  # hard cap on draining MCP server init notifications
# Idle early-exit: once no init notification has arrived for this long, MCP
# servers have gone quiet and we stop draining instead of always waiting the full
# _DRAIN_DURATION. The cap still bounds genuinely slow servers; the idle window
# short-circuits the common fast case (servers quiet well under the cap), cutting
# time-to-first-token on new sessions without risking a missed banner from an
# active server. Must stay strictly below _DRAIN_DURATION, otherwise the hard cap
# fires first and the idle path becomes dead code.
_DRAIN_IDLE_EXIT = 0.5
_DEFAULT_PROMPT_TIMEOUT = 7200.0  # 2 hours — allow very long tool execution
_READ_TIMEOUT = 20.0
# After streaming content, if no new data arrives for this many seconds,
# treat the turn as done.  Handles kiro-cli silently finishing without
# sending the JSON-RPC `result` response.
_STALE_TURN_TIMEOUT = 90.0
# After a tool is DISPATCHED, if no data of ANY kind (tool result, progress
# update, permission request, completion) arrives for this many seconds, treat
# the turn as a dead stall and exit.  Unlike _STALE_TURN_TIMEOUT this does NOT
# require _stale_eligible (which is cleared the moment a tool_call is yielded),
# so it catches the "tool dispatched but never resolves" hang that otherwise
# runs to the caller's full prompt timeout — e.g. a cron job dispatching a tool
# that silently never returns, burning the whole job timeout.  Long real tools
# keep resetting the timer via tool_call_update progress frames and tool
# results, so this only trips on a genuine stall.
_TOOL_STALL_TIMEOUT = 600.0
_CANCEL_GRACE_SECS = 10.0  # grace window for cooperative cancel ack
# Absolute safety cap for _wait_for_response's activity-based deadline. The
# per-call deadline resets on every received frame (so a long session/load
# replay that streams the whole transcript as notifications is not killed),
# but never extends past this hard ceiling.
_WAIT_RESPONSE_MAX_TIMEOUT = 600.0  # 10 min absolute ceiling
# JSON-RPC 2.0 reserved error code for an unrecognized method — used to answer
# unknown server→client requests so the agent fails fast instead of hanging.
_JSONRPC_METHOD_NOT_FOUND = -32601

# Legacy kiro permission options omit the spec-mandated `kind` field. Only
# synthesize a kind for these well-known literals — unknown ids stay empty
# so we don't fabricate intent the agent didn't express.
_LEGACY_OPTION_KIND: dict[str, str] = {
    OPTION_ALLOW_ONCE: "allow_once",
    "allow": "allow_once",
    OPTION_ALLOW_ALWAYS: "allow_always",
    "reject_once": "reject_once",
    "reject_always": "reject_always",
}


class AcpError(Exception):
    """Base ACP error."""


class AcpTimeoutError(AcpError):
    """Prompt timed out."""

    def __init__(self, partial_output: str = ""):
        self.partial_output = partial_output
        super().__init__("ACP prompt timed out")


class AcpPermissionNeeded(AcpError):  # noqa: N818
    """Tool approval required."""

    def __init__(self, prompt: str, response_so_far: str = ""):
        self.prompt = prompt
        self.response_so_far = response_so_far
        super().__init__("Permission needed")


class AcpProcessDied(AcpError):  # noqa: N818
    """kiro-cli process exited unexpectedly."""


def _format_acp_error(error: object) -> str:
    """Format a JSON-RPC error from the ACP backend into actionable user text.

    The ACP backend (kiro-cli or claude-agent-acp) surfaces upstream Bedrock
    failures as JSON-RPC ``error`` objects with shape
    ``{"code": int, "message": str, "data": str}``.  The ``data`` field
    typically contains the raw provider error string and a request_id.

    For known failure modes (model unavailable, throttling, auth) we rewrite
    the message into concrete recovery steps.  For everything else we fall
    back to the previous behaviour ``"Prompt error: <raw dict>"`` so we don't
    swallow new error shapes.

    The provider request_id is preserved in every variant so that operators
    can correlate against support tickets and Bedrock logs.

    Security: the ``data`` field originates from upstream and may contain
    credential patterns or exfiltration URLs (especially in the fallback
    path that echoes the raw dict).  The return value is therefore passed
    through ``redact_credentials`` and ``redact_exfiltration_urls`` before
    being raised to the dashboard / Slack / CLI surfaces.
    """
    if isinstance(error, dict):
        data = str(error.get("data", "") or "")
        message = str(error.get("message", "") or "")
        haystack = f"{data} {message}"

        req_id_match = re.search(r"request_id:\s*([0-9a-fA-F-]+)", data)
        req_id_suffix = f" (request_id: {req_id_match.group(1)})" if req_id_match else ""

        # Bedrock model alias resolved to a version that is currently
        # unavailable (capacity throttle, region rollout in progress,
        # deprecated, etc.).
        model_match = re.search(r"[Tt]he model '([^']+)' is not available", data)
        if model_match:
            model = model_match.group(1)
            formatted = (
                f"Model '{model}' is unavailable on Bedrock right now (capacity "
                f"throttle or region rollout). Try: (1) pick a different alias in "
                f"the model picker, (2) edit ~/.claude/settings.json 'model' field "
                f"to a different version (e.g. claude-opus-4-6-v1), or (3) wait a "
                f"minute and retry."
                f"{req_id_suffix}"
            )
        elif re.search(
            r"\b(ThrottlingException|TooManyRequestsException|ServiceQuotaExceededException)\b",
            haystack,
        ) or re.search(r"\b(rate.?limit|throttl(?:e|ed|ing))\b", haystack, re.IGNORECASE):
            # Bedrock throttle / rate limit. Cover both AWS service exception
            # names and the generic phrasing the ACP backend sometimes uses.
            formatted = (
                "Bedrock is throttling requests. Try: (1) wait a few seconds and "
                "retry, or (2) switch to a different model in the picker (e.g. sonnet)."
                f"{req_id_suffix}"
            )
        elif re.search(
            r"\b(AccessDenied(?:Exception)?|UnauthorizedException|ExpiredToken(?:Exception)?"
            r"|InvalidSignatureException|UnrecognizedClientException)\b",
            haystack,
        ):
            # Bedrock auth failure — almost always missing/expired AWS
            # credentials.
            formatted = (
                "Bedrock authentication failed. Refresh your AWS credentials "
                "(e.g. re-run your SSO/login or 'aws sso login'), then retry. If "
                "the failure persists, check that the configured AWS profile has "
                "Bedrock InvokeModel access."
                f"{req_id_suffix}"
            )
        elif re.search(
            r"\b(InternalServerError|InternalFailure|ServiceUnavailable(?:Exception)?"
            r"|DispatchFailure|ConnectionReset(?:Error)?)\b",
            data,
        ) or re.search(
            r"(?:HTTP|status)\s*(?:code\s*)?50[023]\b",
            data,
            re.IGNORECASE,
        ) or re.search(
            r"(please try again|response stream)",
            data,
            re.IGNORECASE,
        ):
            # Transient backend 5xx — Bedrock/Codewhisperer surfaces a
            # momentary InternalServerError (often wrapped in a
            # CodewhispererChatResponseStream ServiceError with a
            # "please try again" hint). Distinct from throttling: this is a
            # server-side blip, not a rate limit, so the guidance is just to
            # retry rather than switch models or back off.
            #
            # Match only the provider `data` field, never the generic
            # JSON-RPC `message`: -32603's canonical message is literally
            # "Internal error", so keying off `message` would sweep *every*
            # uncaught -32603 (malformed-request, context-length, deterministic
            # backend bugs) into this transient bucket and tell the user to
            # retry a condition that will never succeed — while killing the
            # unknown-shape fallback for the most common error code. We also
            # require a real transient token (named exception, HTTP/status-50x,
            # or an explicit retry hint) rather than a bare numeric like
            # "max_tokens 500", which is not a status code.
            formatted = (
                "The model backend hit a transient error (HTTP 5xx). This is "
                "usually momentary — retry in a moment. If it keeps happening, "
                "switch to a different model in the picker."
                f"{req_id_suffix}"
            )
        else:
            # Unknown shape — preserve the raw dict so we don't lose
            # information.  Redaction below scrubs any embedded secrets.
            formatted = f"Prompt error: {error}"
    else:
        formatted = f"Prompt error: {error}"

    # Defense-in-depth: scrub any credentials or suspicious exfiltration URLs
    # that may have been embedded in the upstream provider response before
    # the message reaches dashboard / Slack / CLI surfaces.
    redacted, url_warnings = redact_exfiltration_urls(formatted)
    redacted, cred_warnings = redact_credentials(redacted)
    if url_warnings or cred_warnings:
        # Log so security review can spot when an upstream provider is
        # echoing sensitive content back. The warning lists are bounded
        # (one entry per match) and intentionally do NOT include the matched
        # values themselves — those have already been redacted.
        logger.warning(
            "ACP error contained sensitive content (scrubbed before raise): "
            "%d suspicious url(s), %d credential pattern(s)",
            len(url_warnings),
            len(cred_warnings),
        )
    return redacted


def _get_child_pids(parent_pid: int | None, _visited: set[int] | None = None) -> list[int]:
    """Return PIDs of all descendants recursively (best-effort).

    Uses a visited set to prevent infinite loops from PID cycles.
    On Linux, reads /proc/<pid>/task/*/children (kernel-provided, fast).
    Falls back to pgrep -P on other platforms.
    """
    if not parent_pid:
        return []
    if _visited is None:
        _visited = set()
    if parent_pid in _visited:
        return []
    _visited.add(parent_pid)

    direct = _direct_children(parent_pid)
    all_pids = []
    for cpid in direct:
        if cpid not in _visited:
            all_pids.append(cpid)
            all_pids.extend(_get_child_pids(cpid, _visited))
    return all_pids


def _direct_children(pid: int) -> list[int]:
    """Return direct child PIDs. Uses /proc on Linux, pgrep elsewhere."""
    if sys.platform == "linux":
        try:
            children: list[int] = []
            tasks_dir = Path(f"/proc/{pid}/task")
            if tasks_dir.is_dir():
                for tid in tasks_dir.iterdir():
                    cf = tid / "children"
                    if cf.exists():
                        children.extend(int(p) for p in cf.read_text().split() if p.strip())
            if children:
                return children
        except Exception:
            pass  # fall through to pgrep
    try:
        out = subprocess_mod.check_output(["pgrep", "-P", str(pid)], stderr=subprocess_mod.DEVNULL)
        return [int(p) for p in out.decode().split() if p.strip()]
    except Exception:
        return []


def _get_start_time(pid: int) -> int | None:
    """Read process start time to detect PID recycling."""
    try:
        if sys.platform == "linux":
            stat = Path(f"/proc/{pid}/stat").read_text()
            fields = stat.rsplit(")", 1)[1].split()
            return int(fields[19])  # field 22 = starttime
        # macOS: use ps -o lstart= (absolute start timestamp, constant for process lifetime)
        out = subprocess_mod.check_output(
            ["ps", "-o", "lstart=", "-p", str(pid)], stderr=subprocess_mod.DEVNULL, timeout=2
        )
        return hash(out.strip())  # stable per-process, changes on recycle
    except Exception:
        return None


def _is_our_child(pid: int, expected_start: int | None = None) -> bool:
    """Verify a PID still belongs to an ACP-related process (deny-by-default).

    Uses allowlist on executable basename — only kills processes whose binary
    matches known ACP adapter/MCP runtime names. Returns False for anything else,
    including recycled PIDs.
    """
    allowed_prefixes = (
        b"kiro",
        b"claude",
        b"node",
        b"npx",
        b"python",
        b"ruby",
        b"builder",
        b"aim",
        b"arcc",
        b"deep-research",
    )
    allowed_exact = (b"uv",)
    try:
        if sys.platform == "linux":
            cmdline_path = Path(f"/proc/{pid}/cmdline")
            if not cmdline_path.exists():
                return False
            cmdline = cmdline_path.read_bytes()
            exe = cmdline.split(b"\x00", 1)[0].rsplit(b"/", 1)[-1]
        else:
            out = subprocess_mod.check_output(
                ["ps", "-o", "comm=", "-p", str(pid)], stderr=subprocess_mod.DEVNULL, timeout=2
            )
            exe = out.strip().rsplit(b"/", 1)[-1]
        # Match runtime prefixes, exact names, or any binary with "mcp" in the name
        if not (
            any(exe.startswith(tok) for tok in allowed_prefixes)
            or exe in allowed_exact
            or b"mcp" in exe
        ):
            return False
        # Start-time check: definitive PID recycling detection (always required)
        actual_start = _get_start_time(pid)
        if expected_start is None or actual_start is None:
            logger.debug("PID %d start time unavailable — denying (fail-closed)", pid)
            return False
        if actual_start != expected_start:
            logger.debug("PID %d start time mismatch (recycled)", pid)
            return False
        return True
    except Exception:
        return False


def _kill_escaped_children(child_pids: dict[int, int | None]) -> None:
    """SIGKILL descendants that survived killpg (different PGID). Kills leaf-first."""
    for cpid in reversed(list(child_pids.keys())):
        try:
            os.kill(cpid, 0)  # still alive?
            if not _is_our_child(cpid, expected_start=child_pids.get(cpid)):
                logger.debug("Skipping PID %d — not an ACP/MCP process (recycled?)", cpid)
                continue
            os.kill(cpid, signal.SIGKILL)
            logger.debug("Killed escaped child PID %d", cpid)
        except (ProcessLookupError, OSError):
            pass


def _make_unified_diff(old: str, new: str, path: str, max_len: int = 6000) -> str:
    """Generate a unified diff string from old/new text, handling empty inputs."""
    old_lines = (old if old.endswith("\n") else old + "\n").splitlines(keepends=True) if old else []
    new_lines = (new if new.endswith("\n") else new + "\n").splitlines(keepends=True) if new else []
    udiff = difflib.unified_diff(old_lines, new_lines, fromfile=path, tofile=path, n=3)
    return "".join(udiff).rstrip()[:max_len]


def _select_tool_title(title: object, raw_input: object) -> str | None:
    """Pick the pill label, preferring a human-readable `description` when present.

    Claude Code's Bash tool emits a `description` field alongside `command`
    (e.g. "List KiroClaw ACP module files" rather than `ls /workplace/...`).
    We surface it on the pill when supplied; otherwise we fall back to the
    SDK-provided `title` (the literal tool invocation). Used by both
    `_extract_tool_event` (initial tool_call) and
    `_extract_tool_call_refinement` (the second-phase tool_call_update from
    claude-agent-acp) so the title rule stays consistent across both events.
    """
    if isinstance(raw_input, dict):
        desc = raw_input.get("description")
        if isinstance(desc, str) and desc.strip():
            return desc
    if isinstance(title, str) and title:
        return title
    return None


class AcpClient:
    """JSON-RPC 2.0 client over stdio with kiro-cli acp."""

    def __init__(
        self,
        work_dir: str | Path | None = None,
        model: str | None = None,
        agent: str = CLIENT_NAME,
        sandbox_mode: str = "auto",
        session_key: str | None = None,
        channel_id: str | None = None,
        extra_env: dict[str, str] | None = None,
        acp_backend: str = "",
    ):
        self._work_dir = Path(work_dir) if work_dir else Path.home() / ".kiroclaw" / "workspace"
        self._model = model or DEFAULT_MODEL
        self._agent = agent
        self._sandbox_mode = sandbox_mode
        self._acp_backend = acp_backend
        self._session_key = session_key
        self._channel_id = channel_id
        self._extra_env = extra_env or {}
        self._sandbox_cleanup: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._pid: int | None = None
        self._start_time: int | None = None  # process start time for PID recycling detection
        self._session_id: str | None = None
        self._next_id = 1
        self._buffer: deque[JsonRpcMessage] = deque(maxlen=100)
        self._mcp_notifications: list[JsonRpcMessage] = []
        # MCP OAuth requests collected during session init from
        # `_kiro.dev/mcp/oauth_request` notifications. Drained by callers via
        # `pop_pending_oauth_requests()` after `ensure_ready()` so the UI can
        # surface an Authorize button to the user. Each entry: {"serverName", "oauthUrl"}.
        self._pending_oauth_requests: list[dict[str, str]] = []
        # Server names already surfaced to the UI in this ACP session — kiro-cli
        # may emit `_kiro.dev/mcp/oauth_request` multiple times per server (e.g.
        # once per probe attempt). Dedupe so the user sees one banner per server.
        self._oauth_emitted_servers: set[str] = set()
        self._cancelled = False
        self._cancel_ts: float = 0.0
        # Cooperative-cancel read-grace for the CURRENT cancel. Defaults to the
        # module floor but is raised to the caller's ack budget by
        # cancel_session() so a configured soft_stop_budget_secs > 10 actually
        # extends the window instead of being silently capped (the read loop
        # would otherwise abort the turn at 10s while the soft waiter blocked
        # the full budget, then hard-kill — losing the session).
        self._cancel_grace_secs: float = _CANCEL_GRACE_SECS
        self._resume_session_id: str | None = None
        self._resumed = False
        self._can_load_session = False
        # Models advertised by the backend in the session/new (or session/load)
        # response. claude-agent-acp returns the real versioned Claude list
        # (Opus 4.8/4.7, Sonnet 4.6, …); kiro-cli returns its own. Captured so
        # the dashboard model dropdown reflects what the backend actually
        # offers rather than a hardcoded guess. Each entry: {modelId, name,
        # description}.
        self._available_models: list[dict[str, str]] = []
        self._child_pids: dict[int, int | None] = {}  # pid → start_time snapshot
        self.last_prompt_stats = AcpPromptStats()
        self._tool_call_inputs: dict[str, str] = {}
        # Map JSON-RPC request id → {"once": optionId, "always": optionId} so
        # the host can echo back the exact optionIds the agent advertised.
        # kiro-cli uses "allow_once"/"allow_always"; claude-agent-acp uses
        # "allow"/"allow_always". Falling back to OPTION_ALLOW_ONCE causes
        # claude-agent-acp to reject the response.
        self._permission_options: dict[str | int, dict[str, str]] = {}
        self._stderr_lines: deque[str] = deque(maxlen=20)
        self._jsonl_pos: int = 0  # track read position in session JSONL for tool results
        self._stderr_task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._last_activity: float = time.monotonic()
        self._turn_done: asyncio.Event = asyncio.Event()
        # Serializes whole read turns on this client's single stdout StreamReader.
        # An asyncio StreamReader permits exactly ONE waiting reader; the shared
        # `_bg` session is streamed by ~8 callers and the per-session Semaphore(1)
        # does not cover abnormal-exit overlap (a turn dying mid-readline leaves a
        # parked read the next caller collides with -> "readuntil() called while
        # another coroutine is already waiting"). Acquired at the top of
        # _prompt_loop, released in its finally.
        #
        # Finalization caveat: a consumer that `return`s on "complete" without
        # exhausting _prompt_loop leaves the async-gen SUSPENDED; CPython runs
        # its finally via a *deferred* scheduled athrow (next loop tick), not at
        # the consumer's return. So the lock releases promptly on a live loop but
        # NOT synchronously. send_message_stream wraps the loop in `aclosing(...)`
        # to force deterministic release on its hot path; the other consumers
        # rely on the next-tick finalization.
        #
        # Coverage caveat: this lock only covers reads inside _prompt_loop.
        # _read_message also has callers OUTSIDE the loop (_wait_for_response
        # during init, wait_for_compaction). Those run in distinct lifecycle
        # phases that do not overlap a streaming _bg turn, so they are not
        # serialized here; if that ever changes, the readuntil race could recur.
        self._turn_lock: asyncio.Lock = asyncio.Lock()
        self._stale_eligible: bool = False  # set by _dispatch_events after text chunks
        # Set when a tool_call is yielded, cleared when the tool resolves
        # (tool_call_update result) or the turn starts/completes.  NOT cleared
        # on arbitrary inbound frames — that would disarm the watchdog after a
        # single progress frame.  Gates the _TOOL_STALL_TIMEOUT watchdog so a
        # dispatched-but-never-resolved tool can't hang the whole turn.
        self._tool_dispatched: bool = False
        self._last_stop_reason: str = ""
        # Dynamic config from ACP session/new response and config_option_update notifications.
        # Only the effort configOptions are consumed (model lists come from
        # _capture_available_models, which parses the real dict-shaped `models`).
        self._acp_config_options: list[dict] = []

    @property
    def backend(self) -> str:
        """ACP backend identifier (e.g. ACP_BACKEND_CLAUDE for claude-agent-acp)."""
        return getattr(self, "_acp_backend", "")

    @property
    def _is_claude(self) -> bool:
        return self.backend == ACP_BACKEND_CLAUDE

    @property
    def is_ready(self) -> bool:
        return self._process is not None and self._session_id is not None

    def _is_process_alive(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def is_process_alive(self) -> bool:
        """True if the underlying process exists and has not exited."""
        return self._is_process_alive()

    @property
    def exit_code(self) -> int | None:
        """Return the process exit code, or None if still running / never started."""
        return self._process.returncode if self._process else None

    def is_responsive(self, stale_threshold: float = 600.0) -> bool:
        """True if process is alive AND has had I/O activity within threshold seconds."""
        if not self._is_process_alive():
            return False
        return (time.monotonic() - self._last_activity) < stale_threshold

    def touch_activity(self) -> None:
        """Refresh _last_activity without I/O. Used by long-running MCP tools
        (e.g. the `wait` tool) to prevent is_responsive() from flagging a
        deliberately-idle session as stale and triggering SIGTERM."""
        self._last_activity = time.monotonic()

    @property
    def resumed(self) -> bool:
        """True if the last session was restored via session/load."""
        return self._resumed

    def set_resume_session_id(self, sid: str) -> None:
        """Set a kiro-cli session ID to restore via session/load on next ensure_ready()."""
        self._resume_session_id = sid

    def rekey(self, session_key: str, channel_id: str | None = None) -> None:
        """Re-key this client for a different session (used by warm pool)."""
        self._session_key = session_key
        self._channel_id = channel_id
        self._last_activity = time.monotonic()

    async def set_model(self, model_id: str) -> None:
        """Switch model on a running session (used by warm pool post-claim)."""
        if not self._session_id:
            raise AcpError("Cannot set model before session is initialized")
        if self._is_claude:
            await self.set_config_option("model", model_id)
        else:
            await self._send_request(
                METHOD_SET_MODEL,
                {"sessionId": self._session_id, "modelId": model_id},
            )
        self._model = model_id

    def _capture_available_models(self, session_resp: dict) -> None:
        """Record the model list the backend advertised in a session response.

        The ACP ``session/new`` / ``session/load`` response carries a
        ``models`` object ``{availableModels: [{modelId, name, description}],
        currentModelId}``. We keep the list so the dashboard dropdown shows the
        real backend models (e.g. the versioned Claude list from
        claude-agent-acp) instead of a hardcoded guess. Best-effort and never
        raises — a backend that omits ``models`` simply leaves the list empty.
        """
        models = session_resp.get("models")
        if not isinstance(models, dict):
            return
        advertised = models.get("availableModels")
        if not isinstance(advertised, list):
            return
        captured: list[dict[str, str]] = []
        for m in advertised:
            if not isinstance(m, dict):
                continue
            model_id = m.get("modelId") or m.get("value") or ""
            if not model_id:
                continue
            captured.append(
                {
                    "modelId": str(model_id),
                    "name": str(m.get("name") or model_id),
                    "description": str(m.get("description") or ""),
                }
            )
        if captured:
            self._available_models = captured

    def available_models(self) -> list[dict[str, str]]:
        """Models advertised by the backend at session init (may be empty)."""
        return list(self._available_models)

    async def set_config_option(self, config_id: str, value: str) -> None:
        """Set a session config option (e.g. effort level) via session/set_config_option."""
        if not self._session_id:
            raise AcpError("Cannot set config option before session is initialized")
        req_id = await self._send_request(
            "session/set_config_option",
            {"sessionId": self._session_id, "configId": config_id, "value": value},
        )
        await self._wait_for_response(req_id, timeout=10.0)

    # ── Dynamic Config from ACP ──

    def _store_session_config(self, resp: dict) -> None:
        """Extract effort configOptions from a session/new or session/load response.

        Model lists are captured separately by ``_capture_available_models``,
        which parses the real dict-shaped ``models`` payload
        (``{availableModels: [...]}``); only the ``configOptions`` effort
        selector is consumed here.
        """
        logger.debug("_store_session_config keys: %s", list(resp.keys()))
        config_options = resp.get("configOptions")
        if isinstance(config_options, list):
            self._acp_config_options = config_options
            logger.debug("ACP config options loaded: %d entries", len(config_options))
            self._sync_effort_levels()

    def _handle_config_option_update(self, msg: JsonRpcMessage) -> None:
        """Process a config_option_update session notification.

        ACP emits this when config changes (e.g. model switch rebuilds effort options).
        The payload is a full configOptions array that replaces the previous one.
        """
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict):
            return
        config_options = update.get("configOptions")
        if isinstance(config_options, list):
            self._acp_config_options = config_options
            logger.debug("ACP config options updated: %d entries", len(config_options))
            self._sync_effort_levels()

    def _sync_effort_levels(self) -> None:
        """Push ACP-reported effort levels to the global validation set."""
        levels = self.get_valid_effort_levels()
        if levels:
            # circular import: chat_persistence → dashboard → session → acp.client
            from kiro_claw.dashboard.chat_persistence import update_reasoning_effort_values

            update_reasoning_effort_values(levels)

    @property
    def acp_config_options(self) -> list[dict]:
        """Config options reported by ACP (effort, model, mode selectors)."""
        return self._acp_config_options

    def supports_config_option(self, config_id: str) -> bool:
        """Whether the session advertised a config option with this id.

        Older claude-agent-acp builds do not expose an ``effort`` selector at
        all; pushing ``session/set_config_option`` for it then fails with
        ``Unknown config option`` (a -32603 Internal error, distinct from a
        value-level rejection). Callers gate on this so an adapter that lacks
        the option is a silent no-op rather than a noisy error + session reset.

        Returns True when no config options were reported yet, so that a
        backend which advertises options lazily (after the first turn) is not
        permanently treated as unsupported.
        """
        if not self._acp_config_options:
            return True
        return any(
            isinstance(opt, dict) and opt.get("id") == config_id for opt in self._acp_config_options
        )

    def get_valid_effort_levels(self) -> list[str]:
        """Return valid effort levels from ACP config, preserving ACP order.

        Parses configOptions for the entry with id="effort" and extracts its
        options[].value list in the order ACP reported them.
        """
        for opt in self._acp_config_options:
            if not isinstance(opt, dict):
                continue
            if opt.get("id") == "effort":
                options = opt.get("options", [])
                if isinstance(options, list):
                    return [o["value"] for o in options if isinstance(o, dict) and "value" in o]
        return []

    def _next_req_id(self) -> int:
        rid = self._next_id
        self._next_id += 1
        return rid

    # ── Process Management ──

    async def _spawn(self) -> None:
        """Start the ACP backend subprocess with stdio pipes.

        KiroClaw's public core only ever drives the kiro-cli backend. The
        claude-agent-acp branch below is the dormant protocol seam (see
        ``ACP_BACKEND_CLAUDE``): the public provider factory never selects it,
        so it is unreachable here, but an internal companion that re-registers
        the Claude Code provider reuses this same client over the seam.
        """
        self._work_dir.mkdir(parents=True, exist_ok=True)

        if self._is_claude:
            # Dormant seam — see method docstring. Binary resolution only; the
            # ~/.claude registration glue (settings.local.json, the MCP-registry
            # reader) lived in the deleted cc_agent module and is re-added by the
            # internal companion, not the public core.
            global _claude_acp_argv_cache  # noqa: PLW0603
            if _claude_acp_argv_cache is _UNRESOLVED:
                _claude_acp_argv_cache = await asyncio.to_thread(_resolve_claude_acp_bin)
            claude_argv = _claude_acp_argv_cache
            if not isinstance(claude_argv, list) or not claude_argv:
                raise AcpError(
                    f"{CLAUDE_ACP_BIN} not found. Install it with "
                    f"'npm i -g {CLAUDE_ACP_NPM_PKG}' (or add it as a project "
                    f"dependency), or set CLAUDE_AGENT_ACP_BIN to its entry "
                    f"script."
                )
            argv: list[str] = claude_argv
        else:
            kiro_bin = _resolve_kiro_bin()
            if not kiro_bin:
                raise AcpError(f"{KIRO_CLI_BIN} not found in PATH")
            argv = [kiro_bin, KIRO_CLI_SUBCMD, "--agent", self._agent]

        # OS-level sandbox: wrap the command to hide sensitive paths
        argv, self._sandbox_cleanup = wrap_argv(argv, mode=self._sandbox_mode)

        # Process group isolation: start_new_session=True (calls setsid, enables killpg)
        env = {**os.environ}
        if self._extra_env:
            env.update(self._extra_env)
        env["PATH"] = augmented_path(env.get("PATH", ""))
        if self._is_claude and not env.get("CLAUDE_CODE_EXECUTABLE"):
            # Dormant seam (see _spawn docstring): the adapter's SDK needs a
            # native Claude binary we don't vendor and does NOT search PATH for
            # `claude` itself, so point it at one explicitly when the seam is
            # driven. Only set when unset so an operator override always wins.
            claude_exe = _resolve_claude_code_executable()
            if claude_exe:
                env["CLAUDE_CODE_EXECUTABLE"] = claude_exe
            else:
                logger.warning(
                    "%s not found on PATH; the claude-agent-acp adapter will "
                    "fail with 'Claude native binary not found'. Set "
                    "CLAUDE_CODE_EXECUTABLE.",
                    CLAUDE_CODE_BIN,
                )
        if self._session_key:
            env["KIROCLAW_SESSION_KEY"] = self._session_key
        else:
            env.pop("KIROCLAW_SESSION_KEY", None)
        if self._channel_id:
            env["KIROCLAW_CHANNEL_ID"] = self._channel_id
        else:
            env.pop("KIROCLAW_CHANNEL_ID", None)

        # Resolve SSH_AUTH_SOCK dynamically — the gateway's env may be stale
        # after an ssh-agent restart.
        _resolve_ssh_auth_sock(env)
        # Resolve KRB5CCNAME to a FILE: ccache — the kernel keyring (AL2023
        # default) is invisible to this child, so Kerberos-gated MCP servers
        # (e.g. amazon-quick-mcp) fail without it. Covers the session agent and
        # all ACP-provider subagents, which spawn through this same path.
        resolve_krb5_ccname(env)

        kwargs: dict = {
            "stdin": asyncio.subprocess.PIPE,
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": str(self._work_dir),
            "limit": _STDOUT_BUFFER_LIMIT,
            "start_new_session": True,
            "env": env,
        }

        self._process = await asyncio.create_subprocess_exec(
            *argv,
            **kwargs,
        )
        self._pid = self._process.pid
        self._start_time = _get_start_time(self._pid)
        _spawn_label = (
            "claude-agent-acp" if self._is_claude else f"{KIRO_CLI_BIN} {KIRO_CLI_SUBCMD}"
        )
        logger.info("Spawned %s (PID %d)", _spawn_label, self._pid)
        # Track root PID and do an early descendant scan.  kiro-cli forks
        # child processes quickly after launch.  Recording them here means
        # _kill_process() can clean up even if _initialize_session() fails.
        from kiro_claw.session import (  # circular: session -> config.loader -> providers.acp -> acp.client
            _track_child_pids,
            _track_pid,
            _track_session_pid,
        )

        _track_pid(self._pid)
        _track_session_pid(self._pid)  # separate file for startup cleanup
        await asyncio.sleep(0.3)
        early_descendants = _get_child_pids(self._pid)
        if early_descendants:
            self._child_pids = {p: _get_start_time(p) for p in early_descendants}
            _track_child_pids(self._child_pids, parent_pid=self._pid or 0)
            logger.info("Early tracking %d descendants of PID %d", len(self._child_pids), self._pid)

        if self._process.stderr:
            self._stderr_task = asyncio.ensure_future(self._drain_stderr(self._process.stderr))

    async def _drain_stderr(self, stderr: asyncio.StreamReader) -> None:
        # Count of suppressed high-frequency marker lines (see
        # _SUPPRESSED_STDERR_MARKERS) and the monotonic timestamp of the last
        # throttled summary, so a thinking burst is observable in the log
        # without re-introducing the per-delta flood it replaced.
        suppressed = 0
        last_summary = time.monotonic()
        while True:
            line = await stderr.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if not text:
                continue
            # Liveness must advance for EVERY line, including suppressed ones:
            # the adapter is provably alive while emitting them, and the idle
            # watchdog (is_responsive) must not kill an actively-thinking turn.
            # One monotonic read, reused for the throttle check below.
            now = time.monotonic()
            self._last_activity = now
            if any(marker in text for marker in _SUPPRESSED_STDERR_MARKERS):
                # Drop the line: no per-occurrence WARNING, and crucially do not
                # append to the bounded _stderr_lines ring buffer — otherwise a
                # thinking burst evicts the last real errors from diagnostics.
                suppressed += 1
                if now - last_summary >= _SUPPRESSED_STDERR_SUMMARY_INTERVAL_SECS:
                    logger.debug("suppressed %d adapter stderr marker line(s)", suppressed)
                    suppressed = 0
                    last_summary = now
                continue
            self._stderr_lines.append(text)
            redacted, _ = redact_exfiltration_urls(text)
            redacted, _ = redact_credentials(redacted)
            _bin_label = "claude-acp" if self._is_claude else KIRO_CLI_BIN
            logger.warning("%s stderr: %s", _bin_label, redacted)
        if suppressed:
            # Flush the residual count once the stream closes so the final burst
            # is still accounted for.
            logger.debug("suppressed %d adapter stderr marker line(s)", suppressed)

    async def _snapshot_process_tree(self) -> None:
        """Discover and track the full process tree after MCP servers are loaded.

        Merges with any early snapshot taken in _spawn().  MCP servers
        (builder-mcp, node) may not exist until after _initialize_session().
        """
        descendants = _get_child_pids(self._pid)
        if not descendants:
            # Retry once — children may not have forked yet
            await asyncio.sleep(0.5)
            descendants = _get_child_pids(self._pid)

        for p in descendants:
            if p not in self._child_pids:
                self._child_pids[p] = _get_start_time(p)

        if self._child_pids:
            from kiro_claw.session import _track_child_pids

            _track_child_pids(self._child_pids, parent_pid=self._pid or 0)
            logger.info("Tracked %d descendant PIDs for PID %d", len(self._child_pids), self._pid)

    async def _kill_process(self, *, force: bool = False) -> None:
        """Kill the subprocess and wait for it to exit.

        Uses process groups (killpg) for clean tree kill, then sweeps
        child PIDs that escaped to a different PGID.

        Args:
            force: If True, kill immediately (used during shutdown).
        """
        if not self._process or self._process.returncode is not None:
            return
        pid = self._pid
        # Close pipes first to unblock any pending reads/writes
        for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
            if pipe:
                try:
                    pipe.close()  # type: ignore[union-attr]
                except Exception:
                    pass

        # Snapshot child PIDs before killing — children in different
        # process groups survive killpg (kiro-cli-chat acp leak).
        # Merge stored snapshot (from init, catches reparented-to-init PIDs)
        # with fresh scan (catches children spawned after init).
        fresh = _get_child_pids(pid)
        stored = self._child_pids
        # Build merged dict: pid → start_time (stored has start times, fresh doesn't)
        merged: dict[int, int | None] = dict(stored)
        for p in fresh:
            if p not in merged:
                merged[p] = _get_start_time(p)

        if not force:
            try:
                os.killpg(os.getpgid(pid), signal.SIGTERM)  # type: ignore[arg-type]
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(self._process.wait(), timeout=3.0)
                _kill_escaped_children(merged)
                return
            except asyncio.TimeoutError:
                pass
        # Force kill
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)  # type: ignore[arg-type]
        except (ProcessLookupError, OSError):
            try:
                self._process.kill()
            except (ProcessLookupError, OSError):
                pass
        _kill_escaped_children(merged)
        try:
            await asyncio.wait_for(self._process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            logger.warning("PID %s did not exit after force kill", pid)

    def _reset_state(self) -> None:
        """Reset all session state (call after process is dead)."""
        if self._process:
            for pipe in (self._process.stdin, self._process.stdout, self._process.stderr):
                if pipe:
                    try:
                        pipe.close()  # type: ignore[union-attr]
                    except Exception:
                        pass
        # Clean up sandbox temp files (macOS seatbelt profile)
        if self._sandbox_cleanup:
            try:
                os.remove(self._sandbox_cleanup)
            except OSError:
                pass
            self._sandbox_cleanup = None
        # Remove settings.local.json so bypassPermissions doesn't persist after crash
        if self._is_claude:
            _stale = self._work_dir / ".claude" / "settings.local.json"
            try:
                _stale.unlink(missing_ok=True)
            except OSError:
                pass
        # Save PIDs before clearing state — needed for untracking
        saved_pid = self._pid
        saved_child_pids = self._child_pids
        self._process = None
        self._pid = None
        self._session_id = None
        self._buffer.clear()
        self._stderr_lines.clear()
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
        self._stderr_task = None
        self._cancelled = False
        self._cancel_ts = 0.0
        self._cancel_grace_secs = _CANCEL_GRACE_SECS
        self._resumed = False
        self._turn_done = asyncio.Event()
        self._last_stop_reason = ""
        self._pending_oauth_requests.clear()
        self._oauth_emitted_servers.clear()
        # Untrack child PIDs from the orphan tracking file
        if saved_child_pids:
            try:
                from kiro_claw.session import _untrack_child_pids

                _untrack_child_pids(saved_child_pids)
            except Exception:
                pass
        # Untrack parent kiro-cli PID
        if saved_pid is not None:
            try:
                from kiro_claw.session import _untrack_pid

                _untrack_pid(saved_pid)
            except Exception:
                pass
            try:
                from kiro_claw.session import _untrack_session_pid

                _untrack_session_pid(saved_pid)
            except Exception:
                pass
        self._child_pids = {}

    async def _initialize_session(self) -> None:
        """Handshake: initialize → session/load or session/new → set_mode → set_model."""
        # 1. Initialize
        protocol_version: int | str = (
            PROTOCOL_VERSION_CLAUDE if self._is_claude else PROTOCOL_VERSION
        )
        init_id = await self._send_request(
            METHOD_INITIALIZE,
            {
                "protocolVersion": protocol_version,
                "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            },
        )
        init_resp = await self._wait_for_response(init_id, timeout=_INIT_TIMEOUT)
        logger.info("ACP initialized (protocol=%s)", init_resp.get("protocolVersion"))

        # Check if kiro-cli supports session/load
        self._can_load_session = init_resp.get("agentCapabilities", {}).get("loadSession", False)

        # 2. Try session/load if we have a resume ID and kiro-cli supports it
        self._resumed = False
        resume_sid = self._resume_session_id
        self._resume_session_id = None  # consume — no retry loop

        if resume_sid and self._can_load_session:
            # Only attempt session/load when the prior session transcript
            # actually exists on disk. Without this guard a stale persisted SID
            # (e.g. a slot reopened for a brand-new conversation) triggers a
            # session/load that REPLAYS the old transcript on top of the fresh
            # system prompt + memory injection — inflating base context to
            # ~38% on turn 1. kiro-cli stores transcripts at ~/.kiro/sessions/
            # cli/<sid>.json; a missing transcript falls back to session/new
            # (a genuinely fresh start).
            if self._is_claude:
                # Dormant seam: claude session/load takes no file path, and the
                # SDK transcript-path resolver lived in the deleted cc cleanup
                # helper. The internal companion re-adds it; the public core
                # simply attempts the load.
                session_file = ""
                file_ok = True
            else:
                session_file = str(
                    Path.home() / ".kiro" / "sessions" / "cli" / f"{resume_sid}.json"
                )
                file_ok = Path(session_file).exists()
            if file_ok:
                try:
                    load_params: dict = {
                        "sessionId": resume_sid,
                        "cwd": str(self._work_dir),
                        # kiro-cli gets its servers via --agent; the claude
                        # backend must receive them here (it does not read
                        # kiroclaw.mcp.json itself).
                        "mcpServers": [],
                    }
                    if self._is_claude:
                        load_params["_meta"] = {"claudeCode": {"options": {}}}
                    else:
                        load_params["_meta"] = {"_kiro.dev/session_file": session_file}
                    load_id = await self._send_request(METHOD_SESSION_LOAD, load_params)
                    load_resp = await self._wait_for_response(load_id, timeout=_INIT_TIMEOUT)
                    if "modes" in load_resp:
                        self._session_id = resume_sid
                        self._resumed = True
                        self._capture_available_models(load_resp)
                        self._store_session_config(load_resp)
                        logger.info("ACP session resumed: %s", resume_sid)
                except (AcpError, AcpTimeoutError):
                    logger.info(
                        "session/load failed for %s, falling back to session/new", resume_sid
                    )
            else:
                logger.info("Session file missing for %s, skipping load", resume_sid)

        # 3. Create new session if load didn't succeed
        if not self._session_id:
            new_params: dict = {
                "cwd": str(self._work_dir),
                # kiro-cli loads servers from --agent; claude-agent-acp must be
                # told here — it does not read kiroclaw.mcp.json on its own.
                "mcpServers": [],
            }
            if self._is_claude:
                new_params["_meta"] = {"claudeCode": {"options": {}}}
            session_id = await self._send_request(METHOD_SESSION_NEW, new_params)
            session_resp = await self._wait_for_response(session_id, timeout=_INIT_TIMEOUT)
            self._session_id = session_resp.get("sessionId")
            self._capture_available_models(session_resp)
            self._store_session_config(session_resp)
            logger.info("ACP session created: %s", self._session_id)
        self._last_activity = time.monotonic()

        # Seek to end of JSONL so we only read new tool results.
        # claude-agent-acp stores sessions via Claude Code SDK, not ~/.kiro/ — skip.
        if self._session_id and not self._is_claude:
            _jpath = Path.home() / ".kiro" / "sessions" / "cli" / f"{self._session_id}.jsonl"
            try:
                self._jsonl_pos = _jpath.stat().st_size if _jpath.exists() else 0
            except OSError:
                self._jsonl_pos = 0

        # 4. Activate agent via set_mode (claude-agent-acp does not support set_mode — skip).
        if not self._is_claude:
            await self._send_request(
                METHOD_SET_MODE,
                {"sessionId": self._session_id, "modeId": self._agent},
            )
            logger.info("ACP agent activated: %s", self._agent)

        # 5. Set model — override if KiroClaw config specifies non-default.
        if self._model and self._model != DEFAULT_MODEL:
            if self._is_claude:
                await self.set_config_option("model", self._model)
            else:
                await self._send_request(
                    METHOD_SET_MODEL,
                    {"sessionId": self._session_id, "modelId": self._model},
                )
            logger.info("ACP model: %s", self._model)
        else:
            logger.info("ACP model: %s (from agent config)", self._model or "auto")

        # Drain MCP server init notifications
        await self._drain_notifications()

    async def ensure_ready(self) -> None:
        """Ensure process is spawned and session is initialized."""
        # Re-create cwd in case it was deleted after spawn.
        self._work_dir.mkdir(parents=True, exist_ok=True)
        if self._process and self._process.returncode is None and self._session_id:
            return

        # Retry once — kiro-cli first launch can be slow (MCP server init),
        # and transient failures (MCP crash, bad config read) are recoverable.
        for attempt in range(2):
            try:
                if self._process and self._process.returncode is not None:
                    self._reset_state()

                if not self._process:
                    await self._spawn()

                await self._initialize_session()
                try:
                    await self._snapshot_process_tree()
                except Exception:
                    logger.warning("Failed to snapshot process tree", exc_info=True)

                return
            except (AcpTimeoutError, AcpError) as exc:
                if attempt == 0:
                    logger.warning("ACP init failed (%s), retrying with fresh process...", exc)
                    await self._kill_process(force=True)
                    self._reset_state()
                else:
                    await self._kill_process(force=True)
                    self._reset_state()
                    raise

    async def shutdown(self) -> None:
        """Gracefully stop the ACP process."""
        await self._kill_process(force=True)
        self._reset_state()  # untracks all PIDs (root + children)

    # ── JSON-RPC Transport ──

    async def _send_request(self, method: str, params: dict) -> int:
        if not self._process or not self._process.stdin:
            raise AcpError("ACP process not running")

        req_id = self._next_req_id()
        req = JsonRpcRequest(method=method, params=params, id=req_id)
        data = json.dumps(req.to_dict()) + "\n"
        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
        self._last_activity = time.monotonic()
        return req_id

    async def _send_response(self, request_id: str | int, result: dict) -> None:
        if not self._process or not self._process.stdin:
            raise AcpError("ACP process not running")

        msg = {"jsonrpc": "2.0", "id": request_id, "result": result}
        data = json.dumps(msg) + "\n"
        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
        self._last_activity = time.monotonic()

    async def _send_error(self, request_id: str | int, code: int, message: str) -> None:
        """Send a JSON-RPC 2.0 error response for a server→client request.

        Used to answer an unrecognized inbound request (e.g. ``fs/read_text_file``,
        ``terminal/create``) with ``-32601 Method not found`` so the agent fails
        fast instead of blocking forever waiting for a response we'd never send.
        """
        if not self._process or not self._process.stdin:
            raise AcpError("ACP process not running")

        msg = {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
        data = json.dumps(msg) + "\n"
        try:
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AcpProcessDied(f"ACP process pipe broken: {exc}") from exc
        self._last_activity = time.monotonic()

    async def _read_message(self, timeout: float = _READ_TIMEOUT) -> JsonRpcMessage | None:
        if self._cancelled:
            if time.monotonic() - self._cancel_ts > self._cancel_grace_secs:
                raise AcpError("Cancel grace window exceeded; agent unresponsive")

        if self._buffer:
            return self._buffer.popleft()

        if not self._process or not self._process.stdout:
            raise AcpError("ACP process not running")

        try:
            line = await asyncio.wait_for(self._process.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except (ValueError, asyncio.LimitOverrunError) as exc:
            # A single JSON-RPC line exceeded the stdout StreamReader buffer
            # (_STDOUT_BUFFER_LIMIT). asyncio leaves the stream in a corrupted
            # state after an overrun — every subsequent read also fails — so
            # treat the process as dead and let session recovery respawn it
            # instead of freezing the session on an unhandled exception.
            raise AcpProcessDied(
                f"ACP stdout line exceeded {_STDOUT_BUFFER_LIMIT}-byte buffer: {exc}"
            ) from exc

        if not line:
            # EOF — process likely died or closing. Check and avoid busy-loop.
            if self._process and self._process.returncode is not None:
                if self._stderr_task and not self._stderr_task.done():
                    try:
                        await asyncio.wait_for(self._stderr_task, timeout=0.5)
                    except (Exception, asyncio.CancelledError):
                        pass
                stderr_tail = "; ".join(self._stderr_lines) if self._stderr_lines else ""
                if stderr_tail:
                    from kiro_claw.security import redact_credentials, redact_exfiltration_urls

                    stderr_tail, _ = redact_exfiltration_urls(stderr_tail)
                    stderr_tail, _ = redact_credentials(stderr_tail)
                detail = f" — {stderr_tail}" if stderr_tail else ""
                raise AcpError(f"ACP process exited (code={self._process.returncode}){detail}")
            await asyncio.sleep(0.1)
            return None

        text = line.decode(errors="replace").strip()
        if not text:
            return None

        self._last_activity = time.monotonic()

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON line from ACP: %.100s", text)
            return None

        return JsonRpcMessage(
            id=data.get("id"),
            method=data.get("method"),
            result=data.get("result"),
            error=data.get("error"),
            params=data.get("params"),
        )

    async def _wait_for_response(self, req_id: int, timeout: float = 50.0) -> dict:
        """Block until a JSON-RPC response matching *req_id* arrives.

        Explicitly classifies JSON-RPC 2.0 messages with the same method-aware
        discipline as ``JsonRpcMessage.is_response_for`` (a *response* has an
        id + no method; a *request* has both id AND method):

        - Notifications (method + no id): buffered in ``_mcp_notifications`` for
          ``_drain_notifications`` to process.
        - Matching response (id == req_id + no method): returned.
        - Server→client request (method + id) and foreign-id responses
          (id != req_id + no method): collected into a LOCAL ``deferred`` list
          and re-injected into ``self._buffer`` IN ORDER once the matching
          response arrives or on timeout.

        Server requests and foreign responses must NOT be re-appended to
        ``self._buffer`` mid-loop and ``continue``-d: ``_read_message`` pops
        ``self._buffer`` first, so it would immediately re-read the same frame,
        re-defer it, and spin until the deadline (the original bug). Holding
        them in a local list until exit guarantees forward progress while
        preserving the frame so a later ``_prompt_loop`` / ``_process_message``
        can answer the deferred ``session/request_permission`` request.

        The deadline is **activity-based**: any received message (notification,
        deferred frame, or the matching response) resets it to ``now + timeout``,
        bounded by an absolute ``_WAIT_RESPONSE_MAX_TIMEOUT`` safety cap. This
        keeps a long ``session/load`` replay (which streams the entire prior
        transcript as ``session/update`` notifications before resolving) alive
        instead of being killed by a fixed wall-clock and silently falling back
        to ``session/new``.
        """
        from kiro_claw import shutdown_event

        start = time.monotonic()
        deadline = start + timeout
        hard_deadline = start + max(timeout, _WAIT_RESPONSE_MAX_TIMEOUT)
        # Frames that are not the awaited response but must survive this call.
        deferred: list[JsonRpcMessage] = []

        def _reinject() -> None:
            # Re-inject in order at the FRONT of the buffer so a later
            # _prompt_loop / _process_message sees them before newer frames.
            for d in reversed(deferred):
                self._buffer.appendleft(d)
            deferred.clear()

        while time.monotonic() < deadline and time.monotonic() < hard_deadline:
            if shutdown_event.is_set():
                _reinject()
                raise AcpError("Shutdown in progress")
            remaining = min(deadline, hard_deadline) - time.monotonic()
            if remaining <= 0:
                break
            msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
            if msg is None:
                continue
            # Activity-based deadline: extend on any received frame, capped by
            # the hard safety deadline. Safe for init/handshake callers — only
            # extends while the agent is actively sending us data.
            deadline = min(time.monotonic() + timeout, hard_deadline)
            if msg.is_response_for(req_id):
                _reinject()
                if msg.error:
                    raise AcpError(f"JSON-RPC error: {msg.error}")
                return msg.result or {}
            # Notification (has method, no id) — buffer for drain.
            if msg.method and msg.id is None:
                self._mcp_notifications.append(msg)
                logger.debug("Buffered notification: %s (req=%d)", msg.method, req_id)
                continue
            # Server→client request (method AND id) or a foreign-id response
            # (id != req_id, no method). Defer locally — do NOT re-append to
            # self._buffer here, that would spin (see docstring). Re-injected
            # in order on return/raise so the permission request survives.
            if msg.method:
                logger.debug(
                    "Deferring inbound server request: method=%s id=%s (waiting for %d)",
                    msg.method,
                    msg.id,
                    req_id,
                )
            else:
                logger.debug(
                    "Deferring non-matching response: id=%s (waiting for %d)", msg.id, req_id
                )
            deferred.append(msg)

        _reinject()
        raise AcpTimeoutError()

    async def _drain_notifications(
        self,
        duration: float = _DRAIN_DURATION,
        idle_exit: float = _DRAIN_IDLE_EXIT,
    ) -> None:
        """Drain init notifications (buffered + live) and log MCP servers.

        Captures `_kiro.dev/mcp/oauth_request` into `_pending_oauth_requests` so
        callers can surface an Authorize prompt after `ensure_ready()` returns.

        Exits early once no notification has arrived for ``idle_exit`` seconds
        (MCP servers have gone quiet), bounded by the ``duration`` hard cap. This
        avoids always waiting the full cap on the common fast path while still
        giving genuinely slow servers up to ``duration`` to report in.
        """
        deadline = time.monotonic() + duration
        last_activity = time.monotonic()
        drained = 0
        mcp_servers: list[str] = []

        def _capture_oauth(msg: JsonRpcMessage) -> None:
            if not msg.is_method(METHOD_MCP_OAUTH_REQUEST):
                return
            params = msg.params if isinstance(msg.params, dict) else {}
            server_name = str(params.get("serverName") or params.get("name") or "")
            oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
            # Drop unsafe-scheme URLs *before* recording dedupe so a later safe
            # retry for the same server still gets through.
            if not _is_safe_oauth_url(oauth_url):
                if oauth_url:
                    logger.warning(
                        "ACP: refusing unsafe MCP OAuth URL for %s", server_name or "(unknown)"
                    )
                return
            # Without a server_name we can't reliably correlate this banner with
            # the matching server_initialized/server_init_failure notification —
            # the discard path keys on server_name only.  Drop rather than risk
            # a permanently-stuck dedupe entry.
            if not server_name:
                logger.warning("ACP: dropping MCP OAuth request with empty serverName")
                return
            if server_name in self._oauth_emitted_servers:
                logger.debug("ACP: dropping duplicate MCP OAuth request for %s", server_name)
                return
            self._oauth_emitted_servers.add(server_name)
            self._pending_oauth_requests.append({"serverName": server_name, "oauthUrl": oauth_url})
            logger.info("ACP: MCP OAuth request for %s", server_name)

        def _capture_config_update(msg: JsonRpcMessage) -> None:
            if not msg.is_method(METHOD_SESSION_UPDATE):
                return
            params = msg.params or {}
            update = params.get("update", {})
            if isinstance(update, dict) and update.get("sessionUpdate") == UPDATE_CONFIG_OPTION:
                self._handle_config_option_update(msg)

        # Process notifications buffered during _wait_for_response
        for msg in self._mcp_notifications:
            drained += 1
            _capture_oauth(msg)
            _capture_config_update(msg)
            name = ""
            if isinstance(msg.params, dict):
                name = msg.params.get("name") or msg.params.get("serverName") or ""
            if name or "mcp" in (msg.method or ""):
                mcp_servers.append(name or msg.method or "unknown")
        self._mcp_notifications.clear()

        while True:
            # Single time snapshot per iteration so the deadline and idle checks
            # can't diverge on a loaded host (CR feedback).
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break
            # Early-exit once servers have been quiet for the idle window. Poll in
            # idle-sized slices (capped by remaining) so we notice quiet promptly.
            idle_remaining = idle_exit - (now - last_activity)
            if idle_remaining <= 0:
                break
            try:
                read_msg = await self._read_message(
                    timeout=min(remaining, idle_remaining, 2.0)
                )
                if not read_msg:
                    continue
                # Any received message counts as activity (servers still talking),
                # resetting the idle window even if it carries no method.
                last_activity = time.monotonic()
                if not read_msg.method:
                    continue
                drained += 1
                _capture_oauth(read_msg)
                _capture_config_update(read_msg)
                if "mcp" in (read_msg.method or ""):
                    name = ""
                    if isinstance(read_msg.params, dict):
                        name = (
                            read_msg.params.get("name") or read_msg.params.get("serverName") or ""
                        )
                    mcp_servers.append(name or read_msg.method)
            except AcpError:
                break
        if mcp_servers:
            logger.info("ACP: MCP servers loaded: %s", ", ".join(mcp_servers))

    def pop_pending_oauth_requests(self) -> list[dict[str, str]]:
        """Drain and return MCP OAuth requests captured during session init.

        Each entry has keys ``serverName`` and ``oauthUrl``. Callers (typically
        the dashboard chat runner) surface these to the UI as an Authorize
        prompt — kiro-cli's local callback handles the rest of the OAuth flow.
        """
        out = list(self._pending_oauth_requests)
        self._pending_oauth_requests.clear()
        return out

    # ── Prompt Loop Helpers ──

    def _process_message(self, msg: JsonRpcMessage, req_id: int) -> str:
        """Classify a message into an action string.

        Actions: "complete", "error", "permission", "update", "metadata",
        "server_request_unknown", "skip".
        """
        if msg.is_response_for(req_id):
            return "error" if msg.error else "complete"

        if msg.is_method(METHOD_REQUEST_PERMISSION):
            return "permission"

        if msg.is_method(METHOD_SESSION_UPDATE):
            return "update"

        if msg.is_method(METHOD_METADATA):
            return "metadata"

        if msg.is_method(METHOD_COMPACTION_STATUS):
            return "compaction"

        if msg.is_method(METHOD_CLEAR_STATUS):
            return "clear"

        if msg.is_method(METHOD_AGENT_SWITCHED):
            return "agent_switched"

        if msg.is_method(METHOD_MCP_OAUTH_REQUEST):
            return "mcp_oauth_request"

        if msg.is_method(METHOD_MCP_SERVER_INITIALIZED):
            return "mcp_server_initialized"

        if msg.is_method(METHOD_MCP_SERVER_INIT_FAILURE):
            return "mcp_server_init_failure"

        if msg.is_method(METHOD_SUBAGENT_LIST_UPDATE):
            return "subagent_list"

        if msg.is_method(METHOD_KIRO_SESSION_UPDATE):
            return "subagent_activity"

        # Unknown server→client REQUEST (has both method AND id). Per JSON-RPC
        # the agent blocks until it gets a response, so it must be answered
        # (with -32601 by the dispatch sites) rather than silently skipped —
        # otherwise the agent hangs forever. Known requests (request_permission)
        # are handled above; only genuinely unrecognized requests reach here.
        if msg.method is not None and msg.id is not None:
            return "server_request_unknown"

        return "skip"

    async def _prompt_loop(
        self,
        req_id: int,
        timeout: float,
    ) -> AsyncGenerator[tuple[str, JsonRpcMessage], None]:
        """Core prompt read loop. Yields (action, msg) pairs.

        Always releases ``_turn_done`` on exit — including abnormal exits
        (process death, cancel-grace exceeded, or a caller that raises on an
        ``error`` action and closes this generator). Without the ``finally``,
        those paths bypass the callers' trailing ``_turn_done.set()`` and a
        ``wait_turn_done`` waiter (the cooperative-stop ack) blocks for its
        full budget before escalating to a session-losing hard kill.
        """
        # L1 turn-lock: serialize the whole read turn on this client's single
        # stdout StreamReader so two _bg streaming turns can't both park on
        # readline() and trip "readuntil() called while another coroutine is
        # already waiting". Acquired here (every streaming consumer funnels
        # through _prompt_loop), released in the finally — see the __init__
        # comment for the finalization + coverage caveats.
        await self._turn_lock.acquire()
        try:
            deadline = time.monotonic() + timeout
            consecutive_empty = 0
            last_data_ts = time.monotonic()

            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
                if msg is None:
                    consecutive_empty += 1
                    if consecutive_empty >= _MAX_CONSECUTIVE_EMPTY and not self._is_process_alive():
                        rc = self._process.returncode if self._process else "?"
                        raise AcpProcessDied(f"Process exited during prompt (exit code {rc})")
                    # Staleness check: if caller set _stale_eligible (text was
                    # streamed) and kiro-cli has gone silent, exit early.
                    # Fold in _last_activity (refreshed by the stderr drain) so a
                    # turn that is still streaming thinking_tokens on stderr —
                    # between its final text chunk and its next tool call — is not
                    # mistaken for silence. Using only the stdout clock
                    # (last_data_ts) trips a false stale-turn on every multi-turn
                    # reasoning step, burning ~_STALE_TURN_TIMEOUT s per turn and
                    # reaping long subagent runs at the timeout.
                    last_seen = max(last_data_ts, self._last_activity)
                    if (
                        self._stale_eligible
                        and (time.monotonic() - last_seen) > _STALE_TURN_TIMEOUT
                    ):
                        logger.warning(
                            "Stale turn detected for req %d — no data for %.0fs after text was streamed. "
                            "Treating as complete.",
                            req_id,
                            time.monotonic() - last_seen,
                        )
                        return
                    # Tool-stall watchdog: a tool was dispatched but NOTHING has
                    # come back (no result, no progress, no permission) for the
                    # stall window.  This is the silent-hang case where
                    # _stale_eligible is False (cleared on tool_call) so the
                    # check above never fires.  Treat as a dead turn and exit so
                    # the caller raises/escalates instead of waiting out the full
                    # prompt timeout (or, for crons, the job timeout).
                    if self._tool_dispatched and (time.monotonic() - last_data_ts) > _TOOL_STALL_TIMEOUT:
                        logger.warning(
                            "Tool stall detected for req %d — tool dispatched but no data for %.0fs. "
                            "Treating turn as dead.",
                            req_id, time.monotonic() - last_data_ts,
                        )
                        return
                    continue

                consecutive_empty = 0
                last_data_ts = time.monotonic()
                # NB: do NOT clear _tool_dispatched here.  The last_data_ts reset
                # above already prevents false positives for tools that stream
                # progress frames (each frame restarts the _TOOL_STALL_TIMEOUT
                # countdown).  Clearing the flag on every inbound frame would
                # disarm the watchdog after a single progress frame, so a tool
                # that emits one frame then silently stalls would hang anyway —
                # exactly the bug this watchdog targets.  The flag is cleared
                # only when a tool actually resolves or the turn completes
                # (see _dispatch_events).
                self.last_prompt_stats.event_count += 1

                action = self._process_message(msg, req_id)
                yield action, msg
        finally:
            self._turn_lock.release()
            # Release any cooperative-stop waiter regardless of how the loop
            # ends. The callers set the precise stop reason on the clean
            # "complete" path before this runs (idempotent); on abnormal exit
            # the reason stays "" → provider.cancel reports "timeout" →
            # escalates to hard kill, the correct outcome for a dead turn.
            if not self._turn_done.is_set():
                self._turn_done.set()

    # ── Public API ──

    async def send_message(self, message: str, timeout: float = _DEFAULT_PROMPT_TIMEOUT) -> str:
        """Send a prompt and return the full response text."""
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()

        req_id = await self._send_prompt(message)
        return await self._read_prompt_response(req_id, timeout)

    async def send_message_stream(
        self, message: str, timeout: float = _DEFAULT_PROMPT_TIMEOUT
    ) -> AsyncIterator[str]:
        """Send a prompt and yield text chunks as they arrive."""
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()

        req_id = await self._send_prompt(message)
        prev_pct = self.last_prompt_stats.context_pct
        _prev_used = self.last_prompt_stats.context_used_tokens
        _prev_window = self.last_prompt_stats.context_window_tokens
        self.last_prompt_stats = AcpPromptStats(
            context_pct=prev_pct,
            context_used_tokens=_prev_used,
            context_window_tokens=_prev_window,
        )

        # aclosing(): _prompt_loop holds _turn_lock and releases it in its
        # finally. Consumers below `return` on "complete" without exhausting the
        # loop, which leaves the async-generator SUSPENDED — and CPython
        # finalizes async-gens via a *deferred* scheduled athrow, not at the
        # return point, so the lock would stay held past the turn (next _bg
        # caller blocks = the freeze). aclosing() runs aclose() deterministically
        # on block exit, firing the finally and releasing the lock immediately.
        async with aclosing(self._prompt_loop(req_id, timeout)) as _loop:
            async for action, msg in _loop:
                if action == "complete":
                    reason = ""
                    result = msg.result or {}
                    if isinstance(result, dict):
                        reason = result.get("stopReason", "") or ""
                    self._last_stop_reason = reason
                    self._turn_done.set()
                    return
                if action == "error":
                    raise AcpError(_format_acp_error(msg.error))
                if action == "permission":
                    await self._handle_permission(msg)
                elif action == "server_request_unknown":
                    await self._reject_unknown_server_request(msg)
                elif action == "update":
                    self._track_usage_update(msg)
                    chunk, is_thinking = self._extract_text_chunk(msg)
                    if chunk and not is_thinking:
                        self.last_prompt_stats.text_chunks += 1
                        yield chunk
                        if _is_tool_interrupted_marker(chunk):
                            self._emit_tool_interrupted_sel("send_message_stream")
                            # send_message_stream yields only text chunks (str),
                            # not AcpEvent objects. Tool-result events are a
                            # different shape and cannot be yielded here; callers
                            # of this API do not consume them. Unlike
                            # _dispatch_events (which yields AcpEvent and must
                            # drain tool results before EVENT_COMPLETE), we just
                            # return — no further text will arrive from kiro-cli.
                            return
                    self._track_tool_call(msg)
                elif action == "metadata":
                    self._track_metadata(msg)
                elif action == "compaction":
                    self._log_compaction_status(msg)

        # Loop ended without "complete" — timeout or process death.
        self._last_stop_reason = ""
        self._turn_done.set()

    async def stream_events(
        self,
        message: str,
        timeout: float = _DEFAULT_PROMPT_TIMEOUT,
    ) -> AsyncIterator[AcpEvent]:
        """Send a prompt and yield AcpEvent objects (text, tool_call, permission, complete)."""
        self._cancelled = False
        self._turn_done.clear()
        await self.ensure_ready()
        req_id = await self._send_prompt(message)
        async for event in self._dispatch_events(req_id, timeout):
            yield event

    async def _dispatch_events(
        self,
        req_id: int,
        timeout: float,
        *,
        extract_agent_from_result: bool = False,
    ) -> AsyncIterator[AcpEvent]:
        """Shared event dispatch loop for prompts and commands."""
        prev_pct = self.last_prompt_stats.context_pct
        _prev_used = self.last_prompt_stats.context_used_tokens
        _prev_window = self.last_prompt_stats.context_window_tokens
        self.last_prompt_stats = AcpPromptStats(
            context_pct=prev_pct,
            context_used_tokens=_prev_used,
            context_window_tokens=_prev_window,
        )
        self._tool_call_inputs.clear()
        # Clear stale permission options so an aborted/cancelled request from
        # a prior turn cannot leak into this one (memory + correctness).
        self._permission_options.clear()
        self._stale_eligible = False
        self._tool_dispatched = False
        got_complete = False
        saw_agent_switch = False

        async for action, msg in self._prompt_loop(req_id, timeout):
            if action != "update":
                logger.debug("ACP event: method=%s id=%s action=%s", msg.method, msg.id, action)

            # Reset staleness only on events that indicate active work.
            # Passive updates (usage_update, tool_call_update after completion,
            # available_commands) must NOT reset it — they can arrive after the
            # final text chunk when kiro-cli has finished but hasn't sent the
            # complete response yet.
            if action != "update":
                self._stale_eligible = False

            if action == "complete":
                got_complete = True
                result = msg.result or {}
                reason = ""
                if isinstance(result, dict):
                    reason = result.get("stopReason", "") or ""
                if extract_agent_from_result and isinstance(result, dict):
                    # commands/execute returns output in result fields,
                    # not via session/update chunks — yield as text.
                    text = self._format_command_result(result)
                    if text:
                        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)
                    if not saw_agent_switch:
                        data = result.get("data", {})
                        if isinstance(data, dict) and data.get("agent"):
                            agent_info = data["agent"]
                            name = (
                                agent_info.get("name", "") if isinstance(agent_info, dict) else ""
                            )
                            if name:
                                yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=name)
                # Flush any remaining tool results before completing
                for tr_event in await asyncio.to_thread(self._read_new_tool_results_sync):
                    yield tr_event
                # Turn is over — disarm the stall watchdog.
                self._tool_dispatched = False
                self._last_stop_reason = reason
                self._turn_done.set()
                yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=reason)
                return
            if action == "error":
                raise AcpError(_format_acp_error(msg.error))
            if action == "permission":
                yield self._build_permission_event(msg)
            elif action == "server_request_unknown":
                await self._reject_unknown_server_request(msg)
            elif action == "update":
                self._track_usage_update(msg)
                chunk, is_thinking = self._extract_text_chunk(msg)
                if chunk:
                    # Before yielding text, check for tool results from JSONL
                    for tr_event in await asyncio.to_thread(self._read_new_tool_results_sync):
                        yield tr_event
                    kind = EVENT_THINKING_CHUNK if is_thinking else EVENT_TEXT_CHUNK
                    if not is_thinking:
                        self.last_prompt_stats.text_chunks += 1
                        self._stale_eligible = True
                    yield AcpEvent(kind=kind, text=chunk)
                    if not is_thinking and _is_tool_interrupted_marker(chunk):
                        # kiro-cli's built-in security filter cancelled the turn's tools.
                        # It will not send a ``complete`` response — synthesize one so the
                        # caller exits instead of waiting 2 hours for the prompt timeout.
                        # (_emit_tool_interrupted_sel logs + audits the cancellation.)
                        self._emit_tool_interrupted_sel("_dispatch_events")
                        got_complete = True
                        for tr_event in await asyncio.to_thread(self._read_new_tool_results_sync):
                            yield tr_event
                        yield AcpEvent(kind=EVENT_COMPLETE)
                        return
                tool_event = self._extract_tool_event(msg)
                if tool_event:
                    self._stale_eligible = False
                    # Arm the tool-stall watchdog: if no further data arrives
                    # within _TOOL_STALL_TIMEOUT, _prompt_loop treats the turn
                    # as dead instead of hanging to the full prompt timeout.
                    self._tool_dispatched = True
                    # Check for results from previous tool before yielding new tool_call
                    for tr_event in await asyncio.to_thread(self._read_new_tool_results_sync):
                        yield tr_event
                    yield tool_event
                # Real-time tool result from `tool_call_update` session updates.
                # kiro-cli emits these the moment a tool completes — fires before
                # the JSONL flush, so the inline pill gets its output the instant
                # the tool finishes instead of waiting for the next tool call or
                # message end.  See `_extract_tool_call_update` for the dual-path
                # (content blocks vs rawOutput) details.
                tool_result_event = self._extract_tool_call_update(msg)
                if tool_result_event:
                    # The dispatched tool produced a result — disarm the stall
                    # watchdog.  (Cleared here, not on every inbound frame, so a
                    # tool that streams progress then silently stalls is still
                    # caught.)
                    self._tool_dispatched = False
                    yield tool_result_event
                # claude-agent-acp emits a separate `tool_call_update` carrying
                # the refined title / kind / rawInput once `chunk.input` finishes
                # streaming (the initial `tool_call` arrives with empty input and
                # generic title like "Terminal"/"grep").  Yield as a refinement
                # event so the dashboard pill + persisted message can be
                # patched in place — see `EVENT_TOOL_CALL_UPDATE` in chat_runner.
                tool_refine_event = self._extract_tool_call_refinement(msg)
                if tool_refine_event:
                    yield tool_refine_event
            elif action == "metadata":
                self._track_metadata(msg)
            elif action == "compaction":
                self._log_compaction_status(msg)
                params = msg.params or {}
                status = params.get("status", {})
                status_type = status.get("type", "") if isinstance(status, dict) else str(status)
                summary = params.get("summary", "")
                yield AcpEvent(kind=EVENT_COMPACTION_STATUS, text=status_type, title=summary)
            elif action == "clear":
                yield AcpEvent(kind=EVENT_CLEAR_STATUS)
            elif action == "subagent_list":
                params = msg.params or {}
                _subs = params.get("subagents")
                logger.debug(
                    "ACP subagent_list received: %s entries",
                    len(_subs) if isinstance(_subs, list) else "n/a",
                )
                if isinstance(_subs, list):
                    yield AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=_subs)
            elif action == "subagent_activity":
                # _kiro.dev/session/update: a sub-agent session's own update,
                # tagged with its sessionId. Carries either:
                # - tool_call_chunk (inner tool starting) with toolCallId/title
                # - agent_message_chunk with text (sub-agent's streamed output)
                params = msg.params or {}
                _ssid = str(params.get("sessionId") or "")
                _upd_raw = params.get("update")
                _upd = _upd_raw if isinstance(_upd_raw, dict) else {}
                _su_kind = str(_upd.get("sessionUpdate") or "")
                _tcid = str(_upd.get("toolCallId") or "")
                _su_text = str(_upd.get("text") or "")
                if _ssid and _tcid:
                    yield AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=_ssid,
                        tool_call_id=_tcid,
                        title=str(_upd.get("title") or ""),
                    )
                elif _ssid and _su_text and _su_kind == "agent_message_chunk":
                    # Sub-agent's streamed text output — the real content.
                    yield AcpEvent(
                        kind=EVENT_SUBAGENT_ACTIVITY,
                        sub_session_id=_ssid,
                        text=_su_text,
                    )
            elif action == "agent_switched":
                saw_agent_switch = True
                params = msg.params or {}
                agent_name = params.get("agentName", "")
                yield AcpEvent(kind=EVENT_AGENT_SWITCHED, text=agent_name)
            elif action == "mcp_oauth_request":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                oauth_url = str(params.get("oauthUrl") or params.get("url") or "")
                # Reject unsafe-scheme URLs *before* recording dedupe so a later
                # safe retry for the same server still gets through.
                if not _is_safe_oauth_url(oauth_url):
                    if oauth_url:
                        logger.warning(
                            "ACP: refusing unsafe mid-session MCP OAuth URL for %s",
                            server_name or "(unknown)",
                        )
                    continue
                # Without a server_name we can't correlate this banner with the
                # later server_initialized/server_init_failure notification (the
                # discard path keys on server_name only).
                if not server_name:
                    logger.warning(
                        "ACP: dropping mid-session MCP OAuth request with empty serverName"
                    )
                    continue
                if server_name in self._oauth_emitted_servers:
                    logger.debug(
                        "ACP: dropping duplicate mid-session MCP OAuth request for %s",
                        server_name,
                    )
                    continue
                self._oauth_emitted_servers.add(server_name)
                logger.info("ACP: MCP OAuth request mid-session for %s", server_name)
                yield AcpEvent(
                    kind=EVENT_MCP_OAUTH_REQUEST,
                    server_name=server_name,
                    oauth_url=oauth_url,
                )
            elif action == "mcp_server_initialized":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                if server_name:
                    logger.info("ACP: MCP server initialized: %s", server_name)
                    # Allow re-emission of oauth_request if this server's token expires later.
                    self._oauth_emitted_servers.discard(server_name)
                    yield AcpEvent(
                        kind=EVENT_MCP_SERVER_INITIALIZED,
                        server_name=server_name,
                    )
            elif action == "mcp_server_init_failure":
                params = msg.params or {}
                server_name = str(params.get("serverName") or params.get("name") or "")
                err = str(params.get("error") or "")
                if server_name:
                    logger.warning("ACP: MCP server init failure: %s — %s", server_name, err)
                    # The current banner is in a closed (failed) state — clear
                    # the dedupe entry so kiro-cli's next oauth_request retry
                    # for this server surfaces a new banner instead of being
                    # silently dropped.
                    self._oauth_emitted_servers.discard(server_name)
                    yield AcpEvent(
                        kind=EVENT_MCP_SERVER_INIT_FAILURE,
                        server_name=server_name,
                        text=err,
                    )

        if not got_complete:
            self._last_stop_reason = ""
            self._turn_done.set()
            # If text was streamed, this is a stale turn (kiro-cli finished
            # but never sent `result`).  Yield a synthetic complete so callers
            # finalize normally instead of showing a timeout error.
            if self._stale_eligible:
                logger.info(
                    "Synthesizing EVENT_COMPLETE after stale turn (chunks=%d)",
                    self.last_prompt_stats.text_chunks,
                )
                yield AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
                return
            raise AcpTimeoutError()

    async def approve_tool(
        self,
        request_id: str | int,
        option_id: str | None = None,
        *,
        always: bool = False,
    ) -> None:
        """Approve a pending session/request_permission.

        ``option_id`` overrides the auto-resolved id when provided. Otherwise
        the recorded options for ``request_id`` are consulted — picking the
        "always" variant if ``always=True``, else the "once" variant. This
        keeps kiro-cli ("allow_once"/"allow_always") and claude-agent-acp
        ("allow"/"allow_always") working without caller knowledge.
        """
        resolved_id = option_id
        if resolved_id is None:
            recorded = self._permission_options.pop(request_id, None)
            # A recorded entry may carry only a "reject" id (a request that
            # advertised a reject option but no allow option), so use .get and
            # fall back to the canonical allow id rather than KeyError-ing.
            resolved_id = (recorded or {}).get("always" if always else "once")
            if resolved_id is None:
                resolved_id = OPTION_ALLOW_ALWAYS if always else OPTION_ALLOW_ONCE
        await self._send_response(
            request_id,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": resolved_id}},
        )

    async def reject_tool(self, request_id: str | int) -> None:
        """Reject a pending session/request_permission.

        Prefers a clean ``selected`` reject using the reject optionId the agent
        advertised (claude-agent-acp offers ``reject`` → behavior:"deny",
        surfacing a clear "permission denied" rather than the cryptic
        "Tool use aborted" the adapter throws on a ``cancelled`` outcome).
        Falls back to ``cancelled`` when no reject option was advertised
        (kiro-cli), which kiro handles as an ordinary rejection.
        """
        recorded = self._permission_options.pop(request_id, None)
        reject_id = recorded.get("reject") if recorded else None
        if reject_id:
            await self._send_response(
                request_id, {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": reject_id}}
            )
        else:
            await self._send_response(request_id, {"outcome": {"outcome": OUTCOME_CANCELLED}})

    async def send_command(self, command: str, args: dict | None = None) -> str:
        """Execute a kiro slash command (e.g. '/compact', '/usage', '/effort').

        Returns the response text (if any).  For streaming output use
        :meth:`stream_command` instead.

        When *args* is provided (e.g. ``{"level": "high"}`` for ``/effort``),
        the TuiCommand object form ``{command, args}`` is used so kiro-cli
        receives the arguments — the plain-string form silently drops them.
        Otherwise the plain-string form is kept for backward compat with
        older kiro-cli.
        """
        await self.ensure_ready()
        if args:
            cmd_name = command.strip().split(None, 1)[0].lstrip("/")
            payload: dict = {
                "sessionId": self._session_id,
                "command": {"command": cmd_name, "args": args},
            }
        else:
            payload = {"sessionId": self._session_id, "command": command}
        req_id = await self._send_request(METHOD_COMMANDS_EXECUTE, payload)
        try:
            result = await self._wait_for_response(req_id, timeout=60.0)
            raw = result.get("text", "") or result.get("message", "")
            if raw:
                from kiro_claw.security import redact_exfiltration_urls

                raw, _ = redact_exfiltration_urls(raw)
            return raw
        except AcpTimeoutError:
            logger.debug("Command '%s' response timed out (may still be running)", command)
            return ""

    async def stream_command(
        self, command: str, timeout: float = _DEFAULT_PROMPT_TIMEOUT
    ) -> AsyncIterator[AcpEvent]:
        """Execute a slash command and yield streaming AcpEvents.

        Uses ``_kiro.dev/commands/execute`` with the TuiCommand object
        format (``{command, args}``) so kiro-cli executes the command
        natively and streams full output via ``session/update``.
        """
        self._cancelled = False
        await self.ensure_ready()

        cmd_name, cmd_args = self._parse_slash_command(command)
        req_id = await self._send_request(
            METHOD_COMMANDS_EXECUTE,
            {
                "sessionId": self._session_id,
                "command": {"command": cmd_name, "args": cmd_args},
            },
        )
        async for event in self._dispatch_events(req_id, timeout, extract_agent_from_result=True):
            yield event

    @staticmethod
    def _format_command_result(result: dict) -> str:
        """Extract displayable text from a commands/execute response."""
        import json as _json

        data = result.get("data")
        message = result.get("message", "")
        # Structured data — format as readable JSON block
        if isinstance(data, dict) and data:
            # Filter out agent/model metadata (handled separately)
            display = {k: v for k, v in data.items() if k not in ("agent", "model")}
            if display:
                return (
                    f"{message}\n```json\n{_json.dumps(display, indent=2)}\n```"
                    if message
                    else f"```json\n{_json.dumps(display, indent=2)}\n```"
                )
        return message or ""

    @staticmethod
    def _parse_slash_command(command: str) -> tuple[str, dict]:
        """Parse ``/foo bar baz`` into TuiCommand ``(name, args)``."""
        parts = command.strip().split(None, 1)
        name = parts[0].lstrip("/") if parts else command.lstrip("/")
        value = parts[1] if len(parts) > 1 else None
        args: dict = {"value": value} if value else {}
        return name, args

    async def cancel_session(self, grace_secs: float = 0.0) -> None:
        """Cancel the current in-flight operation via ACP session/cancel.

        Per ACP spec, session/cancel is a JSON-RPC notification (no id).
        The ack arrives as stopReason:"cancelled" on the session/prompt
        response, not as a response to this message.

        ``grace_secs`` is the caller's cooperative-cancel ack budget. The read
        loop aborts the turn as "unresponsive" once this elapses, so it must
        not be shorter than the budget the caller will wait on; we raise the
        per-cancel grace to ``max(floor, grace_secs)`` so a configured budget
        above the 10s floor genuinely extends the window instead of the loop
        bailing early and forcing a session-losing hard kill.
        """
        if not self._session_id:
            logger.debug("cancel_session: no session_id, skip")
            return
        self._cancelled = True
        self._cancel_ts = time.monotonic()
        self._cancel_grace_secs = max(_CANCEL_GRACE_SECS, grace_secs)
        logger.debug(
            "cancel_session: sending session/cancel notification (sid=%s, turn_done=%s, proc_alive=%s)",
            self._session_id,
            self._turn_done.is_set(),
            self._is_process_alive(),
        )
        if not self._process or not self._process.stdin:
            logger.debug("cancel_session: process not running")
            return
        try:
            notification = {
                "jsonrpc": "2.0",
                "method": METHOD_CANCEL,
                "params": {"sessionId": self._session_id},
            }
            data = json.dumps(notification) + "\n"
            self._process.stdin.write(data.encode())
            await self._process.stdin.drain()
            self._last_activity = time.monotonic()
            logger.debug("cancel_session: wrote session/cancel notification")
        except Exception:
            logger.debug("Cancel notification failed", exc_info=True)

    async def wait_turn_done(self, timeout: float) -> str:
        """Wait for the current prompt to finish. Returns stop_reason or raises TimeoutError."""
        await asyncio.wait_for(self._turn_done.wait(), timeout=timeout)
        return self._last_stop_reason

    def has_active_turn(self) -> bool:
        """True if a prompt is in flight AND has not yet been cancelled.

        Returns False as soon as ``cancel_session()`` has been called, even
        before the agent acknowledges the cancel. Callers that need to force
        a kill regardless of cancel state should skip this check.
        """
        return not self._cancelled and not self._turn_done.is_set() and self._is_process_alive()

    # ── Private Helpers ──

    _IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
    _IMAGE_MEDIA_TYPES = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
    }

    async def _send_prompt(self, message: str) -> int:
        content: list[dict] = []
        # Extract image paths from message and inline them as image blocks
        path_re = re.compile(r"(/[\w./@~\s()\-]+\.(?:png|jpg|jpeg|gif|webp|bmp))", re.IGNORECASE)
        remaining = message
        for match in path_re.finditer(message):
            p = Path(match.group(1).strip())
            if p.is_file() and p.suffix.lower() in self._IMAGE_EXTENSIONS:
                try:
                    data = base64.b64encode(p.read_bytes()).decode()
                    media = self._IMAGE_MEDIA_TYPES.get(p.suffix.lower(), "image/png")
                    content.append({"type": "image", "data": data, "mimeType": media})
                    remaining = remaining.replace(match.group(1), f"[image: {p.name}]")
                except Exception:
                    pass  # skip unreadable files
        content.insert(0, {"type": "text", "text": remaining})

        return await self._send_request(
            METHOD_PROMPT,
            {
                "sessionId": self._session_id,
                "prompt": content,
            },
        )

    async def _read_prompt_response(self, req_id: int, timeout: float) -> str:
        output: list[str] = []
        prev_pct = self.last_prompt_stats.context_pct
        _prev_used = self.last_prompt_stats.context_used_tokens
        _prev_window = self.last_prompt_stats.context_window_tokens
        self.last_prompt_stats = AcpPromptStats(
            context_pct=prev_pct,
            context_used_tokens=_prev_used,
            context_window_tokens=_prev_window,
        )

        async for action, msg in self._prompt_loop(req_id, timeout):
            if action == "complete":
                reason = ""
                result = msg.result or {}
                if isinstance(result, dict):
                    reason = result.get("stopReason", "") or ""
                self._last_stop_reason = reason
                self._turn_done.set()
                return "".join(output)
            if action == "error":
                raise AcpError(_format_acp_error(msg.error))
            if action == "permission":
                await self._handle_permission(msg)
            elif action == "server_request_unknown":
                await self._reject_unknown_server_request(msg)
            elif action == "update":
                self._track_usage_update(msg)
                chunk, is_thinking = self._extract_text_chunk(msg)
                if chunk and not is_thinking:
                    output.append(chunk)
                    self.last_prompt_stats.text_chunks += 1
                    if _is_tool_interrupted_marker(chunk):
                        self._emit_tool_interrupted_sel("_read_prompt_response")
                        return "".join(output)  # see _dispatch_events for rationale
                self._track_tool_call(msg)
            elif action == "metadata":
                self._track_metadata(msg)
            elif action == "compaction":
                self._log_compaction_status(msg)

        self._last_stop_reason = ""
        self._turn_done.set()
        raise AcpTimeoutError(partial_output="".join(output))

    async def _handle_permission(self, msg: JsonRpcMessage) -> None:
        """Auto-approve tool permissions."""
        request_id = msg.id if msg.id is not None else ""

        params = msg.params or {}
        tool_call = params.get("toolCall", {})
        title = tool_call.get("title", "unknown")
        logger.info("Auto-approving tool: %s", title)

        await self.approve_tool(request_id)

    async def _reject_unknown_server_request(self, msg: JsonRpcMessage) -> None:
        """Answer an unrecognized server→client request with -32601.

        KiroClaw implements only ``session/request_permission`` as an inbound
        server request. Any other request (e.g. ``fs/read_text_file``,
        ``terminal/create``) has no handler, but JSON-RPC requires a response or
        the agent blocks forever. Reply ``Method not found`` so it fails fast.
        """
        if msg.id is None:
            return
        logger.warning("ACP: rejecting unknown server request: method=%s id=%s", msg.method, msg.id)
        await self._send_error(msg.id, _JSONRPC_METHOD_NOT_FOUND, f"Method not found: {msg.method}")

    def _extract_text_chunk(self, msg: JsonRpcMessage) -> tuple[str | None, bool]:
        """Extract text from an agent_message_chunk or agent_thought_chunk update.

        Returns (text, is_thinking). is_thinking is True when the chunk is an
        ``agent_thought_chunk`` (claude-agent-acp emits reasoning under this
        dedicated update type) or when an ``agent_message_chunk``'s inner
        content block type indicates reasoning (kiro-cli style).
        """
        params = msg.params or {}
        update = params.get("update", {})
        kind = update.get("sessionUpdate")
        if kind == UPDATE_AGENT_MESSAGE_CHUNK:
            content = update.get("content", {})
            text = content.get("text")
            content_type = content.get("type", "text")
            is_thinking = content_type in ("thinking", "reasoning")
            return text, is_thinking
        if kind == UPDATE_AGENT_THOUGHT_CHUNK:
            content = update.get("content", {})
            text = content.get("text")
            return text, True
        return None, False

    def _track_usage_update(self, msg: JsonRpcMessage) -> None:
        """Track context usage and config updates from session update notifications."""
        params = msg.params or {}
        update = params.get("update", {})
        kind = update.get("sessionUpdate") if isinstance(update, dict) else None
        if kind == UPDATE_USAGE:
            used = update.get("used")
            size = update.get("size")
            if used is not None and size and size > 0:
                self.last_prompt_stats.context_pct = round((used / size) * 100, 1)
                # Keep the raw counts so the dashboard token text uses the real
                # served window (size) instead of re-deriving it from the model id.
                self.last_prompt_stats.context_used_tokens = int(used)
                self.last_prompt_stats.context_window_tokens = int(size)
            else:
                logger.debug("usage_update missing used/size: %s", update)
        elif kind == UPDATE_CONFIG_OPTION:
            self._handle_config_option_update(msg)
        elif self._is_claude and kind and kind not in KNOWN_SESSION_UPDATES:
            logger.debug("Unhandled session update type: %s", kind)

    def _emit_tool_interrupted_sel(self, site: str) -> None:
        """Emit a SEL audit event when kiro-cli cancels tool uses via its security filter.

        This is a security-relevant permission decision (kiro-cli denied tool execution)
        that KiroClaw observes but does not control.  Logged so the audit trail reflects
        that tools were blocked even though the decision was made outside KiroClaw.
        Also emits a single WARNING log line (grep-friendly for on-call) with session
        correlation — covers all three call sites so none of them fire silently.
        """
        logger.warning(
            "kiro-cli cancelled tool use(s) [site=%s session=%s]", site, self._session_id
        )
        try:
            from kiro_claw.sel import sel

            sel().log_tool_invocation(
                session_key=self._session_key or "",
                source="acp",
                tool_name="kiro_cli_security_filter",
                tool_kind="client_built_in",
                outcome="denied",
                metadata={"site": site, "reason": "tool_interrupted_marker"},
            )
        except Exception:
            logger.warning("SEL audit failed for tool_interrupted at %s", site, exc_info=True)

    def _track_tool_call(self, msg: JsonRpcMessage) -> None:
        """Track tool calls in stats (used by send_message/send_message_stream)."""
        params = msg.params or {}
        update = params.get("update", {})
        if update.get("sessionUpdate") == UPDATE_TOOL_CALL:
            title = update.get("title", "unknown")
            kind = update.get("kind", "unknown")
            self.last_prompt_stats.tool_calls.append((kind, title))
            logger.debug("ACP tool_call: %s (%s)", title, kind)

    def _extract_tool_event(self, msg: JsonRpcMessage) -> AcpEvent | None:
        params = msg.params or {}
        update = params.get("update", {})
        if update.get("sessionUpdate") == UPDATE_TOOL_CALL:
            title = update.get("title", "unknown")
            kind = update.get("kind", "unknown")
            raw_input = update.get("rawInput") or update.get("input") or update.get("params")
            purpose = raw_input.get("__tool_use_purpose", "") if isinstance(raw_input, dict) else ""
            logger.debug(
                "ACP tool_call raw: %s",
                {k: v for k, v in update.items() if k != "sessionUpdate"},
            )
            # Build initial tool input string from raw params
            tool_call_id = update.get("toolCallId", "")
            input_str = ""
            if tool_call_id and raw_input:
                input_str = (
                    json.dumps(raw_input, indent=2)
                    if isinstance(raw_input, (dict, list))
                    else str(raw_input)
                )
            # For edit tools with diff content blocks, generate unified diff
            found_diff = False
            content_blocks = update.get("content", [])
            if isinstance(content_blocks, list):
                for cb in content_blocks:
                    if isinstance(cb, dict) and cb.get("type") == "diff":
                        old = cb.get("oldText") or ""
                        new = cb.get("newText") or ""
                        path = cb.get("path", "")
                        diff_str = _make_unified_diff(old, new, path)
                        if diff_str:
                            input_str = diff_str
                            found_diff = True
                        break
            # Fallback for strReplace when no diff content block was found
            if (
                not found_diff
                and isinstance(raw_input, dict)
                and raw_input.get("command") == "strReplace"
            ):
                old = raw_input.get("oldStr") or ""
                new = raw_input.get("newStr") or ""
                path = raw_input.get("path") or ""
                if old or new:
                    diff_str = _make_unified_diff(old, new, path)
                    if diff_str:
                        input_str = diff_str
            # Redact sensitive content before caching/displaying
            if input_str:
                input_str, _ = redact_exfiltration_urls(input_str)
                input_str, _ = redact_credentials(input_str)
            if tool_call_id and input_str:
                self._tool_call_inputs[tool_call_id] = input_str
            # Redact LLM-influenced fields before dashboard display
            if purpose:
                purpose, _ = redact_exfiltration_urls(purpose)
                purpose, _ = redact_credentials(purpose)
            # Prefer rawInput.description over the SDK-provided title (e.g.
            # Claude Code's Bash tool emits "List KiroClaw ACP module files"
            # alongside `ls /workplace/...`). For claude-agent-acp this rarely
            # fires here because the initial tool_call has empty rawInput —
            # the refinement path in `_extract_tool_call_refinement` is what
            # the user actually sees. Same helper is used in both places.
            title = _select_tool_title(title, raw_input) or ""
            if title:
                title, _ = redact_exfiltration_urls(title)
                title, _ = redact_credentials(title)
            if kind:
                kind, _ = redact_exfiltration_urls(kind)
                kind, _ = redact_credentials(kind)
            self.last_prompt_stats.tool_calls.append((kind, title))
            return AcpEvent(
                kind=EVENT_TOOL_CALL,
                title=title,
                tool_kind=kind,
                tool_purpose=purpose,
                tool_input=input_str,
                tool_call_id=tool_call_id,
                raw_tool_params=raw_input if isinstance(raw_input, dict) else None,
            )
        return None

    def _extract_tool_call_update(self, msg: JsonRpcMessage) -> AcpEvent | None:
        """Extract a real-time tool result from a `tool_call_update` session update.

        kiro-cli streams tool completion via ACP `session/update` notifications
        (not just the JSONL session file). Two updates fire per tool:
          1. A `content` array carrying the tool output as text blocks — arrives
             as soon as the tool finishes, often mid-stream during the agent's
             follow-up text.
          2. A `status: completed` update with `rawOutput.items[].Json.stdout`
             for shell-style tools.
        Both carry the same `toolCallId`; we yield an EVENT_TOOL_RESULT on
        whichever provides output. Hooking these gives the inline pill its real
        output the moment the tool finishes, instead of waiting for the kiro-cli
        JSONL flush at the next tool_call boundary or message end.
        """
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call_update":
            return None
        tool_use_id = update.get("toolCallId", "")
        if not tool_use_id:
            return None

        output_parts: list[str] = []

        # Path 1: `content` blocks (arrive during tool execution / mid-stream)
        content = update.get("content")
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                inner = block.get("content")
                if isinstance(inner, dict) and inner.get("type") == "text":
                    text = inner.get("text", "")
                    if text:
                        output_parts.append(str(text)[:4000])

        # Path 2: `rawOutput` (arrives with status=completed) — fallback when
        # there were no content blocks (e.g. some tools only emit rawOutput).
        # kiro-cli tool results land here in two shapes:
        #   items[].Text  — fs_read contents, shell-style text, etc.
        #   items[].Json  — structured tool output (use .stdout when present)
        if not output_parts:
            raw_output = update.get("rawOutput")
            if isinstance(raw_output, dict):
                items = raw_output.get("items", [])
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        if "Text" in item and item.get("Text"):
                            output_parts.append(str(item["Text"])[:4000])
                            continue
                        j = item.get("Json")
                        if isinstance(j, dict):
                            if "stdout" in j and j.get("stdout"):
                                output_parts.append(str(j["stdout"])[:4000])
                            else:
                                output_parts.append(json.dumps(j, default=str)[:4000])

        if not output_parts:
            return None

        final_output = "\n".join(output_parts)[:8000]
        final_output, _ = redact_exfiltration_urls(final_output)
        final_output, _ = redact_credentials(final_output)
        return AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id=tool_use_id,
            tool_output=final_output,
            tool_final=update.get("status") == "completed",
        )

    def _extract_tool_call_refinement(self, msg: JsonRpcMessage) -> AcpEvent | None:
        """Extract a refined title/kind/input from a `tool_call_update`.

        claude-agent-acp emits two events per tool: an initial `tool_call`
        on streaming `content_block_start` (when `chunk.input` is still empty,
        so the title falls back to the generic tool name like "Terminal" or
        "grep"), then a follow-up `tool_call_update` once `chunk.input` is
        fully streamed — that update carries the populated `rawInput` and a
        refined `title`/`kind` from the upstream `toolInfoFromToolUse`
        (e.g. `"ls /local/home/hugocost/.kiroclaw/workspace"`).

        We yield an EVENT_TOOL_CALL_UPDATE so the dashboard can patch the
        existing pill / persisted message in place — see the matching
        handler in `chat_runner.py`. Returns None when the update only
        carries output (handled separately by `_extract_tool_call_update`).
        """
        params = msg.params or {}
        update = params.get("update", {})
        if not isinstance(update, dict) or update.get("sessionUpdate") != "tool_call_update":
            return None
        tool_use_id = update.get("toolCallId", "")
        if not tool_use_id:
            return None
        title = update.get("title")
        kind = update.get("kind")
        raw_input = update.get("rawInput")
        # Only emit when at least one refinement field is present. Pure-output
        # updates (content/rawOutput only) are handled by the result extractor.
        if title is None and kind is None and not raw_input:
            return None
        # Build the input string the same way `_extract_tool_event` does so
        # the merged toolLog entry / message meta lines up across both events.
        input_str = ""
        if isinstance(raw_input, (dict, list)) and raw_input:
            try:
                input_str = json.dumps(raw_input, indent=2)
            except (TypeError, ValueError):
                input_str = str(raw_input)
        elif isinstance(raw_input, str):
            input_str = raw_input
        # Edit-style diff content blocks: prefer the rendered unified diff over
        # the raw input dict (mirrors `_extract_tool_event`).
        content_blocks = update.get("content", [])
        if isinstance(content_blocks, list):
            for cb in content_blocks:
                if isinstance(cb, dict) and cb.get("type") == "diff":
                    old = cb.get("oldText") or ""
                    new = cb.get("newText") or ""
                    path = cb.get("path", "")
                    diff_str = _make_unified_diff(old, new, path)
                    if diff_str:
                        input_str = diff_str
                    break
        if input_str:
            input_str, _ = redact_exfiltration_urls(input_str)
            input_str, _ = redact_credentials(input_str)
            self._tool_call_inputs[tool_use_id] = input_str
        # Prefer rawInput.description over the SDK-supplied title (e.g.
        # Bash's "List KiroClaw ACP module files" rather than `ls /workplace/...`).
        # Same helper as `_extract_tool_event` so the rule is consistent.
        title_source = _select_tool_title(title, raw_input)
        title_str = ""
        if title_source:
            title_str, _ = redact_exfiltration_urls(title_source)
            title_str, _ = redact_credentials(title_str)
        kind_str = ""
        if isinstance(kind, str) and kind:
            kind_str, _ = redact_exfiltration_urls(kind)
            kind_str, _ = redact_credentials(kind_str)
        return AcpEvent(
            kind=EVENT_TOOL_CALL_UPDATE,
            title=title_str,
            tool_kind=kind_str,
            tool_input=input_str,
            tool_call_id=tool_use_id,
            raw_tool_params=raw_input if isinstance(raw_input, dict) else None,
        )

    def _read_new_tool_results_sync(self) -> list[AcpEvent]:
        """Read new ToolResults entries from the kiro-cli session JSONL file."""
        if not self._session_id:
            return []
        jsonl_path = Path.home() / ".kiro" / "sessions" / "cli" / f"{self._session_id}.jsonl"
        if not jsonl_path.exists():
            return []
        results: list[AcpEvent] = []
        try:
            with open(jsonl_path, "r") as f:
                f.seek(self._jsonl_pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        break  # partial line — retry next call
                    self._jsonl_pos = f.tell()
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if entry.get("kind") == "ToolResults":
                        for c in entry.get("data", {}).get("content", []):
                            if c.get("kind") == "toolResult":
                                tr = c.get("data")
                                if not isinstance(tr, dict):
                                    continue
                                tool_use_id = tr.get("toolUseId", "")
                                output_parts: list[str] = []
                                for rc in tr.get("content", []):
                                    if isinstance(rc, dict):
                                        if rc.get("kind") == "json":
                                            d = rc.get("data", {})
                                            if isinstance(d, dict) and "stdout" in d:
                                                out = d.get("stdout", "")
                                                if out:
                                                    output_parts.append(out[:4000])
                                            else:
                                                output_parts.append(json.dumps(d, indent=2)[:4000])
                                        elif rc.get("kind") == "text":
                                            output_parts.append(str(rc.get("data", ""))[:4000])
                                if output_parts:
                                    results.append(
                                        AcpEvent(
                                            kind=EVENT_TOOL_RESULT,
                                            tool_call_id=tool_use_id,
                                            tool_output="\n".join(output_parts)[:8000],
                                        )
                                    )
        except Exception:
            logger.debug("Failed to read JSONL for tool results", exc_info=True)
        if results:
            logger.debug("JSONL: read %d tool result(s) from %s", len(results), jsonl_path.name)
        return results

    def _build_permission_event(self, msg: JsonRpcMessage) -> AcpEvent:
        request_id = msg.id if msg.id is not None else ""
        params = msg.params or {}
        tool_call = params.get("toolCall", {})
        title = tool_call.get("title", "unknown")
        # The ACP toolCall carries a `kind` ("execute" for Bash, "read"/"edit"/
        # …). Carry it onto the event so downstream validation can apply the
        # execute-tool exemptions — notably the display-name length gate, which
        # is meaningless for a full shell command string. Missing/empty stays "".
        tool_kind = tool_call.get("kind", "")
        # ACP spec uses optionId/name + kind ("allow_once"|"allow_always"|
        # "reject_once"|"reject_always"); kiro-cli historically uses id/label
        # with id values "allow_once"/"allow_always". Accept both shapes and
        # remember the actual optionIds keyed by kind so approve_tool/
        # reject_tool can echo the exact id the agent advertised.
        options: list[dict[str, str]] = []
        kind_to_id: dict[str, str] = {}
        for o in params.get("options", []):
            opt_id = o.get("optionId") or o.get("id") or ""
            opt_label = o.get("name") or o.get("label") or ""
            opt_kind = o.get("kind") or ""
            if not opt_id:
                continue
            options.append({"id": opt_id, "label": opt_label})
            if not opt_kind:
                # Only synthesize a kind for well-known literals; unknown ids
                # leave kind empty so we don't mis-classify agent intent.
                opt_kind = _LEGACY_OPTION_KIND.get(opt_id.lower(), "")
            if opt_kind:
                kind_to_id.setdefault(opt_kind, opt_id)
        if not options:
            options = [
                {"id": OPTION_ALLOW_ONCE, "label": "Allow once"},
                {"id": OPTION_ALLOW_ALWAYS, "label": "Allow always"},
            ]
            kind_to_id = {"allow_once": OPTION_ALLOW_ONCE, "allow_always": OPTION_ALLOW_ALWAYS}
        # Record optionIds the agent advertised so approve_tool / reject_tool
        # can echo the exact ids. We record when EITHER an allow option (for
        # approve) OR a reject option (for a clean reject) was advertised.
        # claude-agent-acp advertises a {kind:"reject_once", optionId:"reject"}
        # option whose selection yields behavior:"deny" — sending that is far
        # better than a "cancelled" outcome, which the adapter turns into the
        # cryptic "Tool use aborted". kiro-cli advertises no reject option, so
        # reject_tool falls back to "cancelled" there (handled as a clean
        # rejection by kiro).
        any_allow = kind_to_id.get("allow_once") or kind_to_id.get("allow_always")
        any_reject = kind_to_id.get("reject_once") or kind_to_id.get("reject_always")
        if request_id != "" and (any_allow is not None or any_reject is not None):
            recorded: dict[str, str] = {}
            if any_allow is not None:
                recorded["once"] = kind_to_id.get("allow_once") or any_allow
                recorded["always"] = kind_to_id.get("allow_always") or any_allow
            if any_reject is not None:
                recorded["reject"] = any_reject
            self._permission_options[request_id] = recorded

        # Resolve full tool input — the preceding ToolCall session/notification
        # carries the complete params that we cache by toolCallId.  The
        # request_permission message only has a truncated human-readable title.
        tool_input = ""
        tool_call_id = tool_call.get("toolCallId", "")

        # 1. Look up cached input from the ToolCall notification
        if tool_call_id and tool_call_id in self._tool_call_inputs:
            tool_input = self._tool_call_inputs.pop(tool_call_id)

        # 2. Fallback: check if toolCall itself carries input/params
        if not tool_input:
            raw_input = tool_call.get("input") or tool_call.get("params")
            if raw_input:
                tool_input = (
                    json.dumps(raw_input, indent=2)
                    if isinstance(raw_input, (dict, list))
                    else str(raw_input)
                )

        logger.info("Permission requested for tool: %s (req=%s)", title, request_id)
        logger.debug("Permission toolCall payload: %s", tool_call)
        return AcpEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=request_id,
            title=title,
            tool_kind=tool_kind,
            options=options,
            tool_input=tool_input,
            tool_call_id=tool_call_id,
        )

    def _track_metadata(self, msg: JsonRpcMessage) -> None:
        params = msg.params or {}
        pct = params.get("contextUsagePercentage")
        if pct is not None:
            self.last_prompt_stats.context_pct = float(pct)

    def _log_compaction_status(self, msg: JsonRpcMessage) -> None:
        params = msg.params or {}
        status = params.get("status", "")
        logger.info("Compaction status: %s", status)

    async def wait_for_compaction(self, timeout: float = 120.0) -> dict:
        """Read messages until compaction completed/failed arrives. Returns status dict."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            msg = await self._read_message(timeout=min(remaining, _READ_TIMEOUT))
            if msg is None:
                continue
            if msg.is_method(METHOD_COMPACTION_STATUS):
                self._log_compaction_status(msg)
                params = msg.params or {}
                status = params.get("status", {})
                s_type = status.get("type", "") if isinstance(status, dict) else str(status)
                if s_type in ("completed", "failed"):
                    self._track_metadata(msg)
                    return {"type": s_type, "summary": params.get("summary", "")}
            elif msg.is_method(METHOD_METADATA):
                self._track_metadata(msg)
            else:
                # Don't drop — buffer for later processing
                if msg.method and not msg.id:
                    self._mcp_notifications.append(msg)
        return {"type": "timeout"}
