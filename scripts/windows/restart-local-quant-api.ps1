[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$Repository = 'F:\AIWorkflow\trading_hareness',
    [int]$Port = 5681
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener) {
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if ($existing.CommandLine -notmatch 'run_server\.py') {
        throw "Port $Port belongs to unexpected process $($existing.ProcessId)"
    }
    Stop-Process -Id $existing.ProcessId -Force
    Wait-Process -Id $existing.ProcessId -ErrorAction SilentlyContinue
}
Remove-Item -LiteralPath 'G:\StockPlatform\logs\quant-api.pid' -Force -ErrorAction SilentlyContinue
& (Join-Path $Repository 'scripts\windows\start-stock-platform.ps1') `
    -PlatformRoot 'G:\StockPlatform' -RepositoryRoot $Repository -ApiPort $Port
