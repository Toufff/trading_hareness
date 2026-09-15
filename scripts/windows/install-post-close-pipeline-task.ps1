[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-post-close-pipeline',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RuntimeEnv = '',
    # First attempt of the day, in exchange time. The pipeline is idempotent
    # and run-post-close-pipeline.ps1 no-ops once the date has landed, so the
    # repetition below is free insurance against a transient upstream failure
    # rather than repeated work.
    [string]$StartTime = '16:40',
    [int]$RetryIntervalMinutes = 30,
    [int]$RetryWindowHours = 6,
    [ValidateSet('S4U', 'Interactive', 'Password')][string]$LogonType = 'S4U',
    [PSCredential]$Credential
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if ($LogonType -eq 'Password' -and -not $Credential) {
    throw 'LogonType Password requires -Credential'
}

$RepositoryRoot = if ($RepositoryRoot) { [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\') } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\') }
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$RuntimeEnv = if ($RuntimeEnv) { $RuntimeEnv } else { Join-Path $platform 'config\runtime.env' }

$runner = Join-Path $RepositoryRoot 'scripts\windows\run-post-close-pipeline.ps1'
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
foreach ($path in @($runner)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" }
}

$action = New-HiddenPowerShellTaskAction -RepositoryRoot $RepositoryRoot -ScriptPath $runner `
    -ScriptArguments @('-RuntimeEnv', $RuntimeEnv, '-PlatformRoot', $platform)

$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
# New-ScheduledTaskTrigger cannot set a repetition on a daily trigger
# directly; borrow the pattern from a throwaway one-time trigger.
$trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $StartTime `
    -RepetitionInterval (New-TimeSpan -Minutes $RetryIntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Hours $RetryWindowHours)).Repetition

$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$description = 'Post-close A-share pipeline: market ingestion, incremental Longhu history, six short-term watchlists, and same-date persisted readback; retry missing stages every 30 minutes.'

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
