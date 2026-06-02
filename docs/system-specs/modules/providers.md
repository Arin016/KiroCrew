## LLM Provider Abstraction

Decouples KiroClaw from any specific LLM backend. Supports multiple providers via a common interface.

### Architecture

```
┌─────────────────────────────────────────────┐
│  Consumers (handler, gateway, cli, session) │
│  Use LLMProvider interface only             │
└──────────────────┬──────────────────────────┘
                   │
         ┌─────────┴─────────┐
         │   LLMProvider ABC  │
         │   providers/base   │
         └─────────┬─────────┘
          ┌────────┼────────────┐
          │        │            │
 ┌────────┴──┐ ┌───┴──────────┐ ┌──────────────────┐
 │AcpProvider│ │BedrockProvider│ │ClaudeCodeProvider │
 │ acp.py    │ │ bedrock.py    │ │ claude_code.py    │
 │ kiro-cli  │ │ (boto3 API)  │ │(LEGACY/DEPRECATED) │
 │ OR claude │ │              │ │                    │
 │ -agent-acp│ │              │ │                    │
 └───────────┘ └──────────────┘ └──────────────────┘
```

**Note:** `ClaudeCodeProvider` (subprocess-based `claude` CLI wrapper) has been replaced by `AcpProvider(acp_backend="claude")` which uses the `claude-agent-acp` npm package for full ACP protocol parity. The unified `AcpProvider` handles both kiro-cli and claude backends via the `acp_backend` parameter.

### LLMProvider ABC (`providers/base.py`)

```python
class LLMProvider(ABC):
    async def start() -> None
    async def shutdown() -> None
    async def stream(message: str) -> AsyncIterator[LLMEvent]
    async def approve_tool(request_id) -> None
    async def reject_tool(request_id) -> None
    def context_usage_pct() -> float
    # Optional (have defaults):
    async def stream_command(command: str) -> AsyncIterator[LLMEvent]
    async def compact(context: str = "") -> None
    async def wait_for_compaction(timeout: float = 120.0) -> dict
    async def cancel(*, wait_ack_timeout: float = 0.0) -> CancelOutcome
    def is_alive() -> bool
    def touch_activity() -> None
```

### LLMEvent (`providers/base.py`)

Provider-agnostic event dataclass (aliased from `AcpEvent`):

| Kind | Description |
|------|-------------|
| `text_chunk` | Text output from agent |
| `thinking_chunk` | Extended thinking (Claude 3.7+) |
| `tool_call` | Tool invocation |
| `tool_result` | Tool output |
| `permission_request` | Tool approval request (ACP only) |
| `complete` | End of turn |
| `compaction_status` | Compaction result |
| `clear_status` | Clear display |
| `agent_switched` | Agent mode changed |
| `mcp_oauth_request` | MCP server needs OAuth (has `server_name`, `oauth_url`) |
| `mcp_server_initialized` | MCP server ready after OAuth (has `server_name`) |
| `mcp_server_init_failure` | MCP server OAuth/init failed (has `server_name`, `text`) |

### AcpProvider (`providers/acp.py`)

Unified provider for both kiro-cli and claude-agent-acp backends. Spawns a long-lived subprocess using JSON-RPC 2.0 over stdio.

**Backend selection** via `AcpProvider(acp_backend=...)`:
- `""` (default) — spawns `kiro-cli acp --agent <name>`
- `"claude"` (`ACP_BACKEND_CLAUDE`) — spawns `claude-agent-acp` (the `claude-agent-acp` npm package provides full ACP protocol parity, replacing the legacy `ClaudeCodeProvider`)

**Key APIs:**
- `start()` → `AcpClient.ensure_ready()` (spawns process, handshake, session/new)
- `stream()` → maps events from `stream_events()`
- `stream_command()` → native slash command execution
- `approve_tool()`/`reject_tool()` → JSON-RPC response
- `context_usage_pct()` → reads `last_prompt_stats.context_pct`
- `context_window_tokens()` → reads `last_prompt_stats.context_window_tokens` (the real served window from `usage_update.size`, 0 if unknown). Used by the dashboard token text instead of re-deriving the window from the model id.
- `compact()` → sends `/compact` via `send_command()`
- `cancel()` → sends `session/cancel` notification
- `supports_effort()` / `change_effort(level)` / `clear_effort()` → reasoning-effort control (see below)
- `is_alive()` → `AcpClient.is_responsive()` (600s stale threshold)
- `is_process_alive()` → OS-level process check

