#!/bin/sh
# Install museum on this machine: config, skill, pi extension.
# Usage: ./install.sh [STORE [NAME]]     e.g. ./install.sh devbox:/data/museum
# NAME is this machine's folder in the store; defaults to `hostname -s`, pinned in config.
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
SYNC="$ROOT/bin/museum-sync"
CONF_DIR="$HOME/.config/museum"

mkdir -p "$CONF_DIR"
if [ ! -f "$CONF_DIR/config" ]; then
  [ $# -ge 1 ] || { echo "usage: $0 STORE   (first install needs a store, e.g. host:/path)" >&2; exit 1; }
  STORE="$1" NAME="${2:-$(hostname -s)}"
  # A taken name means another machine (or an old install) owns that folder.
  # An explicit NAME argument is the caller saying the reuse is intended.
  case "$STORE" in
    *:*) ssh -o BatchMode=yes "${STORE%%:*}" "test -d '${STORE#*:}/$NAME'" && TAKEN=1 || TAKEN= ;;
    *)   [ -d "$STORE/$NAME" ] && TAKEN=1 || TAKEN= ;;
  esac
  if [ -n "$TAKEN" ] && [ $# -lt 2 ]; then
    echo "museum: $STORE/$NAME already exists." >&2
    echo "  same machine reinstalling:  $0 $STORE $NAME" >&2
    echo "  a different machine:        $0 $STORE <unique-name>" >&2
    exit 1
  fi
  # Pin NAME so a hostname change never starts a new folder.
  printf 'STORE=%s\nNAME=%s\n' "$STORE" "$NAME" > "$CONF_DIR/config"
  echo "wrote $CONF_DIR/config"
fi

# Skill: symlink so edits in the repo are live.
mkdir -p "$HOME/.agents/skills"
ln -sfn "$ROOT/skills/museum" "$HOME/.agents/skills/museum"
echo "linked skill -> ~/.agents/skills/museum"

# The pi extension runs museum-sync; no scheduler. Symlinked so it finds
# bin/museum-sync beside it and repo edits are live.
mkdir -p "$HOME/.pi/agent/extensions"
ln -sfn "$ROOT/pi/museum.ts" "$HOME/.pi/agent/extensions/museum.ts"
echo "linked pi extension -> ~/.pi/agent/extensions/museum.ts (running pi agents pick it up on restart or /reload)"

# Remove the schedulers earlier versions installed.
PLIST="$HOME/Library/LaunchAgents/com.museum.sync.plist"
if [ -f "$PLIST" ]; then
  launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "removed old launchd agent"
fi
if command -v crontab >/dev/null 2>&1 && crontab -l 2>/dev/null | grep -q 'museum-sync'; then
  crontab -l 2>/dev/null | grep -v 'museum-sync' | crontab -
  echo "removed old cron entry"
fi

echo "running first sync..."
"$SYNC" --now && echo "ok"
