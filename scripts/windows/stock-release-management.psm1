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
            tunnel_release = $null
            tunnel_ssh_target_sha256 = $null
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
    # tunnel_release / tunnel_ssh_target_sha256 describe the shared-peer tunnel's
    # own lifecycle, which is deliberately decoupled from the release lifecycle
    # by the reinstall gate below: the tunnel keeps running out of whatever
    # release it was last really installed from. Every other key in this payload
    # is reset to its default unless the caller supplies it, so these two are
    # seeded from the existing state instead -- a caller that is not changing the
    # tunnel must not be able to silently erase the pin that keeps that release
    # directory alive (which would let retention delete the tree the live tunnel
    # is executing from).
    $existing = Get-StockReleaseState -PlatformRoot $PlatformRoot
    $carried = @{}
    foreach ($key in 'tunnel_release', 'tunnel_ssh_target_sha256') {
        $carried[$key] = if ($existing.PSObject.Properties[$key]) { $existing.$key } else { $null }
    }
    $payload = [ordered]@{
        schema_version = 1
        active_release = $null
        previous_release = $null
        updated_at = [DateTimeOffset]::Now.ToString('o')
        last_verification = $null
        last_failed_release = $null
        tunnel_release = $carried['tunnel_release']
        tunnel_ssh_target_sha256 = $carried['tunnel_ssh_target_sha256']
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

function Get-StockReleaseRetentionPlan {
    # PURE: given the release directory names (newest first) and the three
    # pinned releases, decide what retention keeps and what it removes. Both
    # Remove-ExpiredStockReleases and the tunnel reinstall gate judge with this
    # one function, so the gate can never authorize a skip whose release
    # directory retention is about to delete.
    #
    # TunnelRelease is the release the shared-peer tunnel's supervisor,
    # background host and ssh client are ACTUALLY executing out of (see
    # release-state.json's tunnel_release). It is pinned unconditionally: after
    # a run of consecutive skipped publishes it is neither the active nor the
    # previous release, and without this pin the {active, previous} + fill-to-N
    # policy deletes the live tunnel's own tree.
    [CmdletBinding()]
    param(
        [string[]]$ReleaseNames = @(),
        [string]$ActiveRelease = '',
        [string]$PreviousRelease = '',
        [string]$TunnelRelease = '',
        [int]$RetainCount = 3
    )
    $keep = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($pinned in $ActiveRelease, $PreviousRelease, $TunnelRelease) {
        if ($pinned) { [void]$keep.Add([string]$pinned) }
    }
    foreach ($name in @($ReleaseNames)) {
        if (-not $name) { continue }
        if ($keep.Count -ge [Math]::Max(2, $RetainCount)) { break }
        [void]$keep.Add([string]$name)
    }
    $remove = @(foreach ($name in @($ReleaseNames)) {
        if ($name -and -not $keep.Contains([string]$name)) { $name }
    })
    return [pscustomobject]@{
        Keep = @($keep)
        Remove = $remove
    }
}

function Get-StockReleaseRetentionState {
    # The I/O half of the retention decision: which release directories exist
    # (newest first, excluding staging and "<id>.failed" leftovers) and which
    # releases release-state.json pins.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [int]$RetainCount = 3
    )
    $layout = Get-StockReleaseLayout -PlatformRoot $PlatformRoot
    $state = Get-StockReleaseState -PlatformRoot $PlatformRoot
    $names = @()
    if (Test-Path -LiteralPath $layout.ReleasesRoot -PathType Container) {
        # Releases that never activated successfully are renamed to "<id>.failed"
        # by the publish script's rollback path; exclude them here so the
        # retention policy only ever considers releases that were actually
        # activated, not a crash-looping publish's leftovers.
        $names = @(Get-ChildItem -LiteralPath $layout.ReleasesRoot -Directory -Force |
            Where-Object { $_.Name -notlike '.staging-*' -and $_.Name -notlike '*.failed' } |
            Sort-Object LastWriteTimeUtc -Descending |
            ForEach-Object { $_.Name })
    }
    $read = {
        param($Name)
        if ($state.PSObject.Properties[$Name]) { [string]$state.$Name } else { '' }
    }
    return Get-StockReleaseRetentionPlan -ReleaseNames $names `
        -ActiveRelease (& $read 'active_release') `
        -PreviousRelease (& $read 'previous_release') `
        -TunnelRelease (& $read 'tunnel_release') `
        -RetainCount $RetainCount
}

