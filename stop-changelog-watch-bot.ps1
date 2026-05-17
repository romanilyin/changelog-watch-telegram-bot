#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$WslDistro = "",

    [string]$WindowsRepoPath = (Resolve-Path $PSScriptRoot).Path,

    [int]$WaitSeconds = 12,

    [string]$SystemdServiceName = ""
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
$tmpBashWindowsPath = Join-Path $WindowsRepoPath ".stop-changelog-watch-bot-wsl.tmp.sh"
$tmpBashWslPath = "$repoWslPath/.stop-changelog-watch-bot-wsl.tmp.sh"

if ($WaitSeconds -lt 1) {
    $WaitSeconds = 1
}

$bashScript = @'
#!/usr/bin/env bash
set -Eeuo pipefail

REPO="${1:?repo path is required}"
WAIT_SECONDS="${2:-12}"
SYSTEMD_SERVICE_NAME="${3:-}"

log() { printf "[bot-stop] %s\n" "$*"; }
warn() { printf "[bot-stop] WARN: %s\n" "$*" >&2; }
fail() { printf "[bot-stop] ERROR: %s\n" "$*" >&2; exit 1; }

if [ ! -d "$REPO" ]; then
    fail "repo not found: $REPO"
fi

    PID_FILE="$REPO/data/bot.pid"
    resolve_lock_file() {
        local env_lock="${BOT_INSTANCE_LOCK_PATH:-}"
        local env_file="${REPO}/.env"
        local repo_lock=""
        local script_abs
        local lock_suffix

        if [ -n "$env_lock" ]; then
            printf '%s\n' "$env_lock"
            return
        fi

        if [ -f "$env_file" ]; then
            repo_lock="$(grep -E '^[[:space:]]*BOT_INSTANCE_LOCK_PATH[[:space:]]*=' "$env_file" | tail -n1 | cut -d= -f2- | tr -d '\r' | tr -d '[:space:]' | tr -d '"' | tr -d "'")"
            if [ -n "$repo_lock" ]; then
                printf '%s\n' "$repo_lock"
                return
            fi
        fi

        script_abs="$(readlink -f "$REPO/bot.py")"
        lock_suffix="$(printf '%s' "$script_abs" | sha1sum | awk '{print substr($1, 1, 16)}')"
        printf '/tmp/changelog-watch-telegram-bot-%s.lock\n' "$lock_suffix"
    }

    LOCK_FILE="$(resolve_lock_file)"

    read_lock_pid() {
        local path="$1"
        local pid

        if [ -z "$path" ] || [ ! -f "$path" ]; then
            return 1
        fi

        pid="$(sed -n '1p' "$path" 2>/dev/null | tr -d '[:space:]')"
        if [[ "$pid" =~ ^[0-9]+$ ]]; then
            printf '%s\n' "$pid"
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

proc_cmdline() {
    local pid="$1"
    tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true
}

is_systemd_user_process() {
    local pid="$1"
    local ppid=""
    local parent_cmd=""

    if [ -z "$pid" ] || ! kill -0 "$pid" 2>/dev/null; then
        return 1
    fi

    ppid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d '[:space:]')"
    if [ -z "$ppid" ]; then
        return 1
    fi

    parent_cmd="$(proc_cmdline "$ppid")"
    if [[ "$parent_cmd" == *"systemd --user"* ]]; then
        return 0
    fi

    return 1
}

