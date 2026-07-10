# Session Manager Module

Last Updated: 2026-07-10 (DM channel session-key model + dm_scope + generation reset + mid-turn steer/queue; Slack thread linking, bidirectional dashboard-Slack sync, slash commands)

## Overview

Maps thread keys to LLMProvider instances (`session.py`). Each thread gets
its own kiro-cli session with idle expiry, context compaction, circuit
breaker, per-session semaphore, and persistent background session.

Chat sessions cold-start on first message via `get_or_create()`. There is
no warm pool — this avoids race conditions where pre-spawned sessions have
stale MCP config.

## Background Session

`BACKGROUND_KEY = "_bg"` is a persistent shared session for lightweight
background work. It is:

- **Created on startup** by `start_pool()` alongside the warm pool
- **Never expired** by idle cleanup (`_expire_idle` skips it)
- **Serialized** by the per-session semaphore (one background task at a time)
- **Shared by**: heartbeat tasks, lesson extraction (NOT cron — see below)

This eliminates the cost of spawning/tearing down a kiro-cli process for
every cron job or heartbeat tick. Background tasks acquire the semaphore,
do their work, and release — the process stays warm.

### Context Overflow Protection

`recycle_background()` is called after every background task completes.
It checks context usage and **recycles** (kill + fresh spawn) the session
if needed — no compaction, since background tasks are stateless:

