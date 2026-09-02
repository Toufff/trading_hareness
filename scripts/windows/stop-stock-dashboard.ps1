[CmdletBinding()]
param([string]$PlatformRoot = 'G:\StockPlatform')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'runtime-observability.psm1') -Force
$logs = Join-Path ([IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')) 'logs'
$root = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
foreach ($entry in @(
    @{ Service = 'dashboard-tunnel'; PidFile = 'dashboard-tunnel.pid' },
    @{ Service = 'dashboard-adapter'; PidFile = 'dashboard-adapter.pid' }
)) {
    $state = Request-RuntimeStop -PlatformRoot $root -Service $entry.Service -Reason 'operator_requested' -RequestedBy 'stop-stock-dashboard.ps1'
    $pids = [Collections.Generic.HashSet[int]]::new()
    if ($state) {
        foreach ($property in 'listener_pid', 'launcher_pid') {
            if ($state.PSObject.Properties[$property] -and [int]$state.$property -gt 0) { [void]$pids.Add([int]$state.$property) }
        }
    }
    $name = $entry.PidFile
    $path = Join-Path $logs $name
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        $pidValue = 0
        [void][int]::TryParse(([IO.File]::ReadAllText($path).Trim()), [ref]$pidValue)
        if ($pidValue -gt 0) { [void]$pids.Add($pidValue) }
    }
    foreach ($pidValue in $pids) { Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if ($state -and $state.PSObject.Properties['supervisor_pid']) {
        $supervisorPid = [int]$state.supervisor_pid
        $supervisor = Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue
        if ($supervisor) {
            Wait-Process -Id $supervisorPid -Timeout 3 -ErrorAction SilentlyContinue
            if (Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue) { Stop-Process -Id $supervisorPid -Force -ErrorAction SilentlyContinue }
        }
    }
}
& (Join-Path $PSScriptRoot 'stop-stock-platform.ps1') -PlatformRoot $PlatformRoot
