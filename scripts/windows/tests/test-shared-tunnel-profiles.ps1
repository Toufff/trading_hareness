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
$expectedIntraday = @(
    '-NT',
    '-o', 'BatchMode=yes',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30',
    '-o', 'ServerAliveCountMax=3',
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
Assert-True ($installer -match '\[switch\]\$WhatIf') 'the installer must offer a dry run'
# The dry run must return before anything mutates the host.
$whatIfIndex = $installer.IndexOf('if ($WhatIf) {')
$stopIndex = $installer.IndexOf('Stop-ScheduledTask')
$registerIndex = $installer.IndexOf('Register-ScheduledTask')
Assert-True ($whatIfIndex -gt 0 -and $whatIfIndex -lt $stopIndex -and $whatIfIndex -lt $registerIndex) 'the dry run must short-circuit before any scheduled-task or process change'
Assert-True ($bothInstaller -match '-Profile intraday' -and $bothInstaller -match '-Profile batch') 'the combined installer must install both profiles'
Assert-True ($bothInstaller.IndexOf('-Profile intraday') -lt $bothInstaller.IndexOf('-Profile batch')) 'intraday must be installed first'
Assert-True ($bothInstaller -match 'RequireBatch') 'a batch failure must be non-fatal unless the caller demands it'

# --- peer side -------------------------------------------------------------
$entrypoint = Get-Content (Join-Path $repository 'deploy\shared-peer\ssh-tunnel-entrypoint.sh') -Raw
Assert-True ($entrypoint -match '\$\{PEER_BATCH_DB_PORT:-\}') 'the peer entrypoint must treat the batch forward as optional'
Assert-True ($entrypoint -match '\$\{PEER_BATCH_REMOTE_PORT:-15433\}') 'the peer entrypoint must default the batch remote port to 15433'
Assert-True ($entrypoint -match '(?s)5432:127\.0\.0\.1:\$\{REMOTE_DB_PORT\}.*5681:127\.0\.0\.1:\$\{REMOTE_API_PORT\}') 'the existing peer forwards must stay unchanged'
$composeText = Get-Content (Join-Path $repository 'deploy\shared-peer\compose.yaml') -Raw
Assert-True ($composeText -match 'PEER_BATCH_DB_PORT: \$\{PEER_BATCH_DB_PORT:-\}') 'compose must default the batch port to off'
Assert-True ($composeText -match 'ssh-tunnel-healthcheck') 'the db-tunnel healthcheck must not start depending on the optional batch port'

[pscustomobject]@{
    passed = $true
    intraday_vector_pinned = $true
    batch_compression_only = $true
    reclaim_sets_disjoint = $true
    installer_dry_run = $true
}
