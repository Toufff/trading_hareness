param(
    # 'intraday' is the historical connection (owner database + owner API) and
    # must keep behaving exactly as it did. 'batch' is a second, independent
    # SSH connection carrying only the database forward, so a bulk/COPY job
    # cannot starve intraday queries on a shared TCP window. See
    # shared-tunnel-profiles.psm1 for why a second forward would not have
    # helped.
    [ValidateSet('intraday', 'batch')][string]$Profile = 'intraday',
    [string]$SshAlias = "lightServer1",
    [int]$RemoteDatabasePort = 15432,
    [int]$RemoteApiPort = 15681,
    [int]$RemoteBatchDatabasePort = 15433,
    [int]$LocalDatabasePort = 55432,
    [int]$LocalApiPort = 5681,
    [string]$PlatformRoot = 'G:\StockPlatform'
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\runtime-observability.psm1') -Force
Import-Module (Join-Path $repository 'scripts\windows\background-process.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'shared-tunnel-profiles.psm1') -Force
$tunnelProfile = Get-SharedTunnelProfile -Name $Profile `
    -RemoteDatabasePort $RemoteDatabasePort -RemoteApiPort $RemoteApiPort `
    -RemoteBatchDatabasePort $RemoteBatchDatabasePort `
    -LocalDatabasePort $LocalDatabasePort -LocalApiPort $LocalApiPort
$service = $tunnelProfile.Service
$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$runtimeEnvPath = Join-Path $PlatformRoot 'config\runtime.env'
$tunnelTarget = Resolve-OwnerTunnelSshTarget -RuntimeEnv $runtimeEnvPath -FallbackAlias $SshAlias
$controlTarget = Resolve-OwnerTunnelControlSshTarget -FallbackAlias $SshAlias

# --- reclaim stale remote listeners before binding -------------------------
#
# When the ssh client dies without a clean disconnect, the far end can keep the
# forwarded listener open. The socket still accepts TCP, but no channel can be
# opened through it, so a caller sees "connect succeeds, query fails" while the
# next launch here dies in about four seconds with
#     Error: remote port forwarding failed for listen port 15432
# because -o ExitOnForwardFailure=yes refuses to run without its forwards.
# Measured 2026-09-05: 23 consecutive launches failed that way between 00:02
# and 00:46, and the two-minute supervising trigger could not break the
# deadlock - it faithfully retried into the same occupied port until the stale
# listener happened to expire. The dashboard tunnel has reclaimed its own port
# this way since it was written; this path never did.
function Test-RemoteTunnelListener {
    param([Parameter(Mandatory)][int]$Port)
    $probe = Invoke-ConsoleFreeCommand -FilePath $ssh -Arguments (@($controlTarget.ConnectionArguments) + @(
        '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', $controlTarget.Destination,
        "ss -ltn 'sport = :$Port' | tail -n +2 | grep -q .")) -TimeoutSeconds 15
    return $probe.ExitCode -eq 0
}

function Remove-StaleRemoteTunnelListener {
    param([Parameter(Mandatory)][int]$Port)
    # The reserved-port guard defaults to the dashboard/API/peer trio and does
    # not include the database port, so the allowed set is passed explicitly:
    # this must never be able to kill an arbitrary listener on the shared host.
    # The set is the active profile's own ports only - the batch port 15433 is
    # reclaimable from the batch profile and from nothing else.
    [void](Assert-ReservedRemoteTunnelPort -Port $Port -AllowedPorts @($tunnelProfile.RemotePorts))
    [void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service $service `
        -Event 'stale_remote_listener_cleanup_requested' -Level 'warning' -Data @{
            remote_port = $Port
            ssh_host = $SshAlias
            ssh_target_mode = $controlTarget.Mode
            tunnel_profile = $tunnelProfile.Name
        })
    $cleanup = Invoke-ConsoleFreeCommand -FilePath $ssh -Arguments (@($controlTarget.ConnectionArguments) + @(
        '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', $controlTarget.Destination,
        "fuser -k $Port/tcp >/dev/null 2>&1 || true")) -TimeoutSeconds 15
    if ($cleanup.ExitCode -ne 0) { throw "Failed to request cleanup of stale remote listener $Port" }
    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    while ((Test-RemoteTunnelListener -Port $Port) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    if (Test-RemoteTunnelListener -Port $Port) {
        throw "Remote tunnel port $Port remains occupied after bounded cleanup"
    }
}

# A live local client owns its remote listener, so that listener is not stale.
# Only reclaim when nothing here still holds this profile's forwards; otherwise
# a manual run of this script would tear down a perfectly healthy tunnel. The
# match is per profile, so starting the batch tunnel never inspects, reclaims
# or disturbs the intraday connection.
$liveLocalTunnel = @(Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { Test-SharedTunnelCommandLine -TunnelProfile $tunnelProfile -CommandLine $_.CommandLine })
if ($liveLocalTunnel.Count -eq 0) {
    foreach ($reservedPort in $tunnelProfile.RemotePorts) {
        if (Test-RemoteTunnelListener -Port $reservedPort) {
            Remove-StaleRemoteTunnelListener -Port $reservedPort
        }
    }
}

$arguments = Get-SharedTunnelSshArgument -TunnelProfile $tunnelProfile `
    -ConnectionArguments $tunnelTarget.ConnectionArguments -Destination $tunnelTarget.Destination

$run = Start-RuntimeSupervisor -PlatformRoot $PlatformRoot -RepositoryRoot $repository -Service $service `
    -Executable $ssh -WorkingDirectory $repository -Arguments $arguments -Metadata @{
        tunnel_profile = $tunnelProfile.Name
        ssh_alias = $SshAlias
        ssh_target_mode = $tunnelTarget.Mode
        ssh_control_target_mode = $controlTarget.Mode
        remote_database_port = $tunnelProfile.RemoteDatabasePort
        remote_api_port = $tunnelProfile.RemoteApiPort
        local_database_port = $tunnelProfile.LocalDatabasePort
        local_api_port = $tunnelProfile.LocalApiPort
        # The profile's own declared property, not "does it have any ssh
        # option at all" - the latter was true only as long as compression
        # happened to be the only option either profile carried.
        compression = [bool]$tunnelProfile.Compression
        stop_with_owner = $true
    }
while (Get-Process -Id ([int]$run.supervisor_pid) -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 2 }
$state = Get-RuntimeState -PlatformRoot $PlatformRoot -Service $service
# A duplicate caller must join the lock owner's lifetime, not overwrite its
# healthy state or return a failure that provokes another scheduled launch.
if ($state -and $state.run_id -ne $run.run_id -and $state.PSObject.Properties['supervisor_pid']) {
    $existing = Get-Process -Id ([int]$state.supervisor_pid) -ErrorAction SilentlyContinue
    if ($existing) { $existing.WaitForExit() }
    $state = Get-RuntimeState -PlatformRoot $PlatformRoot -Service $service
}
if ($state -and $state.PSObject.Properties['exit_code']) { exit ([int]$state.exit_code) }
exit 125
