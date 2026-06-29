#!/bin/bash
# KiroClaw Persistent Sessions Setup
#
# Installs kiroclaw gateway as a systemd user service.
# Requires the systemd user manager to be running (see README.md Phase 1).
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
USERNAME=$(whoami)
HOSTNAME=$(hostname)

echo "🐾 KiroClaw Persistent Sessions Setup"
echo ""

# ── Check: systemd user manager running? ──
if ! systemctl --user status >/dev/null 2>&1; then
    echo "❌ Systemd user manager is not running."
    echo "   Complete Phase 1 in README.md first (requires sudo, one-time)."
    exit 1
fi
echo "✅ Systemd user manager running"

# ── Check: kiroclaw gateway already running in tmux? ──
if pgrep -f "kiro_claw gateway\|kiroclaw gateway" | grep -v $$ >/dev/null 2>&1; then
    echo ""
    echo "⚠️  kiroclaw gateway is already running (tmux or manual)."
    echo "   Kill it first: tmux kill-session -t kiroclaw"
    echo "   Then re-run this script."
    exit 1
fi

# ── Install user service ──
echo "→ Installing kiroclaw user service..."
USER_UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$USER_UNIT_DIR"
NODE_VERSION=$(node --version 2>/dev/null || basename "$(ls -d "$HOME"/.nvm/versions/node/v* 2>/dev/null | tail -1)")

# Resolve kiroclaw binary from current shell PATH
KIROCLAW_BIN="$(command -v kiroclaw 2>/dev/null)" || { echo "❌ kiroclaw not found in PATH"; exit 1; }
echo "  Binary: $KIROCLAW_BIN"

sed -e "s/%u/$USERNAME/g" \
    -e "s|KIROCLAW_BIN|$KIROCLAW_BIN|g" \
    -e "s/NVM_NODE_VERSION/$NODE_VERSION/g" \
    "$SCRIPT_DIR/kiroclaw.service" > "$USER_UNIT_DIR/kiroclaw.service"

systemctl --user daemon-reload
systemctl --user enable kiroclaw
systemctl --user start kiroclaw

echo ""
systemctl --user status kiroclaw --no-pager || true

# ── Mac instructions ──
echo ""
echo "━━━ Mac Setup (run on your laptop) ━━━"
echo ""
echo "scp $USERNAME@$HOSTNAME:$SCRIPT_DIR/com.kiroclaw.tunnel.plist ~/Library/LaunchAgents/"
echo "sed -i '' 's|ALIAS@DEV_DESKTOP_HOSTNAME|$USERNAME@$HOSTNAME|g' ~/Library/LaunchAgents/com.kiroclaw.tunnel.plist"
echo "launchctl load ~/Library/LaunchAgents/com.kiroclaw.tunnel.plist"
echo ""
echo "Done! Dashboard: http://localhost:5476"
