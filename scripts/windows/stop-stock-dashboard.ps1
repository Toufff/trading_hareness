[CmdletBinding()]
param([string]$PlatformRoot = 'G:\StockPlatform')

$ErrorActionPreference = 'Stop'
$logs = Join-Path ([IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')) 'logs'
foreach ($name in 'dashboard-tunnel.pid', 'dashboard-adapter.pid') {
    $path = Join-Path $logs $name
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { continue }
    $pidValue = 0
    [void][int]::TryParse(([IO.File]::ReadAllText($path).Trim()), [ref]$pidValue)
    if ($pidValue -gt 0) { Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue }
    Remove-Item -LiteralPath $path -Force
}
& (Join-Path $PSScriptRoot 'stop-stock-platform.ps1') -PlatformRoot $PlatformRoot
