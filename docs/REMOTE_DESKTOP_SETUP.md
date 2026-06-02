# Running KiroClaw on a Remote Desktop (24/7)

> ⚠️ **Do not enter Critical or Restricted classified data into KiroClaw. Your cloud desktop and laptop are not approved to handle this data classification. See the [Data Handling Standard](https://policy.a2z.com/docs/99/publication).**

Run KiroClaw on an always-on Cloud Desktop so the Slack bot, cron jobs, and task runner work while your laptop sleeps.

## Host Requirements

- **OS**: Amazon Linux 2023 (AL2023) — recommended over AL2 (EOL June 2025)
- **Instance**: m7a.4xlarge (16 vCPU, 64GB RAM) recommended. KiroClaw itself uses ~10GB RAM, but MCP cold starts and heavy tool calls (e.g. `brazil-build`) can cause memory spikes well beyond that. Extra vCPUs help with CPU-intensive tool calls like Brazil builds and parallel subagent execution.
- **Architecture**: x86_64

AL2023 advantages over AL2: modern GLIBC 2.34+, `slack-mcp` support (needs Node 20+), supported until 2028.

## Pre-Setup (SSH into the new host)

### 1. Install basics (for development only)

Only needed if you plan to develop KiroClaw or other packages on this host:

```bash
sudo dnf install -y git tmux
git config --global user.name "Your Name"
git config --global user.email "your-alias@amazon.com"
```

### 2. Install Kerberos (kinit)

Not pre-installed on AL2023:

```bash
sudo dnf install -y krb5-workstation
sudo sed -i '/\[libdefaults\]/a\    default_realm = ANT.AMAZON.COM' /etc/krb5.conf
kinit -f
```

> **Note**: `kinit` (Kerberos) is on a deprecation path — Midway SSH certs via `mwinit` are the replacement. Some tools still require it, but most modern workflows only need `mwinit`.

### 3. Install Midway (mwinit)

`mwinit` generates Midway SSH certificates and cookies needed for `git.amazon.com`, `cr`, `brazil`, and other internal tools. It should be pre-installed on Cloud Desktops, but if missing:

```bash
# Check if already installed
which mwinit || ls /usr/bin/mwinit

# If missing, install via yum/dnf
sudo dnf install -y mwinit 2>/dev/null || sudo yum install -y mwinit

# If not in package repos, download directly
if ! command -v mwinit &>/dev/null; then
    curl -fsSL https://s3.amazonaws.com/com.amazon.aws.midway.software/linux/mwinit -o /tmp/mwinit
    chmod +x /tmp/mwinit
    sudo mv /tmp/mwinit /usr/local/bin/mwinit
fi
```

Generate SSH key (required before first `mwinit`):

```bash
# Generate ECDSA key if not present
[ -f ~/.ssh/id_ecdsa ] || ssh-keygen -t ecdsa -N "" -f ~/.ssh/id_ecdsa
```

Authenticate:

```bash
mwinit -o    # OTP mode (required on remote hosts — PIN + YubiKey long-press)
```

> **Tip**: Always use `mwinit -o` on Cloud Desktops. The `-o` flag enables OTP mode — `mwinit` without `-o` tries U2F which doesn't work on remote hosts.

### 4. Install Builder Toolbox

Builder Toolbox is the package manager for Amazon's internal developer tools (brazil, cr, ada, etc.). It should be pre-installed on Cloud Desktops, but if missing:

```bash
# Check if already installed
toolbox --version
```

If `toolbox` is missing, install it per the official Builder Toolbox docs, then
add `~/.toolbox/bin` to your `PATH`:

```bash
export PATH="$HOME/.toolbox/bin:$PATH"
echo 'export PATH=$HOME/.toolbox/bin:$PATH' >> ~/.zshrc
source ~/.zshrc 2>/dev/null || source ~/.bashrc
```

Install essential developer tools (only needed for development — skip if you just want to run KiroClaw):

```bash
toolbox install brazilcli cr ada
```

**Alternative for developers — AxE Initialize** (installs everything at once, ~20 min):

```bash
toolbox install axe
axe init builder-tools    # installs brazil, cr, ada, and all standard builder tools
source ~/.zshrc 2>/dev/null || source ~/.bashrc
```

> **Note on Cloud Dev Machines (CDMs)**: CDMs come with `axe` pre-installed. Skip the toolbox bootstrap and go straight to `axe init builder-tools`.

### 5. Install kiro-cli

kiro-cli is the AI agent backend that KiroClaw uses for LLM inference and tool execution:

```bash
toolbox install kiro-cli
kiro-cli login    # opens browser for AWS SSO — grant permissions
```
> **Tip**: `kiro-cli login` will provide a number of login options. Check `Cloud Desktop` Section in https://docs.hub.amazon.dev/kiro/user-guide/getting-started-cli/#log-in-to-kirocli for instruction on how to select SSO option.  
> **Tip**: `kiro-cli login` prints a URL and short code — open the URL in your **local browser** and confirm. This works over plain SSH without a graphical desktop.

## Install KiroClaw

Install the Python backend (`pip`) and build the React dashboard (`npm`),
exactly as on a local machine — see the [README](../README.md#quick-start) for
the full walkthrough:

```bash
# 1. Clone and install the backend
git clone https://github.com/YOUR_ORG/kiroclaw.git
cd kiroclaw

# 2. Build the frontend bundle
cd website && npm install && npm run build && cd ..
cp -r website/dist src/kiro_claw/static/dist

# 3. Install the backend (bundles the dashboard)
pip install .

# 4. Configure
kiroclaw setup
```

This installs the `kiroclaw` command onto your `PATH`. See the
[README](../README.md) for the agent backend and Ollama setup.

## Complete kiro-cli Login

`setup.sh` invokes `kiro-cli login` automatically. It prints a URL and a short code — open the URL in your **local browser** and confirm the code. This works over plain SSH without a graphical desktop.

If the browser-based flow fails, see [Appendix: DCV Remote Desktop](#appendix-dcv-remote-desktop-fallback-for-kiro-cli-login).

After setup:
```bash
kiroclaw doctor            # verify everything
kiroclaw setup             # configure Slack tokens (optional)
```

## Run 24/7 (`kiroclaw service install`)

The simplest path is the built-in installer. It registers a
system-level systemd unit (Linux) or launchd LaunchAgent (macOS), so
the gateway survives SSH disconnects, auto-restarts on crash, and
auto-starts on boot (Linux) or user login (macOS). On Linux the
install step prompts for sudo once to write the unit file and run
`systemctl`; the gateway itself runs as your user, not root:

```bash
kiroclaw service install
```

Manage:

```bash
kiroclaw service status      # check status
kiroclaw logs -f             # tail live logs
kiroclaw stop                # stop the service
kiroclaw restart             # restart the service (atomic on systemd; unload+load on launchd)
kiroclaw service uninstall   # remove the unit / plist
```

**Boot survival** is handled by the unit's `WantedBy=multi-user.target`
plus `enable --now`. Nothing extra needed. The gateway will start on
cloud desktop reboot.

**Sudo scope:** `kiroclaw service install` only runs `sudo tee`
(to write the unit file under `/etc/systemd/system/`) and `sudo
systemctl ...` (daemon-reload, enable, restart). No kiroclaw / MCP /
LLM code path is invoked under sudo. Once started, the gateway runs
as `User=$USER Group=$(id -gn)` — not root.

**Alternative — manual recipe** (e.g. if you want to customize the
unit, or for hosts where the wrapped install isn't appropriate):

```bash
# Resolve the actual binary path at install time
KIROCLAW_BIN=$(command -v kiroclaw 2>/dev/null || echo "$HOME/.toolbox/bin/kiroclaw")

sudo tee /etc/systemd/system/kiroclaw.service << EOF
[Unit]
Description=KiroClaw AI Agent Gateway
After=network-online.target
Wants=network-online.target
StartLimitBurst=5
StartLimitIntervalSec=300

[Service]
Type=simple
User=$(whoami)
ExecStart=$KIROCLAW_BIN gateway
Restart=on-failure
RestartSec=10
WorkingDirectory=$HOME
Environment=HOME=$HOME
Environment=PATH=$(dirname $KIROCLAW_BIN):$HOME/.toolbox/bin:$HOME/.local/bin:$HOME/.nvm/versions/node/$(node -v 2>/dev/null || echo v16.20.2)/bin:/usr/local/bin:/usr/bin
Environment=KIROCLAW_PROJECT_DIR=$(git -C "$(dirname "$(readlink -f "$KIROCLAW_BIN")")" rev-parse --show-toplevel 2>/dev/null || echo "")

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable kiroclaw
sudo systemctl start kiroclaw
```

The system-level unit lets you tail logs with
`sudo journalctl -u kiroclaw -f` or restart with `sudo systemctl restart kiroclaw`.

**Alternative — tmux** (if you don't want a service at all):

```bash
tmux new -s kiroclaw
kiroclaw gateway
# Ctrl+B, D to detach
# Reconnect: tmux attach -t kiroclaw
```

tmux survives SSH disconnect but does **not** auto-restart on crash or
auto-start on reboot. Use the service install path unless you have a
specific reason not to.

## Auto-Renew Midway

Midway cookies expire ~10 hours. `mwinit -o` requires interactive input (PIN + YubiKey), so it cannot be fully automated via cron. Run it manually when your cookie expires:

```bash
mwinit -o -s
```

> **Tip**: KiroClaw itself doesn't need Midway — it uses kiro-cli SSO tokens. Midway is only needed for `cr` (code reviews), `brazil ws sync`, and other Brazil/toolbox commands.

## Sync KiroClaw to a New Host

When setting up a new dev-dsk (or replacing a dead one), sync your local KiroClaw state so the remote instance has your memories, preferences, lessons, and agent configs from day one.

### What to Sync

| Category | Path | Why |
|---|---|---|
| **Memory** | `~/.kiroclaw/workspace/memory/` | Preferences, projects, history — the agent's long-term knowledge of you |
| **Databases** | `~/.kiroclaw/memory.db`, `memory.db-wal`, `memory.db-shm`, `memory_index.db` | Episodic & semantic memory (SQLite + WAL for complete state) |
| **Config** | `~/.kiroclaw/config.json` | KiroClaw settings (Slack tokens, model prefs) |
| **Lessons** | Stored in `memory.db` | Learned corrections that override default behavior |
| **Task specs** | `~/.kiroclaw/tasks/` | Saved task runner specs |
| **Skills** | `~/.kiroclaw/skills/` | Custom skill definitions |
| **Hooks** | `~/.kiroclaw/hooks/` | Webhook listener configs |
| **Cron jobs** | `~/.kiroclaw/crons.json` | Scheduled recurring jobs |
| **Dotfiles** | `~/.gitconfig`, `~/.bashrc`, `~/.zshrc` | Shell & git config |

> **Why sync WAL files?** SQLite uses Write-Ahead Logging — recent writes go to `memory.db-wal` before being checkpointed into `memory.db`. Without the WAL, the remote gets a stale snapshot missing your latest memories and lessons.

### What NOT to Sync

- `~/.kiroclaw/sessions/` — optional; sync if you want chat history on remote (sync-to-remote.sh includes this)
- `~/.kiroclaw/session_pid_*.txt` — process tracking files, host-specific
- `~/.kiroclaw/audit.log`, `security_events.jsonl` — large logs, not needed on new host
- `~/.kiroclaw/.env`, `.local_secret`, `sel_hmac.key` — secrets, regenerated on first run
- `~/brazil-pkg-cache/`, `~/.brazil/` — rebuilt by `brazil workspace use`
- Brazil workspace `src/` packages — git repos, re-clone with `brazil ws use -p <pkg>`

### Quick Sync Script

A standalone sync script is available at [`scripts/sync-to-remote.sh`](../scripts/sync-to-remote.sh):
- Custom dashboard port support (for multi-host setups)
- Atomic SQLite backup for complete memory state
- Session sync for dashboard chat history restore
- Patches `config.json` with remote-specific settings (`auto_open_browser=false`)
- `--dry-run` flag to preview without transferring

Run from your **local machine**:

```bash
# Basic usage (edit DEFAULT_HOST in the script, or pass as arg)
scripts/sync-to-remote.sh dev-dsk-yourname-2b-XXXXX.us-west-2.amazon.com

# Custom port (if running multiple remote hosts)
scripts/sync-to-remote.sh dev-dsk-host1.us-west-2.amazon.com 7779

# Preview what would sync
scripts/sync-to-remote.sh --dry-run

# Help
scripts/sync-to-remote.sh --help
```

> **Note on sessions**: The script syncs `~/.kiroclaw/sessions/` by default so your chat history appears on the remote dashboard. If you prefer a clean slate, remove the sessions step from the script.

### After Syncing

On the new host:
```bash
# 1. Install KiroClaw (if not already)
source setup.sh

# 2. Verify
kiroclaw doctor

# 3. Re-authenticate (tokens are host-specific)
kiro-cli login
kiroclaw setup          # re-enter Slack tokens if needed

# 4. Start the gateway
sudo systemctl start kiroclaw   # or: tmux new -s kiroclaw && kiroclaw gateway
```

### Ongoing Sync (Optional)

If you develop on both local and remote, keep memories in sync with a cron alias:

```bash
alias sync-claw='rsync -avz ~/.kiroclaw/workspace/memory/ YOUR_HOST:~/.kiroclaw/workspace/memory/ && rsync -avz ~/.kiroclaw/memory.db ~/.kiroclaw/memory_index.db YOUR_HOST:~/.kiroclaw/'
```

## Access Dashboard via SSH Tunnel

Instead of DCV, you can use an SSH tunnel to view the KiroClaw dashboard in your local browser:

```bash
ssh -L 7777:localhost:7777 YOUR_HOST.us-west-2.amazon.com
```

Then open `http://localhost:7777` in your local browser.

**Custom port** — if you configured a non-default port (e.g., via `sync-to-remote.sh HOST 7779`), match the tunnel:

```bash
# Tunnel-only (no shell, stays in foreground)
ssh -N -L 7779:localhost:7779 YOUR_HOST.us-west-2.amazon.com

# Background tunnel (returns immediately)
ssh -fN -L 7779:localhost:7779 YOUR_HOST.us-west-2.amazon.com
```

To make this automatic on every SSH connection, add to `~/.ssh/config` on your local machine:

```
Host YOUR_HOST.us-west-2.amazon.com
    LocalForward 7777 localhost:7777
```

Now a plain `ssh YOUR_HOST.us-west-2.amazon.com` will always set up the tunnel.

This works on macOS, Linux, and Windows (OpenSSH is built into Windows 10+ — the SSH config file lives at `%USERPROFILE%\.ssh\config`).

### Persistent Tunnel (macOS LaunchAgent)

The above approaches require an active terminal session — if the terminal closes, the tunnel dies. To keep the tunnel running permanently (survives reboots, auto-reconnects on network drops):

```bash
DEVDESKTOP="YOUR_HOST.us-west-2.amazon.com"

cat > ~/Library/LaunchAgents/com.kiroclaw.tunnel.plist << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.kiroclaw.tunnel</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>-c</string>
        <string>while true; do echo "\$(date): Connecting..." >> /tmp/kiroclaw-tunnel.log; ssh -N -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes -o ConnectTimeout=10 -L 7777:localhost:7777 $DEVDESKTOP 2>> /tmp/kiroclaw-tunnel.log; echo "\$(date): Disconnected (exit \$?)" >> /tmp/kiroclaw-tunnel.log; sleep 15; done</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>StandardErrorPath</key>
    <string>/tmp/kiroclaw-tunnel.err</string>
</dict>
</plist>
EOF

launchctl bootout gui/$(id -u)/com.kiroclaw.tunnel 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kiroclaw.tunnel.plist
```

Manage:

```bash
tail -f /tmp/kiroclaw-tunnel.log                                    # check status
launchctl bootout gui/$(id -u)/com.kiroclaw.tunnel                  # stop
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.kiroclaw.tunnel.plist  # start
```

> **Note**: The tunnel auto-reconnects when SSH drops (network change, sleep/wake, etc.) but cannot recover from expired Midway auth. Run `mwinit -o` and the tunnel will self-heal within 15 seconds.

### Raycast Script (MacOS - zsh)
If you use Raycast scripts, you can use the below snippet to start an ssh tunnel and open a web page with the KiroClaw token directly from a Raycast Script. Replace `"YOUR_HOST.us-west-2.amazon.com"` with your Dev Desktop hostname.

```zsh
#!/bin/zsh -e
# Required parameters:
# @raycast.schemaVersion 1
# @raycast.title Open Kiroclaw
# @raycast.mode compact
# Optional parameters:
# @raycast.icon 🐉
# @raycast.packageName KiroClaw Utils
# Documentation:
# @raycast.description Get token and start KiroClaw
DEV_DESKTOP="YOUR_HOST.us-west-2.amazon.com"
if lsof -i :7777 -sTCP:LISTEN > /dev/null 2>&1; then
  echo "Tunnel already opened, accessing KiroClaw"
  URL="$(ssh "${DEV_DESKTOP}" 'source ~/.zshrc; kiroclaw token')"
  open "$URL"
else
  ssh -fNT -L 7777:localhost:7777 "${DEV_DESKTOP}"
  echo "Tunnel opened, accessing KiroClaw!"
  URL="$(ssh "${DEV_DESKTOP}" 'source ~/.zshrc; kiroclaw token')"
  open "$URL"
fi
```

## Troubleshooting

| Issue | Fix |
|---|---|
| `kinit: command not found` | `sudo dnf install -y krb5-workstation` |
| `kinit: Configuration file does not specify default realm` | `kinit -f user@ANT.AMAZON.COM` or set default_realm in `/etc/krb5.conf` |
| `kinit: KDC reply did not match expectations` | Realm must be uppercase: `ANT.AMAZON.COM` not `ant.amazon.com` |
| `mwinit: command not found` | See [Install Midway](#3-install-midway-mwinit) — download from S3 or install via dnf |
| `mwinit` fails without `-o` | Always use `mwinit -o` on remote hosts (OTP mode). U2F doesn't work over SSH |
| `toolbox: command not found` | See [Install Builder Toolbox](#4-install-builder-toolbox) — install per the Builder Toolbox docs and add `~/.toolbox/bin` to `PATH` |
| `brazil: command not found` | `toolbox install brazilcli && source ~/.zshrc` |
| DCV connects but no UI | `sudo dnf groupinstall -y "Desktop" && sudo systemctl restart dcvserver` |
| `MATE Desktop` / `Server with GUI` not available | Use `dnf group list` to find available groups — AL2023 Cloud Desktops use `Desktop` |
| Midway expired overnight | `mwinit -o -s` (interactive — cannot be automated via cron) |
| `AcpTimeoutError` | `kiro-cli whoami` to check auth, `kiro-cli login` to re-authenticate |
| Service won't start | `sudo journalctl -u kiroclaw -n 50` to check logs |
| Service keeps restarting | Check `systemctl status kiroclaw` for exit code, then check logs |
| Dashboard shows Ollama offline (✗) but it was working before | The `ollama.com` install script creates a systemd service (`User=ollama`) that conflicts with KiroClaw's own process management. The systemd user can't find models in your `~/.ollama/models/`. Fix: `sudo systemctl disable --now ollama` — KiroClaw manages Ollama itself via `ollama serve` subprocess |
| `ollama list` shows no models but files exist in `~/.ollama/` | Same systemd user mismatch. The `ollama` system user looks in `/usr/share/ollama/.ollama/models/` instead of your home directory. Either disable the systemd service (recommended) or set `Environment="OLLAMA_MODELS=/home/YOUR_USER/.ollama/models"` in `/etc/systemd/system/ollama.service` |

## Appendix: DCV Remote Desktop (fallback for kiro-cli login)

If the SSH + local browser flow doesn't work, you can complete `kiro-cli login` via a graphical DCV session.

### 1. Install desktop environment on the remote host (SSH)

```bash
sudo dnf groupinstall -y "Desktop"
sudo systemctl restart dcvserver
```

### 2. Get the DCV connection script (on your laptop)

The script already exists on EC2 instances. SCP it down:

```bash
scp YOUR_HOST.us-west-2.amazon.com:/usr/share/dcv/cdd/dcv-cdd.py ~/Downloads/
chmod +x ~/Downloads/dcv-cdd.py
```

Also install the [NICE DCV native client](https://download.nice-dcv.com/).

See https://w.amazon.com/bin/view/Users/aowsmith/nicedcv/ for full details.

### 3. Connect from your laptop

```bash
# Native client (requires DCV Viewer installed)
python3 ~/Downloads/dcv-cdd.py connect YOUR_HOST.us-west-2.amazon.com

# Web client (opens in browser, no install needed)
python3 ~/Downloads/dcv-cdd.py connect -w YOUR_HOST.us-west-2.amazon.com
```

Other useful commands:
```bash
dcv-cdd.py create-session HOST   # create session without connecting
dcv-cdd.py close-session HOST    # close existing session
dcv-cdd.py share HOST            # get a shareable connection link
```

### 4. Complete kiro-cli login in the DCV session

Open a terminal in the DCV desktop and run:
```bash
kiro-cli login    # opens browser for AWS SSO — grant permissions
```

> **Note**: After the one-time `kiro-cli login`, you can do everything over SSH. DCV is only needed when the SSO token expires and the SSH + local browser flow fails.
