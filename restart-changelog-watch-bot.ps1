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

    [switch]$Tail,

    [int]$TailLines = 80
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $PSScriptRoot -PathType Container)) {
    throw "Script directory not found: $PSScriptRoot"
}

Write-Host "[restart] stopping existing instances..." -ForegroundColor Cyan
& "$PSScriptRoot\stop-changelog-watch-bot.ps1" -WslDistro $WslDistro -WindowsRepoPath $WindowsRepoPath -WaitSeconds $WaitSeconds -SystemdServiceName $SystemdServiceName

Write-Host "[restart] starting fresh instance..." -ForegroundColor Cyan
& "$PSScriptRoot\start-changelog-watch-bot.ps1" -WslDistro $WslDistro -WindowsRepoPath $WindowsRepoPath -Force -DryRun:$DryRun -Once:$Once -BotArgs $BotArgs -Tail:$Tail -TailLines $TailLines

Write-Host "[restart] done." -ForegroundColor Green
