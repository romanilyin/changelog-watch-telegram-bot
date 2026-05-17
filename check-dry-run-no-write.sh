#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${PROJECT_ROOT}"

if [ -f .env ]; then
    set -a
    source .env
    set +a
fi

DB_FILE="${DB_PATH:-data/posted.sqlite3}"
PYTHON_BIN="${VENV_PYTHON:-${PROJECT_ROOT}/.venv/bin/python}"
if [ ! -x "${PYTHON_BIN}" ]; then
    PYTHON_BIN="$(command -v python3 || command -v python)"
fi

if [ -f "${DB_FILE}" ]; then
    before_hash="$(sha256sum "${DB_FILE}" | cut -d ' ' -f1)"
    before_mtime="$(stat -c '%Y' "${DB_FILE}")"
else
    before_hash=""
    before_mtime=""
fi

"${PYTHON_BIN}" bot.py --once --dry-run

if [ -f "${DB_FILE}" ]; then
    after_hash="$(sha256sum "${DB_FILE}" | cut -d ' ' -f1)"
    after_mtime="$(stat -c '%Y' "${DB_FILE}")"
else
    after_hash=""
    after_mtime=""
fi

if [ "${before_hash}" != "${after_hash}" ] || [ "${before_mtime}" != "${after_mtime}" ]; then
    printf 'dry-run changed DB state: %s\n' "${DB_FILE}" >&2
    exit 1
fi

printf 'dry-run left DB unchanged: %s\n' "${DB_FILE}"
