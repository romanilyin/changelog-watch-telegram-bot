#!/usr/bin/env bash
set -Eeuo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

PYTHON_BIN="${PYTHON:-}"
if [ -z "$PYTHON_BIN" ]; then
    if [ -x .venv/bin/python ]; then
        PYTHON_BIN=".venv/bin/python"
    else
        PYTHON_BIN="$(command -v python3 || command -v python)"
    fi
fi

"$PYTHON_BIN" -m py_compile bot.py
"$PYTHON_BIN" bot.py --validate-config
"$PYTHON_BIN" scripts/test-summary-startup.py
./check-dry-run-no-write.sh
