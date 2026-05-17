#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$WslDistro = "",

    [string]$WindowsRepoPath = (Resolve-Path $PSScriptRoot).Path,

    [switch]$Tail,

    [int]$TailLines = 80,

    [switch]$NotifyAdmins
)

$ErrorActionPreference = "Stop"

function Convert-ToWslPath {
    param([string]$Path)

    $resolved = [System.IO.Path]::GetFullPath($Path)
    if ($resolved -match "^([A-Za-z]):[\\/](.*)$") {
        $drive = $matches[1].ToLower()
        $rest = $matches[2] -replace "\\", "/"
        return "/mnt/$drive/$rest"
    }

    return $resolved
}

function Invoke-WslBot {
    param([string[]]$Arguments)

    $wslArgs = @()
    if ($WslDistro) {
        $wslArgs += @("-d", $WslDistro)
    }

    $wslArgs += $Arguments
    & wsl.exe @wslArgs
    return $LASTEXITCODE
}

if (-not (Test-Path -LiteralPath $WindowsRepoPath -PathType Container)) {
    throw "Repo path not found: $WindowsRepoPath"
}

$repoWslPath = Convert-ToWslPath $WindowsRepoPath
$tmpBashWindowsPath = Join-Path $WindowsRepoPath ".status-changelog-watch-bot-wsl.tmp.sh"
$tmpBashWslPath = "$repoWslPath/.status-changelog-watch-bot-wsl.tmp.sh"

$notifyArg = if ($NotifyAdmins) { "1" } else { "0" }
$tailArg = if ($Tail) { "1" } else { "0" }

$bashScript = @'
#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${1:?repo path is required}"
NOTIFY="${2:-0}"
INCLUDE_TAIL="${3:-0}"
TAIL_LINES="${4:-80}"

warn() { printf "WARN: %s\n" "$*" >&2; }
fail() { printf "ERROR: %s\n" "$*" >&2; exit 1; }

if [ ! -d "$REPO" ]; then
    fail "repo not found: $REPO"
fi

cd "$REPO"

ENV_FILE="$REPO/.env"
if [ -f "$ENV_FILE" ]; then
    set -a
    source "$ENV_FILE"
    set +a
fi

python_bin="${VENV_PYTHON:-$REPO/.venv/bin/python}"
if [ ! -x "$python_bin" ]; then
    python_bin="$(command -v python3 || command -v python || true)"
fi

resolve_lock_file() {
    local env_lock="${BOT_INSTANCE_LOCK_PATH:-}"
    local env_file="$REPO/.env"
    local repo_lock=""
    local script_abs
    local lock_suffix

    if [ -n "$env_lock" ]; then
        printf "%s\n" "$env_lock"
        return
    fi

    if [ -f "$env_file" ]; then
        repo_lock="$(sed -n 's/^[[:space:]]*BOT_INSTANCE_LOCK_PATH[[:space:]]*=//p' "$env_file" | tail -n1 | tr -d '\r' | tr -d '[:space:]' | tr -d '"' | tr -d "'")"
        if [ -n "$repo_lock" ]; then
            printf "%s\n" "$repo_lock"
            return
        fi
    fi

    script_abs="$(readlink -f "$REPO/bot.py")"
    lock_suffix="$(printf '%s' "$script_abs" | sha1sum | awk '{print substr($1, 1, 16)}')"
    printf '/tmp/changelog-watch-telegram-bot-%s.lock\n' "$lock_suffix"
}

read_lock_pid() {
    local path="$1"
    local pid

    if [ -z "$path" ] || [ ! -f "$path" ]; then
        return 1
    fi

    pid="$(sed -n '1p' "$path" 2>/dev/null | tr -d '[:space:]')"
    if [[ "$pid" =~ ^[0-9]+$ ]]; then
        printf "%s\n" "$pid"
        return 0
    fi
    return 1
}

is_bot_process() {
    local pid="$1"

    if ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi

    local cmd
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
    if [[ "$cmd" != *"bot.py"* ]]; then
        return 1
    fi

    local cwd
    cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
    if [ -z "$cwd" ]; then
        return 1
    fi

    if [ "$cwd" != "$REPO" ] && [ "${cwd#${REPO}/}" = "$cwd" ]; then
        return 1
    fi

    return 0
}

collect_running_pids() {
    local -a pids=()
    local pid

    while read -r pid; do
        if [ -z "$pid" ]; then
            continue
        fi
        if is_bot_process "$pid"; then
            pids+=("$pid")
        fi
    done < <(pgrep -f "bot.py" || true)

    local lock_file
    local lock_pid
    local file_pid

    lock_file="$(resolve_lock_file)"
    if [ -f "$lock_file" ]; then
        lock_pid="$(read_lock_pid "$lock_file" 2>/dev/null || true)" || true
        if [ -n "$lock_pid" ] && is_bot_process "$lock_pid"; then
            pids+=("$lock_pid")
        fi
    fi

    if [ -f "$REPO/data/bot.pid" ]; then
        file_pid="$(sed -n '1p' "$REPO/data/bot.pid" 2>/dev/null | tr -d '[:space:]')"
        if [[ "$file_pid" =~ ^[0-9]+$ ]] && is_bot_process "$file_pid"; then
            pids+=("$file_pid")
        fi
    fi

    printf '%s\n' "${pids[@]}"
}

