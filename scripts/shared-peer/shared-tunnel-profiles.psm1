Set-StrictMode -Version Latest

# Owner -> lightServer reverse tunnel profiles.
#
# There were two forwards on one SSH connection for a year: the owner database
# (15432 -> 55432) and the owner API (15681 -> 5681). That is fine for the
# intraday request/response traffic it was built for, but a bulk job (COPY,
# backfill, a full-table read) fills the single TCP connection's window, and
# every intraday query then queues behind it. The peer author measured 52.5 ms
# RTT on this link, so one saturated connection is not a small penalty.
#
# SSH multiplexes channels over one TCP connection, so a second forward on the
# same connection does NOT get its own window - the only way to stop bulk
# traffic from starving intraday traffic is a second connection. That is what
# the 'batch' profile is: its own ssh process, its own scheduled task, its own
# supervised runtime service, its own remote port (15433 -> 55432), and
# compression enabled because bulk result sets compress well and the link is
# latency- rather than CPU-bound.
#
# 'intraday' must stay exactly what it was: no compression, ports 15432/15681,
# service 'shared-peer-tunnels', task 'trading-hareness-shared-peer-tunnels'.
# Get-SharedTunnelSshArgument is the single place that builds the ssh argument
# vector, and the profile test pins the intraday vector literally so a future
# edit here cannot quietly change the live intraday tunnel.
#
# The one deliberate exception to "intraday byte for byte" is the connection
# multiplexing block below. The whole point of this feature is that batch gets
# its OWN TCP connection; today that holds only because `ssh -G lightServer1`
# happens to report controlmaster false. One `ControlMaster auto` +
# `ControlPath ~/.ssh/cm-%r@%h:%p` line in ~/.ssh/config - a routine latency
# tweak on a 52.5 ms link - would silently make the batch client open a channel
# on the intraday client's existing socket, and bulk traffic would go straight
# back into the window it was moved out of, with nothing failing and no way to
# see it except by counting sockets. So both profiles pin the property instead
# of inheriting it: -o ControlMaster=no -o ControlPath=none. This DOES change
# the intraday argument vector (the pinned vector in
# test-shared-tunnel-profiles.ps1 was updated with it), and it is a no-op
# against the current host configuration - it only removes the ability of a
# later ssh_config edit to collapse the two connections into one.

function Get-SharedTunnelProfile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][ValidateSet('intraday', 'batch')][string]$Name,
        [int]$RemoteDatabasePort = 15432,
        [int]$RemoteApiPort = 15681,
        [int]$RemoteBatchDatabasePort = 15433,
        [int]$LocalDatabasePort = 55432,
        [int]$LocalApiPort = 5681
    )
    if ($Name -eq 'batch') {
        return [pscustomobject]@{
            Name = 'batch'
            Service = 'shared-peer-batch-tunnel'
            TaskName = 'trading-hareness-shared-peer-batch-tunnel'
            # The reclaim guard's allowed set. 15433 is allowed ONLY here; the
            # intraday profile must never be able to reclaim the batch port and
            # vice versa, because an unrelated listener on the shared host must
            # stay out of reach of `fuser -k`.
            RemotePorts = @($RemoteBatchDatabasePort)
            Forwards = @("127.0.0.1:$RemoteBatchDatabasePort`:127.0.0.1:$LocalDatabasePort")
            SshOptions = @('-o', 'Compression=yes')
            # Observed, not inferred. The runtime metadata used to derive this
            # from `SshOptions.Count -gt 0`, which is only true by coincidence:
            # the first non-compression option added to either profile would
            # have made the recorded value a lie, and the recorded value is the
            # one field an operator reads to tell the two connections apart.
            Compression = $true
            HealthCheck = 'remote_listener'
            RemoteDatabasePort = $RemoteBatchDatabasePort
            RemoteApiPort = 0
            LocalDatabasePort = $LocalDatabasePort
            LocalApiPort = 0
        }
    }
    return [pscustomobject]@{
        Name = 'intraday'
        Service = 'shared-peer-tunnels'
        TaskName = 'trading-hareness-shared-peer-tunnels'
        RemotePorts = @($RemoteDatabasePort, $RemoteApiPort)
        Forwards = @(
            "127.0.0.1:$RemoteDatabasePort`:127.0.0.1:$LocalDatabasePort",
            "127.0.0.1:$RemoteApiPort`:127.0.0.1:$LocalApiPort"
        )
        SshOptions = @()
        Compression = $false
        HealthCheck = 'remote_api_http'
        RemoteDatabasePort = $RemoteDatabasePort
        RemoteApiPort = $RemoteApiPort
        LocalDatabasePort = $LocalDatabasePort
        LocalApiPort = $LocalApiPort
    }
}

