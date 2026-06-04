# Getting Started with KiroClaw

## What Is KiroClaw?

KiroClaw is an autonomous AI agent layer that runs on top of an agent backend
(claude-agent-acp by default; kiro-cli optional). It adds persistent memory,
scheduled jobs, background subagents, self-learning, and multi-session
orchestration. You interact with it via Slack DMs or a web dashboard.

## Prerequisites

- **Python 3.10+** and **pip** (backend)
- **Node.js 18+** and **npm** (frontend dashboard)
- **An agent backend** — `claude-agent-acp` (default) or the Claude Code CLI.
  kiro-cli is supported but optional.
- **macOS or Linux** — Windows is not supported
- *(Optional)* **Ollama** for local vector-memory embeddings

## Installation

### Backend (pip)

```bash
pip install kiroclaw
```

Or, from a checkout of the repository:

```bash
pip install -e .
```

### Frontend (npm)

The web dashboard is built with Vite:

```bash
cd website
npm install
npm run build
```

In development, `npm run dev` serves the dashboard with hot reload.

### Agent backend

Install the default ACP backend (published to npm):

```bash
npm install -g @agentclientprotocol/claude-agent-acp
```

Alternatively, install the Claude Code CLI and set `agent.provider` to
`claude_code`. See [Configuration](configuration.md) for provider options.

## First-Time Setup

```bash
kiroclaw setup
```

This interactive wizard:
1. Detects the agent backend (claude-agent-acp / Claude Code) on your PATH
2. Saves the project directory for future use
3. Installs the agent config to `~/.kiro/agents/kiroclaw.json`
4. Prompts for Slack credentials (app token, bot token, owner ID)
5. Optionally sets up `http://kiroclaw.localhost:8765` custom domain

### Slack Credentials

You need three values from your Slack app:
- `SLACK_APP_TOKEN` — starts with `xapp-`
- `SLACK_BOT_TOKEN` — starts with `xoxb-`
- `KIROCLAW_OWNER_ID` — your Slack user ID (starts with `U`)

> ⚠️ **Use the user ID from the workspace where the bot is installed.** Your
> user ID is different in each Slack workspace.

These are stored in `~/.kiroclaw/.env`.

## Starting KiroClaw

### Gateway Mode (Slack + Dashboard)

```bash
kiroclaw gateway
```

This starts the full server: Slack Socket Mode listener, web dashboard, cron
scheduler, heartbeat, and auto-update checker. The dashboard opens at
`http://localhost:8765`.

### Chat Mode (CLI only)

```bash
# Interactive chat
kiroclaw chat

# Single message
kiroclaw chat -m "what's the weather like?"
```

Lightweight mode — no Slack, no dashboard, just a terminal REPL with the LLM.

## Verifying Your Setup

```bash
kiroclaw doctor
```

Checks: agent backend installation, project directory, agent config, MCP
tools, Slack credentials, gateway status, and vector memory.

## Updating

```bash
pip install -U kiroclaw
```

For a checkout, `git pull` and rebuild the frontend (`cd website && npm run
build`). Clicking "Update Available" in the dashboard topbar runs the update
for source installs.

## Running in the Background

### macOS (Launch Agent)

Create `~/Library/LaunchAgents/com.kiroclaw.gateway.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kiroclaw.gateway</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/zsh</string>
        <string>-lic</string>
        <string>kiroclaw gateway</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key>
    <string>$HOME/.kiroclaw/logs/gateway.stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$HOME/.kiroclaw/logs/gateway.stderr.log</string>
</dict>
</plist>
```

Then load it:
```bash
mkdir -p ~/.kiroclaw/logs
launchctl load ~/Library/LaunchAgents/com.kiroclaw.gateway.plist
```

Replace `$HOME` with your actual home path. Uses `/bin/zsh -lic` to load your
shell profile (so `kiroclaw` is on PATH). Swap to `/bin/bash` if needed.

### Linux (systemd)

```bash
kiroclaw service install   # creates and enables the systemd user service
kiroclaw service start
```

## Dev Mode

For development, use isolated data directories:

```bash
export KIROCLAW_HOME=.kiroclaw-dev
export KIROCLAW_PORT=6777
kiroclaw gateway
```

This keeps dev data separate from your real `~/.kiroclaw`. Useful for running
multiple KiroClaw instances simultaneously — each with its own memory, crons,
and sessions.
