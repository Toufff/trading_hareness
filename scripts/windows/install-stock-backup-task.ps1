[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-stock-backup',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RuntimeEnv = '',
    [string]$StartTime = '20:30',
    # Interactive logon requires an active console session and, even with
    # -WindowStyle Hidden, briefly flashes a conhost window per run (see
    # scripts/windows/run-hidden.vbs, used below regardless of this choice,
    # for the actual fix). S4U runs whether or not anyone is logged on and
    # needs no stored password; pass -LogonType Password (with -Credential)
    # only where the account cannot be granted "Log on as a batch job".
    [ValidateSet('S4U', 'Password')][string]$LogonType = 'S4U',
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

$backupScript = Join-Path $RepositoryRoot 'scripts\windows\backup-stock-database.ps1'
$hiddenHost = Join-Path $RepositoryRoot 'scripts\windows\run-hidden.vbs'
foreach ($path in @($backupScript, $hiddenHost)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" }
}

$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
$wscript = (Get-Command wscript.exe -ErrorAction Stop).Source
$innerCommand = '"{0}" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{1}" -RuntimeEnv "{2}" -PlatformRoot "{3}"' -f `
    $pwsh, $backupScript, $RuntimeEnv, $platform
$action = New-ScheduledTaskAction -Execute $wscript -Argument ('"{0}" "{1}"' -f $hiddenHost, $innerCommand) -WorkingDirectory $RepositoryRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

if ($LogonType -eq 'S4U') {
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description 'Nightly pg_dump -Fc backup of the authoritative stock-platform database, with SHA-256 evidence and daily/weekly retention.' -Force | Out-Null
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password -RunLevel Limited `
        -Settings $settings -Description 'Nightly pg_dump -Fc backup of the authoritative stock-platform database, with SHA-256 evidence and daily/weekly retention.' -Force | Out-Null
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
