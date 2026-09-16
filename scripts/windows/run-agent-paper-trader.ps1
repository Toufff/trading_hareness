[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$AccountKey = 'agent-claude-opus',
    [ValidateSet('run-day','model-check')][string]$Command = 'run-day'
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$arguments = @((Join-Path $root 'scripts\agent-paper-trader.py'), $Command, '--env-file', $RuntimeEnv,
    '--platform-root', $PlatformRoot, '--account-key', $AccountKey)
& (Join-Path $root '.venv\Scripts\python.exe') @arguments
exit $LASTEXITCODE
