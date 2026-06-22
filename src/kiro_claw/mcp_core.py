"""MCP server exposing spawn, learn, and task tools to kiro-cli.

Runs as ``kiroclaw mcp-core`` — kiro-cli spawns it as a child process
and calls tools via JSON-RPC over stdio (MCP protocol).

Tools:
    spawn_run       — spawn a background subagent
    spawn_list      — list running/completed subagents
    spawn_status    — retrieve full subagent output
    learn_add       — save a learned correction
    learn_list      — list all lessons
    learn_remove    — remove lessons by substring
    task_run        — start the autonomous task runner
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import platform
import re as _re
import subprocess
import tempfile
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid

try:
    import fcntl  # POSIX-only; not available on Windows.
except ImportError:  # pragma: no cover — guard for non-POSIX systems.
    fcntl = None  # type: ignore[assignment]
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

from kiro_claw.aim_agents import list_agents
from kiro_claw.config.loader import KiroClawConfig, config_dir, outbox_dir
from kiro_claw.dashboard.origin import parse_dashboard_url
from kiro_claw.history import ConversationLog
from kiro_claw.hooks import FileTooLargeError, safe_read_file_bytes
from kiro_claw.knowledge.embedder import create_embedder_from_config
from kiro_claw.knowledge.retrieval import HybridRetriever
from kiro_claw.knowledge.store import KnowledgeStore
from kiro_claw.mcp_shared import call_tool_with_logging, run_mcp_stdio_loop
from kiro_claw.platform import PlatformCompositionError, current_context
from kiro_claw.security import (
    BINARY_MIME_ALLOWLIST,
)
from kiro_claw.security import redact as _security_redact
from kiro_claw.security import (
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_claw.sel import sel
from kiro_claw.validation import (
    _SLACK_TS_RE,
    ARTIFACT_DELETE_SCHEMA,
    ARTIFACT_GET_SCHEMA,
    ARTIFACT_LIST_SCHEMA,
    ARTIFACT_REVERT_SCHEMA,
    ARTIFACT_SAVE_SCHEMA,
    ARTIFACT_UPDATE_SCHEMA,
    ARTIFACT_VERSIONS_SCHEMA,
    AUTONUDGE_STOP_SCHEMA,
    CHANNEL_ID_RE,
    GET_CHAT_SESSION_SCHEMA,
    LOCAL_KNOWLEDGE_SEARCH_SCHEMA,
    MAX_MEDIUM_STRING,
    MAX_SHORT_STRING,
    MCP_CORE_SCHEMAS,
    REGISTER_HOOK_SCHEMA,
    SEARCH_CHAT_HISTORY_SCHEMA,
    SPAWN_RUN_SCHEMA,
    SPAWN_SUB_AGENTS_SCHEMA,
    TASK_RUN_SCHEMA,
    WAIT_SCHEMA,
    validate_tool_args,
)


def redact(text: str) -> str:
    """Redact credentials/exfil from *text* via the active PlatformContext.

    Routes through ``current_context().credentials.redact`` so the Amazon
    companion's M365 exfil guard (or any extra credential regexes) applies when
    loaded.  The Default ``CredentialPolicy.redact`` delegates to
    ``security.redact`` — so a standalone process gets byte-for-byte today's
    redaction.  Recursion-safe: the Default delegates to ``security.redact``
    (here imported as ``_security_redact``), which never calls back into the
    context; only the *callers* were switched to this context-routed shim.
    A genuinely-unexpected context failure falls back to the bare
    ``security.redact`` so the security pass never silently disappears — but the
    fail-closed ``PlatformCompositionError`` is re-raised, never swallowed, so a
    non-standalone host that cannot compose its context does NOT silently
    downgrade redaction to the OSS baseline (which the file_send sensitivity
    gate relies on detecting internal tokens).  No logging here — ``mcp_core``
    runs as a stdio MCP server and stray writes corrupt the JSON-RPC stream.
    """
    try:
        return current_context().credentials.redact(text)
    except Exception as exc:
        if isinstance(exc, PlatformCompositionError):
            raise
        return _security_redact(text)


def _resolve_api_base() -> str:
    """Resolve the gateway API base URL from ``dashboard.url`` config."""
    cfg = KiroClawConfig.load()
    _host, port = parse_dashboard_url(cfg.dashboard.url)
    return f"http://localhost:{port}"


_API = _resolve_api_base()


def _compress_snapshot_to_outline(snapshot: str, max_lines: int = 100) -> str:
    """Compress a full accessibility snapshot into a compact outline.

    Keeps: headings, links, buttons, inputs, images with alt text, and
    structural landmarks. Strips: empty containers, decorative elements,
    redundant whitespace. Returns element refs so agent can interact
    without re-reading the full snapshot.
    """
    if not snapshot:
        return "Empty snapshot — page may not have loaded."

    lines = snapshot.split("\n")
    keep_patterns = _re.compile(
        r"(heading|link|button|textbox|combobox|checkbox|radio|tab|menu"
        r"|img|image|navigation|main|banner|contentinfo|search|alert"
        r"|dialog|listitem|row|cell|ref=)"
    )
    outline: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped == "-":
            continue
        if keep_patterns.search(stripped.lower()):
            indent = len(line) - len(line.lstrip())
            compact_indent = "  " * min(indent // 2, 4)
            outline.append(f"{compact_indent}{stripped}")
            if len(outline) >= max_lines:
                outline.append(f"... (truncated at {max_lines} lines)")
                break

    if not outline:
        total = len([ln for ln in lines if ln.strip()])
        return f"No interactive elements found in snapshot ({total} total lines). Try browser_snapshot with a more specific target."

    return f"Page outline ({len(outline)} elements):\n" + "\n".join(outline)


def _search_snapshot(snapshot: str, query: str, max_results: int = 50) -> str:
    """Search a snapshot for lines matching a query pattern."""
    if not snapshot:
        return "Empty snapshot."
    if not query:
        return "Error: query is required"

    try:
        pattern = _re.compile(query, _re.IGNORECASE)
    except _re.error:
        pattern = _re.compile(_re.escape(query), _re.IGNORECASE)

    lines = snapshot.split("\n")
    matches: list[str] = []
    for i, line in enumerate(lines, 1):
        if pattern.search(line):
            matches.append(f"L{i}: {line.strip()}")
            if len(matches) >= max_results:
                break

    if not matches:
        return f"No matches for '{query}' in snapshot ({len(lines)} lines)."

    return f"Found {len(matches)} matches:\n" + "\n".join(matches)


def _list_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "spawn_run",
            "description": (
                "Spawn subagent(s) to run tasks in the background. "
                "Returns immediately — results arrive as [Subagent completion event] "
                "messages in your conversation. For parallel work, use 'tasks' array. "
                "Tasks are automatically batched if they exceed the concurrency limit. "
                "WAIT for all completion events before responding to the user."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task": {
                        "type": "string",
                        "description": "Single task description",
                    },
                    "tasks": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Multiple tasks to run in parallel",
                    },
                    "agent": {
                        "type": "string",
                        "description": "Agent name for the subagent. Use spawn_list to see available agents.",
                    },
                    "agents": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Agent names corresponding to each task in 'tasks' array",
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Override tool-call budget for this spawn (default: config or 100)",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Optional absolute path to launch the subagent subprocess in, "
                            "instead of the default sandbox. Enables cwd-relative resource globs "
                            "(.kiro/steering, AGENTS.md, CLAUDE.md) to resolve against this directory. "
                            "Must be under a configured subagent_cwd_allowed_roots entry "
                            "(default: [~/workspace, ~/workplace]). Applies to all tasks in a batch spawn."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": (
                            "Optional model override for the subagent (e.g. 'deepseek-3.2', "
                            "'claude-haiku-4.5'). When set, the subagent runs on this model "
                            "instead of the gateway default. To discover available models, "
                            "run: kiro-cli chat --list-models --format json"
                        ),
                    },
                },
            },
        },
        {
            "name": "spawn_list",
            "description": "List all running and completed subagents (read-only, no commands executed)",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "spawn_status",
            "description": (
                "Call with the agent ID from a subagent completion event "
                "to retrieve the full output in the event of truncation."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {
                        "type": "string",
                        "description": "Subagent ID from completion event",
                    },
                },
                "required": ["agent_id"],
            },
        },
        {
            "name": "spawn_sub_agents",
            "description": (
                "Spawn one or more sub-agents to run tasks in parallel. Each sub-agent "
                "gets its own session with full tool access. BLOCKS until all sub-agents "
                "complete, then returns their collected results. Use for delegating "
                "independent subtasks to specialist agents. Preferred over spawn_run when "
                "you need results before continuing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agents": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "agent_or_mode": {
                                    "type": "string",
                                    "description": "Agent name for the sub-agent",
                                },
                                "prompt": {
                                    "type": "string",
                                    "description": "Task/prompt for the sub-agent",
                                },
                            },
                            "required": ["prompt"],
                        },
                        "description": "Array of sub-agents to spawn in parallel",
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Optional absolute path to launch sub-agents in. "
                            "Must be under a configured subagent_cwd_allowed_roots entry."
                        ),
                    },
                },
                "required": ["agents"],
            },
        },
        {
            "name": "learn_add",
            "description": (
                "Save a learned correction or preference that persists across all "
                "future sessions. MUST be called when the user corrects you, says "
                "'always do X', 'never do Y', or 'remember that'. Include both "
                "the rule (what to do) and negative (what not to do)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "rule": {"type": "string", "description": "The lesson to remember"},
                    "category": {
                        "type": "string",
                        "enum": ["tool", "preference", "knowledge"],
                        "description": "Category: tool, preference, or knowledge",
                    },
                    "negative": {
                        "type": "string",
                        "description": "What NOT to do (optional)",
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["global", "workspace"],
                        "description": "Where to save: 'global' (default, all workspaces) or 'workspace' (active workspace only)",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Workspace name (required when scope='workspace'). Use the workspace name from your session context.",
                    },
                },
                "required": ["rule", "category"],
            },
        },
        {
            "name": "learn_list",
            "description": "List all saved lessons and corrections",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "learn_remove",
            "description": "Remove lessons whose rule contains the given substring",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Substring to match"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "task_run",
            "description": (
                "Start the autonomous task runner from a spec file or inline content. "
                "Use when the user provides a task spec or says 'run this task'. "
                "For inline specs, prefix content with __inline__:"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "spec": {
                        "type": "string",
                        "description": "Path to spec file, or inline content prefixed with __inline__:",
                    },
                    "name": {
                        "type": "string",
                        "description": "Human-readable task name (auto-derived from spec if omitted)",
                    },
                },
                "required": ["spec"],
            },
        },
        {
            "name": "wait",
            "description": (
                "Pause execution for a specified duration while preserving full session "
                "context. Use when waiting for external systems (AutoSDE review, CI "
                "pipeline, deployment). Max 1800s (30 min)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "seconds": {
                        "type": "integer",
                        "description": "Duration to wait in seconds (60-1800)",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why we are waiting (shown to user)",
                    },
                },
                "required": ["seconds", "reason"],
            },
        },
        {
            "name": "register_hook",
            "description": (
                "Register a webhook listener so an external system can inject a message "
                "into a dedicated agent session later. Returns the webhook URL and session "
                "key. Use this when you need to hand off to an external process (e.g. "
                "submit a code review, then wait for AutoSDE to call back with results). "
                "The external system POSTs to the returned URL with the results."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "hook_id": {
                        "type": "string",
                        "description": "Unique identifier for this hook (e.g. 'autosde:pr-123')",
                    },
                    "context_summary": {
                        "type": "string",
                        "description": "Summary of current work context for session resume",
                    },
                },
                "required": ["hook_id", "context_summary"],
            },
        },
        {
            "name": "send_message",
            "description": (
                "Send a message to the user. By default delivers a dashboard "
                'notification only. Set session="slack" to also send a Slack DM. '
                "Set 'channel' to target a tracked channel, or 'user' to DM an "
                "allowed user — specify at most one, not both. "
                "Use this whenever you decide someone should be notified — most "
                "commonly in silent cron jobs, but applicable any time proactive "
                "notification is needed."
                "\n\nsession param (optional):"
                "\n  omitted  — dashboard notification only (default)."
                '\n  "slack"  — Slack DM + dashboard notification.'
                '\n  "origin" — inject into the dashboard session that spawned'
                " this cron. Falls through to notification-only if origin is"
                " unreachable (tab closed, history deleted, or cron has no origin)."
                "\n\nExplicit channel=... or user=... always sends to Slack."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Message text. Also used as fallback when blocks are provided.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional title for the notification",
                    },
                    "blocks": {
                        "type": "array",
                        "description": "Optional Slack Block Kit blocks array. When provided, the message is sent as a rich Block Kit message with text as fallback.",
                        "items": {"type": "object"},
                        "maxItems": 50,
                    },
                    "channel": {
                        "type": "string",
                        "description": "Target channel ID (e.g. C0123ABC456). Must be a tracked channel. Omit to send to owner DM.",
                    },
                    "user": {
                        "type": "string",
                        "description": "Target user ID (e.g. U0123ABC456) to DM. Must be an allowed user. Omit to send to owner DM.",
                    },
                    "unfurl_links": {
                        "type": "boolean",
                        "description": "Whether to unfurl URL link previews. Defaults to true.",
                    },
                    "unfurl_media": {
                        "type": "boolean",
                        "description": "Whether to unfurl media (images/video) previews. Defaults to true.",
                    },
                    "thread_ts": {
                        "type": "string",
                        "description": (
                            "Optional Slack thread timestamp (e.g. '1712793600.123456'). "
                            "When provided, the message is posted as a threaded reply under "
                            "that parent message. Works with 'channel' (thread in channel) "
                            "or 'user' (thread in DM)."
                        ),
                    },
                    "reply_broadcast": {
                        "type": "boolean",
                        "description": (
                            "When true and 'thread_ts' is set, also broadcast the threaded reply "
                            "to the channel's main message list. Requires 'thread_ts' — passing "
                            "reply_broadcast=true without thread_ts returns 400. Defaults to false."
                        ),
                    },
                    "session": {
                        "type": "string",
                        "enum": ["origin", "slack"],
                        "description": (
                            "Delivery routing. Omit for notification bell only (default). "
                            '"slack" adds Slack DM delivery. '
                            '"origin" injects into the dashboard session that spawned '
                            "this cron (falls back to notification if unreachable)."
                        ),
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "delete_message",
            "description": (
                "Delete a message previously sent by this bot. Only works on "
                "messages authored by the KiroClaw bot itself (Slack API constraint). "
                "Use to clean up transient notifications after the user acknowledges them."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "channel": {
                        "type": "string",
                        "description": "Channel ID where the message was posted.",
                    },
                    "ts": {
                        "type": "string",
                        "description": "Timestamp of the message to delete (from send_message response).",
                    },
                },
                "required": ["channel", "ts"],
            },
        },
        {
            "name": "read_slack_profile",
            "description": (
                "Read a Slack user's profile. Returns display name, title, "
                "status, timezone, and other profile fields. Rate limited to "
                "5 lookups per minute."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user": {
                        "type": "string",
                        "description": "Slack user ID (e.g. U0123ABC456).",
                    },
                },
                "required": ["user"],
            },
        },
        {
            "name": "file_send",
            "description": (
                "Send a file to the user. Copies the file to the outbox and "
                "notifies the dashboard/Slack with a download link. Use when "
                "you've generated a report, export, artifact, or any file the "
                "user should receive."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file to send"},
                    "description": {
                        "type": "string",
                        "description": "Brief description of what the file is",
                    },
                    "channel": {
                        "type": "string",
                        "description": (
                            "Optional Slack channel ID (e.g. C0123ABC456) to upload "
                            "the file to. Must be a tracked channel the bot is a "
                            "member of. Omit to send to the owner's DM."
                        ),
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "artifact_save",
            "description": (
                "Save a chat-rendered artifact (typically the HTML body of an "
                "<mcwidget>) so the user can find, view, and iterate on it later. "
                "Returns the slug — a stable handle the user (and you) can "
                "reference in future sessions ('iterate on artifact <slug>'). "
                "Use this when the user asks to save a widget, when you create "
                "something worth keeping, or before iterating (use artifact_update "
                "for the iteration step itself)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Human-readable name (e.g. 'CR Queue Dashboard'). Used to derive the slug if omitted.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Artifact content. For widgets, the inner HTML of the <mcwidget> tag (NOT the surrounding tag itself).",
                    },
                    "slug": {
                        "type": "string",
                        "description": "Optional explicit slug (lowercase, digits, hyphens). Auto-derived from name when omitted.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["widget", "html", "markdown", "svg", "json", "text"],
                        "description": "Artifact kind. Default: widget.",
                    },
                    "source": {
                        "type": "string",
                        "enum": ["chat", "cron", "subagent", "manual", "import"],
                        "description": "Provenance marker. Default: chat.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Short description of what the artifact shows or does.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for filtering in the library (max 16).",
                    },
                },
                "required": ["name", "content"],
            },
        },
        {
            "name": "artifact_get",
            "description": (
                "Load an artifact by slug. Returns the metadata and content. "
                "Use this before artifact_update to read the current HTML when "
                "the user asks to iterate on an existing artifact."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug (lowercase, digits, hyphens).",
                    },
                    "version": {
                        "type": "integer",
                        "description": "Specific version to read. Omit for current.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_update",
            "description": (
                "Update an artifact's live state. Each agent edit "
                "automatically creates a new version (like a git commit) — "
                "the user can revert to any prior agent iteration via "
                "artifact_revert. Use after artifact_get when iterating "
                "on an existing artifact at the user's request."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to update.",
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "New content. Each call records a new version "
                            "automatically when invoked via MCP."
                        ),
                    },
                    "name": {
                        "type": "string",
                        "description": "New name (optional rename).",
                    },
                    "description": {
                        "type": "string",
                        "description": "New description (optional).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Replacement tag list (optional).",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_revert",
            "description": (
                "Revert an artifact's live state to a prior version. Reads "
                "version N's content and writes it as the new live state, "
                "creating a fresh snapshot tagged 'reverted' so the activity "
                "timeline shows the rollback. Use this instead of "
                "artifact_update when the user asks to undo recent changes "
                "or restore an earlier state — it avoids the agent having "
                "to manually fetch the old content first."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to revert.",
                    },
                    "target_version": {
                        "type": "integer",
                        "description": (
                            "Version number to restore. Use artifact_versions "
                            "first to list available versions."
                        ),
                        "minimum": 1,
                    },
                },
                "required": ["slug", "target_version"],
            },
        },
        {
            "name": "artifact_list",
            "description": (
                "List saved artifacts. Optionally filter by tag, kind, or "
                "name substring. Use this to discover what artifacts exist "
                "before iterating, or when the user asks 'what have we saved?'"
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "tag": {"type": "string", "description": "Filter by tag."},
                    "kind": {
                        "type": "string",
                        "enum": ["widget", "html", "markdown", "svg", "json", "text"],
                        "description": "Filter by kind.",
                    },
                    "q": {
                        "type": "string",
                        "description": "Case-insensitive substring filter on artifact name.",
                    },
                },
            },
        },
        {
            "name": "artifact_versions",
            "description": (
                "List the version numbers stored for an artifact. Use this "
                "before artifact_get with an explicit version to figure out "
                "what's available."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "artifact_delete",
            "description": (
                "Permanently delete an artifact and all its versions. Use only "
                "when the user explicitly asks to remove an artifact."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Artifact slug to delete.",
                    },
                },
                "required": ["slug"],
            },
        },
        {
            "name": "autonudge_stop",
            "description": (
                "Stop the auto-nudge loop driving your current session. Call this "
                "when you determine the loop should halt (e.g. goal complete, "
                "blocked on user input, or a STOP sentinel file indicates shutdown). "
                "Removes the loop from the AutoNudgeService so no further nudges "
                "fire into this session. Safe to call even if no loop is active — "
                "returns a no-op message."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why the loop is being stopped (logged for audit)",
                    },
                },
            },
        },
        {
            "name": "local_knowledge_search",
            "description": (
                "Search the user's knowledge library. Call ONLY when the user's "
                "message contains one of these explicit signals:\n"
                "- Asks 'what do we know about X' or 'check knowledge for X'\n"
                "- References a specific document, wiki, or stored content by name\n"
                "- Says 'in my docs', 'in my notes', 'according to our knowledge'\n"
                "- Asks a factual question AND mentions a topic you know is in "
                "their knowledge base\n\n"
                "Do NOT call for: general coding questions, file operations, "
                "debugging, or any task you can answer from context alone."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query to find relevant knowledge chunks",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 3, max 5)",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "search_chat_history",
            "description": (
                "Search your own past conversation transcripts (chat history) by "
                "keyword and get back ranked, snippet-level hits. Use this to "
                "recover context that is NOT in your injected memory — e.g. 'what "
                "did we decide about X three weeks ago', 'the error message from "
                "that debugging session', a name/number/path mentioned earlier. "
                "Search like a human: try a query, read the snippets, then re-search "
                "with different keywords if the first hit isn't right. Returns "
                "metadata + a short snippet per session (NOT full transcripts) — "
                "call get_chat_session with a returned session_key to read the full "
                "thread once a hit looks promising. Scoped to your current workspace "
                "by default. This is a READ — it never modifies memory or history."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keyword(s) to search for in past conversations.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 10, max 50).",
                        "default": 10,
                    },
                    "before": {
                        "type": "string",
                        "description": "Optional ISO date (YYYY-MM-DD); only sessions modified before this day.",
                    },
                    "after": {
                        "type": "string",
                        "description": "Optional ISO date (YYYY-MM-DD); only sessions modified on/after this day.",
                    },
                    "all_workspaces": {
                        "type": "boolean",
                        "description": "Search across all workspaces instead of just the current one (default false).",
                        "default": False,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "get_chat_session",
            "description": (
                "Read the full message transcript of one past conversation, "
                "identified by a session_key returned from search_chat_history. "
                "Returns the messages as role/content pairs, tail-capped at "
                "max_messages. Use after search_chat_history when a snippet hit "
                "looks like the thread you need. Refuses incognito/temporary "
                "sessions. This is a READ — it never modifies memory or history."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "session_key": {
                        "type": "string",
                        "description": "The session_key from a search_chat_history result.",
                    },
                    "max_messages": {
                        "type": "integer",
                        "description": "Max (most recent) messages to return (default 50, max 200).",
                        "default": 50,
                    },
                    "all_workspaces": {
                        "type": "boolean",
                        "description": "Allow reading a session from a different workspace than the caller's (default false — deny cross-workspace).",
                        "default": False,
                    },
                },
                "required": ["session_key"],
            },
        },
        {
            "name": "browse_outline",
            "description": (
                "Compress a browser snapshot into a compact outline with element refs. "
                "Use AFTER calling browser_snapshot to reduce a large accessibility tree "
                "(50-100K tokens) into a navigable outline (~2-5K tokens). "
                "Returns interactive elements with refs for clicking, plus page structure."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot": {
                        "type": "string",
                        "description": "The raw browser_snapshot output text to compress",
                    },
                    "max_lines": {
                        "type": "integer",
                        "description": "Max output lines (default 100)",
                        "default": 100,
                    },
                },
                "required": ["snapshot"],
            },
        },
        {
            "name": "browse_search",
            "description": (
                "Search a browser snapshot for specific text or patterns. "
                "Returns matching lines with element refs. Use instead of reading "
                "the full snapshot when looking for specific content on a page."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "snapshot": {
                        "type": "string",
                        "description": "The raw browser_snapshot output text to search",
                    },
                    "query": {
                        "type": "string",
                        "description": "Text or regex pattern to search for",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max matching lines to return (default 50)",
                        "default": 50,
                    },
                },
                "required": ["snapshot", "query"],
            },
        },
    ]


def _internal_secret() -> str:
    """Read the per-session secret for IPC authentication."""
    try:
        return (config_dir() / ".local_secret").read_text().strip()
    except Exception:
        return ""


def _get_ppid(pid: int) -> int:
    """Get parent PID cross-platform. Returns 0 on failure."""
    try:
        if platform.system() == "Linux":
            for line in Path(f"/proc/{pid}/status").read_text().splitlines():
                if line.startswith("PPid:"):
                    return int(line.split()[1])
        else:
            out = subprocess.check_output(
                ["ps", "-o", "ppid=", "-p", str(pid)], text=True, timeout=2
            )
            return int(out.strip())
    except Exception:
        pass
    return 0


# ── Knowledge-search store/embedder cache ──
#
# local_knowledge_search runs per LLM tool call in a long-lived MCP server.
# Rebuilding KnowledgeStore every call re-runs the schema DDL, an orphan-cleanup
# DELETE transaction, and a full SELECT of all entities/relations into the
# in-memory graph; rebuilding the embedder re-runs Ollama's /api/tags probe
# (up to 3s when configured). We cache both, keyed on a signature of the DB
# files (main + -wal, since WAL commits land in -wal) and config.json, so
# out-of-band dashboard ingestion or config edits trigger a rebuild on the next
# call. The MCP stdio loop services calls serially, but a lock keeps this safe
# if that ever changes.
_KNOWLEDGE_CACHE_LOCK = threading.Lock()
# (signature_tuple, KnowledgeStore, embedder_or_None)
_KNOWLEDGE_CACHE: tuple[tuple, Any, Any] | None = None


def _knowledge_db_signature(db_path: Path, cfg_path: Path) -> tuple:
    """Cheap fingerprint of the knowledge DB (+WAL) and config files.

    Any ingestion (which writes the main DB or its -wal sidecar) or config edit
    changes this, busting the cache so a fresh search sees new data / embedder.
    """
    sig: list = []
    wal_path = db_path.with_name(db_path.name + "-wal")
    for p in (db_path, wal_path, cfg_path):
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            sig.append((str(p), None))
    return tuple(sig)


def _get_knowledge_search(db_path: Path, cfg_path: Path) -> tuple[Any, Any]:
    """Return a cached ``(KnowledgeStore, embedder)`` pair, rebuilding on change.

    Rebuilds (and closes the prior connection) only when the DB/WAL/config
    signature changes; otherwise reuses the live store + embedder, avoiding the
    per-call schema/migrate/graph-load and Ollama availability probe.
    """
    global _KNOWLEDGE_CACHE
    sig = _knowledge_db_signature(db_path, cfg_path)
    with _KNOWLEDGE_CACHE_LOCK:
        if _KNOWLEDGE_CACHE is not None and _KNOWLEDGE_CACHE[0] == sig:
            return _KNOWLEDGE_CACHE[1], _KNOWLEDGE_CACHE[2]
        # Rebuild. Build the new store FIRST; only close the stale connection
        # after the build succeeds. If KnowledgeStore.__init__ raises (locked or
        # corrupt DB, disk-full during the migrate DELETE), we leave the existing
        # cache entry — and its still-open connection — intact rather than
        # stranding a closed connection in the cache for the next caller.
        prev = _KNOWLEDGE_CACHE
        store = KnowledgeStore(str(db_path))
        try:
            cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        except Exception:
            cfg = {}
        embedder = create_embedder_from_config(cfg)
        # Close the stale connection only AFTER the full rebuild (store + cfg +
        # embedder) succeeds. If any step above raised, the existing cache entry
        # — and its open connection — is left intact and usable for the next call.
        if prev is not None:
            with contextlib.suppress(Exception):
                prev[1].db.close()
        # Re-fingerprint AFTER building: KnowledgeStore.__init__ creates/migrates
        # the DB (writing the file + -wal), so the pre-build signature no longer
        # matches the on-disk state. Caching under the post-build signature lets
        # the next idle call hit the cache instead of rebuilding every time.
        post_sig = _knowledge_db_signature(db_path, cfg_path)
        _KNOWLEDGE_CACHE = (post_sig, store, embedder)
        return store, embedder


def _resolve_session_key() -> str:
    """Return the real session key, falling back to PID file when env var is absent.

    Warm-pool kiro-cli processes have no KIROCLAW_SESSION_KEY env var (the pool
    spawns with an empty key so rekey() + PID file provide the correct mapping).

    After rekey, the process tree may be: gateway -> kiro-cli (pool, has PID file)
    -> kiro-cli-chat (forked child) -> MCP server.  os.getppid() returns the
    immediate parent (kiro-cli-chat) which has no PID file.  Walk up ancestors
    until we find a matching file or hit init.
    """
    sk = os.environ.get("KIROCLAW_SESSION_KEY", "")
    if sk:
        return sk
    try:
        cfg_dir = config_dir()
        pid = os.getppid()
        seen: set[int] = set()
        while pid > 1 and pid not in seen:
            seen.add(pid)
            pid_file = cfg_dir / f"session_pid_{pid}.txt"
            if pid_file.exists():
                return pid_file.read_text(encoding="utf-8").strip()
            pid = _get_ppid(pid)
    except Exception:
        pass
    return ""


def _post(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    headers = {"Content-Type": "application/json", "X-Internal-Secret": _internal_secret()}
    sk = _resolve_session_key()
    if sk:
        headers["X-Session-Key"] = sk
    req = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _get(path: str) -> dict:
    headers = {"X-Internal-Secret": _internal_secret()}
    sk = _resolve_session_key()
    if sk:
        headers["X-Session-Key"] = sk
    req = urllib.request.Request(
        f"{_API}{path}",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _delete(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode() if body else None
    headers = {"X-Internal-Secret": _internal_secret()}
    sk = _resolve_session_key()
    if sk:
        headers["X-Session-Key"] = sk
    if data:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(
        f"{_API}{path}",
        data=data,
        headers=headers,
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def _artifact_reemit_hint(slug: str, name: str, kind: str = "widget") -> str:
    """Render the canonical re-emit-this-artifact-in-chat instruction.

    Appended to artifact_save / artifact_get / artifact_update tool
    responses so the agent has the exact tag string in context at the
    moment it's about to render the artifact in chat. The artifacts
    skill says ``slug=`` is required on every re-emission of a saved
    artifact, but skill rules can be overlooked at emission time —
    Mesh-1715 session logs confirmed an LLM had the slug in front of
    it twice (artifact_get response + artifact_update response) and
    still emitted ``<mcwidget title="...">`` without the attribute,
    creating a duplicate artifact when the user clicked save.

    The hint reduces this to "copy the tag I just gave you."
    """
    if kind != "widget":
        # Non-widget artifacts (markdown, html, svg, json, text) don't
        # round-trip through `<mcwidget>` — they render via the artifact
        # detail page or MarkdownPanel. No re-emit hint needed.
        return ""
    safe_name = (name or "").replace('"', "'")
    return (
        "When you re-emit this widget in chat, use this exact opening tag\n"
        "(slug attribute is REQUIRED — without it, the user clicking save\n"
        "creates a duplicate artifact):\n\n"
        f'<mcwidget title="{safe_name}" slug="{slug}">'
    )


def _validate_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Validate tool arguments against schema. Returns cleaned args."""
    schema = MCP_CORE_SCHEMAS.get(name)
    if schema:
        return validate_tool_args(args, schema)
    return args  # tools without schemas (learn_list) pass through


