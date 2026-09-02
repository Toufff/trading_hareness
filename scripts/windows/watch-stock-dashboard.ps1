[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [int]$IntervalSeconds = 30
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $RepositoryRoot) { $RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
$module = Join-Path ([IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')) 'scripts\windows\runtime-observability.psm1'
Import-Module $module -Force
$startScript = Join-Path ([IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')) 'scripts\windows\start-stock-dashboard.ps1'
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$log = Join-Path $platform 'logs\dashboard-watchdog.log'
$watchdogRunId = "watchdog-$PID-$([DateTimeOffset]::Now.ToString('yyyyMMddTHHmmss'))"
$consecutiveFailures = 0
$baseIntervalSeconds = [Math]::Max(30, $IntervalSeconds)
# After MaxConsecutiveFailures back-to-back restart attempts fail, stop
# hammering the target for CooldownSeconds and log a distinct lifecycle event
# instead of retrying every (backed-off) interval forever; a crash loop
# (e.g. a bad release) would otherwise re-run the alembic bootstrap and
# relaunch child processes every ~30s indefinitely.
$maxConsecutiveFailures = 5
$cooldownSeconds = 600
[void](Write-RuntimeEvent -PlatformRoot $platform -Service 'dashboard-watchdog' -Event 'watchdog_started' -RunId $watchdogRunId -Data @{
    interval_seconds = $baseIntervalSeconds
    repository_root = [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
})
while ($true) {
    $sleepSeconds = $baseIntervalSeconds
    try {
        & $startScript -PlatformRoot $PlatformRoot -RepositoryRoot $RepositoryRoot | Out-Null
        if ($consecutiveFailures -gt 0) {
            [void](Write-RuntimeEvent -PlatformRoot $platform -Service 'dashboard-watchdog' -Event 'watchdog_recovered' -RunId $watchdogRunId -Data @{
                previous_consecutive_failures = $consecutiveFailures
            })
        }
        $consecutiveFailures = 0
    } catch {
        $consecutiveFailures += 1
        $line = "$(Get-Date -Format o) $($_.Exception.Message)"
        [IO.File]::AppendAllText($log, $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
        [void](Write-RuntimeEvent -PlatformRoot $platform -Service 'dashboard-watchdog' -Event 'watchdog_iteration_failed' -RunId $watchdogRunId -Level 'error' -Data @{
            consecutive_failures = $consecutiveFailures
            error_type = $_.Exception.GetType().FullName
            message = $_.Exception.Message
            script_stack_trace = [string]$_.ScriptStackTrace
        })
        if ($consecutiveFailures -ge $maxConsecutiveFailures) {
            [void](Write-RuntimeEvent -PlatformRoot $platform -Service 'dashboard-watchdog' -Event 'watchdog_cooldown' -RunId $watchdogRunId -Level 'error' -Data @{
                consecutive_failures = $consecutiveFailures
                cooldown_seconds = $cooldownSeconds
            })
            $sleepSeconds = $cooldownSeconds
            $consecutiveFailures = 0
        } else {
            # Exponential backoff between retries, capped at 5x the base
            # interval so a stuck dependency (e.g. lightServer unreachable)
            # doesn't spin the loop as fast as a healthy one would.
            $sleepSeconds = [Math]::Min($baseIntervalSeconds * 5, $baseIntervalSeconds * [Math]::Pow(2, $consecutiveFailures - 1))
        }
    }
    Start-Sleep -Seconds $sleepSeconds
}
