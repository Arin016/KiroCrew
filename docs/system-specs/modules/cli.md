# CLI Module

Last Updated: 2026-07-09 (cloud launch resume safety)

## Overview

The CLI module (`kiro_claw/cli.py`) provides the `kiroclaw` command using stdlib `argparse`.

## Project Directory Detection

At startup, `main()` auto-detects the project root and sets `KIROCLAW_PROJECT_DIR`:

1. If `KIROCLAW_PROJECT_DIR` env var is already set, use it
2. Walk up from CWD looking for a directory with both `skills/` and `src/kiro_claw/` (`_PROJECT_MARKERS`). The project-level `agents/` dir was removed when agent config was consolidated into `src/kiro_claw/config/` (commit bbbc1f6e), so the marker no longer references it — a stale `agents/` requirement left detection (and the dashboard changelog) silently broken.
3. Read saved path from `~/.kiroclaw/project_dir` (written by `kiroclaw setup`); the saved path is re-validated against the same markers

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
| `kiroclaw stop` | Stop a running gateway (service-aware: stops the systemd/launchd service if active, otherwise terminates the gateway found by a cross-platform port lookup — lsof on POSIX, netstat on Windows). Pass `--port N` to bypass the service short-circuit and target a specific gateway. |
| `kiroclaw restart` | Restart a running gateway (service-aware: restarts the systemd/launchd service if active, otherwise terminates the foreground gateway and respawns it detached). Pass `--port N` to bypass the service short-circuit and target a specific gateway. |
| `kiroclaw service install` | Install gateway as a system-level systemd service (Linux, requires sudo for `tee` + `systemctl` only) or launchd LaunchAgent (macOS, no sudo). Auto-restarts on crash, auto-starts on boot. |
| `kiroclaw service uninstall` | Stop and remove the systemd unit / launchd plist. |
| `kiroclaw service status` | Show service status (`systemctl status` or `launchctl list`). No sudo required. |
| `kiroclaw logs` | Tail gateway logs from the systemd journal, launchd stdout file, or `~/.kiroclaw/gateway.log`. |
| `kiroclaw logs -f` | Follow logs live (long-running tail). |
| `kiroclaw cloud launch/list/status/connect/stop/start/destroy/iam-policy/doctor` | Provision, connect to, and manage a KiroClaw EC2 instance in the user's AWS account. |
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

After credentials, `kiroclaw setup` offers to add `127.0.0.1 kiroclaw.localhost` to the system hosts file so the dashboard is accessible at `http://kiroclaw.localhost:5476`:

- **macOS/Linux**: Uses `sudo tee -a /etc/hosts` for safe append

Skipped if `kiroclaw.localhost` is already present or user declines.

## Cloud Command

`kiroclaw cloud` is a human installer/control-plane surface for running
KiroClaw on the user's own AWS EC2 instance. Provisioning and teardown are not
LLM-facing tools. AWS credentials are resolved by the AWS CLI; KiroClaw stores
only profile, region, and the most recent instance tag in `cloud.json`.

`kiroclaw cloud launch` runs a six-step wizard: check AWS reachability, explain
permissions, choose whether to keep an existing deployment or create a new one,
choose an instance size when creating a new stack, deploy or resume the
CloudFormation stack, sign in the remote `kiro-cli`, and open the dashboard
through SSM port forwarding. Launch is resume-safe by default: if `cloud.json`
contains a `last_tag` whose stack still exists in the same saved profile/region,
rerunning interactive `launch` offers to keep/resume that stack or create a new
installation. If `cloud.json` is missing or stale, launch discovers existing
`kiroclaw-*` CloudFormation stacks with `cloudformation:ListStacks` and offers a
choice to resume one or create a new installation. `kiroclaw cloud launch --new`
is the explicit escape hatch for creating a separate new stack. `--yes` keeps a
single or saved existing stack; if multiple unsaved stacks exist it fails closed
instead of choosing one arbitrarily. For a new launch, the generated tag is
written to `cloud.json` before the long CloudFormation deploy starts, so an
interrupted provisioning run can be found on the next launch attempt.

