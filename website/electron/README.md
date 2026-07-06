# KiroClaw Desktop (Electron)

Native macOS app wrapping the KiroClaw web dashboard. Automatically starts `kiroclaw gateway` and connects to `localhost:5476`.

## Quick Start

```bash
cd electron
npm install
npx electron .
```

The app will:
1. Launch `kiroclaw gateway` if it's not already running
2. Show a loading screen while the backend boots
3. Load the dashboard once ready

## Install as macOS App

Build a native `.app` bundle and install to `/Applications`:

```bash
cd electron
npm install
npx electron-builder --mac --dir
APP_DIR=$([ "$(uname -m)" = "arm64" ] && echo "dist/mac-arm64" || echo "dist/mac")
sudo rm -rf /Applications/KiroClaw.app
sudo cp -R "$APP_DIR/KiroClaw.app" /Applications/KiroClaw.app
```

Launch via Spotlight (Cmd+Space → "KiroClaw"), Dock, or `open /Applications/KiroClaw.app`.
Right-click the Dock icon → Options → Keep in Dock to pin it.

## Build `.dmg`

```bash
npm run dist
```

Output goes to `electron/dist/`.

## Updating

After pulling new code and rebuilding (`brazil-build clean && brazil-build`):

```bash
# Rebuild and reinstall the desktop app
cd electron && npx electron-builder --mac --dir
APP_DIR=$([ "$(uname -m)" = "arm64" ] && echo "dist/mac-arm64" || echo "dist/mac")
sudo rm -rf /Applications/KiroClaw.app
sudo cp -R "$APP_DIR/KiroClaw.app" /Applications/KiroClaw.app

# Restart the gateway (if using Launch Agent)
launchctl stop com.amazon.kiroclaw.gateway
launchctl start com.amazon.kiroclaw.gateway
```

## Uninstall

```bash
# Remove the desktop app
sudo rm -rf /Applications/KiroClaw.app

# Remove the Launch Agent (if configured from main README)
launchctl unload ~/Library/LaunchAgents/com.amazon.kiroclaw.gateway.plist 2>/dev/null
rm -f ~/Library/LaunchAgents/com.amazon.kiroclaw.gateway.plist
```

## Remote Tunnel Mode (Headless CDE)

If the gateway runs on a remote dev desktop (the recommended setup per
`docs/REMOTE_DESKTOP_SETUP.md`), the app can fetch tokens automatically
via SSH instead of reading the local `.local_secret`.

### Prerequisites

1. An SSH tunnel forwarding the remote gateway port to localhost:
   ```bash
   ssh -L 5476:localhost:5476 YOUR_HOST.corp.amazon.com
   ```
   Or use a macOS LaunchAgent (see `docs/persistent-sessions/`).

2. `kiroclaw` installed on the remote host. The default auto-discovers across common
   install layouts — no configuration needed unless you installed somewhere unusual.

### Configure

Remote host settings are **per-port** — each tab can have its own remote host
(or none, for local gateways). Focus the tab you want to configure, then use
**Tab menu → Set Remote Host…** or right-click the tab bar:

1. The modal shows which port it's configuring (e.g. "Remote host for :5476")
2. Enter your remote host's hostname or SSH config alias (e.g. `myhost.example.com` or `clouddesk`)
3. Leave the binary path at the default unless you installed kiroclaw somewhere
   unusual. The default tries, in order:
   - `~/.toolbox/bin/kiroclaw` (toolbox install — recommended)
   - `~/.local/bin/kiroclaw` (install.sh / source install)
   - `~/.kiroclaw-app/.venv/bin/kiroclaw` (one-liner installer venv)
4. Optionally set a **Remote port** if the gateway port on the remote host differs
   from the local tab port (default: same as tab port)
5. Optionally set a **Remote PATH** if kiroclaw needs additional directories
   (default: `~/.toolbox/bin:/usr/bin:/bin`)
6. Click Save. Leave hostname empty to clear (use local token for that port).

**Multi-instance example:**
- Tab 1 on `:5476` — local gateway, no remote host needed
- Tab 2 on `:7778` — SSH tunnel to another host, remote host configured

The app will SSH into the configured remote host and run `kiroclaw token` on
each launch to get a fresh JWT — no manual paste required.

### Token flow (per tab)

```
1. Try local ~/.kiroclaw/.local_secret  →  /api/token/local on tab's port
2. If remote host configured for this port:
   SSH: export PATH=<remotePath> KIROCLAW_PORT=<port>; <bin> token
3. Fallback: show manual token prompt
```

### Menus

| Location | Item | Action |
|----------|------|--------|
| Tab menu / tab bar right-click | Set Remote Host… | Configure hostname for the **focused tab's** port |
| Tab menu / tab bar right-click | Refresh Token (⌘⇧T) | Fetch a fresh token for the **focused tab** |
| Tab menu / tray | Open Config File | Open `config.json` in default editor |

### Tab naming

Tabs default to `[:port]`. You can set a **default name** per port via
**Rename Tab → ☑ Set as default name**. New tabs on that port will use it
automatically. Names are stored in `remoteHosts[port].defaultName`.

### Config file

Settings are persisted via `electron-store` in
`~/Library/Application Support/KiroClaw/config.json`:

```json
{
  "remoteHosts": {
    "5476": {
      "host": "myhost.example.com",
      "binPath": "~/.toolbox/bin/kiroclaw",
      "remotePort": "",
      "remotePath": "",
      "defaultName": "Cloud"
    }
  },
  "sshTimeoutMs": 20000
}
```

Open via **Tab menu → Open Config File** or tray menu.

### Troubleshooting

| Issue | Fix |
|-------|-----|
| "SSH token fetch failed" | Check `ssh YOUR_HOST` works from Terminal |
| "kiroclaw binary not found in any of …" | Install kiroclaw (`pip install kiroclaw`), or set a custom path |
| "command not found: kiro-cli" | Set Remote PATH to include `~/.toolbox/bin` (default does this) |
| "command not found: dirname" | Remote PATH missing `/usr/bin` — reset to default or add it |
| Token fetched but 403 | Gateway may need restart — `ssh host systemctl --user restart kiroclaw` |
| Wrong tab refreshed | Focus the target tab first (use Tab menu, not tray) |

## Notes

- Closing the window hides to tray — right-click the tray icon or Cmd+Q to quit
- External links open in your default browser
- The backend must be set up first (`source setup.sh` from the project root)
