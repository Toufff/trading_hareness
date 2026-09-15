param(
    [string]$SshAlias = "lightServer1",
    [int]$RemoteDatabasePort = 15432,
    [int]$RemoteApiPort = 15681,
    [int]$LocalDatabasePort = 55432,
    [int]$LocalApiPort = 5681,
    [string]$PlatformRoot = 'G:\StockPlatform'
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\runtime-observability.psm1') -Force
Import-Module (Join-Path $repository 'scripts\windows\background-process.psm1') -Force
$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$runtimeEnvPath = Join-Path $PlatformRoot 'config\runtime.env'
$target = Resolve-OwnerTunnelSshTarget -RuntimeEnv $runtimeEnvPath -FallbackAlias $SshAlias

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
    $probe = Invoke-ConsoleFreeCommand -FilePath $ssh -Arguments (@($target.ConnectionArguments) + @(
        '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', $target.Destination,
        "ss -ltn 'sport = :$Port' | tail -n +2 | grep -q .")) -TimeoutSeconds 15
    return $probe.ExitCode -eq 0
}

function Remove-StaleRemoteTunnelListener {
    param([Parameter(Mandatory)][int]$Port)
    # The reserved-port guard defaults to the dashboard/API/peer trio and does
    # not include the database port, so the allowed set is passed explicitly:
    # this must never be able to kill an arbitrary listener on the shared host.
    [void](Assert-ReservedRemoteTunnelPort -Port $Port -AllowedPorts @($RemoteDatabasePort, $RemoteApiPort))
    [void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels' `
        -Event 'stale_remote_listener_cleanup_requested' -Level 'warning' -Data @{
            remote_port = $Port
            ssh_host = $SshAlias
            ssh_target_mode = $target.Mode
        })
    $cleanup = Invoke-ConsoleFreeCommand -FilePath $ssh -Arguments (@($target.ConnectionArguments) + @(
        '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', $target.Destination,
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
# Only reclaim when nothing here still holds both forwards; otherwise a manual
# run of this script would tear down a perfectly healthy tunnel.
$liveLocalTunnel = @(Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -match "127\.0\.0\.1:$RemoteDatabasePort`:127\.0\.0\.1:$LocalDatabasePort" -and
        $_.CommandLine -match "127\.0\.0\.1:$RemoteApiPort`:127\.0\.0\.1:$LocalApiPort"
    })
if ($liveLocalTunnel.Count -eq 0) {
    foreach ($reservedPort in @($RemoteDatabasePort, $RemoteApiPort)) {
        if (Test-RemoteTunnelListener -Port $reservedPort) {
            Remove-StaleRemoteTunnelListener -Port $reservedPort
        }
    }
}

$arguments = @($target.ConnectionArguments) + @(
    "-NT",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-R", "127.0.0.1:$RemoteDatabasePort`:127.0.0.1:$LocalDatabasePort",
    "-R", "127.0.0.1:$RemoteApiPort`:127.0.0.1:$LocalApiPort",
    $target.Destination
)

$run = Start-RuntimeSupervisor -PlatformRoot $PlatformRoot -RepositoryRoot $repository -Service 'shared-peer-tunnels' `
    -Executable $ssh -WorkingDirectory $repository -Arguments $arguments -Metadata @{
        ssh_alias = $SshAlias
        ssh_target_mode = $target.Mode
        remote_database_port = $RemoteDatabasePort
        remote_api_port = $RemoteApiPort
        local_database_port = $LocalDatabasePort
        local_api_port = $LocalApiPort
        stop_with_owner = $true
    }
while (Get-Process -Id ([int]$run.supervisor_pid) -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 2 }
$state = Get-RuntimeState -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels'
# A duplicate caller must join the lock owner's lifetime, not overwrite its
# healthy state or return a failure that provokes another scheduled launch.
if ($state -and $state.run_id -ne $run.run_id -and $state.PSObject.Properties['supervisor_pid']) {
    $existing = Get-Process -Id ([int]$state.supervisor_pid) -ErrorAction SilentlyContinue
    if ($existing) { $existing.WaitForExit() }
    $state = Get-RuntimeState -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels'
}
if ($state -and $state.PSObject.Properties['exit_code']) { exit ([int]$state.exit_code) }
exit 125
