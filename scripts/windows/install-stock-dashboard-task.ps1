[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-dashboard-runtime',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    # Interactive logon requires an active console session and, even with
    # -WindowStyle Hidden, briefly flashes a conhost window per launch/restart
    # (the same issue documented for the stock-brain scheduled tasks: Hidden
    # only suppresses the window after conhost has already flashed once).
    # S4U runs without a logged-on session and without flashing anything, at
    # the cost of not being able to interact with the desktop (not needed
    # here). Pass -LogonType Interactive only if S4U cannot be granted the
    # "Log on as a batch job" right on this host.
    [ValidateSet('S4U', 'Interactive')][string]$LogonType = 'S4U'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepositoryRoot = if ($RepositoryRoot) { [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\') } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\') }
$script = Join-Path ([IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')) 'scripts\windows\watch-stock-dashboard.ps1'
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw "Missing $script" }
$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
$arguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`" -RepositoryRoot `"$RepositoryRoot`" -PlatformRoot `"$PlatformRoot`""
$action = New-ScheduledTaskAction -Execute $pwsh -Argument $arguments
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType $LogonType -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logon -Principal $principal `
    -Settings $settings -Description 'Keeps the local G-drive stock database, API, dashboard adapter, and LightServer reverse tunnel healthy.' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
