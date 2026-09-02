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
$releases = if (Test-Path -LiteralPath $layout.ReleasesRoot -PathType Container) {
    @(Get-ChildItem -LiteralPath $layout.ReleasesRoot -Directory -Force | Where-Object Name -notlike '.staging-*' | Sort-Object LastWriteTimeUtc -Descending | ForEach-Object {
        $releaseManifest = Join-Path $_.FullName 'app\release-manifest.json'
        [ordered]@{
            release_id = $_.Name
            active = [bool]($state.active_release -eq $_.Name)
            previous = [bool]($state.previous_release -eq $_.Name)
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
    releases = $releases
} | ConvertTo-Json -Depth 12