Launch and connect require the local AWS Session Manager plugin for
`AWS-StartPortForwardingSession`. If `session-manager-plugin` is missing,
`cloud launch` prompts to install AWS's official package for the current local
platform (macOS `.pkg`, Debian/Ubuntu `.deb`, or RPM Linux `.rpm`) before the
wizard reaches sign-in/dashboard tunneling. `--yes` accepts this installer
prompt. `cloud connect` performs the same check and installer prompt before
opening the dashboard tunnel. If installation is declined or fails, the command
exits non-zero and tells the user to retry after fixing the local prerequisite.

The instance-size picker supports arrow keys in an interactive terminal
(`↑`/`↓`, `j`/`k`, digit shortcuts, Enter to select) and falls back to the
numbered prompt for non-TTY input. Ctrl-C must interrupt prompts and long AWS
subprocesses; unhandled cloud-command interrupts return exit code 130.

Remote Kiro sign-in prefers the device-code flow over SSM. The launcher starts
`kiro-cli login --use-device-flow` as a background process on the instance,
captures the URL/code from its log, and leaves that same process alive while the
wizard polls for completion. It must not kill that process after scraping the
prompt or start a second hidden device-code flow. If device-code startup does
not produce an actionable URL, launch falls back to the Google/GitHub callback
flow automatically: it starts `kiro-cli login` on the instance with FIFO-backed
stdin, captures the printed loopback callback port, opens an
`AWS-StartPortForwardingSession` from the same local port to the remote port,
sends the Enter continuation back to the remote CLI, then opens or prints the
local browser URL. The temporary callback tunnel is closed after the sign-in
poll completes. In headless local terminals, browser auto-open is skipped and
the URL is printed for manual opening.

`kiroclaw cloud connect` mints a dashboard token over SSM, opens an
`AWS-StartPortForwardingSession`, waits for the local tunnel port to accept TCP
connections, and opens or prints the local dashboard URL. If the tunnel port
does not become reachable, the command reports failure, does not present the
dashboard URL as usable, and does not keep a dead tunnel process open. If final
dashboard opening fails during `cloud launch`, the instance remains running but
launch returns non-zero and tells the user to rerun `kiroclaw cloud connect`
after fixing the local SSM tunnel issue.

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
| `KIROCLAW_PORT` | Override dashboard port (default `5476`, validated as int at CLI startup) |
| `KIROCLAW_PROJECT_DIR` | Override agent config/skills directory |
| `KIROCLAW_WORKSPACE` | Override workspace root directory |

For local dev:
- **macOS/Linux**: `bin/kiroclaw` (POSIX shell wrapper); `source setup.sh` adds `bin/` to PATH

The wrapper sets `KIROCLAW_PROJECT_DIR` and routes to the right runtime based on install type:

- **One-liner install** (`install.sh` clones the repo into `~/.kiroclaw-app/`): if a sibling `.venv/bin/kiroclaw` exists, the wrapper execs it directly.
- **pip editable install** (`pip install -e .`): the console_scripts entry point resolves directly.

## Setup Scripts (First-Time Bootstrap)

`setup.sh` (macOS/Linux) auto-installs all dependencies from scratch using public tooling only.

> **Note:** Windows is not supported.

**Install order:**
1. Node.js (via `ensure-node.sh`)
2. Optional tools (git-lfs, ffmpeg for voice)
3. kiro-cli (`npm i -g`)
4. kiro-cli login (guided authentication)
5. Frontend build (`npm install && npm run build`)
6. Backend build (`pip install -e .`)
7. PATH setup + shell profile persistence
8. `kiroclaw setup --agent-only` (install kiro-cli agent config)
9. Optional Slack credential configuration

Each step checks if the tool is already installed and skips if present.

## Doctor Checks

1. `kiro-cli` binary in PATH
2. Project directory and git repo
3. Agent config installed
4. Config values (provider, model, approval mode, dashboard port)
5. **MCP tools**: `@kiroclaw-cron` and `@kiroclaw-core` in `tools`, `allowedTools`, and `mcpServers` — auto-fixes missing entries
6. **Global mcp.json**: kiroclaw MCP servers present with valid binary paths — auto-fixes stale paths
7. **Python environment**: checks Python 3.9+ availability and dependency installation
8. **Vector memory**: Ollama server health and embedding model status
9. Slack credentials (optional)
10. kiro-cli connectivity
11. Gateway running status

