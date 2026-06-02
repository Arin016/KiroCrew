# TaskRunner Module

Last Updated: 2026-05-10 (pause/resume, crash recovery, force_approval gates)

## Overview

Autonomous task executor that reads a spec file, decomposes it into
ordered steps via LLM, and executes each step through ACP sessions
with test verification, retries, and progress checkpointing.

Supports multiple concurrent tasks, interactive tool approval,
per-step session isolation with full memory injection, git-coordinated
step commits and reverts, independent review via actual diffs,
cycle detection, disk persistence across restarts, activity-aware
stall detection, and throttled parallel execution to prevent resource
exhaustion.

## Module Architecture

The task runner is split into 7 focused modules under `src/kiro_claw/`:

```
taskrunner.py        (orchestrator, ~700 lines)
├── task_models.py   (data models + constants, 119 lines)
├── task_planner.py  (LLM decomposition + step parsing, ~250 lines)
├── task_executor.py (step execution + retries + tests, ~500 lines)
├── task_reviewer.py (acceptance review + self-review + replan, ~284 lines)
├── task_reporter.py (status dict + chat context + watchdog, ~192 lines)
└── task_persistence.py (save/load runs + checkpoints, ~174 lines)
```

### Module Responsibilities

| Module | Class/Functions | Responsibility |
|--------|----------------|----------------|
| `task_models.py` | `StepStatus`, `Step`, `WorkingMemory`, `TaskRun`, `NotifyCallback`, constants | Shared data types and configuration constants |
| `task_planner.py` | `TaskPlanner` | LLM spec decomposition, step parsing, dependency normalization, acceptance step creation |
| `task_executor.py` | `TaskExecutor`, `group_parallel_steps()` | Step execution with retry/recovery budgets, prompt building, context compaction, test running, self-review |
| `task_reviewer.py` | `TaskReviewer` | Acceptance review (LLM checks criteria), replan after failure (delegates to planner) |
| `task_reporter.py` | `build_status_dict()`, `build_plan_chat_context()`, `watchdog_loop()` | Status reporting, plan-to-chat formatting, stall detection watchdog |
| `task_persistence.py` | `persist_runs()`, `load_runs()`, `save_progress()`, `load_checkpoint()`, `build_resume_context()` | Standalone I/O functions for JSON run storage and TASK_PROGRESS.md checkpointing |
| `taskrunner.py` | `TaskRunner` | Orchestrator — creates planner/executor/reviewer, manages lifecycle, delegates to modules |

### Import Graph (no cycles)

```
task_models ← task_persistence
task_models ← task_planner
task_models ← task_executor
task_models ← task_reviewer (+ task_planner for replan)
task_models ← task_reporter
task_models ← taskrunner (+ all above modules)
```

### Backward Compatibility

`taskrunner.py` re-exports all public symbols so existing imports work unchanged:
```python
from kiro_claw.task_models import StepStatus, Step, WorkingMemory, TaskRun, NotifyCallback  # noqa: F401
```

These files import from `kiro_claw.taskrunner` and require no changes:
- `dashboard/handlers.py` → `StepStatus`, `TaskRun`
- `dashboard/server.py` → `TaskRunner`
- `dashboard/state.py` → `TaskRunner`
- `git_coord.py` → `Step`, `TaskRun`
- `slack/gateway.py` → `TaskRunner`
- `slack/handler.py` → `TaskRunner`
- `cli.py` → `TaskRunner`

## Public API

### `TaskRunner`

```python
class TaskRunner:
    def __init__(
        self,
        sessions: SessionManager,
        context_builder: ContextBuilder | None = None,
        on_notify: NotifyCallback | None = None,
        on_approval: ApprovalCallback | None = None,
        auto_test: bool = True,
        auto_commit: bool = False,
        work_dir: Path | None = None,
        conversation_log: ConversationLog | None = None,
        consolidator: HistoryConsolidator | None = None,
        lesson_store: LessonStore | None = None,
        fresh: bool = False,
        global_timeout: float = 0.0,
        token_budget: int = 0,
        max_parallel_steps: int = 2,
    ) -> None: ...

    # Internally creates: self._planner (TaskPlanner), self._executor (TaskExecutor),
    # self._reviewer (TaskReviewer) — delegates to these modules

    async def run(self, spec_path: str | Path, task_id: str = "", name: str = "", source: str = "file") -> TaskRun
    def start_background(self, spec_path: str | Path, agent: str = "", name: str = "", source: str = "file") -> str
    def cancel(self, task_id: str | None = None) -> None  # None = cancel all
    def status(self) -> dict

    @property
    def running(self) -> bool
    @property
    def current_run(self) -> TaskRun | None

    # Internal but accessed by handlers for delete
    _runs: dict[str, TaskRun]
    _persist_runs() -> None
```

