# CLI Module

Last Updated: 2026-05-18 (restart subcommand)

## Overview

The CLI module (`kiro_claw/cli.py`) provides the `kiroclaw` command using stdlib `argparse`.

## Project Directory Detection

At startup, `main()` auto-detects the project root and sets `KIROCLAW_PROJECT_DIR`:

1. If `KIROCLAW_PROJECT_DIR` env var is already set, use it
2. Walk up from CWD looking for a directory with both `agents/` and `skills/`
3. Read saved path from `~/.kiroclaw/project_dir` (written by `kiroclaw setup`)

This allows `kiroclaw` to find project-level agent config and skills from any directory.

## Commands

| Command | Description |
|---------|-------------|
| `kiroclaw chat -m "msg"` | Send a single message, print streaming response |
| `kiroclaw chat` | Interactive chat mode (readline, exit with Ctrl+D) |
| `kiroclaw chat --model X` | Override model for this session |
| `kiroclaw gateway` | Start the KiroClaw server (dashboard + Slack) |
| `kiroclaw gateway --slack-only` | Start without dashboard or SSH tunnel instructions |
| `kiroclaw gateway --no-crons` | Start without cron scheduler (use when another instance handles crons) |
| `kiroclaw setup` | Install agent config, save project dir, configure credentials |
| `kiroclaw setup --agent-only` | Only install agent config (skip credentials) |
| `kiroclaw doctor` | Verify kiro-cli is installed and config is valid |
| `kiroclaw cron add/list/remove` | Manage cron jobs |
| `kiroclaw spawn run/list` | Manage background subagents |
| `kiroclaw learn add/list/remove` | Manage learned corrections |
| `kiroclaw run TASK.md` | Run an autonomous task from a spec file |
| `kiroclaw token` | Print a dashboard access URL with auth token |
| `kiroclaw logout` | Revoke all active dashboard sessions |
| `kiroclaw manifest` | Generate Slack manifest with user alias auto-populated |
| `kiroclaw update` | Update to latest version (git pull + rebuild) |
| `kiroclaw status` | Show runtime stats from running gateway |
| `kiroclaw stop` | Stop a running gateway (service-aware: stops the systemd/launchd service if active, otherwise SIGTERM via port lookup) |
| `kiroclaw restart` | Restart a running gateway (service-aware: restarts the systemd/launchd service if active, otherwise SIGTERMs the foreground gateway and respawns it detached). |
| `kiroclaw service install` | Install gateway as a system-level systemd service (Linux, requires sudo for `tee` + `systemctl` only) or launchd LaunchAgent (macOS, no sudo). Auto-restarts on crash, auto-starts on boot. |
| `kiroclaw service uninstall` | Stop and remove the systemd unit / launchd plist. |
| `kiroclaw service status` | Show service status (`systemctl status` or `launchctl list`). No sudo required. |
| `kiroclaw logs` | Tail gateway logs from the systemd journal, launchd stdout file, or `~/.kiroclaw/gateway.log`. |
| `kiroclaw logs -f` | Follow logs live (long-running tail). |
| `kiroclaw security events` | Show recent SEL audit events (`-n N` for count) |
| `kiroclaw security verify` | Verify SEL HMAC chain integrity |
| `kiroclaw snapshot` | Create a .tar.gz snapshot of all KiroClaw state |
| `kiroclaw snapshot --keep N` | Auto-prune to N most recent snapshots (default 7) |
| `kiroclaw snapshot --list` | List existing snapshots |
| `kiroclaw restore <file>` | Restore from a snapshot (auto-detects replace vs merge) |
| `kiroclaw restore <file> --mode replace\|merge` | Force restore mode |
| `kiroclaw restore <file> --components X,Y` | Selective component restore |
| `kiroclaw restore <file> --dry-run` | Preview restore without writing |
| `kiroclaw restore --list-components` | Show available component names |
| `kiroclaw config get [key]` | Print full config or a dot-path value |
| `kiroclaw config set <key> <val>` | Set a config value (auto type detection) |
| `kiroclaw config set --file <path>` | Replace config from a JSON file |
| `kiroclaw config edit` | Open config in `$EDITOR` |
| `kiroclaw memory show/edit` | Show or edit memory (preferences, projects, history) |
| `kiroclaw mcp-cron` | MCP server for cron tools (spawned by kiro-cli) |
| `kiroclaw mcp-core` | MCP server for spawn, learn, task tools (spawned by kiro-cli) |
| `kiroclaw --version` | Print version |

