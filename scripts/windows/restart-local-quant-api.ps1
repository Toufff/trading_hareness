[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$Repository = '',
    [int]$Port = 5681
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$Repository = if ($Repository) { [IO.Path]::GetFullPath($Repository).TrimEnd('\') } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\') }

& (Join-Path $Repository 'scripts\windows\stop-stock-platform.ps1') -PlatformRoot 'G:\StockPlatform' -ApiPort $Port | Out-Null
& (Join-Path $Repository 'scripts\windows\start-stock-platform.ps1') `
    -PlatformRoot 'G:\StockPlatform' -RepositoryRoot $Repository -ApiPort $Port