### Task Source & Visibility

`TaskRun.source` tracks where a task was started from. The dashboard Tasks page
filters runs by source to avoid showing cron-triggered background tasks:

```python
dashboard_sources = {"text", "spec", "file", "chat", "dashboard"}
```

| Entry Point | Source Value | Visible on Tasks Page |
|-------------|-------------|----------------------|
| Dashboard UI | `"dashboard"` | ✅ |
| Slack `run <path>` | `"chat"` | ✅ |
| MCP `task_run` tool | `"file"` (default) | ✅ |
| CLI `kiroclaw run` | `"file"` (default) | ✅ |
| `plan()` API | `"text"`, `"spec"`, `"file"` | ✅ |
| Cron job | must pass `source="cron"` | ❌ (filtered out) |

### Data Types

```python
class StepStatus(Enum):
    PENDING, IN_PROGRESS, REVIEWING, PASSED, FAILED, SKIPPED

@dataclass
class Step:
    index: int
    title: str
    description: str
    status: StepStatus = PENDING
    attempts: int = 0
    error: str = ""
    result: str = ""  # updated during streaming (partial results visible)
    requires_approval: bool = False
    depends_on: list[int] = field(default_factory=list)

@dataclass
class TaskRun:
    spec_path: str
    spec_content: str
    steps: list[Step]
    started_at: float
    finished_at: float
    status: str  # pending, running, completed, failed, cancelled
    current_step: int
    error: str
    tokens_used: int
    replan_count: int
    memory: WorkingMemory
    task_id: str
    work_dir: str
    last_step_time: float  # tracks activity for watchdog
    branch_name: str       # git branch for task (e.g. kiroclaw/task/{task_id})
    base_branch: str       # original branch before task started
    commit_hashes: list[str]  # per-step commit SHAs
    worktree_path: str     # git worktree path (empty if git init)
    repo_root: str         # original repo root (for worktree cleanup)
```

## Concurrent Tasks

- `_runs: dict[str, TaskRun]` — keyed by task_id
- `_tasks: dict[str, asyncio.Task]` — background asyncio tasks
- `start_background()` accepts optional `agent` param, returns task_id (`{spec_stem}_{timestamp}`)
- All `get_or_create()` calls pass `agent=self._agent` so the task runs with the specified agent
- Each step gets its own session: `taskrunner:{task_id}:step{N}` (fresh per step, reset after)
- Each task gets its own work dir: `{work_dir}/{spec_stem}/`
- `cancel(task_id)` cancels specific task; `cancel()` cancels all
- Completed runs pruned on new start (keep last 10)
- `_tasks` cleaned in `finally` block (no leaks)
- Max `_MAX_CONCURRENT_TASKS` (3) running tasks — enforced in `start_background()` and `execute_plan()`
- Replanned steps also reset sessions after execution (no leaks in `_try_replan`)

## Pause / Resume

Tasks can be paused and resumed without losing progress:

- `pause(task_id)` — sets `run.status = "paused"`, cancels the asyncio task gracefully
- `resume(task_id)` — restarts execution from the current step (not from beginning)
- Paused status visible in dashboard UI as distinct color/icon
- API: `POST /api/taskrunner/{task_id}/pause`, `POST /api/taskrunner/{task_id}/resume`

### Crash Recovery

On gateway restart, any task with `status == "running"` is automatically transitioned to `"paused"`:

- Prevents zombie tasks that appear running but have no backing asyncio task
- User can resume manually from dashboard
- Persisted via `runs.json` — status survives restart

### Force Approval Gates

Steps can be marked with `force_approval: true` in the spec. These gates block execution even in YOLO mode:

- Task pauses at the gate, shows inline Approve/Deny buttons in dashboard
- User must explicitly approve before the step executes
- Useful for destructive operations (deploy, delete, publish)
- Frontend: inline approval buttons rendered in project detail view

## Parallel Step Throttling

