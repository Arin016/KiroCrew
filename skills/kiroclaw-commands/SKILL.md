---
name: kiroclaw-commands
description: Complete CLI reference for KiroClaw commands. Use for help, commands, setup, how to, what can you do, getting started, onboarding.
always: false
triggers: help, commands, setup, gateway, how to, what can you do, getting started, onboard, browse, auth, doctor, cron, artifact, memory, snapshot, eval, security
---
# KiroClaw CLI Reference

## Setup & System

| Command | Description |
|---------|-------------|
| `kiroclaw setup` | Interactive wizard — install agent config and configure credentials |
| `kiroclaw setup --agent-only` | Only install kiro-cli agent config, skip credential prompts |
| `kiroclaw setup --clean` | Fresh install — don't merge from existing config |
| `kiroclaw doctor` | Verify KiroClaw setup (checks all dependencies) |
| `kiroclaw update` | Update KiroClaw to the latest version |
| `kiroclaw --version` | Print installed version |

## Gateway (Server)

| Command | Description |
|---------|-------------|
| `kiroclaw gateway` | Start dashboard + Slack gateway |
| `kiroclaw gateway --slack-only` | Slack only — skip dashboard web server |
| `kiroclaw gateway --no-crons` | Skip cron scheduler |
| `kiroclaw gateway --port 9999` | Override dashboard port |
| `kiroclaw gateway --port auto` | OS-assigned ephemeral port |
| `kiroclaw gateway --no-open` | Don't auto-open dashboard URL in browser |
| `kiroclaw gateway --approval reads` | Auto-approve read-only tools |
| `kiroclaw gateway --approval yolo` | Auto-approve all tools (requires isolated KIROCLAW_HOME) |
| `kiroclaw gateway --approval interactive` | Prompt for every tool (default) |
| `kiroclaw gateway --seed FIXTURE` | Seed $KIROCLAW_HOME from fixture before starting (dev) |
| `kiroclaw gateway --test-mode` | Alias for `--port auto --no-open --json-ready --approval reads` |
| `kiroclaw stop` | Stop a running gateway |
| `kiroclaw stop --port 9999` | Stop gateway on specific port |
| `kiroclaw restart` | Restart gateway (service-aware) |
| `kiroclaw status` | Show runtime stats (uptime, sessions, crons, lessons) |

## Service Management

| Command | Description |
|---------|-------------|
| `kiroclaw service install` | Install and start as system service (sudo on Linux) |
| `kiroclaw service uninstall` | Stop and remove system service |
| `kiroclaw service status` | Show service status (systemctl/launchctl) |
| `kiroclaw logs` | Show gateway logs (last 100 lines) |
| `kiroclaw logs -f` | Follow (tail) live log output |
| `kiroclaw logs -n 50` | Show last N lines |

## Dashboard Access

| Command | Description |
|---------|-------------|
| `kiroclaw token` | Print a dashboard URL with auth token (TTL: 20h) |
| `kiroclaw token --ttl 1h` | Token with custom TTL (e.g. 1h, 30m) |
| `kiroclaw logout` | Revoke all active dashboard sessions |
| `kiroclaw manifest` | Generate Slack app manifest with your alias |
| `kiroclaw manifest --url` | Print one-click Slack app creation URL |

## Chat

| Command | Description |
|---------|-------------|
| `kiroclaw chat` | Interactive chat (REPL mode) |
| `kiroclaw chat -m "message"` | Single message (non-interactive) |
| `kiroclaw chat --model claude-opus` | Use specific model |
| `kiroclaw chat --tui` | Launch TUI instead of REPL |
| `kiroclaw tui` | Launch Terminal UI |
| `kiroclaw tui --yolo` | TUI with auto-approve all tools |
| `kiroclaw tui --session SESSION_KEY` | Resume a specific session |
| `kiroclaw tui --workspace NAME` | Start with a specific workspace |
| `kiroclaw tui --agent NAME` | Start with a specific agent |

## Browsing (Playwright MCP)

Browsing uses **Playwright MCP tools**, not kiroclaw CLI. The `kiroclaw browse` subcommands manage auth only.

| Command | Description |
|---------|-------------|
| `kiroclaw browse setup` | Install Playwright MCP + browsers via AIM |
| `kiroclaw browse auth health` | Check Midway/Kerberos/MCS auth status (prints JSON) |
| `kiroclaw browse auth inject` | Get cookies for Playwright injection (prints JSON) |
| `kiroclaw browse auth federate <url>` | Complete federate SSO for a URL, print final URL |

**Browsing workflow:** Load the `browser-auth` skill (or follow it directly):
1. `kiroclaw browse auth health` — check auth; if unhealthy, tell user to run `kinit -f` / `mwinit -o` / etc.
2. `kiroclaw browse auth refresh` — write Playwright storage state from midway cookies (pre-loads auth into browser context)
3. Use Playwright MCP tools: `browser_navigate`, `browser_snapshot`, `browser_click`, `browser_fill_form`, `browser_take_screenshot`, `browser_evaluate`
4. If you hit `idp.federate.amazon.com`, run `kiroclaw browse auth federate <url>` and navigate to the returned `final_url`

