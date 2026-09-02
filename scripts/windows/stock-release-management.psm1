Set-StrictMode -Version Latest

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
        if ($candidate -eq (Get-StockCurrentReleaseTarget -PlatformRoot $PlatformRoot)) {
            throw "Refusing to remove the active release target: $candidate"
        }
        Remove-Item -LiteralPath $candidate -Recurse -Force
        $removed += $release.Name
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

Export-ModuleMember -Function @(
    'Get-StockReleaseLayout',
    'Get-StockReleaseState',
    'Set-StockReleaseState',
    'Get-StockReleaseAppPath',
    'Get-StockCurrentReleaseTarget',
    'Set-StockCurrentRelease',
    'Remove-ExpiredStockReleases',
    'Test-StockReleaseFileHashes',
    'Test-StockReleaseIntegrity'
)
