# Claude Code Provider

Last Updated: 2026-05-10 (session resume fix, selective-mount sandbox, pool reset on switch)

## Overview

The Claude Code (CC) provider (`providers/claude_code.py`) enables using the `claude` CLI as an alternative LLM backend with full ACP parity. It supports two connection modes, OS-level sandboxing, session resume across restarts, and MCP tool access via `.mcp.json`.

## Connection Modes

### Per-Session (default)

Long-lived bidirectional communication via stream-json (NDJSON over stdin/stdout):

- Single `claude` process per session, kept alive across messages
- Messages sent as JSON lines on stdin, responses streamed back
- Process group management via `os.killpg()` for clean shutdown
- Crash recovery: detects process death, auto-restarts with `--resume`
- Cancel: sends SIGINT to process group (graceful) before SIGKILL (forced)

### Ephemeral

Fresh subprocess per message:

- Spawns `claude -p -` for each user message
- Simpler lifecycle, no state management
- Higher latency (~2s cold start per message)
- No session resume capability

Config: `agent.cc_connection_mode` = `"per_session"` | `"ephemeral"`

## Security: Selective-Mount Sandbox

The CC provider runs inside an OS-level sandbox that hides credential directories while preserving necessary access:

### Linux (unshare namespace)
- Bind-mount empty dirs over `~/.aws`, `~/.kube`, `~/.gnupg`, `~/.ssh`, `~/.npmrc`, `~/.pypirc`, `~/.netrc`, `~/.git-credentials`, `~/.env`
- Selectively restore `~/.aws/config` (needed for `credential_process` in Bedrock auth)
- 84 deny patterns for `--disallowedTools` (down from 162 after consolidation)

### macOS (Seatbelt)
- `sandbox-exec` profile denies file-read-data on credential paths
- Exception: `~/.aws` is NOT hidden on macOS because `credential_process` needs full access

### Protected Files
- `.kiroclaw/config.json`, `.kiroclaw/memory/`, `.env`, `.npmrc`, `.pypirc`, `.netrc`, `.git-credentials`

## Session Resume

CC sessions persist across gateway restarts:

1. `session_map.json` stores `{session_key: {provider: "claude_code", cwd: "/path", ...}}`
2. On resume: detects provider tag, spawns CC with `--resume` flag
3. Provider mismatch detection: if session was CC but current provider is ACP, skips resume
4. CWD persisted per-session to prevent "No conversation found" errors

## Context Usage Calculation

Per-turn context % = `(input_tokens + cache_creation_tokens + cache_read_tokens) / model_context_window`

Key fix: uses per-turn `assistant` event token counts, NOT cumulative `result` event totals.

## Warm Pool Integration

- CC sessions skip warm pool (fast cold start makes it unnecessary)
- On provider switch (ACP↔CC): `_pool_started` flag reset, stale health loop cancelled, pool refilled for new provider

## Config

```json
{
  "agent": {
    "provider": "claude_code",
    "cc_connection_mode": "per_session",
    "cc_model": "",
    "cc_permission_mode": "bypassPermissions",
    "cc_max_turns": 0,
    "cc_max_budget_usd": 0.0
  }
}
```

## Model Registry & Translation

`model_registry.json` (loaded by `model_registry.py`, mirrored to `website/src/model_registry.json`) is the single source of truth for model names. Canonical keys are **versioned+capability** (`opus-4.8-1m`, `opus-4.7-1m`, `sonnet-4.6-1m`, `opus-4.8`, `auto`). Each entry maps to `{display, description, window, providers:{claude_code:<provider id>}, aliases}`.

**Translation boundary:** canonical keys live only on the wire (frontend ↔ HTTP API) and in persisted `agent.cc_model`. Translation canonical→provider-id happens **exactly once** at the `config.loader._claude_code` factory (`model_registry.to_provider_id`). Everything below (`AcpProvider`, `acp/client.py`, the injected `settings.local.json`) uses provider ids. `to_provider_id` is identity for an already-resolved provider id (back-compat) and maps dotted ids (`claude-opus-4.6`, shipped by some externally-managed agents) via each entry's `aliases` (so e.g. `kiroclaw-lite` stays on Sonnet, not the flagship).

**Window:** the adapter infers the context window from the resolved model-id string (`/\b1m\b/`), so the `[1m]` id must be the one selected. That only happens when the id is in `availableModels` (see below).

### availableModels injection (the 1M-window fix)

The adapter resolves the served model + window from the `availableModels` allowlist in the `settings.json` files its `SettingsManager` watches (user/project/local/enterprise, merged union+dedup). With a default allowlist like `["opus","sonnet"]`, the `[1m]` id substring-collapses to the `opus` alias → 200K. KiroClaw writes the full versioned allowlist into the **KiroClaw-owned per-session** `<work_dir>/.claude/settings.local.json` (`AcpClient._write_claude_local_settings`), the adapter's **highest-precedence** source — so the `[1m]` id wins by exact match (1M window) even when the operator's `~/.claude` is polluted. This file is created at session start and removed on cleanup; KiroClaw never writes model config to the operator's real `~/.claude`.

