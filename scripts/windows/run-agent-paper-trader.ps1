[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$AccountKey = 'agent-claude-opus',
    [ValidateSet('run-day','model-check')][string]$Command = 'run-day',
    [ValidateSet('claude_cli','dsh','event_research')][string]$Backend = 'claude_cli',
    # The Claude Code CLI needs the terminal proxy (same as the profile's `proxy`); only its subprocess uses it.
    [string]$CliProxy = 'http://127.0.0.1:4537'
)
$ErrorActionPreference = 'Stop'
$env:AGENT_PAPER_CLI_PROXY = $CliProxy
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$arguments = @((Join-Path $root 'scripts\agent-paper-trader.py'), $Command, '--env-file', $RuntimeEnv,
    '--platform-root', $PlatformRoot, '--account-key', $AccountKey, '--backend', $Backend)
& (Join-Path $root '.venv\Scripts\python.exe') @arguments
exit $LASTEXITCODE
