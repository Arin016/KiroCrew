# Config Module

Last Updated: 2026-04-24 (snapshot_dir)

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
2. CWD walk-up — CLI walks up from CWD looking for `agents/` + `skills/`
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
Writes current config to `~/.kiroclaw/config.json` via `to_dict()`.

### `config_dir() -> Path`
Returns `~/.kiroclaw/`. Overridden by `KIROCLAW_HOME` env var (refuses system directories like `/`, `/usr`, `/System`, `/etc`).

### `config_path() -> Path`
Returns `~/.kiroclaw/config.json` (or `$KIROCLAW_HOME/config.json` if overridden).

## Schema

```python
@dataclass
class AgentConfig:
    approval_mode: str = "auto"    # "auto" or "interactive"
    streaming: bool = True
    model: str = "auto"            # resolved from agent config
    provider: str = "acp"          # "acp" or "bedrock"
    bedrock_model_id: str = "anthropic.claude-sonnet-4-20250514"
    bedrock_region: str = "us-west-2"
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
| `KIROCLAW_PORT` | Override dashboard port (dev mode — run dev + prod side by side) | `7777` |
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

The `dashboard.url` field controls where the dashboard is reachable. From it, the system derives the port to bind on, the bind address (`0.0.0.0` for non-loopback hosts, `127.0.0.1` otherwise), and the allowed origins for CSRF/WebSocket checks. When omitted, defaults to `localhost:7777`.

## Model Resolution Chain

When `agent.model` is `"auto"` (default):

1. `~/.kiro/agents/kiroclaw.json` → `model` field (installed agent config)
2. `$KIROCLAW_PROJECT_DIR/agents/defaults.json` → `model` field
3. Falls back to `"auto"` (passed through to provider)

## Error Handling

- Missing file → defaults
- Invalid JSON → defaults (warning logged)
- Missing fields → individual defaults
