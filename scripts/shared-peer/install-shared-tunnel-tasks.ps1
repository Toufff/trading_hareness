param(
    [string]$ScriptPath = (Join-Path $PSScriptRoot "start-shared-tunnels.ps1"),
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$SshAlias = 'lightServer1',
    [int]$RemoteDatabasePort = 15432,
    [int]$RemoteApiPort = 15681,
    [int]$RemoteBatchDatabasePort = 15433,
    [int]$LocalDatabasePort = 55432,
    [int]$LocalApiPort = 5681,
    [ValidateSet('S4U', 'Interactive')][string]$LogonType = 'S4U',
    # The batch tunnel is a throughput optimization, not a dependency of the
    # platform: intraday traffic, the dashboard and the peer gateway all work
    # without it. A failure to bring it up is therefore reported and recorded,
    # not raised, so it can never turn a good release into a failed one. Pass
    # -RequireBatch when the caller genuinely needs both (an acceptance run).
    [switch]$RequireBatch,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$installer = Join-Path $PSScriptRoot 'install-shared-tunnel-task.ps1'
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\runtime-observability.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'shared-tunnel-profiles.psm1') -Force

$common = @{
    ScriptPath = $ScriptPath
    PlatformRoot = $PlatformRoot
    SshAlias = $SshAlias
    RemoteDatabasePort = $RemoteDatabasePort
    RemoteApiPort = $RemoteApiPort
    RemoteBatchDatabasePort = $RemoteBatchDatabasePort
    LocalDatabasePort = $LocalDatabasePort
    LocalApiPort = $LocalApiPort
    LogonType = $LogonType
}
if ($WhatIf) { $common['WhatIf'] = $true }

# Intraday first and unguarded: it is the connection everything else depends
# on, so its failure must still surface exactly as it did when this was the
# only task.
$intraday = & $installer -Profile intraday @common

$batch = $null
$batchError = $null
try {
    $batch = & $installer -Profile batch @common
} catch {
    $batchError = $_.Exception.Message
    if ($RequireBatch) { throw }
    Write-Warning "Batch tunnel install failed; intraday traffic is unaffected. $batchError"
    if (-not $WhatIf) {
        [void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service 'shared-peer-batch-tunnel' `
            -Event 'install_failed' -Level 'error' -Data @{
                error = $batchError
                required = [bool]$RequireBatch
            })
    }
}

# --- the point of the whole feature, observed rather than assumed ------------
#
# `-o ControlMaster=no -o ControlPath=none` in shared-tunnel-profiles.psm1 makes
# it impossible for ssh_config to multiplex the batch session onto the intraday
# connection. That is a claim about the command line. This is the measurement:
# find each profile's live ssh.exe by its forwarding tuples, read the TCP
# connections the OS says that process owns, and require the two sets to be
# disjoint. If they are not - or if one of them cannot be observed at all - the
# batch tunnel is not buying a second congestion window and the operator must
# hear about it. It stays non-fatal for the same reason a batch install failure
# is: batch is an optimization and must not be able to fail a release.
$connectionVerdict = $null
if (-not $WhatIf -and $null -ne $batch) {
    try {
        $profiles = @{}
        $pids = @{}
        $connections = @{}
        foreach ($name in 'intraday', 'batch') {
            $profiles[$name] = Get-SharedTunnelProfile -Name $name `
                -RemoteDatabasePort $RemoteDatabasePort -RemoteApiPort $RemoteApiPort `
                -RemoteBatchDatabasePort $RemoteBatchDatabasePort `
                -LocalDatabasePort $LocalDatabasePort -LocalApiPort $LocalApiPort
            $process = @(Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
                Where-Object { Test-SharedTunnelCommandLine -TunnelProfile $profiles[$name] -CommandLine $_.CommandLine }) |
                Select-Object -First 1
            $pids[$name] = if ($process) { [int]$process.ProcessId } else { 0 }
            $connections[$name] = if ($pids[$name] -ne 0) {
                @(Get-NetTCPConnection -OwningProcess $pids[$name] -State Established -ErrorAction SilentlyContinue)
            } else { @() }
        }
        $connectionVerdict = Get-SharedTunnelConnectionVerdict `
            -IntradayConnections $connections['intraday'] -BatchConnections $connections['batch'] `
            -IntradayProcessId $pids['intraday'] -BatchProcessId $pids['batch']
        [void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service 'shared-peer-batch-tunnel' `
            -Event 'tunnel_connection_separation_checked' `
            -Level $(if ($connectionVerdict.distinct) { 'info' } else { 'warning' }) -Data @{
                distinct = [bool]$connectionVerdict.distinct
                reason = $connectionVerdict.reason
                shared_endpoints = @($connectionVerdict.shared_endpoints)
                intraday_ssh_pid = $pids['intraday']
                batch_ssh_pid = $pids['batch']
            })
        if (-not $connectionVerdict.distinct) {
            $separationError = "The intraday and batch tunnels do not own distinct TCP connections ($($connectionVerdict.reason)); batch traffic is not getting its own congestion window."
            if ($RequireBatch) { throw $separationError }
            Write-Warning $separationError
        }
    } catch {
        if ($RequireBatch) { throw }
        Write-Warning "Could not verify that the two tunnels own distinct TCP connections: $($_.Exception.Message)"
    }
}

[pscustomobject][ordered]@{
    intraday = $intraday
    batch = $batch
    batch_error = $batchError
    batch_required = [bool]$RequireBatch
    connection_separation = $connectionVerdict
}
