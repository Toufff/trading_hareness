[CmdletBinding()]
param()

# Pure contract test for the owner->peer tunnel profiles. It touches no
# scheduled task, no ssh process and no remote host: the production recovery
# acceptance stays in test-shared-tunnel-recovery.ps1, which deliberately
# requires -AllowFaultInjection because it kills the live tunnel.
#
# The point of this file is the intraday guarantee. Adding the batch profile
# reworked the argument construction that the live intraday tunnel depends on,
# so the intraday ssh vector is pinned here literally - byte for byte, in
# order - rather than derived from the same code it is supposed to check.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

$windowsScripts = Split-Path -Parent $PSScriptRoot
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..')).TrimEnd('\')
$sharedPeer = Join-Path $repository 'scripts\shared-peer'
Import-Module (Join-Path $sharedPeer 'shared-tunnel-profiles.psm1') -Force
Import-Module (Join-Path $windowsScripts 'runtime-observability.psm1') -Force

# --- profile identity ------------------------------------------------------
$intraday = Get-SharedTunnelProfile -Name 'intraday'
Assert-True ($intraday.Name -eq 'intraday') 'the default profile identifies itself as intraday'
Assert-True ($intraday.Service -eq 'shared-peer-tunnels') 'the intraday runtime service name must not change'
Assert-True ($intraday.TaskName -eq 'trading-hareness-shared-peer-tunnels') 'the intraday scheduled task name must not change'
Assert-True ((@($intraday.RemotePorts) -join ',') -eq '15432,15681') 'intraday reclaims exactly the database and API ports'
Assert-True (@($intraday.SshOptions).Count -eq 0) 'intraday must add no extra ssh options, compression included'
Assert-True ($intraday.HealthCheck -eq 'remote_api_http') 'intraday health is proven by HTTP through the tunnel'

$batch = Get-SharedTunnelProfile -Name 'batch'
Assert-True ($batch.Service -eq 'shared-peer-batch-tunnel') 'the batch runtime service has its own name, state and lock file'
Assert-True ($batch.TaskName -eq 'trading-hareness-shared-peer-batch-tunnel') 'the batch scheduled task has its own name'
Assert-True ((@($batch.RemotePorts) -join ',') -eq '15433') 'batch reclaims only 15433'
Assert-True ($batch.HealthCheck -eq 'remote_listener') 'batch carries no HTTP endpoint, so its health claim is the listener'

# The two profiles must not overlap on any reserved port: a reclaim is a
# `fuser -k` on the shared host, and one profile reaping the other's listener
# would be an outage disguised as recovery.
$shared = @($intraday.RemotePorts | Where-Object { $_ -in @($batch.RemotePorts) })
Assert-True ($shared.Count -eq 0) 'the intraday and batch reclaim sets must be disjoint'

$rejected = $false
try { [void](Get-SharedTunnelProfile -Name 'bulk') } catch { $rejected = $true }
Assert-True $rejected 'an unknown profile name must be rejected, not silently treated as intraday'

# --- intraday ssh vector, pinned literally ---------------------------------
# DELIBERATE CHANGE to the pinned intraday vector, made once and recorded here:
# '-o ControlMaster=no -o ControlPath=none' were added to BOTH profiles. Without
# them "batch gets its own TCP connection" is a property of ~/.ssh/config, not of
# this code: one 'ControlMaster auto' + 'ControlPath' pair there and the batch
# client opens a channel on the intraday client's socket, silently undoing the
# whole feature. The options are a no-op against the current host (ssh -G
# lightServer1 reports controlmaster false); nothing else about the intraday
# vector may change without the same kind of note.
$expectedIntraday = @(
    '-NT',
    '-o', 'BatchMode=yes',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'ControlMaster=no',
    '-o', 'ControlPath=none',
    '-R', '127.0.0.1:15432:127.0.0.1:55432',
    '-R', '127.0.0.1:15681:127.0.0.1:5681',
    'lightServer1'
)
$actualIntraday = @(Get-SharedTunnelSshArgument -TunnelProfile $intraday -Destination 'lightServer1')
Assert-True ($actualIntraday.Count -eq $expectedIntraday.Count) 'the intraday ssh vector must keep its exact length'
for ($index = 0; $index -lt $expectedIntraday.Count; $index++) {
    Assert-True ($actualIntraday[$index] -eq $expectedIntraday[$index]) `
        "the intraday ssh vector must be unchanged at position $index (expected '$($expectedIntraday[$index])', got '$($actualIntraday[$index])')"
}
$withConnection = @(Get-SharedTunnelSshArgument -TunnelProfile $intraday `
    -ConnectionArguments @('-i', 'key', '-p', '3535') -Destination 'stockowner@host')
Assert-True (($withConnection[0..3] -join ' ') -eq '-i key -p 3535') 'restricted-key connection arguments stay in front'
Assert-True ($withConnection[-1] -eq 'stockowner@host') 'the destination stays last'

# --- batch ssh vector ------------------------------------------------------
$expectedBatch = @(
    '-NT',
    '-o', 'BatchMode=yes',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'ControlMaster=no',
    '-o', 'ControlPath=none',
    '-o', 'Compression=yes',
    '-R', '127.0.0.1:15433:127.0.0.1:55432',
    'lightServer1'
)
$actualBatch = @(Get-SharedTunnelSshArgument -TunnelProfile $batch -Destination 'lightServer1')
Assert-True ((($actualBatch) -join ' ') -eq (($expectedBatch) -join ' ')) 'the batch ssh vector must be one compressed database forward with the same keepalive contract'
Assert-True (@($actualBatch | Where-Object { $_ -eq '-R' }).Count -eq 1) 'batch must carry exactly one reverse forward'
Assert-True ($actualBatch -contains 'Compression=yes') 'batch must enable compression'
Assert-True (-not ($actualIntraday -contains 'Compression=yes')) 'compression must appear only in the batch profile'
foreach ($required in 'BatchMode=yes', 'ExitOnForwardFailure=yes', 'ServerAliveInterval=30', 'ServerAliveCountMax=3') {
    Assert-True ($actualBatch -contains $required) "batch must keep the shared ssh contract '$required'"
}

# --- connection separation, pinned on the command line ---------------------
# Neither profile may inherit multiplexing from ssh_config: a shared
# ControlMaster socket would put bulk traffic back on the intraday connection's
# congestion window while every other signal in this file still looked correct.
foreach ($required in 'ControlMaster=no', 'ControlPath=none') {
    Assert-True ($actualIntraday -contains $required) "intraday must pin '$required' so ssh_config cannot multiplex it"
    Assert-True ($actualBatch -contains $required) "batch must pin '$required' so it cannot be collapsed onto the intraday connection"
}