resolve_systemd_units_for_repo() {
    local script_abs
    local -a units=()

    if ! command -v systemctl >/dev/null 2>&1; then
        return 1
    fi

    script_abs="$(readlink -f "$REPO/bot.py")"

    while IFS= read -r unit; do
        if [ -z "$unit" ]; then
            continue
        fi

        local exec_start
        exec_start="$(systemctl --user show "$unit" --property=ExecStart --value 2>/dev/null | tr -d '\r')"
        if [ -z "$exec_start" ]; then
            continue
        fi

        if [[ "$exec_start" == *"$script_abs"* ]] || [[ "$exec_start" == *"$REPO/bot.py"* ]]; then
            units+=("$unit")
        fi
    done < <(systemctl --user list-units --type=service --all --no-legend --no-pager 2>/dev/null | awk '{print $1}')

    if [ ${#units[@]} -eq 0 ]; then
        return 0
    fi

    printf '%s\n' "${units[@]}"
}

systemd_value() {
    local unit="$1"
    local property="$2"
    local value

    value="$(systemctl --user show "$unit" --property="$property" --value 2>/dev/null | tr -d '\r' || true)"
    if [ -z "$value" ]; then
        value="n/a"
    fi
    printf '%s\n' "$value"
}

dedupe_pids() {
    local -A seen=()
    local -a out=()
    local pid

    for pid in "$@"; do
        pid="${pid//[[:space:]]/}"
        if [ -z "$pid" ]; then
            continue
        fi
        if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
            continue
        fi
        if [ -n "${seen[$pid]:-}" ]; then
            continue
        fi
        seen[$pid]=1
        out+=("$pid")
    done

    if [ ${#out[@]} -eq 0 ]; then
        return 0
    fi

    printf '%s\n' "${out[@]}"
}

mapfile -t RAW_PIDS < <(collect_running_pids)
mapfile -t PIDS < <(dedupe_pids "${RAW_PIDS[@]}")
mapfile -t SYSTEMD_UNITS < <(resolve_systemd_units_for_repo || true)

LOCK_FILE="$(resolve_lock_file)"
LOCK_PID=""
LOCK_VALID=0
if [ -f "$LOCK_FILE" ]; then
    LOCK_PID="$(read_lock_pid "$LOCK_FILE")" || true
    if [ -n "$LOCK_PID" ] && is_bot_process "$LOCK_PID"; then
        LOCK_VALID=1
    fi
fi

PID_FILE="$REPO/data/bot.pid"
PID_FILE_PID=""
PID_FILE_VALID=0
if [ -f "$PID_FILE" ]; then
    PID_FILE_PID="$(sed -n '1p' "$PID_FILE" 2>/dev/null | tr -d '[:space:]')"
    if [[ "$PID_FILE_PID" =~ ^[0-9]+$ ]] && is_bot_process "$PID_FILE_PID"; then
        PID_FILE_VALID=1
    fi
fi

LOG_FILE="$REPO/data/bot.log"

status_payload=""
append_status() {
    status_payload+="$*"$'\n'
}

append_status "changelog-watch-telegram-bot status"
append_status "Repository: $REPO"
append_status "Checked: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
append_status "Config: ${CONFIG_PATH:-products.yaml}"
append_status "DB: ${DB_PATH:-data/posted.sqlite3}"
append_status "Lock file: $LOCK_FILE"

if [ -f "$LOCK_FILE" ]; then
    if [ "$LOCK_VALID" -eq 1 ]; then
        append_status "Lock holder PID: $LOCK_PID (running)"
    elif [ -n "$LOCK_PID" ]; then
        append_status "Lock holder PID: $LOCK_PID (stale)"
    else
        append_status "Lock holder PID: unavailable"
    fi
else
    append_status "Lock holder PID: none"
fi

append_status "PID file: $PID_FILE"
if [ -f "$PID_FILE" ]; then
    if [ "$PID_FILE_VALID" -eq 1 ]; then
        append_status "PID file PID: $PID_FILE_PID (running)"
    else
        append_status "PID file PID: ${PID_FILE_PID:-unknown} (stale/missing)"
    fi
else
    append_status "PID file: not found"
fi

if [ ${#PIDS[@]} -gt 0 ]; then
    append_status "Running instances: ${#PIDS[@]} (${PIDS[*]})"
    append_status "Processes:"
    for pid in "${PIDS[@]}"; do
        cmdline="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
        ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
        parent_cmd=""
        if [ -n "$ppid" ]; then
            parent_cmd="$(tr '\0' ' ' < "/proc/$ppid/cmdline" 2>/dev/null || true)"
        fi

        append_status "  - pid=$pid ppid=${ppid:-unknown} cmd=${cmdline}"
        if [ -n "$parent_cmd" ]; then
            append_status "    parent: ${parent_cmd}"
        fi
    done
else
    append_status "Running instances: 0"
    append_status "Processes: none"
fi

append_status "systemd --user units:"
if ! command -v systemctl >/dev/null 2>&1; then
    append_status "  - systemctl is unavailable"
elif [ ${#SYSTEMD_UNITS[@]} -gt 0 ]; then
    for systemd_unit in "${SYSTEMD_UNITS[@]}"; do
        systemd_active="$(systemd_value "$systemd_unit" ActiveState)"
        systemd_sub="$(systemd_value "$systemd_unit" SubState)"
        systemd_main_pid="$(systemd_value "$systemd_unit" ExecMainPID)"
        systemd_restart="$(systemd_value "$systemd_unit" Restart)"
        append_status "  - $systemd_unit: active=$systemd_active sub=$systemd_sub restart=$systemd_restart main_pid=$systemd_main_pid"
    done
else
    append_status "  - none"
fi

if [ ${#PIDS[@]} -gt 1 ]; then
    append_status "WARNING: multiple bot instances detected!"
fi

if [ "$INCLUDE_TAIL" = "1" ] && [ -f "$LOG_FILE" ]; then
    append_status ""
    append_status "Last ${TAIL_LINES} log lines:"
    while IFS= read -r line; do
        append_status "$line"
    done < <(tail -n "$TAIL_LINES" "$LOG_FILE")
elif [ "$INCLUDE_TAIL" = "1" ]; then
    append_status "Log file not found: $LOG_FILE"
fi

printf "%s\n" "$status_payload"

if [ "$NOTIFY" != "1" ]; then
    exit 0
fi

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    warn "TELEGRAM_BOT_TOKEN is not set; skip Telegram notify"
    exit 0
fi

if [ -z "${python_bin:-}" ] || [ ! -x "$python_bin" ]; then
    warn "python executable not found; skip Telegram notify"
    exit 0
fi

ROUTING_CONFIG_PATH="${ROUTING_CONFIG_PATH:-$REPO/admin-routing.yaml}"
ADMIN_IDS="${ADMIN_IDS:-}"

if [ -z "$ADMIN_IDS" ] && [ -f "$ROUTING_CONFIG_PATH" ] && [ -n "$python_bin" ]; then
    ADMIN_IDS="$(
        "$python_bin" - "$ROUTING_CONFIG_PATH" <<'PY'
import sys

path = sys.argv[1]
try:
    import yaml
except Exception:
    sys.exit(1)

try:
    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
except Exception:
    data = {}

admins = []
for raw_admin in data.get("admins", []) or []:
    try:
        admin_id = raw_admin.get("id")
    except Exception:
        continue
    if admin_id is None:
        continue
    admins.append(str(admin_id))

if admins:
    print(" ".join(admins))
PY
    )" || true
fi

if [ -z "$ADMIN_IDS" ]; then
    warn "No admin IDs found; skip Telegram notify"
    exit 0
fi

if ! printf "%s" "$status_payload" | "$python_bin" - "$TELEGRAM_BOT_TOKEN" "$ADMIN_IDS" <<'PY'
import json
import sys
import urllib.request

token = sys.argv[1]
admins = [item.strip() for item in sys.argv[2].split() if item.strip()]
text = sys.stdin.read()

if not token or not admins or not text:
    raise SystemExit(0)

url = f"https://api.telegram.org/bot{token}/sendMessage"
payload = {
    "text": text,
}

for admin_id in admins:
    payload["chat_id"] = admin_id
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            response.read()
    except Exception:
        pass
PY
then
    warn "Failed to send Telegram status notification"
fi
'@

try {
    [System.IO.File]::WriteAllText($tmpBashWindowsPath, $bashScript, [System.Text.UTF8Encoding]::new($false))

    $wslCmd = @(
        "--",
        "bash",
        "$tmpBashWslPath",
        "$repoWslPath",
        $notifyArg,
        $tailArg,
        "$TailLines"
    )

    Invoke-WslBot -Arguments $wslCmd
} finally {
    if (Test-Path -LiteralPath $tmpBashWindowsPath) {
        Remove-Item -LiteralPath $tmpBashWindowsPath -Force
    }
}

if ($LASTEXITCODE -ne 0) {
    throw "Failed to check bot status. WSL exit code: $LASTEXITCODE"
}
