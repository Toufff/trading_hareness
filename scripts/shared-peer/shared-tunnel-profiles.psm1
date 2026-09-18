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

Export-ModuleMember -Function @(
    'Get-SharedTunnelProfile',
    'Get-SharedTunnelSshArgument',
    'Get-SharedTunnelProcessPattern',
    'Test-SharedTunnelCommandLine',
    'Get-SharedTunnelConnectionVerdict'
)
