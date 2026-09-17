[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [int]$Port = 15792
)
$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
& (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'scripts\serve-agent-paper-review.py') '--env-file' $RuntimeEnv '--port' $Port
exit $LASTEXITCODE
