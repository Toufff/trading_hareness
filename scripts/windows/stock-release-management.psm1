Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1')
Import-Module (Join-Path $PSScriptRoot 'runtime-observability.psm1')

function Write-ReleaseAtomicJson {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][object]$Value)
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = "$Path.$PID.$([Guid]::NewGuid().ToString('N')).tmp"
    [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Get-StockReleaseLayout {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$PlatformRoot)
    $platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
    return [pscustomobject]@{
        PlatformRoot = $platform
        ReleasesRoot = Join-Path $platform 'releases'
        CurrentPath = Join-Path $platform 'current'
        StatePath = Join-Path $platform 'release-state.json'
    }
}

function Get-StockReleaseState {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$PlatformRoot)
    $layout = Get-StockReleaseLayout -PlatformRoot $PlatformRoot
    if (-not (Test-Path -LiteralPath $layout.StatePath -PathType Leaf)) {
        return [pscustomobject]@{
            schema_version = 1
            active_release = $null
            previous_release = $null
            updated_at = $null
            last_verification = $null
            last_failed_release = $null
        }
    }
    return Get-Content -LiteralPath $layout.StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Set-StockReleaseState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][hashtable]$State
    )
    $layout = Get-StockReleaseLayout -PlatformRoot $PlatformRoot
    $payload = [ordered]@{
        schema_version = 1
        active_release = $null
        previous_release = $null
        updated_at = [DateTimeOffset]::Now.ToString('o')
        last_verification = $null
        last_failed_release = $null
    }
    foreach ($key in $State.Keys) { $payload[$key] = $State[$key] }
    $payload.updated_at = [DateTimeOffset]::Now.ToString('o')
    Write-ReleaseAtomicJson -Path $layout.StatePath -Value $payload
    return [pscustomobject]$payload
}

function Assert-StockReleaseId {
    param([Parameter(Mandatory)][string]$ReleaseId)
    if ($ReleaseId -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{2,127}$') { throw "Invalid release ID: $ReleaseId" }
}

function Get-StockReleaseAppPath {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$ReleaseId
    )
    Assert-StockReleaseId -ReleaseId $ReleaseId
    $layout = Get-StockReleaseLayout -PlatformRoot $PlatformRoot
    return Join-Path (Join-Path $layout.ReleasesRoot $ReleaseId) 'app'
}

function Get-StockCurrentReleaseTarget {
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$PlatformRoot)
    $layout = Get-StockReleaseLayout -PlatformRoot $PlatformRoot
    if (-not (Test-Path -LiteralPath $layout.CurrentPath)) { return $null }
    $item = Get-Item -LiteralPath $layout.CurrentPath -Force
    if (-not ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Production current path is not a junction: $($layout.CurrentPath)"
    }
    return [IO.Path]::GetFullPath([string]$item.Target).TrimEnd('\')
}

