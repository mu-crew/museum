#!/bin/sh
# Install museum on this machine: config, periodic sync, skill.
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

# Schedule: launchd on macOS, cron elsewhere. Every 10 minutes.
case "$(uname)" in
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.museum.sync.plist"
    sed -e "s|@MUSEUM_SYNC@|$SYNC|" -e "s|@HOME@|$HOME|g" "$ROOT/launchd/com.museum.sync.plist" > "$PLIST"
    launchctl bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "launchd agent loaded (log: ~/Library/Logs/museum-sync.log)"
    ;;
  *)
    LINE="*/10 * * * * $SYNC >>\$HOME/.cache/museum-sync.log 2>&1"
    ( crontab -l 2>/dev/null | grep -v 'museum-sync'; echo "$LINE" ) | crontab -
    echo "cron entry installed (log: ~/.cache/museum-sync.log)"
    ;;
esac

echo "running first sync..."
"$SYNC" && echo "ok"
