[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-dashboard-runtime',
    [string]$RepositoryRoot = 'F:\AIWorkflow\trading_hareness'
)

$ErrorActionPreference = 'Stop'
$script = Join-Path ([IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')) 'scripts\windows\watch-stock-dashboard.ps1'
if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { throw "Missing $script" }
$arguments = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$script`""
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument $arguments
$logon = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $logon `
    -Settings $settings -Description 'Keeps the local G-drive stock database, API, dashboard adapter, and LightServer reverse tunnel healthy.' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
