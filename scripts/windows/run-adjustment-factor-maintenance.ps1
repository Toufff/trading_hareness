[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [int]$LookbackDays = 30,
    [switch]$DryRun
)

# Maintenance-window runner for the cumulative adjustment-factor lane.
#
# The settled full-market cross-section and the corporate-action factor come
# from different providers, so a longhu evening lands with complete bars and
# limits while its adj_factor is still missing. The post-close pipeline runs
# the same lane as a NON-GATING stage; this task is the backlog owner, because
# a route that is unavailable at 17:00 is usually available at 04:30 and the
# evening pipeline must never wait for it.
#
# Exit code: 0 for completed / planned / unchanged / skipped, 1 only when a
# date actually failed (provider error or exception). A date the daily-controls
# coverage gate refuses is reported as skipped with its reason -- this lane
# cannot repair a thin daily cross-section and must not alert every night for
# it (scripts/adjustment-factor-maintenance.py owns that rule).

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# tushare is reached directly; an inherited desktop proxy turns a working route
# into a nightly "provider refused" and would be recorded as a real failure.
foreach ($name in @('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'all_proxy', 'ALL_PROXY')) {
    [Environment]::SetEnvironmentVariable($name, $null, 'Process')
}

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$python = Join-Path $root '.venv\Scripts\python.exe'
$script = Join-Path $root 'scripts\adjustment-factor-maintenance.py'
foreach ($path in @($python, $script, $RuntimeEnv)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" }
}

$logDir = Join-Path $platform 'logs\adjustment-factors'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ((Get-Date).ToString('yyyy-MM-dd') + '.log')

function Write-FactorLog([string]$Message) {
    # One appended line per event, never a credential: the runner only ever
    # passes the env file's PATH to the Python process, which loads it itself.
    [IO.File]::AppendAllText(
        $logFile,
        ((Get-Date).ToString('o') + ' ' + $Message + [Environment]::NewLine),
        [Text.UTF8Encoding]::new($false))
}

$arguments = @($script, 'sync', '--lookback-days', "$LookbackDays", '--env-file', $RuntimeEnv)
if ($DryRun) { $arguments += '--dry-run' }

Write-FactorLog "started lookback_days=$LookbackDays dry_run=$([bool]$DryRun)"
$output = & $python @arguments 2>&1
$exitCode = $LASTEXITCODE
foreach ($line in @($output)) { Write-FactorLog ([string]$line) }
Write-FactorLog "finished exit_code=$exitCode"
if ($exitCode -ne 0) { exit $exitCode }
[string]($output | Select-Object -Last 1)