function Remove-ExpiredStockReleases {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [int]$RetainCount = 3
    )
    $layout = Get-StockReleaseLayout -PlatformRoot $PlatformRoot
    if (-not (Test-Path -LiteralPath $layout.ReleasesRoot -PathType Container)) { return @() }
    $plan = Get-StockReleaseRetentionState -PlatformRoot $PlatformRoot -RetainCount $RetainCount
    $keep = [Collections.Generic.HashSet[string]]::new([string[]]@($plan.Keep), [StringComparer]::OrdinalIgnoreCase)
    $releases = @(Get-ChildItem -LiteralPath $layout.ReleasesRoot -Directory -Force |
        Where-Object { $_.Name -notlike '.staging-*' -and $_.Name -notlike '*.failed' } |
        Sort-Object LastWriteTimeUtc -Descending)
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
# 2-minute supervising trigger after a drop, or a reboot). That release is
# therefore pinned, not merely assumed to survive: every real reinstall records
# it as `tunnel_release` in release-state.json, Get-StockReleaseRetentionPlan
# keeps that release unconditionally, and the gate below refuses to skip when
# tunnel_release is unknown, no longer on disk, or not in the retained set. A
# skip can never outlive the directory it is running from. It is still not a
# licence to delete a release directory by hand while its tunnel supervisor is
# attached to it.
#
# The file list is the transitive closure of the tunnel's execution chain, and
# Get-StockTunnelExecutionChainFile below re-derives it by parsing that chain
# (test-stock-release-safety.ps1 asserts the two are equal, so a new
# Import-Module/Add-Type anywhere on the chain fails the test instead of
# silently leaving the gate under-detecting):
#
#   scheduled task action (stock-background-host.exe, registered by
#   install-shared-tunnel-task.ps1 via New-HiddenPowerShellTaskAction in
#   background-process.psm1)
#     -> the executable's build inputs (build-background-task-host.ps1 ->
#        process-lifetime.cs, background-task-host.cs)
#     -> start-shared-tunnels.ps1 (the script the host launches)
#        -> Import-Module runtime-observability.psm1, background-process.psm1
#        -> Start-RuntimeSupervisor (runtime-observability.psm1)
#           -> scripts\windows\supervise-runtime-process.ps1, which owns the
#              cross-process lock, the child-process lifetime job, the output
#              pumps and every state/exit transition of the tunnel
#              -> Import-Module runtime-observability.psm1,
#                 background-process.psm1; Add-Type process-lifetime.cs
$script:StockTunnelAffectingFiles = @(
    # Executed by the supervised task on every launch.
    'scripts\shared-peer\start-shared-tunnels.ps1',
    # Defines the task action, triggers, settings and the health gate itself.
    'scripts\shared-peer\install-shared-tunnel-task.ps1',
    # Supervisor, runtime state/event writers and the ssh target resolution
    # used by the tunnel script.
    'scripts\windows\runtime-observability.psm1',
    'scripts\windows\background-process.psm1',
    # The process the task actually runs: pwsh -File supervise-runtime-process.ps1.
    'scripts\windows\supervise-runtime-process.ps1',
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
    # tunnel would never actually be spared. Its deterministic, tracked inputs
    # are hashed instead, which is what actually answers "did the host this task
    # launches change?"; the executable itself is presence-checked below.
    'scripts\windows\background-task-host.cs',
    'scripts\windows\process-lifetime.cs',
    'scripts\windows\build-background-task-host.ps1'
)

