[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-stock-backup-offsite',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RuntimeEnv = '',
    # The nightly DB backup now runs at 04:10 inside the maintenance window;
    # its dump normally takes a couple of minutes, but the 2026-09-16 incident
    # showed it can run for ~an hour when the disk is busy. The off-site run is
    # idempotent, so starting late (or being started again after a failure) is
    # harmless: it uploads whatever backup files are not yet accounted for in
    # the cloud catalog. 05:10 keeps the same one-hour gap after the dump.
    [string]$StartTime = '05:10',
    # Empty means "pick by elevation", the way publish-stock-release.ps1 does:
    # S4U needs the account to hold "Log on as a batch job" and is refused
    # outright when the register call is not elevated, while Interactive needs
    # no privilege but only runs while the operator is logged on.
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
$RuntimeEnv = if ($RuntimeEnv) { $RuntimeEnv } else { Join-Path $platform 'config\runtime.env' }

$offsiteScript = Join-Path $RepositoryRoot 'scripts\windows\run-stock-backup-offsite.ps1'
$hiddenHost = Join-Path $RepositoryRoot 'scripts\windows\run-hidden.vbs'
foreach ($path in @($offsiteScript, $hiddenHost, $RuntimeEnv)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" }
}

$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
$wscript = (Get-Command wscript.exe -ErrorAction Stop).Source
# Same single-quoting-level constraint as install-stock-backup-task.ps1: every
# token is a separate wscript argument and run-hidden.vbs reassembles them.
$innerArguments = @(
    $pwsh, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', $offsiteScript, '-Command', 'nightly',
    '-RuntimeEnv', $RuntimeEnv, '-PlatformRoot', $platform
)
$taskArguments = (@($hiddenHost) + $innerArguments |
    ForEach-Object { if ($_ -match '\s') { '"{0}"' -f $_ } else { $_ } }) -join ' '
$action = New-ScheduledTaskAction -Execute $wscript -Argument $taskArguments -WorkingDirectory $RepositoryRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
# Uploading a multi-GB dump over a home uplink is slow, so the limit is far
# longer than the backup task's; a stuck run must not be killed mid-upload and
# leave the catalog pointing at a half-written object (the run is idempotent,
# but a retry then re-uploads the whole file).
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 6) -RestartCount 2 -RestartInterval (New-TimeSpan -Minutes 30) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$description = 'Encrypt the nightly stock-platform database backup and copy it to Baidu Pan, then prune local copies already verified in the cloud.'

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
