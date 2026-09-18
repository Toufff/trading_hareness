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
    # activated_at is the moment the `current` junction was actually moved to
    # active_release: publish and switch record it immediately after
    # Set-StockCurrentRelease returns. It cannot be derived from the release id,
    # whose yyyyMMddTHHmmss prefix is stamped before the test suite runs -- i.e.
    # minutes to tens of minutes before activation -- and the tunnel pin refresh
    # below needs the real instant, so it is carried like the tunnel keys.
    #
    # tunnel_release / tunnel_ssh_target_sha256 describe the shared-peer tunnel's
    # own lifecycle, which is deliberately decoupled from the release lifecycle
    # by the reinstall gate below: tunnel_release is the release the tunnel was
    # LAST INSTALLED FROM and is therefore still executing out of, while the
    # registered task action points at <PlatformRoot>\current, so the next
    # relaunch starts it from whatever `current` resolves to then. Every other key in this payload
    # is reset to its default unless the caller supplies it, so these two are
    # seeded from the existing state instead -- a caller that is not changing the
    # tunnel must not be able to silently erase the pin that keeps that release
    # directory alive (which would let retention delete the tree the live tunnel
    # is executing from). activated_at is carried for the same reason: a state
    # write that does not move the junction must not erase when it last moved.
    $existing = Get-StockReleaseState -PlatformRoot $PlatformRoot
    $carried = @{}
    foreach ($key in 'tunnel_release', 'tunnel_ssh_target_sha256', 'activated_at') {
        $carried[$key] = if ($existing.PSObject.Properties[$key]) { $existing.$key } else { $null }
    }
    $payload = [ordered]@{
        schema_version = 1
        active_release = $null
        previous_release = $null
        activated_at = $carried['activated_at']
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

function Get-StockTextSha256 {
    # SHA-256 of a UTF-8 string, lower-case hex. Used for the synthetic
    # hash-set entries that describe things which are NOT release files (the
    # owner-tunnel SSH target, the scheduled task's PowerShell host, the task
    # principal), so they can ride through the same comparison as real files.
    [CmdletBinding()]
    param([Parameter(Mandatory)][AllowEmptyString()][string]$Text)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))
        return ([BitConverter]::ToString($digest) -replace '-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Get-StockBytesSha256 {
    [CmdletBinding()]
    param([Parameter(Mandatory)][byte[]]$Bytes)
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha.ComputeHash($Bytes)) -replace '-', '').ToLowerInvariant()
    } finally { $sha.Dispose() }
}