# --- compression is declared, not inferred ---------------------------------
# The runtime metadata used to derive this from 'SshOptions.Count -gt 0', which
# is only true while compression is the only option either profile carries.
Assert-True ($intraday.PSObject.Properties['Compression'] -and $intraday.Compression -eq $false) 'intraday must declare Compression = $false explicitly'
Assert-True ($batch.PSObject.Properties['Compression'] -and $batch.Compression -eq $true) 'batch must declare Compression = $true explicitly'

# --- the distinct-connection judge -----------------------------------------
function New-TestConnection([string]$LocalPort, [string]$RemotePort) {
    return [pscustomobject]@{ LocalAddress = '192.168.1.10'; LocalPort = $LocalPort; RemoteAddress = '203.0.113.7'; RemotePort = $RemotePort }
}
$distinct = Get-SharedTunnelConnectionVerdict -IntradayConnections @((New-TestConnection '51001' '3535')) `
    -BatchConnections @((New-TestConnection '51002' '3535')) -IntradayProcessId 11 -BatchProcessId 22
Assert-True $distinct.distinct 'two ssh processes on two sockets are two connections'
Assert-True ($distinct.reason -eq 'distinct_tcp_connections') 'the distinct verdict names itself'

$multiplexed = Get-SharedTunnelConnectionVerdict -IntradayConnections @((New-TestConnection '51001' '3535')) `
    -BatchConnections @((New-TestConnection '51001' '3535')) -IntradayProcessId 11 -BatchProcessId 22
Assert-True (-not $multiplexed.distinct) 'a shared four-tuple is one connection wearing two hats'
Assert-True ($multiplexed.reason -eq 'shared_tcp_connection') 'a shared socket is reported as such'
Assert-True (@($multiplexed.shared_endpoints).Count -eq 1) 'the shared endpoint is named so an operator can find it'

$samePid = Get-SharedTunnelConnectionVerdict -IntradayConnections @((New-TestConnection '51001' '3535')) `
    -BatchConnections @((New-TestConnection '51002' '3535')) -IntradayProcessId 11 -BatchProcessId 11
Assert-True (-not $samePid.distinct) 'one ssh process cannot own both profiles'
Assert-True ($samePid.reason -eq 'same_ssh_process') 'a shared owning process is reported as such'

$unobserved = Get-SharedTunnelConnectionVerdict -IntradayConnections @((New-TestConnection '51001' '3535')) `
    -BatchConnections @() -IntradayProcessId 11 -BatchProcessId 0
Assert-True (-not $unobserved.distinct) 'an unobserved pair must never be reported as proven distinct'
Assert-True ($unobserved.reason -eq 'no_connection_observed') 'absence of evidence is reported as absence of evidence'

