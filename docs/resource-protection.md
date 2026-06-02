# Resource Protection Mechanisms

KiroClaw runs long-lived LLM sessions that spawn OS processes (kiro-cli, MCP servers) across
multiple workflows — chat subagents, cron jobs, task runner steps, and background sessions.
Each workflow has different failure modes (event-loop saturation, orphaned tasks, hung processes,
context overflow), so protection is layered: primary timeouts catch the common case, independent
watchdogs catch what timeouts miss, and startup/periodic sweeps clean up anything that survived
a gateway crash. This defense-in-depth approach ensures no single mechanism is a single point
of failure.

## Mechanism Table

| Mechanism | Module | Scope | Timeout / Threshold | Independent Watchdog? | What Happens When It Fires |
|-----------|--------|-------|--------------------|-----------------------|---------------------------|
| `asyncio.wait_for` on `_run_inner` | `subagent.py` | Subagent tasks | 30 min (`_TIMEOUT_SECS`) | No (see reaper below) | Raises `TimeoutError`, marks subagent failed, resets session |
| Periodic reaper loop | `subagent.py` | Subagent tasks | 60s sweep (`_REAPER_INTERVAL`), kills at 30 min | Yes — runs independently of spawning session | `_force_reap`: reset → SIGKILL fallback → mark done → SEL audit → announce |
| Reset timeout in `_run` finally | `subagent.py` | Subagent cleanup | 30s (`_RESET_TIMEOUT`) | No | SIGKILL fallback + SEL audit if `reset()` hangs |
| Turn limit | `subagent.py` | Subagent tool calls | 100 turns (`_TURN_LIMIT`, configurable) | No | Stops execution, returns partial output |
| `asyncio.wait_for` on `_execute` | `cron.py` | Cron jobs | 30 min (`_JOB_TIMEOUT_SECS`) | No | Raises `TimeoutError`, logs error, marks job failed |
| Periodic reaper loop | `cron.py` | Cron jobs | 60s sweep (`_REAPER_INTERVAL`), kills at 30 min | Yes — runs independently of job execution | `_force_reap`: reset → SIGKILL fallback → mark failed → SEL audit |
| Task runner watchdog | `taskrunner.py` | Task runner steps | 60 min warn / 2 hr kill (`STALL_TIMEOUT` / `STALL_CANCEL_TIMEOUT`) | Yes — 30s heartbeat loop (`_HEARTBEAT_INTERVAL`) | Notifies on stall, resets stuck session after 2 hr |
| Global task timeout | `taskrunner.py` | Entire task run | User-configurable (`--timeout`) | Checked in watchdog loop | Stops task run, marks failed |
| ACP process death detection | `acp/client.py` | All sessions | 5 consecutive empty reads (`_MAX_CONSECUTIVE_EMPTY`) | No | Raises `AcpProcessDied`, triggers session recovery |
| ACP init timeout | `acp/client.py` | Session creation | 4 min (`_INIT_TIMEOUT`) | No | Raises `AcpTimeoutError`, retries once |
| ACP prompt timeout | `acp/client.py` | Per-prompt | 2 hr (`_DEFAULT_PROMPT_TIMEOUT`) | No | Raises `AcpTimeoutError` |
| ACP read timeout | `acp/client.py` | Per-readline | 20s (`_READ_TIMEOUT`) | No | Allows `CancelledError` delivery at each yield point |
| Process group kill | `acp/client.py` | Process cleanup | Immediate | No | `killpg(SIGTERM)` → `killpg(SIGKILL)` → `_kill_escaped_children` for different-PGID descendants |
| Bounded restart shutdown | `dashboard/handlers.py` | Dashboard ⚡ Apply & Restart | 5s (`_SHUTDOWN_TIMEOUT_SECS`) | No | `asyncio.wait_for` on `provider.shutdown()`; `_sync_kill_provider` fallback on timeout |
| Subagent injection outer cap | `subagent.py _run()` | Per-subagent completion | 1200s (`_ON_DONE_TIMEOUT`) | No | Semaphore wait + injection combined; on timeout kills stuck kiro-cli via `sessions.reset()` and queues failure event for parent to drain |
| Subagent injection inner cap | `gateway.py` | Per `stream_and_collect` | 300s (`INJECTION_TIMEOUT`) | No | `_inject_with_retry` up to 2 retries (3 attempts) with backoff; bounded by outer 1200s cap |
| Prompt-busy recovery | `llm_helpers.py` | Per `stream_and_collect` | 2 retries + backoff | No | Cancels orphaned prompt; kills provider on exhaustion |
| Message queue | `session.py` + `events.py` | Per Slack thread | Unbounded FIFO | No | Queues when busy; `message_deleted` cancels; `!stop` clears |
| Orphaned dashboard reaping | `session.py` | Dashboard sessions | Immediate | Yes | `set_active_dashboard_slots()` reaps sessions whose slot is gone |
| Stale PID cleanup | `session.py` | `session_pid_*.txt` | Startup | No | Removes PID files for dead processes |
| Empty dir cleanup | `session.py` | `sessions/` subdirs | Startup | No | Removes empty dirs from timed-out subagents |
| `cleanup_orphaned_sessions` | `session.py` | All kiro-cli PIDs | Startup + shutdown only | No | Reads `kiro_pids.txt`, validates via `/proc`, sends SIGKILL, clears file. Also calls `_cleanup_orphaned_mcp_servers()` internally at startup |
| `_cleanup_orphaned_mcp_servers` | `session.py` | MCP child PIDs | Every ~5 min (periodic sweep) | Yes — runs in `_cleanup_loop` | Scans for orphaned MCP processes, sends SIGKILL |
| Idle session expiry | `session.py` | All sessions | 30 min idle (configurable via `session.timeout_secs`) | Yes — runs in `_cleanup_loop` (~5 min interval) | Calls `provider.shutdown()`, removes session |
| Circuit breaker | `session.py` | Per-session | 5 consecutive failures (`_CIRCUIT_BREAKER_THRESHOLD`) | No | Auto-resets session (kills process, creates fresh) |
| Context compaction | `session.py` | Chat sessions | Configurable (`session.autocompact_pct`, default 90%) | No | Sends `/compact` to kiro-cli to free context window |
| Background session recycle | `session.py` | Background sessions (cron, subagent) | 70% context usage (`_BG_RECYCLE_PCT`) | No | Recycles session before context overflow |
| Watchdog process liveness | `taskrunner.py` | Task runner steps | 2 consecutive dead checks (`_DEAD_THRESHOLD`) at 30s intervals | Yes — part of watchdog loop | Resets session to trigger crash recovery |

