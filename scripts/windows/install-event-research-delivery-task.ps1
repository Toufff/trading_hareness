[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-event-research-delivery',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [ValidateSet('S4U','Interactive')][string]$LogonType = 'Interactive'
)
$ErrorActionPreference = 'Stop'
$RepositoryRoot = if ($RepositoryRoot) { $RepositoryRoot } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw 'News schedule requires Windows China Standard Time; refusing shifted triggers' }
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
$action = New-HiddenPowerShellTaskAction -RepositoryRoot $RepositoryRoot `
    -ScriptPath (Join-Path $RepositoryRoot 'scripts\windows\run-event-research-delivery.ps1') `
    -ScriptArguments @('-RuntimeEnv', (Join-Path $PlatformRoot 'config\runtime.env'), '-PlatformRoot', $PlatformRoot)
$triggers = @(
    # Daily silent candidates; exchange-calendar gating happens before API/model calls.
    New-ScheduledTaskTrigger -Daily -At '08:55'
    New-ScheduledTaskTrigger -Daily -At '11:55'
    New-ScheduledTaskTrigger -Daily -At '21:55'
)
foreach ($trigger in $triggers) {
    # Completed slots no-op; failed slots retry at most through target + 35min.
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At '08:55' `
        -RepetitionInterval (New-TimeSpan -Minutes 10) -RepetitionDuration (New-TimeSpan -Minutes 40)).Repetition
}
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 8) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType $LogonType -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Description 'Exchange-calendar gated news: trading days 09/12/22; closure endpoints last trading day 22, next trading eve 22, next trading day 09. Silent no-op otherwise; no broker operations.' -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
