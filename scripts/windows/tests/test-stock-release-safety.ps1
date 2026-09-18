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

Assert-True ($publishSource.Contains("`$branch = (@(& git -C `$source branch --show-current) -join '').Trim()")) `
    'publish-stock-release.ps1 must normalize an empty detached-HEAD branch result before Trim()'
Assert-True ($publishSource.Contains("if (-not `$branch) { `$branch = 'DETACHED' }")) `
    'publish-stock-release.ps1 must label a detached clean release source explicitly'
Assert-True ($publishSource.Contains("[IO.FileShare]::None")) `
    'publish-stock-release.ps1 must serialize production activation across concurrent agents and branches'
Assert-True ($publishSource.Contains("production-publish.lock")) `
    'publish-stock-release.ps1 must use the platform-wide production publish lock'

# --- shared-peer tunnel reinstall gate -------------------------------------
# Regression for the 2026-09-18 measurement: every publish stopped and
# reinstalled the tunnel task, dropping the owner->peer reverse SSH tunnel for
# ~15s and resetting all peer database connections (21/23/5 PostgreSQL client
# resets at 19:42/20:50/21:35) even when no tunnel code had changed.
$healthyHashes = @{
    'scripts\shared-peer\start-shared-tunnels.ps1'       = 'aa'
    'scripts\shared-peer\install-shared-tunnel-task.ps1' = 'bb'
    'scripts\windows\runtime-observability.psm1'         = 'cc'
    'scripts\windows\background-process.psm1'            = 'dd'
    'scripts\windows\background-task-host.cs'            = 'ee'
    'scripts\windows\process-lifetime.cs'                = 'ff'
    'scripts\windows\build-background-task-host.ps1'     = 'gg'
    'scripts\windows\bin\stock-background-host.exe'      = 'present'
}
function New-HashSet([hashtable]$Overrides = @{}) {
    $copy = @{}
    foreach ($key in $healthyHashes.Keys) { $copy[$key] = $healthyHashes[$key] }
    foreach ($key in $Overrides.Keys) {
        if ($null -eq $Overrides[$key]) { $copy.Remove($key) } else { $copy[$key] = $Overrides[$key] }
    }
    return $copy
}
function Get-Decision([hashtable]$Arguments = @{}) {
    $call = @{
        CurrentHashes = (New-HashSet)
        NewHashes = (New-HashSet)
        TaskState = 'Running'
        RuntimeStatus = 'healthy'
        RemoteHealthStatus = '200'
    }
    foreach ($key in $Arguments.Keys) { $call[$key] = $Arguments[$key] }
    return Get-StockTunnelReinstallDecision @call
}

# The gate must keep covering every input the running tunnel uses.
$tunnelFiles = @(Get-StockTunnelAffectingFile -IncludePresenceOnly)
foreach ($required in $healthyHashes.Keys) {
    Assert-True ($tunnelFiles -contains $required) "the tunnel reinstall gate must cover $required"
}
# stock-background-host.exe is the scheduled task's Execute path, but it is
# recompiled on every publish and csc.exe embeds a fresh module GUID, so two
# builds of identical sources differ byte for byte (measured 2026-09-19).
# Hashing it would force a reinstall on every publish and the gate would never
# spare the tunnel. It is presence-checked, and its three tracked, deterministic
# build inputs are hashed in its place.
Assert-True ((@(Get-StockTunnelPresenceOnlyFile)) -contains 'scripts\windows\bin\stock-background-host.exe') `
    'the non-deterministic background host executable must be presence-checked, not hashed'
Assert-True (-not ((@(Get-StockTunnelAffectingFile)) -contains 'scripts\windows\bin\stock-background-host.exe')) `
    'hashing the recompiled background host executable would make the gate reinstall on every publish'
foreach ($buildInput in 'scripts\windows\background-task-host.cs', 'scripts\windows\process-lifetime.cs', 'scripts\windows\build-background-task-host.ps1') {
    Assert-True ((@(Get-StockTunnelAffectingFile)) -contains $buildInput) `
        "the background host's build input $buildInput must be hashed in place of the executable"
}

# All four conditions hold: the only case that may skip.
$skip = Get-Decision
Assert-True ($skip.decision -eq 'skip') 'identical tunnel files + Running task + healthy runtime state + remote HTTP 200 must skip the stop/reinstall'
Assert-True (@($skip.changed_files).Count -eq 0) 'a skip must report no changed tunnel files'

# Every individual condition failing must force a reinstall.
foreach ($file in $healthyHashes.Keys) {
    $changed = Get-Decision @{ NewHashes = (New-HashSet @{ $file = 'changed' }) }
    Assert-True ($changed.decision -eq 'reinstall') "a changed $file must force the tunnel reinstall"
    Assert-True ($changed.changed_files -contains $file) "the decision must name $file as changed"
    $dropped = Get-Decision @{ NewHashes = (New-HashSet @{ $file = $null }) }
    Assert-True ($dropped.decision -eq 'reinstall') "a $file missing from the new release must force the tunnel reinstall"
}
foreach ($taskState in 'Ready', 'Disabled', 'not_registered', '') {
    Assert-True ((Get-Decision @{ TaskState = $taskState }).decision -eq 'reinstall') `
        "task state '$taskState' is not Running and must force the tunnel reinstall"
}
foreach ($runtimeStatus in 'degraded', 'stopped', 'missing', 'unreadable', '') {
    Assert-True ((Get-Decision @{ RuntimeStatus = $runtimeStatus }).decision -eq 'reinstall') `
        "runtime state '$runtimeStatus' is not healthy and must force the tunnel reinstall"
}
foreach ($code in '000', '502', 'probe_failed', 'not_probed', '') {
    Assert-True ((Get-Decision @{ RemoteHealthStatus = $code }).decision -eq 'reinstall') `
        "remote API health '$code' is not 200 and must force the tunnel reinstall"
}
# No comparable current release (first publish, unresolvable junction) must
# never skip.
$noCurrent = Get-Decision @{ CurrentHashes = @{}; NewHashes = @{} }
Assert-True ($noCurrent.decision -eq 'reinstall') 'an unresolvable current release must force the tunnel reinstall'
Assert-True ($noCurrent.reasons -contains 'no_tunnel_files_hashed') 'the decision must record why it could not compare'
Assert-True ((Get-Decision @{ CurrentHashes = @{} }).decision -eq 'reinstall') `
    'a new release with no current tree to compare against must force the tunnel reinstall'

# Real hashes over a real directory pair must reach the same decision.
$hashSandbox = Join-Path ([IO.Path]::GetTempPath()) "trading-hareness-release-safety-$([Guid]::NewGuid().ToString('N'))"
try {
    foreach ($tree in 'old', 'new') {
        foreach ($relative in $tunnelFiles) {
            $path = Join-Path (Join-Path $hashSandbox $tree) $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
            [IO.File]::WriteAllText($path, "content of $relative", [Text.UTF8Encoding]::new($false))
        }
    }
    $oldHashes = Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'old')
    $newHashes = Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new')
    Assert-True ((Get-StockTunnelReinstallDecision -CurrentHashes $oldHashes -NewHashes $newHashes `
        -TaskState 'Running' -RuntimeStatus 'healthy' -RemoteHealthStatus '200').decision -eq 'skip') `
        'byte-identical release trees must hash identically and skip'
    # A byte-different recompile of the host executable alone must NOT force a
    # reinstall; that is the whole reason it is presence-checked.
    [IO.File]::WriteAllText((Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\bin\stock-background-host.exe'), 'a different build of identical sources', [Text.UTF8Encoding]::new($false))
    Assert-True ((Get-StockTunnelReinstallDecision -CurrentHashes $oldHashes `
        -NewHashes (Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new')) `
        -TaskState 'Running' -RuntimeStatus 'healthy' -RemoteHealthStatus '200').decision -eq 'skip') `
        'a recompiled but functionally identical background host executable must not force a tunnel reinstall'
    # Deleting it entirely must.
    Remove-Item -LiteralPath (Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\bin\stock-background-host.exe') -Force
    Assert-True ((Get-StockTunnelReinstallDecision -CurrentHashes $oldHashes `
        -NewHashes (Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new')) `
        -TaskState 'Running' -RuntimeStatus 'healthy' -RemoteHealthStatus '200').decision -eq 'reinstall') `
        'a release without the background host executable must force a tunnel reinstall'
    [IO.File]::WriteAllText((Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\bin\stock-background-host.exe'), 'rebuilt', [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path (Join-Path $hashSandbox 'new') 'scripts\shared-peer\start-shared-tunnels.ps1'), 'changed', [Text.UTF8Encoding]::new($false))
    $newHashes = Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new')
    Assert-True ((Get-StockTunnelReinstallDecision -CurrentHashes $oldHashes -NewHashes $newHashes `
        -TaskState 'Running' -RuntimeStatus 'healthy' -RemoteHealthStatus '200').decision -eq 'reinstall') `
        'a real byte difference in a tunnel file must be caught by the SHA-256 comparison'
    Assert-True ((Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'absent'))['scripts\shared-peer\start-shared-tunnels.ps1'] -eq 'missing') `
        'a tunnel file that does not exist must hash as "missing", never be silently omitted'
} finally {
    $resolvedHashSandbox = [IO.Path]::GetFullPath($hashSandbox)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ($resolvedHashSandbox.StartsWith($tempRoot + '\trading-hareness-release-safety-', [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedHashSandbox -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --- both release scripts must consult the gate before stopping the tunnel ---
$tunnelStop = "Stop-ScheduledTask -TaskName 'trading-hareness-shared-peer-tunnels'"

$publishGateIndex = $publishSource.IndexOf('Resolve-StockTunnelReinstallPlan -PlatformRoot')
$publishStopIndex = $publishSource.IndexOf('Stop-ProductionRuntime -RuntimeRoot $fallbackRoot')
Assert-True ($publishGateIndex -ge 0) 'publish-stock-release.ps1 must evaluate the tunnel reinstall gate'
Assert-True ($publishStopIndex -ge 0) 'publish-stock-release.ps1 must still stop the old runtime on activation'
Assert-True ($publishGateIndex -lt $publishStopIndex) `
    'publish-stock-release.ps1 must evaluate the tunnel reinstall gate before stopping the runtime; the task state, runtime state and remote probe are only meaningful while the current release is still live'
Assert-True ($stopBody -match ('(?s)if \(-not \$KeepTunnelTask\) \{\s*' + [regex]::Escape($tunnelStop))) `
    'publish-stock-release.ps1 must only stop the shared-peer tunnel task when the gate asked for a reinstall'
Assert-True ($publishSource.Contains('Stop-ProductionRuntime -RuntimeRoot $fallbackRoot -KeepTunnelTask:$keepTunnel')) `
    'publish-stock-release.ps1 must forward the gate decision to Stop-ProductionRuntime'
Assert-True ($publishSource.Contains('Start-ProductionRuntime -RuntimeRoot $layout.CurrentPath -KeepTunnelTask:$keepTunnel')) `
    'publish-stock-release.ps1 must forward the gate decision to Start-ProductionRuntime so the installer is skipped too'
Assert-True ($publishSource.Contains('Write-StockTunnelReinstallSkipEvent')) `
    'publish-stock-release.ps1 must record a tunnel_reinstall_skipped runtime event when it skips'

$switchScript = Join-Path $windowsScripts 'switch-stock-release.ps1'
$switchSource = Get-Content -LiteralPath $switchScript -Raw -Encoding UTF8
$switchGateIndex = $switchSource.IndexOf('Resolve-TunnelGate -NewRuntimeRoot $target')
$switchStopIndex = $switchSource.IndexOf($tunnelStop)
Assert-True ($switchGateIndex -ge 0) 'switch-stock-release.ps1 must evaluate the tunnel reinstall gate'
Assert-True ($switchStopIndex -ge 0) 'switch-stock-release.ps1 must still be able to stop the tunnel task'
Assert-True ($switchGateIndex -lt $switchStopIndex) `
    'switch-stock-release.ps1 must evaluate the tunnel reinstall gate before Stop-ScheduledTask of the tunnel task'
Assert-True ($switchSource.Contains('if (-not $keepTunnel) { ' + $tunnelStop)) `
    'switch-stock-release.ps1 must only stop the shared-peer tunnel task when the gate asked for a reinstall'
Assert-True ($switchSource.Contains('Resolve-TunnelGate -NewRuntimeRoot $revertTarget')) `
    'switch-stock-release.ps1 must apply the same gate on the rollback/revert path'
Assert-True ($switchSource.Contains('Write-StockTunnelReinstallSkipEvent')) `
    'switch-stock-release.ps1 must record a tunnel_reinstall_skipped runtime event when it skips'

[pscustomobject]@{
    passed = $true
    sha256_rejects_tampered_file = $true
    sha256_rejects_missing_manifest = $true
    failed_release_excluded_from_retention = $true
    stop_order_is_graceful_before_scheduled_task = $true
    dev_checkout_fallback_removed = $true
    detached_head_source_supported = $true
    concurrent_publish_serialized = $true
    tunnel_reinstall_gate_fails_closed = $true
    tunnel_reinstall_gate_wired_into_publish_and_switch = $true
}
