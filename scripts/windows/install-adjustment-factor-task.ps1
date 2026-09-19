[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-adjustment-factors',
    # Defaults point at the published release, not a development checkout: the
    # task must keep working after a publish and must never pin a worktree.
    [string]$RepositoryRoot = 'G:\StockPlatform\current',
    [string]$HostRoot = 'G:\StockPlatform\current',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RuntimeEnv = '',
    # Inside the 04:00-08:00 maintenance window and well clear of the
    # post-close pipeline's 16:40-22:40 retry span.
    [string]$StartTime = '04:30',
    [int]$LookbackDays = 30,
    [ValidateSet('S4U', 'Interactive')][string]$LogonType = 'Interactive'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$RepositoryRoot = [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
$HostRoot = [IO.Path]::GetFullPath($HostRoot).TrimEnd('\')
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$RuntimeEnv = if ($RuntimeEnv) { $RuntimeEnv } else { Join-Path $platform 'config\runtime.env' }

$runner = Join-Path $RepositoryRoot 'scripts\windows\run-adjustment-factor-maintenance.ps1'
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
if (-not (Test-Path -LiteralPath $runner -PathType Leaf)) { throw "Missing $runner" }

$action = New-HiddenPowerShellTaskAction -RepositoryRoot $RepositoryRoot -HostRoot $HostRoot -ScriptPath $runner `
    -ScriptArguments @('-RuntimeEnv', $RuntimeEnv, '-PlatformRoot', $platform,
                       '-LookbackDays', "$LookbackDays")

$trigger = New-ScheduledTaskTrigger -Daily -At $StartTime
# No restart-on-failure and no repetition on purpose. The work list is
# recomputed from real factor coverage on every run, so a missed night is
# picked up by the next one; and a date the coverage gate refuses is reported
# as skipped rather than retried, with a durable consecutive-block counter that
# drops it off the list after five runs (app/adjustment_factor_maintenance.py).
# Retrying a provider that is down would only multiply the provider traffic.
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 45) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType $LogonType -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Force `
    -Description ('Cumulative adjustment-factor backlog: derive quant.daily_adjustment_factors from the licensed ' +
                  'longhu daily kline (provider longhu_qfq_derived) for every settled trading date still missing ' +
                  'a real factor, then promote it onto the daily bars. Read/write to the local database and the ' +
                  'longhu kline route only (no tushare call); no broker operation, no order, and no post-close ' +
                  'pipeline stage depends on it.') | Out-Null

Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