**Reasoning effort** (Opus/Sonnet only, both backends — shared vocabulary in `effort.py`: levels `low|medium|high|xhigh|max`, capability via `model_supports_effort`, resolution via `resolve_effort_for_model` with priority slot-override > workspace default > None):
- **kiro backend** — applied via a workspace `cli.json` overlay at `<work_dir>/.kiro/settings/cli.json` → `chat.modelDefaults.<model>.output_config.effort`, written before every spawn (`_write_cli_overlay`) and recovered on init (`_read_cli_overlay`) for server-restart resilience. Live change pushes `/effort` with the TuiCommand args form (`send_command(args={"level": …})`).
- **claude backend** — `CLAUDE_CODE_EFFORT_LEVEL` is NOT read by claude-agent-acp, so effort is applied live via `session/set_config_option` (configId `effort`): once after session-ready (`_apply_initial_effort`) and on each change. The adapter validates the level against the model's `supportedEffortLevels` and throws on a mismatch.
- The factory threads `reasoning_effort_override` → `effort_per_model[current_model]`; the dashboard handler routes through `change_effort`/`clear_effort` and only resets the session when there is no live provider. Non-effort-capable models persist the slot value without a live apply or reset.

**Claude backend specifics:**
- `_is_claude` property drives backend-specific handshake logic
- `protocolVersion`: integer `1` (vs kiro-cli's `"2025-08-22"` date string)
- Skips `session/set_mode`; uses `session/set_config_option` (configId `model`) instead of `session/set_model`
- Pre-spawn writes `settings.local.json` with `defaultMode: default` so every tool decision routes through KiroClaw's `session/request_permission` pipeline (the four-tier approve/trust_reads/trust/yolo protocol)
- Context usage tracked from `usage_update` session events
- Provider switch detection distinguishes kiro-acp from claude-acp sessions
- **MCP servers** are passed in the `session/new`/`session/load` `mcpServers` param (the adapter reads no config file). Built per spawn by `_claude_acp_mcp_servers()` from `~/.claude/agents/kiroclaw.mcp.json`; kiroclaw-core/cron forced to stdio and always present. See acp-client.md.
- **Model list:** the `session/new`/`session/load` response carries `models.availableModels` (the real versioned Claude set). `AcpClient._capture_available_models` records it; `AcpProvider.available_models()` exposes it. `/api/models` (claude_code branch, `dashboard/handlers/agents.py`) merges this advertised list with hardcoded Opus 4.8 entries (`global.anthropic.claude-opus-4-8[1m]` / `[200K]` — enabled on Bedrock and the default `cc_model`, but not yet advertised by claude-agent-acp) and force-includes the configured default, deduped by model id. Falls back to the static `_CC_VALID_MODELS` catalog before any session initializes. The frontend `ClaudeCodeAdapter.fetchAvailableModels` keeps `model_name` as the switchable id and folds `display_name` into the description.
- **Resume guard:** `session/load` (resume) is only attempted when the prior session transcript exists on disk — kiro at `~/.kiro/sessions/cli/<sid>.json`, claude at `<cc-config-root>/projects/<encoded-cwd>/<sid>.jsonl` where `<cc-config-root>` = `cc_agent.cc_config_root()` (the isolated `<config_dir>/cc-config` under isolation, else `~/.claude`). A stale persisted sid with no transcript falls back to `session/new` for BOTH backends, preventing a fresh conversation from replaying old turns (which inflated base context).
- **Config isolation (CLAUDE_CONFIG_DIR):** the spawned `claude-agent-acp` subprocess defaults to loading the user's global `~/.claude` (all `enabledPlugins` → ~17× duplicated `builder-mcp` blocks + ~80 agents + ~100 skills = ~30–55k tokens of base-prompt bloat). KiroClaw isolates it by injecting `CLAUDE_CONFIG_DIR=<config_dir>/cc-config` into the subprocess env (`config/loader._claude_code` cc_env) and seeding `<cc-config>/settings.json` (`cc_agent.seed_isolated_cc_config`) as a copy of `~/.claude/settings.json` with `enabledPlugins`/`extraKnownMarketplaces`/`enabledMcpjsonServers`/cosmetic keys **stripped** but `awsCredentialExport` (Bedrock cred refresh), `availableModels`+`model` (1M window), `env`, and `permissions` (minus `defaultMode`) **kept**. A single resolver `cc_agent.cc_config_root()` drives env-injection, the resume guard, and cleanup so they target the same dir. The host-side deny gate (`canUseTool`→`hooks.on_tool_call`→`reject_tool`) is independent of settings and survives. Guard: `KIROCLAW_CC_ISOLATE=0` disables isolation (falls back to `~/.claude` everywhere). NOT `settingSources:[]` — that dropped the `user` tier the native CLI reads for `awsCredentialExport`, breaking Bedrock auth.
- **Per-agent model:** because the claude backend passes neither `--agent` nor `set_mode`, a non-default agent (e.g. `kiroclaw-lite` for cheap background title/compaction/heartbeat work) cannot pick up its own model the way kiro-cli does. The `_claude_code` factory resolves it via `KiroClawConfig._resolve_agent_cc_model(agent)` (reads the agent's kiro json `cc_model`, else `model`), threading it as the provider model. The default `kiroclaw` agent keeps the global `cc_model`; a `model_override` (slot model) always wins. `kiroclaw-lite` declares `cc_model: claude-sonnet-4.6`. The resolved id is translated through `_CC_MODEL_ALIASES` (`claude-opus-4.6`/`claude-sonnet-4.6` → `opus`/`sonnet`): a kiro dotted id is NOT a valid claude-agent-acp model, so sent verbatim to `session/set_config_option("model", …)` the backend rejects the whole session with `-32603 Invalid value for config option model`. This matters because the AIM-managed `kiroclaw-lite` agent json ships only `model: claude-opus-4.6` (no `cc_model`); already-CC-valid ids (full `global.anthropic.…` profiles) are not alias keys and pass through unchanged.
- **Working dir:** `AcpProvider.cwd` (and `ClaudeCodeProvider.cwd`) override the `LLMProvider` ABC default so `session_map` persists the real workspace path. AcpProvider's work_dir lives on the inner client (`_client._work_dir`), so the prior `getattr(provider, "_work_dir", "")` persisted `""` for all ACP sessions — `provider.cwd` fixes resume-cwd-override for both backends.
- **Cleanup:** orphaned/tombstoned CC subagent transcripts are cleaned via `SubagentManager._is_cc_provider`, which now recognizes the `AcpProvider` claude backend (not just the dead `ClaudeCodeProvider`) and records `provider="claude_code"` + a derived `cwd` so `_cleanup_session_files_sync` targets the CC config root (`cc_agent.cc_config_root()` — the isolated `<config_dir>/cc-config` under isolation, else `~/.claude`) instead of the wrong `~/.kiro` path. `_cc_session_paths`/`_cleanup_cc_session` take an optional `config_root` defaulting to the resolver, so the reaper (which runs post-restart with only `cwd/sid/provider`) recomputes the same deterministic isolated root with no persisted state.

### ClaudeCodeProvider (`providers/claude_code.py`) — LEGACY/DEPRECATED

**Replaced by `AcpProvider(acp_backend="claude")`** which uses the `claude-agent-acp` npm package for full ACP protocol parity. The claude-agent-acp adapter communicates over the same JSON-RPC 2.0 protocol as kiro-cli, eliminating the need for a separate NDJSON-based provider.

The file still exists at `providers/claude_code.py` (1135 lines) and is still imported for type-checking, but the config factory routes `"claude_code"` to `AcpProvider(acp_backend="claude")` for new sessions. The old `ClaudeCodeProvider` wrapped the `claude` CLI directly with bidirectional `stream-json` protocol — all Claude model access now routes through the unified AcpProvider with `acp_backend="claude"`.

**Migration summary:**
- `provider: "claude_code"` in config → resolved to `AcpProvider(acp_backend="claude")`
- Permission bypass: `settings.local.json` with `defaultMode: bypassPermissions` (written pre-spawn)
- Context management: provider-agnostic session replay from `conversation_log` (80K char budget)
- MCP servers: same registration path as kiro-cli backend (via agent config)
- Session resume: uses ACP `session/load` (same as kiro-cli backend)

### BedrockProvider (`providers/bedrock.py`)

Direct Amazon Bedrock `converse_stream()` API for simple Q&A and advisory tasks:
- **Text-only — no tool execution by design.**
- Conversation history managed in-memory (max 50 turns)
- Context usage estimated from input tokens / model-specific window

### Config (`config/loader.py`)

```json
{
  "agent": {
    "provider": "acp | bedrock",
    "model": "claude-opus-4.6",
    "bedrock_model_id": "anthropic.claude-sonnet-4-20250514",
    "bedrock_region": "us-west-2"
  }
}
```

- `create_provider_factory()` returns a `Callable` that creates the configured provider
- Default: `"acp"` (kiro-cli)
- `"claude_code"` is accepted for backward compatibility but resolves to `AcpProvider(acp_backend="claude")`

### MCP Server Registration

**ACP path (both backends):** MCP servers passed directly in `session/new` params. The claude-agent-acp backend uses the same mechanism as kiro-cli.

**CC artifacts (still generated):** `generate_mcp_json()` and `install_cc_agent_config()` still render `~/.claude/agents/kiroclaw.md` and `kiroclaw.mcp.json` regardless of active provider, so Claude Code IDE integrations continue to work.

Both paths get the same servers via `build_agent_config()`:
- `builder-mcp` (with `--skill-paths` for AIM skills, `--include-tool-tags` and `--exclude-tools` injection)
- `kiroclaw-core`, `kiroclaw-cron`
- `arcc-governance`
- User-configured servers

### SessionManager (`session.py`)

- Provider-agnostic via factory
- Calls `repair_agent_configs()` on gateway startup and periodically
- context_info() reports model/agent per backend
- Resume: calls `set_resume_session_id()` before `start()`

### Subagent Approval Mode Inheritance (`subagent.py`)

Subagents inherit the global `approval_mode=auto` config as a final fallback when:
1. No parent session key exists (spawned independently), OR
2. Parent session key exists but the session is no longer in the store (garbage-collected)

If the parent session is alive but returned no policy, deny-by-default applies — the session is intentionally non-auto. This ensures subagents spawned from dashboard sessions still get auto-approval even if the parent session is GC'd before the subagent executes.

### Installation (claude-agent-acp)

For the claude backend (`AcpProvider(acp_backend="claude")`):

```bash
npm i -g @agentclientprotocol/claude-agent-acp
```

Or set `CLAUDE_AGENT_ACP_BIN` to point to the binary. Resolution uses mise, nvm, fnm, volta, or npm global installs automatically (see `_resolve_claude_acp_bin` in `acp/client.py`).

### Trade-offs

| | ACP (kiro-cli) | ACP (claude-agent-acp) | Bedrock (direct) |
|---|---|---|---|
| Tool execution | Full MCP tools | Full MCP tools | Text-only |
| Latency | Low (persistent process) | Low (persistent process) | Lowest (API) |
| Dependencies | kiro-cli binary | claude-agent-acp npm pkg | boto3 + AWS creds |
| Models | kiro-cli managed | set_config_option | Any Bedrock model |
| Streaming | Full events | Full events | Text only |
| Thinking blocks | agent_message_chunk type=thinking | agent_thought_chunk (dedicated) | No |
| Permission model | Interactive approve/reject | bypassPermissions (KiroClaw hooks enforce) | N/A |
| Context management | KiroClaw controls | KiroClaw controls (80K replay budget) | KiroClaw controls |
| MCP servers | session/new params | session/new params | N/A |
| AIM skills | builder-mcp --skill-paths | builder-mcp --skill-paths | N/A |
| Warm pool | Yes | Yes | No |
| Session resume | session/load | session/load | N/A |
