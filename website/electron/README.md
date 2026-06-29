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

1. An SSH tunnel forwarding port 5476 from the remote host:
   ```bash
   ssh -L 5476:localhost:5476 YOUR_HOST.corp.amazon.com
   ```
   Or use a macOS LaunchAgent (see `docs/persistent-sessions/`).

2. `kiroclaw` installed on the remote host. The default auto-discovers across common
   install layouts — no configuration needed unless you installed somewhere unusual.

### Configure

On first launch (or via tray icon → **Set Remote Host…**):

1. Enter your remote host's hostname (e.g. `myhost.example.com`)
2. Leave the binary path at the default unless you installed kiroclaw somewhere
   unusual. The default tries, in order:
   - `$HOME/.local/bin/kiroclaw` (install.sh / source install)
   - `$HOME/.kiroclaw-app/.venv/bin/kiroclaw` (one-liner installer venv)
3. Click Save

The app will now SSH into the remote host and run `kiroclaw token` on
each launch to get a fresh JWT — no manual paste required.

### Token flow

```
1. Try local ~/.kiroclaw/.local_secret  →  /api/token/local
2. Try SSH: ssh <host> kiroclaw token   →  parse JWT from stdout
   (default binPath iterates candidates; customized binPath is used directly)
3. Fallback: show manual token prompt
```

### Tray menu

| Item | Action |
|------|--------|
| Set Remote Host… | Configure hostname and binary path |
| Refresh Token | Fetch a fresh token and reload the dashboard |

Settings are persisted via `electron-store` in
`~/Library/Application Support/KiroClaw/config.json`.

### Troubleshooting

| Issue | Fix |
|-------|-----|
| "SSH token fetch failed" | Check `ssh YOUR_HOST` works from Terminal |
| "kiroclaw binary not found in any of …" | None of the default candidates exist on the remote host. Install kiroclaw (`pip install kiroclaw`) or set a custom path in **Set Remote Host…** |
| "command not found: kiro-cli" in SSH | kiro-cli is the agent backend; install it on the remote host and log in (`kiro-cli login`), or set the binary path to your kiroclaw install (e.g. `$HOME/.local/bin/kiroclaw`) |
| Token fetched but 403 | Gateway may need restart — `ssh host systemctl --user restart kiroclaw` |

## Notes

- Closing the window hides to tray — right-click the tray icon or Cmd+Q to quit
- External links open in your default browser
- The backend must be set up first (`source setup.sh` from the project root)
