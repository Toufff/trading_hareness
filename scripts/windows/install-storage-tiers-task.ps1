[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-storage-tiers',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$ReleaseRoot = 'G:\StockPlatform\current',
    [string]$RuntimeEnv = '',
    # Inside the 04:00-08:00 maintenance window and after the 04:10 dump and the
    # 05:10 off-site upload, so the tier job never moves rows out from under a
    # running backup.
    [string]$StartTime = '06:00',
    # A long-lived task host must not lock the checkout's bin directory that
    # publishing rebuilds; production passes the published release here.
    [string]$HostRoot = '',
    [ValidateSet('', 'S4U', 'Interactive', 'Password')][string]$LogonType = '',
    [PSCredential]$Credential
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $LogonType) {
    # Same rule as install-stock-backup-offsite-task.ps1: S4U needs elevation to
    # register, Interactive needs none but only runs while the operator is on.
    $elevated = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $LogonType = if ($elevated) { 'S4U' } else { 'Interactive' }
}
if ($LogonType -eq 'Password' -and -not $Credential) {
    throw 'LogonType Password requires -Credential'
}

$RepositoryRoot = if ($RepositoryRoot) { [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\') } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\') }
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$release = [IO.Path]::GetFullPath($ReleaseRoot).TrimEnd('\')
$RuntimeEnv = if ($RuntimeEnv) { $RuntimeEnv } else { Join-Path $platform 'config\runtime.env' }

$runner = Join-Path $RepositoryRoot 'scripts\windows\run-storage-tiers.ps1'
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
foreach ($path in @($runner, $RuntimeEnv)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" }
}

$action = New-HiddenPowerShellTaskAction -RepositoryRoot $RepositoryRoot -ScriptPath $runner -HostRoot $HostRoot `
    -ScriptArguments @('-RuntimeEnv', $RuntimeEnv, '-PlatformRoot', $platform, '-ReleaseRoot', $release)

$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
# A single pass moves bounded batches and is resumable, but a year-long first
# migration of five tables can take hours on the cold HDD; killing it midway is
# safe (every batch is its own transaction) yet wasteful, so the limit is
# generous and the job simply continues on the next night.
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 4) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$description = 'Move owner-database rows older than the hot window into the stock_cold tablespace on G:, enforce the hot-tier space budget, and record the run in logs\storage-tiers.jsonl.'

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