function Set-StockCurrentRelease {
    # Windows has no atomic replace-existing-directory primitive for
    # reparse points, so this cannot be made fully atomic. It is built to
    # minimize the window instead: the new junction is created under a
    # temporary sibling name and verified to resolve correctly *before*
    # anything happens to the existing `current` junction, so `current`
    # keeps pointing at the old (still-running) release for as long as
    # possible. The only gap where `current` does not exist at all is the
    # Remove-Item + Rename-Item pair immediately below, both fast
    # metadata-only operations on the same volume.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$ReleaseId
    )
    $layout = Get-StockReleaseLayout -PlatformRoot $PlatformRoot
    New-Item -ItemType Directory -Force -Path $layout.ReleasesRoot | Out-Null
    $target = [IO.Path]::GetFullPath((Get-StockReleaseAppPath -PlatformRoot $PlatformRoot -ReleaseId $ReleaseId)).TrimEnd('\')
    $releaseRoot = [IO.Path]::GetFullPath($layout.ReleasesRoot).TrimEnd('\')
    if (-not $target.StartsWith($releaseRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
        throw "Release target escapes the release root: $target"
    }
    if (-not (Test-Path -LiteralPath $target -PathType Container)) { throw "Release app does not exist: $target" }
    if (Test-Path -LiteralPath $layout.CurrentPath) {
        $current = Get-Item -LiteralPath $layout.CurrentPath -Force
        if (-not ($current.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "Refusing to replace a non-junction production path: $($layout.CurrentPath)"
        }
    }
    $stagingJunction = "$($layout.CurrentPath).next-$PID-$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
    if (Test-Path -LiteralPath $stagingJunction) { Remove-Item -LiteralPath $stagingJunction -Force }
    New-Item -ItemType Junction -Path $stagingJunction -Target $target | Out-Null
    $stagingItem = Get-Item -LiteralPath $stagingJunction -Force
    $stagingResolved = [IO.Path]::GetFullPath([string]$stagingItem.Target).TrimEnd('\')
    if (-not $stagingResolved.Equals($target, [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $stagingJunction -Force -ErrorAction SilentlyContinue
        throw "New release junction verification failed before swap: expected $target, got $stagingResolved"
    }
    if (Test-Path -LiteralPath $layout.CurrentPath) { Remove-Item -LiteralPath $layout.CurrentPath -Force }
    Rename-Item -LiteralPath $stagingJunction -NewName (Split-Path -Leaf $layout.CurrentPath)
    $resolved = Get-StockCurrentReleaseTarget -PlatformRoot $PlatformRoot
    if (-not $resolved.Equals($target, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Current release junction verification failed: expected $target, got $resolved"
    }
    return $resolved
}

function Remove-ExpiredStockReleases {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [int]$RetainCount = 3
    )
    $layout = Get-StockReleaseLayout -PlatformRoot $PlatformRoot
    if (-not (Test-Path -LiteralPath $layout.ReleasesRoot -PathType Container)) { return @() }
    $state = Get-StockReleaseState -PlatformRoot $PlatformRoot
    $active = if ($state.PSObject.Properties['active_release']) { [string]$state.active_release } else { '' }
    $previous = if ($state.PSObject.Properties['previous_release']) { [string]$state.previous_release } else { '' }
    # Releases that never activated successfully are renamed to "<id>.failed"
    # by the publish script's rollback path; exclude them here so the
    # retention policy only ever considers releases that were actually
    # activated, not a crash-looping publish's leftovers.
    $releases = @(Get-ChildItem -LiteralPath $layout.ReleasesRoot -Directory -Force |
        Where-Object { $_.Name -notlike '.staging-*' -and $_.Name -notlike '*.failed' } |
        Sort-Object LastWriteTimeUtc -Descending)
    $keep = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    if ($active) { [void]$keep.Add($active) }
    if ($previous) { [void]$keep.Add($previous) }
    foreach ($release in $releases) {
        if ($keep.Count -ge [Math]::Max(2, $RetainCount)) { break }
        [void]$keep.Add($release.Name)
    }
    $releaseRoot = [IO.Path]::GetFullPath($layout.ReleasesRoot).TrimEnd('\')
    $removed = @()
    foreach ($release in $releases) {
        if ($keep.Contains($release.Name)) { continue }
        $candidate = [IO.Path]::GetFullPath($release.FullName).TrimEnd('\')
        if (-not $candidate.StartsWith($releaseRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove a release outside the release root: $candidate"
        }
        $currentTarget = Get-StockCurrentReleaseTarget -PlatformRoot $PlatformRoot
        if ($currentTarget -and ($currentTarget.Equals($candidate, [StringComparison]::OrdinalIgnoreCase) -or $currentTarget.StartsWith($candidate + '\', [StringComparison]::OrdinalIgnoreCase))) {
            throw "Refusing to remove the active release target: $candidate"
        }
        try {
            Remove-Item -LiteralPath $candidate -Recurse -Force -ErrorAction Stop
            $removed += $release.Name
        } catch {
            # Retention is housekeeping, not activation. A locked old directory
            # must not turn a healthy deployment into a reported failure.
            Write-Warning "Release cleanup deferred for $($release.Name): $($_.Exception.Message)"
        }
    }
    return $removed
}

function Test-StockReleaseFileHashes {
    # Verifies every entry in a release's evidence/files.sha256 manifest
    # against the actual bytes on disk. Used before ever making a release
    # live (publish, right after the manifest is written) and before
    # switching to an already-published one (switch/rollback), so silent
    # corruption during the snapshot copy or a manually-edited release
    # cannot be activated undetected.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$AppPath,
        [Parameter(Mandatory)][string]$ManifestPath
    )
    if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
        throw "Missing integrity manifest: $ManifestPath"
    }
    $mismatched = @()
    $missing = @()
    $checked = 0
    foreach ($line in [IO.File]::ReadAllLines($ManifestPath, [Text.Encoding]::UTF8)) {
        if (-not $line) { continue }
        $parts = $line -split '  ', 2
        if ($parts.Count -ne 2) { continue }
        $expectedHash = $parts[0].Trim().ToLowerInvariant()
        $relative = $parts[1].Trim()
        $target = Join-Path $AppPath ($relative -replace '/', '\')
        if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
            $missing += $relative
            continue
        }
        $actualHash = (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actualHash -ne $expectedHash) { $mismatched += $relative }
        $checked++
    }
    if ($mismatched.Count -gt 0 -or $missing.Count -gt 0) {
        throw ("Integrity verification failed against {0}: {1} mismatched, {2} missing of {3} checked file(s)" -f `
            $ManifestPath, $mismatched.Count, $missing.Count, $checked)
    }
    return [pscustomobject]@{ manifest = $ManifestPath; checked_files = $checked }
}

function Test-StockReleaseIntegrity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$ReleaseId
    )
    $layout = Get-StockReleaseLayout -PlatformRoot $PlatformRoot
    $releaseRoot = Join-Path $layout.ReleasesRoot $ReleaseId
    return Test-StockReleaseFileHashes -AppPath (Join-Path $releaseRoot 'app') -ManifestPath (Join-Path $releaseRoot 'evidence\files.sha256')
}

# --- shared-peer tunnel reinstall gate -------------------------------------
#
# Measured 2026-09-18: publish-stock-release.ps1 and switch-stock-release.ps1
# unconditionally ran `Stop-ScheduledTask trading-hareness-shared-peer-tunnels`
# and then re-ran install-shared-tunnel-task.ps1 on every publish. That tears
# down the owner->peer reverse SSH tunnel for roughly 15 seconds, which resets
# every peer database connection riding through it (the PostgreSQL log recorded
# 21/23/5 client resets at 19:42/20:50/21:35 that evening) even when the
# publish did not change a single byte of tunnel code.
#
# The tunnel is an independent long-lived surface: the scheduled task's action
# points at G:\StockPlatform\current\scripts\windows\bin\stock-background-host.exe
# and the supervised ssh client is launched from whatever `current` resolved to
# when the supervisor started.  So the tunnel only needs to be restarted when
# the code it actually executes changes.  This gate answers exactly that
# question and nothing else.
#
# IMPORTANT consequence of a 'skip': the supervisor process, the background
# host executable and the ssh client keep running out of the PREVIOUS release
# directory until the next real restart (a code change, a tunnel fault, the
# 2-minute supervising trigger after a drop, or a reboot). That is safe here
# because release directories are immutable and retained (see
# Remove-ExpiredStockReleases: the active and previous releases are never
# pruned, and a running process additionally holds its own image open), and
# because the skip is only taken when the files that process would re-read are
# byte-identical in the new release. It is NOT a licence to delete a release
# directory by hand while its tunnel supervisor is still attached to it.
$script:StockTunnelAffectingFiles = @(
    # Executed by the supervised task on every launch.
    'scripts\shared-peer\start-shared-tunnels.ps1',
    # Defines the task action, triggers, settings and the health gate itself.
    'scripts\shared-peer\install-shared-tunnel-task.ps1',
    # Supervisor, runtime state/event writers and the ssh target resolution
    # used by the tunnel script.
    'scripts\windows\runtime-observability.psm1',
    'scripts\windows\background-process.psm1',
    # The registered task action IS scripts\windows\bin\stock-background-host.exe
    # (verified: install-shared-tunnel-task.ps1 calls
    # New-HiddenPowerShellTaskAction without -HostRoot, so the action's Execute
    # path is <runtime root>\scripts\windows\bin\stock-background-host.exe).
    # That executable is deliberately NOT hashed: it is not tracked in Git, it
    # is recompiled by build-background-task-host.ps1 on every publish, and
    # csc.exe embeds a fresh module GUID each time, so two builds of identical
    # sources differ byte for byte (measured 2026-09-19: e03e6cf1... then
    # 94ff63b4... from the same unchanged sources one second apart). Hashing it
    # would make the gate decide 'reinstall' on every single publish and the
    # tunnel would never actually be spared. Its three deterministic, tracked
    # inputs are hashed instead, which is what actually answers "did the host
    # this task launches change?"; the executable itself is presence-checked
    # below.
    'scripts\windows\background-task-host.cs',
    'scripts\windows\process-lifetime.cs',
    'scripts\windows\build-background-task-host.ps1'
)

# Presence-only: recorded as 'present'/'missing' rather than hashed. A release
# that does not carry the host executable at all must never be allowed to skip,
# because the next scheduled launch resolves through the `current` junction and
# would find nothing to run.
$script:StockTunnelPresenceOnlyFiles = @(
    'scripts\windows\bin\stock-background-host.exe'
)

function Get-StockTunnelAffectingFile {
    [CmdletBinding()]
    param([switch]$IncludePresenceOnly)
    if ($IncludePresenceOnly) {
        return @($script:StockTunnelAffectingFiles + $script:StockTunnelPresenceOnlyFiles)
    }
    return @($script:StockTunnelAffectingFiles)
}

function Get-StockTunnelPresenceOnlyFile {
    [CmdletBinding()]
    param()
    return @($script:StockTunnelPresenceOnlyFiles)
}

function Get-StockTunnelFileHash {
    # SHA-256 of every tunnel-affecting file in one release tree. A file that
    # does not exist is reported as 'missing' rather than omitted, so the pure
    # decision below can tell "absent in both trees" from "absent in the new
    # tree" without a second lookup.
    [CmdletBinding()]
    param([string]$RuntimeRoot = '')
    $hashes = @{}
    if (-not $RuntimeRoot) { return $hashes }
    foreach ($relative in $script:StockTunnelAffectingFiles) {
        $path = Join-Path $RuntimeRoot $relative
        if (Test-Path -LiteralPath $path -PathType Leaf) {
            $hashes[$relative] = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        } else {
            $hashes[$relative] = 'missing'
        }
    }
    foreach ($relative in $script:StockTunnelPresenceOnlyFiles) {
        $path = Join-Path $RuntimeRoot $relative
        $hashes[$relative] = if (Test-Path -LiteralPath $path -PathType Leaf) { 'present' } else { 'missing' }
    }
    return $hashes
}

function Get-StockTunnelReinstallDecision {
    # PURE: no file system, no scheduler, no network. Everything it judges is
    # passed in, so both release scripts and the contract test exercise the
    # same decision. Fails closed: 'skip' requires every condition to hold.
    [CmdletBinding()]
    param(
        [hashtable]$CurrentHashes = @{},
        [hashtable]$NewHashes = @{},
        [string]$TaskState = '',
        [string]$RuntimeStatus = '',
        [string]$RemoteHealthStatus = ''
    )
    if ($null -eq $CurrentHashes) { $CurrentHashes = @{} }
    if ($null -eq $NewHashes) { $NewHashes = @{} }
    $names = [Collections.Generic.SortedSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($key in $CurrentHashes.Keys) { [void]$names.Add([string]$key) }
    foreach ($key in $NewHashes.Keys) { [void]$names.Add([string]$key) }
    $changed = @()
    foreach ($name in $names) {
        $currentValue = if ($CurrentHashes.ContainsKey($name)) { [string]$CurrentHashes[$name] } else { 'missing' }
        $newValue = if ($NewHashes.ContainsKey($name)) { [string]$NewHashes[$name] } else { 'missing' }
        # A file missing from the new release is always a change: the tunnel
        # must never be left running against a tree that no longer carries the
        # code it is supposed to execute.
        if ($newValue -eq 'missing' -or -not $newValue.Equals($currentValue, [StringComparison]::OrdinalIgnoreCase)) {
            $changed += $name
        }
    }
    $reasons = @()
    # No hashes at all means no comparable current release (first publish, or
    # a caller that could not resolve the running tree): reinstall.
    if ($names.Count -eq 0) { $reasons += 'no_tunnel_files_hashed' }
    if ($changed.Count -gt 0) { $reasons += 'tunnel_files_changed' }
    if ($TaskState -ne 'Running') { $reasons += 'task_not_running' }
    if ($RuntimeStatus -ne 'healthy') { $reasons += 'runtime_state_not_healthy' }
    if ($RemoteHealthStatus -ne '200') { $reasons += 'remote_health_not_200' }
    $currentOrdered = [ordered]@{}
    $newOrdered = [ordered]@{}
    foreach ($name in $names) {
        $currentOrdered[$name] = if ($CurrentHashes.ContainsKey($name)) { [string]$CurrentHashes[$name] } else { 'missing' }
        $newOrdered[$name] = if ($NewHashes.ContainsKey($name)) { [string]$NewHashes[$name] } else { 'missing' }
    }
    return [pscustomobject]@{
        decision = if ($reasons.Count -eq 0) { 'skip' } else { 'reinstall' }
        reasons = $reasons
        changed_files = $changed
        task_state = $TaskState
        runtime_status = $RuntimeStatus
        remote_health_status = $RemoteHealthStatus
        current_hashes = $currentOrdered
        new_hashes = $newOrdered
    }
}

function Invoke-StockTunnelRemoteHealthProbe {
    # The same probe install-shared-tunnel-task.ps1 uses for its own post-
    # install gate, so a skip is held to exactly the standard a reinstall would
    # have had to meet. Returns the raw HTTP status text ('200' when healthy).
    [CmdletBinding()]
    param(
        [string]$SshAlias = 'lightServer1',
        [int]$RemoteApiPort = 15681,
        [int]$TimeoutSeconds = 12
    )
    try {
        $probe = Invoke-ConsoleFreeCommand -FilePath (Get-Command ssh.exe -ErrorAction Stop).Source -Arguments @(
            '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', $SshAlias,
            "curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$RemoteApiPort/health") -TimeoutSeconds $TimeoutSeconds
        return $probe.Stdout.Trim()
    } catch {
        return 'probe_failed'
    }
}

function Resolve-StockTunnelReinstallPlan {
    # Collects the four observations and hands them to the pure decision.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$NewRuntimeRoot,
        [string]$CurrentRuntimeRoot = '',
        [string]$TaskName = 'trading-hareness-shared-peer-tunnels',
        [string]$SshAlias = 'lightServer1',
        [int]$RemoteApiPort = 15681
    )
    $newHashes = Get-StockTunnelFileHash -RuntimeRoot $NewRuntimeRoot
    $currentHashes = Get-StockTunnelFileHash -RuntimeRoot $CurrentRuntimeRoot
    $taskState = 'not_registered'
    try {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
        $taskState = [string]$task.State
    } catch { $taskState = 'not_registered' }
    $runtimeStatus = 'missing'
    $statePath = Join-Path ([IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')) 'logs\runtime\shared-peer-tunnels.current.json'
    if (Test-Path -LiteralPath $statePath -PathType Leaf) {
        try {
            $state = Get-Content -LiteralPath $statePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $runtimeStatus = if ($state -and $state.PSObject.Properties['status']) { [string]$state.status } else { 'unknown' }
        } catch { $runtimeStatus = 'unreadable' }
    }
    # The SSH round trip is the only expensive observation, and it cannot
    # change a decision that is already 'reinstall'. Ask the pure function
    # first with the health condition satisfied; probe only if nothing else
    # already forces a reinstall.
    $withoutProbe = Get-StockTunnelReinstallDecision -CurrentHashes $currentHashes -NewHashes $newHashes `
        -TaskState $taskState -RuntimeStatus $runtimeStatus -RemoteHealthStatus '200'
    $health = 'not_probed'
    if ($withoutProbe.decision -eq 'skip') {
        $health = Invoke-StockTunnelRemoteHealthProbe -SshAlias $SshAlias -RemoteApiPort $RemoteApiPort
    }
    return Get-StockTunnelReinstallDecision -CurrentHashes $currentHashes -NewHashes $newHashes `
        -TaskState $taskState -RuntimeStatus $runtimeStatus -RemoteHealthStatus $health
}

function Write-StockTunnelReinstallSkipEvent {
    # Only a skip is recorded: a reinstall is the pre-existing behaviour and
    # already leaves its own install/healthy events behind.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][object]$Plan,
        [string]$Context = ''
    )
    if (-not $Plan -or [string]$Plan.decision -ne 'skip') { return }
    try {
        [void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels' `
            -Event 'tunnel_reinstall_skipped' -Data @{
                context = $Context
                task_state = [string]$Plan.task_state
                runtime_status = [string]$Plan.runtime_status
                remote_health_status = [string]$Plan.remote_health_status
                current_hashes = $Plan.current_hashes
                new_hashes = $Plan.new_hashes
                changed_files = @($Plan.changed_files)
            })
    } catch {
        # Observability must never fail a release.
        Write-Warning "Could not record tunnel_reinstall_skipped: $($_.Exception.Message)"
    }
}

Export-ModuleMember -Function @(
    'Get-StockReleaseLayout',
    'Get-StockReleaseState',
    'Set-StockReleaseState',
    'Get-StockReleaseAppPath',
    'Get-StockCurrentReleaseTarget',
    'Set-StockCurrentRelease',
    'Remove-ExpiredStockReleases',
    'Test-StockReleaseFileHashes',
    'Test-StockReleaseIntegrity',
    'Get-StockTunnelAffectingFile',
    'Get-StockTunnelPresenceOnlyFile',
    'Get-StockTunnelFileHash',
    'Get-StockTunnelReinstallDecision',
    'Invoke-StockTunnelRemoteHealthProbe',
    'Resolve-StockTunnelReinstallPlan',
    'Write-StockTunnelReinstallSkipEvent'
)
