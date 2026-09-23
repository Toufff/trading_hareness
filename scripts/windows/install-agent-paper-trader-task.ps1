[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-agent-paper-trader',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$AccountKey = 'agent-claude-opus',
    [ValidateSet('claude_cli','codex_cli','dsh','event_research','jev')][string]$Backend = 'claude_cli',
    [string]$Model = '',
    [string]$ProviderEnvFile = '',
    [ValidateSet('','none','minimal','low','medium','high','xhigh','max','ultra')][string]$ReasoningEffort = '',
    [ValidateSet('S4U','Interactive')][string]$LogonType = 'Interactive'
)
$ErrorActionPreference = 'Stop'
$RepositoryRoot = if ($RepositoryRoot) { $RepositoryRoot } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw 'Agent paper session requires Windows China Standard Time; refusing shifted triggers' }
if ($Backend -eq 'jev' -and -not $ProviderEnvFile) { throw 'JEV scheduled paper trading requires -ProviderEnvFile' }
if ($ProviderEnvFile) {
    $ProviderEnvFile = [IO.Path]::GetFullPath($ProviderEnvFile)
    if (-not (Test-Path -LiteralPath $ProviderEnvFile -PathType Leaf)) { throw 'Agent paper provider environment file is missing' }
}
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
$scriptArguments = @('-RuntimeEnv', (Join-Path $PlatformRoot 'config\runtime.env'), '-PlatformRoot', $PlatformRoot,
    '-AccountKey', $AccountKey, '-Backend', $Backend)
if ($Model) { $scriptArguments += @('-Model', $Model) }
if ($ProviderEnvFile) { $scriptArguments += @('-ProviderEnvFile', $ProviderEnvFile) }
if ($ReasoningEffort) { $scriptArguments += @('-ReasoningEffort', $ReasoningEffort) }
$action = New-HiddenPowerShellTaskAction -RepositoryRoot $RepositoryRoot -HostRoot (Join-Path $PlatformRoot 'current') `
    -ScriptPath (Join-Path $RepositoryRoot 'scripts\windows\run-agent-paper-trader.ps1') `
    -ScriptArguments $scriptArguments
$trigger = New-ScheduledTaskTrigger -Daily -At '09:20'
# The day loop is restart-safe and single-instance (file lock); repetition only
# restarts it after a crash or a late logon, and stops being attempted at 15:10.
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At '09:20' `
    -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Minutes 350)).Repetition
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 6) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# Interactive: the Claude Code and Codex CLIs use the logged-on user's existing login.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType $LogonType -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Paper-only LLM trader: exchange-calendar gated 09:30-15:00 loop on its own simulated account. No broker operations.' -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
