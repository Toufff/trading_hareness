[CmdletBinding()]
param(
    [string]$RepositoryRoot = 'F:\AIWorkflow\trading_hareness',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [int]$IntervalSeconds = 30
)

$ErrorActionPreference = 'Continue'
$startScript = Join-Path ([IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')) 'scripts\windows\start-stock-dashboard.ps1'
$log = Join-Path ([IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')) 'logs\dashboard-watchdog.log'
while ($true) {
    try {
        & $startScript -PlatformRoot $PlatformRoot -RepositoryRoot $RepositoryRoot | Out-Null
    } catch {
        $line = "$(Get-Date -Format o) $($_.Exception.Message)"
        [IO.File]::AppendAllText($log, $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    }
    Start-Sleep -Seconds ([Math]::Max(30, $IntervalSeconds))
}
