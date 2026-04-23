#!/usr/bin/env bash
# Launch tmuxmux. Creates the venv on first run, then execs into the TUI.
# Usage: ./run.sh [path/to/hosts.toml]

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$HERE/.venv"

if [[ ! -x "$VENV/bin/tmuxmux" ]]; then
    python3 -m venv "$VENV"
    "$VENV/bin/pip" install --upgrade pip >/dev/null
    "$VENV/bin/pip" install -e "$HERE"
fi

exec "$VENV/bin/tmuxmux" "$@"
