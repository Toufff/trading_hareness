[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-postgres-io-window',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    # Fine enough to follow the window boundaries (09:00, 15:40, 16:30, 23:00,
    # 04:00, 08:00) within a quarter of an hour, coarse enough to be free.
    [int]$IntervalMinutes = 15,
    [string]$HostRoot = '',
    [ValidateSet('', 'S4U', 'Interactive', 'Password')][string]$LogonType = '',
    [PSCredential]$Credential
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $LogonType) {
    $elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $LogonType = if ($elevated) { 'S4U' } else { 'Interactive' }
}
if ($LogonType -eq 'Password' -and -not $Credential) {
    throw 'LogonType Password requires -Credential'
}

$RepositoryRoot = if ($RepositoryRoot) { [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\') } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\') }
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')

$runner = Join-Path $RepositoryRoot 'scripts\windows\set-postgres-io-window.ps1'
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "Missing $runner" }

$action = New-HiddenPowerShellTaskAction -RepositoryRoot $RepositoryRoot -ScriptPath $runner -HostRoot $HostRoot `
    -ScriptArguments @('-PlatformRoot', $platform)

# A daily trigger cannot carry a repetition directly; borrow it from a
# throwaway one-time trigger, the way install-post-close-pipeline-task.ps1 does.
# The repetition is indefinite: the script is a cheap idempotent no-op whenever
# the mode has not changed.
$trigger = New-ScheduledTaskTrigger -Daily -At '00:02'
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At '00:02' `
    -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) `
    -RepetitionDuration ([TimeSpan]::MaxValue)).Repetition

$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$description = 'Every 15 minutes, put every postgres.exe at Normal priority inside the trading/review/maintenance windows and BelowNormal (low I/O priority) outside them, so background database work never makes the workstation feel slow.'

if ($LogonType -in @('S4U', 'Interactive')) {
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType $LogonType -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description $description -Force | Out-Null
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password -RunLevel Limited `
        -Settings $settings -Description $description -Force | Out-Null
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