Parallel groups are throttled to prevent resource exhaustion from
simultaneous kiro-cli cold starts. Each kiro-cli process spawns ~4-5
MCP server child processes, so N parallel steps = ~5N processes all
initializing at once.

**Three-layer protection:**

1. **Semaphore** (`asyncio.Semaphore(_max_parallel_steps)`): caps concurrent
   step sessions within a parallel group. Default 2, configurable via
   `taskrunner.max_parallel_steps` in `~/.kiroclaw/config.json`.

2. **Stagger delay** (`_PARALLEL_STAGGER_SECS = 3.0`): each step in a
   parallel group waits `index * 3s` before starting, so cold starts
   don't overlap their heaviest initialization phase.

3. **System load guard** (`_wait_for_load()`): before spawning a session,
   checks `os.getloadavg()`. If 1-minute load average exceeds 85% of
   CPU count, waits up to 60s with 5s polling. Non-fatal on platforms
   where `getloadavg` is unavailable.

**Config:**
```json
{"taskrunner": {"max_parallel_steps": 2}}
```

Set to 1 for sequential-only execution on resource-constrained machines.

## Runs Persistence

Finished runs saved to `{work_dir}/runs.json` as JSON array.
Loaded on `__init__` — survives gateway restarts.

- Persisted on: task completion, task delete
- Each run stores: task_id, spec_path, status, timestamps, error, tokens, replans, step_details (result truncated to 2K)
- Delete via `DELETE /api/taskrunner/{task_id}` removes from memory and disk

## Access Paths

| Path | Entry Point | Behavior |
|------|-------------|----------|
| CLI | `kiroclaw run TASK.md` | Blocking, stdout progress, `--no-test` flag |
| Slack | `run <path>`, `run status`, `run cancel` | Keyword interception in handler |
| Dashboard | REST API + Tasks UI panel | See API Endpoints below |

## API Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/api/taskrunner` | Status with all runs, step_details |
| POST | `/api/taskrunner` | Start from file path or inline (`__inline__:` prefix) |
| POST | `/api/taskrunner/cancel` | Cancel specific (`{task_id}` in body) or all |
| DELETE | `/api/taskrunner/{task_id}` | Delete finished run from memory + disk |
| POST | `/api/taskrunner/{task_id}/retry` | Retry from step N (`{from_step}` in body) |
| POST | `/api/taskrunner/{task_id}/to-chat` | Open task results in a new chat slot for manual review |
| POST | `/api/taskrunner/refine` | Refine user input → task spec (SSE stream) |
| GET | `/api/taskrunner/refine` | Refine status |
| POST | `/api/taskrunner/refine/cancel` | Cancel refine |
| POST | `/api/taskrunner/refine/answer` | Answer clarifying question during refine |
| POST | `/api/reveal` | Reveal file path in Finder (`open -R` macOS, `xdg-open` Linux) |

### Status Response

```json
{
  "running": true,
  "runs": [{
    "task_id": "my-task_1771822344",
    "running": true,
    "status": "running",
    "spec": "/path/to/spec.md",
    "spec_name": "my-task",
    "started_at": 1771822344.0,
    "finished_at": 0,
    "steps": 3,
    "current_step": 2,
    "completed": 1,
    "failed": 0,
    "skipped": 0,
    "error": "",
    "tokens_used": 5000,
    "replan_count": 0,
    "step_details": [{
      "index": 1, "title": "Create handler", "description": "...",
      "status": "passed", "error": "", "result": "...(up to 2K)...", "attempts": 1
    }],
    "work_dir": "/path/to/work/dir",
    "branch_name": "kiroclaw/task/my-task_1771822344"
  }]
}
```

## Constants (in `task_models.py`)