# --- process identification ------------------------------------------------
$intradayCommandLine = 'C:\Windows\System32\OpenSSH\ssh.exe -NT -o BatchMode=yes -R 127.0.0.1:15432:127.0.0.1:55432 -R 127.0.0.1:15681:127.0.0.1:5681 lightServer1'
$batchCommandLine = 'C:\Windows\System32\OpenSSH\ssh.exe -NT -o BatchMode=yes -o Compression=yes -R 127.0.0.1:15433:127.0.0.1:55432 lightServer1'
Assert-True (Test-SharedTunnelCommandLine -TunnelProfile $intraday -CommandLine $intradayCommandLine) 'the intraday profile must recognise its own ssh process'
Assert-True (Test-SharedTunnelCommandLine -TunnelProfile $batch -CommandLine $batchCommandLine) 'the batch profile must recognise its own ssh process'
Assert-True (-not (Test-SharedTunnelCommandLine -TunnelProfile $intraday -CommandLine $batchCommandLine)) 'the intraday profile must not claim the batch ssh process'
Assert-True (-not (Test-SharedTunnelCommandLine -TunnelProfile $batch -CommandLine $intradayCommandLine)) 'the batch profile must not claim the intraday ssh process'
Assert-True (-not (Test-SharedTunnelCommandLine -TunnelProfile $intraday -CommandLine '')) 'a process without a readable command line is never a match'
# A half-started intraday client holding only the database forward is not the
# owner of the full profile, or a reinstall would leave the API forward down.
Assert-True (-not (Test-SharedTunnelCommandLine -TunnelProfile $intraday `
    -CommandLine 'ssh.exe -NT -R 127.0.0.1:15432:127.0.0.1:55432 lightServer1')) 'every forward of a profile must be present to claim ownership'

# --- reserved port guard ---------------------------------------------------
Assert-True ((Assert-ReservedRemoteTunnelPort -Port 15433 -AllowedPorts @($batch.RemotePorts)) -eq 15433) 'the batch profile may reclaim 15433'
foreach ($case in @(@{ Port = 15433; Allowed = @($intraday.RemotePorts) }, @{ Port = 15432; Allowed = @($batch.RemotePorts) },
                    @{ Port = 22; Allowed = @($batch.RemotePorts) }, @{ Port = 55432; Allowed = @($batch.RemotePorts) })) {
    $blocked = $false
    try { [void](Assert-ReservedRemoteTunnelPort -Port $case.Port -AllowedPorts $case.Allowed) } catch { $blocked = $true }
    Assert-True $blocked "port $($case.Port) must not be reclaimable outside its own profile"
}
# 15433 must NOT leak into the module-wide default allowed set, which other
# callers (the dashboard tunnel) rely on.
$defaultBlocked = $false
try { [void](Assert-ReservedRemoteTunnelPort -Port 15433) } catch { $defaultBlocked = $true }
Assert-True $defaultBlocked 'the default reserved set must stay the dashboard/API/peer trio'

# --- script wiring ---------------------------------------------------------
$starter = Get-Content (Join-Path $sharedPeer 'start-shared-tunnels.ps1') -Raw
$installer = Get-Content (Join-Path $sharedPeer 'install-shared-tunnel-task.ps1') -Raw
$bothInstaller = Get-Content (Join-Path $sharedPeer 'install-shared-tunnel-tasks.ps1') -Raw
foreach ($pair in @(@{ Name = 'start-shared-tunnels.ps1'; Text = $starter }, @{ Name = 'install-shared-tunnel-task.ps1'; Text = $installer })) {
    Assert-True ($pair.Text -match "(?m)^\s*\[ValidateSet\('intraday', 'batch'\)\]\[string\]\`$Profile = 'intraday'") "$($pair.Name) must take -Profile with intraday as the default"
    Assert-True ($pair.Text -match 'Get-SharedTunnelProfile') "$($pair.Name) must resolve names and ports through the shared profile module"
    Assert-True ($pair.Text -notmatch "Service '?shared-peer-tunnels'?") "$($pair.Name) must not hardcode the intraday service name"
}
Assert-True ($starter -match 'Get-SharedTunnelSshArgument') 'the starter must build its ssh vector through the shared helper'
Assert-True ($starter -notmatch 'Compression=yes') 'the compression flag belongs to the profile definition, not to the starter'
Assert-True ($starter -match 'compression = \[bool\]\$tunnelProfile\.Compression') 'the starter must record the declared Compression property, not infer it from the option count'
Assert-True ($starter -notmatch 'SshOptions\.Count') 'nothing may infer compression from how many ssh options a profile happens to carry'
Assert-True ($installer -match '\[switch\]\$WhatIf') 'the installer must offer a dry run'
# The dry run must return before anything mutates the host.
$whatIfIndex = $installer.IndexOf('if ($WhatIf) {')
$stopIndex = $installer.IndexOf('Stop-ScheduledTask')
$registerIndex = $installer.IndexOf('Register-ScheduledTask')
Assert-True ($whatIfIndex -gt 0 -and $whatIfIndex -lt $stopIndex -and $whatIfIndex -lt $registerIndex) 'the dry run must short-circuit before any scheduled-task or process change'
Assert-True ($bothInstaller -match '-Profile intraday' -and $bothInstaller -match '-Profile batch') 'the combined installer must install both profiles'
Assert-True ($bothInstaller.IndexOf('-Profile intraday') -lt $bothInstaller.IndexOf('-Profile batch')) 'intraday must be installed first'
Assert-True ($bothInstaller -match 'RequireBatch') 'a batch failure must be non-fatal unless the caller demands it'

# A batch install that cannot prove its health must not be left registered with
# a two-minute trigger retrying into the same failure for as long as the host is
# up. Intraday is the opposite case and must keep retrying, so the disable is
# scoped to the batch profile.
Assert-True ($installer -match 'function Stop-TunnelInstallOnFailure') 'the installer must route install failures through one helper'
Assert-True ($installer -match "(?s)function Stop-TunnelInstallOnFailure.*?\`$tunnelProfile\.Name -eq 'batch'.*?Disable-ScheduledTask") 'a failed batch install must disable its own task before rethrowing'
Assert-True ($installer -notmatch '(?m)^\s*throw "(Shared peer tunnel task|Batch tunnel task)') 'no install failure path may throw without going through the helper'
Assert-True ($installer -match 'remote_listener_open_owned_by_local_client') 'the batch health label must state the stronger claim it now proves'
Assert-True ($installer -match '(?s)Get-SharedTunnelStateFreshnessVerdict -State \$state `?\s*-InstallStartedAt \$installStartedAt -PreviousRunId \$previousRunId') 'the batch health check must judge state freshness through the pure function the tests below execute, against the pre-install run_id'
Assert-True ($installer -match '\$installStartedAt') 'the installer must timestamp the install so stale state cannot pass for health'
# The pre-install state was read and thrown away; its run_id is the one piece of
# evidence that needs no clock at all.
Assert-True ($installer -match '\$previousState = Request-RuntimeStop') 'the installer must keep the pre-install runtime state, not discard it'
Assert-True ($installer -match "(?s)\`$previousRunId = if \(\`$previousState") 'the pre-install run_id must be derived from that state'
# Judged once, a state file written a moment after the gate looked disabled a
# batch task that was coming up. It must be polled.
Assert-True ($installer -match '\$freshnessDeadline = \[DateTime\]::UtcNow\.AddSeconds\(30\)') 'the freshness verdict must be given a deadline to poll against'
Assert-True ($installer -match '(?s)do \{[^}]*Get-RuntimeState -PlatformRoot \$PlatformRoot -Service \$service[\s\S]*?\} while \(\[DateTime\]::UtcNow -lt \$freshnessDeadline\)') 'the freshness verdict must be re-taken from a re-read state until the deadline'
# accept, not fresh: a duplicate_start_skipped supervisor leaves the previous
# run''s state in place while the tunnel is up, and disabling it would be the
# worse failure.
Assert-True ($installer -match 'if \(\$null -ne \$freshness -and -not \$freshness\.accept\)') 'the installer must gate on accept, so a live older supervisor is not treated as a failed install'
# ...but the POLL must break on fresh. Breaking on accept let the weaker claim
# win a race against this install's own state - and the state it accepted is the
# one Request-RuntimeStop had just stamped 'stop_requested'.
Assert-True ($installer -match '(?m)^\s*if \(\$freshness\.fresh\) \{ break \}') 'the freshness poll must keep polling until this install''s OWN state arrives'
Assert-True ($installer -notmatch 'if \(\$freshness\.accept\) \{ break \}') 'the poll must not stop on the weaker accept'
Assert-True ($installer -match '\$acceptedFallback') 'an accept-but-not-fresh verdict must be kept as a fallback rather than discarded'
Assert-True ($installer -match '(?s)if \(-not \$freshness\.fresh -and \$null -ne \$acceptedFallback\) \{[\s\S]*?\$freshness = \$acceptedFallback\.Freshness') 'the fallback must be restored only after the deadline expires'
# The liveness verdict is half the claim and used to be computed and thrown away.
Assert-True ($installer -match '\$healthyState\.supervisor_liveness = \$livenessReason') 'the supervisor liveness verdict must reach the runtime state the acceptance step reads'
Assert-True ($installer -match '\$eventData\.supervisor_liveness = \$livenessReason') 'and the lifecycle event, which is where an operator looks for one install'
Assert-True ($installer -match '\$healthyState\.supervisor_pid_checked') 'the pid the liveness verdict was taken against must be recorded with it'
Assert-True ($installer -match '(?s)\$livenessDetail = if \(\$null -ne \$liveness\)[\s\S]*?Stop-TunnelInstallOnFailure -Message \(\$freshness\.message \+ \$livenessDetail\)') 'a refused install writes no state, so its message must carry the liveness reason'
Assert-True ($installer -notmatch 'if \(-not \$freshness\.fresh\) \{\s*\r?\n\s*Stop-TunnelInstallOnFailure') 'nothing may disable the task purely because the state is not this install''s own'
Assert-True ($installer -match 'remote_listener_open_owned_by_live_supervisor') 'the weaker accepted claim must get its own health label'
# The first version of this gate asserted on requested_at, a field only the
# object Start-RuntimeSupervisor RETURNS ever carries; the supervisor that
# writes <service>.current.json writes started_at. Nothing may go back to it.
$installerCode = (($installer -split "`r?`n") | Where-Object { $_ -notmatch '^\s*#' }) -join "`n"
Assert-True ($installerCode -notmatch 'requested_at') 'no code path may assert on requested_at, which never reaches the runtime state file (the comment explaining that may name it)'
$supervisor = Get-Content (Join-Path $windowsScripts 'supervise-runtime-process.ps1') -Raw
Assert-True ($supervisor -match "status = 'process_started'[\s\S]{0,400}started_at = ") 'the supervisor must still write started_at into the state the gate reads'

# --- the batch health gate, executed against a real state file --------------
# Everything above about the gate is a regex over source text. These cases write
# an actual <service>.current.json, read it back through Get-RuntimeState (the
# same call the installer makes) and drive the judge, so a gate that can never
# pass cannot report green here again.
$stateRoot = Join-Path ([IO.Path]::GetTempPath()) ("tunnel-gate-" + [Guid]::NewGuid().ToString('N'))
try {
    $batchService = $batch.Service
    $installStartedAt = [DateTimeOffset]::Now

    function Write-FakeRuntimeState([hashtable]$Payload) {
        $path = Join-Path $stateRoot 'logs\runtime'
        New-Item -ItemType Directory -Force -Path $path | Out-Null
        $file = Join-Path $path "$batchService.current.json"
        ($Payload | ConvertTo-Json -Depth 10) | Set-Content -LiteralPath $file -Encoding UTF8
        return $file
    }

    $previousRunId = '20260918T030000000-0badbeef'

    # 1. Fresh: the supervisor started this run after the task was registered,
    #    and minted a run_id the pre-install state did not carry.
    $fresh = Write-FakeRuntimeState @{
        schema_version = 1; service = $batchService; status = 'process_started'
        run_id = '20260919T040000000-abcdef12'; supervisor_pid = 4242; launcher_pid = 4243
        started_at = $installStartedAt.AddSeconds(3).ToString('o')
        updated_at = $installStartedAt.AddSeconds(3).ToString('o')
    }
    Assert-True (Test-Path -LiteralPath $fresh -PathType Leaf) 'the fake runtime state file must exist where Get-RuntimeState looks for it'
    $freshState = Get-RuntimeState -PlatformRoot $stateRoot -Service $batchService
    Assert-True ($null -ne $freshState -and [bool]$freshState.PSObject.Properties['run_id']) 'the installer run_id precondition must be satisfied by a real supervised state'
    $freshVerdict = Get-SharedTunnelStateFreshnessVerdict -State $freshState `
        -InstallStartedAt $installStartedAt -PreviousRunId $previousRunId
    Assert-True $freshVerdict.fresh 'a state whose started_at post-dates the install is this install''s state'
    Assert-True $freshVerdict.accept 'this install''s own state must be accepted'
    Assert-True ($freshVerdict.reason -eq 'state_belongs_to_install') 'the fresh verdict names itself'
    Assert-True ($freshVerdict.field -eq 'started_at') 'the gate must key off the field the supervisor actually writes'
    Assert-True ($freshVerdict.run_id_changed) 'a new run_id is what makes the state this install''s own'
    Assert-True ($freshVerdict.previous_run_id -eq $previousRunId) 'the verdict must record which run it was compared against'

    # 1b. The clock alone is not enough. Same post-dating started_at, but the
    #     run_id never moved: no supervisor of THIS install wrote it.
    $sameRunVerdict = Get-SharedTunnelStateFreshnessVerdict -State $freshState `
        -InstallStartedAt $installStartedAt -PreviousRunId ([string]$freshState.run_id)
    Assert-True (-not $sameRunVerdict.fresh) 'a state still carrying the pre-install run_id is not this install''s state'
    Assert-True (-not $sameRunVerdict.accept) 'and with no live supervisor to vouch for it, it must not be accepted'
    Assert-True ($sameRunVerdict.reason -eq 'run_id_unchanged') 'an unchanged run_id is reported as such'

    # 1c. duplicate_start_skipped: the new supervisor could not take the lock and
    #     wrote nothing, so the state still names the PREVIOUS run - which is
    #     alive and serving. Disabling that task would take down a working
    #     tunnel, so the verdict accepts without claiming freshness.
    $duplicateVerdict = Get-SharedTunnelStateFreshnessVerdict -State $freshState `
        -InstallStartedAt $installStartedAt -PreviousRunId ([string]$freshState.run_id) -SupervisorAlive $true
    Assert-True (-not $duplicateVerdict.fresh) 'a duplicate_start_skipped state is still not this install''s own state'
    Assert-True $duplicateVerdict.accept 'a live supervisor owning the run must not be reported as a failed install'
    Assert-True ($duplicateVerdict.reason -eq 'duplicate_supervisor_still_serving') 'the weaker accepted claim must name itself'

    # 1d. The liveness judge itself: pid present and the process start time sits
    #     beside the state's started_at, versus a recycled pid.
    $liveProcess = [pscustomobject]@{ Id = 4242; StartTime = $installStartedAt.AddSeconds(2).LocalDateTime }
    $liveness = Get-SharedTunnelSupervisorLiveness -State $freshState -Process $liveProcess
    Assert-True $liveness.alive 'a supervisor process that started beside the state''s started_at owns that run'
    Assert-True ($liveness.reason -eq 'supervisor_process_owns_this_run') 'the liveness verdict names itself'
    $reused = Get-SharedTunnelSupervisorLiveness -State $freshState `
        -Process ([pscustomobject]@{ Id = 4242; StartTime = $installStartedAt.AddHours(4).LocalDateTime })
    Assert-True (-not $reused.alive) 'a pid whose process started hours away from started_at is a reused pid, not the supervisor'
    Assert-True ($reused.reason -eq 'supervisor_pid_reused') 'pid reuse is reported as pid reuse'
    $gone = Get-SharedTunnelSupervisorLiveness -State $freshState -Process $null
    Assert-True ((-not $gone.alive) -and $gone.reason -eq 'supervisor_pid_not_running') 'a dead supervisor pid vouches for nothing'
    # And the accept path is genuinely gated on it: the same unchanged run_id
    # with a dead supervisor must NOT be accepted.
    $deadDuplicate = Get-SharedTunnelStateFreshnessVerdict -State $freshState `
        -InstallStartedAt $installStartedAt -PreviousRunId ([string]$freshState.run_id) -SupervisorAlive $gone.alive
    Assert-True (-not $deadDuplicate.accept) 'an unchanged run_id with no live supervisor must still fail the gate'

    # 1e. The race the accept-break lost. install-shared-tunnel-tasks.ps1 runs
    #     both profiles, and the supervising trigger fires every two minutes, so
    #     a batch run started seconds ago is still alive on its pid when this
    #     install begins - and Request-RuntimeStop has just stamped THAT state
    #     'stop_requested' (runtime-observability.psm1:231). A live pid plus the
    #     previous run_id is therefore also the shape of "the run this install is
    #     tearing down", and accepting it certifies a tunnel that is going away.
    $stoppingState = $null
    [void](Write-FakeRuntimeState @{
        schema_version = 1; service = $batchService; status = 'stop_requested'
        run_id = $previousRunId; supervisor_pid = 4242; launcher_pid = 4243
        started_at = $installStartedAt.AddSeconds(-40).ToString('o')
        stop_requested_at = $installStartedAt.ToString('o')
        stop_reason = 'task_reinstall'; stop_requested_by = 'install-shared-tunnel-task.ps1'
    })
    $stoppingState = Get-RuntimeState -PlatformRoot $stateRoot -Service $batchService
    $stoppingVerdict = Get-SharedTunnelStateFreshnessVerdict -State $stoppingState `
        -InstallStartedAt $installStartedAt -PreviousRunId $previousRunId -SupervisorAlive $true
    Assert-True (-not $stoppingVerdict.accept) 'the state this install just asked to stop must never pass for health, however alive its pid is'
    Assert-True ($stoppingVerdict.reason -eq 'previous_run_stopping') 'a stopping previous run names itself'
    Assert-True ($stoppingVerdict.status -eq 'stop_requested') 'the verdict must record the status it judged'
    Assert-True ($stoppingVerdict.message -match 'stopping or already over') 'the message must say why a live pid was not enough'
    foreach ($terminal in 'stopped', 'unexpected_exit', 'supervisor_failed', 'start_failed') {
        [void](Write-FakeRuntimeState @{
            schema_version = 1; service = $batchService; status = $terminal
            run_id = $previousRunId; supervisor_pid = 4242
            started_at = $installStartedAt.AddSeconds(-40).ToString('o')
        })
        $terminalVerdict = Get-SharedTunnelStateFreshnessVerdict `
            -State (Get-RuntimeState -PlatformRoot $stateRoot -Service $batchService) `
            -InstallStartedAt $installStartedAt -PreviousRunId $previousRunId -SupervisorAlive $true
        Assert-True (-not $terminalVerdict.accept) "a '$terminal' previous run must not be accepted as a serving tunnel"
        Assert-True ($terminalVerdict.reason -eq 'previous_run_stopping') "a '$terminal' previous run is reported as stopping or over"
    }
    # A previous run that is genuinely still serving keeps the weak accept: this
    # is duplicate_start_skipped, and disabling that task is the worse failure.
    [void](Write-FakeRuntimeState @{
        schema_version = 1; service = $batchService; status = 'process_started'
        run_id = $previousRunId; supervisor_pid = 4242; launcher_pid = 4243
        started_at = $installStartedAt.AddSeconds(-40).ToString('o')
    })
    $servingState = Get-RuntimeState -PlatformRoot $stateRoot -Service $batchService
    $servingVerdict = Get-SharedTunnelStateFreshnessVerdict -State $servingState `
        -InstallStartedAt $installStartedAt -PreviousRunId $previousRunId -SupervisorAlive $true
    Assert-True ($servingVerdict.accept -and -not $servingVerdict.fresh) 'a live supervisor on a running previous run still carries the weaker claim'
    Assert-True ($servingVerdict.reason -eq 'duplicate_supervisor_still_serving') 'and it is still named as the weaker claim'

    # 1f. The poll as the installer runs it, driven over a SEQUENCE of state
    #     files. The installer's loop is inline, so this models it exactly -
    #     break on fresh, keep an accept as a fallback, restore the fallback only
    #     when the deadline passed without a fresh verdict - and the regexes
    #     above pin the installer to this shape.
    function Invoke-FreshnessPoll([object[]]$States, [bool[]]$Alive) {
        $fallback = $null
        $verdict = $null
        for ($i = 0; $i -lt $States.Count; $i++) {
            $verdict = Get-SharedTunnelStateFreshnessVerdict -State $States[$i] `
                -InstallStartedAt $installStartedAt -PreviousRunId $previousRunId `
                -SupervisorAlive $Alive[$i]
            if ($verdict.fresh) { break }
            if ($verdict.accept) { $fallback = $verdict }
        }
        if (-not $verdict.fresh -and $null -ne $fallback) { $verdict = $fallback }
        return $verdict
    }
    # The race itself: iteration 1 reads the stopping previous run (live pid),
    # iteration 2 reads this install's own state. The old poll broke on
    # iteration 1 and certified the run it had just stopped.
    $raceVerdict = Invoke-FreshnessPoll -States @($stoppingState, $freshState) -Alive @($true, $true)
    Assert-True $raceVerdict.fresh 'this install''s own state must win the race against the run it stopped'
    Assert-True ($raceVerdict.reason -eq 'state_belongs_to_install') 'and the accepted verdict must be the fresh one'
    Assert-True ($raceVerdict.run_id -eq [string]$freshState.run_id) 'the verdict must name this install''s run'
    # Even a genuinely serving previous run must not stop the poll early: if this
    # install's supervisor writes a state one iteration later, that is the one.
    $lateVerdict = Invoke-FreshnessPoll -States @($servingState, $servingState, $freshState) -Alive @($true, $true, $true)
    Assert-True ($lateVerdict.fresh -and $lateVerdict.reason -eq 'state_belongs_to_install') 'a duplicate_start_skipped read must not stop the poll while this install''s state is still coming'
    # And when nothing fresher ever arrives, the fallback is what the install is
    # accepted on - including when the LAST read is a non-accepting one.
    $fallbackVerdict = Invoke-FreshnessPoll -States @($servingState, $stoppingState) -Alive @($true, $true)
    Assert-True ($fallbackVerdict.accept -and -not $fallbackVerdict.fresh) 'an earlier live-supervisor accept must survive a later non-accepting read'
    Assert-True ($fallbackVerdict.reason -eq 'duplicate_supervisor_still_serving') 'the fallback must be the weaker accept, not the refusal'
    # A poll that only ever sees the stopping run refuses, and that is the
    # bounded failure: the batch task is disabled rather than certified.
    $refusedVerdict = Invoke-FreshnessPoll -States @($stoppingState, $stoppingState) -Alive @($true, $true)
    Assert-True (-not $refusedVerdict.accept) 'a poll that only ever sees the stopped run must refuse'

    # 2. Stale: yesterday's run left a state file behind and the task never came up.
    [void](Write-FakeRuntimeState @{
        schema_version = 1; service = $batchService; status = 'healthy'
        run_id = '20260917T030000000-deadbeef'; supervisor_pid = 1111; launcher_pid = 1112
        started_at = $installStartedAt.AddDays(-1).ToString('o')
        updated_at = $installStartedAt.AddDays(-1).ToString('o')
    })
    $staleVerdict = Get-SharedTunnelStateFreshnessVerdict `
        -State (Get-RuntimeState -PlatformRoot $stateRoot -Service $batchService) `
        -InstallStartedAt $installStartedAt -PreviousRunId $previousRunId
    Assert-True (-not $staleVerdict.fresh) 'yesterday''s leftover state must never pass for this install''s health'
    Assert-True (-not $staleVerdict.accept) 'and it must not be accepted either'
    Assert-True ($staleVerdict.reason -eq 'state_predates_install') 'a stale state is reported as predating the install'
    Assert-True ($staleVerdict.message -match 'stale runtime state') 'the stale failure message must say what happened'

    # 3. The field is simply absent - the shape the old gate died on. Under
    #    Set-StrictMode -Version Latest a direct property read throws here, the
    #    throw escapes Stop-TunnelInstallOnFailure, and the batch task stays
    #    enabled retrying every two minutes. The verdict must be an ordinary
    #    failure instead, message included.
    [void](Write-FakeRuntimeState @{
        schema_version = 1; service = $batchService; status = 'process_started'
        run_id = '20260919T040000000-cafe0001'; supervisor_pid = 5150
    })
    $missingState = Get-RuntimeState -PlatformRoot $stateRoot -Service $batchService
    Assert-True (-not $missingState.PSObject.Properties['started_at']) 'this case must genuinely lack the field'
    $threw = $false
    try { $missingVerdict = Get-SharedTunnelStateFreshnessVerdict -State $missingState `
            -InstallStartedAt $installStartedAt -PreviousRunId $previousRunId }
    catch { $threw = $true }
    Assert-True (-not $threw) 'a state file without the field must not throw under StrictMode'
    Assert-True (-not $missingVerdict.fresh) 'an absent timestamp is not evidence that this install produced the state'
    Assert-True (-not $missingVerdict.accept) 'nor is it something to accept'
    Assert-True ($missingVerdict.reason -eq 'field_missing') 'a missing field is reported as missing'
    Assert-True ($missingVerdict.message -match '<absent>') 'the failure message must be buildable without the field'
    # The liveness judge reads the same state and must not throw either.
    $missingLiveness = Get-SharedTunnelSupervisorLiveness -State $missingState `
        -Process ([pscustomobject]@{ Id = 5150; StartTime = (Get-Date) })
    Assert-True ((-not $missingLiveness.alive) -and $missingLiveness.reason -eq 'state_started_at_unreadable') 'liveness without a started_at to anchor against proves nothing'

    # 4. No state file at all, and an unparsable stamp.
    $noStateVerdict = Get-SharedTunnelStateFreshnessVerdict -State $null `
        -InstallStartedAt $installStartedAt -PreviousRunId $previousRunId
    Assert-True ((-not $noStateVerdict.fresh) -and (-not $noStateVerdict.accept) -and $noStateVerdict.reason -eq 'no_runtime_state') 'a missing state file is reported as such'
    $noStateLiveness = Get-SharedTunnelSupervisorLiveness -State $null -Process $null
    Assert-True ((-not $noStateLiveness.alive) -and $noStateLiveness.reason -eq 'no_supervisor_pid') 'no state means no supervisor to vouch for it'
    [void](Write-FakeRuntimeState @{ schema_version = 1; service = $batchService; run_id = 'x'; started_at = 'not a timestamp' })
    $badVerdict = Get-SharedTunnelStateFreshnessVerdict `
        -State (Get-RuntimeState -PlatformRoot $stateRoot -Service $batchService) `
        -InstallStartedAt $installStartedAt -PreviousRunId $previousRunId
    Assert-True ((-not $badVerdict.fresh) -and (-not $badVerdict.accept) -and $badVerdict.reason -eq 'field_unparsable') 'an unreadable timestamp is not proof of freshness'

    # 5. First install of all: there was no previous run at all, so there is no
    #    run_id to be confused with and a post-dating stamp is the whole claim.
    $firstVerdict = Get-SharedTunnelStateFreshnessVerdict -State $freshState `
        -InstallStartedAt $installStartedAt -PreviousRunId ''
    Assert-True ($firstVerdict.fresh -and $firstVerdict.accept) 'a first install has no previous run_id and must still be able to pass'
    Assert-True ($firstVerdict.reason -eq 'state_belongs_to_install') 'a first install''s state belongs to it'
} finally {
    Remove-Item -LiteralPath $stateRoot -Recurse -Force -ErrorAction SilentlyContinue
}

# --- peer key restrictions, both directions --------------------------------
# permitlisten (owner -R) and permitopen (peer -L) fail in completely different
# ways, and 15433 must be in both default option strings or the batch path dies
# loudly on one side and silently on the other.
$ownerKeyScript = Get-Content (Join-Path $sharedPeer 'install-owner-tunnel-key.sh') -Raw
$peerKeyScript = Get-Content (Join-Path $sharedPeer 'provision-lightserver-rootless.sh') -Raw
Assert-True ($ownerKeyScript -match 'permitlisten=\\"127\.0\.0\.1:15433\\"') 'the owner tunnel key must be allowed to listen on 15433'
Assert-True ($peerKeyScript -match 'permitopen=\\"127\.0\.0\.1:15433\\"') 'the peer key must be allowed to open 15433'
foreach ($keptPort in '15432', '15681') {
    Assert-True ($peerKeyScript -match "permitopen=\\`"127\.0\.0\.1:$keptPort\\`"") "the peer key must keep permitopen for $keptPort"
}
$runtimeDoc = Get-Content (Join-Path $repository 'docs\SHARED_PEER_RUNTIME.md') -Raw
Assert-True ($runtimeDoc -match 'permitopen="127\.0\.0\.1:15433"') 'the runtime doc must show the four-port permitopen default'
Assert-True ($runtimeDoc -match 'permitlisten="127\.0\.0\.1:15433"') 'the runtime doc must show the four-port permitlisten default'
Assert-True ($runtimeDoc -notmatch 'all three `permitlisten` entries') 'the runtime doc must stop saying there are three permitlisten entries'

# --- peer side -------------------------------------------------------------
$entrypoint = Get-Content (Join-Path $repository 'deploy\shared-peer\ssh-tunnel-entrypoint.sh') -Raw
Assert-True ($entrypoint -match '\$\{PEER_BATCH_DB_PORT:-\}') 'the peer entrypoint must treat the batch forward as optional'
Assert-True ($entrypoint -match '\$\{PEER_BATCH_REMOTE_PORT:-15433\}') 'the peer entrypoint must default the batch remote port to 15433'
Assert-True ($entrypoint -match '(?s)5432:127\.0\.0\.1:\$\{REMOTE_DB_PORT\}.*5681:127\.0\.0\.1:\$\{REMOTE_API_PORT\}') 'the existing peer forwards must stay unchanged'
$composeText = Get-Content (Join-Path $repository 'deploy\shared-peer\compose.yaml') -Raw
Assert-True ($composeText -match 'PEER_BATCH_DB_PORT: \$\{PEER_BATCH_DB_PORT:-\}') 'compose must default the batch port to off'
Assert-True ($composeText -match 'ssh-tunnel-healthcheck') 'the db-tunnel healthcheck must not start depending on the optional batch port'

# --- peer deploy script ----------------------------------------------------
# The peer's deployed tree and this repository have drifted apart before, and
# they differ in exactly the place that matters: the peer's live entrypoint
# binds 0.0.0.0 literally while the repo copy binds
# ${PEER_LOCAL_BIND_ADDRESS:-127.0.0.1}. Writing one over the other on a peer
# whose compose does not set that variable is a total, undetectable peer outage,
# so the deploy script must recognise the peer's ACTUAL state first.
#
# These are STRUCTURAL assertions only - they prove the wiring is in the file,
# not that the transforms work. The behaviour is executed by
# quant-service/tests/test_peer_batch_tunnel_deploy.py against a committed
# byte-identical copy of the peer's live entrypoint; this suite must not report
# the peer deploy as proven on its own.
$deployScript = Get-Content (Join-Path $sharedPeer 'deploy-batch-tunnel-port.py') -Raw
$deployTest = Join-Path $repository 'quant-service\tests\test_peer_batch_tunnel_deploy.py'
$deployFixture = Join-Path $repository 'quant-service\tests\fixtures\peer-ssh-tunnel-entrypoint-nc-legacy-20260917.sh'
Assert-True (Test-Path -LiteralPath $deployTest -PathType Leaf) 'the peer deploy transforms must have an executing test, not only regexes over their source'
Assert-True (Test-Path -LiteralPath $deployFixture -PathType Leaf) 'that test must run against a committed copy of the peer entrypoint'
$fixtureHash = (Get-FileHash -LiteralPath $deployFixture -Algorithm SHA256).Hash.ToLowerInvariant()
Assert-True ($deployScript -match [regex]::Escape($fixtureHash)) 'the committed fixture must hash to the state KNOWN_PEER_STATES pins'
Assert-True ($deployScript -match 'KNOWN_PEER_STATES') 'the peer deploy script must enumerate the states it supports'
Assert-True ($deployScript -match "(?s)def inspect_peer_state.*?entrypoint_sha256.*?healthcheck_test.*?environment_keys.*?service\.get\('image'\)") 'it must read the hash, healthcheck, environment keys and image of the deployed service'
Assert-True ($deployScript -match "(?s)if not matches:.*?refusing to touch the peer") 'an unrecognised peer state must be a refusal, not a guess'
Assert-True ($deployScript -match "'strategy': 'patch'") 'the observed peer state must be patched, never overwritten with the repo copy'
Assert-True ($deployScript -match "(?s)def patch_entrypoint.*?text\.replace\(anchor, block \+ anchor, 1\)") 'the script must insert the batch block into the peer entrypoint rather than replace the file'
Assert-True ($deployScript -match "(?s)if known\['strategy'\] == 'patch':\s*\r?\n\s*\(ROOT / 'ssh-tunnel-entrypoint\.sh'\)\.write_text\(patch_entrypoint\(") 'the patch strategy must be the one applied to the observed peer state'
Assert-True ($deployScript -match "(?s)with_local_bind and 'PEER_LOCAL_BIND_ADDRESS' not in text") 'a repo-copy write must also supply PEER_LOCAL_BIND_ADDRESS'
Assert-True ($deployScript -notmatch "\[SERVICE\]\['image'\]") 'a build-only service has no image key; .get must be used'
Assert-True ($deployScript -match "\.get\('image'\)") 'the rendered image must be read with .get'
# The running image id has to be pinned BEFORE the build, because the build
# retags it in place and the known-good image would otherwise become dangling.
$preserveIndex = $deployScript.IndexOf("preserved_tag")
$buildIndex = $deployScript.IndexOf("compose_run('build'")
Assert-True ($preserveIndex -gt 0 -and $preserveIndex -lt $buildIndex) 'the running image must be tagged pre-batch-<stamp> before the build'
Assert-True ($deployScript -match "pre-batch-") 'the preserved tag must name itself'
Assert-True ($deployScript -match "(?s)rollback = .*?'tag', preserved_tag, running_image_ref") 'the printed rollback must restore the image tag, not only the files'
# Preconditions must refuse before the first backup or write.
$preconditionIndex = $deployScript.IndexOf('preconditions = assert_preconditions()')
$backupIndex = $deployScript.IndexOf('backup.mkdir(')
Assert-True ($preconditionIndex -gt 0 -and $preconditionIndex -lt $backupIndex) 'preconditions must be asserted before any backup or rewrite'
# Verification runs from quant-research (psycopg + PG*), not from the sidecar,
# which has no PostgreSQL client at all - and 5432 must still answer afterwards.
Assert-True ($deployScript -match "VERIFIER = 'quant-research'") 'the probe must run from the container that actually has a database client'
Assert-True ($deployScript -match "host='db-tunnel'") 'the probe must target db-tunnel explicitly rather than a unix socket'
Assert-True ($deployScript -match 'SELECT 1, inet_server_port') 'the probe must prove what is behind the socket, not that the socket is open'
Assert-True ($deployScript -match "1\|55432") '55432 is the only answer that proves the owner PostgreSQL'
Assert-True ($deployScript -match "(?s)batch_query = probe_port\(BATCH_LOCAL_PORT\).*?intraday_query = probe_port\(INTRADAY_LOCAL_PORT\)") 'the intraday port must be re-proven after the recreate'
# compose.yaml is rewritten at a literal anchor, so its rendered form is not
# enough evidence: a reformatted-but-equivalent compose passes a rendered check
# and then dies mid-write, after .env has already been changed.
Assert-True ($deployScript -match "'compose_sha256':") 'each known peer state must pin the raw compose.yaml it may be rewritten in'
Assert-True ($deployScript -match "(?s)mismatch = .*?'compose_sha256'") 'the compose pin must be checked in the refusal gate'
# Retagging an image does nothing to a container already running from it.
Assert-True ($deployScript -match "(?s)recreated = True.*?except BaseException:.*?if recreated:") 'a failure after up -d must recreate the container, not only restore files and tags'
Assert-True ($deployScript -match "(?s)if recreated:.*?probe_port\(INTRADAY_LOCAL_PORT\)") 'the rollback must re-probe 5432 and report whether the peer is back'
# A retag that failed silently used to let the recreate come back on the
# REJECTED image while the script printed "rolled back".
Assert-True ($deployScript -match "retag = subprocess\.run\(D \+ \['tag', preserved_tag, running_image_ref\], check=False\)") 'the rollback retag result must be kept, not discarded'
Assert-True ($deployScript -match "restored_image_id != running_image_id") 'the recreated container image id must be compared with the preserved one'
Assert-True ($deployScript -match "(?s)preserved image id:.*?recreated image id:") 'both image ids must be printed so the rollback claim can be checked'
# The build retags the reference whether or not a container is recreated, so the
# retag has to be read back on BOTH paths. This branch used to print "nothing
# else changed on this peer" without ever looking at retag.returncode.
Assert-True ($deployScript -match "(?s)if built:[\s\S]*?D \+ \['image', 'inspect', running_image_ref\]") 'the restored tag must be resolved with docker image inspect, not inferred from docker tag''s exit code'
Assert-True ($deployScript -match "tag_restored = built and retag\.returncode == 0 and retagged_image_id == running_image_id") 'the tag is restored only when the reference resolves to the preserved image'
Assert-True ($deployScript -match "elif built and not tag_restored:") 'a build-but-no-recreate failure must judge the retag too'
Assert-True ($deployScript -match 'IMAGE TAG NOT RESTORED') 'and it must say so loudly instead of claiming nothing else changed'
$tagRestoredIndex = $deployScript.IndexOf('tag_restored = built')
$recreatedBranchIndex = $deployScript.IndexOf('if recreated:', $tagRestoredIndex)
Assert-True ($tagRestoredIndex -gt 0 -and $recreatedBranchIndex -gt $tagRestoredIndex) 'the retag verdict must be taken before either rollback branch uses it'
# The already-deployed short-circuit reads file state, and the files are written
# BEFORE the build: a run killed in that window leaves them behind on a peer
# whose image never included the forward. The image tag is the build's own trace.
Assert-True ($deployScript -match "def find_batch_image_tag") 'the short-circuit must be able to look for the tag a completed build leaves'
Assert-True ($deployScript -match "D \+ \['image', 'ls', '--filter', 'reference=' \+ pattern") 'the batch tags must be narrowed with a docker image ls reference filter'
Assert-True ($deployScript -match "(?s)def find_batch_image_tag[\s\S]*?D \+ \['image', 'inspect', tag\][\s\S]*?if resolved == image_id:") 'and each candidate tag must resolve to the image the container is actually running'
Assert-True ($deployScript -match "(?s)if batch_value and BATCH_FORWARD_MARKER in entrypoint_text:[\s\S]*?batch_tag = find_batch_image_tag\([\s\S]*?if batch_tag:") 'already-deployed must additionally require the batch image tag to exist'
Assert-True ($deployScript -match 'a build that included it') 'an interrupted deploy must be refused by name, not reported as deployed'
Assert-True ($deployScript -match 'interrupted between the entrypoint write and') 'the refusal must name the interrupted-deploy case so the operator knows what to restore'
Assert-True ($deployScript -match "batch_tag\], check=True\)") 'the batch tag is now evidence a later run reads, so failing to create it must fail the deploy'
# The idempotent re-run must not look like a failure to a wrapper - but the mere
# PRESENCE of the key is not evidence of a deploy. This repository's own compose
# declares it unconditionally with an empty default.
Assert-True ($deployScript -match "(?s)already deployed on this peer.*?raise SystemExit\(0\)") 'already-deployed must print and exit 0'
$matchesIndex = $deployScript.IndexOf('matches = [(key, value)')
$deployedIndex = $deployScript.IndexOf('already deployed on this peer')
Assert-True ($matchesIndex -gt 0 -and $matchesIndex -lt $deployedIndex) 'the already-deployed short-circuit must be judged after the known-state lookup'
Assert-True ($deployScript -match "batch_value = str\(environment\.get\('PEER_BATCH_DB_PORT'\) or ''\)\.strip\(\)") 'the short-circuit must test the rendered VALUE, not the key'
Assert-True ($deployScript -match "if batch_value and BATCH_FORWARD_MARKER in entrypoint_text:") 'and it must also require the batch forward to be in the deployed entrypoint'
Assert-True ($deployScript -notmatch "if 'PEER_BATCH_DB_PORT' in observed\['environment_keys'\]") 'the key-presence short-circuit must be gone'

[pscustomobject]@{
    passed = $true
    intraday_vector_pinned = $true
    control_master_pinned_both_profiles = $true
    compression_declared_not_inferred = $true
    connection_verdict_covered = $true
    batch_compression_only = $true
    reclaim_sets_disjoint = $true
    installer_dry_run = $true
    batch_install_failure_disables_task_in_source = $true
    batch_health_gate_executed_fresh_and_stale = $true
    batch_health_gate_survives_missing_field = $true
    batch_health_gate_checks_run_id_not_only_the_clock = $true
    batch_health_gate_accepts_a_live_duplicate_supervisor = $true
    batch_health_gate_refuses_a_stopping_previous_run = $true
    batch_health_gate_poll_breaks_on_fresh_not_accept = $true
    batch_health_gate_race_against_the_stopped_run_executed = $true
    batch_health_gate_weak_accept_kept_as_fallback = $true
    batch_health_gate_polls_until_deadline_in_source = $true
    installer_persists_supervisor_liveness_receipt = $true
    peer_deploy_already_deployed_needs_value_entrypoint_and_image_tag = $true
    peer_deploy_rollback_verifies_the_restored_image = $true
    peer_deploy_rollback_verifies_the_retag_without_a_recreate = $true
    permitopen_and_permitlisten_cover_15433 = $true
    peer_deploy_refusal_wired_in_source = $true
    peer_deploy_behaviour_tested_in = 'quant-service/tests/test_peer_batch_tunnel_deploy.py'
}