## Setup Command

`kiroclaw setup` performs:

1. Saves `KIROCLAW_PROJECT_DIR` to `~/.kiroclaw/project_dir`
2. Installs agent config to `~/.kiro/agents/kiroclaw.json`
3. Prompts for Slack credentials (unless `--agent-only`)
4. Offers to set up custom domain `kiroclaw.localhost` (macOS/Linux)

The saved project dir enables running `kiroclaw` from any directory.

### Custom Domain

After credentials, `kiroclaw setup` offers to add `127.0.0.1 kiroclaw.localhost` to the system hosts file so the dashboard is accessible at `http://kiroclaw.localhost:7777`:

- **macOS/Linux**: Uses `sudo tee -a /etc/hosts` for safe append

Skipped if `kiroclaw.localhost` is already present or user declines.

## Config Command

`kiroclaw config` manages `~/.kiroclaw/config.json`:

- **get** — prints full effective config (with defaults resolved) or a single dot-path value
- **set key value** — sets a value with auto type detection (bool/int/float/JSON/string). Rejects unknown leaf keys.
- **set --file path** — replaces entire config from a JSON file. File read routed through `hooks.safe_read_file()` (blocks sensitive paths).
- **edit** — opens config in `$EDITOR` (supports args like `code --wait` via `shlex.split`). Creates default config if missing.

All write paths emit SEL audit events (`config_get`, `config_set`, `config_set_file`, `config_edit`).

### Gateway Auto-Create

`kiroclaw gateway` creates `~/.kiroclaw/config.json` with defaults if the file doesn't exist. Does nothing if it already exists.

## Verbosity

| Flag | Level | What you see |
|------|-------|-------------|
| (none) | WARNING | Errors only |
| `-v` | INFO | Session lifecycle, context %, compaction |
| `-vv` | DEBUG | ACP events, message updates, full traces |

## Interactive Mode

- Prompt: `you> `
- Exit: `exit`, `quit`, `/exit`, `/quit`, `:q`, Ctrl+D
- Streaming output printed as chunks arrive

### Context Tracking

After each message, checks `provider.context_usage_pct()`:
- `>= autocompact_pct` (default 90%): compact → shutdown → restart provider, reset counter
- `>= 75%`: warning printed to stderr

CLI compaction is blocking (single-user, acceptable).

## Entry Point

`console_scripts` in `setup.cfg` maps `kiroclaw` → `kiro_claw.cli:main`.

## Environment Variables

| Variable | Purpose |
|----------|---------|
| `KIROCLAW_HOME` | Override config/data directory (default `~/.kiroclaw`) |
| `KIROCLAW_PORT` | Override dashboard port (default `7777`, validated as int at CLI startup) |
| `KIROCLAW_PROJECT_DIR` | Override agent config/skills directory |
| `KIROCLAW_WORKSPACE` | Override workspace root directory |

For local dev:
- **macOS/Linux**: `bin/kiroclaw` (POSIX shell wrapper); `source setup.sh` adds `bin/` to PATH

The wrapper sets `KIROCLAW_PROJECT_DIR` and routes to the right runtime based on install type:

- **One-liner install** (`install.sh` clones the repo into `~/.kiroclaw-app/`): if a sibling `.venv/bin/kiroclaw` exists, the wrapper execs it directly. No brazilcli required, which matters because child processes spawned under a minimal PATH (e.g. MCP servers launched by kiro-cli) cannot rely on brazilcli being available.
- **Brazil workspace**: the wrapper falls through to `brazil-runtime-exec kiroclaw`, letting the runtime farm resolve dependencies.

