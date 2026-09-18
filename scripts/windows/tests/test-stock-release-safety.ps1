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
    'scripts\windows\supervise-runtime-process.ps1'      = 'hh'
    'scripts\windows\background-task-host.cs'            = 'ee'
    'scripts\windows\process-lifetime.cs'                = 'ff'
    'scripts\windows\build-background-task-host.ps1'     = 'gg'
    'scripts\windows\bin\stock-background-host.exe'      = 'present'
    # Not a release file: the SHA-256 of the resolved owner-tunnel SSH target.
    'config:owner_tunnel_ssh_target'                     = 'ss'
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
        TunnelReleaseState = 'retained'
        TaskActionUnderCurrent = 'yes'
    }
    foreach ($key in $Arguments.Keys) { $call[$key] = $Arguments[$key] }
    return Get-StockTunnelReinstallDecision @call
}

# --- the file list must BE the execution chain, not a hand-maintained guess ---
# Derived by parsing the chain the scheduled task actually runs:
#   task action (stock-background-host.exe, registered by
#   install-shared-tunnel-task.ps1) -> the executable's build inputs ->
#   start-shared-tunnels.ps1 -> its Import-Module targets ->
#   Start-RuntimeSupervisor -> supervise-runtime-process.ps1 -> its
#   Import-Module / Add-Type targets.
# Asserting equality (not containment) is what makes an omission fail: the
# previous version of this test compared the module's list against a copy of
# itself and was structurally incapable of noticing that
# supervise-runtime-process.ps1 -- the process that owns the tunnel's lock,
# lifetime job and every state transition -- was missing.
$repositoryRoot = [IO.Path]::GetFullPath((Join-Path $windowsScripts '..\..')).TrimEnd('\')
$chain = Get-StockTunnelExecutionChainFile -RuntimeRoot $repositoryRoot
$declaredHashed = @(Get-StockTunnelAffectingFile) | Sort-Object
$declaredPresence = @(Get-StockTunnelPresenceOnlyFile) | Sort-Object
Assert-True (($chain.Hashed -join '|') -eq ($declaredHashed -join '|')) `
    ("the hashed tunnel-affecting file list must equal the parsed execution chain; declared [" +
     ($declaredHashed -join ', ') + '] vs chain [' + ($chain.Hashed -join ', ') + ']')
Assert-True (($chain.PresenceOnly -join '|') -eq ($declaredPresence -join '|')) `
    'the presence-only tunnel file list must equal the executables found on the parsed execution chain'
Assert-True ($chain.Hashed -contains 'scripts\windows\supervise-runtime-process.ps1') `
    'the execution chain must reach supervise-runtime-process.ps1, the process the tunnel task actually runs'
Assert-True ($chain.Hashed -contains 'scripts\windows\process-lifetime.cs') `
    'the execution chain must reach process-lifetime.cs, which supervise-runtime-process.ps1 Add-Types'

# The gate must keep covering every input the running tunnel uses.
$tunnelFiles = @(Get-StockTunnelAffectingFile -IncludePresenceOnly)
foreach ($required in $healthyHashes.Keys) {
    if ($required -eq 'config:owner_tunnel_ssh_target') { continue }
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

# Every condition holds: the only case that may skip.
$skip = Get-Decision
Assert-True ($skip.decision -eq 'skip') 'identical tunnel files + Running task under current + healthy runtime state + remote HTTP 200 + a retained tunnel release must skip the stop/reinstall'
Assert-True (@($skip.changed_files).Count -eq 0) 'a skip must report no changed tunnel files'

# The tunnel's SSH identity is not in the release tree: rotating the owner
# tunnel key/host/port changes nothing about the bytes of a release, but the
# running ssh client would still be connected the old way.
$rotated = Get-Decision @{ NewHashes = (New-HashSet @{ 'config:owner_tunnel_ssh_target' = 'rotated' }) }
Assert-True ($rotated.decision -eq 'reinstall') 'a rotated owner-tunnel SSH target must force the tunnel reinstall'
Assert-True ($rotated.changed_files -contains 'config:owner_tunnel_ssh_target') 'the decision must name the SSH target as changed'
foreach ($unreadable in 'unresolved', 'missing') {
    $unknownTarget = Get-Decision @{ NewHashes = (New-HashSet @{ 'config:owner_tunnel_ssh_target' = $unreadable })
                                     CurrentHashes = (New-HashSet @{ 'config:owner_tunnel_ssh_target' = $unreadable }) }
    Assert-True ($unknownTarget.decision -eq 'reinstall') `
        "an SSH target that resolved to '$unreadable' must force the tunnel reinstall even when both sides agree"
}

# The release the tunnel is really executing out of must still be there, and
# must still be pinned by retention: otherwise the skip outlives its own tree.
foreach ($tunnelState in 'unknown', 'missing', 'not_retained', '') {
    $pin = Get-Decision @{ TunnelReleaseState = $tunnelState }
    Assert-True ($pin.decision -eq 'reinstall') "tunnel_release state '$tunnelState' must force the tunnel reinstall"
}
Assert-True ((Get-Decision @{ TunnelReleaseState = 'unknown' }).reasons -contains 'tunnel_release_unknown') `
    'an unknown tunnel_release must be recorded as its own reason'
Assert-True ((Get-Decision @{ TunnelReleaseState = 'not_retained' }).reasons -contains 'tunnel_release_not_retained') `
    'a tunnel_release outside the retention keep set must be recorded as its own reason'

# A task pinned to a developer checkout is only healed by a reinstall.
foreach ($placement in 'no', 'unknown', '') {
    $placed = Get-Decision @{ TaskActionUnderCurrent = $placement }
    Assert-True ($placed.decision -eq 'reinstall') "task action placement '$placement' must force the tunnel reinstall"
    Assert-True ($placed.reasons -contains 'task_action_not_under_current') 'the decision must record why the task action was rejected'
}

# Every individual condition failing must force a reinstall.
foreach ($file in @($healthyHashes.Keys)) {
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
    function Get-TreeDecision([hashtable]$Current, [hashtable]$New) {
        return Get-StockTunnelReinstallDecision -CurrentHashes $Current -NewHashes $New `
            -TaskState 'Running' -RuntimeStatus 'healthy' -RemoteHealthStatus '200' `
            -TunnelReleaseState 'retained' -TaskActionUnderCurrent 'yes'
    }
    $oldHashes = Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'old')
    $newHashes = Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new')
    Assert-True ((Get-TreeDecision $oldHashes $newHashes).decision -eq 'skip') `
        'byte-identical release trees must hash identically and skip'
    # A byte-different recompile of the host executable alone must NOT force a
    # reinstall; that is the whole reason it is presence-checked.
    [IO.File]::WriteAllText((Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\bin\stock-background-host.exe'), 'a different build of identical sources', [Text.UTF8Encoding]::new($false))
    Assert-True ((Get-TreeDecision $oldHashes (Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new'))).decision -eq 'skip') `
        'a recompiled but functionally identical background host executable must not force a tunnel reinstall'
    # Deleting it entirely must.
    Remove-Item -LiteralPath (Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\bin\stock-background-host.exe') -Force
    Assert-True ((Get-TreeDecision $oldHashes (Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new'))).decision -eq 'reinstall') `
        'a release without the background host executable must force a tunnel reinstall'
    [IO.File]::WriteAllText((Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\bin\stock-background-host.exe'), 'rebuilt', [Text.UTF8Encoding]::new($false))
    # Exactly the regression the old file list missed: only the supervisor
    # script differs, and the tunnel's whole lifecycle lives in that script.
    [IO.File]::WriteAllText((Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\supervise-runtime-process.ps1'), 'changed supervisor', [Text.UTF8Encoding]::new($false))
    $supervisorOnly = Get-TreeDecision $oldHashes (Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new'))
    Assert-True ($supervisorOnly.decision -eq 'reinstall') `
        'a publish that changes only supervise-runtime-process.ps1 must force the tunnel reinstall'
    Assert-True ($supervisorOnly.changed_files -contains 'scripts\windows\supervise-runtime-process.ps1') `
        'the decision must name supervise-runtime-process.ps1 as changed'
    [IO.File]::WriteAllText((Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\supervise-runtime-process.ps1'), "content of scripts\windows\supervise-runtime-process.ps1", [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path (Join-Path $hashSandbox 'new') 'scripts\shared-peer\start-shared-tunnels.ps1'), 'changed', [Text.UTF8Encoding]::new($false))
    $newHashes = Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new')
    Assert-True ((Get-TreeDecision $oldHashes $newHashes).decision -eq 'reinstall') `
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

# --- retention must pin the release the live tunnel is executing out of ------
# Without this, three consecutive skipped publishes leave the tunnel's
# supervisor, background host and ssh client running out of R0 while the keep
# set is {R3, R2, R1}; Remove-ExpiredStockReleases then deletes R0 underneath
# them, and the resulting Remove-Item failure is swallowed as a warning.
$pinned = Get-StockReleaseRetentionPlan -ReleaseNames @('rel-003', 'rel-002', 'rel-001', 'rel-000') `
    -ActiveRelease 'rel-003' -PreviousRelease 'rel-002' -TunnelRelease 'rel-000' -RetainCount 3
Assert-True ($pinned.Keep -contains 'rel-000') 'retention must keep the release the shared-peer tunnel is running from'
Assert-True (-not ($pinned.Remove -contains 'rel-000')) 'retention must never remove the tunnel release'
Assert-True ($pinned.Remove -contains 'rel-001') 'retention must still prune releases nothing is pinned to'
$unpinned = Get-StockReleaseRetentionPlan -ReleaseNames @('rel-003', 'rel-002', 'rel-001', 'rel-000') `
    -ActiveRelease 'rel-003' -PreviousRelease 'rel-002' -RetainCount 3
Assert-True ($unpinned.Remove -contains 'rel-000') 'with no tunnel pin the old policy is unchanged'

$retentionSandbox = Join-Path ([IO.Path]::GetTempPath()) "trading-hareness-release-safety-$([Guid]::NewGuid().ToString('N'))"
try {
    foreach ($release in 'rel-000', 'rel-001', 'rel-002', 'rel-003') {
        New-Item -ItemType Directory -Force -Path (Join-Path (Join-Path $retentionSandbox 'releases') "$release\app") | Out-Null
        Start-Sleep -Milliseconds 20
    }
    [void](Set-StockCurrentRelease -PlatformRoot $retentionSandbox -ReleaseId 'rel-003')
    [void](Set-StockReleaseState -PlatformRoot $retentionSandbox -State @{
        active_release = 'rel-003'; previous_release = 'rel-002'; tunnel_release = 'rel-000' })
    $pruned = @(Remove-ExpiredStockReleases -PlatformRoot $retentionSandbox -RetainCount 3)
    Assert-True (-not ($pruned -contains 'rel-000')) 'Remove-ExpiredStockReleases must not delete the pinned tunnel release'
    Assert-True (Test-Path -LiteralPath (Join-Path $retentionSandbox 'releases\rel-000')) 'the pinned tunnel release directory must survive retention'
    Assert-True ($pruned -contains 'rel-001') 'Remove-ExpiredStockReleases must still prune unpinned old releases'
    # An unrelated state write must not silently drop the pin, or retention
    # would delete the live tunnel's tree on the very next publish.
    [void](Set-StockReleaseState -PlatformRoot $retentionSandbox -State @{ active_release = 'rel-003'; previous_release = 'rel-002' })
    $carried = Get-StockReleaseState -PlatformRoot $retentionSandbox
    Assert-True ([string]$carried.tunnel_release -eq 'rel-000') 'tunnel_release must survive a state write that does not mention it'
} finally {
    $resolvedRetention = [IO.Path]::GetFullPath($retentionSandbox)
    $tempRetention = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ($resolvedRetention.StartsWith($tempRetention + '\trading-hareness-release-safety-', [StringComparison]::OrdinalIgnoreCase)) {
        $currentLink = Join-Path $retentionSandbox 'current'
        if (Test-Path -LiteralPath $currentLink) { Remove-Item -LiteralPath $currentLink -Force -ErrorAction SilentlyContinue }
        Remove-Item -LiteralPath $resolvedRetention -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# --- the registered task action must resolve under <PlatformRoot>\current ----
# install-shared-tunnel-task.ps1 derives the repository from -ScriptPath, so
# running the documented manual recovery step from F:\AIWorkflow\trading_hareness
# registers the task against that checkout. Publishing must reinstall in that
# case; skipping is exactly what would stop it being healed.
$currentPrefix = 'G:\StockPlatform\current'
$goodArguments = '"C:\Program Files\PowerShell\7\pwsh.exe" "G:\StockPlatform\current\scripts\shared-peer\start-shared-tunnels.ps1" "-PlatformRoot" "G:\StockPlatform"'
Assert-True ((Get-StockTunnelTaskActionPlacement -Execute 'G:\StockPlatform\current\scripts\windows\bin\stock-background-host.exe' `
    -Arguments $goodArguments -ExpectedPrefix $currentPrefix) -eq 'yes') `
    'a task whose host and script both live under <PlatformRoot>\current must be accepted'
Assert-True ((Get-StockTunnelTaskActionPlacement -Execute 'F:\AIWorkflow\trading_hareness\scripts\windows\bin\stock-background-host.exe' `
    -Arguments $goodArguments -ExpectedPrefix $currentPrefix) -eq 'no') `
    'a task host pinned to a developer checkout must be rejected'
Assert-True ((Get-StockTunnelTaskActionPlacement -Execute 'G:\StockPlatform\current\scripts\windows\bin\stock-background-host.exe' `
    -Arguments '"C:\Program Files\PowerShell\7\pwsh.exe" "F:\AIWorkflow\trading_hareness\scripts\shared-peer\start-shared-tunnels.ps1"' `
    -ExpectedPrefix $currentPrefix) -eq 'no') `
    'a task script pinned to a developer checkout must be rejected even when the host is under current'
Assert-True ((Get-StockTunnelTaskActionPlacement -Execute 'G:\StockPlatform\current\scripts\windows\bin\stock-background-host.exe' `
    -Arguments '"C:\Program Files\PowerShell\7\pwsh.exe"' -ExpectedPrefix $currentPrefix) -eq 'no') `
    'a task action that names no script at all must be rejected'
Assert-True ((Get-StockTunnelTaskActionPlacement -Execute '' -Arguments '' -ExpectedPrefix $currentPrefix) -eq 'unknown') `
    'an unreadable task action must be reported as unknown, which the decision treats as a reinstall'
Assert-True ((Get-Decision @{ TaskActionUnderCurrent = 'unknown' }).decision -eq 'reinstall') `
    'an unknown task action placement must force the tunnel reinstall'

# --- Resolve-StockTunnelReinstallPlan itself, against a temp platform root ---
# The I/O half used to be untested: this drives it with injected task/probe
# seams so the state-file, tunnel-pin and probe short-circuit paths are real.
$planSandbox = Join-Path ([IO.Path]::GetTempPath()) "trading-hareness-release-safety-$([Guid]::NewGuid().ToString('N'))"
try {
    $releasesRoot = Join-Path $planSandbox 'releases'
    foreach ($release in 'rel-000', 'rel-001') {
        foreach ($relative in @(Get-StockTunnelAffectingFile -IncludePresenceOnly)) {
            $path = Join-Path (Join-Path $releasesRoot "$release\app") $relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $path) | Out-Null
            [IO.File]::WriteAllText($path, "content of $relative", [Text.UTF8Encoding]::new($false))
        }
        Start-Sleep -Milliseconds 20
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $planSandbox 'logs\runtime') | Out-Null
    $runtimeStatePath = Join-Path $planSandbox 'logs\runtime\shared-peer-tunnels.current.json'
    [IO.File]::WriteAllText($runtimeStatePath, '{"status":"healthy"}', [Text.UTF8Encoding]::new($false))
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{
        active_release = 'rel-000'; previous_release = $null; tunnel_release = 'rel-000'
        tunnel_ssh_target_sha256 = (Get-StockTunnelSshTargetHash -PlatformRoot $planSandbox) })
    $newApp = Join-Path $releasesRoot 'rel-001\app'
    $script:probeCalls = 0
    $runningTask = { param($Name) [pscustomobject]@{
        State = 'Running'
        Execute = (Join-Path $planSandbox 'current\scripts\windows\bin\stock-background-host.exe')
        Arguments = ('"pwsh.exe" "' + (Join-Path $planSandbox 'current\scripts\shared-peer\start-shared-tunnels.ps1') + '"')
    } }
    $probe200 = { $script:probeCalls++; '200' }
    $resolveArgs = @{
        PlatformRoot = $planSandbox
        NewRuntimeRoot = $newApp
        ScheduledTaskProvider = $runningTask
        RemoteHealthProbe = $probe200
    }
    $livePlan = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($livePlan.decision -eq 'skip') 'Resolve-StockTunnelReinstallPlan must skip when every real observation holds'
    Assert-True ($livePlan.tunnel_release -eq 'rel-000') 'the plan must report which release the tunnel is running from'
    Assert-True ($livePlan.tunnel_release_state -eq 'retained') 'a pinned, on-disk tunnel release must be reported as retained'
    Assert-True ($script:probeCalls -eq 1) 'the expensive SSH probe must be issued exactly once when the cheap observations pass'

    # A stopped task must not even pay for the probe.
    $script:probeCalls = 0
    $stopped = Resolve-StockTunnelReinstallPlan @resolveArgs -ScheduledTaskProvider { param($Name) [pscustomobject]@{ State = 'Ready'; Execute = ''; Arguments = '' } }
    Assert-True ($stopped.decision -eq 'reinstall') 'a task that is not Running must force the tunnel reinstall'
    Assert-True ($script:probeCalls -eq 0) 'the SSH probe must never be issued when the cheap observations already force a reinstall'

    # No registered task at all.
    $absent = Resolve-StockTunnelReinstallPlan @resolveArgs -ScheduledTaskProvider { param($Name) $null }
    Assert-True ($absent.decision -eq 'reinstall') 'an unregistered tunnel task must force the tunnel reinstall'
    Assert-True ($absent.reasons -contains 'task_not_running') 'the decision must record that the task was not running'

    # A task action pointing at a developer checkout.
    $strayTask = { param($Name) [pscustomobject]@{
        State = 'Running'
        Execute = 'F:\AIWorkflow\trading_hareness\scripts\windows\bin\stock-background-host.exe'
        Arguments = '"pwsh.exe" "F:\AIWorkflow\trading_hareness\scripts\shared-peer\start-shared-tunnels.ps1"'
    } }
    $stray = Resolve-StockTunnelReinstallPlan @resolveArgs -ScheduledTaskProvider $strayTask
    Assert-True ($stray.decision -eq 'reinstall') 'a tunnel task running out of a developer checkout must force the tunnel reinstall'
    Assert-True ($stray.reasons -contains 'task_action_not_under_current') 'the decision must record the stray task action'

    # A corrupt runtime state file must not be read as healthy.
    [IO.File]::WriteAllText($runtimeStatePath, '{not json', [Text.UTF8Encoding]::new($false))
    $corrupt = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($corrupt.decision -eq 'reinstall') 'an unreadable runtime state file must force the tunnel reinstall'
    Assert-True ($corrupt.runtime_status -eq 'unreadable') 'an unreadable runtime state file must be reported as such'
    Remove-Item -LiteralPath $runtimeStatePath -Force
    $noState = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($noState.decision -eq 'reinstall') 'a missing runtime state file must force the tunnel reinstall'
    [IO.File]::WriteAllText($runtimeStatePath, '{"status":"healthy"}', [Text.UTF8Encoding]::new($false))

    # The pin itself: an unknown, deleted or unretained tunnel release.
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{ active_release = 'rel-000'; tunnel_release = $null })
    $unknownPin = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($unknownPin.decision -eq 'reinstall') 'an unknown tunnel_release must force the tunnel reinstall'
    Assert-True ($unknownPin.reasons -contains 'tunnel_release_unknown') 'the decision must record an unknown tunnel_release'
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{ active_release = 'rel-000'; tunnel_release = 'rel-gone' })
    $missingPin = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($missingPin.decision -eq 'reinstall') 'a tunnel_release that is no longer on disk must force the tunnel reinstall'
    Assert-True ($missingPin.reasons -contains 'tunnel_release_missing') 'the decision must record a pruned tunnel_release'

    # A rotated SSH identity, with the release tree byte-identical.
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{
        active_release = 'rel-000'; tunnel_release = 'rel-000'; tunnel_ssh_target_sha256 = ('0' * 64) })
    $rotatedTarget = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($rotatedTarget.decision -eq 'reinstall') `
        'an owner-tunnel SSH identity that no longer matches the one recorded at the last reinstall must force the tunnel reinstall'
    Assert-True ($rotatedTarget.changed_files -contains 'config:owner_tunnel_ssh_target') `
        'the plan must name the SSH target as the changed input'
} finally {
    $resolvedPlanSandbox = [IO.Path]::GetFullPath($planSandbox)
    $tempPlanRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ($resolvedPlanSandbox.StartsWith($tempPlanRoot + '\trading-hareness-release-safety-', [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedPlanSandbox -Recurse -Force -ErrorAction SilentlyContinue
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
Assert-True ($publishSource.Contains('-TunnelReleaseId $previousTunnelRelease')) `
    'publish-stock-release.ps1 must compare the new tree against the release the tunnel is really running from, not against `current`'
$publishHealthIndex = $publishSource.IndexOf('$healthVerification = Wait-ProductionHealth')
$publishSkipEventIndex = $publishSource.IndexOf('Write-StockTunnelReinstallSkipEvent -PlatformRoot')
Assert-True ($publishHealthIndex -ge 0 -and $publishSkipEventIndex -gt $publishHealthIndex) `
    'publish-stock-release.ps1 must write the tunnel_reinstall_skipped event only after activation and health verification, so a rolled-back publish cannot leave a receipt its own rollback contradicts'
Assert-True ($publishSource.Contains('Install-SharedPeerTunnelTask -RuntimeRoot $layout.CurrentPath')) `
    'publish-stock-release.ps1 must reinstall the shared-peer tunnel when post-switch shared-runtime verification is degraded after a skip'
Assert-True ($publishSource -match "(?s)if \(\`$keepTunnel -and \[string\]\`$healthVerification\.shared_runtime -ne 'ok'\)") `
    'publish-stock-release.ps1 must treat a degraded shared runtime as a trigger to reinstall the tunnel the gate spared'
Assert-True ($publishSource.Contains('tunnel_release = $tunnelReleaseAfter')) `
    'publish-stock-release.ps1 must persist the release the tunnel is running from in release-state.json'

$switchScript = Join-Path $windowsScripts 'switch-stock-release.ps1'
$switchSource = Get-Content -LiteralPath $switchScript -Raw -Encoding UTF8
$switchGateIndex = $switchSource.IndexOf('$tunnelPlan = Resolve-TunnelGate -NewRuntimeRoot $target')
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
Assert-True ($switchSource.Contains('-TunnelReleaseId $tunnelRelease')) `
    'switch-stock-release.ps1 must compare against the release the tunnel is really running from'
$switchVerifyIndex = $switchSource.IndexOf('verify-shared-runtime.ps1')
$switchSkipEventIndex = $switchSource.IndexOf('Write-StockTunnelReinstallSkipEvent -PlatformRoot')
Assert-True ($switchVerifyIndex -ge 0 -and $switchSkipEventIndex -gt $switchVerifyIndex) `
    'switch-stock-release.ps1 must write the tunnel_reinstall_skipped event only after shared-runtime verification succeeds'
Assert-True ($switchSource.Contains("`$tunnelOutcome = 'reinstalled_after_degraded_verification'")) `
    'switch-stock-release.ps1 must reinstall the spared tunnel and re-verify when shared-runtime verification fails after a skip'
Assert-True ($switchSource.Contains('tunnel_release = if ($tunnelRelease)')) `
    'switch-stock-release.ps1 must persist the release the tunnel is running from'

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
    tunnel_file_list_equals_parsed_execution_chain = $true
    tunnel_release_pinned_against_retention = $true
    tunnel_ssh_target_rotation_forces_reinstall = $true
    tunnel_task_action_must_run_under_current = $true
    tunnel_skip_event_written_only_after_verification = $true
}