# Where Get-StockTunnelExecutionChainFile starts walking. These two are the
# chain's roots and cannot be discovered from inside it: the installer defines
# the task action, and the build script defines what the action's executable is
# compiled from.
$script:StockTunnelChainEntryPoints = @(
    'scripts\shared-peer\install-shared-tunnel-task.ps1',
    'scripts\windows\build-background-task-host.ps1'
)

# Synthetic hash-set entry for the tunnel's SSH identity. The release tree says
# nothing about it: start-shared-tunnels.ps1 resolves user/key/host/port through
# Resolve-OwnerTunnelSshTarget against <PlatformRoot>\config\runtime.env, which
# is shared by all releases. Rotating the owner tunnel key (see
# scripts/shared-peer/install-owner-tunnel-key.sh) changes the connection the
# running ssh client would have to make without changing a single release byte,
# so the resolved target is hashed and carried in release-state.json
# (tunnel_ssh_target_sha256) alongside tunnel_release. Only the SHA-256 is ever
# stored or logged; the resolved values themselves are not.
$script:StockTunnelSshTargetKey = 'config:owner_tunnel_ssh_target'

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

function Get-StockTunnelExecutionChainFile {
    # Re-derives the tunnel-affecting file list by parsing the execution chain
    # in a release tree, instead of trusting a hand-maintained list. Starting
    # from $script:StockTunnelChainEntryPoints it takes the transitive closure
    # of every script/module/source-file path literal in each file's AST:
    #
    #   * a literal containing a directory separator is release-root relative
    #     ('scripts\windows\supervise-runtime-process.ps1' in
    #     runtime-observability.psm1's Start-RuntimeSupervisor);
    #   * a bare file name is resolved next to the file that mentions it
    #     (Join-Path $PSScriptRoot 'process-lifetime.cs');
    #   * a .ps1/.psm1/.cs candidate only counts when it actually exists, which
    #     drops absolute references to things outside the tree (csc.exe) and
    #     build-script output names that are not inputs;
    #   * an .exe under scripts\windows\bin is the non-deterministic build
    #     artifact and is classified presence-only even when it is absent (it is
    #     gitignored, so a plain checkout does not carry it).
    #
    # Returns Hashed/PresenceOnly/All so a test can assert equality with
    # $script:StockTunnelAffectingFiles.
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$RuntimeRoot)
    $root = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $binPrefix = 'scripts\windows\bin\'
    $hashed = [Collections.Generic.SortedSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $presence = [Collections.Generic.SortedSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $queue = [Collections.Generic.Queue[string]]::new()
    foreach ($seed in $script:StockTunnelChainEntryPoints) {
        if ($seen.Add($seed)) { $queue.Enqueue($seed) }
    }
    while ($queue.Count -gt 0) {
        $relative = $queue.Dequeue()
        [void]$hashed.Add($relative)
        $full = Join-Path $root $relative
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
            throw "Tunnel execution chain references a missing file: $relative"
        }
        if ([IO.Path]::GetExtension($relative) -notin @('.ps1', '.psm1')) { continue }
        $parseErrors = $null
        $tokens = $null
        $ast = [Management.Automation.Language.Parser]::ParseFile($full, [ref]$tokens, [ref]$parseErrors)
        if ($parseErrors -and @($parseErrors).Count -gt 0) {
            throw "Could not parse tunnel execution chain file $relative`: $(@($parseErrors)[0].Message)"
        }
        $literals = @($ast.FindAll({
            param($node) $node -is [Management.Automation.Language.StringConstantExpressionAst]
        }, $true))
        $parent = Split-Path -Parent $relative
        foreach ($literal in $literals) {
            $value = [string]$literal.Value
            if ($value -notmatch '\.(ps1|psm1|cs|exe)$') { continue }
            if ($value -match '[*?"<>|]') { continue }
            if ($value -match '^[A-Za-z]:' -or $value.StartsWith('\\')) { continue }
            $candidate = if ($value.Contains('\')) { $value.TrimStart('\') } else { Join-Path $parent $value }
            if ($candidate.StartsWith($binPrefix, [StringComparison]::OrdinalIgnoreCase) -and
                $candidate.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) {
                [void]$presence.Add($candidate)
                continue
            }
            if ($candidate.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) { continue }
            if (-not (Test-Path -LiteralPath (Join-Path $root $candidate) -PathType Leaf)) { continue }
            if ($seen.Add($candidate)) { $queue.Enqueue($candidate) }
        }
    }
    return [pscustomobject]@{
        Hashed = @($hashed)
        PresenceOnly = @($presence)
        All = @(@($hashed) + @($presence))
    }
}

function Get-StockTunnelTaskActionPlacement {
    # PURE. 'yes' only when the registered action's executable AND every .ps1 it
    # is asked to run resolve under <PlatformRoot>\current\. The manual recovery
    # step documented in docs/SHARED_PEER_RUNTIME.md
    # (`pwsh .\scripts\shared-peer\install-shared-tunnel-task.ps1`) run from the
    # F: development checkout registers Execute and the script path under that
    # checkout instead; publishing must then never be allowed to skip, because
    # skipping is precisely what stops it from being healed.
    # The arguments legitimately also name pwsh.exe itself, which lives outside
    # the release, so only the .ps1 tokens are judged.
    [CmdletBinding()]
    param(
        [string]$Execute = '',
        [string]$Arguments = '',
        [Parameter(Mandatory)][string]$ExpectedPrefix
    )
    $prefix = $ExpectedPrefix
    if (-not $prefix.EndsWith('\')) { $prefix += '\' }
    if (-not $Execute) { return 'unknown' }
    if (-not ([string]$Execute).Trim('"').StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return 'no' }
    $scripts = @([regex]::Matches([string]$Arguments, '"([^"]*)"') |
        ForEach-Object { $_.Groups[1].Value } |
        Where-Object { $_ -match '\.ps1$' })
    if ($scripts.Count -eq 0) { return 'no' }
    foreach ($scriptPath in $scripts) {
        if (-not $scriptPath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { return 'no' }
    }
    return 'yes'
}

function Get-StockTunnelSshTargetHash {
    # SHA-256 over the resolved owner-tunnel SSH target (mode + destination +
    # connection arguments). Returns 'unresolved' when it cannot be computed,
    # which the decision treats as a change, never as a match.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [string]$FallbackAlias = 'lightServer1'
    )
    try {
        $runtimeEnv = Join-Path ([IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')) 'config\runtime.env'
        $target = Resolve-OwnerTunnelSshTarget -RuntimeEnv $runtimeEnv -FallbackAlias $FallbackAlias -WarningAction SilentlyContinue
        $material = @([string]$target.Mode, [string]$target.Destination) +
            @(@($target.ConnectionArguments) | ForEach-Object { [string]$_ })
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes(($material -join "`n")))
            return ([BitConverter]::ToString($digest) -replace '-', '').ToLowerInvariant()
        } finally { $sha.Dispose() }
    } catch {
        return 'unresolved'
    }
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
        [string]$RemoteHealthStatus = '',
        # 'retained' only when release-state.json's tunnel_release names a
        # release that is still on disk and still in the retention keep set.
        [string]$TunnelReleaseState = '',
        # 'yes' only when the registered task action runs out of
        # <PlatformRoot>\current\ (see Get-StockTunnelTaskActionPlacement).
        [string]$TaskActionUnderCurrent = ''
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
        # code it is supposed to execute. 'unresolved' is the same for the
        # synthetic SSH-target entry: an identity we could not read is never
        # evidence that the identity is unchanged.
        if ($newValue -eq 'missing' -or $newValue -eq 'unresolved' -or
            -not $newValue.Equals($currentValue, [StringComparison]::OrdinalIgnoreCase)) {
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
    # A skip must never outlive the release directory the tunnel is executing
    # out of, and must never leave a task pinned to a developer checkout
    # un-healed.
    switch ($TunnelReleaseState) {
        'retained' { }
        'missing' { $reasons += 'tunnel_release_missing' }
        'not_retained' { $reasons += 'tunnel_release_not_retained' }
        default { $reasons += 'tunnel_release_unknown' }
    }
    if ($TaskActionUnderCurrent -ne 'yes') { $reasons += 'task_action_not_under_current' }
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
        tunnel_release_state = $TunnelReleaseState
        task_action_under_current = $TaskActionUnderCurrent
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
    # Collects the observations and hands them to the pure decision.
    #
    # The comparison tree is the release the tunnel is REALLY running from
    # (release-state.json's tunnel_release), not `current`. Those differ by
    # construction as soon as one publish skips, and comparing against `current`
    # is only equivalent while the file list never changes: R1->R2 having been
    # judged identical under yesterday's list says nothing about R1 == R2 under
    # a list that has since gained a file.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$NewRuntimeRoot,
        # Both default to release-state.json, so a read-only diagnostic run
        # needs nothing but the platform root.
        [string]$TunnelReleaseId = '',
        [string]$TunnelSshTargetSha256 = '',
        # Diagnostics only: hash against this tree when tunnel_release is
        # unknown. It cannot produce a 'skip' on its own -- an unknown
        # tunnel_release is itself a reinstall reason.
        [string]$CurrentRuntimeRoot = '',
        [int]$RetainCount = 3,
        [string]$TaskName = 'trading-hareness-shared-peer-tunnels',
        [string]$SshAlias = 'lightServer1',
        [int]$RemoteApiPort = 15681,
        # Test seams. Both default to the real scheduler / real SSH probe; the
        # contract test injects them so it can drive this function (not just the
        # pure decision) against a temporary platform root without a scheduled
        # task or a network. ScheduledTaskProvider receives the task name and
        # returns $null or an object with State/Execute/Arguments;
        # RemoteHealthProbe returns the HTTP status text.
        [scriptblock]$ScheduledTaskProvider = $null,
        [scriptblock]$RemoteHealthProbe = $null
    )
    $platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
    $state = Get-StockReleaseState -PlatformRoot $platform
    if (-not $TunnelReleaseId -and $state.PSObject.Properties['tunnel_release']) {
        $TunnelReleaseId = [string]$state.tunnel_release
    }
    if (-not $TunnelSshTargetSha256 -and $state.PSObject.Properties['tunnel_ssh_target_sha256']) {
        $TunnelSshTargetSha256 = [string]$state.tunnel_ssh_target_sha256
    }
    $tunnelReleaseState = 'unknown'
    $tunnelRuntimeRoot = $CurrentRuntimeRoot
    if ($TunnelReleaseId) {
        $tunnelAppPath = $null
        try { $tunnelAppPath = Get-StockReleaseAppPath -PlatformRoot $platform -ReleaseId $TunnelReleaseId }
        catch { $tunnelAppPath = $null }
        if (-not $tunnelAppPath -or -not (Test-Path -LiteralPath $tunnelAppPath -PathType Container)) {
            $tunnelReleaseState = 'missing'
            $tunnelRuntimeRoot = ''
        } else {
            $retention = Get-StockReleaseRetentionState -PlatformRoot $platform -RetainCount $RetainCount
            $retained = @($retention.Keep) | Where-Object { $_ -and ([string]$_).Equals($TunnelReleaseId, [StringComparison]::OrdinalIgnoreCase) }
            if (@($retained).Count -eq 0) {
                $tunnelReleaseState = 'not_retained'
                $tunnelRuntimeRoot = ''
            } else {
                $tunnelReleaseState = 'retained'
                $tunnelRuntimeRoot = $tunnelAppPath
            }
        }
    }
    $newHashes = Get-StockTunnelFileHash -RuntimeRoot $NewRuntimeRoot
    $currentHashes = Get-StockTunnelFileHash -RuntimeRoot $tunnelRuntimeRoot
    # The SSH identity is not in either tree; it is resolved now and compared
    # against the value recorded at the last real reinstall.
    $newHashes[$script:StockTunnelSshTargetKey] = Get-StockTunnelSshTargetHash -PlatformRoot $platform -FallbackAlias $SshAlias
    if ($currentHashes.Count -gt 0) {
        $currentHashes[$script:StockTunnelSshTargetKey] = if ($TunnelSshTargetSha256) { $TunnelSshTargetSha256 } else { 'missing' }
    }
    $taskState = 'not_registered'
    $taskActionUnderCurrent = 'unknown'
    try {
        $observed = if ($ScheduledTaskProvider) { & $ScheduledTaskProvider $TaskName } else {
            $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
            $action = @($task.Actions)[0]
            [pscustomobject]@{
                State = [string]$task.State
                Execute = if ($action) { [string]$action.Execute } else { '' }
                Arguments = if ($action) { [string]$action.Arguments } else { '' }
            }
        }
        if ($observed) {
            $taskState = [string]$observed.State
            $taskActionUnderCurrent = Get-StockTunnelTaskActionPlacement -Execute ([string]$observed.Execute) `
                -Arguments ([string]$observed.Arguments) -ExpectedPrefix (Join-Path $platform 'current')
        }
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
    $observations = @{
        CurrentHashes = $currentHashes
        NewHashes = $newHashes
        TaskState = $taskState
        RuntimeStatus = $runtimeStatus
        TunnelReleaseState = $tunnelReleaseState
        TaskActionUnderCurrent = $taskActionUnderCurrent
    }
    $withoutProbe = Get-StockTunnelReinstallDecision @observations -RemoteHealthStatus '200'
    $health = 'not_probed'
    if ($withoutProbe.decision -eq 'skip') {
        $health = if ($RemoteHealthProbe) { [string](& $RemoteHealthProbe) }
                  else { Invoke-StockTunnelRemoteHealthProbe -SshAlias $SshAlias -RemoteApiPort $RemoteApiPort }
    }
    $plan = Get-StockTunnelReinstallDecision @observations -RemoteHealthStatus $health
    return [pscustomobject]@{
        decision = $plan.decision
        reasons = $plan.reasons
        changed_files = $plan.changed_files
        task_state = $plan.task_state
        runtime_status = $plan.runtime_status
        remote_health_status = $plan.remote_health_status
        tunnel_release_state = $plan.tunnel_release_state
        task_action_under_current = $plan.task_action_under_current
        tunnel_release = $TunnelReleaseId
        tunnel_runtime_root = $tunnelRuntimeRoot
        current_hashes = $plan.current_hashes
        new_hashes = $plan.new_hashes
    }
}

function Write-StockTunnelReinstallSkipEvent {
    # Only a skip is recorded: a reinstall is the pre-existing behaviour and
    # already leaves its own install/healthy events behind.
    #
    # Callers must invoke this only AFTER activation and post-switch
    # verification have succeeded. The documented acceptance check is "grep
    # lifecycle-<date>.jsonl for tunnel_reinstall_skipped"; writing it before
    # the switch would leave a skip receipt behind for a publish that then
    # rolled back and unconditionally reinstalled the tunnel, i.e. a receipt
    # the log itself contradicts.
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
                tunnel_release = if ($Plan.PSObject.Properties['tunnel_release']) { [string]$Plan.tunnel_release } else { '' }
                tunnel_release_state = [string]$Plan.tunnel_release_state
                task_action_under_current = [string]$Plan.task_action_under_current
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
    'Get-StockReleaseRetentionPlan',
    'Get-StockReleaseRetentionState',
    'Remove-ExpiredStockReleases',
    'Test-StockReleaseFileHashes',
    'Test-StockReleaseIntegrity',
    'Get-StockTunnelAffectingFile',
    'Get-StockTunnelPresenceOnlyFile',
    'Get-StockTunnelExecutionChainFile',
    'Get-StockTunnelTaskActionPlacement',
    'Get-StockTunnelSshTargetHash',
    'Get-StockTunnelFileHash',
    'Get-StockTunnelReinstallDecision',
    'Invoke-StockTunnelRemoteHealthProbe',
    'Resolve-StockTunnelReinstallPlan',
    'Write-StockTunnelReinstallSkipEvent'
)
