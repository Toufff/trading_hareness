param(
    # 'intraday' installs the historical task unchanged. 'batch' installs the
    # separate bulk-traffic tunnel (see shared-tunnel-profiles.psm1). The two
    # are independent scheduled tasks, runtime services and lock files; one can
    # be reinstalled or stopped without touching the other.
    [ValidateSet('intraday', 'batch')][string]$Profile = 'intraday',
    # Empty means "the profile's own name". An explicit value still wins so an
    # operator can stage a second task, as before.
    [string]$TaskName = '',
    [string]$ScriptPath = (Join-Path $PSScriptRoot "start-shared-tunnels.ps1"),
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$SshAlias = 'lightServer1',
    [int]$RemoteDatabasePort = 15432,
    [int]$RemoteApiPort = 15681,
    [int]$RemoteBatchDatabasePort = 15433,
    [int]$LocalDatabasePort = 55432,
    [int]$LocalApiPort = 5681,
    # Both modes use a GUI launcher; Interactive must also remain console-free.
    [ValidateSet('S4U', 'Interactive')][string]$LogonType = 'S4U',
    # Report the plan and exit. Registers nothing, stops nothing, kills nothing
    # and writes no runtime state - safe to run on the live host.
    [switch]$WhatIf
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$resolved = (Resolve-Path -LiteralPath $ScriptPath).Path
$repository = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $resolved) '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\runtime-observability.psm1') -Force
Import-Module (Join-Path $repository 'scripts\windows\background-process.psm1') -Force
Import-Module (Join-Path $repository 'scripts\shared-peer\shared-tunnel-profiles.psm1') -Force
$tunnelProfile = Get-SharedTunnelProfile -Name $Profile `
    -RemoteDatabasePort $RemoteDatabasePort -RemoteApiPort $RemoteApiPort `
    -RemoteBatchDatabasePort $RemoteBatchDatabasePort `
    -LocalDatabasePort $LocalDatabasePort -LocalApiPort $LocalApiPort
$service = $tunnelProfile.Service
if (-not $TaskName) { $TaskName = $tunnelProfile.TaskName }
$scriptArguments = @(
    '-Profile', $tunnelProfile.Name,
    '-PlatformRoot', $PlatformRoot, '-SshAlias', $SshAlias, '-RemoteDatabasePort', "$RemoteDatabasePort",
    '-RemoteApiPort', "$RemoteApiPort", '-RemoteBatchDatabasePort', "$RemoteBatchDatabasePort",
    '-LocalDatabasePort', "$LocalDatabasePort", '-LocalApiPort', "$LocalApiPort")

if ($WhatIf) {
    # The scheduled action can only be materialized where the native task host
    # has been built (publishing builds it; a fresh checkout has not). Report
    # that honestly instead of failing the dry run or pretending it exists.
    $taskHost = Join-Path $repository 'scripts\windows\bin\stock-background-host.exe'
    $plannedAction = if (Test-Path -LiteralPath $taskHost -PathType Leaf) {
        $built = New-HiddenPowerShellTaskAction -RepositoryRoot $repository -ScriptPath $resolved -ScriptArguments $scriptArguments
        [pscustomobject]@{ Execute = $built.Execute; Arguments = $built.Arguments }
    } else {
        [pscustomobject]@{ Execute = "$taskHost (not built in this checkout)"; Arguments = ($scriptArguments -join ' ') }
    }
    [pscustomobject][ordered]@{
        WhatIf = $true
        Profile = $tunnelProfile.Name
        TaskName = $TaskName
        Service = $service
        ScriptPath = $resolved
        ScriptArguments = $scriptArguments
        Forwards = @($tunnelProfile.Forwards)
        SshOptions = @($tunnelProfile.SshOptions)
        Compression = [bool]$tunnelProfile.Compression
        # The whole vector, so a dry run shows the multiplexing options that
        # make "its own TCP connection" true rather than assumed. The
        # destination shown is the fallback alias; the real one is resolved at
        # launch by Resolve-OwnerTunnelSshTarget.
        SshArguments = @(Get-SharedTunnelSshArgument -TunnelProfile $tunnelProfile -Destination $SshAlias)
        ReclaimablePorts = @($tunnelProfile.RemotePorts)
        HealthCheck = $tunnelProfile.HealthCheck
        LogonType = $LogonType
        SuperviseIntervalMinutes = 2
        PlannedAction = $plannedAction
        ScheduledTasksTouched = $false
        RuntimeStateTouched = $false
    }
    return
}
function Stop-TunnelInstallOnFailure {
    # Intraday is a dependency: a failed install must stay registered so the
    # two-minute supervising trigger keeps trying to bring the platform's only
    # owner->peer connection back. Batch is an optimization, and
    # install-shared-tunnel-tasks.ps1 swallows its throw, so a registered batch
    # task that cannot pass its health check would retry every two minutes for
    # as long as the host is up - reclaiming ports, spawning ssh clients and
    # writing events that nobody asked for, with nothing to stop it. Disable it
    # here so the failure is bounded: the task stays registered (an operator can
    # read it and re-enable it) but stops running.
    param([Parameter(Mandatory)][string]$Message)
    if ($tunnelProfile.Name -eq 'batch') {
        try {
            Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
            [void](Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
            [void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service $service `
                -Event 'install_health_failed' -Level 'error' -Data @{
                    task_name = $TaskName
                    task_disabled = $true
                    reason = $Message
                })
        } catch {
            Write-Warning "Could not disable the failed batch tunnel task '$TaskName': $($_.Exception.Message)"
        }
    }
    throw $Message
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
# The PRE-install state, and above all its run_id: this is the run the health
# gate below must NOT mistake for its own. Request-RuntimeStop returns $null when
# there is no previous supervised run, which is the same thing as "no run_id to
# be confused with".
$previousState = Request-RuntimeStop -PlatformRoot $PlatformRoot -Service $service -Reason 'task_reinstall' -RequestedBy 'install-shared-tunnel-task.ps1'
$previousRunId = if ($previousState -and $previousState.PSObject.Properties['run_id'] -and $previousState.run_id) {
    [string]$previousState.run_id
} else { '' }
# State may point to yesterday's dead PID. Reconcile only live ssh processes
# whose command line owns this profile's exact forwarding tuples; never kill by
# stale PID, and never kill the other profile's connection.
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { Test-SharedTunnelCommandLine -TunnelProfile $tunnelProfile -CommandLine $_.CommandLine } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$action = New-HiddenPowerShellTaskAction -RepositoryRoot $repository -ScriptPath $resolved -ScriptArguments $scriptArguments
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
# Everything the health check accepts as evidence must post-date this instant,
# or a leftover 'healthy' state file from an earlier run would pass for proof
# that THIS install came up.
$installStartedAt = [DateTimeOffset]::Now
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
    Stop-TunnelInstallOnFailure -Message "Shared peer tunnel task did not stay running; last result $($info.LastTaskResult)"
}
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $TaskName
if ($task.State -ne 'Running') {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Stop-TunnelInstallOnFailure -Message "Shared peer tunnel task exited during startup; last result $($info.LastTaskResult)"
}

