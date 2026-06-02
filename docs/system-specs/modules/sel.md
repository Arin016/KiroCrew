# Security Event Log (SEL) Module

Last Updated: 2026-03-10

## Overview

Immutable, tamper-evident audit trail for all tool invocations, MCP calls, and dashboard API mutations. Implements transactional event logging per Amazon Security Event Logging Standard.

Storage: `~/.kiroclaw/security_events.jsonl` (append-only JSONL with HMAC-SHA256 chain).

## Event Schema

Each entry records:

| Field | Description |
|-------|-------------|
| `event_id` | Unique 16-char hex identifier |
| `timestamp` | ISO 8601 UTC |
| `event_type` | `tool_invocation`, `api_access` |
| `caller_identity` | Session key (e.g. `dashboard:abc`, `cron:xyz`, `subagent:123`) |
| `agent` | Agent name (`kiroclaw`, custom agent name) |
| `source` | Interface: `slack`, `dashboard`, `cli`, `cron`, `subagent`, `taskrunner`, `mcp`, `background` |
| `operation` | Tool name or `METHOD /api/path` |
| `tool_kind` | Tool category (`execute_bash`, `fs_write`, `mcp_core`, `mcp_cron`, etc.) |
| `outcome` | `invoked`, `auto_approved`, `approved`, `rejected`, `denied`, `completed`, `failed` |
| `resources` | Affected resources summary (truncated to 500 chars) |
| `downstream_service` | MCP server name if applicable (`kiroclaw-core`, `kiroclaw-cron`, `builder-mcp`) |
| `request_id` | ACP permission request ID |
| `error` | Error message if failed/denied |
| `prev_hash` | HMAC of previous entry (chain link) |
| `entry_hash` | HMAC-SHA256 of this entry |
| `metadata` | Additional context (approval reason, step index, etc.) |

## Integrity

- HMAC-SHA256 chain: each entry signs over the previous entry's hash
- HMAC key: `~/.kiroclaw/sel_hmac.key` (32 random bytes, `chmod 600`)
- Verification: `verify_integrity()` walks the chain and reports tampered entries
- Append-only: no in-place edits; pruning rewrites with chain rebuild

## Retention

Default 365 days. Pruned daily by heartbeat service (`_PRUNE_TICKS`).

## Integration Points

| Surface | What's Logged | Module |
|---------|---------------|--------|
| Slack handler | `tool_call` (invoked/denied), `permission_request` (all outcomes) | `slack/handler.py` |
| Dashboard chat | `tool_call` (invoked), `permission_request` (all outcomes) | `dashboard/chat.py` |
| TaskRunner | Permission requests during decomposition and step execution | `taskrunner.py` |
| Subagent | Permission requests during subagent execution | `subagent.py` |
| Background tasks | Permission requests via `_resolve_permission()` | `llm_helpers.py` |
| MCP core tools | `spawn_run`, `learn_add`, `task_run` calls and outcomes | `mcp_core.py` |
| MCP cron tools | `cron_add`, `cron_remove`, etc. calls and outcomes | `mcp_cron.py` |
| Dashboard API | All POST/PUT/DELETE operations via middleware | `dashboard/server.py` |

## APIs

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/sel/events?limit=N` | Recent security events (max 1000) |
| GET | `/api/sel/verify` | HMAC chain integrity check |

## CLI

```
kiroclaw security events [-n 20]   # Show recent events
kiroclaw security verify            # Verify HMAC chain integrity
```

## Thread Safety

Singleton pattern with `threading.Lock` on all writes. Safe for concurrent access from asyncio event loop + MCP server stdio processes.
