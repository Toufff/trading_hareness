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
Assert-True ($installer -match 'Get-SharedTunnelStateFreshnessVerdict -State \$state -InstallStartedAt \$installStartedAt') 'the batch health check must judge state freshness through the pure function the tests below execute'
Assert-True ($installer -match '\$installStartedAt') 'the installer must timestamp the install so stale state cannot pass for health'
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

    # 1. Fresh: the supervisor started this run after the task was registered.
    $fresh = Write-FakeRuntimeState @{
        schema_version = 1; service = $batchService; status = 'process_started'
        run_id = '20260919T040000000-abcdef12'; supervisor_pid = 4242; launcher_pid = 4243
        started_at = $installStartedAt.AddSeconds(3).ToString('o')
        updated_at = $installStartedAt.AddSeconds(3).ToString('o')
    }
    Assert-True (Test-Path -LiteralPath $fresh -PathType Leaf) 'the fake runtime state file must exist where Get-RuntimeState looks for it'
    $freshState = Get-RuntimeState -PlatformRoot $stateRoot -Service $batchService
    Assert-True ($null -ne $freshState -and [bool]$freshState.PSObject.Properties['run_id']) 'the installer run_id precondition must be satisfied by a real supervised state'
    $freshVerdict = Get-SharedTunnelStateFreshnessVerdict -State $freshState -InstallStartedAt $installStartedAt
    Assert-True $freshVerdict.fresh 'a state whose started_at post-dates the install is this install''s state'
    Assert-True ($freshVerdict.reason -eq 'state_belongs_to_install') 'the fresh verdict names itself'
    Assert-True ($freshVerdict.field -eq 'started_at') 'the gate must key off the field the supervisor actually writes'

    # 2. Stale: yesterday's run left a state file behind and the task never came up.
    [void](Write-FakeRuntimeState @{
        schema_version = 1; service = $batchService; status = 'healthy'
        run_id = '20260918T030000000-0badbeef'; supervisor_pid = 1111; launcher_pid = 1112
        started_at = $installStartedAt.AddDays(-1).ToString('o')
        updated_at = $installStartedAt.AddDays(-1).ToString('o')
    })
    $staleVerdict = Get-SharedTunnelStateFreshnessVerdict `
        -State (Get-RuntimeState -PlatformRoot $stateRoot -Service $batchService) -InstallStartedAt $installStartedAt
    Assert-True (-not $staleVerdict.fresh) 'yesterday''s leftover state must never pass for this install''s health'
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
    try { $missingVerdict = Get-SharedTunnelStateFreshnessVerdict -State $missingState -InstallStartedAt $installStartedAt }
    catch { $threw = $true }
    Assert-True (-not $threw) 'a state file without the field must not throw under StrictMode'
    Assert-True (-not $missingVerdict.fresh) 'an absent timestamp is not evidence that this install produced the state'
    Assert-True ($missingVerdict.reason -eq 'field_missing') 'a missing field is reported as missing'
    Assert-True ($missingVerdict.message -match '<absent>') 'the failure message must be buildable without the field'

    # 4. No state file at all, and an unparsable stamp.
    $noStateVerdict = Get-SharedTunnelStateFreshnessVerdict -State $null -InstallStartedAt $installStartedAt
    Assert-True ((-not $noStateVerdict.fresh) -and $noStateVerdict.reason -eq 'no_runtime_state') 'a missing state file is reported as such'
    [void](Write-FakeRuntimeState @{ schema_version = 1; service = $batchService; run_id = 'x'; started_at = 'not a timestamp' })
    $badVerdict = Get-SharedTunnelStateFreshnessVerdict `
        -State (Get-RuntimeState -PlatformRoot $stateRoot -Service $batchService) -InstallStartedAt $installStartedAt
    Assert-True ((-not $badVerdict.fresh) -and $badVerdict.reason -eq 'field_unparsable') 'an unreadable timestamp is not proof of freshness'
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
# The idempotent re-run must not look like a failure to a wrapper.
Assert-True ($deployScript -match "(?s)already deployed on this peer.*?raise SystemExit\(0\)") 'already-deployed must print and exit 0'

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
    permitopen_and_permitlisten_cover_15433 = $true
    peer_deploy_refusal_wired_in_source = $true
    peer_deploy_behaviour_tested_in = 'quant-service/tests/test_peer_batch_tunnel_deploy.py'
}
