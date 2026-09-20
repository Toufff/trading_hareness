[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-intraday-opening-guard',
    [string]$RepositoryRoot = 'G:\StockPlatform\current',
    [string]$HostRoot = 'G:\StockPlatform\current',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RuntimeEnv = '',
    [string]$PreOpenTime = '09:25',
    [string]$LiveTime = '09:32',
    [ValidateSet('S4U','Interactive')][string]$LogonType = 'Interactive'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$RepositoryRoot = [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
$HostRoot = [IO.Path]::GetFullPath($HostRoot).TrimEnd('\')
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$RuntimeEnv = if ($RuntimeEnv) { $RuntimeEnv } else { Join-Path $platform 'config\runtime.env' }
$runner = Join-Path $RepositoryRoot 'scripts\windows\run-intraday-opening-guard.ps1'
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "Missing $runner" }
if ((Get-TimeZone).Id -ne 'China Standard Time') { throw 'Opening guard requires Windows China Standard Time' }

Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
$action = New-HiddenPowerShellTaskAction -RepositoryRoot $RepositoryRoot -HostRoot $HostRoot -ScriptPath $runner `
    -ScriptArguments @('-Stage','Auto','-RepositoryRoot',$RepositoryRoot,'-PlatformRoot',$platform,
                       '-RuntimeEnv',$RuntimeEnv)
$triggers = @(
    (New-ScheduledTaskTrigger -Daily -At $PreOpenTime),
    (New-ScheduledTaskTrigger -Daily -At $LiveTime)
)
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType $LogonType -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $triggers -Settings $settings -Principal $principal `
    -Description '09:25 deterministic readiness and 09:32 real-quote acceptance; one bounded runtime recovery, Feishu receipt, research-only.' `
    -Force | Out-Null
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName,State
