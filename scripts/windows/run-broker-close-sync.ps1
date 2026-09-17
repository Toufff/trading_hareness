[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$AccountKey = 'citics-primary',
    # The Claude Code CLI screenshot reader needs the terminal proxy; only its subprocess uses it.
    [string]$CliProxy = 'http://127.0.0.1:4537',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
$env:AGENT_PAPER_CLI_PROXY = $CliProxy
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$arguments = @((Join-Path $root 'scripts\broker-close-sync.py'), '--env-file', $RuntimeEnv, '--platform-root', $PlatformRoot,
    '--account-key', $AccountKey)
if ($DryRun) { $arguments += '--dry-run' }
& (Join-Path $root '.venv\Scripts\python.exe') @arguments
exit $LASTEXITCODE
