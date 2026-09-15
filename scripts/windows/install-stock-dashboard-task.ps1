[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-dashboard-runtime',
    [string]$RepositoryRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    # Both logon modes use the GUI launcher; Interactive is safe without UAC.
    [ValidateSet('S4U', 'Interactive')][string]$LogonType = 'S4U'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepositoryRoot = if ($RepositoryRoot) { [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\') } else { [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\') }
$script = Join-Path ([IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')) 'scripts\windows\watch-stock-dashboard.ps1'
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw "Missing $script" }
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
$action = New-HiddenPowerShellTaskAction -RepositoryRoot $RepositoryRoot -ScriptPath $script `
    -ScriptArguments @('-RepositoryRoot', $RepositoryRoot, '-PlatformRoot', $PlatformRoot)
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
