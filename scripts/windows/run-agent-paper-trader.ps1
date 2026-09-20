[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$AccountKey = 'agent-claude-opus',
    [ValidateSet('run-day','model-check')][string]$Command = 'run-day',
    [ValidateSet('claude_cli','dsh','event_research')][string]$Backend = 'claude_cli',
    # 2026-09-20: was 5. On 2026-09-18 Opus spent 46 rounds to place 4 orders - 42 of 46 (91%)
    # returned no orders at all. The claude_cli backend runs on the owner's Claude subscription, the
    # same quota as their interactive sessions, so over-sampling competes with their own work.
    # Both backends must share one cadence or the Opus-vs-DSH comparison is confounded.
    [int]$DecisionMinutes = 15,
    # The Claude Code CLI needs the terminal proxy (same as the profile's `proxy`); only its subprocess uses it.
    [string]$CliProxy = 'http://127.0.0.1:4537'
)
$ErrorActionPreference = 'Stop'
$env:AGENT_PAPER_CLI_PROXY = $CliProxy
# WebSearch/WebFetch recorded 0 server-tool calls across 46 live rounds; the tool scaffolding only
# added prompt tokens. Set AGENT_PAPER_TOOLS=WebSearch,WebFetch to restore them.
if (-not $env:AGENT_PAPER_TOOLS) { $env:AGENT_PAPER_TOOLS = '' }
# DSH answers slowly: 2026-09-18 it averaged 100-200s and lost two rounds to the
# 290s cap. The loop only checks whether the last decision is 5 minutes old, so a
# longer call delays the next round instead of overlapping it.
if ($Backend -eq 'dsh') { $env:AGENT_PAPER_TIMEOUT_SECONDS = '420' }
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$arguments = @((Join-Path $root 'scripts\agent-paper-trader.py'), $Command, '--env-file', $RuntimeEnv,
    '--platform-root', $PlatformRoot, '--account-key', $AccountKey, '--backend', $Backend,
    '--decision-minutes', $DecisionMinutes)
& (Join-Path $root '.venv\Scripts\python.exe') @arguments
exit $LASTEXITCODE
