# Config Module

Last Updated: 2026-06-12 (agent_model_state.json sidecar: model_managed/cc_model moved out of kiro agent specs so kiro-cli deny_unknown_fields no longer drops KiroClaw agents)

## Overview

The config module (`kiro_claw/config/loader.py`) loads runtime configuration from `~/.kiroclaw/config.json` using stdlib dataclasses with sensible defaults.

## Workspace Root

`workspace_root()` returns the base directory for all LLM working directories (kiro-cli cwd, task runner output, etc.):

Resolution order:
1. `KIROCLAW_WORKSPACE` env var — used as-is (no `kiroclaw-workspace` subdirectory appended)
2. Saved path in `~/.kiroclaw/workspace_dir` (written by `kiroclaw setup`; re-running setup preserves the existing value as the prompt default)
3. Platform default:

| Platform | Path |
|----------|------|
| macOS | `/Volumes/workplace/kiroclaw-workspace` (falls back to `~/workplace/kiroclaw-workspace` if `/Volumes/workplace` doesn't exist) |
| Linux | `~/workplace/kiroclaw-workspace` |

Each session/task gets an isolated subdirectory under this root via `_session_work_dir(key)`:
- Chat sessions: `kiroclaw-workspace/cli_chat`, `kiroclaw-workspace/{thread_ts}`
- Background: `kiroclaw-workspace/_bg`
- Cron: `kiroclaw-workspace/cron_{job_id}`
- TaskRunner: `kiroclaw-workspace/taskrunner_main`
- Background session: `kiroclaw-workspace/_bg`

The parent directory is created on first call if it doesn't exist.

## Project Directory Resolution

`KIROCLAW_PROJECT_DIR` env var controls where agent config and skills are loaded from:

1. Env var `KIROCLAW_PROJECT_DIR` (if set and valid)
2. CWD walk-up — CLI walks up from CWD looking for `skills/` + `src/kiro_claw/` (the `agents/` dir was removed in commit bbbc1f6e when agent config moved into `src/kiro_claw/config/`)
3. Saved path in `~/.kiroclaw/project_dir` (written by `kiroclaw setup`)
4. Bundled fallback — `config/defaults.json` and `builtin_skills/` inside the package

The CLI (`cli.py:main()`) auto-detects and sets the env var at startup.

## Config Overlay (config.local.json)

User overrides can be placed in `~/.kiroclaw/config.local.json`. This file is
deep-merged on top of `config.json` at load time and is never touched by
`kiroclaw setup` or toolbox upgrades.

Resolution order:
1. Load `config.json` (managed by KiroClaw, may be regenerated on upgrade)
2. Deep-merge `config.local.json` on top (user-owned, never touched by setup/migration)
3. Return merged result

### CLI Usage

```bash
# Save a setting to config.local.json (persists across upgrades):
kiroclaw config set --local agent.yolo true

# Save to config.json (may be overwritten on upgrade):
kiroclaw config set agent.yolo true
```

### `config_local_path() -> Path`
Returns `~/.kiroclaw/config.local.json` (or `$KIROCLAW_HOME/config.local.json`).

### `_deep_merge(base: dict, overlay: dict) -> dict`
Recursively merges overlay into base. Dict values merge recursively; all other
types in overlay replace base values.

## APIs

### `KiroClawConfig.load() -> KiroClawConfig`
Loads config from disk. Merges `config.local.json` overlay if present.
Returns defaults if file is missing or invalid.

**Hot-path cache.** `load()` is called per message / per request on several hot
paths. The expensive work — reading `config.json` (+ `config.local.json`),
`json.loads`, `_deep_merge`, and the full `jsonschema.validate` — is cached as
the validated, merged `data` dict, keyed on a fingerprint of both files
(`st_mtime_ns`, `st_size`, `st_mode`). On a cache hit, `load()` still builds
**fresh dataclasses from a deep copy**, so the many callers that mutate the
returned config in place (settings handlers, the write-back migration) never
corrupt the shared cache. The cache is mtime-keyed (not a blind TTL), so a
runtime edit is reflected on the next `load()`; `save()` also invalidates it
eagerly via `_invalidate_config_cache()`. The defaults-only path (neither file
present) is not cached.

### `KiroClawConfig._resolve_agent_model() -> str`
Reads model from installed agent config (`~/.kiro/agents/kiroclaw.json`),
falling back to project-level `agents/defaults.json`, then `"auto"`.

### `KiroClawConfig.create_provider_factory() -> Callable`
Returns a factory for LLMProvider instances. Resolves `"auto"` model
before creating the provider.

### `KiroClawConfig.to_dict() -> dict`
Serializes config to the JSON structure used by `config.json`. Uses `_configured_port`
(the file value) instead of `dashboard_port` (which may be overridden by `KIROCLAW_PORT`
env var) to avoid clobbering the saved port on write-back.

### `KiroClawConfig.save() -> None`
Writes current config to `~/.kiroclaw/config.json` via `to_dict()`. Invalidates
the `load()` validated-data cache so the next load reflects the write immediately.

### `config_dir() -> Path`
Returns `~/.kiroclaw/`. Overridden by `KIROCLAW_HOME` env var (refuses system directories like `/`, `/usr`, `/System`, `/etc`).

### `config_path() -> Path`
Returns `~/.kiroclaw/config.json` (or `$KIROCLAW_HOME/config.json` if overridden).

### Agent Bookkeeping Sidecar (`agent_model_state.json`)

KiroClaw tracks two pieces of per-agent state that are **not** part of the
kiro-cli agent schema: `model_managed` (whether an agent's `model` tracks the
shipped default or is a frozen user pick) and `cc_model` (a per-agent Claude
Code model). kiro-cli validates `~/.kiro/agents/*.json` with serde
`deny_unknown_fields` and rejects the *entire* spec on any unknown key, then
silently falls back to the default agent (`--agent <name>` resolves to default
with only a stderr "no agent with name X found" line). To keep every spec
schema-valid, this state lives in a KiroClaw-owned sidecar
`~/.kiroclaw/agent_model_state.json` (honoring `KIROCLAW_HOME`), keyed by agent
name:

```json
{
  "kiroclaw":           {"model_managed": true},
  "kiroclaw-heartbeat": {"cc_model": "claude-sonnet-4.6"}
}
```

- Read/written via `kiro_claw/agent_state.py` (atomic, lock-guarded near-leaf
  module: stdlib + `config.paths` + `atomic_write` only).
- `build_agent_config()` is pure (writes no spec key); `rebuild_agent_config()`
  seeds managed-state on a fresh/clean install (never clobbering a frozen pick).
- `_refresh_dynamic_fields()` sources managed-state from the sidecar and strips
  any stray `model_managed`/`cc_model` from the spec (steady-state self-heal).
- `migrate_agent_specs()` runs at startup (top of `rebuild_agent_config`): lifts
  the keys out of every `~/.kiro/agents/*.json` into the sidecar and removes
  them (idempotent), fixing installs polluted by older builds.
- The dashboard model PATCH writes the sidecar, never the spec; agent DELETE
  prunes the sidecar entry.

Note: KiroClaw is KiroACP (kiro-cli) only — the deleted `claude_code` provider
was the sole reader of spec `cc_model`, so `cc_model` is now dead config. The
lite/heartbeat installers still write it to the sidecar (harmless bookkeeping)
purely to keep the kiro spec schema-clean; nothing in the fork resolves it.

**Invariant:** `~/.kiro/agents/*.json` must contain only kiro-cli schema keys at
all times — after install, refresh, and any dashboard edit — or kiro-cli drops
the agent and silently falls back to default.

## Schema

```python
@dataclass
class AgentConfig:
    approval_mode: str = "auto"    # "auto" or "interactive"
    streaming: bool = True
    model: str = "auto"            # resolved from agent config
    provider: str = "acp"          # fixed to "acp" (kiro-cli) — the only provider
    sandbox: str = "auto"          # "auto" (namespace on Linux, seatbelt on macOS), "strict", or "off"
    enforce_denied_commands: str = "all"  # "all" or "kiroclaw"
    soft_stop_budget_secs: float = 10.0  # seconds to wait for cooperative cancel before hard kill [0.5, 60.0]
    yolo: bool = False             # permanent YOLO mode (skip tool approval); tracked via _yolo_from_config flag

@dataclass
class SessionConfig:
    timeout_secs: int = 1800       # 30 min idle timeout
    autocompact_pct: float = 90.0  # context usage % at which auto-compaction triggers (5-90)

@dataclass
class TaskRunnerConfig:
    max_parallel_steps: int = 2    # max concurrent step sessions in parallel groups

@dataclass
class MemoryConfig:
    history_idle_hours: float = 3.0  # consolidate history after N hours idle
    history_max_days: int = 365      # prune daily history files older than this

@dataclass
class ChannelConfig:
    activation: str = "mention"    # "always", "mention", "observe", or "off"
    agent: str = ""                # per-channel agent override (empty = use default)

@dataclass
class SttConfig:
    enabled: bool = True           # enabled by default; gated by whisper availability
    whisper_path: str = ""         # auto-detected if empty
    model: str = "turbo"           # turbo (~1.6 GB, 809M params, ~8x faster than large)
    device: str = "cpu"            # "cpu" or "cuda"
    timeout_secs: int = 300

@dataclass
class SecretaryConfig:
    enabled: bool = False
    user_id: str = ""
    watched_channels: list[str] = field(default_factory=list)
    poll_interval_seconds: int = 60
    style_rules: list[str] = field(default_factory=list)
    alert_keywords: list[str] = field(default_factory=list)
    alert_on_name_mention: bool = False

@dataclass
class KiroClawConfig:
    agent: AgentConfig
    session: SessionConfig
    taskrunner: TaskRunnerConfig
    memory: MemoryConfig
    stt: SttConfig
    secretary: SecretaryConfig
    hooks_data: dict               # raw hooks from config.json
    dashboard_url: str = ""        # e.g. "http://my-host.corp.amazon.com:8080"
    auto_update: bool = True
    snapshot_dir: str = ""         # snapshot output dir (default ~/.kiroclaw/snapshots)
    slack_channels: dict[str, ChannelConfig]  # per-channel config keyed by channel ID
    slack_dm_activation: str = "always"       # activation mode for DMs (D-prefix channels)
```

### `ChannelConfig.from_dict(data: dict) -> ChannelConfig`
Parses a channel config entry from JSON. Invalid activation values fall back to `"mention"`.

### `KiroClawConfig.channel_config(channel_id: str) -> ChannelConfig`
Returns the effective config for a channel:
1. Explicit entry in `slack_channels` → returned as-is
2. DM channel (`D`-prefix) → `ChannelConfig(activation=slack_dm_activation)`
3. Group/public channel (`C`/`G`-prefix) → `ChannelConfig(activation="mention")`

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `KIROCLAW_HOME` | Override config/data directory | `~/.kiroclaw` |
| `KIROCLAW_PORT` | Override dashboard port (dev mode — run dev + prod side by side) | `8765` |
| `KIROCLAW_WORKSPACE` | Override workspace root directory | Platform-dependent |
| `KIROCLAW_PROJECT_DIR` | Override agent config/skills directory | Auto-detected |
```

## Config File Format

```json
{
  "agent": {
    "approval_mode": "auto",
    "streaming": true,
    "provider": "acp"
  },
  "session": {
    "timeout_secs": 1800
  },
  "taskrunner": {
    "max_parallel_steps": 2
  },
  "memory": {
    "history_idle_hours": 3.0,
    "history_max_days": 365
  },
  "hooks": {},
  "slack": {
    "command": "kiroclaw",
    "allowed_users": [],
    "tracking_channels": [],
    "dm_activation": "always",
    "channels": {
      "C0123ONCALL": { "activation": "always", "agent": "ops" },
      "C0456REVIEWS": { "activation": "mention", "agent": "reviewer" },
      "C0789GENERAL": { "activation": "off" }
    }
  },
  "dashboard": {
    "url": "http://my-host.corp.amazon.com:8080"
  },
  "snapshot_dir": ""
}
```

The `dashboard.url` field controls where the dashboard is reachable. From it, the system derives the port to bind on, the bind address (`0.0.0.0` for non-loopback hosts, `127.0.0.1` otherwise), and the allowed origins for CSRF/WebSocket checks. When omitted, defaults to `localhost:8765`.

## Model Resolution Chain

When `agent.model` is `"auto"` (default):

1. `~/.kiro/agents/kiroclaw.json` → `model` field (installed agent config)
2. `$KIROCLAW_PROJECT_DIR/agents/defaults.json` → `model` field
3. Falls back to `"auto"` (passed through to provider)

## Error Handling

- Missing file → defaults
- Invalid JSON → defaults (warning logged)
- Missing fields → individual defaults