resolve_systemd_units_for_repo() {
    local -a target_pids=("$@")
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
        local active_state
        local match
        local main_pid

        exec_start="$(systemctl --user show "$unit" --property=ExecStart --value 2>/dev/null | tr -d '\r')"
        if [ -z "$exec_start" ]; then
            continue
        fi

        active_state="$(systemctl --user show "$unit" --property=ActiveState --value 2>/dev/null | tr -d '\r')"
        if [ "$active_state" != "active" ] && [ "$active_state" != "activating" ] && [ "$active_state" != "deactivating" ] && [ "$active_state" != "reloading" ]; then
            continue
        fi

        match=0
        if [[ "$exec_start" == *"$script_abs"* ]] || [[ "$exec_start" == *"$REPO/bot.py"* ]]; then
            match=1
        elif [ ${#target_pids[@]} -gt 0 ]; then
            main_pid="$(systemctl --user show "$unit" --property=ExecMainPID --value 2>/dev/null | tr -d '\r' | tr -d '[:space:]')"
            if [ -n "$main_pid" ] && [[ "$main_pid" =~ ^[0-9]+$ ]]; then
                for target_pid in "${target_pids[@]}"; do
                    if [ "$target_pid" = "$main_pid" ]; then
                        match=1
                        break
                    fi
                done
            fi
        fi

        if [ "$match" -eq 1 ]; then
            units+=("$unit")
        fi
    done < <(systemctl --user list-units --type=service --all --no-legend --no-pager 2>/dev/null | awk '{print $1}')

    if [ ${#units[@]} -eq 0 ]; then
        return 1
    fi

    printf '%s\n' "${units[@]}"
}

stop_systemd_service() {
    local service_name="$1"

    if [ -z "$service_name" ]; then
        return 0
    fi

    if ! command -v systemctl >/dev/null 2>&1; then
        warn "systemctl not available in WSL environment"
        return 0
    fi

    if systemctl --user is-active "$service_name" >/dev/null 2>&1; then
        warn "attempting to stop systemd --user service: $service_name"
        systemctl --user stop "$service_name" || warn "unable to stop systemd --user service: $service_name"
    fi
}

collect_existing_pids() {
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

    if [ -f "$PID_FILE" ]; then
        local file_pid
        file_pid="$(sed -n '1p' "$PID_FILE" 2>/dev/null | tr -d '[:space:]')"
        if [[ "$file_pid" =~ ^[0-9]+$ ]] && is_bot_process "$file_pid"; then
            pids+=("$file_pid")
        fi
    fi

    if [ -f "$LOCK_FILE" ]; then
        local lock_pid
        lock_pid="$(read_lock_pid "$LOCK_FILE")"

        if [ -n "$lock_pid" ] && is_bot_process "$lock_pid"; then
            pids+=("$lock_pid")
        elif [ -n "$lock_pid" ]; then
            rm -f "$LOCK_FILE"
        fi
    fi

    printf '%s\n' "${pids[@]}"
}

dedupe_pids() {
    local -A seen=()
    local -a out=()
    local pid

    for pid in "$@"; do
        pid="${pid//[[:space:]]/}"

        if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
            continue
        fi

        if [ -n "${seen[$pid]:-}" ]; then
            continue
        fi

        seen[$pid]=1
        out+=("$pid")
    done

    printf '%s\n' "${out[@]}"
}

normalize_pids() {
    local -a out=()
    local pid

    for pid in "$@"; do
        pid="${pid//[[:space:]]/}"

        if [[ "$pid" =~ ^[0-9]+$ ]]; then
            out+=("$pid")
        fi
    done

    if [ ${#out[@]} -eq 0 ]; then
        return 0
    fi

    printf '%s\n' "${out[@]}"
}

mapfile -t RAW_PIDS < <(collect_existing_pids)
mapfile -t PIDS < <(dedupe_pids "${RAW_PIDS[@]}")
mapfile -t PIDS < <(normalize_pids "${PIDS[@]}")
mapfile -t SYSTEMD_UNITS < <(resolve_systemd_units_for_repo "${PIDS[@]}" || true)

if [ "${#PIDS[@]}" -eq 0 ]; then
    if [ -n "$SYSTEMD_SERVICE_NAME" ]; then
        stop_systemd_service "$SYSTEMD_SERVICE_NAME"
    elif [ ${#SYSTEMD_UNITS[@]} -gt 0 ]; then
        warn "No bot processes found matching $REPO, stopping matching systemd --user unit(s): ${SYSTEMD_UNITS[*]}"
        for systemd_unit in "${SYSTEMD_UNITS[@]}"; do
            stop_systemd_service "$systemd_unit"
        done
    fi

    log "No running changelog-watch-telegram-bot instances found in $REPO"
    exit 0
fi

systemd_managed=0
for pid in "${PIDS[@]}"; do
    if is_systemd_user_process "$pid"; then
        systemd_managed=1
        break
    fi
done

if [ "$systemd_managed" -eq 1 ]; then
    if [ -n "$SYSTEMD_SERVICE_NAME" ]; then
        stop_systemd_service "$SYSTEMD_SERVICE_NAME"
    elif [ ${#SYSTEMD_UNITS[@]} -gt 0 ]; then
        warn "stopping matching systemd --user unit(s): ${SYSTEMD_UNITS[*]}"
        for systemd_unit in "${SYSTEMD_UNITS[@]}"; do
            stop_systemd_service "$systemd_unit"
        done
    else
        warn "running instance(s) may be managed by systemd --user"
        warn "check: systemctl --user list-units --type=service --state=running | grep -i changelog"
    fi
fi

for pid in "${PIDS[@]}"; do
    if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
        continue
    fi
    log "Stopping pid=$pid"
    kill -TERM "$pid" || true
done

for attempt in $(seq 1 "$WAIT_SECONDS"); do
    all_stopped=true
    for pid in "${PIDS[@]}"; do
        if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
            continue
        fi
        if kill -0 "$pid" 2>/dev/null; then
            all_stopped=false
            break
        fi
    done

    if [ "$all_stopped" = true ]; then
        rm -f "$PID_FILE"
        log "Stopped ${#PIDS[@]} instance(s)."
        exit 0
    fi

    sleep 1
done

for pid in "${PIDS[@]}"; do
    if ! [[ "$pid" =~ ^[0-9]+$ ]]; then
        continue
    fi
    if kill -0 "$pid" 2>/dev/null; then
        warn "Force stopping pid=$pid"
        kill -KILL "$pid" || true
    fi
done

rm -f "$PID_FILE"
log "Stopped ${#PIDS[@]} instance(s)."
'@

try {
    [System.IO.File]::WriteAllText($tmpBashWindowsPath, $bashScript, [System.Text.UTF8Encoding]::new($false))

    Invoke-WslBot -Arguments @(
        "--",
        "bash",
        "$tmpBashWslPath",
        "$repoWslPath",
        "${WaitSeconds}",
        "$SystemdServiceName"
    )
}
finally {
    if (Test-Path -LiteralPath $tmpBashWindowsPath) {
        Remove-Item -LiteralPath $tmpBashWindowsPath -Force
    }
}

if ($LASTEXITCODE -ne 0) {
    throw "Failed to stop bot. WSL exit code: $LASTEXITCODE"
}