# Health is profile-specific. The intraday tunnel carries the owner API, so an
# HTTP 200 through it proves the whole chain. The batch tunnel carries only the
# database forward and there is no HTTP endpoint behind it, so no owner-side
# probe can prove end to end that a query flows; that proof is
# deploy-batch-tunnel-port.py's authenticated SELECT 1 + inet_server_port() from
# the peer's quant-research container, which runs on the peer.
#
# What the batch check CAN prove, and now does, is that the listener on 15433 is
# THIS install's listener rather than any process that happens to hold the port:
#   1. lightServer publishes a loopback listener on 15433 (`ss -ltn`), and
#   2. a local ssh.exe whose command line carries this profile's exact
#      forwarding tuple is alive, and
#   3. the supervised runtime state was written by this install - a run_id
#      different from the one read before the install AND a started_at not older
#      than the moment the task was registered - or, failing that, the run the
#      state names is still owned by a live supervisor process (the
#      duplicate_start_skipped case, where the tunnel is up but the previous
#      supervisor still holds the lock and therefore the state).
#      started_at is the field supervise-runtime-process.ps1 writes; an earlier
#      version of this gate asserted on requested_at, which never reaches the
#      state file, so it could not pass.
# Without (2) and (3) a foreign process that grabbed 15433 between the reclaim
# and the probe was reported as health='remote_listener_open'. The claim is
# still weaker than the intraday HTTP 200 and is still labelled as such.
$healthDeadline = [DateTime]::UtcNow.AddSeconds(30)
$sshPath = (Get-Command ssh.exe).Source
if ($tunnelProfile.HealthCheck -eq 'remote_api_http') {
    $remoteHealth = ''
    do {
        $probe = Invoke-ConsoleFreeCommand -FilePath $sshPath -Arguments @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', $SshAlias,
            "curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$($tunnelProfile.RemoteApiPort)/health") -TimeoutSeconds 12
        $remoteHealth = $probe.Stdout.Trim()
        if ($remoteHealth -eq '200') { break }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $healthDeadline)
    if ($remoteHealth -ne '200') {
        Stop-TunnelInstallOnFailure -Message "Shared peer tunnel task is running, but remote API health returned '$remoteHealth' instead of 200"
    }
    $healthLabel = 'remote_api_http_200'
} else {
    $batchPort = $tunnelProfile.RemoteDatabasePort
    $listenerUp = $false
    $localClient = $null
    do {
        $probe = Invoke-ConsoleFreeCommand -FilePath $sshPath -Arguments @('-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', $SshAlias,
            "ss -ltn 'sport = :$batchPort' | tail -n +2 | grep -q .") -TimeoutSeconds 12
        # An open remote port is not evidence on its own: `ss` reports whoever
        # holds it. Only accept it together with a live local client of this
        # profile, so the listener is attributable to this tunnel.
        $localClient = @(Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
            Where-Object { Test-SharedTunnelCommandLine -TunnelProfile $tunnelProfile -CommandLine $_.CommandLine }) |
            Select-Object -First 1
        $listenerUp = ($probe.ExitCode -eq 0) -and $null -ne $localClient
        if ($listenerUp) { break }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $healthDeadline)
    if (-not $listenerUp) {
        $detail = if ($null -eq $localClient) { 'no local ssh client owns the batch forward' }
                  else { "lightServer published no loopback listener on $batchPort" }
        Stop-TunnelInstallOnFailure -Message "Batch tunnel task is running, but $detail"
    }
    $remoteHealth = "listening:$batchPort"
    $healthLabel = 'remote_listener_open_owned_by_local_client'
}

