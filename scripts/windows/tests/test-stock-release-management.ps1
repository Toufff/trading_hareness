[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

$module = Join-Path (Split-Path -Parent $PSScriptRoot) 'stock-release-management.psm1'
$sandbox = Join-Path ([IO.Path]::GetTempPath()) "trading-hareness-releases-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null

try {
    Import-Module $module -Force
    foreach ($release in 'release-001', 'release-002', 'release-003') {
        $app = Get-StockReleaseAppPath -PlatformRoot $sandbox -ReleaseId $release
        New-Item -ItemType Directory -Force -Path $app | Out-Null
        [IO.File]::WriteAllText((Join-Path $app 'marker.txt'), $release, [Text.UTF8Encoding]::new($false))
        Start-Sleep -Milliseconds 20
    }
    [void](Set-StockCurrentRelease -PlatformRoot $sandbox -ReleaseId 'release-003')
    [void](Set-StockReleaseState -PlatformRoot $sandbox -State @{
        active_release = 'release-003'
        previous_release = 'release-002'
        last_verification = @{ result = 'unit_test' }
    })
    $target = Get-StockCurrentReleaseTarget -PlatformRoot $sandbox
    Assert-True ($target.EndsWith('release-003\app')) 'current must resolve to the selected immutable release'
    Assert-True ((Get-Content -LiteralPath (Join-Path $sandbox 'current\marker.txt') -Raw) -eq 'release-003') 'the current junction must serve the selected release'
    $removed = @(Remove-ExpiredStockReleases -PlatformRoot $sandbox -RetainCount 2)
    Assert-True ($removed -contains 'release-001') 'retention must prune releases older than active and previous'
    Assert-True (Test-Path -LiteralPath (Get-StockReleaseAppPath -PlatformRoot $sandbox -ReleaseId 'release-003')) 'retention must preserve active'
    Assert-True (Test-Path -LiteralPath (Get-StockReleaseAppPath -PlatformRoot $sandbox -ReleaseId 'release-002')) 'retention must preserve previous'
    [pscustomobject]@{
        passed = $true
        current_target = $target
        active_preserved = $true
        previous_preserved = $true
        pruned = $removed
    }
} finally {
    $current = Join-Path $sandbox 'current'
    if (Test-Path -LiteralPath $current) { Remove-Item -LiteralPath $current -Force -ErrorAction SilentlyContinue }
    $resolved = [IO.Path]::GetFullPath($sandbox)
    $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ($resolved.StartsWith($temp + '\trading-hareness-releases-', [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
    }
}
