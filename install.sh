#!/bin/sh
# Install museum on this machine: config, the skill into each harness's skills
# folder, the pi package (skill and extension) when pi is here, and the timer
# that runs `museum tick` every 5 minutes.
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
  # Pin the first rsync 3, as museum-sync's find_rsync looks for it: a timer,
  # tmux and harness hooks start the sync with a PATH of their own.
  RSYNC_LINE='# rsync_path = "/opt/homebrew/bin/rsync"'
  for candidate in "$(command -v rsync || true)" /opt/homebrew/bin/rsync /usr/local/bin/rsync; do
    if [ -n "$candidate" ] && [ -x "$candidate" ] &&
      "$candidate" --version 2> /dev/null | grep -Eq '^rsync[[:space:]]+version 3'; then
      RSYNC_LINE="rsync_path = $(toml_str "$candidate")"
      break
    fi
  done
  # Pin the machine name so a hostname change never starts a new folder.
  # Keep the comments in step with TEMPLATE in bin/museum-sync; a test checks.
  cat > "$CONF" << EOF
# museum: https://github.com/mu-crew/museum

# This machine's folder in the store: sessions go to <store path>/<machine>/.
# Pinned at install, so a hostname change keeps using the same folder.
machine = $(toml_str "$MACHINE")

# The rsync 3 the sync runs, pinned at install: a timer, tmux and harness hooks
# start the sync with a PATH of their own. Left out, the sync looks on PATH.
$RSYNC_LINE

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

# Older installs symlinked the skill and the extension into pi's discovery
# paths; with the package installed too, pi would load both twice. Only
# symlinks are removed: a real file or folder there is not ours. Never put
# the skill back in ~/.agents/skills: pi and other tools also read that tree.
for link in "$HOME/.agents/skills/museum" "$HOME/.pi/agent/extensions/museum.ts"; do
  if [ -L "$link" ]; then
    rm "$link"
    echo "removed old link $link"
  fi
done

# Skill for claude, codex and cursor-agent: symlink into each harness's own
# skills folder when that harness is set up here, so repo edits stay live.
# Muse Code installs skills through `muse skills`; pi loads the skill from
# the package below. A real directory already at the destination is left alone.
SKILL="$ROOT/skills/museum"
link_skill() {
  dest=$1
  mkdir -p "$(dirname "$dest")"
  if [ -e "$dest" ] && [ ! -L "$dest" ]; then
    echo "museum: left $dest (not a symlink)" >&2
    return 0
  fi
  ln -sfn "$SKILL" "$dest"
  echo "linked skill -> $dest"
}

CLAUDE_DIR=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
CODEX_DIR=${CODEX_HOME:-$HOME/.codex}
if [ -d "$CLAUDE_DIR" ]; then
  link_skill "$CLAUDE_DIR/skills/museum"
fi
if [ -d "$CODEX_DIR" ]; then
  link_skill "$CODEX_DIR/skills/museum"
fi
# cursor-agent also reads ~/.claude/skills and ~/.codex/skills (literal home
# paths; it ignores CLAUDE_CONFIG_DIR and CODEX_HOME), so give it its own
# copy only when neither already holds the skill.
CURSOR_SKILL=$HOME/.cursor/skills/museum
if [ -d "$HOME/.cursor" ]; then
  if [ -f "$HOME/.claude/skills/museum/SKILL.md" ] ||
    [ -f "$HOME/.codex/skills/museum/SKILL.md" ]; then
    if [ -L "$CURSOR_SKILL" ]; then
      rm "$CURSOR_SKILL"
      echo "removed covered cursor skill link $CURSOR_SKILL"
    fi
  else
    link_skill "$CURSOR_SKILL"
  fi
fi
echo "museum: Muse Code: add $SKILL with \`muse skills\` (not linked automatically)"

# The skill and the extension for pi, as a package loaded from this folder, so
# repo edits are live. Running pi agents pick it up on restart or /reload.
if command -v pi > /dev/null; then
  pi install "$ROOT"
else
  echo "museum: no pi on PATH; skipping the pi package"
fi

# The first sync also converts an old KEY=value config to config.toml. It runs
# before the timer exists: the timer's first tick would start a sync that
# takes the lock, and this one would then exit 0 without showing an error.
echo "running first sync..."
status=0
if "$SYNC" --now; then echo "ok"; else status=$?; fi

# Syncs for claude and codex, and for a host whose agents have all exited.
"$ROOT/bin/museum" timer install || echo "museum: could not install the timer" >&2
exit "$status"
