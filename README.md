# KiroClaw

Open-source personal AI agent that runs on your own machine. Chat with it from
Slack, a web dashboard, or the command line; let it run multi-step tasks
unattended, schedule recurring jobs, and remember context across sessions.
**[What's New](CHANGELOG.md)**

```
CLI / Slack DM / Dashboard → KiroClaw → LLM agent (claude-agent-acp) + MCP tools
```

KiroClaw talks to an LLM through a pluggable provider layer. The default
provider is [`claude-agent-acp`](https://github.com/zed-industries/agent-client-protocol)
(the Claude Code backend over the Agent Client Protocol). You can also point it
at a local `kiro-cli` ACP agent or at Amazon Bedrock — see
[Configuration](#configuration).

## Quick Start

KiroClaw ships as a Python backend (installed via `pip`) plus a React web
dashboard (built with `npm`). Memory and the knowledge library use a local
[Ollama](https://ollama.com) server for embeddings.

> **Windows is not supported.** The default `claude-agent-acp` backend (and the
> optional `kiro-cli` backend) only run on macOS and Linux.

### 1. Install the backend (pip)

```bash
# From a clone of this repo
git clone https://github.com/YOUR_ORG/kiroclaw.git
cd kiroclaw

# Build the frontend (see step 2) BEFORE installing so the dashboard is bundled
pip install .
# or, for development, an editable install:
pip install -e ".[voice]"   # [voice] adds optional speech-to-text extras
```

This installs the `kiroclaw` (and `kiroclaw-browse`) commands onto your `PATH`.

### 2. Build the frontend (npm)

The dashboard is a React + Vite SPA in the `website/` directory. Production
builds are bundled into `src/kiro_claw/static/dist/` and served by the backend.

```bash
cd website
npm install
npm run build
# Copy the build output into the package so pip bundles it:
#   cp -r website/dist ../src/kiro_claw/static/dist
```

### 3. Install the agent backend

Install whichever LLM backend you want KiroClaw to drive:

- **`claude-agent-acp`** (default) — follow the
  [Claude Code / agent-client-protocol](https://github.com/zed-industries/agent-client-protocol)
  install instructions and make sure the adapter binary is on your `PATH`.
- **`kiro-cli`** (optional) — install per its own docs.
- **Amazon Bedrock** (optional) — configure AWS credentials; no extra binary.

### 4. Install Ollama (for memory / knowledge embeddings)

```bash
# Install Ollama from https://ollama.com, then pull the embedding model:
ollama pull qwen3-embedding:0.6b      # default
# or the documented fallback:
ollama pull nomic-embed-text
```

Ollama runs at `http://localhost:11434` by default. KiroClaw manages its
lifecycle automatically; you can also run your own server.

### 5. Configure and run

```bash
kiroclaw setup                # interactive wizard: data dir, agent, credentials
kiroclaw doctor               # verify everything is wired up
kiroclaw gateway              # start server → open http://localhost:7777
```

**Dashboard-only mode**: skip Slack tokens during `kiroclaw setup` to run
without Slack.

## Installation & Distribution

There are three ways to build and run KiroClaw, from lightest (developer
checkout) to heaviest (double-clickable desktop app). All builds are driven by
the [`Makefile`](Makefile) — plain `pip` + `npm`/Vite + `pytest`, no
proprietary tooling. See [docs/INSTALL.md](docs/INSTALL.md) for the full guide.

### a. From source (development)

Build the dashboard, install the backend into a local virtualenv, then run the
gateway straight from `src/`:

```bash
make build                                   # npm build + venv editable install
PYTHONPATH=src python -m kiro_claw gateway   # → http://localhost:7777
```

### b. Self-contained pip wheel

Produce a wheel that bundles the pre-built dashboard, then install it anywhere
with Python:

```bash
make wheel                # → dist/kiroclaw-0.1.0-*.whl (dashboard bundled)
pip install dist/*.whl    # installs the kiroclaw / kiroclaw-browse commands
kiroclaw gateway          # → http://localhost:7777
```

### c. Bundled desktop app

Build a double-clickable desktop app that embeds a frozen Python backend (via
PyInstaller) inside an Electron shell — end users need **no** Python, pip, npm,
or node:

```bash
make desktop              # → website/electron/dist/KiroClaw-*.dmg (macOS)
                          #   or website/electron/dist/KiroClaw-*.AppImage (Linux)
```

See [docs/DESKTOP_APP.md](docs/DESKTOP_APP.md) for the build pipeline and how
the app locates and launches the bundled backend.

### Makefile targets

| Target | What it does |
|--------|--------------|
| `make build` | Build the frontend (npm/Vite) + install the backend into `.venv` |
| `make wheel` | Self-contained pip wheel with the dashboard bundled → `dist/` |
| `make backend-bin` | Frozen standalone backend binary (PyInstaller), no Electron |
| `make desktop` | Full desktop app — DMG (macOS) / AppImage (Linux) |
| `make test` | Build, then run the `pytest` suite |
| `make clean` | Remove build artifacts, dists, caches |

## What It Does

| Surface | Description |
|---------|-------------|
| **Slack DM** | Chat with your agent in Slack. Each thread = isolated AI session with full tool access |
| **Web Dashboard** | React SPA at localhost:7777 — multi-session chat, memory explorer, cron manager, app store |
| **Desktop App** | Electron wrapper with multi-tab gateway connections and native macOS tabs |
| **CLI** | `kiroclaw chat`, `kiroclaw run TASK.md`, `kiroclaw cron`, `kiroclaw spawn` |

### Key Capabilities

- **Autonomous task execution** — run multi-step specs unattended for hours (`kiroclaw run`)
- **Cron scheduling** — recurring and one-shot jobs with jitter, timeouts, and timezone support
- **Subagent orchestration** — spawn parallel background agents for independent tasks
- **Persistent memory** — learns preferences, remembers context across sessions
- **Self-learning** — corrections persist as lessons that change future behavior
- **App platform** — install and build apps that extend KiroClaw (App Store + SDK)
- **Security sandbox** — OS-level isolation (namespaces/seatbelt), credential redaction, denied command patterns
- **MCP tool ecosystem** — auto-discovers and manages MCP servers (slack-mcp and any MCP server you add)
- **Voice** — optional speech-to-text input + text-to-speech replies (Piper, or AWS via the `voice` extra)
- **Knowledge Library** — ingest docs/code into a searchable graph with SQLite FTS5 + Ollama embeddings
- **System service** — `kiroclaw service install` for systemd/launchd with auto-restart

For the full feature list, see [docs/FEATURES.md](docs/FEATURES.md).

## Running 24/7

For always-on operation (Slack bot, cron jobs, task runner):

```bash
kiroclaw service install      # systemd (Linux) or launchd (macOS)
kiroclaw service status
```

For running on a remote host, see [docs/REMOTE_DESKTOP_SETUP.md](docs/REMOTE_DESKTOP_SETUP.md).

## Configuration

Config: `~/.kiroclaw/config.json` — manage via `kiroclaw config get/set/edit`

```json
{
  "agent": { "provider": "claude_code", "approval_mode": "interactive", "sandbox": "auto" },
  "session": { "timeout_secs": 1800, "pool_size": 0 },
  "dashboard": { "bot_name": "KiroClaw" },
  "slack": { "command": "kiroclaw" }
}
```

> The dashboard port is set via the `KIROCLAW_PORT` env var (default `7777`) or
> `kiroclaw gateway --port <n>`, not in config. `dashboard.url` is the
> externally-advertised URL only.

**Choosing a provider** — set `agent.provider`:

| Provider | Value | Backend |
|----------|-------|---------|
| Claude Code (default) | `claude_code` | `claude-agent-acp` over stdio |
| kiro-cli | `acp` | `kiro-cli` ACP agent over stdio |
| Amazon Bedrock | `bedrock` | AWS Bedrock (requires AWS credentials) |

**Embeddings** — `memory.embedding_model` (default `qwen3-embedding:0.6b`) and
`memory.embedding_url` (default `http://localhost:11434`) control the Ollama
server used for memory and knowledge-library search.

Credentials: `~/.kiroclaw/.env` — `SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `KIROCLAW_OWNER_ID`

## Troubleshooting

### `AcpTimeoutError: ACP prompt timed out`

The agent backend didn't respond to the initialize handshake. Fixes:

1. Confirm the backend binary (`claude-agent-acp` or `kiro-cli`) is on your `PATH`
2. `kiroclaw setup --agent-only --clean` if MCP servers are broken
3. Wait — first launch loads MCP servers and can take >60s
4. `kiroclaw doctor` for a full health check

### Memory / knowledge search not working

1. Confirm Ollama is installed and running (`curl http://localhost:11434/api/tags`)
2. Pull the embedding model: `ollama pull qwen3-embedding:0.6b`
3. `kiroclaw doctor` reports embedding-server health

### Slack integration not working

Slack is optional — dashboard-only mode works without it. For Slack setup, see
[SLACK_SETUP.md](SLACK_SETUP.md).

### MCP server not working after uninstall

```bash
kiroclaw setup --agent-only          # re-validates, drops missing servers
kiroclaw setup --agent-only --clean  # fresh config from scratch
```

## Documentation

| Document | Description |
|----------|-------------|
| [docs/INSTALL.md](docs/INSTALL.md) | Build & install guide — the three run methods, Makefile targets, env vars |
| [docs/DESKTOP_APP.md](docs/DESKTOP_APP.md) | Electron desktop app build pipeline and packaging |
| [FEATURES.md](docs/FEATURES.md) | Complete feature reference |
| [DEPENDENCIES.md](DEPENDENCIES.md) | Full dependency list (pip + npm + optional extras) |
| [CHANGELOG.md](CHANGELOG.md) | Release history |
| [AGENTS.md](AGENTS.md) | AI assistant rules and development conventions |
| [SLACK_SETUP.md](SLACK_SETUP.md) | Slack app creation and setup |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development workflow and PR guidelines |
| [docs/REMOTE_DESKTOP_SETUP.md](docs/REMOTE_DESKTOP_SETUP.md) | 24/7 remote host setup |
| [docs/security-deep-dive.md](docs/security-deep-dive.md) | Security architecture |
| [docs/app-kit/getting-started.md](docs/app-kit/getting-started.md) | App Kit developer guide |
| [docs/system-specs/](docs/system-specs/) | Module-level specifications |

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md) for full guidelines.

```bash
# Backend
pip install -e ".[voice]"
pytest

# Frontend (in website/)
npm install
npm run check          # typecheck + lint + tests
npm run build          # production bundle → website/dist
```

## License

See [LICENSE](LICENSE) if present in this repository.
