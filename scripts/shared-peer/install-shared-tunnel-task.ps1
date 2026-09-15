param(
    [string]$TaskName = "trading-hareness-shared-peer-tunnels",
    [string]$ScriptPath = (Join-Path $PSScriptRoot "start-shared-tunnels.ps1"),
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$SshAlias = 'lightServer1',
    [int]$RemoteDatabasePort = 15432,
    [int]$RemoteApiPort = 15681,
    [int]$LocalDatabasePort = 55432,
    [int]$LocalApiPort = 5681,
    # Both modes use a GUI launcher; Interactive must also remain console-free.
    [ValidateSet('S4U', 'Interactive')][string]$LogonType = 'S4U'
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$resolved = (Resolve-Path -LiteralPath $ScriptPath).Path
$repository = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $resolved) '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\runtime-observability.psm1') -Force
Import-Module (Join-Path $repository 'scripts\windows\background-process.psm1') -Force
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$state = Request-RuntimeStop -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels' -Reason 'task_reinstall' -RequestedBy 'install-shared-tunnel-task.ps1'
# State may point to yesterday's dead PID. Reconcile only live ssh processes
# whose command line owns both exact forwarding tuples; never kill by stale PID.
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -match "127\.0\.0\.1:$RemoteDatabasePort`:127\.0\.0\.1:$LocalDatabasePort" -and
        $_.CommandLine -match "127\.0\.0\.1:$RemoteApiPort`:127\.0\.0\.1:$LocalApiPort"
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$action = New-HiddenPowerShellTaskAction -RepositoryRoot $repository -ScriptPath $resolved -ScriptArguments @(
    '-PlatformRoot', $PlatformRoot, '-SshAlias', $SshAlias, '-RemoteDatabasePort', "$RemoteDatabasePort",
    '-RemoteApiPort', "$RemoteApiPort", '-LocalDatabasePort', "$LocalDatabasePort", '-LocalApiPort', "$LocalApiPort")
# Two triggers on purpose.
#
# -RestartCount/-RestartInterval below do NOT cover the failure that actually
# happens here. Task Scheduler restarts a task whose *engine* could not run it;
# an action that exits nonzero is recorded as a completed run with a return
# code, and nothing restarts it. Measured on 2026-09-04: ssh lost its server
# connection ("client_loop: send disconnect: Connection reset"), the action
# exited 255, and the peer's database and owner-API tunnels simply stayed down
# for as long as nobody looked - the peer kept answering /health out of its own
# process while every call through the gateway returned 500.
#
# The repeating trigger is the actual supervisor: MultipleInstances IgnoreNew
# makes a tick free while the tunnels are up, and the first tick after a drop
# brings them back. It must be a time trigger, not a repetition hung off the
# logon trigger: a logon trigger's repetition only starts when that trigger
# fires, so on a host where the operator logged in hours before the task was
# installed it never starts at all (measured: no recovery 7 minutes after
# killing ssh). A start boundary in the past plus -StartWhenAvailable makes the
# first tick due immediately.
$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# A daily trigger re-arms the repetition every midnight, which is the only way
# to express "forever" that Task Scheduler accepts: an unbounded
# RepetitionDuration is rejected as out of range.
$superviseTrigger = New-ScheduledTaskTrigger -Daily -At (Get-Date).Date
$superviseTrigger.Repetition = (New-ScheduledTaskTrigger -Once -At (Get-Date).Date `
    -RepetitionInterval (New-TimeSpan -Minutes 2) `
    -RepetitionDuration (New-TimeSpan -Hours 23 -Minutes 58)).Repetition
# New-ScheduledTaskTrigger defaults this to true: at the daily boundary it
# killed the task's parent but left detached ssh alive, producing a restart storm.
$superviseTrigger.Repetition.StopAtDurationEnd = $false
$trigger = @($logonTrigger, $superviseTrigger)
$settings = New-ScheduledTaskSettingsSet `
    -Hidden -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType $LogonType -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$deadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    $task = Get-ScheduledTask -TaskName $TaskName
} while ($task.State -ne 'Running' -and [DateTime]::UtcNow -lt $deadline)
if ($task.State -ne 'Running') {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    throw "Shared peer tunnel task did not stay running; last result $($info.LastTaskResult)"
}
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $TaskName
if ($task.State -ne 'Running') {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    throw "Shared peer tunnel task exited during startup; last result $($info.LastTaskResult)"
}

$healthDeadline = [DateTime]::UtcNow.AddSeconds(30)
$remoteHealth = ''
do {
    $probe = Invoke-ConsoleFreeCommand -FilePath (Get-Command ssh.exe).Source -Arguments @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', $SshAlias,
        "curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$RemoteApiPort/health") -TimeoutSeconds 12
    $remoteHealth = $probe.Stdout.Trim()
    if ($remoteHealth -eq '200') { break }
    Start-Sleep -Seconds 1
} while ([DateTime]::UtcNow -lt $healthDeadline)
if ($remoteHealth -ne '200') {
    throw "Shared peer tunnel task is running, but remote API health returned '$remoteHealth' instead of 200"
}

$state = Get-RuntimeState -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels'
if (-not $state -or -not $state.PSObject.Properties['run_id']) {
    throw 'Shared peer tunnel became reachable without a supervised runtime state'
}
$healthyState = @{}
foreach ($property in $state.PSObject.Properties) {
    if ($property.Name -notin @('schema_version', 'service', 'updated_at')) {
        $healthyState[$property.Name] = $property.Value
    }
}
$healthyState.status = 'healthy'
$healthyState.health = 'remote_api_http_200'
$healthyState.verified_at = [DateTimeOffset]::Now.ToString('o')
$healthyState.remote_api_port = $RemoteApiPort
$healthyState.remote_database_port = $RemoteDatabasePort
[void](Set-RuntimeState -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels' -State $healthyState)
[void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels' -Event 'healthy' `
    -RunId ([string]$state.run_id) -Data @{
        health = 'remote_api_http_200'
        remote_api_port = $RemoteApiPort
        remote_database_port = $RemoteDatabasePort
    })
$task | Select-Object TaskName,State,@{Name='RemoteApiHealth';Expression={$remoteHealth}}
