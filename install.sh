#!/bin/sh
# Install museum on this machine: config, skill, pi extension.
# Usage: ./install.sh [STORE [MACHINE]]     e.g. ./install.sh devbox:/data/museum
# STORE is host:/path for an ssh store, or /path for a local one. MACHINE is this
# machine's folder in the store; it defaults to `hostname -s`, pinned in config.
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
SYNC="$ROOT/bin/museum-sync"
CONF_DIR="$HOME/.config/museum"
CONF="$CONF_DIR/config.toml"

# The scripts read config.toml with tomllib, new in Python 3.11.
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2> /dev/null; then
  echo "museum: needs python3 3.11 or later on PATH (macOS: brew install python)" >&2
  exit 1
fi

# A TOML basic string: the value quoted, with \ and " escaped.
toml_str() { printf '"%s"' "$(printf '%s' "$1" | sed 's/[\\"]/\\&/g')"; }

mkdir -p "$CONF_DIR"
if [ ! -f "$CONF" ] && [ ! -f "$CONF_DIR/config" ]; then
  [ $# -ge 1 ] || {
    echo "usage: $0 STORE [MACHINE]   (first install needs a store: host:/path or /path)" >&2
    exit 1
  }
  STORE="$1" MACHINE="${2:-$(hostname -s)}"
  case "$STORE" in
    *:*) HOST=${STORE%%:*} STORE_PATH=${STORE#*:} ;;
    *) HOST='' STORE_PATH=$STORE ;;
  esac
  case "$STORE_PATH" in
    /*) ;;
    *)
      echo "museum: the store folder must be absolute: $STORE_PATH" >&2
      exit 1
      ;;
  esac
  # A taken name means another machine (or an old install) owns that folder.
  # An explicit MACHINE argument is the caller saying the reuse is intended.
  if [ -n "$HOST" ]; then
    ssh -o BatchMode=yes "$HOST" "test -d '$STORE_PATH/$MACHINE'" && TAKEN=1 || TAKEN=
  else
    [ -d "$STORE_PATH/$MACHINE" ] && TAKEN=1 || TAKEN=
  fi
  if [ -n "$TAKEN" ] && [ $# -lt 2 ]; then
    echo "museum: $STORE/$MACHINE already exists." >&2
    echo "  same machine reinstalling:  $0 $STORE $MACHINE" >&2
    echo "  a different machine:        $0 $STORE <unique-name>" >&2
    exit 1
  fi
  # The sync never creates a local store folder, so that a forgotten `host`
  # line fails instead of backing up to this machine. Installing is the one
  # time it is created, on purpose.
  if [ -z "$HOST" ]; then
    mkdir -p "$STORE_PATH"
  fi
  if [ -n "$HOST" ]; then
    HOST_LINE="host = $(toml_str "$HOST")
"
  else
    HOST_LINE=''
  fi
  # Pin the machine name so a hostname change never starts a new folder.
  # Keep the comments in step with TEMPLATE in bin/museum-sync; a test checks.
  cat > "$CONF" << EOF
# museum: https://github.com/mu-crew/museum

# This machine's folder in the store: sessions go to <store path>/<machine>/.
# Pinned at install, so a hostname change keeps using the same folder.
machine = $(toml_str "$MACHINE")

[store]
# The store is on another host, over ssh, when \`host\` is set, and in a folder
# on this machine when it is not. Only \`host\` decides.
#
#   ssh:    host = "devbox"            local:  (no host line)
#           path = "/data/museum"              path = "/data/museum"
#
# \`path\` is absolute, on whichever host holds the store. A local store's
# folder must already exist, so a forgotten \`host\` line fails instead of
# quietly backing up to this machine.
${HOST_LINE}path = $(toml_str "$STORE_PATH")

# ssh only. true: sync only over an ssh ControlMaster that is already open,
# never a new connection, for a store host whose login needs a security-key
# touch or another human step. Open one with \`ssh -MNf <host>\`.
ssh_mux_only = false
EOF
  echo "wrote $CONF"
fi

# Skill: symlink so edits in the repo are live.
mkdir -p "$HOME/.agents/skills"
ln -sfn "$ROOT/skills/museum" "$HOME/.agents/skills/museum"
echo "linked skill -> ~/.agents/skills/museum"

# The pi extension runs museum-sync. Symlinked so it finds
# bin/museum-sync beside it and repo edits are live.
mkdir -p "$HOME/.pi/agent/extensions"
ln -sfn "$ROOT/pi/museum.ts" "$HOME/.pi/agent/extensions/museum.ts"
echo "linked pi extension -> ~/.pi/agent/extensions/museum.ts (running pi agents pick it up on restart or /reload)"

# The first sync also converts an old KEY=value config to config.toml.
echo "running first sync..."
"$SYNC" --now && echo "ok"