$freshness = $null
$liveness = $null
if ($tunnelProfile.HealthCheck -eq 'remote_api_http') {
    $state = Get-RuntimeState -PlatformRoot $PlatformRoot -Service $service
} else {
    # Third leg of the batch claim: the state must belong to this install. The
    # judgement (and the failure message) is built by a pure function that reads
    # every field through PSObject.Properties, so a state file missing the field
    # produces a bounded failure instead of a StrictMode throw that would skip
    # Stop-TunnelInstallOnFailure and leave the task retrying every two minutes.
    #
    # It is POLLED, not judged once. The state file is written by whichever
    # supervisor owns <service>.lock, and the ssh client that just satisfied legs
    # 1 and 2 can be up before that write lands. Judging once turned that race
    # into a disabled batch task.
    #
    # Two pieces of evidence, both of which this script already has:
    #   * $previousRunId - the run_id from before the install. This install's
    #     supervisor mints a new one, so a state still carrying the old id was
    #     not written by this install, no matter what its clock says.
    #   * the liveness of the supervisor named by the state. A supervisor that
    #     cannot take the lock exits via duplicate_start_skipped and writes
    #     nothing (supervise-runtime-process.ps1:27-32), so the previous run_id
    #     can legitimately persist while the tunnel is up and serving. That is
    #     accepted - as 'duplicate_supervisor_still_serving', not as this
    #     install's own state - because disabling a healthy batch tunnel is a
    #     worse failure than accepting an older run that demonstrably owns it.
    #
    # The poll therefore breaks on `fresh`, NOT on `accept`. Breaking on accept
    # let the weaker claim win a race against this install's own state: a run
    # started minutes ago (the two-minute supervising trigger, or
    # install-shared-tunnel-tasks.ps1 doing both profiles) is still alive on its
    # pid for the first seconds of this install, so iteration one accepted the
    # previous run and stopped looking - and the state it accepted is by
    # construction the one Request-RuntimeStop above just stamped
    # 'stop_requested'. The judge now refuses a stopping state outright
    # ('previous_run_stopping'), and the weaker accept is kept as a FALLBACK: it
    # is only used when the whole 30 s elapsed without this install's own
    # supervisor writing anything, which is the genuine duplicate_start_skipped
    # case. The cost is that a real duplicate_start_skipped install polls for the
    # full deadline; that is 30 seconds once, on an install, and it buys the
    # guarantee that this install's own state always wins when it is going to
    # arrive at all.
    $freshnessDeadline = [DateTime]::UtcNow.AddSeconds(30)
    $acceptedFallback = $null
    do {
        $state = Get-RuntimeState -PlatformRoot $PlatformRoot -Service $service
        $supervisorProcess = $null
        if ($state -and $state.PSObject.Properties['supervisor_pid'] -and $state.supervisor_pid) {
            # A state file carrying a non-numeric supervisor_pid must not throw
            # an InvalidCastException here: that throw would escape
            # Stop-TunnelInstallOnFailure and leave the batch task enabled and
            # retrying - the same class of failure the pure judges exist to
            # avoid. A pid that cannot be parsed simply vouches for nothing.
            $supervisorPid = 0
            if ([int]::TryParse([string]$state.supervisor_pid, [ref]$supervisorPid)) {
                $supervisorProcess = Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue
            }
        }
        $liveness = Get-SharedTunnelSupervisorLiveness -State $state -Process $supervisorProcess
        $freshness = Get-SharedTunnelStateFreshnessVerdict -State $state `
            -InstallStartedAt $installStartedAt -PreviousRunId $previousRunId `
            -SupervisorAlive $liveness.alive
        if ($freshness.fresh) { break }
        if ($freshness.accept) {
            # Remember the whole triple, not just the verdict: the state and the
            # liveness reason that go into the receipt below must be the ones
            # this verdict was taken against.
            $acceptedFallback = [pscustomobject]@{
                Freshness = $freshness; State = $state; Liveness = $liveness
            }
        }
        Start-Sleep -Seconds 1
    } while ([DateTime]::UtcNow -lt $freshnessDeadline)
    if (-not $freshness.fresh -and $null -ne $acceptedFallback) {
        $freshness = $acceptedFallback.Freshness
        $state = $acceptedFallback.State
        $liveness = $acceptedFallback.Liveness
    }
}
if (-not $state -or -not $state.PSObject.Properties['run_id']) {
    Stop-TunnelInstallOnFailure -Message 'Shared peer tunnel became reachable without a supervised runtime state'
}
if ($null -ne $freshness -and -not $freshness.accept) {
    # Why the supervisor could not vouch for the run is the first thing an
    # operator needs here, and a refused install writes no runtime state, so the
    # message is the only place it can be recorded.
    $livenessDetail = if ($null -ne $liveness) {
        " (supervisor liveness: $($liveness.reason), pid '$($liveness.supervisor_pid)')"
    } else { '' }
    Stop-TunnelInstallOnFailure -Message ($freshness.message + $livenessDetail)
}
if ($null -ne $freshness -and -not $freshness.fresh) {
    # Accepted, but by the weaker claim. Say so in the health label rather than
    # reporting the same string as an install whose own supervisor wrote the
    # state.
    $healthLabel = 'remote_listener_open_owned_by_live_supervisor'
}
$healthyState = @{}
foreach ($property in $state.PSObject.Properties) {
    if ($property.Name -notin @('schema_version', 'service', 'updated_at')) {
        $healthyState[$property.Name] = $property.Value
    }
}
$healthyState.status = 'healthy'
$healthyState.health = $healthLabel
$healthyState.verified_at = [DateTimeOffset]::Now.ToString('o')
$healthyState.tunnel_profile = $tunnelProfile.Name
$healthyState.remote_api_port = $tunnelProfile.RemoteApiPort
$healthyState.remote_database_port = $tunnelProfile.RemoteDatabasePort
$eventData = @{
    health = $healthLabel
    tunnel_profile = $tunnelProfile.Name
    remote_api_port = $tunnelProfile.RemoteApiPort
    remote_database_port = $tunnelProfile.RemoteDatabasePort
}
if ($null -ne $freshness) {
    # Which claim was accepted, and against which previous run, is the whole
    # point of the gate - keep the receipt where an operator will find it.
    $healthyState.state_freshness = [string]$freshness.reason
    $healthyState.previous_run_id = [string]$freshness.previous_run_id
    $healthyState.state_status = [string]$freshness.status
    $eventData.state_freshness = [string]$freshness.reason
    $eventData.previous_run_id = [string]$freshness.previous_run_id
    $eventData.state_status = [string]$freshness.status
    # The liveness verdict is the OTHER half of the claim, and the branch's own
    # rollout notes tell the operator to check it on the first real install
    # ("the receipt should say supervisor_process_owns_this_run, not
    # process_start_time_unavailable"). It was computed and thrown away, so that
    # receipt did not exist anywhere. Persist both the verdict and the pid it was
    # taken against, in the state file and in the lifecycle event.
    $livenessReason = if ($null -ne $liveness) { [string]$liveness.reason } else { 'not_measured' }
    $livenessPid = if ($null -ne $liveness) { [string]$liveness.supervisor_pid } else { '' }
    $healthyState.supervisor_liveness = $livenessReason
    $healthyState.supervisor_pid_checked = $livenessPid
    $eventData.supervisor_liveness = $livenessReason
    $eventData.supervisor_pid_checked = $livenessPid
}
[void](Set-RuntimeState -PlatformRoot $PlatformRoot -Service $service -State $healthyState)
[void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service $service -Event 'healthy' `
    -RunId ([string]$state.run_id) -Data $eventData)
$task | Select-Object TaskName,State,
    @{Name='Profile';Expression={$tunnelProfile.Name}},
    @{Name='Health';Expression={$healthLabel}},
    @{Name='RemoteApiHealth';Expression={$remoteHealth}}
