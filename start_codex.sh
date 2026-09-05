#!/usr/bin/env bash
set -euo pipefail
cd /opt/agents
export PATH="$PATH:$HOME/.local/bin:$HOME/bin"
if ! command -v codex >/dev/null 2>&1; then
    echo 'Codex CLI не найден в PATH.' >&2
    exit 1
fi
exec codex "$@"
