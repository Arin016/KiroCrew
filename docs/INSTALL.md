# Installing & Building KiroClaw

This guide covers how to build, install, and run KiroClaw. There are three ways
to do it, from lightest (a developer checkout) to heaviest (a double-clickable
desktop app). All builds are driven by the repo-root [`Makefile`](../Makefile)
and use plain `pip` + `npm`/Vite + `pytest` — there is no proprietary build
tooling.

> **Windows is not supported.** The default `claude-agent-acp` backend (and the
> optional `kiro-cli` backend) only run on macOS and Linux.

## Prerequisites

| Requirement | Needed for | Notes |
|-------------|------------|-------|
| **Python 3** | Backend | `pip` install; `make build` creates a `.venv` |
| **Node.js + npm** | Frontend (dashboard) | Builds the React/Vite SPA; also for the desktop app |
| **An agent backend** | Driving the LLM | See below — `claude-agent-acp` is the default |
| **Ollama** (optional) | Memory / knowledge embeddings | Graceful degradation if absent |

### Agent backend (required)

KiroClaw drives an LLM through a pluggable provider layer. Install whichever
backend you want:

- **`claude-agent-acp`** (default) — the Claude Code backend over the
  [Agent Client Protocol](https://github.com/zed-industries/agent-client-protocol):

  ```bash
  npm install -g @agentclientprotocol/claude-agent-acp
  ```

  Make sure the adapter binary is on your `PATH`.
- **`kiro-cli`** (optional) — install per its own docs; select with
  `agent.provider = acp`.
- **Amazon Bedrock** (optional) — configure AWS credentials; no extra binary.
  Select with `agent.provider = bedrock`.

### Ollama (optional — for memory / knowledge embeddings)

Memory and the knowledge library use a local [Ollama](https://ollama.com) server
for embeddings. If Ollama is absent, KiroClaw degrades gracefully (embedding
search is disabled) rather than crashing.

```bash
# Install Ollama from https://ollama.com, then pull the embedding model:
ollama pull qwen3-embedding:0.6b      # default
# or the documented fallback:
ollama pull nomic-embed-text
```

Ollama runs at `http://localhost:11434` by default.

## The three ways to run

### a. From source (development)

Build the dashboard, install the backend into a local virtualenv (`.venv`), and
run the gateway directly from `src/`:

```bash
make build                                   # npm build + editable backend install into .venv
PYTHONPATH=src python -m kiro_claw gateway   # → http://localhost:8765
```

`make build` runs two steps:

1. **`frontend`** — `npm ci` (or `npm install`) + `npm run build` in `website/`,
   then copies `website/dist` into `src/kiro_claw/static/dist` so the backend
   serves the SPA.
2. **`backend`** — creates `.venv` and runs an editable install (`pip install -e .`).

You can also invoke any CLI subcommand the same way, e.g.
`PYTHONPATH=src python -m kiro_claw setup` or
`PYTHONPATH=src python -m kiro_claw doctor`.

### b. Self-contained pip wheel

Produce a wheel that bundles the pre-built dashboard, then install it anywhere
that has Python:

```bash
make wheel                # builds the frontend, then python -m build --wheel → dist/
pip install dist/*.whl    # → installs the kiroclaw / kiroclaw-browse commands onto PATH
kiroclaw gateway          # → http://localhost:8765
```

The wheel is `dist/kiroclaw-0.1.0-*.whl`. The dashboard is bundled into the
package via the custom `BuildWithFrontend` build step in
[`setup.py`](../setup.py); the pip install name is **`kiroclaw`** (the import
package is `kiro_claw`).

Installed console scripts:

| Command | Entry point |
|---------|-------------|
| `kiroclaw` | `kiro_claw.cli:main` |
| `kiroclaw-browse` | `kiro_claw.browser.cli:main` |

Optional extras (install with e.g. `pip install kiroclaw[voice]`):

| Extra | Adds |
|-------|------|
| `voice` | `boto3`, `amazon-transcribe` for speech-to-text |
| `aws` | `boto3` for AWS integrations |
| `desktop` | `pyinstaller` for building the frozen backend |

### c. Bundled desktop app

Build a double-clickable desktop app that embeds a frozen Python backend (via
PyInstaller) inside an Electron shell. End users need **no** Python, pip, npm,
or node:

```bash
make desktop              # → website/electron/dist/KiroClaw-*.dmg (macOS)
                          #   or website/electron/dist/KiroClaw-*.AppImage (Linux)
```

See [DESKTOP_APP.md](DESKTOP_APP.md) for the full build pipeline (frontend →
stage → PyInstaller → electron-builder) and how the app locates and launches the
bundled backend.

To build only the frozen standalone backend binary (no Electron packaging):

```bash
make backend-bin          # → build/pyinstaller/dist/kiroclaw-backend/kiroclaw-backend
```

## Makefile targets

| Target | What it does |
|--------|--------------|
| `make build` | Build the frontend (npm/Vite) + install the backend into `.venv` |
| `make wheel` | Self-contained pip wheel with the dashboard bundled → `dist/` |
| `make backend-bin` | Frozen standalone backend binary (PyInstaller), no Electron |
| `make desktop` | Full desktop app — DMG (macOS) / AppImage (Linux) |
| `make test` | Build, then run the `pytest` suite |
| `make clean` | Remove build artifacts, dists, and caches |

Override the Python interpreter with `make PY=python3.12 build`.

## Configure and run

After installing (any of the three methods), set up and verify:

```bash
kiroclaw setup            # interactive wizard: data dir, agent, credentials
kiroclaw doctor           # verify everything is wired up
kiroclaw gateway          # start the server → open http://localhost:8765
```

From a source checkout, prefix with `PYTHONPATH=src python -m kiro_claw` instead
of `kiroclaw`.

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `KIROCLAW_HOME` | `~/.kiroclaw` | Data directory (config, credentials, databases) |
| `KIROCLAW_PORT` | `8765` | Port the gateway / dashboard listens on |

- Config file: `~/.kiroclaw/config.json` (manage via `kiroclaw config get/set/edit`).
- Credentials: `~/.kiroclaw/.env` (`SLACK_APP_TOKEN`, `SLACK_BOT_TOKEN`, `KIROCLAW_OWNER_ID`).

> **Note:** `KIROCLAW_PORT` is an environment variable (validated at CLI entry),
> not a config key; it sets the port the gateway / dashboard binds to. You can
> also pass `--port` on the CLI to override it. The `dashboard.url` config key is
> only for advertising a remote URL.

## Troubleshooting

For runtime issues (ACP handshake timeouts, embedding/memory search, Slack,
MCP server cleanup), see the **Troubleshooting** section of the
[README](../README.md#troubleshooting). A quick health check is always:

```bash
kiroclaw doctor
```
