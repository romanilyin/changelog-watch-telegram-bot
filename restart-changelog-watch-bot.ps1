#requires -Version 7.0
[CmdletBinding()]
param(
    [string]$WslDistro = "",

    [string]$WindowsRepoPath = (Resolve-Path $PSScriptRoot).Path,

    [string[]]$BotArgs = @(),

    [switch]$DryRun,

    [switch]$Once,

    [int]$WaitSeconds = 12,

    [string]$SystemdServiceName = "",

    [switch]$CheckOnce,

    [switch]$ForceCheckFailure,

    [switch]$Tail,

    [int]$TailLines = 80
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PSScriptRoot -PathType Container)) {
    throw "Script directory not found: $PSScriptRoot"
}

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

if ($CheckOnce) {
    if (-not (Test-Path -LiteralPath $WindowsRepoPath -PathType Container)) {
        throw "Repo path not found: $WindowsRepoPath"
    }

    $repoWslPath = Convert-ToWslPath $WindowsRepoPath
    $precheckScript = @'
repo_path="${1:?repo path is required}"
cd "$repo_path"
python_bin="${VENV_PYTHON:-$PWD/.venv/bin/python}"
if [ ! -x "$python_bin" ]; then
    python_bin="$(command -v python3 || command -v python)"
fi
"$python_bin" bot.py --once --dry-run
'@

    Write-Host "[restart] running dry-run precheck..." -ForegroundColor Cyan
    Invoke-WslBot -Arguments @("--", "bash", "-c", $precheckScript, "--", $repoWslPath)
    $precheckExitCode = $LASTEXITCODE

    if ($precheckExitCode -ne 0) {
        if (-not $ForceCheckFailure) {
            throw "Dry-run precheck failed. Existing bot was not stopped. WSL exit code: $precheckExitCode"
        }
        Write-Warning "Dry-run precheck failed, continuing because -ForceCheckFailure was passed. WSL exit code: $precheckExitCode"
    }
}

Write-Host "[restart] stopping existing instances..." -ForegroundColor Cyan
& "$PSScriptRoot\stop-changelog-watch-bot.ps1" -WslDistro $WslDistro -WindowsRepoPath $WindowsRepoPath -WaitSeconds $WaitSeconds -SystemdServiceName $SystemdServiceName

Write-Host "[restart] starting fresh instance..." -ForegroundColor Cyan
& "$PSScriptRoot\start-changelog-watch-bot.ps1" -WslDistro $WslDistro -WindowsRepoPath $WindowsRepoPath -Force -DryRun:$DryRun -Once:$Once -BotArgs $BotArgs -Tail:$Tail -TailLines $TailLines

Write-Host "[restart] done." -ForegroundColor Green