**Note:** Playwright auto-installs during `kiroclaw setup`. On ARM AL2 fallback to `ReadInternalWebsites` MCP.

## Autonomous Task Runner

| Command | Description |
|---------|-------------|
| `kiroclaw run TASK.md` | Run a task spec file (auto-resumes from checkpoint) |
| `kiroclaw run TASK.md --fresh` | Start from scratch, ignore checkpoint |
| `kiroclaw run TASK.md --no-test` | Skip build/test verification after each step |
| `kiroclaw run TASK.md --timeout 3600` | Set global timeout in seconds |
| `kiroclaw run TASK.md --name "My Task"` | Override human-readable task name |

## Subagents

| Command | Description |
|---------|-------------|
| `kiroclaw spawn run "task"` | Spawn a background subagent (wait for result) |
| `kiroclaw spawn run --async "task"` | Fire-and-forget subagent |
| `kiroclaw spawn list` | List active subagents |

## Cron Jobs

| Command | Description |
|---------|-------------|
| `kiroclaw cron list` | List all cron jobs |
| `kiroclaw cron add NAME MESSAGE --every 3600` | Add job with interval (seconds) |
| `kiroclaw cron add NAME MESSAGE --cron "0 9 * * MON-FRI"` | Add job with cron expression |
| `kiroclaw cron add NAME MESSAGE --agent myagent` | Add job for specific agent |
| `kiroclaw cron add NAME MESSAGE --approval-mode auto` | Add job with auto tool approval |
| `kiroclaw cron add NAME MESSAGE --channel C123456` | Post results to Slack channel |
| `kiroclaw cron update JOB_ID --message "new msg"` | Update job message |
| `kiroclaw cron update JOB_ID --agent myagent` | Update job agent |
| `kiroclaw cron update JOB_ID --approval-mode auto` | Set auto-approval |
| `kiroclaw cron update JOB_ID --approval-mode default` | Reset approval to default |
| `kiroclaw cron remove JOB_ID` | Remove a cron job |
| `kiroclaw cron pause JOB_ID` | Pause a cron job |
| `kiroclaw cron resume JOB_ID` | Resume a paused job |
| `kiroclaw cron trigger JOB_ID` | Trigger a job immediately |

## Learning & Memory

| Command | Description |
|---------|-------------|
| `kiroclaw learn list` | List all saved lessons |
| `kiroclaw learn add "rule text"` | Save a lesson (category: knowledge) |
| `kiroclaw learn add "rule text" --category tool` | Save with category (tool/preference/knowledge) |
| `kiroclaw learn add "rule text" --negative "avoid X"` | Save with negative example |
| `kiroclaw learn remove "query"` | Remove lessons matching substring |
| `kiroclaw memory list` | Show semantic memory entries |
| `kiroclaw memory search "query"` | Search episodic memories |
| `kiroclaw memory stats` | Show memory statistics |
| `kiroclaw memory audit` | Scan memory for suspicious content |
| `kiroclaw memory export` | Export all memory to JSON (stdout) |
| `kiroclaw memory export -o file.json` | Export to file |
| `kiroclaw memory import file.json` | Import memory from JSON |
| `kiroclaw memory migrate` | Migrate legacy markdown memory to vector store |
| `kiroclaw consolidate` | List sessions with unconsolidated messages |
| `kiroclaw consolidate SESSION_KEY` | Force consolidate a session (triggers auto-skill extraction) |
| `kiroclaw consolidate --all` | Consolidate all pending sessions |

## Artifacts

LLM-generated UI components (widgets, HTML, markdown, SVG, JSON, text).

| Command | Description |
|---------|-------------|
| `kiroclaw artifact list` | List all artifacts |
| `kiroclaw artifact list --tag ops --kind widget` | Filter by tag and kind |
| `kiroclaw artifact list -q "CR"` | Substring filter on name |
| `kiroclaw artifact show SLUG` | Print artifact content |
| `kiroclaw artifact show SLUG --version 2` | Show specific version |
| `kiroclaw artifact show SLUG --meta` | Show metadata as JSON |
| `kiroclaw artifact save --name "My Widget" --content-file widget.html` | Save new artifact |
| `kiroclaw artifact save --name "X" --content "..." --tags ops,cr` | Save with inline content |
| `kiroclaw artifact update SLUG --content-file widget.html` | Update artifact content |
| `kiroclaw artifact update SLUG --name "New Name" --tags ops` | Rename/retag |
| `kiroclaw artifact versions SLUG` | List version numbers |
| `kiroclaw artifact delete SLUG` | Delete artifact and all versions |

## Agents & Workspaces

