[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-agent-paper-review',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [int]$Port = 15792
)
$ErrorActionPreference = 'Stop'
$RepositoryRoot = if ($RepositoryRoot) { $RepositoryRoot } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
$action = New-HiddenPowerShellTaskAction -RepositoryRoot $RepositoryRoot -HostRoot (Join-Path $PlatformRoot 'current') `
    -ScriptPath (Join-Path $RepositoryRoot 'scripts\windows\run-agent-paper-review.ps1') `
    -ScriptArguments @('-RuntimeEnv', (Join-Path $PlatformRoot 'config\runtime.env'), '-Port', "$Port")
# Resident read-only page on 127.0.0.1: start at logon, restart if it ever exits.
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description "Local read-only agent paper review page at http://127.0.0.1:$Port/ (no ledger or broker writes)." -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
