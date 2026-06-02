#!/bin/bash
# Copy ~/.kiroclaw into .kiroclaw-dev/ for local development.
# Safe to re-run — wipes .kiroclaw-dev first so you get a clean snapshot.
#
# Usage: ./dev-seed.sh
set -e

SRC="$HOME/.kiroclaw"
DST="$(cd "$(dirname "$0")" && pwd)/.kiroclaw-dev"

if [ ! -d "$SRC" ]; then
  echo "No ~/.kiroclaw found — nothing to seed."
  exit 0
fi

if [ -d "$DST" ]; then
  # Refuse to rm -rf if .kiroclaw-dev is a symlink (could follow to unrelated dir)
  if [ -L "$DST" ]; then
    echo "ERROR: .kiroclaw-dev is a symlink — refusing to remove. Delete it manually."
    exit 1
  fi
  echo "Removing existing .kiroclaw-dev/ ..."
  rm -rf "$DST"
fi

echo "Copying ~/.kiroclaw → .kiroclaw-dev/ ..."
cp -R "$SRC" "$DST"

echo "Done. Start the gateway with:"
echo "  KIROCLAW_HOME=.kiroclaw-dev bin/kiroclaw gateway"
