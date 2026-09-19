[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ReleaseId,
    [string]$PlatformRoot = 'G:\StockPlatform',
    # Same meaning as in publish-stock-release.ps1. Empty = pick S4U when
    # elevated, else Interactive. This script used to leave
    # install-shared-tunnel-task.ps1's own 'S4U' default in place, which both
    # fails outright in an unelevated session and disagrees with what publish
    # registers -- and the tunnel reinstall gate now compares the principal a
    # reinstall would register against the one the registered task carries, so
    # the two callers have to answer it identically.
    [ValidateSet('', 'S4U', 'Interactive')][string]$TaskLogonType = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\stock-release-management.psm1') -Force
if (-not $TaskLogonType) { $TaskLogonType = Get-StockScheduledTaskLogonType }
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$layout = Get-StockReleaseLayout -PlatformRoot $platform
$target = Get-StockReleaseAppPath -PlatformRoot $platform -ReleaseId $ReleaseId
if (-not (Test-Path -LiteralPath $target -PathType Container)) { throw "Unknown release: $ReleaseId" }
$state = Get-StockReleaseState -PlatformRoot $platform
$oldRelease = if ($state.PSObject.Properties['active_release']) { [string]$state.active_release } else { '' }
$oldTarget = Get-StockCurrentReleaseTarget -PlatformRoot $platform
if ($oldRelease -eq $ReleaseId) { [pscustomobject]@{ status = 'already_active'; release_id = $ReleaseId; target = $target }; return }

# The release the shared-peer tunnel task is actually running from, as recorded
# by the last real reinstall. It is NOT necessarily the active release: a
# skipped publish leaves it behind while `current` moves on. It only moves here
# when a reinstall really happens, so the revert path below compares against the
# tree the tunnel is running now, not the one it ran before this script began.
$tunnelRelease = if ($state.PSObject.Properties['tunnel_release']) { [string]$state.tunnel_release } else { '' }
$keepTunnel = $false

function Get-LogonTypeArguments {
    # The revert path runs the PREVIOUS release's installer, and older releases'
    # copies have no -LogonType parameter; only forward it when declared.
    param([string]$Installer)
    $command = Get-Command -Name $Installer -ErrorAction Stop
    if ($command.Parameters.ContainsKey('LogonType')) { return @{ LogonType = $TaskLogonType } }
    return @{}
}

function Install-SharedPeerTunnelTask {
    # The plural fan-out installs the intraday tunnel (unguarded) and then the
    # batch tunnel (reported, not raised). The singular fallback exists for the
    # revert path, which runs the PREVIOUS release's tree: a release published
    # before the batch profile existed carries only the singular script, and
    # reverting to it must still install the tunnel it does have. Same reason as
    # Get-LogonTypeArguments above.
    param([Parameter(Mandatory)][string]$RuntimeRoot)
    $installer = Join-Path $RuntimeRoot 'scripts\shared-peer\install-shared-tunnel-tasks.ps1'
    if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) {
        $installer = Join-Path $RuntimeRoot 'scripts\shared-peer\install-shared-tunnel-task.ps1'
        # Reverting to a tree that predates the batch profile: nothing in it can
        # install, verify or supervise the batch tunnel, so the task a newer
        # release registered is disabled instead of being left enabled and
        # pointed at a tree with no batch profile. SilentlyContinue because
        # "never registered" is the ordinary case, not an error.
        Get-ScheduledTask -TaskName 'trading-hareness-shared-peer-batch-tunnel' -ErrorAction SilentlyContinue |
            Disable-ScheduledTask -ErrorAction SilentlyContinue | Out-Null
    }
    $extra = Get-LogonTypeArguments -Installer $installer
    & $installer -ScriptPath (Join-Path $RuntimeRoot 'scripts\shared-peer\start-shared-tunnels.ps1') `
        -PlatformRoot $platform @extra | Out-Null
}

function Resolve-TunnelGate {
    # Returns the reinstall plan, or $null when the gate could not be
    # evaluated (which the caller must treat as 'reinstall').
    # See Resolve-StockTunnelReinstallPlan in stock-release-management.psm1:
    # identical tunnel-affecting files and SSH target + task Running under
    # <PlatformRoot>\current + runtime state healthy + remote API health 200 +
    # a tunnel_release that is still on disk and still retained. Must be
    # evaluated BEFORE anything is stopped.
    #
    # After a skip the tunnel's supervisor, background host and ssh client keep
    # running out of the tunnel_release directory until their next real restart;
    # that release is pinned in the retention policy, so it cannot be pruned
    # out from under the running process.
    param([Parameter(Mandatory)][string]$NewRuntimeRoot, [string]$TunnelReleaseId)
    try {
        return Resolve-StockTunnelReinstallPlan -PlatformRoot $platform `
            -NewRuntimeRoot $NewRuntimeRoot -TunnelReleaseId ([string]$TunnelReleaseId) `
            -TaskLogonType $TaskLogonType
    } catch {
        Write-Warning "Tunnel reinstall gate could not be evaluated, reinstalling: $($_.Exception.Message)"
        return $null
    }
}

function Get-TunnelSshTargetPin {
    # SHA-256 of the SSH identity the tunnel was just (re)installed against.
    $resolved = Get-StockTunnelSshTargetHash -PlatformRoot $platform
    if ($resolved -eq 'unresolved') { return $null }
    return $resolved
}

function Get-CarriedTunnelSshTargetPin {
    # After a skip the tunnel is still connected with the identity recorded at
    # its last real reinstall; that recorded value must survive this write.
    if ($state.PSObject.Properties['tunnel_ssh_target_sha256']) { return $state.tunnel_ssh_target_sha256 }
    return $null
}

try {
    # Integrity check before touching anything running: refuse the switch
    # outright if the target release's own files no longer match the
    # SHA-256 manifest captured when it was published.
    [void](Test-StockReleaseIntegrity -PlatformRoot $platform -ReleaseId $ReleaseId)
    $tunnelPlan = Resolve-TunnelGate -NewRuntimeRoot $target -TunnelReleaseId $tunnelRelease
    $keepTunnel = ($null -ne $tunnelPlan) -and ([string]$tunnelPlan.decision -eq 'skip')
    # The gate may have refreshed the pin: tunnel_release records where the
    # tunnel was last installed from, but every relaunch starts it from
    # `current`, so a run that began after the active release was published came
    # from the active release. Carry the gate's answer, not the stale record.
    if ($keepTunnel -and [string]$tunnelPlan.tunnel_release) { $tunnelRelease = [string]$tunnelPlan.tunnel_release }
    # Graceful stop (of the currently-active/failed target) before
    # Stop-ScheduledTask: see the matching comment in
    # publish-stock-release.ps1's Stop-ProductionRuntime for why the order
    # matters (Stop-ScheduledTask kills the supervisor before it can record
    # an expected exit).
    if ($oldTarget) { & (Join-Path $oldTarget 'scripts\windows\stop-stock-dashboard.ps1') -PlatformRoot $platform | Out-Null }
    # Both tunnels are stopped together and spared together: the gate judges
    # both tasks, so a skip already proved the batch one Running, healthy and
    # rooted under `current`.
    if (-not $keepTunnel) {
        Stop-ScheduledTask -TaskName 'trading-hareness-shared-peer-tunnels' -ErrorAction SilentlyContinue
        Stop-ScheduledTask -TaskName 'trading-hareness-shared-peer-batch-tunnel' -ErrorAction SilentlyContinue
    }
    Stop-ScheduledTask -TaskName 'trading-hareness-dashboard-runtime' -ErrorAction SilentlyContinue
    [void](Set-StockCurrentRelease -PlatformRoot $platform -ReleaseId $ReleaseId)
    # The instant `current` actually moved, recorded in release-state.json
    # below. The release id's own timestamp is not it (it is stamped before the
    # publish's test suite runs), and the tunnel pin refresh needs the real one.
    $activatedAt = [DateTimeOffset]::Now.ToString('o')
    & (Join-Path $layout.CurrentPath 'scripts\windows\install-stock-dashboard-task.ps1') -RepositoryRoot $layout.CurrentPath -PlatformRoot $platform | Out-Null
    if ($keepTunnel) {
        Write-Verbose 'Shared-peer tunnel task left untouched by the reinstall gate.'
    } else {
        Install-SharedPeerTunnelTask -RuntimeRoot $layout.CurrentPath
        $tunnelRelease = $ReleaseId
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(150)
    do {
        Start-Sleep -Seconds 2
        try {
            $api = Invoke-RestMethod 'http://127.0.0.1:5681/health' -TimeoutSec 20
            $adapter = Invoke-RestMethod 'http://127.0.0.1:5680/health' -TimeoutSec 3
            if ($api.status -eq 'ok' -and $adapter.status -eq 'ok') { break }
        } catch { }
    } while ([DateTime]::UtcNow -lt $deadline)
    if ([DateTime]::UtcNow -ge $deadline) { throw 'Rollback target did not become healthy' }
    # verify-shared-runtime.ps1 is a PowerShell script: it throws on failure
    # (propagated because both scripts run with $ErrorActionPreference =
    # 'Stop'), so checking $LASTEXITCODE afterward would only reflect
    # whatever native command it happened to run last.
    $tunnelOutcome = if ($keepTunnel) { 'reused_without_reinstall' } else { 'reinstalled' }
    try {
        & (Join-Path $layout.CurrentPath 'scripts\shared-peer\verify-shared-runtime.ps1') | Out-Null
    } catch {
        # A skip is only as good as the verification that follows it: when the
        # gate spared the tunnel, the spared tunnel is a prime suspect for the
        # failure and nothing else in this run would ever restart it. Reinstall
        # and verify again before failing. Without a skip this is the
        # pre-existing behaviour: the failure propagates to the revert path.
        if (-not $keepTunnel) { throw }
        Write-Warning "Shared runtime verification failed after a skipped tunnel reinstall; reinstalling the shared-peer tunnel and re-verifying: $($_.Exception.Message)"
        $keepTunnel = $false
        $tunnelOutcome = 'reinstalled_after_degraded_verification'
        Install-SharedPeerTunnelTask -RuntimeRoot $layout.CurrentPath
        $tunnelRelease = $ReleaseId
        & (Join-Path $layout.CurrentPath 'scripts\shared-peer\verify-shared-runtime.ps1') | Out-Null
    }
    [void](Set-StockReleaseState -PlatformRoot $platform -State @{
        active_release = $ReleaseId
        previous_release = if ($oldRelease) { $oldRelease } else { $null }
        activated_at = $activatedAt
        last_verification = @{ verified_at = [DateTimeOffset]::Now.ToString('o'); result = 'verified_after_switch'; shared_peer_tunnel = $tunnelOutcome }
        last_failed_release = $null
        tunnel_release = if ($tunnelRelease) { $tunnelRelease } else { $null }
        tunnel_ssh_target_sha256 = if ($keepTunnel) { Get-CarriedTunnelSshTargetPin } else { Get-TunnelSshTargetPin }
    })
    if ($keepTunnel) {
        # Only now: activation, post-switch verification AND the release-state
        # write have all succeeded, which is what Write-StockTunnelReinstallSkipEvent's
        # own contract requires. The state write can still throw (a state file
        # locked by a concurrent get-stock-release-status run, a full disk), and
        # this script's catch then reverts and may reinstall the tunnel, so a
        # receipt written before it would be one the revert's own events
        # contradict. Same ordering as publish-stock-release.ps1's.
        Write-StockTunnelReinstallSkipEvent -PlatformRoot $platform -Plan $tunnelPlan -Context 'switch-stock-release.ps1'
    }
    [pscustomobject]@{
        status = 'switched'
        release_id = $ReleaseId
        previous_release = $oldRelease
        target = $target
        shared_peer_tunnel = $tunnelOutcome
        tunnel_release = $tunnelRelease
    }
} catch {
    $failure = $_
    if ($oldRelease -and (Test-Path -LiteralPath (Get-StockReleaseAppPath -PlatformRoot $platform -ReleaseId $oldRelease) -PathType Container)) {
        try {
            [void](Test-StockReleaseIntegrity -PlatformRoot $platform -ReleaseId $oldRelease)
            $revertTarget = Get-StockReleaseAppPath -PlatformRoot $platform -ReleaseId $oldRelease
            # Same gate on the way back. If the forward path already skipped,
            # $tunnelRelease still names the release the tunnel is running
            # from, which is normally the one being reverted to, so the revert
            # skips too and the tunnel is never touched by a failed switch.
            $revertPlan = Resolve-TunnelGate -NewRuntimeRoot $revertTarget -TunnelReleaseId $tunnelRelease
            $keepTunnelRevert = ($null -ne $revertPlan) -and ([string]$revertPlan.decision -eq 'skip')
            if ($keepTunnelRevert -and [string]$revertPlan.tunnel_release) { $tunnelRelease = [string]$revertPlan.tunnel_release }
            [void](Set-StockCurrentRelease -PlatformRoot $platform -ReleaseId $oldRelease)
            $revertActivatedAt = [DateTimeOffset]::Now.ToString('o')
            & (Join-Path $layout.CurrentPath 'scripts\windows\install-stock-dashboard-task.ps1') -RepositoryRoot $layout.CurrentPath -PlatformRoot $platform | Out-Null
            if ($keepTunnelRevert) {
                Write-Verbose 'Shared-peer tunnel task left untouched by the reinstall gate during revert.'
            } else {
                Install-SharedPeerTunnelTask -RuntimeRoot $layout.CurrentPath
                $tunnelRelease = $oldRelease
            }
            $revertDeadline = [DateTime]::UtcNow.AddSeconds(150)
            do {
                Start-Sleep -Seconds 2
                try {
                    $api = Invoke-RestMethod 'http://127.0.0.1:5681/health' -TimeoutSec 20
                    $adapter = Invoke-RestMethod 'http://127.0.0.1:5680/health' -TimeoutSec 3
                    if ($api.status -eq 'ok' -and $adapter.status -eq 'ok') { break }
                } catch { }
            } while ([DateTime]::UtcNow -lt $revertDeadline)
            if ([DateTime]::UtcNow -ge $revertDeadline) { throw 'Reverted release did not become healthy' }
            [void](Set-StockReleaseState -PlatformRoot $platform -State @{
                active_release = $oldRelease
                previous_release = if ($state.PSObject.Properties['previous_release']) { $state.previous_release } else { $null }
                activated_at = $revertActivatedAt
                last_verification = @{ verified_at = [DateTimeOffset]::Now.ToString('o'); result = 'verified_after_switch_revert' }
                last_failed_release = $ReleaseId
                failure_message = $failure.Exception.Message
                tunnel_release = if ($tunnelRelease) { $tunnelRelease } else { $null }
                tunnel_ssh_target_sha256 = if ($keepTunnelRevert) { Get-CarriedTunnelSshTargetPin } else { Get-TunnelSshTargetPin }
            })
            # After the revert's own release-state write, for the same reason as
            # on the forward path: until it lands, nothing here is a fact.
            if ($keepTunnelRevert) {
                Write-StockTunnelReinstallSkipEvent -PlatformRoot $platform -Plan $revertPlan -Context 'switch-stock-release.ps1:revert'
            }
        } catch { Write-Warning "Automatic revert to $oldRelease also failed: $($_.Exception.Message)" }
    }
    throw $failure
}