| Constant | Value | Purpose |
|----------|-------|---------|
| `MAX_RETRIES` | 3 | Logic/test failure attempts per step |
| `MAX_RECOVERIES` | 2 | Process crash recovery budget per step |
| `MAX_REPLAN` | 2 | Plan revision attempts after step exhausts retries |
| `MAX_TOTAL_STEPS` | 50 | Hard cap on total steps (including replans) |
| `MAX_PARALLEL_STEPS` | 2 | Max concurrent step sessions in a parallel group |
| `MAX_CONCURRENT_TASKS` | 3 | Max simultaneous task runs |
| `PARALLEL_STAGGER_SECS` | 3.0 | Delay between parallel cold starts |
| `LOAD_CHECK_THRESHOLD` | 0.85 | System load / cpu_count ratio to pause spawning |
| `LOAD_CHECK_MAX_WAIT` | 60 | Max seconds to wait for load to drop |
| `CONTEXT_COMPACT_PCT` | 80.0 | Compact threshold |
| `TEST_TIMEOUT` | 5400 | 90 min for test command |
| `STALL_TIMEOUT` | 3600 | 60 min with no activity → warn |
| `STALL_CANCEL_TIMEOUT` | 7200 | 2h with no activity → reset session |
| `DEFAULT_TOKEN_BUDGET` | 0 | 0 = unlimited |
| `PROGRESS_FILE` | `TASK_PROGRESS.md` | Written next to spec file |
| `SESSION_PREFIX` | `taskrunner` | Session key prefix |
| `RUNS_FILE` | `runs.json` | Persisted runs file |

Note: constants were renamed from `_MAX_RETRIES` → `MAX_RETRIES` etc. when moved
to `task_models.py` (no longer private to a single file).

## Notifications

All notifications prefixed with `[spec_name]` via `_notify(title, body, run=run)`.

| Event | Title | Body |
|-------|-------|------|
| Task started | 🚀 Task started | Spec name |
| Plan ready | 📋 Plan ready | Step list |
| Step passed | ✅ Step N/M | Title + result preview (500 chars) |
| Step failed | ❌ Step N/M failed | Title + error |
| Task completed | ✅ Task completed | Steps passed/failed, elapsed, tokens, work dir, full step list |
| Task error | ❌ Task error | Exception message |
| Stall warning | ⚠️ Task may be stalled | Minutes since last activity |
| Session reset | 🔧 Watchdog: cancelling stalled step | Minutes + resetting |
| Process died | 💀 Step N: process died | Recovery count |
| Lesson learned | 📝 Lesson learned | Rule text |
| Replan started | 🔄 Re-planning (N/2) | Failed step title + error |
| Revised plan | 📋 Revised plan | New step count + titles |
| Possible loop | ⚠️ Possible loop | Same error repeated Nx |
| Token budget | 💰 Token budget exceeded | Usage vs budget |
| Branch ready | 🌿 Branch: `name` | Shown in completion summary |

## Git Coordination

Each task runs on an isolated git branch via `git_coord.py`:

- **Existing repo**: `git worktree add` creates isolated working directory; user's checkout untouched
- **No repo**: `git init` in work_dir, then `git checkout -b kiroclaw/task/{task_id}`
- **Per-step commits**: `git add -A && git commit` after each passed step
- **Revert on failure**: `git reset --hard HEAD~1` when review fails (before retry)
- **State summary**: `git log --oneline` + `git diff --stat` injected into step prompts
- **Review diff**: `git diff HEAD~1` fed to independent review session
- **Finalize**: worktree cleaned up on task completion

Git init failure is non-fatal — task continues without git coordination.

## Cycle Detection

Tracks consecutive identical errors within `_execute_step`:

- 2nd identical error → ⚠️ warning notification
- 3rd identical error → step FAILED with "Loop detected" message
- Different error resets the counter
- `AcpProcessDied` (process crash) does NOT count — crashes don't pollute the error tracker

Applies to both exception errors and test failure outputs.

## Step Prompt Context

`_build_step_prompt` assembles context for each step (async):

1. **Role prompt** — autonomous execution agent identity + git branch awareness
2. **Git context** (if available) — `git_coord.get_state_summary()` (log + diff stat)
3. **Working memory fallback** (if no git) — text-based file/decision tracking
4. **Completed steps** — titles of passed steps
5. **Current step** — title, description, spec content
6. **Retry context** (if attempt > 1) — previous error message

## Self-Review

Independent review using separate session (`taskrunner:{task_id}:review`):

