# KiroClaw Persistent Sessions

> **Prerequisite**: Complete [REMOTE_DESKTOP_SETUP.md](../REMOTE_DESKTOP_SETUP.md) first.

Upgrades the tmux-based setup to a fully persistent configuration:
- Gateway auto-restarts on crash and auto-starts on boot (systemd)
- SSH tunnel from your Mac auto-reconnects after laptop sleep (LaunchAgent)

## Dev Desktop Setup

### Phase 1: Enable systemd user services (one-time, requires sudo)

AL2 dev desktops don't have systemd user services enabled by default. AL2023 desktops typically have this working out of the box — skip to Phase 2 if `systemctl --user status` works.

Run `systemctl --user status` — if it returns without error, skip to Phase 2.

Otherwise, run these commands:

```bash
sudo tee /etc/systemd/system/user@$(id -u).service << 'EOF'
[Unit]
Description=User Manager for UID %i
After=systemd-user-sessions.service
After=user-runtime-dir@%i.service
Wants=user-runtime-dir@%i.service

[Service]
LimitNOFILE=infinity
LimitNPROC=infinity
User=%i
PAMName=systemd-user
Type=notify
PermissionsStartOnly=true
ExecStartPre=/bin/loginctl enable-linger %i
ExecStart=/usr/lib/systemd/systemd --user
Slice=user-%i.slice
KillMode=mixed
Delegate=yes
TasksMax=infinity
Restart=always
RestartSec=15

[Install]
WantedBy=default.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable user@$(id -u).service
sudo systemctl start user@$(id -u).service
```

Verify: `systemctl --user status` should now return without error.

### Phase 2: Install KiroClaw service

```bash
cd docs/persistent-sessions
./setup.sh
```

Or manually:

```bash
mkdir -p ~/.config/systemd/user
cp kiroclaw.service ~/.config/systemd/user/
sed -i "s/%u/$(whoami)/g" ~/.config/systemd/user/kiroclaw.service
systemctl --user daemon-reload
systemctl --user enable kiroclaw
systemctl --user start kiroclaw
```

Verify: `systemctl --user status kiroclaw` should show `active (running)`.

## Mac Setup

```bash
# Copy the plist
scp USER@HOST:~/KiroClaw/src/KiroClaw/docs/persistent-sessions/com.kiroclaw.tunnel.plist ~/Library/LaunchAgents/

# Replace placeholder with your dev desktop
sed -i '' 's|ALIAS@DEV_DESKTOP_HOSTNAME|USER@HOST|g' ~/Library/LaunchAgents/com.kiroclaw.tunnel.plist

# Load the tunnel
launchctl load ~/Library/LaunchAgents/com.kiroclaw.tunnel.plist
```

Verify: `curl -s http://localhost:8765/api/status`

Dashboard: http://localhost:8765

## Gotchas

- **sudo broken?** `/etc/sudo.conf` may have wrong ownership on some dev desktops. Run sudo commands from a fresh SSH session, not from kiro-cli or KiroClaw.
- **Kill tmux first** — can't have two gateways on port 8765. Run `tmux kill-session -t kiroclaw` before Phase 2.
- **D-Bus connection error?** Run `export XDG_RUNTIME_DIR=/run/user/$(id -u)` then retry.
- **Laptop sleep** — the LaunchAgent tunnel includes `ServerAliveInterval=30` and `KeepAlive=true`. macOS auto-restarts it after sleep/network change. Reconnect takes ~30 seconds.

## Managing

| Action | Command |
|---|---|
| Gateway status | `systemctl --user status kiroclaw` |
| Gateway restart | `systemctl --user restart kiroclaw` |
| Gateway logs | `journalctl --user -u kiroclaw -f` |
| Tunnel logs (Mac) | `cat /tmp/kiroclaw-tunnel.log` |
| Tunnel restart (Mac) | `launchctl kickstart -k gui/$(id -u)/com.kiroclaw.tunnel` |
| Uninstall gateway | `systemctl --user disable --now kiroclaw` |
| Uninstall tunnel | `launchctl unload ~/Library/LaunchAgents/com.kiroclaw.tunnel.plist` |

## Files

| File | Purpose |
|---|---|
| `kiroclaw.service` | systemd unit file for dev desktop |
| `com.kiroclaw.tunnel.plist` | macOS LaunchAgent for SSH tunnel |
| `setup.sh` | Automated Phase 2 setup |
