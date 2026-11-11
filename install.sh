#!/bin/sh
# Set this machine up from a checkout: `museum init`, run from this folder.
# Usage: ./install.sh [STORE [MACHINE]] [--hooks LIST|all] [--no-hooks]
# An npm install runs `museum init` instead: npm i -g @mu-crew/museum.
set -eu
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT/bin/museum" init "$@"