| Command | Description |
|---------|-------------|
| `kiroclaw agent list` | List KiroClaw agents |
| `kiroclaw agent create --name NAME` | Create a new agent |
| `kiroclaw agent create --name NAME --kiro-agent kiroclaw --workspace default` | Full options |
| `kiroclaw agent update NAME --kiro-agent new-agent` | Update agent settings |
| `kiroclaw agent delete NAME` | Delete an agent |
| `kiroclaw workspace list` | List workspaces |
| `kiroclaw workspace create --name NAME --dir /path/to/dir` | Create workspace |
| `kiroclaw workspace create --name NAME --copy-from existing` | Copy from existing |
| `kiroclaw workspace update NAME --dir /new/path` | Update workspace dir |
| `kiroclaw workspace delete NAME` | Delete workspace |

## Apps

| Command | Description |
|---------|-------------|
| `kiroclaw app list` | List installed apps |
| `kiroclaw app install /path/to/app-dir` | Install app from local directory (needs app.json) |
| `kiroclaw app enable NAME` | Enable an installed app |
| `kiroclaw app disable NAME` | Disable an installed app |
| `kiroclaw app uninstall NAME` | Uninstall an app |
| `kiroclaw app uninstall NAME --keep-data` | Uninstall but preserve data directory |
| `kiroclaw app info NAME` | Show app details |
| `kiroclaw app init NAME` | Scaffold a new app (kebab-case name) |
| `kiroclaw app init NAME --backend --ui --cron` | Scaffold with backend, UI, and sample cron |

## Configuration

| Command | Description |
|---------|-------------|
| `kiroclaw config get` | Show all config |
| `kiroclaw config get agent.provider` | Get specific value (dot-separated key) |
| `kiroclaw config set dashboard.url http://localhost:5476` | Set a config value (port is the KIROCLAW_PORT env var, not a config key) |
| `kiroclaw config set --file config.json` | Load full config from JSON file |
| `kiroclaw config edit` | Open config in $EDITOR |

## Security & Eval

| Command | Description |
|---------|-------------|
| `kiroclaw security audit` | Scan conversation history for suspicious tool usage |
| `kiroclaw security deny-list` | Show active deny patterns |
| `kiroclaw security events` | Show recent security event log entries (last 20) |
| `kiroclaw security events -n 50` | Show N entries |
| `kiroclaw security verify` | Verify security event log HMAC integrity |
| `kiroclaw eval` | Run smoke test evaluation (~30s) |
| `kiroclaw eval memory_recall_basic` | Run specific scenario by name |
| `kiroclaw eval --all` | Run all scenarios (slow) |
| `kiroclaw eval --judge` | Enable LLM judge scoring |

## Snapshot & Restore

| Command | Description |
|---------|-------------|
| `kiroclaw snapshot` | Create a portable backup of KiroClaw state |
| `kiroclaw snapshot /path/to/dir` | Snapshot to specific output directory |
| `kiroclaw snapshot --keep 7` | Keep N most recent snapshots (default: 7) |
| `kiroclaw snapshot --list` | List existing snapshots |
| `kiroclaw restore` | Restore from most recent snapshot |
| `kiroclaw restore /path/to/snap.tar.gz` | Restore from specific snapshot |
| `kiroclaw restore --mode replace` | Replace mode (default) |
| `kiroclaw restore --mode merge` | Merge mode |
| `kiroclaw restore --dry-run` | Preview without applying |
| `kiroclaw restore --components memory,crons` | Restore specific components only |
| `kiroclaw restore --list-components` | List restorable components |
| `kiroclaw restore --force` | Restore even if gateway is running |

## Slack Commands

### All Allowed Users
| Command | Description |
|---------|-------------|
| `!dashboard` | Get a presigned dashboard link (DM'd to you). Link expires in 5 min; session lasts 1h |
| `!dashboard 2h` | Dashboard link with custom duration (accepts `<N>h` or `<N>m`, max 6h) |
| `/kiroclaw dashboard` | Same via slash command |
| `/kiroclaw help` | List available slash sub-commands |
| `!stop` | Force-halt the current agent turn (bypasses semaphore, cancels active task) |
| `status` | Show runtime stats |
| `ping` | Auto-reply `pong` |
| `cron list` | List cron jobs |
| `run <path>` | Run an autonomous task from a spec file |

### Owner-Only Slash Commands
| Command | Description |
|---------|-------------|
| `/kiroclaw yolo` | Toggle YOLO mode (auto-approve all tool calls) |
| `/kiroclaw agent` | Show agent selector dropdown |
| `/kiroclaw agent <name>` | Switch to named agent |
| `/kiroclaw voice` | Open TTS voice settings modal |
| `/kiroclaw config` | Open config modal |
| `/kiroclaw users` | Open allowed users management modal |
| `/kiroclaw channels` | Open tracked channels modal |
| `/kiroclaw sessions` | List recent sessions with resume/end buttons |

## Environment Variables

| Variable | Purpose | Default |
|----------|---------|---------|
| `KIROCLAW_HOME` | Override config/data directory | `~/.kiroclaw` |
| `KIROCLAW_PORT` | Override dashboard port | `5476` |
| `KIROCLAW_PROJECT_DIR` | Override agent config/skills directory | Auto-detected |
