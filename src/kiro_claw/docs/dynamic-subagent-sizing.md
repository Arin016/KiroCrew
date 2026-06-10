# Dynamic Sub-Agent Max Count

By default KiroClaw caps concurrent sub-agents at a fixed `agent.max_subagents`
(3). On a large host that wastes capacity; on a tiny host a fixed number can be
wrong the other way. **Dynamic sizing** computes a sensible cap at gateway
startup from the host's actual memory and CPU, plus a per-agent cost KiroClaw
*learns* from past runs.

## Enabling It

Set the cap to the auto sentinel:

```
kiroclaw config set agent.max_subagents 0
```

- `agent.max_subagents > 0` — explicit cap (unchanged legacy behavior).
- `agent.max_subagents = 0` — **auto**: compute the cap at startup.

The cap is computed once per gateway start. Restart to recompute (e.g. after the
host's resources change).

## How the Cap Is Computed

```
mem_term = floor( (effective_available_GB * (1 - buffer%) - pool_reserve) / mem_cost )
cpu_term = floor( (cpu_count * (1 - buffer%)) / cpu_cost )
cap      = clamp( min(mem_term, cpu_term), 3, hard_cap )
```

- **Memory term** — how many agents fit in available RAM after reserving a
  buffer for the OS and other processes. `effective_available` is
  `min(MemAvailable, cgroup headroom)` so a memory-capped container is respected.
- **CPU term** — how many fit in the core budget, using a measured per-agent
  CPU cost (agents are mostly I/O-bound, so this is generous).
- **`min(...)`** — the tighter of memory/CPU wins.
- **Floor of 3** — never drops below the legacy default, so enabling auto can't
  regress a small host. The per-spawn memory gate (`agent.spawn_min_memory_gb`)
  still refuses individual spawns under real memory pressure.
- **`hard_cap`** — an absolute ceiling (see "Why a hard cap" below).

## Learned Per-Agent Cost

KiroClaw doesn't hard-code how much an agent costs — it measures it:

- While an agent runs, the reaper loop periodically samples its process-tree
  RSS (memory) and CPU, keeping the **high-water** mark for that run (a single
  reading at exit would miss a mid-run peak that has already declined).
- At exit, one sample `{agent, mem_gb, cpu_cores, ts}` is appended to
  `~/.kiroclaw/subagents/cost_samples.jsonl`.
- At the next startup, KiroClaw takes the **p90 of the last N samples per
  agent name** (robust to the occasional outlier run), then the worst case
  across agent types, as the divisor.

The longer the gateway runs, the more accurate the learned cost becomes. The
sample log is bounded to the last N records per agent (FIFO compaction at
startup and periodically at runtime), so it never grows without limit. Before
enough samples accumulate, a conservative fallback is used
(`agent.subagent_cost_gb`, `agent.subagent_cpu_cost_cores`).

## Why a Hard Cap

The formula sizes for **local** resources, but every sub-agent calls the same
upstream LLM provider under one account. The provider's concurrency / rate
limit is frequently the *real* bottleneck — a host that fits 48 agents in RAM
may only get useful throughput from a handful before requests start queueing.

`agent.subagent_auto_max` (default **16**) is an honest ceiling for that
unmodeled limit. On a big host the hard cap binds; on a small host memory or
CPU binds below it. If you've confirmed your provider serves more concurrency,
raise it. KiroClaw does **not** yet measure provider saturation — that's a
deliberate v1 simplification we may revisit.

## Configuration

| Key | Default | Effect |
|-----|---------|--------|
| `agent.max_subagents` | `3` | `0` = auto-size; `>0` = explicit cap |
| `agent.subagent_mem_buffer_pct` | `20` | % of memory/CPU reserved for the OS and other processes |
| `agent.subagent_cost_gb` | `0.5` | First-boot memory-cost fallback (GB/agent) until learned |
| `agent.subagent_cpu_cost_cores` | `1.0` | First-boot CPU-cost fallback (cores/agent) until learned |
| `agent.subagent_auto_max` | `16` | Absolute ceiling on the computed cap |
| `agent.spawn_min_memory_gb` | `4.0` | Per-spawn admission gate (separate runtime guard, refuses a spawn when free memory is low) |
| `session.pool_size` | `0` | Warm-pool size; reserved in the memory term when > 0 |

The cap interacts with `spawn_min_memory_gb` but does not replace it: the cap is
a startup count limit, while `spawn_min_memory_gb` is a real-time per-spawn
memory floor. They are independent guards.

## Notes

- Stdlib only — reads `/proc/meminfo`, `/proc/<pid>/stat`, and cgroup limits.
  No new dependencies.
- On non-Linux hosts the readers fail open (the cap falls back to the configured
  value), so behavior is unchanged there.
- Design rationale and worked examples:
  `~/.kiroclaw/workspace/dynamic-subagent-sizing.md`.