## Setup Scripts (First-Time Bootstrap)

`setup.sh` (macOS/Linux) auto-installs all dependencies from scratch. The only prerequisite is `mwinit` (Midway authentication).

> **Note:** Windows is not supported.

**Install order:**
1. Midway authentication (`mwinit -o`)
2. Builder Toolbox (bootstrap script from `release-service.toolbox.builder-tools.aws.dev`)
3. `toolbox install kiro-cli`, `aim`; Node 16 via nvm (GLIBC 2.26 compatible)
4. kiro-cli login (guided SSO authentication)
5. `aim mcp install builder-mcp`
6. `brazil-build`
7. Frontend build (`npm install && npm run build`)
8. PATH setup + shell profile persistence
9. `kiroclaw setup --agent-only` (install kiro-cli agent config)
10. Optional Slack credential configuration

Each step checks if the tool is already installed and skips if present. See `DEPENDENCIES.md` for the full dependency list and manual install instructions.

## Doctor Checks

1. `kiro-cli` binary in PATH
2. **Java / brazil-path**: runs `brazil-path` smoke test — detects missing Java configuration. On macOS, auto-installs Corretto 8 via Homebrew and runs `brazil setup --java` if needed.
3. Project directory and git repo
4. Agent config installed
5. Config values (provider, model, approval mode, dashboard port)
6. **MCP tools**: `@kiroclaw-cron` and `@kiroclaw-core` in `tools`, `allowedTools`, and `mcpServers` — auto-fixes missing entries
7. **Global mcp.json**: kiroclaw MCP servers present with valid binary paths — auto-fixes stale paths
8. **Brazil runtime**: Python 3.10 at `env/KiroClaw-1.0/runtime/bin/python3` — checks fallback Python and dependency availability
9. **Vector memory**: Ollama server health and embedding model status
10. Slack credentials (optional)
11. kiro-cli connectivity
12. Gateway running status

## Update Command

`kiroclaw update` pulls the latest source and rebuilds:

1. `git pull` from `KIROCLAW_PROJECT_DIR`
2. Reads `.install-method` marker: `brazil` → `brazil-build`, `pip` → `pip install -e .`, absent → auto-detect
3. Rebuilds frontend via `build-frontend.sh` (non-fatal on failure)
4. Updates ARCC CLI via `toolbox install arcc-cli`

## Stop Command

`kiroclaw stop [--port PORT]` stops a running gateway:

1. If a systemd/launchd service is active (see Service Management),
   stop it via the service manager and return — without this branch,
   SIGTERM-by-port would be racing the manager's auto-restart.
2. Otherwise: `lsof -ti TCP:{port} -sTCP:LISTEN` to find PIDs.
3. `ps -p {pid} -o args=` to verify it's a KiroClaw process.
4. `SIGTERM` to each verified PID.
5. Waits up to 1s for exit.
6. SEL audit event logged.

## Restart Command

`kiroclaw restart [--port PORT]` restarts a running gateway. Mirrors
`stop`'s service-aware structure:

1. If a systemd/launchd service is active, ask the platform to restart
   it. On Linux: `sudo systemctl restart kiroclaw.service` (single
   atomic operation, smaller down-window than stop+start, and the
   supervisor stays in charge of the lifecycle the whole time). On
   macOS: `launchctl unload <plist>` + `launchctl load <plist>` (no
   `-w`, so persistent enable state is unchanged). The deprecated
   `launchctl restart` is avoided because under `KeepAlive` it behaves
   like `stop` (SIGTERM + immediate respawn) and never re-reads the plist.
