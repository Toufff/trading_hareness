[CmdletBinding()]
param(
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [int]$IntervalSeconds = 30
)

$ErrorActionPreference = 'Continue'
if (-not $RepositoryRoot) { $RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
$module = Join-Path ([IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')) 'scripts\windows\runtime-observability.psm1'
Import-Module $module -Force
$startScript = Join-Path ([IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')) 'scripts\windows\start-stock-dashboard.ps1'
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$log = Join-Path $platform 'logs\dashboard-watchdog.log'
$watchdogRunId = "watchdog-$PID-$([DateTimeOffset]::Now.ToString('yyyyMMddTHHmmss'))"
$consecutiveFailures = 0
[void](Write-RuntimeEvent -PlatformRoot $platform -Service 'dashboard-watchdog' -Event 'watchdog_started' -RunId $watchdogRunId -Data @{
    interval_seconds = [Math]::Max(30, $IntervalSeconds)
    repository_root = [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
})
while ($true) {
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
    }
    Start-Sleep -Seconds ([Math]::Max(30, $IntervalSeconds))
}