- At ≥ 70% context → recycle (more aggressive than chat's 90% compaction)
- After 20 prompts with no metadata → recycle (blind fallback)
- Below thresholds → no-op (session stays warm)

Callers: heartbeat callback, taskrunner lesson extraction.

## Key Behaviors

- **Context compaction**: at ≥ configured threshold (`session.autocompact_pct`, default 90%, valid 5–90), fires `/compact` to kiro-cli. Context re-injected via
  `build_session_context()` on next message. Blind fallback after 40
  prompts if metadata never reports %.
- **Circuit breaker**: force-resets session after 5 consecutive failures.
- **Dead provider detection**: `get_or_create()` checks `provider.is_alive()`
  on the fast path. If the backing process died (crash, SIGKILL, orphan
  cleanup), the stale session entry is removed and a fresh cold-start
  occurs with `is_new=True` — ensuring full context re-injection. Without
  this, the context builder would see `is_new=False` and skip episodic
  memory, leaving the new ACP process with zero history.
- **Per-session semaphore**: serializes concurrent messages on the same
  thread key. `get_or_create()` acquires; caller must `release()` when done.
- **Idle cleanup**: expires sessions after `session.timeout_secs` (default
  30min). Never expires `BACKGROUND_KEY`. Dashboard per-tab sessions
  (`dashboard:{slot_key}`) idle-expire like any other session.

## APIs

| Method | Purpose |
|--------|---------|
| `start_pool(blocking=True)` | Pre-spawn warm + background sessions. `blocking=False` for non-blocking mode. |
| `get_or_create(key, agent=None, approval_policy="")` | Returns `(LLMProvider, is_new, resumed)`. Uses warm pool for new sessions (default agent only). Sessions with a resume mapping skip warm pool (cold start needed for `session/load`). Non-default agents skip warm pool and get `model=None` so kiro-cli uses the agent's own model. `approval_policy` is persisted on the new `_Session` — callers (e.g. subagent) pass parent policy so the session inherits it. |
| `check_context_usage(key, provider)` | Returns %. Triggers compaction at configured threshold (default 90%), warns at 75%. |
| `record_success(key)` / `record_failure(key)` | Circuit breaker tracking. |
| `release(key)` | Release per-session semaphore (must call in `finally`). |
| `cancel_current(key, *, wait_ack_timeout=0.0)` | Cancel in-flight operation without destroying session. Returns `CancelOutcome`. Default `wait_ack_timeout=0.0` preserves fire-and-forget behavior for internal callers (taskrunner, subagent, llm_helpers). |
| `stop_turn(key, *, force=False, on_soft=None, on_hard=None)` | Cooperative stop with kill fallback. Returns `StopOutcome` (`"soft"`, `"hard"`, or `"idle"`). Clears queue unconditionally, then sends `session/cancel` and waits up to `agent.soft_stop_budget_secs`; falls back to `reset()` + eager respawn on timeout or error. `force=True` skips cancel and goes straight to hard kill. `on_soft`/`on_hard` callbacks fire before return. |
| `reset(key)` | Kill session. Does NOT delete session map entry (kiro-cli file persists for future resume). |
| `remove(key)` | Kill session AND delete session map entry (explicit tab delete — no resume expected). |
| `close_all()` | Save all active session mappings, then shut down every session and drain warm pool. |
| `warm_pool_size` | Property: number of warm sessions available. |

## Stop Orchestration

`stop_turn()` is the shared orchestration layer for both dashboard and Slack stop surfaces. Sequence:

1. `clear_queue(key)` — queue drop is unconditional on first press.
2. If `force=True`: skip cancel, go straight to hard kill (step 4).
3. Send `session/cancel` via `provider.cancel(wait_ack_timeout=budget)`:
   - `"acked"` → set `session.prev_turn_cancelled = True`, call `on_soft` callback, return `"soft"`.
   - `"no_turn"` → return `"idle"`.
   - `"timeout"` or `"error"` → fall through to hard kill.
4. Hard kill: `reset(key)` → fire-and-forget `_eager_respawn(key)` task → call `on_hard` callback → return `"hard"`.

### Cancelled-turn context restore

`_Session.prev_turn_cancelled` is a one-shot flag set on soft-cancel
success. The next prompt handler (dashboard `_run_chat`, Slack
`handle_message`) reads and clears it, then calls
`context.build_cancelled_turn_preamble(conversation_log, session_key)` to
re-inject the cancelled user prompt and partial assistant output. This is
necessary because kiro-cli discards cancelled turns from its own ACP
conversation log, so the LLM has no memory of the interrupted request.

### Eager Respawn

After a hard kill, `_eager_respawn(key)` calls `get_or_create(key)` in a background task so the next user message finds a warm session. On failure, logs at debug and does nothing — the next message triggers `get_or_create` again via the normal path.

## Session Resume (SessionMap)

Persistent mapping of `session_key → kiro_session_id` stored at
`~/.kiroclaw/session_map.json`. Enables `session/load` to restore full
kiro-cli conversation history when a session is recycled.

**Only long-lived conversational sessions are mapped.** Stateless sessions
(cron, subagent, taskrunner, channel, secretary, side, heartbeat/background)
are excluded via `_STATELESS_PREFIXES`. The `side:` prefix is included so
`/side` conversations never resume across KiroClaw restarts — each cold-start
triggers `is_first_turn=True` in `build_side_message` which re-seeds the
parent snapshot + accumulated side history.

**Lifecycle:**
- `get_or_create()`: looks up mapping → if found and `.json` file exists,
  sets `resume_session_id` on the ACP client and skips warm pool. After
  `ensure_ready()`, saves the new `session_key → session_id` mapping.
- `reset()`: does NOT delete mapping — the kiro-cli session file persists
  on disk. Next `get_or_create` will try `session/load`.
- `remove()`: deletes mapping — explicit tab delete, no resume expected.
- `close_all()`: saves all active mappings before killing processes.
- `start_pool()`: prunes stale entries (files deleted by kiro-cli GC).

### Cross-Provider Continuity

kiro session IDs and Claude Code session IDs are NOT interchangeable:
- kiro: arbitrary string, stored in `~/.kiro/sessions/cli/<sid>.{json,jsonl}`
- Claude Code: UUID v4, stored in `~/.claude/projects/<encoded-cwd>/<sid>.jsonl`

When a user switches provider mid-session (e.g. config change from `acp` to
`claude_code`), conversation continuity is maintained via **history replay**,
never via session_id translation.

**Detection:** `detect_provider_switch(session_map, key, new_provider)` in
`session.py` compares the stored provider against the new one. Returns True
when a switch is detected (stored SID exists AND providers differ).

**Behavior on switch:**
1. `resume_sid` is discarded (not passed to the new provider process)
2. `SessionMap.clear_sid(key)` removes the stale SID from persistent state
3. `_Session.provider_switch_replay = True` flags the session for replay
4. The new provider's session_id (once obtained) is saved with the correct
   provider label
5. On the first prompt after the switch, `chat_runner` detects the flag and
   injects history from `compress_thread_history()` (KiroClaw's conversation_log)
6. The flag is consumed (set to False) — replay fires exactly once per switch

**Same-provider resume:** unaffected. Normal `session/load` path with full
native fidelity.

**Audit:** A `provider_switch_detected` SEL event is emitted with both the
stored and new provider names for observability.

**Atomic write:** tmp file + `os.replace()` prevents corruption on crash.

**Auto-prune:** `SessionMap.get()` auto-removes entries whose `.json` file
no longer exists. `SessionMap.prune()` bulk-removes all stale entries at
startup.

**Dashboard history key round-trip:** Session keys use `:` (e.g.
`dashboard:chat-1-xxx`) but JSONL filenames use `_safe_key()` which replaces
`:` with `_`. When a session is resumed from history, the slot name comes from
the filename stem (`dashboard_chat-1-xxx`), producing session key
`dashboard:dashboard_chat-1-xxx`. `SessionMap.get()` handles this by falling
back to the canonical form (`dashboard:chat-1-xxx`) when the direct lookup
fails.

## Slack Thread Linking

Sessions can be linked to Slack threads via `SessionMap` fields
`slack_thread_ts` and `slack_channel_id`. This enables bidirectional sync
between dashboard chat and Slack.

**API:**
- `SessionManager.set_slack_link(key, thread_ts, channel_id)` — persists to session map
- `SessionManager.get_slack_link(key) -> (thread_ts | None, channel_id | None)`
- `SessionManager.get_session_for_thread(thread_ts) -> key | None` — reverse lookup
- `SessionManager.set_channel(key, channel_id)` — backward-compat alias

**Slack handler:** calls `set_slack_link(session_key, session_key, channel)`
outside the `if is_new` guard so every message refreshes the link.

**Dashboard chat:** mirrors user messages to linked Slack threads via
`slack_client.post_message()`. The "Send to Slack" button (`slack/blocks.py`)
opens a DM thread, links the session, and posts the last 5 messages as context.

**Dashboard state:** `ChatSlot.summary()` includes `slack_linked: bool` so
the frontend can show a link indicator.

**Slash commands** (`slack/events.py`):
- `/kiroclaw sessions` — lists active sessions with Slack link status
- `/kiroclaw sessions resume <key>` — resumes a session in the current thread

**Block Kit builders** (`slack/blocks.py`): reusable Block Kit dict builders
for slash command UIs. Action IDs follow `mc_<command>_<action>[_<id>]`.

## DM Channel Session Keys & Mid-Turn Handling

DM channels (Telegram, WeCom) have no thread concept, so `messaging/link.py`
derives the session key with `build_dm_session_key(channel, agent, user, *,
gen, dm_scope)`:

- **Shape** (channel-first): `{channel}:{agent}:{chatType}:{user}` plus an
  optional `:gen{N}` suffix. The part before the suffix is a durable **bucket**
  (history and channel links hang off it); the **generation** rotates to start a
  fresh transcript within the bucket. `chatType` is `direct` today; `group` is
  reserved.
- **`dm_scope`** (`MessagingConfig.dm_scope`): `per-channel-peer` (default) —
  one bucket per `(channel, user)`; `unified` — all DMs collapse into a single
  `unified:{agent}` bucket for cross-surface continuity. `agent` is part of the
  bucket by design, so switching the configured agent starts a fresh session
  rather than replaying another agent's context.
- **Generation reset** rotates on `/new`, an idle window
  (`MessagingConfig.idle_reset_minutes`), or a daily boundary
  (`daily_reset_hour`), decided by `should_rotate_generation()`.

Legacy bare-thread Slack keys are unaffected — they keep the
`canonical_key`/`legacy_key` shim. The DM channels are recent, so the key shape
carries no prior persisted history to migrate.

### Mid-turn messages (steer / queue)

`SessionManager.is_busy(key)` reports whether a turn holds the session
semaphore. When a DM arrives mid-turn, the dispatcher acts on
`MessagingConfig.queue_mode`:

- `steer` (default): fold the message into the running turn via the provider's
  steer channel.
- `queue`: enqueue it — checked atomically against the semaphore, so a turn
  that finishes in the window runs the message instead of stranding it — and
  drain it after the turn, iteratively and capped (not recursively).

WeCom always steers regardless of `queue_mode`: its replies are bound to the
inbound request, so a queued-then-drained reply can't be delivered later
(capability-driven, like `supports_proactive_send=False`).

## Session Lifecycle at Startup

```
start_pool()
  ├── _enforce_denied_commands()  → inject deniedCommands into ALL agent configs
  ├── _spawn_warm() × pool_size   → warm pool queue (instant assignment)
  └── _ensure_background()        → BACKGROUND_KEY session (persistent)
```

## Security: deniedCommands Enforcement

`_enforce_denied_commands()` (from `agent.py`) injects the bundled `deniedCommands`
patterns into agent configs in `~/.kiro/agents/`. The scope is controlled by
`agent.enforce_denied_commands` config option:

- `"all"` (default): enforce on ALL agent configs (kiroclaw + AIM + third-party)
- `"kiroclaw"`: only enforce on `kiroclaw.json`, skip other agents (lite agents always skipped)

This addresses user complaints about KiroClaw overwriting customizations on non-KiroClaw agents every ~60 seconds.

- **At startup**: `start_pool()` calls it before spawning any sessions
- **Periodic**: `_cleanup_loop()` calls it every ~60s (catches manual edits)
- **At install**: `install_agent()` calls it after writing `kiroclaw.json`
- **Mtime-based**: skips unchanged files for efficiency
- **Merge semantics**: union of existing + bundled patterns (never removes agent's own)
- **Targets**: both `execute_bash` and `shell` tool settings
- **Config**: set via `~/.kiroclaw/config.json` or Dashboard Config Summary

## Orphaned MCP Server Cleanup

`_cleanup_orphaned_mcp_servers()` kills MCP server processes that survived
session teardown.  kiro-cli-chat spawns MCP servers (kiro_claw mcp-core/cron,
builder-mcp, andes-mcp, aim slack-mcp) in separate process groups.  When a
session dies, `killpg` only reaches the kiro-cli process group — MCP servers
in other groups get reparented to init and leak memory.

**Tracking**: at session init, `AcpClient.ensure_ready()` snapshots all
descendant PIDs and persists them to `kiro_pids.txt` as `child_pid:parent_pid`
pairs via `_track_child_pids(pids, parent_pid=self._pid)`.  On clean shutdown,
`_reset_state()` removes them via `_untrack_child_pids()`.  If the gateway
crashes, the entries remain in the file for the next startup.

**Detection**: reads `kiro_pids.txt`, processes only `child:parent` lines
(bare PID lines are kiro-cli parents handled by `cleanup_orphaned_sessions()`).
If the child is alive but its parent PID is dead, the child is orphaned and
killed.

**Why not ancestor walk?** MCP servers are spawned in separate process groups
and immediately reparented to init (ppid=1) even while the session is alive.
Walking the process tree would always conclude they are orphaned.  Storing the
parent PID explicitly avoids this.

**Safety**:
- Zero false positives — only kills PIDs we tracked, only when the specific
  parent session that spawned them is confirmed dead
- Dead children are silently pruned from the file
- Bare PID lines (kiro-cli parents) are ignored by MCP cleanup

**Invocation**:
- **At startup**: `cleanup_orphaned_sessions()` calls it after PID-file cleanup
- **Periodic**: `_cleanup_loop()` calls it alongside idle session expiry (~60s)
- **At shutdown**: `cleanup_orphaned_sessions()` on signal/exit

## Resource Budget (Gateway Mode)

| Session | Key Pattern | Lifetime | Process |
|---------|-------------|----------|---------|
| User chat | `{thread_ts}` | Idle timeout (30 min) | Own kiro-cli |
| Dashboard tab | `dashboard:{slot_key}` | Idle timeout (30 min) | Own kiro-cli (from warm pool) |
| Cron job | `cron:{job_id}` | One-shot (reset after) | Own kiro-cli (from warm pool) |
| Background | `_bg` | Entire runtime (recycled at 70%) | Shared kiro-cli |
| Heartbeat | `_bg` | Shared | Shared kiro-cli |
| Lesson extract | `_bg` | Shared | Shared kiro-cli |
| Subagent | `subagent:{uuid}` | Task duration | Own kiro-cli |
| TaskRunner step | `taskrunner:{task_id}:step{N}` | Step duration (reset after) | Own kiro-cli (max 2 concurrent via semaphore) |
| TaskRunner decompose | `taskrunner:{task_id}:decompose` | Seconds | Own kiro-cli |
| TaskRunner review | `taskrunner:{task_id}:review` | Seconds | Own kiro-cli |
| TaskRunner acceptance | `taskrunner:{task_id}:acceptance` | Seconds | Own kiro-cli |
| Warm spare | _(in pool queue)_ | Until assigned | Pre-started kiro-cli |

**Cold-start semaphore**: `_start_sem = Semaphore(2)` limits concurrent
`provider.start()` calls to 2 for memory safety. This
prevents resource exhaustion when multiple sessions cold-start simultaneously,
while still allowing 3 parallel subagents to all run concurrently once started
(they queue briefly during cold-start).

**Parallel step throttling**: TaskRunner limits concurrent step sessions
to `max_parallel_steps` (default 2) via `asyncio.Semaphore`. Cold starts
are staggered by 3s. A system load guard pauses spawning when CPU load
exceeds 85% of available cores.

## Compaction Race Handling

If user sends message during compaction: `get_or_create()` sees key in
`_compacting` → creates fresh session. Background task finishes → checks
provider identity → only shuts down old provider.