2. Otherwise (foreground gateway, no service):
   - `lsof -ti TCP:{port} -sTCP:LISTEN` to detect a running gateway.
     If found, run the existing `_stop` SIGTERM-by-port path.
     If not (e.g. the user runs `restart` after a crash), skip the stop
     step rather than erroring — the user expects to end up with a
     running gateway either way. The `_stop` call is wrapped in a
     `try / except SystemExit` so a TOCTOU race (gateway exits between
     the lsof check and `_stop`'s own lookup → `_stop` calls
     `sys.exit(1)`) does not abort the restart before the spawn.
   - Spawn a detached `kiroclaw gateway` via `subprocess.Popen` with
     `start_new_session=True`, stdin set to `/dev/null`, and stdout +
     stderr redirected to `~/.kiroclaw/gateway.log` (the same file the
     `kiroclaw logs` command tails for foreground gateways). The shell
     returns immediately and the user can follow logs via
     `kiroclaw logs -f`.
3. SEL audit event logged with `via=service` or `via=fork pid=<n>` so
   the audit trail distinguishes the two paths.

## Service Management

`kiroclaw service {install,uninstall,status}` registers the gateway
with the OS service manager so it survives SSH disconnects, restarts
on crash, and starts on boot. Implemented in `src/kiro_claw/service/`.

- **Linux** (`current_platform() == SYSTEMD`):
  - Unit file: `/etc/systemd/system/kiroclaw.service` (root-owned).
  - Install: `sudo tee` writes the unit, then `sudo systemctl
    daemon-reload && sudo systemctl enable --now kiroclaw.service`.
  - The gateway runs as `User=$USER Group=$(id -gn)` — kiroclaw
    code never runs under sudo. Only `tee` and `systemctl` invocations
    are elevated.
  - Boot survival via `WantedBy=multi-user.target` (no linger needed —
    that's a user-service concept; this is system-level).
  - Crash-loop safety: `StartLimitBurst=3 StartLimitIntervalSec=300`.
  - Logs are read from the journal: `sudo journalctl -u kiroclaw -f`,
    or unprivileged if the user is in `systemd-journal` / `adm`.
- **macOS** (`current_platform() == LAUNCHD`):
  - Plist: `~/Library/LaunchAgents/com.amazon.kiroclaw.gateway.plist`
  - Install: `launchctl load -w <plist>`. `RunAtLoad=true` and
    `KeepAlive` ensure auto-start and crash recovery.
  - Stdout and stderr are written to
    `~/Library/Logs/KiroClaw/gateway.{log,err}`.
- **Other platforms**: install/uninstall return exit code 2 with a
  message pointing to manual setup.

`kiroclaw stop` is service-aware: if the service is active it calls
the platform's stop instead of SIGTERM, so the manager does not
immediately restart the gateway under us.

## Logs Command

`kiroclaw logs [-n LINES] [-f]` tails the gateway log from whichever
source is most appropriate:

1. systemd journal if the system service is installed on Linux. Tries
   unprivileged `journalctl` first; falls back to `sudo journalctl`
   only if the unprivileged probe returns no rows.
2. launchd stdout file if a plist exists on macOS
3. `~/.kiroclaw/gateway.log` for foreground gateways

Uses `os.execvp` so signals (Ctrl+C) propagate naturally to the
underlying `journalctl`/`tail` process.

## Dashboard Self-Update

On gateway startup and every 12 hours, a background task runs `git fetch`
and compares the remote `__version__` with the local version. Only triggers
when the remote version is strictly higher (commits without a version bump
are ignored).

- Topbar shows `📦 v0.1.3` badge — click to check and view changelog
- If newer version found: badge turns into "📦 Update Available"
- Clicking opens a dismissible changelog modal with rendered markdown
- "Update Now" button: `git pull` → rebuild → `os.execv()` restart
- Health indicator shows "Updating…" during the process
- SSE auto-reconnects when the new process starts

## Status Command

`kiroclaw status` queries the running gateway's `/api/status` endpoint
and prints uptime, sessions, messages, tool calls, subagents, crons, lessons.
