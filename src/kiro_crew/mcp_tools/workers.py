"""Worker-session tools: how a crew member dispatches and patrols real work.

``schemas()`` returns the ADVERTISEMENT half of each tool -- its name, the
model-facing description, and the JSON Schema a call is validated against.
``HANDLERS`` maps each of those names to the function that runs it. Both halves
of a tool live here so its contract and its behavior are read together, and
``test_mcp_tool_registry`` fails if one arrives without the other.

These are the member-facing spellings of the session-control surface that
``@kirocrew-dashboard`` exposes to conductor agents. They live on
``kirocrew-core`` -- which every agent template already references -- so a crew
member's pinned DM session can dispatch work with ZERO spec or config changes.
Authorization is entirely server-side (``dashboard/session_control.py``): a
member DM session is authorized automatically and bounded to the workers it
created itself; any other caller needs the ``agent.session_control`` opt-in.
The tools therefore mount everywhere but only ANSWER for callers the gateway
authorizes -- the same mounted-but-gated posture the conductor's withheld
verbs take.

Handlers reach this server's shared plumbing as attributes of ``mcp_core`` --
``mcp_core._post``, the identity resolvers. That is deliberate rather than
untidy: an attribute lookup resolves at CALL time, so a test that rebinds one
on the module still intercepts the handler.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from kiro_crew import mcp_core
from kiro_crew.validation import (
    WORKER_CREATE_SCHEMA,
    WORKER_READ_SCHEMA,
    WORKER_SEND_SCHEMA,
    WORKER_STOP_SCHEMA,
    validate_tool_args,
)


def schemas() -> list[dict[str, Any]]:
    """Descriptors for the worker-session tools."""
    return [
        {
            "name": "worker_create",
            "description": (
                "Open a WORKER session for a piece of real work you are dispatching "
                "(fixing an issue, running an investigation) instead of doing it in "
                "this conversation. The worker appears in the user's sidebar, runs "
                "with full tool access, and is YOURS to drive: seed it with "
                "worker_send, watch it with worker_read, and stop a runaway with "
                "worker_stop. Keep this conversation for decisions and reports; put "
                "the heavy lifting in workers. A crew member's DM session may call "
                "this without any configuration; it can only control workers it "
                "created itself."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short human-readable name for the work item.",
                    },
                    "agent": {
                        "type": "string",
                        "description": (
                            "Agent for the worker (defaults to the workspace default). "
                            "Use a name from the crew registry."
                        ),
                    },
                },
            },
        },
        {
            "name": "worker_send",
            "description": (
                "Send instructions to a worker session you created -- the seed brief "
                "after worker_create, or a mid-flight course correction. The text "
                "runs as that worker's next user-role turn. Write briefs that stand "
                "alone: the worker has none of this conversation's context."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Worker session key from worker_create.",
                    },
                    "message": {
                        "type": "string",
                        "description": "The instruction to run as the worker's next turn.",
                    },
                },
                "required": ["target", "message"],
            },
        },
        {
            "name": "worker_read",
            "description": (
                "Read a worker session's recent transcript to check on its progress "
                "-- your patrol primitive. Returns whether it is running, queued "
                "message depth, and the latest messages. Pass the returned "
                "next_since cursor on your next read to resume from where you left "
                "off. A worker that stopped answering or died mid-task is YOURS to "
                "notice: restart it with worker_send or escalate to the user."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Worker session key to read.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default 20).",
                    },
                    "since": {
                        "type": "integer",
                        "description": "Cursor from a previous read's next_since.",
                    },
                },
                "required": ["target"],
            },
        },
        {
            "name": "worker_stop",
            "description": (
                "Stop a worker session's in-flight turn -- for a worker that went "
                "off the rails or whose work is no longer needed. The stop cancels "
                "cooperatively and DISCARDS the turn's unfinished work, so prefer a "
                "worker_send course correction when the work should continue."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "Worker session key to stop.",
                    },
                },
                "required": ["target"],
            },
        },
    ]


def _member_caller_key() -> str:
    """The calling session's key, or ``""`` unless it is a member DM thread.

    The ``worker_*`` tools are the MEMBER-facing spelling of session control.
    They mount on ``kirocrew-core`` (which every agent references), so without
    this check an ordinary agent would gain session control the moment the
    operator enables ``agent.session_control`` — bypassing the second half of
    that grant, the deliberate per-agent ``kirocrew-dashboard`` server
    assignment. Refusing non-member callers here restores the two-key model:
    ordinary agents reach session control only through the assigned
    ``session_*`` tools; members only through ``worker_*``, bounded
    server-side to the workers they created.
    """
    caller_key = mcp_core._resolve_session_key_strict()
    if not caller_key:
        return ""
    # circular import: members' module graph is heavy, resolve at call time.
    from kiro_crew.members import DM_SLOT_KEY_PREFIX

    slot_part = caller_key.split("_", 1)[1] if caller_key.startswith("dashboard_") else caller_key
    if not slot_part.startswith(DM_SLOT_KEY_PREFIX):
        return ""
    return caller_key


_NOT_A_MEMBER = (
    "Error: the worker_* tools answer only for a crew member's DM session. "
    "This session is not one; use the session_* tools (kirocrew-dashboard "
    "server, agent.session_control opt-in) for ordinary session control."
)


def worker_create(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WORKER_CREATE_SCHEMA)
    caller_key = _member_caller_key()
    if not caller_key:
        return _NOT_A_MEMBER
    payload: dict[str, Any] = {}
    if args.get("title"):
        payload["title"] = args["title"]
    if args.get("agent"):
        payload["agent"] = args["agent"]
    resp = mcp_core._post("/api/session-control/create", payload, session_key=caller_key)
    if resp.get("error"):
        return f"Error: could not create a worker: {resp['error']}"
    return (
        f"Opened worker `{resp.get('target')}` ({resp.get('title')}). It is empty "
        "and waiting: seed it with worker_send, then patrol it with worker_read."
    )


def worker_send(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WORKER_SEND_SCHEMA)
    caller_key = _member_caller_key()
    if not caller_key:
        return _NOT_A_MEMBER
    resp = mcp_core._post(
        "/api/session-control/send",
        {"target": args["target"], "message": args["message"]},
        session_key=caller_key,
    )
    if resp.get("error"):
        return f"Error: could not send to that worker: {resp['error']}"
    target = resp.get("target", args["target"])
    if resp.get("started"):
        return f"Delivered to `{target}` — it started a turn. Watch it with worker_read."
    return (
        f"Queued for `{target}` — it is mid-turn, so the message runs when the "
        "current turn ends. Poll with worker_read."
    )


def worker_read(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WORKER_READ_SCHEMA)
    caller_key = _member_caller_key()
    if not caller_key:
        return _NOT_A_MEMBER
    query = f"target={quote(str(args['target']))}&limit={args.get('limit', 20)}"
    if args.get("since") is not None:
        query += f"&since={int(args['since'])}"
    resp = mcp_core._get(f"/api/session-control/read?{query}", caller_key)
    if resp.get("error"):
        return f"Error: could not read that worker: {resp['error']}"
    rows = resp.get("messages") or []
    state_line = "still working" if resp.get("running") else "idle"
    queued = resp.get("queue_depth", 0)
    if queued:
        state_line += f", {queued} message(s) queued"
    head = (
        f"Worker `{resp.get('target', '')}` — {resp.get('title', '')} "
        f"({state_line}; total={resp.get('total', 0)})"
    )
    lines = [head]
    for row in rows:
        role = row.get("role", "?")
        content = str(row.get("content", ""))
        lines.append(f"[{role}] {content}")
    if not rows:
        lines.append("No messages in that window yet.")
    if "next_since" in resp:
        lines.append(f"Pass since={resp['next_since']} on your next read to resume from here.")
    return "\n".join(lines)


def worker_stop(name: str, args: dict[str, Any]) -> str:
    args = validate_tool_args(args, WORKER_STOP_SCHEMA)
    caller_key = _member_caller_key()
    if not caller_key:
        return _NOT_A_MEMBER
    resp = mcp_core._post(
        "/api/session-control/stop", {"target": args["target"]}, session_key=caller_key
    )
    if resp.get("error"):
        return f"Error: could not stop that worker: {resp['error']}"
    target = resp.get("target", args["target"])
    info = resp.get("info")
    if info:
        if resp.get("already_stopping"):
            return f"`{target}`: {info} — the earlier stop still stands."
        return f"`{target}`: {info} — nothing to stop."
    return f"Stop sent to `{target}`."


HANDLERS: dict[str, Callable[[str, dict[str, Any]], str]] = {
    "worker_create": worker_create,
    "worker_send": worker_send,
    "worker_read": worker_read,
    "worker_stop": worker_stop,
}
