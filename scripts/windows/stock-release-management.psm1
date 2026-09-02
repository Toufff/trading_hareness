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
        Remove-Item -LiteralPath $layout.CurrentPath -Force
    }
    New-Item -ItemType Junction -Path $layout.CurrentPath -Target $target | Out-Null
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
    $releases = @(Get-ChildItem -LiteralPath $layout.ReleasesRoot -Directory -Force |
        Where-Object { $_.Name -notlike '.staging-*' } |
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

Export-ModuleMember -Function @(
    'Get-StockReleaseLayout',
    'Get-StockReleaseState',
    'Set-StockReleaseState',
    'Get-StockReleaseAppPath',
    'Get-StockCurrentReleaseTarget',
    'Set-StockCurrentRelease',
    'Remove-ExpiredStockReleases'
)
