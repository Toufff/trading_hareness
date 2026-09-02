[CmdletBinding()]
param(
    [string]$PlatformRoot = 'G:\StockPlatform',
    [int]$ApiPort = 5681
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'runtime-observability.psm1') -Force
$root = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$pidPath = Join-Path $root 'logs\quant-api.pid'
$listener = Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
$state = Request-RuntimeStop -PlatformRoot $root -Service 'quant-api' -Reason 'operator_requested' -RequestedBy 'stop-stock-platform.ps1'
if (-not $listener -and -not $state) {
    [pscustomobject]@{ status = 'not_running' }
    return
}
$pids = [Collections.Generic.HashSet[int]]::new()
if ($listener) { [void]$pids.Add([int]$listener.OwningProcess) }
if ($state) {
    foreach ($property in 'listener_pid', 'launcher_pid') {
        if ($state.PSObject.Properties[$property] -and [int]$state.$property -gt 0) { [void]$pids.Add([int]$state.$property) }
    }
}
foreach ($pidValue in $pids) { Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue }
$deadline = [DateTime]::UtcNow.AddSeconds(10)
while ((Get-NetTCPConnection -LocalPort $ApiPort -State Listen -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $deadline) {
    Start-Sleep -Milliseconds 200
}
if ($state -and $state.PSObject.Properties['supervisor_pid']) {
    $supervisorPid = [int]$state.supervisor_pid
    $supervisor = Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue
    if ($supervisor) {
        Wait-Process -Id $supervisorPid -Timeout 5 -ErrorAction SilentlyContinue
        if (Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue) { Stop-Process -Id $supervisorPid -Force -ErrorAction SilentlyContinue }
    }
}
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
[pscustomobject]@{ status = 'stopped'; pids = @($pids) }