- Step set to `REVIEWING` status before review starts (visible in UI as 🔍)
- Only set to `PASSED` after review succeeds
- Reads actual `git diff HEAD~1` (not LLM's self-report)
- Separate session = no bias from having written the code
- Falls back to generic review prompt when no git diff available
- Review failure → revert commit → retry step → re-commit on success
- Review exceptions are non-fatal (returns True to avoid blocking)

## Tool Approval

Two-layer approval during step execution:

1. **Hook rules** checked first: `hooks.on_tool_call(title)` → DENY/ALLOW
2. **Interactive approval** via `on_tool_approval` callback (if set):
   - In gateway: routes through `_interactive_approval` → checks YOLO/Trust mode → `DashboardState.request_approval()` → WS broadcast → user clicks ✅/🚫
   - 2-hour timeout on interactive approval (auto-reject)
   - YOLO mode: auto-approves all
   - Trust mode: auto-approves when all slots trusted

## Watchdog

Activity-aware stall detection. Tracks `run.last_step_time` which is bumped on:
- Every text chunk during LLM streaming
- Every tool approval (auto or interactive)
- Step/approval gate entry
- AcpProcessDied recovery

Only fires when there is truly ZERO activity for the stall period.

- 60 min no activity → ⚠️ warning notification
- 2h no activity → 🔧 session reset → `AcpProcessDied` → recovery retry
- Resets the current step session: `taskrunner:{task_id}:step{current_step}`
- Stall flag cleared on recovery (can fire again if retry also stalls)
- `last_step_time` reset after recovery (fresh window for retry)
- Watchdog cancelled in `finally` block when task finishes
- **Cannot delete or cancel a task** — only resets ACP session

## Session Management

- Each step: `taskrunner:{task_id}:step{N}` — fresh session per step, reset after completion (owned by `task_executor.py`)
- Decomposition: `taskrunner:{task_id}:decompose` (throwaway, reset in finally) (owned by `task_planner.py`)
  - Returns `{"steps": [...], "acceptance_criteria": [...]}` — criteria shown in final acceptance step
  - Backward compatible with plain JSON arrays (no criteria → step-title fallback)
- Self-review: `taskrunner:{task_id}:review` (separate session, reset in finally) (owned by `task_executor.py`)
- Acceptance review: `taskrunner:{task_id}:acceptance` (owned by `task_reviewer.py`)
- Context compaction at ≥80%, session reset if still ≥95% after compact

Every step gets `is_new=True` on its first message, which triggers full `ContextBuilder`
injection: user preferences, active projects, recent history, semantic memory, lessons,
episodic memory (queried by step prompt text), and triggered skills. Same ~15k budget
as a normal chat session.

## Dynamic Refine

The "✨ Compose" tab uses a single-shot LLM call to rewrite the user's rough
natural language input into a structured task specification. No tools, no file
reading, no clarifying questions — just a fast spec rewrite.

1. User describes task in natural language
2. LLM rewrites it into a structured spec (Goal / Requirements / Acceptance Criteria)
3. Spec appears in editable textarea — user can edit before clicking "▶ Run This Spec"

**No tools allowed during refine** — all tool calls are rejected. The refiner's
only job is to produce a better-written spec from the user's input.

**WS events**: `refine` type with `{status, text, error}` fields.

## Dashboard UI (Projects Page)

Left/right split layout: 260px sidebar + detail/compose area.

- **Sidebar** (visible when runs exist): compact project cards with status icon, name, progress bar, cancel/delete buttons. "＋ New Project" button at top.
- **Compose area** (no project selected): ✨ Compose | 📄 From Spec tabs, shared `AgentSelector`, `ProjectAnimation` shown in empty state
- **Compose mode**: textarea + "✨ Refine into Spec" + "📋 Plan" buttons, `PlanningBanner` with cancel
- **From Spec mode**: textarea + file upload (`<input type="file">`) + "▶ Run" + "📋 Plan" buttons
- **Project detail** (`ProjectDetailPage`): Idea/Tasks tab bar with 🎮 button (right-aligned)
  - **Idea tab**: read-only spec content + "✏️ Edit in Chat" button
  - **Tasks tab**: DAG/Phased view toggle with `DagView` and `PhasedView` components
  - **🎮 button**: opens modal with pixel-art office animation (`PixelCanvasWidget` + `PixelCanvas`). 7 character sprites animate based on task status (typing/looking/celebrate). Badge shows active agent count.
- **Action buttons**: Execute/Chat/Discard (planned), ■ Cancel (running), ↻ Restart/⏰ Schedule (completed/failed)
- **`SubAgentActivity`**: shown below running projects — live subagent table with status pills (Running/Done/Failed)
- **WS-driven updates**: `push_refresh("taskrunner")` on every notification, 3s auto-refresh polling