function Get-StockScheduledTaskLogonType {
    # The logon type a (re)install would register for the background tasks.
    # S4U needs an elevated session (Register-ScheduledTask otherwise fails with
    # access denied); Interactive works unelevated but only while the operator
    # is logged on. publish-stock-release.ps1, switch-stock-release.ps1 and the
    # tunnel reinstall gate must all answer this the same way, or the gate would
    # compare the registered principal against a value no caller would register.
    [CmdletBinding()]
    param()
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    if (([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        return 'S4U'
    }
    return 'Interactive'
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
    # background host and ssh client are currently executing out of, i.e. the
    # one it was last really installed from (see release-state.json's
    # tunnel_release). It is pinned unconditionally: after a run of consecutive
    # skipped publishes it is neither the active nor the previous release, and
    # without this pin the {active, previous} + fill-to-N policy deletes the
    # live tunnel's own tree.
    #
    # A pinned name that does not correspond to an existing release directory is
    # ignored rather than kept: a phantom pin (a release already pruned by hand,
    # or a stale id in release-state.json) would otherwise consume one of the
    # Max(2, RetainCount) slots and silently evict one more real release than
    # the retention policy intends.
    [CmdletBinding()]
    param(
        [string[]]$ReleaseNames = @(),
        [string]$ActiveRelease = '',
        [string]$PreviousRelease = '',
        [string]$TunnelRelease = '',
        [int]$RetainCount = 3
    )
    $keep = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $known = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($name in @($ReleaseNames)) {
        if ($name) { [void]$known.Add([string]$name) }
    }
    foreach ($pinned in $ActiveRelease, $PreviousRelease, $TunnelRelease) {
        if ($pinned -and $known.Contains([string]$pinned)) { [void]$keep.Add([string]$pinned) }
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

function Get-StockReleaseDirectoryName {
    # The release directories retention is allowed to consider, newest first.
    # Releases that never activated successfully are renamed to "<id>.failed"
    # by the publish script's rollback path; they are excluded here so the
    # retention policy only ever considers releases that were actually
    # activated, not a crash-looping publish's leftovers.
    [CmdletBinding()]
    param([Parameter(Mandatory)][string]$PlatformRoot)
    $layout = Get-StockReleaseLayout -PlatformRoot $PlatformRoot
    if (-not (Test-Path -LiteralPath $layout.ReleasesRoot -PathType Container)) { return @() }
    return @(Get-ChildItem -LiteralPath $layout.ReleasesRoot -Directory -Force |
        Where-Object { $_.Name -notlike '.staging-*' -and $_.Name -notlike '*.failed' } |
        Sort-Object LastWriteTimeUtc -Descending |
        ForEach-Object { $_.Name })
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
    $state = Get-StockReleaseState -PlatformRoot $PlatformRoot
    $names = @(Get-StockReleaseDirectoryName -PlatformRoot $PlatformRoot)
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
# host executable and the ssh client keep running out of the release they were
# LAST INSTALLED FROM (release-state.json's `tunnel_release`) until the next
# real restart. But the registered task action is
# <PlatformRoot>\current\scripts\windows\bin\stock-background-host.exe, so that
# restart -- a tunnel fault plus the 2-minute supervising trigger, a logon, a
# reboot -- RELAUNCHES FROM `current`, i.e. from the active release, without
# writing anything to release-state.json. `tunnel_release` is therefore "last
# installed from", never "will run from next".
#
# Two consequences are handled below:
#   * a skip requires the new tree to be byte-identical to BOTH the pinned tree
#     (what the live process came from) AND the `current` junction's tree (what
#     the next relaunch will come from). Comparing only the pin would let a
#     relaunch silently adopt code the gate never compared;
#   * when the tunnel's supervised run started after the `current` junction was
#     moved (release-state.json's `activated_at`, written by publish/switch the
#     moment Set-StockCurrentRelease returns), the running process started from
#     `current`, so Get-StockTunnelPinRefresh moves the pin forward to the
#     active release. Judged against the release id's own timestamp this would
#     not follow: that stamp is taken before the test suite runs, and `current`
#     still resolves to the PREVIOUS release for the whole window between the
#     two, so a run that started there came from the pinned tree after all. With
#     only the approximate instant available the refresh still happens but is
#     marked uncertain, and an uncertain pin cannot buy a skip.
#     No stale pin has been observed in production yet: the case the refresh
#     exists for is a tunnel that relaunched (or was reinstalled by hand with
#     scripts\shared-peer\install-shared-tunnel-task.ps1) between two publishes,
#     or a rollback whose Set-StockReleaseState never landed.
#
# The pinned release is not merely assumed to survive: every real reinstall
# records it as `tunnel_release`, Get-StockReleaseRetentionPlan keeps it
# unconditionally, and the gate below independently re-derives the retained set
# from the release directories plus the {active, previous} pins ONLY and
# refuses to skip when tunnel_release is unknown, no longer on disk, or outside
# that set. That independent check also bounds how far the live tunnel may drift
# behind `current`. It is still not a licence to delete a release directory by
# hand while its tunnel supervisor is attached to it.
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

# Synthetic hash-set entry for the PowerShell host baked into the registered
# task action. Get-InstalledPowerShell (background-process.psm1) falls back to
# the Appx package when no MSI PowerShell is installed, which bakes a
# version-pinned
# C:\Program Files\WindowsApps\Microsoft.PowerShell_<version>_x64__<id>\pwsh.exe
# into the action; the Store removes that directory when it updates PowerShell,
# and only a reinstall re-resolves it. Hashing the path this publish WOULD
# register against the one the task actually carries forces that reinstall.
$script:StockTunnelTaskHostShellKey = 'config:task_host_pwsh'

# Synthetic hash-set entry for the task principal. install-shared-tunnel-task.ps1
# registers New-ScheduledTaskPrincipal -UserId <user> -LogonType <S4U|Interactive>,
# chosen at publish time from the session's elevation; a skip never re-registers
# it, so the gate compares the principal this publish would register against the
# one the registered task carries.
$script:StockTunnelTaskPrincipalKey = 'config:tunnel_task_principal'

# Keys in the hash set that do not come from a release tree. They are compared
# against the values recorded at the last real reinstall, never against the
# `current` junction's tree (which says nothing about them).
$script:StockTunnelSyntheticKeyPrefix = 'config:'

# Presence-only: the background host executable is not hashed (csc.exe embeds a
# fresh module GUID, so two builds of identical sources differ byte for byte).
# It is recorded through build-background-task-host.ps1's build receipt instead
# -- see Get-StockTunnelHostBuildEvidence -- which is deterministic for
# deterministic sources and, unlike bare presence, cannot be satisfied by a
# truncated or stale copy. A release that does not carry the host executable at
# all must never be allowed to skip, because the next scheduled launch resolves
# through the `current` junction and would find nothing to run.
$script:StockTunnelPresenceOnlyFiles = @(
    'scripts\windows\bin\stock-background-host.exe'
)

# Values that are never evidence of an unchanged input, even when both trees
# report the same one.
$script:StockTunnelNeverEqualValues = @('missing', 'unresolved', 'receipt_mismatch')

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

function Resolve-StockTunnelChainExpandableString {
    # PURE. An expandable string such as "$PSScriptRoot\shared-tunnel-profiles.psm1"
    # or "$repository\scripts\shared-peer\$name.ps1" is part of the execution
    # chain exactly as much as a plain literal, but it is not a
    # StringConstantExpressionAst, so a walk that only visits constants would
    # never see it -- the parsed chain would still equal the declared list,
    # test-stock-release-safety.ps1 would pass, and the gate would silently
    # under-detect a change to that file. That is precisely the failure this
    # parser exists to prevent.
    #
    # Only an explicitly enumerated set of root variables can be resolved
    # statically: $PSScriptRoot (the directory of the file that mentions it) and
    # the repository-root names used on this chain. Anything else -- a loop
    # variable, an environment variable, a computed name -- is an unresolvable
    # chain element, and this throws so the omission is loud rather than silent.
    # Returns $null for an expandable string that does not name a script, module
    # or C# source at all -- including a MESSAGE that merely ends in one
    # ("Rerun $name.ps1 by hand"), which must not reach the unresolvable-variable
    # throw below. A path on this chain is a single whitespace-free token.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][AllowEmptyString()][string]$Text,
        [Parameter(Mandatory)][AllowEmptyString()][string]$Relative
    )
    if ($Text -notmatch '^\S+\.(ps1|psm1|cs|exe)$') { return $null }
    $parent = Split-Path -Parent $Relative
    $resolved = $Text
    foreach ($reference in [regex]::Matches($Text, '\$\{?([A-Za-z_][A-Za-z0-9_:]*)\}?')) {
        $name = $reference.Groups[1].Value
        $replacement = $null
        if ($name -ieq 'PSScriptRoot') { $replacement = $parent }
        elseif ($name -imatch '^(repository|repositoryRoot|root|runtimeRoot|releaseRoot|source|sourceRoot)$') { $replacement = '' }
        if ($null -eq $replacement) {
            throw ("Tunnel execution chain file '$Relative' references '$Text', whose variable `$$name cannot be " +
                'resolved statically. Use a plain string literal for the path, or teach ' +
                'Resolve-StockTunnelChainExpandableString about that variable, so the reinstall gate can see the file.')
        }
        $resolved = $resolved.Replace($reference.Value, $replacement)
    }
    return $resolved.TrimStart('\')
}

function Get-StockTunnelExecutionChainFile {
    # Re-derives the tunnel-affecting file list by parsing the execution chain
    # in a release tree, instead of trusting a hand-maintained list. Starting
    # from $script:StockTunnelChainEntryPoints it takes the transitive closure
    # of every script/module/source-file path literal in each file's AST:
    #
    #   * only a literal whose WHOLE value is a path is considered at all: an
    #     operator message that merely ends in one ('Run scripts\windows\tests\
    #     test-shared-tunnel-recovery.ps1') names a file the parsed tree need
    #     not carry, and would otherwise trip the loud rule below into a hard
    #     error. Paths on this chain never contain whitespace;
    #   * '/' and '\' are the same separator to PowerShell, so a value is
    #     normalized to '\' before anything keys on one. Keyed on '\' alone, a
    #     forward-slash literal got neither the root-relative reading nor the
    #     loud rule, i.e. exactly the silent under-detection this parser exists
    #     to end;
    #   * a literal containing a directory separator is tried release-root
    #     relative first ('scripts\windows\supervise-runtime-process.ps1' in
    #     runtime-observability.psm1's Start-RuntimeSupervisor) and then
    #     relative to the directory of the file that mentions it
    #     (Join-Path $PSScriptRoot 'sub\helper.psm1' in a script under
    #     scripts\shared-peer). Resolving only against the root silently dropped
    #     the second form, which is a real chain element the gate would then
    #     never hash, so a separator-carrying literal that resolves NOWHERE now
    #     throws instead of disappearing;
    #   * every candidate is collapsed back to a release-root-relative spelling
    #     before it is used as a key, so a parent-relative literal
    #     (Join-Path $PSScriptRoot '..\windows\x.psm1') enters the chain as
    #     scripts\windows\x.psm1 rather than as a second, '..'-carrying spelling
    #     of a file the declared list already names. A candidate that collapses
    #     to somewhere OUTSIDE the release root is dropped quietly, like an
    #     absolute path: the gate cannot hash what the release does not carry;
    #   * a bare file name is resolved next to the file that mentions it
    #     (Join-Path $PSScriptRoot 'process-lifetime.cs'), and is dropped
    #     quietly when it resolves nowhere: with no separator it is as likely to
    #     be a message, a build-script output name or a doc reference as a path;
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
    param(
        [Parameter(Mandatory)][string]$RuntimeRoot,
        # Test seam: the chain's roots. Defaults to the real entry points.
        [string[]]$EntryPoint = @()
    )
    $root = [IO.Path]::GetFullPath($RuntimeRoot).TrimEnd('\')
    $binPrefix = 'scripts\windows\bin\'
    # Collapses a candidate to its canonical release-root-relative spelling, or
    # to '' when it leaves the release root. Without this a '..'-carrying
    # literal becomes a second key for a file the declared list already names:
    # the equality assertion then fails for a spelling difference, or -- worse
    # when the file is NOT declared -- the chain quietly holds the same file
    # twice under two names.
    $toReleaseRelative = {
        param([string]$Candidate)
        $full = ''
        try { $full = [IO.Path]::GetFullPath((Join-Path $root $Candidate)).TrimEnd('\') } catch { return '' }
        if (-not $full.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase)) { return '' }
        return $full.Substring($root.Length + 1)
    }
    $entryPoints = if (@($EntryPoint).Count -gt 0) { @($EntryPoint) } else { @($script:StockTunnelChainEntryPoints) }
    $hashed = [Collections.Generic.SortedSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $presence = [Collections.Generic.SortedSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    $queue = [Collections.Generic.Queue[string]]::new()
    foreach ($seed in $entryPoints) {
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
            param($node)
            $node -is [Management.Automation.Language.StringConstantExpressionAst] -or
            $node -is [Management.Automation.Language.ExpandableStringExpressionAst]
        }, $true))
        $parent = Split-Path -Parent $relative
        foreach ($literal in $literals) {
            if ($literal -is [Management.Automation.Language.ExpandableStringExpressionAst]) {
                $value = Resolve-StockTunnelChainExpandableString -Text ([string]$literal.Value) -Relative $relative
                if ($null -eq $value) { continue }
            } else {
                $value = [string]$literal.Value
            }
            # '/' and '\' name the same separator to PowerShell, so neither the
            # root-relative reading nor the loud rule below may key on one
            # spelling of it.
            $value = $value.Replace('/', '\')
            # The whole value must BE a path, not merely end in one: a message
            # ('Run scripts\windows\tests\test-shared-tunnel-recovery.ps1')
            # names a file this tree need not carry and must not become a hard
            # error. No path on this chain contains whitespace.
            if ($value -notmatch '^\S+\.(ps1|psm1|cs|exe)$') { continue }
            if ($value -match '[*?"<>|]') { continue }
            if ($value -match '^[A-Za-z]:' -or $value.StartsWith('\\')) { continue }
            # Root-relative first (the historical reading), then relative to the
            # mentioning file's own directory, which is what
            # `Join-Path $PSScriptRoot 'sub\helper.psm1'` means.
            $hasSeparator = $value.Contains('\')
            $trimmed = $value.TrimStart('\')
            $besideMentioningFile = if ($parent) { Join-Path $parent $trimmed } else { $trimmed }
            # @(...) around the whole conditional: a one-element array assigned
            # out of an `if` is unrolled to a bare string, and indexing that
            # would walk its characters. Normalizing here (not at the end) keeps
            # the .exe rules, the Test-Path probe and the queued key on one
            # canonical spelling.
            $candidates = @(@(if ($hasSeparator) { $trimmed; $besideMentioningFile } else { $besideMentioningFile }) |
                ForEach-Object { [string](& $toReleaseRelative $_) } |
                Where-Object { $_ } |
                Select-Object -Unique)
            if ($candidates.Count -eq 0) { continue }
            $candidate = [string]$candidates[0]
            if ($candidate.StartsWith($binPrefix, [StringComparison]::OrdinalIgnoreCase) -and
                $candidate.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) {
                [void]$presence.Add($candidate)
                continue
            }
            if ($candidate.EndsWith('.exe', [StringComparison]::OrdinalIgnoreCase)) { continue }
            $resolvedCandidate = @($candidates | Where-Object { Test-Path -LiteralPath (Join-Path $root $_) -PathType Leaf })
            if ($resolvedCandidate.Count -eq 0) {
                # A literal that spells out a path into the tree and resolves
                # nowhere is either a chain element this release no longer
                # carries or one the parser mis-resolved. Either way the gate
                # would go on hashing a file list that is missing it, and the
                # equality test would still pass green, so say so loudly.
                if ($hasSeparator) {
                    throw ("Tunnel execution chain file '$relative' references '$value', which exists neither at " +
                        "the release root nor beside it. Fix the reference, or teach " +
                        'Get-StockTunnelExecutionChainFile how to resolve it, so the reinstall gate can see the file.')
                }
                continue
            }
            $candidate = [string]$resolvedCandidate[0]
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

function Get-StockTunnelTaskActionPath {
    # PURE. Every absolute path the registered action names: the Execute binary
    # plus every quoted token in Arguments that is an absolute Windows path.
    # These are the paths Task Scheduler will actually try to launch or hand to
    # the host, and only a reinstall re-resolves them -- which matters because
    # Get-InstalledPowerShell bakes a version-pinned
    # C:\Program Files\WindowsApps\Microsoft.PowerShell_<version>_..._x64\pwsh.exe
    # into the action on a host with no MSI PowerShell, and the Store deletes
    # that directory when it updates PowerShell. The caller tests existence;
    # this function only says which paths must exist.
    [CmdletBinding()]
    param([string]$Execute = '', [string]$Arguments = '')
    $paths = [Collections.Generic.List[string]]::new()
    $collect = {
        param($Value)
        $text = ([string]$Value).Trim()
        if ($text.StartsWith('"') -and $text.EndsWith('"') -and $text.Length -ge 2) {
            $text = $text.Substring(1, $text.Length - 2)
        }
        if (-not $text) { return }
        if ($text -match '^[A-Za-z]:\\' -or $text.StartsWith('\\')) { [void]$paths.Add($text) }
    }
    & $collect $Execute
    foreach ($match in [regex]::Matches([string]$Arguments, '"([^"]*)"')) {
        & $collect $match.Groups[1].Value
    }
    return @($paths)
}

function Get-StockTunnelTaskActionShell {
    # PURE. The PowerShell host the registered action launches: the first quoted
    # token in Arguments naming pwsh.exe / powershell.exe. Empty when the action
    # names none, which the decision treats as unresolved, i.e. a change.
    [CmdletBinding()]
    param([string]$Arguments = '')
    foreach ($match in [regex]::Matches([string]$Arguments, '"([^"]*)"')) {
        $value = $match.Groups[1].Value
        if ($value -match '(?i)(^|[\\/])(pwsh|powershell)\.exe$') { return $value }
    }
    return ''
}

function Get-StockTunnelPathHash {
    # PURE. SHA-256 of a normalized (lower-cased, trailing-separator-free) path,
    # or 'unresolved' for an empty one. The decision compares hashes
    # case-insensitively anyway; normalizing here keeps a differently-cased but
    # identical path from reading as a rotation.
    [CmdletBinding()]
    param([string]$Path = '')
    $text = ([string]$Path).Trim().TrimEnd('\')
    if (-not $text) { return 'unresolved' }
    return Get-StockTextSha256 -Text $text.ToLowerInvariant()
}

function Get-StockTunnelTaskPrincipalHash {
    # PURE. SHA-256 over the scheduled task principal (LogonType + UserId)
    # either a (re)install would register or a registered task already carries.
    # The user name is compared bare: Get-ScheduledTask may report
    # '<COMPUTER>\brave' where New-ScheduledTaskPrincipal was given '$env:USERNAME'.
    [CmdletBinding()]
    param([string]$LogonType = '', [string]$UserId = '')
    $logon = ([string]$LogonType).Trim()
    $user = ([string]$UserId).Trim()
    if ($user.Contains('\')) { $user = $user.Substring($user.LastIndexOf('\') + 1) }
    if (-not $logon -or -not $user) { return 'unresolved' }
    return Get-StockTextSha256 -Text ('logon=' + $logon.ToLowerInvariant() + "`nuser=" + $user.ToLowerInvariant())
}

function Get-StockReleaseStamp {
    # PURE. The timestamp embedded in a release id
    # (yyyyMMddTHHmmss-<short head>-clean|dirty), as a round-trip ('o') local
    # timestamp; '' when the id carries none (hand-made ids, test fixtures).
    [CmdletBinding()]
    param([string]$ReleaseId = '')
    $match = [regex]::Match([string]$ReleaseId, '^(\d{8})T(\d{6})')
    if (-not $match.Success) { return '' }
    [datetime]$parsed = [datetime]::MinValue
    if (-not [datetime]::TryParseExact(($match.Groups[1].Value + $match.Groups[2].Value), 'yyyyMMddHHmmss',
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$parsed)) {
        return ''
    }
    return ([DateTimeOffset]::new($parsed, [TimeZoneInfo]::Local.GetUtcOffset($parsed))).ToString('o')
}

function Resolve-StockReleaseActivationInstant {
    # PURE. Picks the instant the `current` junction actually moved to the
    # active release, and says how good that answer is.
    #
    # The release id's own yyyyMMddTHHmmss prefix is NOT that instant:
    # publish-stock-release.ps1 computes it before the eight PowerShell suites,
    # the pytest run and three npm invocations, and only switches the junction
    # afterwards, so the id stamp precedes activation by minutes to tens of
    # minutes. Everything in that window still resolves through `current` to the
    # PREVIOUS release, so a tunnel that started there did not come from the
    # active release at all.
    #
    # Sources, best first:
    #   activated_at           - recorded by publish/switch right after
    #                            Set-StockCurrentRelease returned. Exact, but
    #                            only while `current` still resolves to the
    #                            active release: switch moves the junction
    #                            before it writes state, and if it dies in
    #                            between (its revert failure is only a warning)
    #                            or the junction is moved by hand, the recorded
    #                            instant belongs to a release `current` has
    #                            demonstrably left. Judged against that stale
    #                            instant, a tunnel that started from the NEW
    #                            tree looks like a relaunch from the active one
    #                            and buys a skip it has not earned, so the same
    #                            junction check that guards the source below
    #                            guards this one.
    #   current_junction_created - the junction's own creation time. Exact:
    #                            Set-StockCurrentRelease builds a fresh junction
    #                            under a staging name and renames it into place
    #                            on every switch, so its creation time is the
    #                            swap. Only usable when the junction really
    #                            resolves to the active release.
    #   release_id             - the id stamp. Approximate, and always EARLY.
    #   release_directory_created - the staged tree's creation time, for ids
    #                            that carry no stamp. Approximate, also early.
    # Returns Stamp/Source/Exact; Source 'unknown' with an empty Stamp when
    # nothing is available.
    [CmdletBinding()]
    param(
        [string]$RecordedActivatedAt = '',
        [string]$JunctionCreatedAt = '',
        [bool]$JunctionMatchesActiveRelease = $false,
        [string]$ReleaseIdStamp = '',
        [string]$ReleaseDirectoryCreatedAt = ''
    )
    $answer = {
        param($Stamp, $Source, $Exact)
        [pscustomobject]@{ Stamp = [string]$Stamp; Source = [string]$Source; Exact = [bool]$Exact }
    }
    if ($JunctionMatchesActiveRelease -and [string]$RecordedActivatedAt) {
        return (& $answer $RecordedActivatedAt 'activated_at' $true)
    }
    if ($JunctionMatchesActiveRelease -and [string]$JunctionCreatedAt) {
        return (& $answer $JunctionCreatedAt 'current_junction_created' $true)
    }
    if ([string]$ReleaseIdStamp) { return (& $answer $ReleaseIdStamp 'release_id' $false) }
    if ([string]$ReleaseDirectoryCreatedAt) {
        return (& $answer $ReleaseDirectoryCreatedAt 'release_directory_created' $false)
    }
    return (& $answer '' 'unknown' $false)
}

function Get-StockTunnelPinRefresh {
    # PURE. release-state.json's tunnel_release records where the tunnel was
    # LAST INSTALLED FROM. The registered task action is
    # <PlatformRoot>\current\..., so every relaunch -- the 2-minute supervising
    # trigger after a drop, a logon, a reboot -- starts the tunnel from whatever
    # `current` resolves to at that moment, i.e. the active release, and writes
    # nothing to release-state.json. When the supervised run started at or after
    # the junction moved to the active release, the live process came from
    # `current`, so the pin is refreshed to the active release; anything else
    # leaves the recorded pin alone (an unreadable start time is not evidence).
    #
    # The comparison is only as good as the activation instant it is given (see
    # Resolve-StockReleaseActivationInstant). With an approximate one -- the
    # release id stamp, which is always EARLIER than the real switch -- a
    # started_at inside that window looks like a relaunch but may be the
    # original run, still executing out of the pinned tree. The pin is refreshed
    # anyway, because refreshing is what keeps it from naming a tree nothing
    # runs from, and the answer is marked Uncertain so the caller can refuse to
    # SKIP on it: an uncertain pin buys a reinstall, never a skipped one.
    [CmdletBinding()]
    param(
        [string]$PinnedRelease = '',
        [string]$ActiveRelease = '',
        # Round-trip timestamp of the moment `current` moved to $ActiveRelease.
        [string]$ActiveReleaseStamp = '',
        # $true only when $ActiveReleaseStamp is the activation instant itself
        # (release-state.json's activated_at, or the junction's creation time)
        # rather than something that merely precedes it.
        [bool]$ActivationInstantIsExact = $false,
        # The runtime state file's started_at for the tunnel service.
        [string]$RuntimeStartedAt = ''
    )
    $unchanged = {
        param($Reason)
        [pscustomobject]@{
            Release = [string]$PinnedRelease
            Refreshed = $false
            Reason = [string]$Reason
            Uncertain = $false
        }
    }
    if (-not $PinnedRelease) { return (& $unchanged 'no_pin') }
    if (-not $ActiveRelease) { return (& $unchanged 'no_active_release') }
    if (([string]$ActiveRelease).Equals([string]$PinnedRelease, [StringComparison]::OrdinalIgnoreCase)) {
        return (& $unchanged 'pin_is_active_release')
    }
    [DateTimeOffset]$startedAt = [DateTimeOffset]::MinValue
    if (-not $RuntimeStartedAt -or -not [DateTimeOffset]::TryParse($RuntimeStartedAt,
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$startedAt)) {
        return (& $unchanged 'runtime_start_unknown')
    }
    [DateTimeOffset]$activeAt = [DateTimeOffset]::MinValue
    if (-not $ActiveReleaseStamp -or -not [DateTimeOffset]::TryParse($ActiveReleaseStamp,
            [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::None, [ref]$activeAt)) {
        return (& $unchanged 'active_release_time_unknown')
    }
    if ($startedAt -ge $activeAt) {
        return [pscustomobject]@{
            Release = [string]$ActiveRelease
            Refreshed = $true
            Reason = if ($ActivationInstantIsExact) { 'runtime_started_after_activation' }
                     else { 'runtime_started_after_release_stamp_activation_time_approximate' }
            Uncertain = (-not $ActivationInstantIsExact)
        }
    }
    # Earlier than an instant that is itself at or before the switch: the run
    # cannot have come from `current`, whichever source the stamp came from.
    return (& $unchanged 'runtime_predates_active_release')
}

function Get-StockTunnelHostBuildEvidence {
    # The value recorded for the presence-only background host executable.
    # Hashing the executable itself is impossible (csc.exe embeds a fresh module
    # GUID per build) and bare presence is too weak: with -SkipTests
    # build-background-task-host.ps1 never runs while Copy-DirectorySnapshot
    # still copies whatever sits in the source tree's bin, so a truncated or
    # stale executable would pass as 'present'. build-background-task-host.ps1
    # therefore writes <name>.build.json next to it (input SHA-256s + output
    # length), which IS deterministic for deterministic sources.
    #
    #   'missing'          the executable is not in this tree
    #   'present'          it is there but carries no build receipt (a release
    #                      published before the receipt existed)
    #   'receipt_mismatch' the receipt disagrees with the file on disk, or is
    #                      unreadable -- never equal to anything
    #   'receipt:<sha256>' SHA-256 of the receipt's bytes
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$RuntimeRoot,
        [Parameter(Mandatory)][string]$Relative
    )
    $path = Join-Path $RuntimeRoot $Relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return 'missing' }
    $receiptPath = [IO.Path]::ChangeExtension($path, '.build.json')
    if (-not (Test-Path -LiteralPath $receiptPath -PathType Leaf)) { return 'present' }
    try {
        $bytes = [IO.File]::ReadAllBytes($receiptPath)
        $receipt = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
        if (-not $receipt -or -not $receipt.PSObject.Properties['output_length']) { return 'receipt_mismatch' }
        if ([int64]$receipt.output_length -ne (Get-Item -LiteralPath $path -Force).Length) { return 'receipt_mismatch' }
        return 'receipt:' + (Get-StockBytesSha256 -Bytes $bytes)
    } catch {
        return 'receipt_mismatch'
    }
}

function Resolve-StockTunnelHostBuildComparison {
    # PURE. A release published before build-background-task-host.ps1 wrote a
    # build receipt reports 'present', and there is nothing for a receipt to be
    # compared against, so both sides degrade to presence -- but ONLY when the
    # OLD tree is the one without the receipt. A NEW tree that lost its receipt
    # is a regression and is never excused.
    [CmdletBinding()]
    param([string]$CurrentValue = '', [string]$NewValue = '')
    $current = [string]$CurrentValue
    $new = [string]$NewValue
    if ($current -eq 'present' -and $new.StartsWith('receipt:', [StringComparison]::OrdinalIgnoreCase)) {
        $new = 'present'
    }
    return [pscustomobject]@{ Current = $current; New = $new }
}

function Test-StockTunnelHashChanged {
    # PURE. The single comparison rule, used for both baselines (the pinned tree
    # and the `current` junction's tree) so they can never drift apart.
    [CmdletBinding()]
    param([string]$CurrentValue = '', [string]$NewValue = '')
    $pair = Resolve-StockTunnelHostBuildComparison -CurrentValue $CurrentValue -NewValue $NewValue
    if ($script:StockTunnelNeverEqualValues -contains $pair.New) { return $true }
    return -not ([string]$pair.New).Equals([string]$pair.Current, [StringComparison]::OrdinalIgnoreCase)
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
        return Get-StockTextSha256 -Text ($material -join "`n")
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
        $hashes[$relative] = Get-StockTunnelHostBuildEvidence -RuntimeRoot $RuntimeRoot -Relative $relative
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
        # The `current` junction's tree, i.e. the tree the tunnel's NEXT
        # relaunch will start from (the registered action points at
        # <PlatformRoot>\current). It differs from CurrentHashes as soon as one
        # publish has skipped, and a skip is only safe when the new tree is
        # byte-identical to both. Synthetic 'config:' entries are excluded: they
        # describe the installed task and the resolved SSH identity, not a tree.
        [hashtable]$JunctionHashes = @{},
        [string]$TaskState = '',
        [string]$RuntimeStatus = '',
        [string]$RemoteHealthStatus = '',
        # 'retained' only when release-state.json's tunnel_release names a
        # release that is still on disk and still in the retention keep set.
        [string]$TunnelReleaseState = '',
        # 'yes' only when the registered task action runs out of
        # <PlatformRoot>\current\ (see Get-StockTunnelTaskActionPlacement).
        [string]$TaskActionUnderCurrent = '',
        # 'ok' only when every absolute path the registered action names still
        # exists (see Get-StockTunnelTaskActionPath).
        [string]$TaskActionPathState = '',
        # $true when the pin refresh could not tell whether the live tunnel came
        # from the pinned tree or from `current`, because the activation instant
        # was only approximate (see Get-StockTunnelPinRefresh). CurrentHashes
        # then describes a tree that may not be the one the process is executing
        # out of, which is not a baseline a skip may rest on.
        [bool]$TunnelPinUncertain = $false
    )
    if ($null -eq $CurrentHashes) { $CurrentHashes = @{} }
    if ($null -eq $NewHashes) { $NewHashes = @{} }
    if ($null -eq $JunctionHashes) { $JunctionHashes = @{} }
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
        # synthetic entries (SSH target, task host pwsh, task principal): an
        # input we could not read is never evidence that it is unchanged.
        if (Test-StockTunnelHashChanged -CurrentValue $currentValue -NewValue $newValue) { $changed += $name }
    }
    # Second baseline: the tree a relaunch would pick up through `current`.
    $junctionNames = [Collections.Generic.SortedSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($key in $JunctionHashes.Keys) {
        if (-not ([string]$key).StartsWith($script:StockTunnelSyntheticKeyPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            [void]$junctionNames.Add([string]$key)
        }
    }
    foreach ($key in $NewHashes.Keys) {
        if (-not ([string]$key).StartsWith($script:StockTunnelSyntheticKeyPrefix, [StringComparison]::OrdinalIgnoreCase)) {
            [void]$junctionNames.Add([string]$key)
        }
    }
    $junctionChanged = @()
    foreach ($name in $junctionNames) {
        $junctionValue = if ($JunctionHashes.ContainsKey($name)) { [string]$JunctionHashes[$name] } else { 'missing' }
        $newValue = if ($NewHashes.ContainsKey($name)) { [string]$NewHashes[$name] } else { 'missing' }
        if (Test-StockTunnelHashChanged -CurrentValue $junctionValue -NewValue $newValue) { $junctionChanged += $name }
    }
    $reasons = @()
    # No hashes at all means no comparable current release (first publish, or
    # a caller that could not resolve the running tree): reinstall.
    if ($names.Count -eq 0) { $reasons += 'no_tunnel_files_hashed' }
    if ($changed.Count -gt 0) { $reasons += 'tunnel_files_changed' }
    if ($JunctionHashes.Count -eq 0) { $reasons += 'no_junction_files_hashed' }
    elseif ($junctionChanged.Count -gt 0) { $reasons += 'current_junction_files_changed' }
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
    if ($TunnelPinUncertain) { $reasons += 'tunnel_release_pin_uncertain' }
    if ($TaskActionUnderCurrent -ne 'yes') { $reasons += 'task_action_not_under_current' }
    # A version-pinned WindowsApps pwsh.exe that the Store has since removed
    # leaves an action Task Scheduler can no longer launch; only a reinstall
    # re-resolves it.
    if ($TaskActionPathState -ne 'ok') { $reasons += 'task_action_path_missing' }
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
        junction_changed_files = $junctionChanged
        task_state = $TaskState
        runtime_status = $RuntimeStatus
        remote_health_status = $RemoteHealthStatus
        tunnel_release_state = $TunnelReleaseState
        task_action_under_current = $TaskActionUnderCurrent
        task_action_path_state = $TaskActionPathState
        tunnel_release_pin_uncertain = [bool]$TunnelPinUncertain
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
    # TWO comparison trees, because a skip has two futures to answer for:
    #   * the release the live tunnel processes were last installed from
    #     (release-state.json's tunnel_release) -- the code running right now;
    #   * the `current` junction's tree -- the code the NEXT relaunch starts
    #     from, because the registered task action is
    #     <PlatformRoot>\current\scripts\windows\bin\stock-background-host.exe.
    # Those diverge by construction as soon as one publish skips. Comparing only
    # `current` says nothing about the running process under a file list that
    # has since gained a file; comparing only the pin says nothing about the
    # tree the 2-minute supervising trigger picks up after the next drop. A skip
    # requires byte-equality against both.
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
        # The principal a (re)install by THIS caller would register. Defaults to
        # what publish/switch derive from the session, so a diagnostic run
        # compares against the same thing a real publish would.
        [string]$TaskLogonType = '',
        [string]$TaskUserId = '',
        # Test seams. Both default to the real scheduler / real SSH probe; the
        # contract test injects them so it can drive this function (not just the
        # pure decision) against a temporary platform root without a scheduled
        # task or a network. ScheduledTaskProvider receives the task name and
        # returns $null or an object with State/Execute/Arguments (optionally
        # LogonType/UserId); RemoteHealthProbe returns the HTTP status text.
        [scriptblock]$ScheduledTaskProvider = $null,
        [scriptblock]$RemoteHealthProbe = $null
    )
    $platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
    $layout = Get-StockReleaseLayout -PlatformRoot $platform
    $releaseState = Get-StockReleaseState -PlatformRoot $platform
    $readState = {
        param($Name)
        if ($releaseState -and $releaseState.PSObject.Properties[$Name]) { [string]$releaseState.$Name } else { '' }
    }
    if (-not $TunnelReleaseId) { $TunnelReleaseId = & $readState 'tunnel_release' }
    if (-not $TunnelSshTargetSha256) { $TunnelSshTargetSha256 = & $readState 'tunnel_ssh_target_sha256' }
    if (-not $TaskLogonType) { $TaskLogonType = Get-StockScheduledTaskLogonType }
    if (-not $TaskUserId) { $TaskUserId = [string]$env:USERNAME }
    $activeRelease = & $readState 'active_release'
    $previousRelease = & $readState 'previous_release'

    # The tunnel's own runtime state, read before anything else needs it: its
    # status is one observation, and its started_at is what tells us whether the
    # live process really came from the pinned release or from `current`.
    $runtimeStatus = 'missing'
    $runtimeStartedAt = ''
    $runtimeStatePath = Join-Path $platform 'logs\runtime\shared-peer-tunnels.current.json'
    if (Test-Path -LiteralPath $runtimeStatePath -PathType Leaf) {
        try {
            $runtimeState = Get-Content -LiteralPath $runtimeStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
            $runtimeStatus = if ($runtimeState -and $runtimeState.PSObject.Properties['status']) { [string]$runtimeState.status } else { 'unknown' }
            if ($runtimeState -and $runtimeState.PSObject.Properties['started_at']) { $runtimeStartedAt = [string]$runtimeState.started_at }
        } catch { $runtimeStatus = 'unreadable' }
    }

    # tunnel_release is "last installed from". Every relaunch starts from
    # `current`, so when the supervised run began at or after the junction
    # MOVED, the pin is stale and the active release is what the tunnel is
    # really executing. The comparison instant has to be the junction move
    # itself: the release id's stamp is written before the test suite runs and
    # can precede activation by tens of minutes, and `current` still resolved to
    # the previous release for all of that window.
    $junctionCreated = ''
    $junctionResolved = ''
    try {
        $currentItem = Get-Item -LiteralPath $layout.CurrentPath -Force -ErrorAction Stop
        $junctionCreated = ([DateTimeOffset]$currentItem.CreationTime).ToString('o')
        $junctionResolved = [IO.Path]::GetFullPath([string]$currentItem.Target).TrimEnd('\')
    } catch { $junctionCreated = ''; $junctionResolved = '' }
    $activeAppPath = ''
    if ($activeRelease) {
        try { $activeAppPath = [IO.Path]::GetFullPath((Get-StockReleaseAppPath -PlatformRoot $platform -ReleaseId $activeRelease)).TrimEnd('\') }
        catch { $activeAppPath = '' }
    }
    $releaseDirCreated = ''
    if ($activeRelease) {
        $activeDir = Join-Path $layout.ReleasesRoot $activeRelease
        if (Test-Path -LiteralPath $activeDir -PathType Container) {
            $releaseDirCreated = ([DateTimeOffset](Get-Item -LiteralPath $activeDir -Force).CreationTime).ToString('o')
        }
    }
    $activation = Resolve-StockReleaseActivationInstant -RecordedActivatedAt (& $readState 'activated_at') `
        -JunctionCreatedAt $junctionCreated `
        -JunctionMatchesActiveRelease ([bool]($activeAppPath -and $junctionResolved -and
            $junctionResolved.Equals($activeAppPath, [StringComparison]::OrdinalIgnoreCase))) `
        -ReleaseIdStamp (Get-StockReleaseStamp -ReleaseId $activeRelease) `
        -ReleaseDirectoryCreatedAt $releaseDirCreated
    $pinRefresh = Get-StockTunnelPinRefresh -PinnedRelease $TunnelReleaseId -ActiveRelease $activeRelease `
        -ActiveReleaseStamp $activation.Stamp -ActivationInstantIsExact ([bool]$activation.Exact) `
        -RuntimeStartedAt $runtimeStartedAt
    $TunnelReleaseId = [string]$pinRefresh.Release

    # Deliberately NOT Get-StockReleaseRetentionState: that reads tunnel_release
    # out of the same state file and pins it unconditionally, so the keep set
    # would always contain the id being checked and tunnel_release_not_retained
    # could never fire. The retained set here is derived from the release
    # directories plus the {active, previous} pins ONLY, which also bounds how
    # far behind `current` a chain of skips may leave the live tunnel.
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
            $independent = Get-StockReleaseRetentionPlan -ReleaseNames (Get-StockReleaseDirectoryName -PlatformRoot $platform) `
                -ActiveRelease $activeRelease -PreviousRelease $previousRelease -RetainCount $RetainCount
            $retained = @($independent.Keep) | Where-Object { $_ -and ([string]$_).Equals($TunnelReleaseId, [StringComparison]::OrdinalIgnoreCase) }
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
    $junctionRoot = ''
    try { $junctionRoot = [string](Get-StockCurrentReleaseTarget -PlatformRoot $platform) } catch { $junctionRoot = '' }
    $junctionHashes = Get-StockTunnelFileHash -RuntimeRoot $junctionRoot

    $taskState = 'not_registered'
    $taskActionUnderCurrent = 'unknown'
    $taskActionPathState = 'unknown'
    $taskActionMissingPaths = @()
    $registeredShell = ''
    $registeredPrincipal = 'unresolved'
    try {
        $observed = if ($ScheduledTaskProvider) { & $ScheduledTaskProvider $TaskName } else {
            $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
            $action = @($task.Actions)[0]
            [pscustomobject]@{
                State = [string]$task.State
                Execute = if ($action) { [string]$action.Execute } else { '' }
                Arguments = if ($action) { [string]$action.Arguments } else { '' }
                LogonType = if ($task.Principal) { [string]$task.Principal.LogonType } else { '' }
                UserId = if ($task.Principal) { [string]$task.Principal.UserId } else { '' }
            }
        }
        if ($observed) {
            $read = {
                param($Name)
                if ($observed.PSObject.Properties[$Name]) { [string]$observed.$Name } else { '' }
            }
            $taskState = & $read 'State'
            $execute = & $read 'Execute'
            $arguments = & $read 'Arguments'
            $taskActionUnderCurrent = Get-StockTunnelTaskActionPlacement -Execute $execute `
                -Arguments $arguments -ExpectedPrefix (Join-Path $platform 'current')
            $actionPaths = @(Get-StockTunnelTaskActionPath -Execute $execute -Arguments $arguments)
            if ($actionPaths.Count -gt 0) {
                $taskActionMissingPaths = @($actionPaths | Where-Object { -not (Test-Path -LiteralPath $_) })
                $taskActionPathState = if ($taskActionMissingPaths.Count -gt 0) { 'missing' } else { 'ok' }
            }
            $registeredShell = Get-StockTunnelTaskActionShell -Arguments $arguments
            $registeredPrincipal = Get-StockTunnelTaskPrincipalHash -LogonType (& $read 'LogonType') -UserId (& $read 'UserId')
        }
    } catch { $taskState = 'not_registered' }

    # Synthetic entries: things the running tunnel depends on that live in no
    # release tree. They are compared against the pinned side only (the
    # `current` junction says nothing about them).
    $newHashes[$script:StockTunnelSshTargetKey] = Get-StockTunnelSshTargetHash -PlatformRoot $platform -FallbackAlias $SshAlias
    $resolvedShell = ''
    try { $resolvedShell = [string](Get-InstalledPowerShell) } catch { $resolvedShell = '' }
    $newHashes[$script:StockTunnelTaskHostShellKey] = Get-StockTunnelPathHash -Path $resolvedShell
    $newHashes[$script:StockTunnelTaskPrincipalKey] = Get-StockTunnelTaskPrincipalHash -LogonType $TaskLogonType -UserId $TaskUserId
    if ($currentHashes.Count -gt 0) {
        $currentHashes[$script:StockTunnelSshTargetKey] = if ($TunnelSshTargetSha256) { $TunnelSshTargetSha256 } else { 'missing' }
        $currentHashes[$script:StockTunnelTaskHostShellKey] = Get-StockTunnelPathHash -Path $registeredShell
        $currentHashes[$script:StockTunnelTaskPrincipalKey] = $registeredPrincipal
    }

    # The SSH round trip is the only expensive observation, and it cannot
    # change a decision that is already 'reinstall'. Ask the pure function
    # first with the health condition satisfied; probe only if nothing else
    # already forces a reinstall.
    $observations = @{
        CurrentHashes = $currentHashes
        NewHashes = $newHashes
        JunctionHashes = $junctionHashes
        TaskState = $taskState
        RuntimeStatus = $runtimeStatus
        TunnelReleaseState = $tunnelReleaseState
        TaskActionUnderCurrent = $taskActionUnderCurrent
        TaskActionPathState = $taskActionPathState
        TunnelPinUncertain = [bool]$pinRefresh.Uncertain
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
        junction_changed_files = $plan.junction_changed_files
        task_state = $plan.task_state
        runtime_status = $plan.runtime_status
        remote_health_status = $plan.remote_health_status
        tunnel_release_state = $plan.tunnel_release_state
        task_action_under_current = $plan.task_action_under_current
        task_action_path_state = $plan.task_action_path_state
        task_action_missing_paths = @($taskActionMissingPaths)
        tunnel_release = $TunnelReleaseId
        tunnel_release_pin_refreshed = [bool]$pinRefresh.Refreshed
        tunnel_release_pin_reason = [string]$pinRefresh.Reason
        tunnel_release_pin_uncertain = [bool]$pinRefresh.Uncertain
        activation_instant = [string]$activation.Stamp
        activation_instant_source = [string]$activation.Source
        tunnel_runtime_root = $tunnelRuntimeRoot
        junction_runtime_root = $junctionRoot
        current_hashes = $plan.current_hashes
        new_hashes = $plan.new_hashes
    }
}

function Write-StockTunnelReinstallSkipEvent {
    # Only a skip is recorded: a reinstall is the pre-existing behaviour and
    # already leaves its own install/healthy events behind.
    #
    # Callers must invoke this only AFTER activation, post-switch verification
    # AND the release-state write have all succeeded -- in publish that means
    # after `$activated = $true`, because everything before it is still on the
    # rollback path. The documented acceptance check is "grep
    # lifecycle-<date>.jsonl for tunnel_reinstall_skipped"; writing it earlier
    # would leave a skip receipt behind for a publish that then rolled back and
    # unconditionally reinstalled the tunnel, i.e. a receipt the log itself
    # contradicts.
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
                tunnel_release_pin_refreshed = if ($Plan.PSObject.Properties['tunnel_release_pin_refreshed']) { [bool]$Plan.tunnel_release_pin_refreshed } else { $false }
                tunnel_release_pin_reason = if ($Plan.PSObject.Properties['tunnel_release_pin_reason']) { [string]$Plan.tunnel_release_pin_reason } else { '' }
                activation_instant_source = if ($Plan.PSObject.Properties['activation_instant_source']) { [string]$Plan.activation_instant_source } else { '' }
                task_action_under_current = [string]$Plan.task_action_under_current
                task_action_path_state = if ($Plan.PSObject.Properties['task_action_path_state']) { [string]$Plan.task_action_path_state } else { '' }
                current_hashes = $Plan.current_hashes
                new_hashes = $Plan.new_hashes
                changed_files = @($Plan.changed_files)
                junction_changed_files = if ($Plan.PSObject.Properties['junction_changed_files']) { @($Plan.junction_changed_files) } else { @() }
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
    'Get-StockReleaseDirectoryName',
    'Get-StockReleaseRetentionPlan',
    'Get-StockReleaseRetentionState',
    'Get-StockReleaseStamp',
    'Resolve-StockReleaseActivationInstant',
    'Get-StockScheduledTaskLogonType',
    'Get-StockTextSha256',
    'Remove-ExpiredStockReleases',
    'Test-StockReleaseFileHashes',
    'Test-StockReleaseIntegrity',
    'Get-StockTunnelAffectingFile',
    'Get-StockTunnelPresenceOnlyFile',
    'Get-StockTunnelExecutionChainFile',
    'Resolve-StockTunnelChainExpandableString',
    'Get-StockTunnelTaskActionPlacement',
    'Get-StockTunnelTaskActionPath',
    'Get-StockTunnelTaskActionShell',
    'Get-StockTunnelTaskPrincipalHash',
    'Get-StockTunnelPathHash',
    'Get-StockTunnelPinRefresh',
    'Get-StockTunnelHostBuildEvidence',
    'Resolve-StockTunnelHostBuildComparison',
    'Test-StockTunnelHashChanged',
    'Get-StockTunnelSshTargetHash',
    'Get-StockTunnelFileHash',
    'Get-StockTunnelReinstallDecision',
    'Invoke-StockTunnelRemoteHealthProbe',
    'Resolve-StockTunnelReinstallPlan',
    'Write-StockTunnelReinstallSkipEvent'
)
