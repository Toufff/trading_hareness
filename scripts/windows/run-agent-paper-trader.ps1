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
# DSH answers slowly: 2026-09-18 it averaged 100-200s and lost two rounds to the
# 290s cap. The loop only checks whether the last decision is 5 minutes old, so a
# longer call delays the next round instead of overlapping it.
if ($Backend -eq 'dsh') { $env:AGENT_PAPER_TIMEOUT_SECONDS = '420' }
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$arguments = @((Join-Path $root 'scripts\agent-paper-trader.py'), $Command, '--env-file', $RuntimeEnv,
    '--platform-root', $PlatformRoot, '--account-key', $AccountKey, '--backend', $Backend)
& (Join-Path $root '.venv\Scripts\python.exe') @arguments
exit $LASTEXITCODE
