# System Specifications

Last Updated: 2026-05-10

## How to Use

Load relevant module specs before making changes to that component. Read common patterns for cross-cutting concerns.

## Modules

| Module | Description |
|--------|-------------|
| [acp-client](modules/acp-client.md) | JSON-RPC 2.0 client for kiro-cli ACP protocol |
| [channel-history](modules/channel-history.md) | Group conversation context buffer |
| [config](modules/config.md) | Dataclass config schema and loader |
| [cli](modules/cli.md) | argparse CLI commands (chat, gateway, doctor, setup, manifest) |
| [heartbeat](modules/heartbeat.md) | Periodic background tasks |
| [history](modules/history.md) | Persistent conversation history with LLM consolidation |
| [learn-cron-dashboard](modules/learn-cron-dashboard.md) | Self-learning, cron scheduler, web dashboard |
| [memory-skills-hooks](modules/memory-skills-hooks.md) | Memory files, skill loading, message/tool hooks |
| [persistent-agent-channels](modules/persistent-agent-channels.md) | Multi-agent collaboration channels |
| [providers](modules/providers.md) | LLM provider abstraction (KiroACP / kiro-cli — the sole provider) |
| [security](modules/security.md) | Defense-in-depth: sandbox, XPIA hardening, auth, denied commands |
| [sel](modules/sel.md) | Security Event Log — immutable audit trail for tool invocations |
| [session](modules/session.md) | Thread-keyed ACP session pool with idle expiry |
| [subagent](modules/subagent.md) | Background agent spawning with reaper, approval cascade, turn limits |
| [slack-gateway](modules/slack-gateway.md) | Slack Socket Mode gateway, handler, client abstraction |
| [task](modules/task.md) | Task state machine |
| [taskrunner](modules/taskrunner.md) | Autonomous multi-step task executor with git coordination |

## Common Patterns

| Pattern | Description |
|---------|-------------|
| [error-handling](common/error-handling.md) | Exception hierarchy and error boundaries |
| [testing-conventions](common/testing-conventions.md) | pytest patterns, mocking, test structure |

## Design

| Document | Description |
|----------|-------------|
| [architecture-overview](design/architecture-overview.md) | High-level system architecture and ACP protocol |

## Features

| Feature | Description |
|---------|-------------|
| [claude-code-provider](features/claude-code-provider.md) | Removed — KiroClaw is KiroACP/kiro-cli only; documents the dormant ACP seam that remains |
| [code-approvers](features/code-approvers.md) | Tier-based CR reviewer routing with drift validator |
| [dashboard-token-auth](features/dashboard-token-auth.md) | Slack-gated HMAC token authentication for dashboard |
| [inline-action-buttons](features/inline-action-buttons.md) | Interactive buttons in chat messages |
| [prompt-optimizer](features/prompt-optimizer.md) | Native pre-send prompt optimization (Cmd+Shift+Enter) |
| [stt-streaming](features/stt-streaming.md) | Live speech-to-text via AWS Transcribe Streaming |
| [voice-streaming](features/voice-streaming.md) | Real-time Polly TTS with streaming auto-speak and interrupt |
| [project-agents](features/project-agents.md) | Per-project `.kiro/agents/` discovery, registry, and agent picker integration |
