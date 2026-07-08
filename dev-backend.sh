#!/bin/bash
# Run the KiroClaw gateway from LIVE source. Uses the venv Python (which has
# all deps) but PYTHONPATH=src so code changes are picked up immediately on
# restart.
#
# Usage: ./dev-backend.sh
#   - Runs on port 6777 (dev port, separate from production on 5476)
#   - Uses .kiroclaw-dev/ as data directory (isolated from ~/.kiroclaw/)
#   - Ctrl+C to stop, re-run to pick up changes
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Find the Python with deps installed: prefer the venv created by install.sh /
# setup.sh / minimal_install.sh. Override with RUNTIME_PYTHON if needed.
RUNTIME_PYTHON="${RUNTIME_PYTHON:-}"
if [ -z "$RUNTIME_PYTHON" ] || [ ! -x "$RUNTIME_PYTHON" ]; then
    if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        RUNTIME_PYTHON="$SCRIPT_DIR/.venv/bin/python"
    fi
fi
if [ ! -x "$RUNTIME_PYTHON" ]; then
    echo "ERROR: Cannot find the KiroClaw venv Python at $SCRIPT_DIR/.venv/bin/python."
    echo "Run 'bash minimal_install.sh' once to set up the venv, then try again."
    exit 1
fi

export PYTHONPATH="$SCRIPT_DIR/src"
export KIROCLAW_HOME="${KIROCLAW_HOME:-.kiroclaw-dev}"
# Absolutize: config_dir() resolves this against each process's CWD, and MCP
# subprocesses (mcp-core/mcp-cron) are spawned with session-workspace CWDs —
# a relative HOME makes them create empty config dirs with no .local_secret,
# so their gateway IPC calls fail with 403 Forbidden.
case "$KIROCLAW_HOME" in
    /*) ;;
    *) KIROCLAW_HOME="$SCRIPT_DIR/$KIROCLAW_HOME" ;;
esac
export KIROCLAW_PORT="${KIROCLAW_PORT:-6777}"
export KIROCLAW_PROJECT_DIR="$SCRIPT_DIR"

echo "🐾 Dev backend starting (live source, port $KIROCLAW_PORT)"
echo "   Python: $RUNTIME_PYTHON"
echo "   Source: $PYTHONPATH"
echo "   Data:   $KIROCLAW_HOME"
echo ""

exec "$RUNTIME_PYTHON" -m kiro_claw gateway