## Update Command

`kiroclaw update` pulls the latest source and rebuilds:

1. `git pull` from `KIROCLAW_PROJECT_DIR`
2. Rebuilds frontend via `build-frontend.sh` (non-fatal on failure)
3. Reinstalls backend via `pip install -e .`

## Stop Command

`kiroclaw stop [--port PORT]` stops a running gateway:

1. If a systemd/launchd service is active **and** the caller did not pass
   `--port` explicitly (see Service Management), stop it via the service
   manager and return — without this branch, SIGTERM-by-port would be
   racing the manager's auto-restart.
2. Otherwise (no service active, or `--port` was passed explicitly to
   target a non-default dev gateway): `platform_compat.find_listening_pids(port)`
   to find PIDs — `lsof -ti TCP:{port} -sTCP:LISTEN` on POSIX, `netstat -ano`
   parsing on Windows (there is no `lsof` there; this previously made
   `kiroclaw stop` a no-op on Windows). `listening_pid_tool_available()`
   distinguishes "no listener" from "lookup tool missing".
3. `platform_compat.process_command_line(pid)` to verify it's a KiroClaw process —
   `/proc/<pid>/cmdline` (Linux), `ps -o command=` (macOS), `Win32_Process.CommandLine`
   via WMI (Windows). The Windows venv `kiroclaw.exe` re-execs `python.exe`, so the
   match is on the command line (`-m kiro_claw gateway` / `\Scripts\kiroclaw.exe gateway`),
   not the image name.
4. Terminate each verified PID: `os.kill(SIGTERM)` on POSIX; `taskkill /T /F`
   (via `platform_compat.kill_process_tree`) on Windows so the gateway's detached
   children are reaped too. Liveness is probed with `platform_compat.pid_exists`
   (a raw `os.kill(pid, 0)` would *terminate* the process on Windows).
5. Waits up to 1s for exit.
6. SEL audit event logged.

## Restart Command

`kiroclaw restart [--port PORT]` restarts a running gateway. Mirrors
`stop`'s service-aware structure:

1. If a systemd/launchd service is active **and** the caller did not
   pass `--port` explicitly, ask the platform to restart it. On Linux:
   `sudo systemctl restart kiroclaw.service` (single
   atomic operation, smaller down-window than stop+start, and the
   supervisor stays in charge of the lifecycle the whole time). On
   macOS: `launchctl unload <plist>` + `launchctl load <plist>` (no
   `-w`, so persistent enable state is unchanged). The deprecated
   `launchctl restart` is avoided because under `KeepAlive` it behaves
   like `stop` (SIGTERM + immediate respawn) and never re-reads the plist.
2. Otherwise (foreground gateway, no service, or `--port` passed
   explicitly to target a non-default dev gateway):
   - `platform_compat.find_listening_pids(port)` (lsof on POSIX, netstat
     on Windows) to detect a running gateway. If found — OR if the lookup
     tool is absent (`not listening_pid_tool_available()`, so a missing
     tool is not mistaken for a dead gateway) — run the existing `_stop`
     kill-by-port path. If not (e.g. the user runs `restart` after a
     crash), skip the stop step rather than erroring — the user expects to
     end up with a running gateway either way. The `_stop` call is wrapped
     in a `try / except SystemExit` so a TOCTOU race (gateway exits between
     the listener check and `_stop`'s own lookup → `_stop` calls
     `sys.exit(1)`) does not abort the restart before the spawn.
   - Spawn a detached `kiroclaw gateway` via `subprocess.Popen`, stdin set
     to `subprocess.DEVNULL`, and stdout + stderr redirected to
     `~/.kiroclaw/gateway.log` (the same file the `kiroclaw logs` command
     tails for foreground gateways). Detach is per-platform: POSIX uses
     `start_new_session=True`; Windows uses `creationflags=DETACHED_PROCESS
     | CREATE_NEW_PROCESS_GROUP` (there is no setsid) — both via
     `platform_compat`. The shell returns immediately and the user can
     follow logs via `kiroclaw logs -f`.
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
