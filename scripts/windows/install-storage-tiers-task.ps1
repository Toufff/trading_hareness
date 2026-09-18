[CmdletBinding()]
param(
    [string]$TaskName = 'trading-hareness-storage-tiers',
    # The registered task stores an absolute Execute path and working directory.
    # They default to the published release, never to the checkout this script
    # happens to be run from: a task registered from an F: worktree stops
    # working the moment the worktree is removed, and the failure only shows up
    # at 06:00 in production. Pass -RepositoryRoot explicitly to register a
    # development copy on purpose.
    [string]$RepositoryRoot = 'G:\StockPlatform\current',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$ReleaseRoot = 'G:\StockPlatform\current',
    [string]$RuntimeEnv = '',
    # Inside the 04:00-08:00 maintenance window and after the 04:10 dump and the
    # 05:10 off-site upload, so the tier job never moves rows out from under a
    # running backup.
    [string]$StartTime = '06:00',
    # A long-lived task host must not lock the checkout's bin directory that
    # publishing rebuilds, so the host executable also comes from the release.
    [string]$HostRoot = 'G:\StockPlatform\current',
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
# The job stops ITSELF at the 08:00 deadline run-storage-tiers.ps1 passes, and
# writes its receipt when it does. This limit is only the backstop for a hung
# process: 06:00 + 2h15m leaves fifteen minutes of slack over the deadline, so a
# healthy run is never the one Task Scheduler kills -- and a killed run writes no
# receipt, which is exactly what must not happen every night.
#
# NO restart-on-failure. run-storage-tiers.ps1 exits with the CLI's own exit
# code, so Task Scheduler's "Last Run Result" is the receipt's status verbatim
# -- and every non-zero status this job can produce is a reason to stop, not to
# retry. A retry would run `apply` again the same night, and each run is allowed
# to shave DEFAULT_MAX_SPACE_DAYS = 7 days off a table's hot window: two
# restarts turn the documented "at most 7 days per table per run" into 21, with
# every receipt still reading compliant on its own. -StartWhenAvailable stays,
# because a genuinely missed 06:00 trigger (machine asleep) should still run
# once. If a retry is ever wanted, gate it in the runner for the transient
# statuses only -- never here, where the scheduler cannot tell them apart.
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2 -Minutes 15) `
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

# Print what was actually stored: the Execute path is the one thing a wrong
# -RepositoryRoot/-HostRoot silently breaks, and it is invisible until the task
# fails at 06:00 with "the system cannot find the file specified".
$registered = Get-ScheduledTask -TaskName $TaskName
$registeredAction = @($registered.Actions)[0]
[pscustomobject]@{
    TaskName = $registered.TaskName
    State = $registered.State
    Execute = $registeredAction.Execute
    WorkingDirectory = $registeredAction.WorkingDirectory
    Arguments = $registeredAction.Arguments
}