def _current_session_thread_ts() -> str | None:
    """Read the current session's thread_ts from the most recent session_pid file."""
    try:
        pid_files = sorted(
            (Path.home() / ".kiroclaw").glob("session_pid_*.txt"),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if pid_files:
            raw = safe_read_file_bytes(str(pid_files[0]))
            if raw is None:
                return None
            ts = raw.decode("utf-8").strip()
            if ts and not ts.startswith("dashboard:"):
                return ts
    except Exception:
        pass
    return None


def _call_tool(name: str, raw_args: dict[str, Any]) -> str:
    return call_tool_with_logging(
        name,
        raw_args,
        _validate_args,
        _call_tool_inner,
        session_key="mcp_core",
        downstream_service="kiroclaw-core",
    )


# ── Chat-history search helpers (Phase 1: search_chat_history / get_chat_session) ──

_HISTORY_INCOGNITO_MODES = frozenset({"incognito", "temporary"})
_SNIPPET_RADIUS = 120  # chars of context kept on each side of a match
_SNIPPET_MAX_LEN = 320  # hard cap on a returned snippet


def _history_is_incognito(meta: dict) -> bool:
    """True if a session's memory_mode marks it private (never searchable)."""
    return str(meta.get("memory_mode", "")).lower() in _HISTORY_INCOGNITO_MODES


def _redact_history_output(text: str) -> str:
    """Apply the standard dual redaction to any chat-history tool output.

    Used on EVERY return path (including early-return error strings that echo an
    LLM-supplied session_key) so nothing reaches the dashboard unredacted.

    Routes through the context-aware :func:`redact` shim so the companion's extra
    credential patterns apply to verbatim chat-transcript egress; the Default
    ``CredentialPolicy`` delegates to ``security.redact`` (the same
    exfil-then-credential dual pass), so standalone is byte-for-byte unchanged.
    """
    return redact(text)


def _parse_iso_date_epoch(date_str: str) -> float | None:
    """Parse a YYYY-MM-DD string to a UTC midnight epoch. None on bad input."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return None


def _extract_history_snippet(messages: list[dict], needle: str) -> str:
    """Return a bounded snippet around the first message matching *needle*.

    The matched substring is delimited with ``<<<...>>>``. Returns "" when no
    message content contains the needle (e.g. it only matched the title).
    """
    # Defense-in-depth: an empty/whitespace needle makes str.find return 0 on
    # every message and would wrap meaningless text in <<<>>>. The query is
    # already validated non-empty upstream, but guard here too since this helper
    # is independently callable.
    if not needle.strip():
        return ""
    needle_cf = needle.casefold()
    for m in messages:
        content = m.get("content")
        if not isinstance(content, str) or not content:
            continue
        idx = content.casefold().find(needle_cf)
        if idx < 0:
            continue
        start = max(0, idx - _SNIPPET_RADIUS)
        end = min(len(content), idx + len(needle) + _SNIPPET_RADIUS)
        seg = content[start:end]
        rel = seg.casefold().find(needle_cf)
        if rel >= 0:
            seg = seg[:rel] + "<<<" + seg[rel : rel + len(needle)] + ">>>" + seg[rel + len(needle) :]
        seg = ("…" if start > 0 else "") + seg + ("…" if end < len(content) else "")
        result = seg[:_SNIPPET_MAX_LEN]
        # If the hard cap sliced through the match delimiters (possible with a
        # long query), re-close so the consumer never sees a dangling "<<<".
        if "<<<" in result and ">>>" not in result:
            result = result[: _SNIPPET_MAX_LEN - 3] + ">>>"
        return result
    return ""


def _call_tool_inner(name: str, args: dict[str, Any]) -> str:
    if name == "spawn_run":
        # Re-validate to make schema enforcement visible at the extraction point.
        # _call_tool() already validates, but defense-in-depth ensures agent/agents
        # are schema-clean even if the call chain changes.
        args = validate_tool_args(args, SPAWN_RUN_SCHEMA)

        tasks = args.get("tasks")
        task = args.get("task")

        # Support both single task and batch tasks
        if tasks and isinstance(tasks, list):
            task_list = [t for t in tasks if isinstance(t, str) and t.strip()]
        elif task:
            task_list = [task]
        else:
            return "Error: task or tasks is required"

        # Read parent session key so completions inject back into this session.
        parent_session = _resolve_session_key()

        # Fire-and-forget — gateway's SubagentManager queues excess tasks
        # and auto-spawns them as slots free up.
        agent = args.get("agent") or ""
        agents_list = args.get("agents") or []
        max_turns = args.get("max_turns") or 0
        cwd = args.get("cwd") or ""
        model = args.get("model") or ""
        if agents_list and len(agents_list) != len(task_list):
            return f"Error: agents length ({len(agents_list)}) must match tasks length ({len(task_list)})"

        agent_ids: list[str] = []
        agent_names: list[str] = []
        errors: list[str] = []
        for i, t in enumerate(task_list):
            a = agents_list[i] if agents_list else agent
            body: dict[str, Any] = {"task": t, "agent": a, "parent_session": parent_session}
            if max_turns:
                body["max_turns"] = max_turns
            if cwd:
                body["cwd"] = cwd
            if model:
                body["model"] = model
            d = _post("/api/spawn", body)
            if d.get("error"):
                errors.append(f"{t[:60]}: {d['error']}")
                continue
            agent_ids.append(d.get("id", "?"))
            agent_names.append(a)

        spawn_lines: list[str] = []
        if agent_ids:
            spawn_lines.append(
                f"Spawned {len(agent_ids)} subagent(s). Results will arrive as completion events:"
            )
            for aid, a, t in zip(agent_ids, agent_names, task_list):
                label = f"{aid} ({a})" if a else aid
                spawn_lines.append(f"  {label}: {t[:80]}")
        if errors:
            spawn_lines.append(f"\n{len(errors)} task(s) queued (at capacity):")
            for e in errors:
                spawn_lines.append(f"  - {e}")
        if agent_ids:
            spawn_lines.append(
                "\nWait for [Subagent completion event] messages before responding to the user."
            )
        else:
            spawn_lines.append("All tasks queued — results will arrive as completion events.")
        return "\n".join(spawn_lines)

    if name == "spawn_sub_agents":
        args = validate_tool_args(args, SPAWN_SUB_AGENTS_SCHEMA)
        agents_input = args.get("agents")
        if not agents_input or not isinstance(agents_input, list):
            return "Error: 'agents' array is required"
        cwd = args.get("cwd") or ""
        parent_session = _resolve_session_key()

        def _redact_sa(text: str) -> str:
            return redact(text)

        # Validate individual agent entries (schema guarantees dict entries)
        for entry in agents_input:
            p = entry.get("prompt", "")
            if len(p) > MAX_MEDIUM_STRING:
                entry["prompt"] = p[:MAX_MEDIUM_STRING]
            a = entry.get("agent_or_mode", "")
            if len(a) > MAX_SHORT_STRING:
                entry["agent_or_mode"] = a[:MAX_SHORT_STRING]

        sel().log_tool_invocation(
            session_key=parent_session or "",
            source="mcp_core",
            tool_name="spawn_sub_agents",
            outcome="attempt",
            metadata={"agent_count": len(agents_input)},
        )

        sa_ids: list[str] = []
        sa_errors: list[str] = []
        for entry in agents_input:
            prompt = entry.get("prompt", "").strip()
            if not prompt:
                continue
            sa_agent = entry.get("agent_or_mode") or ""
            sa_body = {
                "task": prompt,
                "agent": sa_agent,
                "parent_session": parent_session,
            }
            if cwd:
                sa_body["cwd"] = cwd
            d = _post("/api/spawn", sa_body)
            if d.get("error"):
                sa_errors.append(f"{_redact_sa(prompt[:60])}: {_redact_sa(d['error'])}")
            else:
                aid = d.get("id", "")
                if aid:
                    sa_ids.append(aid)
                else:
                    sa_errors.append(
                        f"{_redact_sa(prompt[:60])}: spawn returned no agent id"
                    )

        if not sa_ids and sa_errors:
            return "Error spawning sub-agents:\n" + "\n".join(f"  - {e}" for e in sa_errors)
        if not sa_ids:
            return "Error: no valid agent entries found in 'agents' array"

        # Poll until all sub-agents complete. Ping /api/session-keepalive every
        # 60s so the gateway's is_responsive() does not flag this session as
        # stale and SIGTERM the ACP subprocess mid-poll, which would abort the
        # very sub-agents we are waiting on.
        poll_interval = 2.0
        try:
            max_wait = float(os.environ.get("KIROCLAW_SPAWN_SUB_AGENTS_MAX_WAIT", "7200"))
        except (TypeError, ValueError):
            max_wait = 7200.0
        max_wait = max(60.0, min(7200.0, max_wait))  # clamp: 1 min .. 2 hours
        deadline = time.monotonic() + max_wait
        _next_ping = time.monotonic() + 60.0  # first keepalive after 60s, not immediately
        while time.monotonic() < deadline:
            if time.monotonic() >= _next_ping:
                try:
                    _post("/api/session-keepalive", {})
                except Exception:
                    pass  # keepalive is best-effort
                _next_ping = time.monotonic() + 60.0
            all_done = True
            for aid in sa_ids:
                sa_st = _get(f"/api/spawn/{aid}")
                # An errored/crashed agent is "settled" — without this, an agent
                # that never sets done=True would spin the loop until max_wait.
                if not (sa_st.get("done") or sa_st.get("error")):
                    all_done = False
                    break
            if all_done:
                break
            time.sleep(poll_interval)

        # Collect results
        sa_results: list[str] = []
        completed = 0
        timed_out = 0
        errored = 0
        for aid in sa_ids:
            sa_st = _get(f"/api/spawn/{aid}")
            sa_name = _redact_sa(sa_st.get("agent", ""))
            label = sa_name if sa_name else aid
            if sa_st.get("error"):
                errored += 1
                sa_results.append(
                    json.dumps({
                        "agent": label, "status": "error",
                        "error": _redact_sa(sa_st["error"]),
                    })
                )
            elif not sa_st.get("done"):
                timed_out += 1
                sa_results.append(
                    json.dumps({"agent": label, "status": "timed_out"})
                )
            else:
                completed += 1
                result_text = _redact_sa(sa_st.get("result", ""))
                sa_results.append(
                    json.dumps({
                        "agent": label, "status": "completed", "text": result_text,
                    })
                )
        if sa_errors:
            sa_results.append(
                json.dumps({"status": "spawn_errors", "errors": sa_errors})
            )
        sel().log_tool_invocation(
            session_key=parent_session or "",
            source="mcp_core",
            tool_name="spawn_sub_agents",
            outcome="completed" if not timed_out and not errored else "partial",
            metadata={
                "spawned": len(sa_ids),
                "completed": completed,
                "timed_out": timed_out,
                "errored": errored,
            },
        )
        return "\n\n".join(sa_results)

    if name == "spawn_list":
        d = _get("/api/spawn")
        agents = d.get("agents", [])

        def _redact(text: str) -> str:
            return redact(text)

        lines: list[str] = []
        if not agents:
            lines.append("No subagents running.")
        else:
            for a in agents:
                status = "done" if a.get("done") else "running"
                err = f" error: {_redact(a['error'])}" if a.get("error") else ""
                progress = ""
                if not a.get("done"):
                    turns = a.get("turns", 0)
                    tool = _redact(a.get("last_tool", ""))
                    elapsed = a.get("elapsed", 0)
                    parts = [f"{elapsed}s"]
                    if turns:
                        parts.append(f"{turns} turns")
                    if tool:
                        parts.append(tool)
                    progress = f" ({', '.join(parts)})"
                lines.append(f"{a['id']}  [{status}]{err}{progress}  {_redact(a['task'])[:60]}")
        # Always append available agents (fresh read from disk)
        try:
            names = [
                _redact(a.name) for a in list_agents() if a.name.isascii() and len(a.name) < 100
            ]
            if names:
                lines.append(f"\nAvailable agents: {', '.join(names)}")
        except Exception:
            pass  # list_agents failure is non-critical
        return "\n".join(lines)

    if name == "spawn_status":
        agent_id = args.get("agent_id", "")
        if not agent_id or not agent_id.isalnum():
            return "Error: invalid agent_id"
        d = _get(f"/api/spawn/{agent_id}")
        if d.get("error"):
            return f"Error: {d['error']}"

        result = d.get("result") or "_No result._"
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)
        return result

    if name == "learn_add":
        rule = args.get("rule", "")
        category = args.get("category", "knowledge")
        if not rule:
            return "Error: rule is required"
        scope = args.get("scope", "global")
        payload: dict[str, str] = {"rule": rule, "category": category, "scope": scope}
        if scope == "workspace":
            ws = args.get("workspace", "")
            if not ws:
                return "Error: workspace name is required when scope='workspace'"
            payload["workspace"] = ws
        d = _post("/api/lessons", payload)
        err_val = d.get("error")
        if err_val:
            # Map the backend session-scope error to a user-actionable
            # message so the LLM can explain the situation instead of
            # leaking an opaque HTTP 400 as a "transport failed" error.
            # See api_lessons_create in dashboard/handlers/cron.py: the
            # "unknown session" response is returned when the X-Session-Key
            # matches neither a live in-memory slot, a restricted key, the
            # slack: namespace, nor a persisted session JSONL — so the
            # remaining cases are genuinely unrecognised keys (forged, or
            # ephemeral/incognito sessions that never wrote to disk), not
            # merely evicted real sessions.
            if "unknown session" in str(err_val):
                return (
                    "Lesson was NOT saved: this session is not recognised "
                    "by the gateway (no active slot, restricted key, or "
                    "persisted history found for this session key). Start "
                    "a new Slack thread or dashboard tab and re-state the "
                    "lesson you want to save — it will not carry over "
                    "from this session automatically."
                )
            return f"Error: {err_val}"
        return f"Saved lesson ({scope}): {rule}"

    if name == "learn_list":
        d = _get("/api/lessons")
        lessons = d.get("lessons", [])
        if not lessons:
            return "No lessons saved."
        lines = []
        for le in lessons:
            lines.append(f"[{le.get('category', '?')}] {le['rule']}")
        return "\n".join(lines)

    if name == "learn_remove":
        query = args["query"]
        d = _delete("/api/lessons", {"rule": query})
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Removed lessons matching: {query}"

    if name == "task_run":
        args = validate_tool_args(args, TASK_RUN_SCHEMA)
        spec = args["spec"]
        task_name = args.get("name", "")
        _src = "cron" if _resolve_session_key().startswith("cron:") else "mcp"
        d = _post("/api/taskrunner", {"spec": spec, "name": task_name, "source": _src})
        if d.get("error"):
            return f"Error: {d['error']}"

        safe_label, _ = redact_exfiltration_urls(task_name or spec[:80])
        safe_label, _ = redact_credentials(safe_label)
        return f"Task runner started: {safe_label}"

    if name == "wait":

        args = validate_tool_args(args, WAIT_SCHEMA)

        seconds = max(60, min(1800, int(args.get("seconds", 300))))
        reason = str(args.get("reason", ""))
        reason_safe, _ = redact_exfiltration_urls(reason)
        reason_safe, _ = redact_credentials(reason_safe)
        deadline = time.monotonic() + seconds
        # Ping session-keepalive every 60s so the gateway's is_responsive()
        # doesn't flag this session as stale and SIGTERM the ACP subprocess.
        # See taskei f361a79a-ce4f-4b82-a96a-2acdc7e582f4.
        _next_ping = time.monotonic()
        while True:
            now = time.monotonic()
            remaining = deadline - now
            if remaining <= 0:
                break
            if now >= _next_ping:
                try:
                    _post("/api/session-keepalive", {})
                except Exception:
                    pass  # keepalive is best-effort
                _next_ping = now + 60.0
            time.sleep(min(5, remaining))
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="wait",
            outcome="success",
        )
        return f"Waited {seconds}s. Resuming: {reason_safe}"

    if name == "register_hook":

        args = validate_tool_args(args, REGISTER_HOOK_SCHEMA)

        hook_id = str(args.get("hook_id", "")).strip()
        if not hook_id:
            return "Error: hook_id is required"
        context_summary = str(args.get("context_summary", ""))
        session_key = f"hook:{hook_id}"
        # Persist hook registration
        hook_file = Path.home() / ".kiroclaw" / "hooks.json"
        hook_file.parent.mkdir(parents=True, exist_ok=True)
        lock_path = hook_file.parent / "hooks.json.lock"
        with open(lock_path, "w") as lock_fd:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            # Re-read under lock to avoid lost updates
            hooks = {}
            if hook_file.exists():
                try:
                    hooks = json.loads(hook_file.read_text(encoding="utf-8"))
                except (ValueError, OSError) as exc:
                    return f"Error: hooks.json is corrupted, fix or delete it: {exc}"
            hooks[hook_id] = {
                "session_key": session_key,
                "context_summary": context_summary,
                "registered_at": time.time(),
                "compat_flags": 0x4D43,
            }
            fd, tmp = tempfile.mkstemp(dir=str(hook_file.parent), suffix=".tmp")
            try:
                try:
                    os.write(fd, json.dumps(hooks, indent=2).encode("utf-8"))
                    os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(tmp, str(hook_file))
            except BaseException:
                os.unlink(tmp)
                raise
        # Resolve webhook URL
        parsed = urlparse(_API)
        base = f"{parsed.scheme}://{parsed.hostname}"
        if parsed.port:
            base += f":{parsed.port}"
        url = f"{base}/api/hooks/agent"
        hook_id_safe, _ = redact_exfiltration_urls(hook_id)
        hook_id_safe, _ = redact_credentials(hook_id_safe)
        session_key_safe = f"hook:{hook_id_safe}"
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="register_hook",
            outcome="success",
        )
        return (
            f"Hook registered: {hook_id_safe}\n"
            f"Session key: {session_key_safe}\n"
            f"Webhook URL: {url}\n"
            f"External systems should POST to this URL with:\n"
            f'  {{"message": "<results>", "sessionKey": "{session_key_safe}", '
            f'"name": "{hook_id_safe}"}}\n'
            f"Include Authorization: Bearer <webhook_token> header.\n"
            f"Context summary saved for session resume."
        )

    if name == "send_message":
        text = args["text"]
        title = args.get("title", "Agent Message")
        payload = {"text": text, "title": title}
        if args.get("blocks"):
            payload["blocks"] = args["blocks"]
        if args.get("channel"):
            payload["channel"] = args["channel"]
        if args.get("user"):
            payload["user"] = args["user"]
        if "unfurl_links" in args:
            payload["unfurl_links"] = args["unfurl_links"]
        if "unfurl_media" in args:
            payload["unfurl_media"] = args["unfurl_media"]
        if args.get("thread_ts"):
            payload["thread_ts"] = args["thread_ts"]
        if args.get("reply_broadcast"):
            payload["reply_broadcast"] = args["reply_broadcast"]
        if args.get("session"):
            if args["session"] not in ("origin", "slack"):
                return 'Error: session must be "origin" or "slack".'
            payload["session"] = args["session"]
        # Always tell the gateway when the caller is a cron — even on a bare
        # send (no session/channel) — so it can apply the documented
        # "cron → Slack DM by default" routing and report where the message
        # actually landed.
        caller_session = _resolve_session_key()
        is_cron = caller_session.startswith("cron:")
        if is_cron:
            payload["caller_session"] = caller_session
        resp = _post("/api/send-message", payload)
        if not resp.get("ok"):
            return f"Failed: {resp}"
        # Prefer the gateway's explicit delivery channel when present
        # (delivered_to ∈ {"slack", "session", "notification"}); fall back to
        # the legacy slack/session booleans for older gateways.
        delivered_to = resp.get("delivered_to")
        ts = resp.get("ts", "")
        if delivered_to == "session" or (delivered_to is None and resp.get("session")):
            return "Message injected into target session."
        if delivered_to == "slack" or (delivered_to is None and resp.get("slack")):
            return (
                f"Message sent to Slack + notification. ts={ts}"
                if ts
                else "Message sent to Slack + notification."
            )
        # Reached the dashboard notification only. Warn loudly when Slack was
        # intended (explicit session=slack, or a cron — which now defaults to
        # Slack) so the caller can detect the miss and retry instead of
        # reading a success string for a notification-only send.
        if args.get("session") == "slack":
            return "⚠️ Slack unavailable — delivered as dashboard notification only (NOT in Slack)."
        if args.get("session"):
            return "Session injection unavailable — delivered as notification."
        if is_cron:
            return (
                "⚠️ Cron send reached the dashboard notification only — NOT posted to Slack "
                "(owner DM unavailable: no Slack client or owner_id). Verify Slack delivery."
            )
        return "Notification delivered."

    if name == "delete_message":
        channel = args["channel"]
        msg_ts = args["ts"]
        if not CHANNEL_ID_RE.match(channel):
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="delete_message",
                outcome="error",
            )
            return "Error: invalid channel ID format."
        if not _SLACK_TS_RE.match(msg_ts):
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="delete_message",
                outcome="error",
            )
            return "Error: invalid message timestamp format."
        resp = _post("/api/delete-message", {"channel": channel, "ts": msg_ts})
        if resp.get("error"):
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="delete_message",
                outcome="error",
            )
            return f"Failed: {resp['error']}"
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="delete_message",
            outcome="success",
        )
        return "Message deleted."

    if name == "read_slack_profile":
        user_id = args["user"]
        resp = _post("/api/slack-profile", {"user": user_id})
        if resp.get("error"):
            return f"Error: {resp['error']}"
        profile = resp.get("profile", {})
        # Defence-in-depth: redact profile values before returning to LLM.

        for key in list(profile):
            val = profile[key]
            if isinstance(val, str) and key != "id":
                val, _ = redact_exfiltration_urls(val)
                val, _ = redact_credentials(val)
                profile[key] = val
        return json.dumps(profile, indent=2)

    if name == "file_send":
        src = Path(args.get("path", ""))
        desc = redact(args.get("description", ""))
        try:
            raw = safe_read_file_bytes(str(src))
        except FileTooLargeError as e:
            sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="denied",
                error=f"file_too_large: {e}",
            )
            return f"Error: {e}"
        if raw is None:
            sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="denied",
                error=f"path_not_allowed: {src}",
            )
            return f"Error: file not found or access denied: {src}"
        clean_name = src.name
        if redact(clean_name) != clean_name:
            sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="denied",
                error=f"sensitive_filename: {redact(clean_name)}",
            )
            return "Error: filename contains sensitive content. Rename the file first."
        # For text files, check content for sensitive data; binary files skip this
        # and validate MIME against the shared BINARY_MIME_ALLOWLIST (deny-by-default).
        is_text = True
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            is_text = False
            guessed = mimetypes.guess_type(clean_name)[0] or ""
            if guessed not in BINARY_MIME_ALLOWLIST:
                sel().log_tool_invocation(
                    session_key="mcp_core",
                    source="mcp",
                    tool_name="file_send",
                    outcome="denied",
                    error=f"binary_mime_not_allowed: {guessed}",
                )
                return f"Error: binary file type not allowed: {guessed or 'unknown'}. Allowed: audio, video, image, PDF."
            sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="info",
                error="binary_file_skipping_content_scan",
            )
        if is_text and redact(text) != text:
            sel().log_tool_invocation(
                session_key="mcp_core",
                source="mcp",
                tool_name="file_send",
                outcome="denied",
                error="sensitive_content_detected",
            )
            return "Error: file content contains sensitive data; send aborted"
        dest = outbox_dir() / clean_name
        try:
            with dest.open("xb") as f:
                f.write(raw)
        except FileExistsError:
            dest = (
                outbox_dir()
                / f"{Path(clean_name).stem}_{uuid.uuid4().hex}{Path(clean_name).suffix}"
            )
            dest.write_bytes(raw)
        sel().log_tool_invocation(
            session_key="mcp_core",
            source="mcp",
            tool_name="file_send",
            outcome="completed",
            resources=f"src={src} dest={dest}",
        )
        # Notify dashboard (renders file card in chat UI)
        d = _post(
            "/api/outbox/notify",
            {
                "path": str(dest),
                "filename": dest.name,
                "description": desc,
                "size": dest.stat().st_size,
            },
        )
        if d.get("error"):
            return f"Error: {d['error']}"
        # Also upload to Slack if available
        thread_ts = _current_session_thread_ts()
        slack_resp = _post(
            "/api/slack/upload-file",
            {
                "file_path": str(dest),
                "filename": dest.name,
                "thread_ts": thread_ts,
                "channel": args.get("channel", ""),
            },
        )
        slack_warning = ""
        if slack_resp.get("error"):
            slack_warning = f" (Slack upload failed: {slack_resp['error']})"
        msg = f"File sent: {dest.name} ({desc})" if desc else f"File sent: {dest.name}"
        return msg + slack_warning

    if name == "artifact_save":
        args = validate_tool_args(args, ARTIFACT_SAVE_SCHEMA)
        save_body: dict[str, Any] = {
            "name": args["name"],
            "content": args["content"],
        }
        for k in ("slug", "kind", "source", "description", "tags"):
            if k in args and args[k] is not None:
                save_body[k] = args[k]
        # Pre-save dedup probe: when saving a chat-source widget, check for
        # an existing widget artifact with the same NFC-normalized name.
        # If one exists we still allow the save (the agent may have a real
        # reason to create a parallel artifact), but we attach a hint so
        # the agent can self-correct on the next turn — typically that
        # means deleting the just-created duplicate and using
        # ``artifact_update`` on the pre-existing slug instead. Without
        # this hint, the agent's only signal that a duplicate happened is
        # the user noticing in the library, which is exactly the failure
        # mode Mesh-1715 surfaced (Fight Club: agent created
        # ``rules-of-fight-club`` even though ``a07ece9a8c3309aa`` named
        # "The Rules of Fight Club" already existed).
        kind_for_dedup = args.get("kind", "widget")
        source_for_dedup = args.get("source", "chat")
        explicit_slug = args.get("slug")
        target_name = args.get("name", "")
        dedup_hint = ""
        if (
            kind_for_dedup == "widget"
            and source_for_dedup == "chat"
            and not explicit_slug
            and isinstance(target_name, str)
            and target_name
            and target_name.lower() != "widget"
        ):
            try:
                qs = urlencode(
                    {
                        "kind": "widget",
                        "source": "chat",
                        "q": target_name,
                    }
                )
                listing = _get(f"/api/artifacts?{qs}")
                if listing.get("error"):
                    raise ValueError(listing["error"])
                candidates = listing.get("artifacts") or []
                target_norm = unicodedata.normalize("NFC", target_name).lower()
                conflicts = [
                    a
                    for a in candidates
                    if isinstance(a, dict)
                    and isinstance(a.get("name"), str)
                    and isinstance(a.get("slug"), str)
                    and unicodedata.normalize("NFC", a["name"]).lower() == target_norm
                ]
                if conflicts:
                    # Sort newest first, mirror frontend dedup.
                    conflicts.sort(
                        key=lambda a: a.get("updated_at") or "",
                        reverse=True,
                    )
                    existing_slug = conflicts[0]["slug"]
                    if len(conflicts) > 1:
                        dedup_hint = (
                            "\n\n⚠️  Possible duplicate: a widget artifact named "
                            f'"{target_name}" already exists at '
                            f"slug={existing_slug!r} (and {len(conflicts) - 1} "
                            "other same-named match(es))."
                        )
                    else:
                        dedup_hint = (
                            "\n\n⚠️  Possible duplicate: a widget artifact named "
                            f'"{target_name}" already exists at '
                            f"slug={existing_slug!r}."
                        )
                    dedup_hint += (
                        " If you intended to capture a new version of that "
                        "artifact, delete the duplicate just created and "
                        "call `artifact_update` on the existing slug "
                        "instead. If both artifacts are genuinely needed, "
                        "rename one to disambiguate."
                    )
            except Exception:
                # Probe failure is non-fatal — proceed with the save and
                # skip the hint. Don't let a transient list failure block
                # legitimate save calls. We deliberately swallow without
                # logging because mcp_core.py runs as a stdio MCP server
                # — any stdout/stderr writes corrupt the JSON-RPC stream.
                pass
        d = _post("/api/artifacts", save_body)
        if d.get("error"):
            return f"Error: {d['error']}"
        slug = d.get("slug", "?")
        version = d.get("version", 1)
        return (
            f"Saved artifact: slug={slug} version={version}\n\n"
            f"{_artifact_reemit_hint(slug, d.get('name', args.get('name', '')), d.get('kind', args.get('kind', 'widget')))}"
            f"{dedup_hint}"
        )

    if name == "artifact_get":
        args = validate_tool_args(args, ARTIFACT_GET_SCHEMA)
        slug = args["slug"]
        version = args.get("version")
        path = f"/api/artifacts/{slug}"
        if version:
            path = f"/api/artifacts/{slug}/versions/{int(version)}"
        d = _get(path)
        if d.get("error"):
            return f"Error: {d['error']}"

        content = d.get("content") or ""
        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)
        meta_lines = [
            f"slug: {d.get('slug', '?')}",
            f"name: {d.get('name', '?')}",
            f"kind: {d.get('kind', '?')}",
            f"version: {d.get('version', '?')}",
            f"updated_at: {d.get('updated_at', '?')}",
        ]
        if d.get("description"):
            meta_lines.append(f"description: {d['description']}")
        if d.get("tags"):
            meta_lines.append(f"tags: {', '.join(d['tags'])}")
        out_body = "\n".join(meta_lines) + "\n\n--- content ---\n" + content
        # Append a re-emit hint for widgets so the agent has the exact tag
        # string it should use when surfacing the artifact in chat. Without
        # this the slug rule from the artifacts skill is easy to overlook
        # at emission time even though it's right there at the top of this
        # response — verified by Mesh-1715 session logs where the LLM had
        # the slug in front of it twice and still emitted without it.
        kind = d.get("kind", "widget")
        if kind == "widget":
            out_body += "\n\n" + _artifact_reemit_hint(d.get("slug", "?"), d.get("name", ""), kind)
        return out_body

    if name == "artifact_update":
        args = validate_tool_args(args, ARTIFACT_UPDATE_SCHEMA)
        slug = args["slug"]
        update_body = {k: v for k, v in args.items() if k != "slug" and v is not None}
        if not update_body:
            return "Error: nothing to update (provide content/name/description/tags)"
        # Note: 'actor' is no longer set in the body — the API handler infers
        # it from the X-Internal-Secret header presence (MCP=agent,
        # dashboard=user). This is more secure than trusting a body field
        # and saves the agent from having to remember to set it.
        # _post helper sends POST; we need PATCH. Use urllib.request directly
        # (already imported at module top).
        data = json.dumps(update_body).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": _internal_secret(),
        }
        sk = _resolve_session_key()
        if sk:
            headers["X-Session-Key"] = sk
        req = urllib.request.Request(
            f"{_API}/api/artifacts/{slug}", data=data, headers=headers, method="PATCH"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as http_resp:
                d = json.loads(http_resp.read())
        except urllib.error.HTTPError as exc:
            try:
                err_body = json.loads(exc.read()).get("error", str(exc))
            except Exception:
                err_body = str(exc)
            return f"Error: {err_body}"
        except Exception as exc:
            return f"Error: {exc}"
        out = [f"Updated artifact: slug={d.get('slug', slug)} version={d.get('version', '?')}"]
        # Surface source_path so the agent can emit unified-diff headers
        # when summarising the change in chat (powers the dashboard's
        # Open file affordance on diff blocks). See artifacts skill for
        # the exact format.
        sp = d.get("source_path") or ""
        if sp:
            out.append(f"source_path: {sp}")
        # Re-emit hint for widget-kind updates — same rationale as in
        # artifact_get above. Iterate flow especially needs this because
        # the agent's next step is almost always re-emitting the updated
        # widget in chat, and forgetting the slug at that point is the
        # single largest source of duplicate-artifact creation.
        if d.get("kind", "widget") == "widget":
            out.append("")
            out.append(_artifact_reemit_hint(d.get("slug", slug), d.get("name", ""), "widget"))
        return "\n".join(out)

    if name == "artifact_revert":
        args = validate_tool_args(args, ARTIFACT_REVERT_SCHEMA)
        slug = args["slug"]
        target_version = int(args["target_version"])
        # Step 1: read the target version's content. Using the API endpoint
        # so the actor / session_id inference from the PATCH stays consistent
        # — we don't bypass the auth-aware handler.
        target = _get(f"/api/artifacts/{slug}/versions/{target_version}")
        if target.get("error"):
            return f"Error: cannot fetch version {target_version}: {target['error']}"
        target_content = target.get("content") or ""
        # Step 2: PATCH the artifact with the target's content + reverted
        # event metadata. Snapshot is forced True for reverted updates by
        # the handler — this becomes a new version pinned to the timeline.
        body = {
            "content": target_content,
            "event_type": "reverted",
            "from_version": target_version,
        }
        data = json.dumps(body).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Internal-Secret": _internal_secret(),
        }
        sk = _resolve_session_key()
        if sk:
            headers["X-Session-Key"] = sk
        req = urllib.request.Request(
            f"{_API}/api/artifacts/{slug}", data=data, headers=headers, method="PATCH"
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as http_resp:
                d = json.loads(http_resp.read())
        except urllib.error.HTTPError as exc:
            try:
                err_body = json.loads(exc.read()).get("error", str(exc))
            except Exception:
                err_body = str(exc)
            return f"Error: {err_body}"
        except Exception as exc:
            return f"Error: {exc}"
        # Surface source_path on the response so the calling agent can build
        # a proper unified-diff header (--- <path>\n+++ <path>) when
        # summarising the revert in chat. The dashboard's diff renderer
        # reads those headers to show the "Open file" button — without
        # them, the user sees a diff with no way to drop into the file
        # in the side panel (Mesh-1654 round 7 follow-up).
        live_version = d.get("version", "?")
        source_path = d.get("source_path") or ""
        out_lines = [
            f"Reverted {slug} to v{target_version}'s content. "
            f"Live state is now v{live_version} (snapshot of v{target_version}).",
        ]
        if source_path:
            out_lines.append(f"source_path: {source_path}")
            out_lines.append(
                "When summarising in chat, emit a ```diff fenced block "
                f"with `--- {source_path}` and `+++ {source_path}` "
                "headers so the dashboard's Open file button is operable."
            )
        return "\n".join(out_lines)

    if name == "artifact_list":
        args = validate_tool_args(args, ARTIFACT_LIST_SCHEMA)
        params: dict[str, str] = {}
        for k in ("tag", "kind", "q"):
            v = args.get(k)
            if v:
                params[k] = v
        path = "/api/artifacts"
        if params:
            path = f"{path}?{urlencode(params)}"
        d = _get(path)
        if d.get("error"):
            return f"Error: {d['error']}"
        items = d.get("artifacts", [])
        if not items:
            return "No artifacts saved."
        lines = []
        for a in items:
            tags = f"  [{', '.join(a.get('tags', []))}]" if a.get("tags") else ""
            lines.append(
                f"{a.get('slug', '?')}  v{a.get('version', '?')}  "
                f"{a.get('kind', '?')}{tags}  {a.get('name', '?')}"
            )
        return "\n".join(lines)

    if name == "artifact_versions":
        args = validate_tool_args(args, ARTIFACT_VERSIONS_SCHEMA)
        slug = args["slug"]
        d = _get(f"/api/artifacts/{slug}/versions")
        if d.get("error"):
            return f"Error: {d['error']}"
        versions = d.get("versions", [])
        if not versions:
            return f"No versions found for {slug}."
        return f"{slug}: versions {', '.join(f'v{v}' for v in versions)}"

    if name == "artifact_delete":
        args = validate_tool_args(args, ARTIFACT_DELETE_SCHEMA)
        slug = args["slug"]
        d = _delete(f"/api/artifacts/{slug}")
        if d.get("error"):
            return f"Error: {d['error']}"
        return f"Deleted artifact: {slug}"

    if name == "autonudge_stop":
        # Defense-in-depth: _call_tool() already validates via _validate_args;
        # re-validate here so schema enforcement is visible at the extraction
        # point (matches spawn_run pattern above).
        args = validate_tool_args(args, AUTONUDGE_STOP_SCHEMA)

        # Resolve the current session's slot key and stop any loop bound to it.
        sk = _resolve_session_key()
        # Session key is formatted "dashboard:chat-N-TS" for chat slots
        # or "cron:<id>", "hook:<id>", etc. AutoNudge only binds to chat slots.
        if not sk.startswith("dashboard:"):
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="noop"
            )
            return (
                "No auto-nudge loop to stop: this tool only works from within "
                f"a dashboard chat session (current session_key={sk!r})."
            )
        slot_key = sk.split(":", 1)[1]
        reason = args.get("reason", "").strip()
        lookup = _get(f"/api/autonudge/slot/{slot_key}")
        if lookup.get("error"):
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="error"
            )
            return f"Failed to look up loop: {lookup['error']}"
        loop = lookup.get("loop")
        if not loop:
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="noop"
            )
            return "No active auto-nudge loop on this session — nothing to stop."
        loop_id = loop.get("id", "")
        resp = _delete(f"/api/autonudge/{loop_id}")
        if resp.get("error"):
            sel().log_tool_invocation(
                session_key=sk, source="mcp", tool_name="autonudge_stop", outcome="error"
            )
            return f"Failed to stop loop {loop_id}: {resp['error']}"
        sel().log_tool_invocation(
            session_key=sk,
            source="mcp",
            tool_name="autonudge_stop",
            outcome="success",
            metadata={"slot_key": slot_key, "loop_id": loop_id, "reason": reason},
        )
        return (
            f"Auto-nudge loop {loop_id} stopped on session {slot_key}"
            + (f" (reason: {reason})" if reason else "")
            + ". No further nudges will fire."
        )

    if name == "search_chat_history":
        args = validate_tool_args(args, SEARCH_CHAT_HISTORY_SCHEMA)
        query = args["query"]
        limit = args.get("limit", 10)
        all_workspaces = args.get("all_workspaces", False)
        after_epoch = _parse_iso_date_epoch(args["after"]) if args.get("after") else None
        before_epoch = _parse_iso_date_epoch(args["before"]) if args.get("before") else None

        cl = ConversationLog()
        session_key = _resolve_session_key()

        # Default scoping: confine results to the caller's workspace. Sessions
        # with no workspace recorded (legacy, or an unresolved caller) are
        # bucketed as "default" on BOTH sides of the comparison, so an
        # unresolvable caller scopes to the default bucket rather than
        # fail-open-ing to every workspace's history. all_workspaces opts out.
        current_ws: str | None = None
        if not all_workspaces:
            current_ws = (cl.get_metadata(session_key).get("workspace") if session_key else None) or "default"

        # Over-fetch from the ranked backend so post-filtering (incognito,
        # workspace, date) can drop rows without starving the limit. (Named
        # `ranked`, not `raw`: `raw` is bound as bytes|None elsewhere in this
        # function and mypy unifies a function's locals.)
        ranked: list[dict] = cl.search_sessions(query, limit=limit * 3)

        results: list[dict] = []
        for meta in ranked:
            key = meta.get("key", "")
            if not key:
                continue
            # Authoritative metadata (list meta omits workspace and may carry a
            # default memory_mode) — one bounded read per candidate.
            full_meta = cl.get_metadata(key)
            if _history_is_incognito(full_meta) or _history_is_incognito(meta):
                continue  # EB-5: incognito/temporary never surface
            if current_ws is not None and (full_meta.get("workspace") or "default") != current_ws:
                continue  # EB-cc3: workspace scoping (fail-closed via "default" bucket)
            modified = meta.get("modified", 0) or 0
            if after_epoch is not None and modified < after_epoch:
                continue
            if before_epoch is not None and modified >= before_epoch:
                continue

            snippet = _extract_history_snippet(cl.read_messages(key), query)
            results.append(
                {
                    "session_key": key,
                    "title": meta.get("title") or key,
                    "date": meta.get("created") or "",
                    "snippet": snippet,
                }
            )
            if len(results) >= limit:
                break

        if not results:
            sel().log_tool_invocation(
                session_key=session_key, source="mcp",
                tool_name="search_chat_history", outcome="no_results",
                metadata={"query_len": len(query)},
            )
            return "No matching conversations found. Try different keywords."

        lines = [
            "\U0001f50e Chat history matches "
            "(snippets only — use get_chat_session to read a full thread):"
        ]
        for r in results:
            lines.append("\n---")
            lines.append(f"**{r['title']}**  ·  `{r['session_key']}`")
            if r["date"]:
                lines.append(f"_{r['date']}_")
            if r["snippet"]:
                lines.append(f"\n{r['snippet']}")

        output = "\n".join(lines)
        # EB-6: redact secrets/exfil URLs from snippets before returning.
        output = _redact_history_output(output)
        sel().log_tool_invocation(
            session_key=session_key, source="mcp",
            tool_name="search_chat_history", outcome="success",
            metadata={"query_len": len(query), "result_count": len(results)},
        )
        return output

    if name == "get_chat_session":
        args = validate_tool_args(args, GET_CHAT_SESSION_SCHEMA)
        key = args["session_key"]
        max_messages = args.get("max_messages", 50)
        all_workspaces = args.get("all_workspaces", False)

        # Defense-in-depth on a path-bearing identifier: ConversationLog._safe_key
        # already neutralizes separators, but reject traversal markers outright
        # here so a crafted key can never probe outside the sessions dir. (A
        # strict allowlist regex is avoided: real keys legitimately contain ':'
        # and '.', e.g. Slack thread_ts and "dashboard:chat-…".)
        if "/" in key or "\\" in key or ".." in key:
            sel().log_tool_invocation(
                session_key=_resolve_session_key(), source="mcp",
                tool_name="get_chat_session", outcome="rejected_bad_key",
            )
            return "Invalid session_key."

        cl = ConversationLog()
        if not cl.has_log(key):
            sel().log_tool_invocation(
                session_key=_resolve_session_key(), source="mcp",
                tool_name="get_chat_session", outcome="not_found",
            )
            return _redact_history_output(f"No conversation found for session_key `{key}`.")

        meta = cl.get_metadata(key)
        if _history_is_incognito(meta):
            # EB-7b: no bypass of incognito exclusion via direct fetch.
            sel().log_tool_invocation(
                session_key=_resolve_session_key(), source="mcp",
                tool_name="get_chat_session", outcome="refused_incognito",
            )
            return "That conversation is private (incognito/temporary) and cannot be read."

        # Deny-by-default workspace isolation: mirror search_chat_history's
        # fail-closed scoping so a caller can't bypass it by fetching a session
        # from another workspace directly. Unset workspaces bucket as "default".
        if not all_workspaces:
            caller_key = _resolve_session_key()
            caller_ws = (cl.get_metadata(caller_key).get("workspace") if caller_key else None) or "default"
            target_ws = meta.get("workspace") or "default"
            if caller_ws != target_ws:
                sel().log_tool_invocation(
                    session_key=caller_key, source="mcp",
                    tool_name="get_chat_session", outcome="denied_cross_workspace",
                )
                return "Access denied: that conversation belongs to a different workspace."

        messages = cl.recent(key, max_messages=max_messages, roles={"user", "assistant"})
        if not messages:
            sel().log_tool_invocation(
                session_key=_resolve_session_key(), source="mcp",
                tool_name="get_chat_session", outcome="empty",
            )
            return _redact_history_output(f"Conversation `{key}` has no readable messages.")

        title = meta.get("title") or key
        lines = [f"\U0001f4dc Conversation: **{title}**  ·  `{key}`", ""]
        for m in messages:
            role = str(m.get("role", "?")).title()
            lines.append(f"**{role}:** {m.get('content', '')}")
            lines.append("")

        output = _redact_history_output("\n".join(lines))
        sel().log_tool_invocation(
            session_key=_resolve_session_key(), source="mcp",
            tool_name="get_chat_session", outcome="success",
            metadata={"message_count": len(messages)},
        )
        return output

    if name == "local_knowledge_search":
        args = validate_tool_args(args, LOCAL_KNOWLEDGE_SEARCH_SCHEMA)
        query = args["query"]
        limit = args.get("limit", 3)

        db_path = Path(config_dir()) / "workspace" / "knowledge" / "knowledge.db"
        if not db_path.exists():
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="local_knowledge_search",
                outcome="not_configured",
            )
            return "Knowledge Library is not configured. Ingest documents via the dashboard first."

        # Reuse a cached store + embedder across calls; rebuilt only when the
        # knowledge DB (or its -wal) or config.json changes (see
        # _get_knowledge_search). Avoids the per-call schema/migrate/graph-load
        # and the Ollama availability probe.
        cfg_path = Path(config_dir()) / "config.json"
        store, embedder = _get_knowledge_search(db_path, cfg_path)
        embed_fn = embedder.embed if embedder and embedder.is_available() else None
        retriever = HybridRetriever(store, embedder=embed_fn)

        results = retriever.search(query, limit=limit)

        # Filter by minimum confidence score
        min_score = 0.012
        results = [r for r in results if r.get("score", 0) >= min_score]

        if not results:
            sel().log_tool_invocation(
                session_key=_resolve_session_key(),
                source="mcp",
                tool_name="local_knowledge_search",
                outcome="no_results",
                metadata={"query": query},
            )
            return "No relevant knowledge found."

        # Resolve source names
        source_names: dict[str, str] = {}
        for r in results:
            sid = r.get("source")
            if sid and sid not in source_names:
                row = store.db.execute("SELECT name FROM sources WHERE id = ?", (sid,)).fetchone()
                source_names[sid] = row["name"] if row else "(unknown)"

        # Format output
        lines = [
            "\U0001f4da Knowledge Library "
            "(supplementary reference \u2014 extract only what's relevant to the question):"
        ]
        for r in results:
            title = r.get("title") or "(untitled)"
            source = source_names.get(r.get("source", ""), "")
            content = r.get("content", "")
            lines.append("\n---")
            lines.append(f"## {title}")
            if source:
                lines.append(f"**Source:** {source}")
            lines.append(f"\n{content}")

        output = "\n".join(lines)
        output, _ = redact_exfiltration_urls(output)
        output, _ = redact_credentials(output)
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="local_knowledge_search",
            outcome="success",
            metadata={"query": query, "result_count": len(results)},
        )
        return output

    if name == "browse_outline":
        snapshot = args.get("snapshot", "")
        max_lines = args.get("max_lines", 100)
        result = _compress_snapshot_to_outline(snapshot, max_lines)
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="browse_outline",
            outcome="success",
        )
        return result

    if name == "browse_search":
        snapshot = args.get("snapshot", "")
        query = args.get("query", "")
        max_results = args.get("max_results", 50)
        result = _search_snapshot(snapshot, query, max_results)
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)
        sel().log_tool_invocation(
            session_key=_resolve_session_key(),
            source="mcp",
            tool_name="browse_search",
            outcome="success",
        )
        return result

    return f"Unknown tool: {name}"


def run_mcp_core_server() -> None:
    """Run MCP stdio server for core agent tools."""
    run_mcp_stdio_loop("kiroclaw-core", "1.0.0", _list_tools, _call_tool)
