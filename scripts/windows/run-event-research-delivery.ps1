[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [switch]$Manual
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$arguments = @((Join-Path $root 'scripts\deliver-event-research.py'), '--env-file', $RuntimeEnv, '--platform-root', $PlatformRoot)
if ($Manual) { $arguments += '--manual' }
& (Join-Path $root '.venv\Scripts\python.exe') @arguments
exit $LASTEXITCODE
