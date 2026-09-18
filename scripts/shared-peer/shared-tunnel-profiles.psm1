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
        '-o', 'ServerAliveCountMax=3'
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

Export-ModuleMember -Function @(
    'Get-SharedTunnelProfile',
    'Get-SharedTunnelSshArgument',
    'Get-SharedTunnelProcessPattern',
    'Test-SharedTunnelCommandLine'
)
