[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

function Assert-Throws([scriptblock]$Block, [string]$Message) {
    $threw = $false
    try { & $Block | Out-Null } catch { $threw = $true }
    if (-not $threw) { throw "Assertion failed (expected a throw): $Message" }
}

$windowsScripts = Split-Path -Parent $PSScriptRoot
$module = Join-Path $windowsScripts 'stock-release-management.psm1'
Import-Module $module -Force

# --- SHA-256 integrity verification must reject a tampered/incomplete release ---
$sandbox = Join-Path ([IO.Path]::GetTempPath()) "trading-hareness-release-safety-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null
try {
    $releaseRoot = Join-Path $sandbox 'releases\release-x'
    $app = Join-Path $releaseRoot 'app'
    $evidence = Join-Path $releaseRoot 'evidence'
    New-Item -ItemType Directory -Force -Path $app, $evidence | Out-Null
    [IO.File]::WriteAllText((Join-Path $app 'a.txt'), 'hello', [Text.UTF8Encoding]::new($false))
    $hash = (Get-FileHash -LiteralPath (Join-Path $app 'a.txt') -Algorithm SHA256).Hash.ToLowerInvariant()
    [IO.File]::WriteAllLines((Join-Path $evidence 'files.sha256'), @("$hash  a.txt"), [Text.UTF8Encoding]::new($false))

    # Matching manifest must pass.
    $verified = Test-StockReleaseFileHashes -AppPath $app -ManifestPath (Join-Path $evidence 'files.sha256')
    Assert-True ($verified.checked_files -eq 1) 'a correct manifest must verify successfully'

    # Tamper with the file after the manifest was written: must be rejected.
    [IO.File]::WriteAllText((Join-Path $app 'a.txt'), 'tampered', [Text.UTF8Encoding]::new($false))
    Assert-Throws { Test-StockReleaseFileHashes -AppPath $app -ManifestPath (Join-Path $evidence 'files.sha256') } `
        'a tampered file must fail SHA-256 verification'

    # Missing file must also be rejected.
    Remove-Item -LiteralPath (Join-Path $app 'a.txt') -Force
    Assert-Throws { Test-StockReleaseFileHashes -AppPath $app -ManifestPath (Join-Path $evidence 'files.sha256') } `
        'a missing file must fail SHA-256 verification'

    # Test-StockReleaseIntegrity (the PlatformRoot/ReleaseId-based wrapper
    # switch-stock-release.ps1 and publish-stock-release.ps1's rollback path
    # actually call) must refuse to switch to a release with no manifest at all.
    $bareRelease = Join-Path $sandbox 'releases\release-bare\app'
    New-Item -ItemType Directory -Force -Path $bareRelease | Out-Null
    Assert-Throws { Test-StockReleaseIntegrity -PlatformRoot $sandbox -ReleaseId 'release-bare' } `
        'a release with no evidence/files.sha256 manifest must fail integrity verification'
} finally {
    $resolved = [IO.Path]::GetFullPath($sandbox)
    $temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ($resolved.StartsWith($temp + '\trading-hareness-release-safety-', [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolved -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --- Retention must never consider a release renamed to "<id>.failed" ---
$sandbox2 = Join-Path ([IO.Path]::GetTempPath()) "trading-hareness-release-safety-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $sandbox2 | Out-Null
try {
    foreach ($release in 'release-001', 'release-002', 'release-003.failed') {
        $releaseDir = Join-Path (Join-Path $sandbox2 'releases') $release
        New-Item -ItemType Directory -Force -Path (Join-Path $releaseDir 'app') | Out-Null
        Start-Sleep -Milliseconds 20
    }
    [void](Set-StockCurrentRelease -PlatformRoot $sandbox2 -ReleaseId 'release-002')
    [void](Set-StockReleaseState -PlatformRoot $sandbox2 -State @{ active_release = 'release-002'; previous_release = 'release-001' })
    $removed = @(Remove-ExpiredStockReleases -PlatformRoot $sandbox2 -RetainCount 1)
    Assert-True (-not ($removed -contains 'release-003.failed')) 'a "<id>.failed" release must never be selected by the retention policy (kept or pruned) as if it were a real release'
    Assert-True (Test-Path -LiteralPath (Join-Path $sandbox2 'releases\release-003.failed')) 'a "<id>.failed" release must be left untouched by Remove-ExpiredStockReleases'
} finally {
    $resolved2 = [IO.Path]::GetFullPath($sandbox2)
    $temp2 = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ($resolved2.StartsWith($temp2 + '\trading-hareness-release-safety-', [StringComparison]::OrdinalIgnoreCase)) {
        $current2 = Join-Path $sandbox2 'current'
        if (Test-Path -LiteralPath $current2) { Remove-Item -LiteralPath $current2 -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $resolved2 -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --- Static regression guards over publish-stock-release.ps1's source ---
# These two failure modes (documented in the trading_hareness audit, section
# I) are process-orchestration bugs that only reproduce with real scheduled
# tasks, real running processes and a real git checkout, which is out of
# reach for a unit test; instead this asserts the specific fixes are present
# in source form so a future edit cannot silently reintroduce them.
$publishScript = Join-Path $windowsScripts 'publish-stock-release.ps1'
$publishSource = Get-Content -LiteralPath $publishScript -Raw -Encoding UTF8

$stopFunctionMatch = [regex]::Match($publishSource, 'function Stop-ProductionRuntime \{.*?\n\}', [Text.RegularExpressions.RegexOptions]::Singleline)
Assert-True $stopFunctionMatch.Success 'Stop-ProductionRuntime function must exist in publish-stock-release.ps1'
$stopBody = $stopFunctionMatch.Value
# Look for the actual invocations (not just any mention of the names, which
# also appear in this function's own explanatory comment).
$gracefulStopIndex = $stopBody.IndexOf('& $stop -PlatformRoot')
$schedTaskIndex = $stopBody.IndexOf("Stop-ScheduledTask -TaskName 'trading-hareness-shared-peer-tunnels'")
Assert-True ($gracefulStopIndex -ge 0 -and $schedTaskIndex -ge 0) 'Stop-ProductionRuntime must call both the graceful stop script and Stop-ScheduledTask'
Assert-True ($gracefulStopIndex -lt $schedTaskIndex) `
    'Stop-ProductionRuntime must run the graceful stop script (which writes the stop marker and kills the actual listener PID) before Stop-ScheduledTask (which kills the whole job object first and would leave the runtime-state file stuck on healthy)'

Assert-True ($publishSource -notmatch 'Start-ProductionRuntime\s+-RuntimeRoot\s+\$source\b') `
    'publish-stock-release.ps1 must never start production from $source (the F: development checkout) when no previous release is available to roll back to'

Assert-True ($publishSource -match '\.failed') `
    'publish-stock-release.ps1 must rename a release that never activated successfully to "<id>.failed" so the retention policy skips it'

[pscustomobject]@{
    passed = $true
    sha256_rejects_tampered_file = $true
    sha256_rejects_missing_manifest = $true
    failed_release_excluded_from_retention = $true
    stop_order_is_graceful_before_scheduled_task = $true
    dev_checkout_fallback_removed = $true
}