function Get-SharedTunnelSshArgument {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][psobject]$TunnelProfile,
        [string[]]$ConnectionArguments = @(),
        [Parameter(Mandatory)][string]$Destination
    )
    $forwardArguments = @(foreach ($forward in $TunnelProfile.Forwards) { '-R'; $forward })
    return @($ConnectionArguments) + @(
        '-NT',
        '-o', 'BatchMode=yes',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=30',
        '-o', 'ServerAliveCountMax=3',
        # See the header: this is what makes "its own TCP connection" a
        # property of the command line rather than of ~/.ssh/config.
        '-o', 'ControlMaster=no',
        '-o', 'ControlPath=none'
    ) + @($TunnelProfile.SshOptions) + $forwardArguments + @($Destination)
}

function Get-SharedTunnelProcessPattern {
    # A live ssh client is identified by the exact forwarding tuples on its
    # command line, never by a stored PID: a stale PID is reused by unrelated
    # processes and killing it is how an operator loses something else.
    [CmdletBinding()]
    param([Parameter(Mandatory)][psobject]$TunnelProfile)
    return @(foreach ($forward in $TunnelProfile.Forwards) { [regex]::Escape($forward) })
}

function Test-SharedTunnelCommandLine {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][psobject]$TunnelProfile,
        [AllowNull()][AllowEmptyString()][string]$CommandLine
    )
    if (-not $CommandLine) { return $false }
    foreach ($pattern in (Get-SharedTunnelProcessPattern -TunnelProfile $TunnelProfile)) {
        if ($CommandLine -notmatch $pattern) { return $false }
    }
    return $true
}

function Get-SharedTunnelConnectionVerdict {
    # Pure judge for "the two tunnels really are two TCP connections".
    #
    # -o ControlMaster=no makes multiplexing impossible from the command line,
    # but that is a claim about ssh's configuration, not an observation of the
    # host. This function judges the observation: each side passes the four
    # tuples (LocalAddress, LocalPort, RemoteAddress, RemotePort) of the TCP
    # connections owned by its ssh.exe, and a shared tuple - or a shared owning
    # process - means the feature silently degraded to one connection.
    #
    # It deliberately refuses to call an unobserved pair 'distinct': zero
    # connections on either side proves nothing, and reporting proof from
    # absence is how this check would become decoration.
    [CmdletBinding()]
    param(
        [AllowEmptyCollection()][object[]]$IntradayConnections = @(),
        [AllowEmptyCollection()][object[]]$BatchConnections = @(),
        [int]$IntradayProcessId = 0,
        [int]$BatchProcessId = 0
    )
    function Get-Key([object]$Connection) {
        return '{0}:{1}->{2}:{3}' -f $Connection.LocalAddress, $Connection.LocalPort,
            $Connection.RemoteAddress, $Connection.RemotePort
    }
    $intradayKeys = @(foreach ($connection in @($IntradayConnections)) { Get-Key $connection })
    $batchKeys = @(foreach ($connection in @($BatchConnections)) { Get-Key $connection })
    $shared = @($intradayKeys | Where-Object { $_ -in $batchKeys } | Select-Object -Unique)
    $reason = 'distinct_tcp_connections'
    $distinct = $true
    if ($IntradayProcessId -ne 0 -and $IntradayProcessId -eq $BatchProcessId) {
        $distinct = $false; $reason = 'same_ssh_process'
    } elseif ($intradayKeys.Count -eq 0 -or $batchKeys.Count -eq 0) {
        $distinct = $false; $reason = 'no_connection_observed'
    } elseif ($shared.Count -gt 0) {
        $distinct = $false; $reason = 'shared_tcp_connection'
    }
    return [pscustomobject][ordered]@{
        distinct = $distinct
        reason = $reason
        shared_endpoints = $shared
        intraday_endpoints = $intradayKeys
        batch_endpoints = $batchKeys
    }
}

