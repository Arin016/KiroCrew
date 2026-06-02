# Subagents & Parallel Work

KiroClaw can spawn background subagents to handle tasks in parallel. This is
useful for fan-out work like reviewing multiple packages, running parallel
searches, or delegating independent tasks.

## How to Use

### Via Chat

Ask naturally:
- "Review these 3 packages in parallel"
- "Search for X, Y, and Z at the same time"
- "Run this task in the background"

KiroClaw uses the `spawn_run` MCP tool to create subagents.

### Via Slack

```
spawn run "review the latest CR for MyPackage"
spawn list
```

### Via MCP Tool

The `spawn_run` tool accepts:
- `task` — single task description
- `tasks` — array of tasks for parallel execution
- `agent` / `agents` — optional agent name(s) for each task

## How It Works

1. KiroClaw spawns one or more subagent processes
2. Each subagent gets its own agent session with full tool access
3. Results are automatically injected back as `[Subagent completion event]`
4. KiroClaw synthesizes the results into a final response

## Limits

- **Max concurrent**: 3 subagents at a time (configurable via `agent.max_subagents`)
- **Timeout**: 30 minutes per subagent task, 20 minutes delivery, 5 minutes per injection attempt
- **Turn limit**: 100 turns per subagent (configurable via `agent.subagent_max_turns`, UI max 200)
- **Memory guard**: spawns are refused when available memory drops below 4 GB (configurable via `agent.spawn_min_memory_gb`, set to 0 to disable)
- **No nesting**: subagents cannot spawn their own subagents
- **Redaction**: task strings in SubagentInfo are redacted (credentials + exfiltration URLs) before surfacing to Slack/dashboard

## Named Agents

You can specify which agent a subagent should use:

```
spawn_run(tasks=["review code", "check tests"], agents=["code-reviewer", "test-analyzer"])
```

Named agents use their own system prompt and skills.

## Results

Subagent results are posted to:
- The dashboard (via WebSocket notification)
- Slack DM (with an ack button)
- The parent conversation (as completion events)

Long results are split into multiple Slack messages (3900 chars per chunk).

## Completion Event Truncation

The completion event injected back into the parent conversation is a bounded
copy of the subagent's streamed transcript. The full transcript stays in
`~/.kiroclaw/subagents/<id>/result.txt` while the subagent is running and is
removed after the completion event is delivered to the parent. Use the
`spawn_status` MCP tool to read the full transcript before that cleanup
completes.

Two `agent.*` config knobs control what the parent session sees:

| Key | Values | Default | Effect |
|-----|--------|---------|--------|
| `agent.completion_keep` | `"head"` / `"tail"` / `"both"` | `"head"` | Which end of the transcript to keep when it exceeds the cap |
| `agent.completion_keep_chars` | int (`0` disables) | `3000` | Character cap applied after `completion_keep` |

Pick the mode that matches how your agents emit their useful output:

- **`head`** — first N characters. Best for agents whose verdict appears
  up front (verdict-then-evidence).
- **`tail`** — last N characters. Best for agents that narrate throughout
  and summarize at the end (developer agents, code reviewers, on-call
  triage).
- **`both`** — roughly N/2 from the head, a middle marker, and N/2 from
  the tail. Best for parent agents that need both the task framing and
  the conclusion.

Set `completion_keep_chars: 0` to disable truncation entirely.

Set via `kiroclaw config set agent.completion_keep tail` or by editing
`~/.kiroclaw/config.json` directly.
