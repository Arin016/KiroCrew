#!/usr/bin/env bash
# Build the standalone KiroClaw desktop app end-to-end.
#
# Pipeline:
#   1. Build the React dashboard (npm)        -> website/dist
#   2. Stage it into the Python package        -> src/kiro_claw/static/dist
#   3. Freeze the backend with PyInstaller      -> a self-contained kiroclaw-backend/
#   4. Drop the frozen backend into the Electron app's backend-dist/
#   5. Package the desktop app with electron-builder -> DMG (mac) / AppImage (linux)
#
# The result is a double-clickable app that embeds the whole Python backend +
# dashboard — no system Python, pip, npm, or node required by the end user.
#
# ARCHITECTURE: this builds for the HOST OS *and* HOST CPU ARCH only (not a
# universal binary). PyInstaller (target_arch=None) and electron-builder (no
# arch key) both follow the host. To ship macOS arm64 + x86_64 and Linux
# x86_64 + aarch64 you must run this once per architecture. See
# docs/DESKTOP_APP.md -> "Builds are host-architecture-only".
#
# Usage:
#   bash packaging/build-desktop.sh            # build for the host OS + arch
#   SKIP_FRONTEND=1 bash packaging/...         # reuse an already-staged dist
#   SKIP_ELECTRON=1 bash packaging/...         # stop after the backend binary
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

printf '\n\033[1;33m▶ Building for host arch only: %s/%s. Universal binaries are NOT produced — run once per target arch (see docs/DESKTOP_APP.md).\033[0m\n' \
  "$(uname -s)" "$(uname -m)"

# Prefer the repo's build venv (created by `make backend`) so PyInstaller installs
# without tripping PEP 668 on an externally-managed system Python. An explicit
# PYTHON=... still wins; otherwise fall back to python3.
if [ -n "${PYTHON:-}" ]; then
  PY="$PYTHON"
elif [ -x "$ROOT/.venv/bin/python" ]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="python3"
fi
ELECTRON_DIR="$ROOT/website/electron"
SPEC="$ROOT/packaging/kiroclaw-backend.spec"
PYI_DIST="$ROOT/build/pyinstaller/dist"
PYI_WORK="$ROOT/build/pyinstaller/build"

log() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

# --- 1. Frontend ------------------------------------------------------------
if [ "${SKIP_FRONTEND:-0}" != "1" ]; then
  log "Building dashboard (npm)…"
  ( cd "$ROOT/website"
    if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
    npm run build )
  log "Staging dashboard into the package…"
  rm -rf "$ROOT/src/kiro_claw/static/dist"
  mkdir -p "$ROOT/src/kiro_claw/static"
  cp -R "$ROOT/website/dist" "$ROOT/src/kiro_claw/static/dist"
else
  log "SKIP_FRONTEND=1 — reusing existing src/kiro_claw/static/dist"
fi

if [ ! -f "$ROOT/src/kiro_claw/static/dist/index.html" ]; then
  echo "❌ Dashboard dist missing at src/kiro_claw/static/dist — cannot bundle." >&2
  exit 1
fi

# --- 2. PyInstaller backend -------------------------------------------------
log "Freezing backend with PyInstaller…"
if ! "$PY" -c "import PyInstaller" 2>/dev/null; then
  echo "PyInstaller not installed — installing into the active environment…"
  "$PY" -m pip install pyinstaller
fi
# Make the build hermetic: point PYTHONPATH at THIS repo's src only, so a
# polluted ambient PYTHONPATH (e.g. a sibling checkout) can't leak modules
# into the frozen bundle. The spec's own pathex=[SRC] reinforces this.
export PYTHONPATH="$ROOT/src"
"$PY" -m PyInstaller "$SPEC" --noconfirm --distpath "$PYI_DIST" --workpath "$PYI_WORK"

BACKEND_OUT="$PYI_DIST/kiroclaw-backend"
if [ ! -x "$BACKEND_OUT/kiroclaw-backend" ]; then
  echo "❌ PyInstaller did not produce $BACKEND_OUT/kiroclaw-backend" >&2
  exit 1
fi

log "Smoke-testing the frozen backend…"
"$BACKEND_OUT/kiroclaw-backend" --version

# --- 3. Hand the backend to the Electron app -------------------------------
log "Staging frozen backend into the Electron app…"
rm -rf "$ELECTRON_DIR/backend-dist/kiroclaw-backend"
mkdir -p "$ELECTRON_DIR/backend-dist"
cp -R "$BACKEND_OUT" "$ELECTRON_DIR/backend-dist/kiroclaw-backend"

if [ "${SKIP_ELECTRON:-0}" = "1" ]; then
  log "SKIP_ELECTRON=1 — backend ready at $ELECTRON_DIR/backend-dist/kiroclaw-backend"
  exit 0
fi

# --- 4. Package the desktop app --------------------------------------------
log "Packaging desktop app (electron-builder)…"
( cd "$ELECTRON_DIR"
  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
  npm run dist )

log "Done. Installer(s) are in $ELECTRON_DIR/dist/"
ls -1 "$ELECTRON_DIR/dist" 2>/dev/null | sed 's/^/   /' || true