function Get-SharedTunnelStateFreshnessVerdict {
    # Pure judge for the third leg of the batch health claim: "the supervised
    # runtime state was written by THIS install".
    #
    # It is keyed off `started_at` because that is the field
    # scripts\windows\supervise-runtime-process.ps1 actually writes into
    # <service>.current.json (line 55, the `process_started` Set-RuntimeState
    # call). The first version of this gate read `requested_at`, which only ever
    # exists on the object Start-RuntimeSupervisor RETURNS - the lock-owning
    # supervisor is the only writer of current state and it never carries that
    # key - so the gate could never pass and every batch install failed.
    #
    # Every read goes through PSObject.Properties, including the one that builds
    # the failure message: this function runs under Set-StrictMode -Version
    # Latest in the installer, where `$State.started_at` on a state file that
    # does not carry the key throws PropertyNotFoundException. That throw would
    # escape Stop-TunnelInstallOnFailure, so the failure path would leave the
    # batch task enabled and retrying every two minutes - exactly the unbounded
    # failure the helper exists to prevent.
    #
    # A state whose timestamp is unreadable is never fresh. An unparsable or
    # absent stamp is the same evidence as an old one: no proof that this
    # install produced the state.
    #
    # A timestamp alone is still not enough, which is why -PreviousRunId exists.
    # A clock that moved, or a state file whose started_at happens to sit inside
    # the tolerance window, can pass on time alone. The run_id the installer
    # already holds (Request-RuntimeStop returns the PRE-install state) settles
    # it with no clock at all: this install's supervisor minted a new run_id, so
    # a state still carrying the previous one was not written by this install,
    # whatever its stamp says.
    #
    # THERE IS NO WEAK ACCEPT ANY MORE, and why it was deleted rather than
    # repaired is worth keeping. Earlier revisions accepted - as healthy, under
    # the health label 'remote_listener_open_owned_by_live_supervisor' - a state
    # that still carried the previous run_id whenever a live process still owned
    # that run. The case is real: supervise-runtime-process.ps1 exits 0 with
    # `duplicate_start_skipped` and writes NO state when it cannot take
    # <service>.lock, so a previous run_id can persist while a tunnel is up. The
    # branch was nevertheless unreachable from the installer, and not by
    # accident. The installer calls Request-RuntimeStop before it registers the
    # task, and Request-RuntimeStop either stamps this very file 'stop_requested'
    # or returns a state that is already terminal
    # (runtime-observability.psm1:197-235). Every state the installer can then
    # read for the PREVIOUS run therefore carries a stopping status, and only one
    # run_id ever holds a non-stopping status at a time - this install's own,
    # which is `fresh`. A branch that cannot fire is not a safety net; it is a
    # promise in a comment. Both docs made that promise too, and both were wrong.
    #
    # So the status is judged FIRST, before the run-id and clock branches, and it
    # disqualifies every claim rather than one branch of them. That also closes
    # the case the old ordering missed entirely: this install's OWN run dying
    # between the listener probe and the state read. supervise-runtime-process.ps1
    # writes status='unexpected_exit' (or 'supervisor_failed', 'stopped') with
    # THIS install's run_id and a started_at that post-dates the install - which
    # the run-id and clock branches both read as proof of a healthy install. It
    # is reported as 'run_ended_before_health_was_proved' instead.
    #
    # Refusing is not the same as condemning the install. 'previous_run_stopping'
    # says the handover this install started has not finished yet, and the
    # two-minute supervising trigger finishes it without anyone's help, so that
    # verdict alone carries `handover_in_progress` and the installer refuses
    # WITHOUT disabling the batch task. Every other refusal is a broken install.
    [CmdletBinding()]
    param(
        [AllowNull()][psobject]$State,
        [Parameter(Mandatory)][DateTimeOffset]$InstallStartedAt,
        # The run_id read BEFORE this install started, or '' when there was no
        # previous state at all (then any run_id is a new one).
        [AllowNull()][string]$PreviousRunId,
        # Statuses that say the run this state names is stopping or already over.
        # A parameter only so a caller can widen it; the default is every status
        # supervise-runtime-process.ps1 and Request-RuntimeStop write for such a
        # run.
        [string[]]$StoppingStatus = @('stop_requested', 'stopped', 'unexpected_exit',
            'supervisor_failed', 'start_failed'),
        [string]$Field = 'started_at',
        # Clock granularity only. The installer stamps $InstallStartedAt before
        # Register-ScheduledTask, so a legitimate run's started_at post-dates it.
        [double]$ToleranceSeconds = 1
    )
    $property = if ($null -ne $State) { $State.PSObject.Properties[$Field] } else { $null }
    $rawText = if ($null -ne $property -and $null -ne $property.Value) { [string]$property.Value } else { '' }
    $hasValue = -not [string]::IsNullOrWhiteSpace($rawText)
    $parsed = [DateTimeOffset]::MinValue
    $parsedOk = $hasValue -and [DateTimeOffset]::TryParse($rawText, [ref]$parsed)
    $floor = $InstallStartedAt.AddSeconds(-$ToleranceSeconds)
    $runIdProperty = if ($null -ne $State) { $State.PSObject.Properties['run_id'] } else { $null }
    $runId = if ($null -ne $runIdProperty -and $null -ne $runIdProperty.Value) { [string]$runIdProperty.Value } else { '' }
    $previous = if ($null -ne $PreviousRunId) { [string]$PreviousRunId } else { '' }
    # No previous state means nothing to be confused with, so any run_id is new.
    $runIdChanged = [string]::IsNullOrWhiteSpace($previous) -or ($runId -ne $previous)
    # The state's own status, read the same defensive way as every other field.
    $statusProperty = if ($null -ne $State) { $State.PSObject.Properties['status'] } else { $null }
    $status = if ($null -ne $statusProperty -and $null -ne $statusProperty.Value) { [string]$statusProperty.Value } else { '' }
    $stopping = $status -in @($StoppingStatus)
    $fresh = $false
    if ($null -eq $State) { $reason = 'no_runtime_state' }
    elseif ($stopping) {
        # Judged before the run-id and clock branches on purpose: a run that is
        # stopping or already over cannot be the proof of a serving tunnel, and
        # that is true of this install's own run_id as much as of the previous
        # one. Which of the two it is decides whether the install is broken
        # (its own run died) or merely unfinished (the handover it started).
        $reason = if ($runIdChanged) { 'run_ended_before_health_was_proved' } else { 'previous_run_stopping' }
    }
    elseif (-not $hasValue) { $reason = 'field_missing' }
    elseif (-not $parsedOk) { $reason = 'field_unparsable' }
    elseif (-not $runIdChanged) {
        # The supervisor of this install has not replaced the state yet. The
        # installer polls, so this verdict may still be re-taken.
        $reason = 'run_id_unchanged'
    }
    elseif ($parsed -lt $floor) { $reason = 'state_predates_install' }
    else { $fresh = $true; $reason = 'state_belongs_to_install' }
    # Only the unfinished handover self-heals; every other refusal is a broken
    # install and the caller is expected to treat it as one.
    $handover = $reason -eq 'previous_run_stopping'
    $observed = if ($hasValue) { $rawText } else { '<absent>' }
    $statusObserved = if ([string]::IsNullOrWhiteSpace($status)) { '<absent>' } else { $status }
    $message = if ($fresh) {
        ("Batch tunnel runtime state accepted ({0} '{1}', run_id '{2}', status '{3}' [{4}])") -f `
            $Field, $observed, $runId, $statusObserved, $reason
    } elseif ($handover) {
        # Not "stale": this state is the run the install itself just stopped, and
        # saying so is the difference between an operator re-running the install
        # and an operator hunting a tunnel that was never up.
        ("Batch tunnel health found only the previous run's state ({0} '{1}', run_id '{2}', " +
            "status '{3}' [{4}]): that run is stopping or already over, so a live pid does not " +
            "make it a serving tunnel. This install's supervisor never wrote a state of its own " +
            "before the deadline at '{5}'. The handover is unfinished, not broken, so the task " +
            "is left enabled and its two-minute trigger will finish it.") -f `
            $Field, $observed, $runId, $statusObserved, $reason, $InstallStartedAt.ToString('o')
    } elseif ($reason -eq 'run_ended_before_health_was_proved') {
        # This install's OWN run, already over by the time the state was read.
        # The listener probe passed moments earlier, so "the tunnel was up" and
        # "the tunnel is up" have to be told apart here or nowhere.
        ("Batch tunnel health found this install's own run already over ({0} '{1}', run_id " +
            "'{2}' (new since '{3}'), status '{4}' [{5}]): the supervisor started and exited " +
            "before its health could be proved, so nothing is serving the batch port now.") -f `
            $Field, $observed, $runId, $previous, $statusObserved, $reason
    } else {
        ("Batch tunnel health used a stale runtime state ({0} '{1}', run_id '{2}' vs previous " +
            "'{3}', status '{4}' [{5}] does not post-date this install at '{6}')") -f `
            $Field, $observed, $runId, $previous, $statusObserved, $reason,
            $InstallStartedAt.ToString('o')
    }
    return [pscustomobject][ordered]@{
        fresh = $fresh
        reason = $reason
        handover_in_progress = $handover
        field = $Field
        observed = $observed
        run_id = $runId
        previous_run_id = $previous
        run_id_changed = $runIdChanged
        status = $statusObserved
        install_started_at = $InstallStartedAt.ToString('o')
        message = $message
    }
}

