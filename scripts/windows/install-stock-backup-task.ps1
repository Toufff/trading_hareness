[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-stock-backup',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RuntimeEnv = '',
    # Moved out of the evening into the 04:00-08:00 maintenance window agreed
    # with the operator (docs/OWNER_DATABASE_STORAGE.md): pg_dump saturates the
    # disk, and the evening slot overlapped post-close publication and the
    # operator's own use of the machine. 04:10 leaves the whole night free and
    # still finishes long before the 06:00 storage-tier job.
    [string]$StartTime = '04:10',
    # Interactive logon requires an active console session and, even with
    # -WindowStyle Hidden, briefly flashes a conhost window per run (see
    # scripts/windows/run-hidden.vbs, used below regardless of this choice,
    # for the actual fix). S4U runs whether or not anyone is logged on and
    # needs no stored password; pass -LogonType Password (with -Credential)
    # only where the account cannot be granted "Log on as a batch job".
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

$backupScript = Join-Path $RepositoryRoot 'scripts\windows\backup-stock-database.ps1'
$hiddenHost = Join-Path $RepositoryRoot 'scripts\windows\run-hidden.vbs'
foreach ($path in @($backupScript, $hiddenHost)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" }
}

$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
$wscript = (Get-Command wscript.exe -ErrorAction Stop).Source
# One quoting level only: every token below is a separate wscript argument and
# run-hidden.vbs reassembles them. Passing the inner command as a single
# pre-quoted argument silently breaks - the nested quotes are eaten by the
# command-line parser, Shell.Run fails, and wscript.exe blocks on a modal error
# dialog until the task hits its execution time limit.
$innerArguments = @(
    $pwsh, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
    '-File', $backupScript, '-RuntimeEnv', $RuntimeEnv, '-PlatformRoot', $platform
)
$taskArguments = (@($hiddenHost) + $innerArguments |
    ForEach-Object { if ($_ -match '\s') { '"{0}"' -f $_ } else { $_ } }) -join ' '
$action = New-ScheduledTaskAction -Execute $wscript -Argument $taskArguments -WorkingDirectory $RepositoryRoot
$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 10) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

if ($LogonType -in @('S4U', 'Interactive')) {
    # Interactive needs no elevation but only runs while the operator is logged on.
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType $LogonType -RunLevel Limited
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
        -Settings $settings -Description 'Nightly pg_dump -Fc backup of the authoritative stock-platform database, with SHA-256 evidence and daily/weekly retention.' -Force | Out-Null
} else {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
        -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password -RunLevel Limited `
        -Settings $settings -Description 'Nightly pg_dump -Fc backup of the authoritative stock-platform database, with SHA-256 evidence and daily/weekly retention.' -Force | Out-Null
}

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
