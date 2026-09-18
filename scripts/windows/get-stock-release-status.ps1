[CmdletBinding()]
param([string]$PlatformRoot = 'G:\StockPlatform')

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\stock-release-management.psm1') -Force
$layout = Get-StockReleaseLayout -PlatformRoot $PlatformRoot
$state = Get-StockReleaseState -PlatformRoot $PlatformRoot
$target = Get-StockCurrentReleaseTarget -PlatformRoot $PlatformRoot
$manifest = $null
if ($target -and (Test-Path -LiteralPath (Join-Path $target 'release-manifest.json') -PathType Leaf)) {
    $manifest = Get-Content -LiteralPath (Join-Path $target 'release-manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
}
# The release the shared-peer tunnel was LAST INSTALLED FROM, and is therefore
# still executing out of. After a run of skipped publishes it is neither the
# active nor the previous release, which is exactly why retention pins it -- and
# why an operator needs to see it before deciding a release directory is safe to
# remove. It is NOT where the tunnel will run from next: the registered task
# action points at <PlatformRoot>\current, so the next relaunch (a fault plus
# the 2-minute supervising trigger, a logon, a reboot) starts it from whatever
# `current` resolves to then.
$tunnelRelease = if ($state.PSObject.Properties['tunnel_release']) { [string]$state.tunnel_release } else { '' }
$releases = if (Test-Path -LiteralPath $layout.ReleasesRoot -PathType Container) {
    @(Get-ChildItem -LiteralPath $layout.ReleasesRoot -Directory -Force | Where-Object Name -notlike '.staging-*' | Sort-Object LastWriteTimeUtc -Descending | ForEach-Object {
        $releaseManifest = Join-Path $_.FullName 'app\release-manifest.json'
        [ordered]@{
            release_id = $_.Name
            active = [bool]($state.active_release -eq $_.Name)
            previous = [bool]($state.previous_release -eq $_.Name)
            shared_peer_tunnel_installed_from = [bool]($tunnelRelease -and $tunnelRelease -eq $_.Name)
            created_at = $_.CreationTime.ToString('o')
            manifest_exists = Test-Path -LiteralPath $releaseManifest -PathType Leaf
            size_bytes = (Get-ChildItem -LiteralPath $_.FullName -Recurse -File | Measure-Object Length -Sum).Sum
        }
    })
} else { @() }
[ordered]@{
    checked_at = [DateTimeOffset]::Now.ToString('o')
    state = $state
    current_path = $layout.CurrentPath
    current_target = $target
    current_manifest = $manifest
    shared_peer_tunnel = [ordered]@{
        last_installed_from = $tunnelRelease
        relaunches_from = $layout.CurrentPath
    }
    releases = $releases
} | ConvertTo-Json -Depth 12
