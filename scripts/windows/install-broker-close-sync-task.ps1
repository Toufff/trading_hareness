[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-broker-close-sync',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$AccountKey = 'citics-primary'
)
$ErrorActionPreference = 'Stop'
$RepositoryRoot = if ($RepositoryRoot) { $RepositoryRoot } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw 'Close sync requires Windows China Standard Time; refusing shifted triggers' }
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
$action = New-HiddenPowerShellTaskAction -RepositoryRoot $RepositoryRoot `
    -ScriptPath (Join-Path $RepositoryRoot 'scripts\windows\run-broker-close-sync.ps1') `
    -ScriptArguments @('-RuntimeEnv', (Join-Path $PlatformRoot 'config\runtime.env'), '-PlatformRoot', $PlatformRoot, '-AccountKey', $AccountKey)
# Exactly one attempt per day (user authorization 2026-09-17: only the post-close run may be scheduled).
# The script itself skips non-trading days and anything outside 15:00-15:40.
$trigger = New-ScheduledTaskTrigger -Daily -At '15:10'
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 20) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# Interactive: window capture needs the logged-on desktop, and the Claude Code CLI uses that user's login.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Post-close broker holdings sync (read-only; may only click to the 资金股份 page) and agent paper comparison report.' -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