### User ~/.claude is deny-only

`install_cc_global_deny_settings` writes ONLY `permissions.deny` (security control) + a `_kiroclaw_managed` marker to the user's `~/.claude/settings.json` — never `availableModels`/`model`. `revert_user_model_settings` (CLI `kiroclaw cc revert-settings [--dry-run]`, plus an idempotent auto-run on gateway boot) removes model keys KiroClaw wrote in earlier versions, value-matched against the historical constants so user-customized values and deny patterns are left intact.

## MCP Server Registration

CC reads MCP servers from `~/.claude/.mcp.json` (generated by `cc_agent.py:generate_mcp_json()`). Same server set as ACP path: builder-mcp, kiroclaw-core, kiroclaw-cron, arcc-governance, user-configured servers.

## Config Isolation (CLAUDE_CONFIG_DIR)

> Note: the **live** `claude_code` provider is `AcpProvider(acp_backend="claude")` (claude-agent-acp), not the legacy `ClaudeCodeProvider` whose Session Resume / MCP sections above describe `--resume` and `~/.claude/.mcp.json`. The isolation below applies to the live ACP path.

Without isolation, the spawned `claude-agent-acp` subprocess loads the operator's global `~/.claude/settings.json` `enabledPlugins` (e.g. ~25 AIM plugins). Each plugin re-declares its own parameterized `builder-mcp` server, so the session prompt carries ~17× duplicated `builder-mcp` instruction blocks + plugin-namespaced deferred tools, plus ~80 plugin/user agent descriptions and ~100 skills — ~30–55k tokens of base-prompt bloat before any conversation. This duplication originates in how AIM generates one `.mcp.json` per cc-plugin (`~/.aim/cc-plugins/`); there is no cross-plugin dedup upstream.

KiroClaw isolates the spawned session with a dedicated config dir (the internal DTxGateway/Dagwooda pattern):

- **`CLAUDE_CONFIG_DIR=<config_dir>/cc-config`** is injected into the subprocess env (`config/loader._claude_code`). The adapter's `SettingsManager` and the SDK read settings/transcripts from there instead of `~/.claude`. `<config_dir>` honors `KIROCLAW_HOME` (dev isolation).
- **`cc_agent.seed_isolated_cc_config()`** writes `<cc-config>/settings.json` as a copy of the real `~/.claude/settings.json` with `enabledPlugins` / `extraKnownMarketplaces` / `enabledMcpjsonServers` / cosmetic keys **stripped**, then layers KiroClaw's deny patterns + 1M `availableModels`. It runs at gateway boot (`repair_agent_configs`) and before every spawn (idempotent, never early-returns so creds are always re-copied).
- **`cc_agent.cc_config_root()`** is the single source of truth shared by env-injection, the resume guard (`acp/client.py`), and cleanup (`providers/cleanup.py`) so transcript storage, resume `.exists()`, and deletion all agree on the relocated root.

**Why it's safe (kept verbatim from the seed source):**
- **Bedrock auth** — `awsCredentialExport` (the native CLI's cred-refresh command) is copied into the isolated settings.json. Dropping it is what broke auth when we tried `settingSources:[]`; that key lives in the `user` tier the native CLI reads, and `settingSources` is all-or-nothing per tier.
- **1M window** — the KiroClaw-owned isolated `<cc-config>/settings.json` keeps the full `[1m]` `availableModels` (via `_apply_deny_and_models_for_isolated`) as a fallback, but the authoritative lever is the per-session `<work_dir>/.claude/settings.local.json` (highest precedence). The user's real `~/.claude` no longer receives model keys (see Model Registry & Translation above).
- **Deny gate** — enforced host-side (`canUseTool` → `hooks.on_tool_call` → `reject_tool`), independent of settings; `permissions.defaultMode` is dropped from the seed so it can't auto-approve.

**Guard:** set `KIROCLAW_CC_ISOLATE=0` to disable isolation — `cc_config_root()` then resolves to `~/.claude` everywhere (legacy shared behavior). `CLAUDE_CONFIG_DIR` set in the gateway env overrides the resolver (test/operator escape hatch).

**Caveat:** the seed only carries `awsCredentialExport`; non-Bedrock (OAuth/API-key) deployments whose auth lives in `~/.claude/.credentials.json` would lose it under isolation — gate before shipping to such deploys.

## Installation

```bash
toolbox install claude-code
claude --version
```

Then: `{"agent": {"provider": "claude_code"}}` in config.json.

## Key Files

- `src/kiro_claw/providers/claude_code.py` — provider implementation
- `src/kiro_claw/cc_agent.py` — .mcp.json generation, agent config
- `src/kiro_claw/session_map.py` — CWD + provider persistence
