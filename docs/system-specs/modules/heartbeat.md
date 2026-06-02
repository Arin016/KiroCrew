# Heartbeat Module

Last Updated: 2026-05-31 (per-task timeout + session teardown for unattended turns)

## Overview

The heartbeat service (`kiro_claw/heartbeat.py`) runs periodic background tasks on a configurable interval (default 60s).

## Responsibilities

1. **Task processing** — reads `~/.kiroclaw/workspace/HEARTBEAT.md`, sends non-empty tasks to the agent
2. **FTS index rebuild** — every 15 ticks (~15 min at default interval)

## HEARTBEAT.md Format

```md
# Heartbeat Tasks

<!-- Add tasks below (one per line). KiroClaw picks them up on next heartbeat. -->
- check my pipeline status
- summarize open PRs
```

- One task per line (no multiline support)
- Lines starting with `#`, empty lines, and HTML comments (`<!-- -->`) are ignored
- List markers (`-`, `*`, `- [ ]`, `- [x]`) are stripped
- File is auto-created on first start with an empty template

## Task Lifecycle

1. Tick fires → read file → extract tasks
2. For each task: call `on_task` callback (gateway sends through ACP, posts result)
3. Callback returns response text; heartbeat checks for `HEARTBEAT_KEEP` sentinel
4. Tasks with `HEARTBEAT_KEEP` in response are retained for next tick (incomplete)
5. Tasks without the sentinel are removed (complete)
6. Tasks that raise exceptions are retained automatically (retry)

### Task Retention (`HEARTBEAT_KEEP`)

The agent can include `HEARTBEAT_KEEP` anywhere in its response to signal incomplete:

```
Progress: 3/5 files processed. HEARTBEAT_KEEP
```

- `_should_keep(result)` checks for the sentinel (case-insensitive)
- `None` return (legacy) treated as complete (removed)
- Sentinel stripped from display text before posting
- Deliver tags (`<!-- deliver:channel_id -->`) preserved on retention

## Concurrency

- `_processing` flag prevents overlapping ticks from double-processing tasks
- FTS rebuild runs independently of task processing
- Uses dedicated `heartbeat:task` session key — no interference with user or cron sessions

## Per-Task Timeout (Unattended Turn Bound)

A heartbeat turn runs without a human present. The `_heartbeat_task` callback in
`slack/gateway.py` wraps `stream_and_collect` in
`asyncio.wait_for(..., timeout=HEARTBEAT_TASK_TIMEOUT_SECS)` (1800s / 30 min,
mirroring cron's `_JOB_TIMEOUT_SECS`). This is the analogue of cron's
`_execute_with_timeout`.

Without it, if the agent calls a non-allowlisted tool, the interactive-approval
callback would block on the human-approval wait with no human present — wedging
`HeartbeatService._processing=True` and freezing the whole heartbeat subsystem.

On `asyncio.TimeoutError`:
1. Reset the background session (`sessions.reset(BACKGROUND_KEY)`) BEFORE the
   `finally` releases it — kills the lingering `claude-agent-acp` turn/process so
   it does not outlive the timeout. A failing reset is logged and swallowed.
2. Log a warning.
3. Return a graceful incomplete result string (the loop is NOT crashed).
4. The existing `finally` still runs: `release(BACKGROUND_KEY)` +
   `recycle_background()`.

Background approvals additionally deny-fast (see security / slack-gateway specs),
so the common "non-allowlisted tool" case resolves in minutes; this hard timeout
is the backstop for any other long-running turn.

## Gateway Wiring

`HeartbeatService` is started in `slack/gateway.py` after cron service:
- `on_task` callback: opens owner DM, resets session, streams response, posts result
- Callback re-raises exceptions so heartbeat can track failures
- Stopped during gateway shutdown

## Constants

| Constant | Value | Location |
|----------|-------|----------|
| `_DEFAULT_INTERVAL` | 60 | `heartbeat.py` |
| `_FTS_REBUILD_TICKS` | 15 | `heartbeat.py` |
| `HEARTBEAT_TASK_TIMEOUT_SECS` | 1800 | `heartbeat.py` |
| `HEARTBEAT_FILE` | `HEARTBEAT.md` | `heartbeat.py` |

## Known Limitations

- No multiline tasks — each line is a separate task
- If user edits file while tasks are processing, new additions may be lost
- Exception-retried tasks have no max retry count

## Delivery Modes

Tasks can specify a delivery target via HTML comment tags:

```md
- [ ] Check CR-123 <!-- deliver:prompt:dashboard:chat-0 -->
```

### Supported Modes

| Mode | Syntax | Behavior |
|------|--------|----------|
| Slack DM (default) | _(no tag)_ | Posts result to owner's Slack DM |
| Dashboard slot | `<!-- deliver:prompt:dashboard:<slot> -->` | Injects result into a specific dashboard chat slot (e.g., `chat-0`, `chat-3`) |
| Channel | `<!-- deliver:<channel_id> -->` | Posts to a specific Slack channel |

### Dashboard Delivery (`prompt:dashboard:<slot>`)

Resolves `chat-N` slot names to active session keys. The result is injected as a user message into the target slot, triggering an LLM response in that session. Useful for heartbeat tasks that should report back into an active dashboard conversation.

Slot resolution: `chat-0` → first active slot, `chat-3` → fourth slot. Falls back to Slack DM if slot not found.

### Slack Suppression for Incomplete Tasks

When a task response contains `HEARTBEAT_KEEP`, Slack delivery is suppressed. The task is retained for the next tick without notifying the user. Only completed tasks (no `HEARTBEAT_KEEP`) trigger Slack/dashboard delivery.