## Per-Workflow Coverage Matrix

|  | Primary Timeout | Watchdog / Reaper | Process Cleanup | Context Management |
|--|----------------|-------------------|-----------------|-------------------|
| **Chat subagents** | ✅ `wait_for` 30 min | ✅ Reaper (60s sweep) | ✅ `reset()` + SIGKILL fallback | ✅ `_BG_RECYCLE_PCT` 70% recycle |
| **Cron jobs** | ✅ `wait_for` 30 min | ✅ Reaper (60s sweep) | ✅ `reset()` + SIGKILL fallback | ✅ `_BG_RECYCLE_PCT` 70% recycle |
| **Task runner** | ✅ Global timeout + stall detection | ✅ Watchdog (30s heartbeat) | ✅ `_cleanup_run_sessions` + `asyncio.shield` | ✅ Compaction at 90% |
| **Background sessions** (shared: cron, heartbeat, lessons) | ⚠️ Idle expiry only (30 min) | ✅ Periodic sweep (~5 min) | ✅ `cleanup_orphaned_sessions` at startup | ✅ `_BG_RECYCLE_PCT` 70% recycle |

## Known Gaps

1. **Subagent timeout is not configurable.** `_TIMEOUT_SECS` (30 min) is hardcoded. Some
   legitimate tasks (large code generation, complex multi-tool workflows) may need longer.
   Tracked: [configurable subagent timeout](https://taskei.amazon.dev/tasks/1b15e311)

2. **`cleanup_orphaned_sessions` only runs at startup/shutdown.** If a session's process dies
   mid-run without triggering `AcpProcessDied` (e.g. OOM kill), the PID stays in
   `kiro_pids.txt` until the next gateway restart. The periodic `_cleanup_orphaned_mcp_servers`
   sweep catches MCP children but not the root kiro-cli process.

3. **No per-process resource limits (cgroups/ulimits).** Agent subprocesses and MCP servers
   run without CPU, memory, or file descriptor caps. A runaway process can consume unlimited
   host resources. Mitigation: `apply_resource_limits()` helper exists in `security.py` for
   use as `preexec_fn` but is not yet wired to subprocess spawn sites. Tracked as Shepherd
   finding 444f0e03.

## Interaction Notes

- **Reaper `reaped` flag prevents double cleanup.** When the reaper force-kills a subagent,
  it sets `info.reaped = True`. The `_run()` method's `CancelledError` handler and `finally`
  block check this flag and skip their own cleanup (release, reset, decrement, announce) to
  avoid double side-effects. The cron reaper uses the same pattern: `_reaped_jobs` set
  prevents `_run_job_isolated` from merging stale results after the reaper has already
  updated job state.

- **`asyncio.shield` in task runner protects cleanup from cancellation.** When a task run is
  cancelled, `_cleanup_run_sessions` is wrapped in `asyncio.shield()` so session resets
  complete even if the parent task is cancelled. This prevents orphaned processes.

- **Circuit breaker and context compaction are complementary.** The circuit breaker handles
  repeated failures (likely a broken session), while compaction handles context window
  exhaustion (a healthy session that's been running too long). Both trigger session reset
  but for different reasons.

- **Idle expiry and `_cleanup_orphaned_mcp_servers` run on the same loop.** The
  `_cleanup_loop` in `session.py` runs every ~5 min (timeout/6, min 60s) and performs both
  idle session expiry and orphaned MCP server cleanup in the same iteration.

- **ACP read timeout enables cooperative cancellation.** The 20s `_READ_TIMEOUT` on each
  `readline()` in the prompt loop ensures `CancelledError` can be delivered at every yield
  point, which is what makes the reaper's `task.cancel()` effective.
