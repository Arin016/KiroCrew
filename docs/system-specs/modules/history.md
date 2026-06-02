# Conversation History Module

Last Updated: 2026-04-21 (session archive, configurable autocompact)

## Overview

Persistent conversation history with provenance tracking and LLM-driven consolidation. Conversations survive session expiry and gateway restarts.

## ConversationLog (`history.py`)

Per-thread JSONL files at `~/.kiroclaw/sessions/{safe_key}.jsonl`. First line is metadata, subsequent lines are messages with `role`, `content`, `ts`, `tools`, `source_thread`, `source_user`.

- Append-only for LLM cache efficiency
- Rotation at 2MB (keeps metadata + last 200 messages, atomic write)
- `recent(key)` — last 20 messages for context injection
- `recent_with_provenance(key)` — entries with source citations
- `list_sessions()` — lists all sessions with title (first user message or LLM-generated). Sort key uses ISO `created` string consistently (defaults to ISO from `st_mtime` if no metadata `created` field, ensuring string-only comparisons).
- `search_sessions(query, limit=50)` — case-insensitive substring content search over session JSONL files. Scans newest first, stops early on first hit per file, caps results. Exposed via `GET /api/sessions/search?q=<q>&limit=<n>` (min 2 chars); used by the dashboard history filter to find sessions by content (CR ids, error messages, file paths) rather than title alone.
- `delete_session(key)` — permanently removes a session JSONL file
## Session Archive (`history.py`)

Rotated and compacted session lines are archived instead of being permanently deleted:

- **Archive location**: `~/.kiroclaw/sessions/archive/{key}__{YYYYMMDD-HHMMSS}.jsonl`
- **Triggers**: `_rotate()` (>2MB) and `rewrite()` (compact) both call `_archive_lines()`
- **Atomic writes**: exclusive-create (`open mode 'x'`) avoids TOCTOU clobber
- **Retention**: 7-day cleanup via `_cleanup_old_archives()`, rate-limited to once per hour
- **API**: `GET /api/session/archive` (list), `GET /api/session/archive/{name}` (read with path traversal protection)

- `set_title(key, title)` — persists a title into the session's metadata line (first line of JSONL)

## HistoryConsolidator (`history.py`)

Background task that fires when unconsolidated count ≥ 10 messages. Uses the
persistent background ACP session (kiro-cli long-running session, same as
cron/heartbeat/lesson extraction) to extract:
- `history_entry` → appended to today's daily history file
- `preferences_update` → overwrites `preferences.md` if changed
- `projects_update` → overwrites `projects.md` if changed

Non-blocking via `asyncio.create_task`. Requires `SessionManager` to be passed
at construction time; consolidation is silently skipped if no session manager
is available.

## Stop Events

Stop events are persisted to JSONL as `system` messages. The structured
stop-event data lives in the `cls` field as a JSON-encoded object (which
`parse_cls_meta` lifts into `meta` for frontend consumers via
`StopEventCard`). The `content` field mirrors the same JSON for
backward-compatible consumers that only read `content`.

```json
{
  "role": "system",
  "content": "{\"kind\":\"stop_event\",\"id\":\"stop-<uuid>\",\"state\":\"stopped\",\"outcome\":\"soft\",\"ts_start\":\"2026-04-27T00:07:40Z\",\"ts_end\":\"2026-04-27T00:07:40Z\"}",
  "cls": "{\"kind\":\"stop_event\",\"id\":\"stop-<uuid>\",\"state\":\"stopped\",\"outcome\":\"soft\",\"ts_start\":\"2026-04-27T00:07:40Z\",\"ts_end\":\"2026-04-27T00:07:40Z\"}",
  "ts": "2026-04-27T00:07:40Z",
  "source_thread": "dashboard",
  "source_user": "dashboard"
}
```

Possible `state` values:

| State | Meaning |
|-------|---------|
| `stopping` | Cooperative cancel in flight; waiting for agent ack |
| `stopped` | Agent acknowledged cancel; session preserved |
| `stop_failed_reset` | Agent did not ack within budget; session was hard-killed and reset |

The stop event is inserted at soft-start time with `state: "stopping"` and
updated in place (same `id`) when the outcome resolves. The updated message
is re-broadcast via `_on_message` so the frontend `StopEventCard` transitions
from `stopping` → `stopped`/`stop_failed_reset`.

After a cancelled turn, `context.build_cancelled_turn_preamble` reads the
cancelled user prompt and partial assistant output from this log and
prepends them to the next prompt as a bracketed preamble, because kiro-cli
discards cancelled turns from its own ACP conversation log. The flag
`_Session.prev_turn_cancelled` (set by `SessionManager.stop_turn` on
soft-cancel success) gates the one-shot re-injection.

## Session Lifecycle

1. New session → full context injected (memory + skills + lessons + last 20 messages)
2. Messages saved to JSONL with provenance after each response
3. Context ≥ configured threshold (`session.autocompact_pct`, default 90%) → compaction via kiro-cli `/compact` (fire-and-forget)
4. Session expires (30min idle) → provider killed
5. User returns → new session with history re-injected
6. After 10+ messages → background consolidation → structured memory updated

## Source Provenance

Messages include `source_thread` and `source_user` fields:
- **Slack**: `source_thread` = Slack thread_ts, `source_user` = Slack user ID
- **Dashboard**: `source_thread` = "dashboard", `source_user` = "dashboard"
- Session keys prefixed `dashboard:` for dashboard chat slots

Dashboard history list shows source icons: 🖥 (dashboard) / 💬 (Slack).