function Get-SharedTunnelSupervisorLiveness {
    # Pure judge for "is the run named by this state still supervised?".
    #
    # This is a RECEIPT, not a gate. It used to be the evidence behind the weak
    # accept in Get-SharedTunnelStateFreshnessVerdict; that accept is gone (see
    # the note there), so nothing is admitted on the strength of a live pid any
    # more. What it still answers is the first question an operator asks about a
    # batch install that did not pass - was anything supervising the run the
    # state names? - which is why the installer writes its verdict into the
    # runtime state, the `healthy` event and the failure message.
    #
    # The caller passes the process it found for $State.supervisor_pid (or
    # $null). A bare "the pid exists" is not enough: pids are reused, and after a
    # reboot the pid in a leftover state file very likely belongs to something
    # else entirely. So the process's StartTime must sit in a window around the
    # state's own started_at - a supervisor stamps started_at within moments of
    # its own process start, while a recycled pid belongs to a process that
    # started much later (or earlier).
    [CmdletBinding()]
    param(
        [AllowNull()][psobject]$State,
        # Anything exposing Id and StartTime; Get-Process output in production.
        [AllowNull()][psobject]$Process,
        [double]$ToleranceSeconds = 120
    )
    $pidProperty = if ($null -ne $State) { $State.PSObject.Properties['supervisor_pid'] } else { $null }
    $statePid = if ($null -ne $pidProperty -and $null -ne $pidProperty.Value) { [string]$pidProperty.Value } else { '' }
    $startedProperty = if ($null -ne $State) { $State.PSObject.Properties['started_at'] } else { $null }
    $startedText = if ($null -ne $startedProperty -and $null -ne $startedProperty.Value) { [string]$startedProperty.Value } else { '' }
    $started = [DateTimeOffset]::MinValue
    $startedOk = (-not [string]::IsNullOrWhiteSpace($startedText)) -and [DateTimeOffset]::TryParse($startedText, [ref]$started)
    $alive = $false
    if ([string]::IsNullOrWhiteSpace($statePid)) { $reason = 'no_supervisor_pid' }
    elseif ($null -eq $Process) { $reason = 'supervisor_pid_not_running' }
    elseif (-not $Process.PSObject.Properties['StartTime'] -or $null -eq $Process.StartTime) { $reason = 'process_start_time_unavailable' }
    elseif (-not $startedOk) { $reason = 'state_started_at_unreadable' }
    else {
        $processStart = [DateTimeOffset]$Process.StartTime
        $drift = [Math]::Abs(($processStart - $started).TotalSeconds)
        if ($drift -gt $ToleranceSeconds) { $reason = 'supervisor_pid_reused' }
        else { $alive = $true; $reason = 'supervisor_process_owns_this_run' }
    }
    return [pscustomobject][ordered]@{
        alive = $alive
        reason = $reason
        supervisor_pid = $statePid
        state_started_at = $startedText
    }
}

Export-ModuleMember -Function @(
    'Get-SharedTunnelProfile',
    'Get-SharedTunnelSshArgument',
    'Get-SharedTunnelProcessPattern',
    'Test-SharedTunnelCommandLine',
    'Get-SharedTunnelConnectionVerdict',
    'Get-SharedTunnelStateFreshnessVerdict',
    'Get-SharedTunnelSupervisorLiveness'
)
