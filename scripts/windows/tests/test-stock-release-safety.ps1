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
# Get-InstalledPowerShell is what New-HiddenPowerShellTaskAction bakes into the
# registered task action, and what the gate's 'config:task_host_pwsh' entry
# hashes; the plan test below has to register the same value.
Import-Module (Join-Path $windowsScripts 'background-process.psm1') -Force

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
    # Not release files: the SHA-256 of the resolved owner-tunnel SSH target,
    # of the PowerShell host baked into the registered task action, and of the
    # task principal (LogonType + UserId) a reinstall would register.
    'config:owner_tunnel_ssh_target'                     = 'ss'
    'config:task_host_pwsh'                              = 'pp'
    'config:tunnel_task_principal'                       = 'nn'
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
        # The `current` junction's tree: identical here, because before any
        # publish has skipped the pinned tree and `current` are the same tree.
        JunctionHashes = (New-HashSet)
        TaskState = 'Running'
        RuntimeStatus = 'healthy'
        RemoteHealthStatus = '200'
        TunnelReleaseState = 'retained'
        TaskActionUnderCurrent = 'yes'
        TaskActionPathState = 'ok'
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

# An expandable-string import ("$PSScriptRoot\x.psm1") is not a
# StringConstantExpressionAst. A walk that only visits constants would not see
# it, the parsed chain would still equal the declared list, this test would pass
# green, and the gate would under-detect a change to that file -- precisely the
# failure the parser exists to prevent.
$chainSandbox = Join-Path ([IO.Path]::GetTempPath()) "trading-hareness-release-safety-$([Guid]::NewGuid().ToString('N'))"
try {
    $chainScripts = Join-Path $chainSandbox 'scripts\windows'
    New-Item -ItemType Directory -Force -Path $chainScripts | Out-Null
    [IO.File]::WriteAllText((Join-Path $chainScripts 'chained.psm1'), '# reached only through an expandable string', [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $chainScripts 'entry.ps1'),
        'Import-Module "$PSScriptRoot\chained.psm1" -Force', [Text.UTF8Encoding]::new($false))
    $expandable = Get-StockTunnelExecutionChainFile -RuntimeRoot $chainSandbox -EntryPoint @('scripts\windows\entry.ps1')
    Assert-True ($expandable.Hashed -contains 'scripts\windows\chained.psm1') `
        'the execution chain parser must follow an expandable-string import, not only plain string literals'
    [IO.File]::WriteAllText((Join-Path $chainScripts 'entry.ps1'),
        '& "$env:SOMEWHERE\mystery.ps1"', [Text.UTF8Encoding]::new($false))
    Assert-Throws { Get-StockTunnelExecutionChainFile -RuntimeRoot $chainSandbox -EntryPoint @('scripts\windows\entry.ps1') } `
        'a path expression the parser cannot resolve statically must throw loudly instead of silently dropping a chain element'

    # A literal naming a subdirectory of the MENTIONING file's directory
    # (Join-Path $PSScriptRoot 'sub\helper.psm1') used to be read as
    # release-root relative only, resolve nowhere, and be dropped in silence:
    # the parsed chain still equalled the declared list, the equality assertion
    # above still passed, and the gate never hashed a file the tunnel executes.
    $chainShared = Join-Path $chainSandbox 'scripts\shared-peer'
    New-Item -ItemType Directory -Force -Path (Join-Path $chainShared 'sub') | Out-Null
    [IO.File]::WriteAllText((Join-Path $chainShared 'sub\helper.psm1'), '# reached only beside its mentioning file', [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $chainShared 'entry2.ps1'),
        "Import-Module (Join-Path `$PSScriptRoot 'sub\helper.psm1')", [Text.UTF8Encoding]::new($false))
    $relative = Get-StockTunnelExecutionChainFile -RuntimeRoot $chainSandbox -EntryPoint @('scripts\shared-peer\entry2.ps1')
    Assert-True ($relative.Hashed -contains 'scripts\shared-peer\sub\helper.psm1') `
        'a path literal naming a subdirectory must be resolved relative to the directory of the file that mentions it, not only against the release root'
    # Root-relative keeps precedence, so the historical reading is unchanged.
    [IO.File]::WriteAllText((Join-Path $chainScripts 'entry.ps1'),
        "& 'scripts\windows\chained.psm1'", [Text.UTF8Encoding]::new($false))
    $rootRelative = Get-StockTunnelExecutionChainFile -RuntimeRoot $chainSandbox -EntryPoint @('scripts\windows\entry.ps1')
    Assert-True ($rootRelative.Hashed -contains 'scripts\windows\chained.psm1') `
        'a release-root-relative path literal must keep resolving against the release root'
    # And a spelled-out path that resolves in NEITHER place is a chain element
    # this tree does not carry: loud, not dropped.
    [IO.File]::WriteAllText((Join-Path $chainScripts 'entry.ps1'),
        "& 'scripts\windows\not-here.ps1'", [Text.UTF8Encoding]::new($false))
    Assert-Throws { Get-StockTunnelExecutionChainFile -RuntimeRoot $chainSandbox -EntryPoint @('scripts\windows\entry.ps1') } `
        'a path literal that resolves neither at the release root nor beside the mentioning file must throw instead of leaving the gate a file list that is quietly missing it'
    # A bare name with no separator stays lenient: it is as likely to be a
    # message or a build-script output as a path into the tree.
    [IO.File]::WriteAllText((Join-Path $chainScripts 'entry.ps1'),
        "Write-Verbose 'rebuilt by build-something.ps1'", [Text.UTF8Encoding]::new($false))
    $bare = Get-StockTunnelExecutionChainFile -RuntimeRoot $chainSandbox -EntryPoint @('scripts\windows\entry.ps1')
    Assert-True (@($bare.Hashed).Count -eq 1) `
        'a bare file name that names nothing in the tree must be dropped quietly, not turned into a hard failure'

    # '/' and '\' are the same separator to PowerShell. Keyed on '\' alone, a
    # forward-slash literal was tried ONLY beside the mentioning file and then
    # dropped in silence -- the exact under-detection the loud rule was written
    # to end -- so the root-relative reading must cover either spelling.
    [IO.File]::WriteAllText((Join-Path $chainShared 'forward.ps1'),
        "Import-Module 'scripts/windows/chained.psm1'", [Text.UTF8Encoding]::new($false))
    $forward = Get-StockTunnelExecutionChainFile -RuntimeRoot $chainSandbox -EntryPoint @('scripts\shared-peer\forward.ps1')
    Assert-True ($forward.Hashed -contains 'scripts\windows\chained.psm1') `
        'a release-root-relative path literal spelled with forward slashes must resolve exactly like one spelled with backslashes'
    # ...and so must the loud rule, or a forward-slash dead path is the one
    # spelling that still disappears quietly.
    [IO.File]::WriteAllText((Join-Path $chainShared 'forward.ps1'),
        "Import-Module 'scripts/windows/not-here.psm1'", [Text.UTF8Encoding]::new($false))
    Assert-Throws { Get-StockTunnelExecutionChainFile -RuntimeRoot $chainSandbox -EntryPoint @('scripts\shared-peer\forward.ps1') } `
        'a forward-slash path literal that resolves nowhere must throw like its backslash spelling, not be dropped'
    Remove-Item -LiteralPath (Join-Path $chainShared 'forward.ps1') -Force

    # A parent-relative literal resolves, but its raw spelling carries '..' and
    # is a SECOND key for a file the declared list already names under its
    # canonical one: the equality assertion would fail on a spelling, or the
    # chain would hold the same file twice.
    [IO.File]::WriteAllText((Join-Path $chainShared 'parentrel.ps1'),
        "Import-Module (Join-Path `$PSScriptRoot '..\windows\chained.psm1')", [Text.UTF8Encoding]::new($false))
    $normalized = Get-StockTunnelExecutionChainFile -RuntimeRoot $chainSandbox -EntryPoint @('scripts\shared-peer\parentrel.ps1')
    Assert-True ($normalized.Hashed -contains 'scripts\windows\chained.psm1') `
        'a parent-relative path literal must enter the chain under its canonical release-root-relative spelling'
    Assert-True (@($normalized.Hashed | Where-Object { $_ -like '*..*' }).Count -eq 0) `
        'no chain entry may carry a `..` segment, which can never match the declared file list'
    Assert-True (@($normalized.Hashed).Count -eq 2) `
        'a parent-relative literal must not double-count the file it names'
    # And one that climbs out of the release root is not a file this release
    # carries at all, so it is dropped like an absolute path rather than
    # becoming a hard error about a tree the gate does not own.
    [IO.File]::WriteAllText((Join-Path $chainShared 'parentrel.ps1'),
        "Import-Module (Join-Path `$PSScriptRoot '..\..\..\outside\chained.psm1')", [Text.UTF8Encoding]::new($false))
    $escaped = Get-StockTunnelExecutionChainFile -RuntimeRoot $chainSandbox -EntryPoint @('scripts\shared-peer\parentrel.ps1')
    Assert-True (@($escaped.Hashed).Count -eq 1) `
        'a path literal that resolves outside the release root must be dropped quietly, like an absolute path'
    Remove-Item -LiteralPath (Join-Path $chainShared 'parentrel.ps1') -Force

    # The loud rule must fire on PATHS, not on prose. An operator message that
    # merely ends in a path names a file the parsed tree need not carry (a test
    # script, a doc-referenced helper), and turning that into a hard error makes
    # the safety suite fail for a harmless string.
    [IO.File]::WriteAllText((Join-Path $chainScripts 'entry.ps1'),
        "throw 'Run scripts\windows\tests\test-shared-tunnel-recovery.ps1 by hand'" + "`n" +
        "Write-Host 'see docs/SHARED_PEER_RUNTIME.md and scripts/windows/tests/test-runtime-observability.ps1'" + "`n" +
        "Write-Verbose `"rerun `$PSScriptRoot\missing-helper.ps1`"", [Text.UTF8Encoding]::new($false))
    $message = Get-StockTunnelExecutionChainFile -RuntimeRoot $chainSandbox -EntryPoint @('scripts\windows\entry.ps1')
    Assert-True (@($message.Hashed).Count -eq 1) `
        'an operator message that merely ends in a path must be neither hashed nor turned into a hard failure by the loud rule'
} finally {
    $resolvedChain = [IO.Path]::GetFullPath($chainSandbox)
    $tempChain = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ($resolvedChain.StartsWith($tempChain + '\trading-hareness-release-safety-', [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedChain -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# The gate must keep covering every input the running tunnel uses.
$tunnelFiles = @(Get-StockTunnelAffectingFile -IncludePresenceOnly)
foreach ($required in $healthyHashes.Keys) {
    # 'config:' entries are the synthetic ones (SSH target, task host pwsh.exe,
    # task principal): real inputs of the running tunnel that live in no release
    # tree, so they are not part of the file list.
    if ($required.StartsWith('config:')) { continue }
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

# --- the `current` junction is the SECOND tree a skip answers for -------------
# tunnel_release records where the tunnel was last INSTALLED from; the task
# action is <PlatformRoot>\current\..., so the next relaunch -- the 2-minute
# supervising trigger after a drop, a logon, a reboot -- starts from `current`
# instead, without touching release-state.json. Byte-equality against the pinned
# tree alone would let that relaunch silently adopt code the gate never compared.
$junctionDrift = Get-Decision @{ JunctionHashes = (New-HashSet @{ 'scripts\shared-peer\start-shared-tunnels.ps1' = 'drifted' }) }
Assert-True ($junctionDrift.decision -eq 'reinstall') `
    'a `current` junction tree that differs from the new release must force the tunnel reinstall even when the pinned tree matches'
Assert-True ($junctionDrift.reasons -contains 'current_junction_files_changed') `
    'the decision must record that the tree the next relaunch would start from differs'
Assert-True ($junctionDrift.junction_changed_files -contains 'scripts\shared-peer\start-shared-tunnels.ps1') `
    'the decision must name which file differs against the `current` junction'
Assert-True ((Get-Decision @{ JunctionHashes = @{} }).reasons -contains 'no_junction_files_hashed') `
    'an unresolvable `current` junction must force the tunnel reinstall with its own reason'
# The synthetic entries describe the installed task and the resolved SSH
# identity, not a tree, so the junction comparison must ignore them -- otherwise
# every skip would fail on a key the junction can never carry.
$junctionWithoutSynthetic = Get-Decision @{ JunctionHashes = (New-HashSet @{
    'config:owner_tunnel_ssh_target' = $null; 'config:task_host_pwsh' = $null; 'config:tunnel_task_principal' = $null }) }
Assert-True ($junctionWithoutSynthetic.decision -eq 'skip') `
    'the `current` junction comparison must ignore the synthetic config: entries, which no release tree carries'

# --- the registered action's absolute paths must still exist -----------------
# Get-InstalledPowerShell falls back to the Appx package on a host with no MSI
# PowerShell, baking a version-pinned WindowsApps pwsh.exe path into the action;
# the Store deletes that directory when it updates PowerShell and only a
# reinstall re-resolves it.
foreach ($pathState in 'missing', 'unknown', '') {
    $paths = Get-Decision @{ TaskActionPathState = $pathState }
    Assert-True ($paths.decision -eq 'reinstall') "task action path state '$pathState' must force the tunnel reinstall"
    Assert-True ($paths.reasons -contains 'task_action_path_missing') 'the decision must record that the task action names a path that is gone'
}
$actionPaths = @(Get-StockTunnelTaskActionPath `
    -Execute 'G:\StockPlatform\current\scripts\windows\bin\stock-background-host.exe' `
    -Arguments '"C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.6.0_x64__8wekyb3d8bbwe\pwsh.exe" "G:\StockPlatform\current\scripts\shared-peer\start-shared-tunnels.ps1" "-PlatformRoot" "G:\StockPlatform"')
Assert-True ($actionPaths -contains 'C:\Program Files\WindowsApps\Microsoft.PowerShell_7.6.6.0_x64__8wekyb3d8bbwe\pwsh.exe') `
    'the version-pinned WindowsApps pwsh.exe in the action arguments must be one of the paths required to exist'
Assert-True ($actionPaths -contains 'G:\StockPlatform\current\scripts\windows\bin\stock-background-host.exe') `
    'the action Execute path must be one of the paths required to exist'
Assert-True (-not ($actionPaths -contains '-PlatformRoot')) `
    'a flag token is not a path and must not be required to exist'
Assert-True (@(Get-StockTunnelTaskActionPath -Execute '' -Arguments '').Count -eq 0) `
    'an unreadable action names no paths, which the plan reports as unknown'
Assert-True ((Get-StockTunnelTaskActionShell -Arguments '"C:\Program Files\PowerShell\7\pwsh.exe" "x.ps1"') -eq 'C:\Program Files\PowerShell\7\pwsh.exe') `
    'the PowerShell host the action launches must be recoverable from the arguments'
Assert-True ((Get-StockTunnelTaskActionShell -Arguments '"x.ps1"') -eq '') `
    'an action naming no PowerShell host must report none, which hashes as unresolved'
Assert-True ((Get-StockTunnelPathHash -Path 'C:\A\pwsh.exe') -eq (Get-StockTunnelPathHash -Path 'c:\a\PWSH.EXE')) `
    'the task host path hash must not treat a differently-cased identical path as a rotation'
Assert-True ((Get-StockTunnelPathHash -Path 'C:\A\pwsh.exe') -ne (Get-StockTunnelPathHash -Path 'C:\B\pwsh.exe')) `
    'a different PowerShell host path must hash differently'
Assert-True ((Get-StockTunnelPathHash -Path '') -eq 'unresolved') `
    'an unresolvable PowerShell host must hash as unresolved, which the decision treats as a change'

# --- the task principal a reinstall would register ---------------------------
# publish derives S4U when elevated and Interactive otherwise; a skip never
# re-registers the principal, so the gate has to observe it.
Assert-True ((Get-StockTunnelTaskPrincipalHash -LogonType 'Interactive' -UserId 'brave') -eq
             (Get-StockTunnelTaskPrincipalHash -LogonType 'Interactive' -UserId 'HOST\brave')) `
    'Get-ScheduledTask may report a machine-qualified user name; the principal hash must compare the bare name'
Assert-True ((Get-StockTunnelTaskPrincipalHash -LogonType 'S4U' -UserId 'brave') -ne
             (Get-StockTunnelTaskPrincipalHash -LogonType 'Interactive' -UserId 'brave')) `
    'a different LogonType must produce a different principal hash'
Assert-True ((Get-StockTunnelTaskPrincipalHash -LogonType 'S4U' -UserId 'brave') -ne
             (Get-StockTunnelTaskPrincipalHash -LogonType 'S4U' -UserId 'someone-else')) `
    'a different UserId must produce a different principal hash'
foreach ($incomplete in @(@{ LogonType = ''; UserId = 'brave' }, @{ LogonType = 'S4U'; UserId = '' })) {
    Assert-True ((Get-StockTunnelTaskPrincipalHash @incomplete) -eq 'unresolved') `
        'an unreadable principal must hash as unresolved, which the decision treats as a change'
}
$rotatedPrincipal = Get-Decision @{ NewHashes = (New-HashSet @{ 'config:tunnel_task_principal' = 'rotated' }) }
Assert-True ($rotatedPrincipal.decision -eq 'reinstall') `
    'a task principal this publish would register but the registered task does not carry must force the tunnel reinstall'
Assert-True ($rotatedPrincipal.changed_files -contains 'config:tunnel_task_principal') `
    'the decision must name the task principal as the changed input'
$rotatedShell = Get-Decision @{ NewHashes = (New-HashSet @{ 'config:task_host_pwsh' = 'rotated' }) }
Assert-True ($rotatedShell.decision -eq 'reinstall') `
    'a PowerShell host path that no longer matches the registered action must force the tunnel reinstall'
Assert-True ($rotatedShell.changed_files -contains 'config:task_host_pwsh') `
    'the decision must name the task host pwsh.exe as the changed input'
foreach ($syntheticKey in 'config:task_host_pwsh', 'config:tunnel_task_principal') {
    $bothUnresolved = Get-Decision @{ NewHashes = (New-HashSet @{ $syntheticKey = 'unresolved' })
                                      CurrentHashes = (New-HashSet @{ $syntheticKey = 'unresolved' }) }
    Assert-True ($bothUnresolved.decision -eq 'reinstall') `
        "$syntheticKey resolved to 'unresolved' must force the tunnel reinstall even when both sides agree"
}

# --- the background host's build receipt, not bare presence ------------------
# The executable cannot be hashed (csc.exe embeds a fresh module GUID) but bare
# presence accepts a truncated or stale copy: with -SkipTests
# build-background-task-host.ps1 never runs while the publish still copies
# whatever sits in the source tree's bin.
Assert-True ((Test-StockTunnelHashChanged -CurrentValue 'receipt:aa' -NewValue 'receipt:bb')) `
    'a background host built from different sources (different receipt) must count as a change'
Assert-True (-not (Test-StockTunnelHashChanged -CurrentValue 'receipt:aa' -NewValue 'receipt:aa')) `
    'an identical build receipt must not count as a change'
Assert-True ((Test-StockTunnelHashChanged -CurrentValue 'receipt:aa' -NewValue 'receipt_mismatch')) `
    'a receipt that disagrees with the executable on disk must count as a change'
Assert-True ((Test-StockTunnelHashChanged -CurrentValue 'receipt_mismatch' -NewValue 'receipt_mismatch')) `
    'two unreadable build receipts agreeing is not evidence that the build is unchanged'
Assert-True (-not (Test-StockTunnelHashChanged -CurrentValue 'present' -NewValue 'receipt:aa')) `
    'an OLD release published before the build receipt existed must degrade to presence, not force a reinstall forever'
Assert-True ((Test-StockTunnelHashChanged -CurrentValue 'receipt:aa' -NewValue 'present')) `
    'a NEW release that lost its build receipt must NOT be excused by the presence fallback'
Assert-True ((Resolve-StockTunnelHostBuildComparison -CurrentValue 'present' -NewValue 'receipt:aa').New -eq 'present') `
    'the presence fallback must apply to the new side only when the old side has no receipt'
$receiptMismatch = Get-Decision @{ NewHashes = (New-HashSet @{ 'scripts\windows\bin\stock-background-host.exe' = 'receipt_mismatch' })
                                   JunctionHashes = (New-HashSet @{ 'scripts\windows\bin\stock-background-host.exe' = 'receipt_mismatch' }) }
Assert-True ($receiptMismatch.decision -eq 'reinstall') `
    'a background host whose build receipt does not match the file on disk must force the tunnel reinstall'

$receiptSandbox = Join-Path ([IO.Path]::GetTempPath()) "trading-hareness-release-safety-$([Guid]::NewGuid().ToString('N'))"
try {
    $hostRelative = 'scripts\windows\bin\stock-background-host.exe'
    $hostPath = Join-Path $receiptSandbox $hostRelative
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $hostPath) | Out-Null
    [IO.File]::WriteAllText($hostPath, 'MZ-ish build output', [Text.UTF8Encoding]::new($false))
    Assert-True ((Get-StockTunnelHostBuildEvidence -RuntimeRoot $receiptSandbox -Relative $hostRelative) -eq 'present') `
        'an executable with no build receipt beside it must report presence'
    $receiptPath = Join-Path $receiptSandbox 'scripts\windows\bin\stock-background-host.build.json'
    $length = (Get-Item -LiteralPath $hostPath).Length
    [IO.File]::WriteAllText($receiptPath, (@{ schema_version = 1; output_name = 'stock-background-host.exe'
        output_length = $length; inputs = @{ 'process-lifetime.cs' = 'aa' } } | ConvertTo-Json -Depth 5), [Text.UTF8Encoding]::new($false))
    $matched = Get-StockTunnelHostBuildEvidence -RuntimeRoot $receiptSandbox -Relative $hostRelative
    Assert-True ($matched.StartsWith('receipt:')) 'a receipt matching the executable on disk must be hashed'
    # A truncated copy is exactly what bare presence could never catch.
    [IO.File]::WriteAllText($hostPath, 'MZ', [Text.UTF8Encoding]::new($false))
    Assert-True ((Get-StockTunnelHostBuildEvidence -RuntimeRoot $receiptSandbox -Relative $hostRelative) -eq 'receipt_mismatch') `
        'a truncated background host executable must be rejected by its own build receipt, which bare presence could never catch'
    [IO.File]::WriteAllText($receiptPath, 'not json at all', [Text.UTF8Encoding]::new($false))
    Assert-True ((Get-StockTunnelHostBuildEvidence -RuntimeRoot $receiptSandbox -Relative $hostRelative) -eq 'receipt_mismatch') `
        'an unreadable build receipt must be rejected, never silently treated as presence'
    Remove-Item -LiteralPath $hostPath -Force
    Assert-True ((Get-StockTunnelHostBuildEvidence -RuntimeRoot $receiptSandbox -Relative $hostRelative) -eq 'missing') `
        'an absent background host executable must still report missing'
} finally {
    $resolvedReceipt = [IO.Path]::GetFullPath($receiptSandbox)
    $tempReceipt = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ($resolvedReceipt.StartsWith($tempReceipt + '\trading-hareness-release-safety-', [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolvedReceipt -Recurse -Force -ErrorAction SilentlyContinue
    }
}
$buildScript = Get-Content -LiteralPath (Join-Path $windowsScripts 'build-background-task-host.ps1') -Raw -Encoding UTF8
Assert-True ($buildScript.Contains('stock-background-host.build.json')) `
    'build-background-task-host.ps1 must write the build receipt the gate hashes in place of the executable'
Assert-True ($buildScript.Contains('output_length')) `
    'the build receipt must record the output length so a truncated copy cannot pass'
Assert-True ($buildScript -match 'inputs') `
    'the build receipt must record the SHA-256 of its C# inputs'

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
        # Before any publish has skipped, the pinned tree and the `current`
        # junction are the same tree, so the junction baseline is $Current.
        return Get-StockTunnelReinstallDecision -CurrentHashes $Current -NewHashes $New -JunctionHashes $Current `
            -TaskState 'Running' -RuntimeStatus 'healthy' -RemoteHealthStatus '200' `
            -TunnelReleaseState 'retained' -TaskActionUnderCurrent 'yes' -TaskActionPathState 'ok'
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
    # Get-StockTunnelFileHash itself must read the build receipt, not just test
    # for the file's existence: with bare presence a truncated or stale
    # executable is indistinguishable from a good one.
    function Write-HostBuildReceipt([string]$Tree, [string]$Body) {
        $exe = Join-Path (Join-Path $hashSandbox $Tree) 'scripts\windows\bin\stock-background-host.exe'
        [IO.File]::WriteAllText($exe, $Body, [Text.UTF8Encoding]::new($false))
        [IO.File]::WriteAllText((Join-Path (Join-Path $hashSandbox $Tree) 'scripts\windows\bin\stock-background-host.build.json'),
            (@{ schema_version = 1; output_name = 'stock-background-host.exe'
                output_length = (Get-Item -LiteralPath $exe).Length
                inputs = @{ 'process-lifetime.cs' = 'aa'; 'background-task-host.cs' = 'bb' } } | ConvertTo-Json -Depth 5),
            [Text.UTF8Encoding]::new($false))
    }
    # Same length, different bytes: exactly what csc.exe produces from
    # unchanged sources (a fresh module GUID, same size).
    Write-HostBuildReceipt 'old' 'build-one'
    Write-HostBuildReceipt 'new' 'build-two'
    $oldReceiptHashes = Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'old')
    $newReceiptHashes = Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new')
    Assert-True (([string]$oldReceiptHashes['scripts\windows\bin\stock-background-host.exe']).StartsWith('receipt:')) `
        'Get-StockTunnelFileHash must record the background host through its build receipt, not through bare presence'
    Assert-True ((Get-TreeDecision $oldReceiptHashes $newReceiptHashes).decision -eq 'skip') `
        'two non-deterministic builds of identical sources carry identical receipts and must still skip'
    # Truncate the new tree's executable without touching its receipt: exactly
    # the case bare presence accepts and the receipt rejects.
    [IO.File]::WriteAllText((Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\bin\stock-background-host.exe'), 'MZ', [Text.UTF8Encoding]::new($false))
    $truncated = Get-TreeDecision $oldReceiptHashes (Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new'))
    Assert-True ($truncated.decision -eq 'reinstall') `
        'a truncated background host executable must force the tunnel reinstall; bare presence could never catch it'
    Assert-True ($truncated.changed_files -contains 'scripts\windows\bin\stock-background-host.exe') `
        'the decision must name the background host executable as the changed input'
    # A build from genuinely different sources changes the receipt.
    Write-HostBuildReceipt 'new' 'build-two'
    [IO.File]::WriteAllText((Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\bin\stock-background-host.build.json'),
        (@{ schema_version = 1; output_name = 'stock-background-host.exe'
            output_length = (Get-Item -LiteralPath (Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\bin\stock-background-host.exe')).Length
            inputs = @{ 'process-lifetime.cs' = 'aa'; 'background-task-host.cs' = 'CHANGED' } } | ConvertTo-Json -Depth 5),
        [Text.UTF8Encoding]::new($false))
    Assert-True ((Get-TreeDecision $oldReceiptHashes (Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new'))).decision -eq 'reinstall') `
        'a background host built from different C# inputs must force the tunnel reinstall'
    # An OLD tree published before the receipt existed must not be condemned forever.
    Remove-Item -LiteralPath (Join-Path (Join-Path $hashSandbox 'old') 'scripts\windows\bin\stock-background-host.build.json') -Force
    Write-HostBuildReceipt 'new' 'build-two'
    Assert-True ((Get-TreeDecision (Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'old')) `
        (Get-StockTunnelFileHash -RuntimeRoot (Join-Path $hashSandbox 'new'))).decision -eq 'skip') `
        'a pinned release published before the build receipt existed must degrade to presence, not reinstall forever'
    Remove-Item -LiteralPath (Join-Path (Join-Path $hashSandbox 'new') 'scripts\windows\bin\stock-background-host.build.json') -Force
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

# A pin naming a release that is not on disk must not consume a retention slot:
# the fill-to-Max(2,RetainCount) loop would otherwise keep one real release
# fewer than the policy intends, i.e. a phantom pin silently evicts a release.
$phantom = Get-StockReleaseRetentionPlan -ReleaseNames @('rel-003', 'rel-002', 'rel-001') `
    -ActiveRelease 'rel-003' -PreviousRelease 'rel-002' -TunnelRelease 'rel-deleted-by-hand' -RetainCount 3
Assert-True (-not ($phantom.Keep -contains 'rel-deleted-by-hand')) 'a pin that names no existing release directory must be ignored'
Assert-True ($phantom.Keep -contains 'rel-001') 'a phantom pin must not consume the retention slot a real release would have taken'
Assert-True (@($phantom.Remove).Count -eq 0) 'with three real releases and RetainCount 3 nothing may be pruned because of a phantom pin'

# --- tunnel_release_not_retained must be able to fire at all -----------------
# Derived the way Resolve-StockTunnelReinstallPlan derives it: from the release
# directories plus the {active, previous} pins ONLY. Passing the tunnel pin in
# (as Get-StockReleaseRetentionState does, reading it out of the same state
# file the check is about) makes Keep always contain the id being checked, so
# the check would be structurally unable to fail.
$independentKeep = Get-StockReleaseRetentionPlan -ReleaseNames @('rel-004', 'rel-003', 'rel-002', 'rel-001', 'rel-000') `
    -ActiveRelease 'rel-003' -PreviousRelease 'rel-002' -RetainCount 3
Assert-True (-not ($independentKeep.Keep -contains 'rel-000')) `
    'the independently-derived keep set must be able to exclude the tunnel release, or tunnel_release_not_retained can never fire'
$circularKeep = Get-StockReleaseRetentionPlan -ReleaseNames @('rel-004', 'rel-003', 'rel-002', 'rel-001', 'rel-000') `
    -ActiveRelease 'rel-003' -PreviousRelease 'rel-002' -TunnelRelease 'rel-000' -RetainCount 3
Assert-True ($circularKeep.Keep -contains 'rel-000') `
    'retention itself must still pin the tunnel release unconditionally, which is exactly why the gate may not reuse that answer'

# --- the pin is "last installed from", and goes stale on every relaunch -------
# The task action is <PlatformRoot>\current\..., so a relaunch after a drop
# starts the tunnel from the ACTIVE release without writing release-state.json.
$stale = Get-StockTunnelPinRefresh -PinnedRelease 'rel-000' -ActiveRelease 'rel-003' `
    -ActiveReleaseStamp '2026-09-18T21:32:15+08:00' -ActivationInstantIsExact $true `
    -RuntimeStartedAt '2026-09-18T21:35:59+08:00'
Assert-True ($stale.Refreshed -and $stale.Release -eq 'rel-003') `
    'a tunnel run that started after `current` moved came from `current` and must refresh the pin'
Assert-True (-not $stale.Uncertain) `
    'a refresh judged against the real activation instant is not uncertain'

# --- the instant compared against must be the junction move, not the id ------
# publish stamps the release id before eight PowerShell suites, pytest and three
# npm builds, and only switches `current` afterwards. Judged against that stamp,
# a tunnel that started during the test run looks like a relaunch from `current`
# while it was in fact still executing out of the pinned tree, and refreshing
# the pin on that evidence hands the gate the wrong baseline to compare against.
# The pin is still moved forward -- a pin naming a tree nothing runs from is no
# better -- but the answer is marked uncertain and an uncertain pin cannot skip.
$approximate = Get-StockTunnelPinRefresh -PinnedRelease 'rel-000' -ActiveRelease 'rel-003' `
    -ActiveReleaseStamp '2026-09-18T21:32:15+08:00' -RuntimeStartedAt '2026-09-18T21:35:59+08:00'
Assert-True ($approximate.Refreshed -and $approximate.Uncertain) `
    'with only the release-id stamp to compare against, a refresh must be marked uncertain'
Assert-True ((Get-Decision @{ TunnelPinUncertain = $true }).decision -eq 'reinstall') `
    'an uncertain pin must force the tunnel reinstall: CurrentHashes may describe a tree the live process never ran'
Assert-True ((Get-Decision @{ TunnelPinUncertain = $true }).reasons -contains 'tunnel_release_pin_uncertain') `
    'the decision must record that it could not tell which tree the live tunnel came from'
Assert-True ((Get-StockTunnelPinRefresh -PinnedRelease 'rel-000' -ActiveRelease 'rel-003' `
    -ActiveReleaseStamp '2026-09-18T21:32:15+08:00' -RuntimeStartedAt '2026-09-18T20:00:00+08:00').Uncertain -eq $false) `
    'a run that predates even the early id stamp predates the junction move too, so nothing is uncertain about leaving the pin alone'

# Resolve-StockReleaseActivationInstant picks that instant and says how good it
# is. activated_at is what publish/switch record the moment
# Set-StockCurrentRelease returns; the junction's own creation time is equally
# exact (Set-StockCurrentRelease builds a fresh junction and renames it into
# place on every switch) but only when the junction really resolves to the
# active release; the id stamp and the staged directory's creation time are both
# EARLIER than activation and only ever approximate.
$fromRecord = Resolve-StockReleaseActivationInstant -RecordedActivatedAt '2026-09-18T21:40:00+08:00' `
    -JunctionCreatedAt '2026-09-18T21:39:00+08:00' -JunctionMatchesActiveRelease $true `
    -ReleaseIdStamp '2026-09-18T21:32:15+08:00'
Assert-True ($fromRecord.Source -eq 'activated_at' -and $fromRecord.Exact -and $fromRecord.Stamp -eq '2026-09-18T21:40:00+08:00') `
    'release-state.json''s activated_at is the authoritative activation instant'
$fromJunction = Resolve-StockReleaseActivationInstant -JunctionCreatedAt '2026-09-18T21:39:00+08:00' `
    -JunctionMatchesActiveRelease $true -ReleaseIdStamp '2026-09-18T21:32:15+08:00'
Assert-True ($fromJunction.Source -eq 'current_junction_created' -and $fromJunction.Exact) `
    'without activated_at the junction''s own creation time is still the moment it moved'
$fromId = Resolve-StockReleaseActivationInstant -JunctionCreatedAt '2026-09-18T21:39:00+08:00' `
    -JunctionMatchesActiveRelease $false -ReleaseIdStamp '2026-09-18T21:32:15+08:00'
Assert-True ($fromId.Source -eq 'release_id' -and (-not $fromId.Exact)) `
    'a junction that does not resolve to the active release says nothing about when that release was activated'
$fromDirectory = Resolve-StockReleaseActivationInstant -ReleaseDirectoryCreatedAt '2026-09-18T21:00:00+08:00'
Assert-True ($fromDirectory.Source -eq 'release_directory_created' -and (-not $fromDirectory.Exact)) `
    'an id without an embedded stamp falls back to the staged directory time, which is approximate too'
Assert-True ((Resolve-StockReleaseActivationInstant).Source -eq 'unknown') `
    'with no source at all the activation instant must be reported as unknown, not invented'
$fresh = Get-StockTunnelPinRefresh -PinnedRelease 'rel-000' -ActiveRelease 'rel-003' `
    -ActiveReleaseStamp '2026-09-18T21:32:15+08:00' -RuntimeStartedAt '2026-09-18T20:00:00+08:00'
Assert-True ((-not $fresh.Refreshed) -and $fresh.Release -eq 'rel-000') `
    'a tunnel run that predates the active release still comes from the pinned tree'
Assert-True ((Get-StockTunnelPinRefresh -PinnedRelease 'rel-000' -ActiveRelease 'rel-003' `
    -ActiveReleaseStamp '2026-09-18T21:32:15+08:00' -RuntimeStartedAt '').Reason -eq 'runtime_start_unknown') `
    'an unreadable start time is not evidence that the pin is stale; the recorded pin must stand'
Assert-True ((Get-StockTunnelPinRefresh -PinnedRelease 'rel-000' -ActiveRelease 'rel-000' `
    -ActiveReleaseStamp '2026-09-18T21:32:15+08:00' -RuntimeStartedAt '2026-09-19T00:00:00+08:00').Refreshed -eq $false) `
    'a pin that already names the active release needs no refresh'
Assert-True ((Get-StockTunnelPinRefresh -PinnedRelease '' -ActiveRelease 'rel-003' `
    -ActiveReleaseStamp '2026-09-18T21:32:15+08:00' -RuntimeStartedAt '2026-09-19T00:00:00+08:00').Release -eq '') `
    'no pin stays no pin: an unknown tunnel_release is itself a reinstall reason and must not be invented here'
Assert-True ((Get-StockReleaseStamp -ReleaseId '20260918T213215-ba717c8b6631-clean') -ne '') `
    'the release id timestamp must be recoverable, so the pin refresh has something to compare against'
Assert-True ((Get-StockReleaseStamp -ReleaseId 'rel-000') -eq '') `
    'a release id without an embedded timestamp must report none, so the caller falls back to the directory time'

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
    # The `current` junction is the second tree the gate compares against, and
    # the tree every relaunch of the tunnel actually starts from.
    [void](Set-StockCurrentRelease -PlatformRoot $planSandbox -ReleaseId 'rel-000')
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{
        active_release = 'rel-000'; previous_release = $null; tunnel_release = 'rel-000'
        tunnel_ssh_target_sha256 = (Get-StockTunnelSshTargetHash -PlatformRoot $planSandbox) })
    $newApp = Join-Path $releasesRoot 'rel-001\app'
    $script:probeCalls = 0
    # The registered action has to carry what a reinstall by THIS session would
    # register: the same PowerShell host Get-InstalledPowerShell resolves and
    # the same principal publish/switch derive from the session's elevation.
    $registeredShell = Get-InstalledPowerShell
    $runningTask = { param($Name) [pscustomobject]@{
        State = 'Running'
        Execute = (Join-Path $planSandbox 'current\scripts\windows\bin\stock-background-host.exe')
        Arguments = ('"' + $registeredShell + '" "' + (Join-Path $planSandbox 'current\scripts\shared-peer\start-shared-tunnels.ps1') + '"')
        LogonType = (Get-StockScheduledTaskLogonType)
        UserId = $env:USERNAME
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
    Assert-True ($livePlan.task_action_path_state -eq 'ok') 'every absolute path the registered action names must be observed to exist'
    Assert-True ($livePlan.junction_runtime_root -and @($livePlan.junction_changed_files).Count -eq 0) `
        'the plan must hash the `current` junction tree too, and report it as unchanged when it matches'

    # An action naming a path that no longer exists -- the WindowsApps pwsh.exe
    # the Store removed when it updated PowerShell -- must never skip.
    $vanishedShell = Join-Path $planSandbox 'no-such-dir\pwsh.exe'
    $vanishedTask = { param($Name) [pscustomobject]@{
        State = 'Running'
        Execute = (Join-Path $planSandbox 'current\scripts\windows\bin\stock-background-host.exe')
        Arguments = ('"' + $vanishedShell + '" "' + (Join-Path $planSandbox 'current\scripts\shared-peer\start-shared-tunnels.ps1') + '"')
        LogonType = (Get-StockScheduledTaskLogonType)
        UserId = $env:USERNAME
    } }
    $vanished = Resolve-StockTunnelReinstallPlan @resolveArgs -ScheduledTaskProvider $vanishedTask
    Assert-True ($vanished.decision -eq 'reinstall') 'a registered action naming a path that no longer exists must force the tunnel reinstall'
    Assert-True ($vanished.reasons -contains 'task_action_path_missing') 'the decision must record which observation rejected the action'
    Assert-True (@($vanished.task_action_missing_paths) -contains $vanishedShell) 'the plan must name the path that is gone'

    # A principal this session would not register must not survive a skip.
    $otherPrincipalTask = { param($Name) [pscustomobject]@{
        State = 'Running'
        Execute = (Join-Path $planSandbox 'current\scripts\windows\bin\stock-background-host.exe')
        Arguments = ('"' + $registeredShell + '" "' + (Join-Path $planSandbox 'current\scripts\shared-peer\start-shared-tunnels.ps1') + '"')
        LogonType = 'SomethingElse'
        UserId = $env:USERNAME
    } }
    $otherPrincipal = Resolve-StockTunnelReinstallPlan @resolveArgs -ScheduledTaskProvider $otherPrincipalTask
    Assert-True ($otherPrincipal.decision -eq 'reinstall') `
        'a registered task principal that differs from the one this publish would register must force the tunnel reinstall'
    Assert-True ($otherPrincipal.changed_files -contains 'config:tunnel_task_principal') `
        'the plan must name the task principal as the changed input'

    # A `current` junction whose tunnel files differ from the new release must
    # not skip, even though the pinned tree matches: the next relaunch starts
    # from `current`, not from the pin.
    $junctionFile = Join-Path $releasesRoot 'rel-000\app\scripts\shared-peer\start-shared-tunnels.ps1'
    $junctionOriginal = [IO.File]::ReadAllText($junctionFile)
    [IO.File]::WriteAllText($junctionFile, 'drifted junction tree', [Text.UTF8Encoding]::new($false))
    $drifted = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($drifted.decision -eq 'reinstall') `
        'a `current` junction tree that differs from the new release must force the tunnel reinstall'
    [IO.File]::WriteAllText($junctionFile, $junctionOriginal, [Text.UTF8Encoding]::new($false))
    Assert-True ((Resolve-StockTunnelReinstallPlan @resolveArgs).decision -eq 'skip') 'restoring the junction tree must restore the skip'

    # The pin is "last installed from": a supervised run that started after the
    # active release was published came from `current` instead.
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{
        active_release = 'rel-001'; previous_release = 'rel-000'; tunnel_release = 'rel-000'
        tunnel_ssh_target_sha256 = (Get-StockTunnelSshTargetHash -PlatformRoot $planSandbox) })
    [IO.File]::WriteAllText($runtimeStatePath,
        ('{"status":"healthy","started_at":"' + ([DateTimeOffset]::Now.AddDays(1).ToString('o')) + '"}'),
        [Text.UTF8Encoding]::new($false))
    $refreshed = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($refreshed.tunnel_release -eq 'rel-001' -and $refreshed.tunnel_release_pin_refreshed) `
        'a tunnel run that started after the active release was published must move the pin to the active release'
    # Here the state carries no activated_at and the junction still resolves to
    # rel-000, so the only instant available is approximate: the refresh happens
    # but must not buy a skip.
    Assert-True ($refreshed.activation_instant_source -eq 'release_directory_created' -and $refreshed.tunnel_release_pin_uncertain) `
        'with no recorded activation instant and a junction that does not resolve to the active release, the pin refresh must be reported as uncertain'
    Assert-True ($refreshed.decision -eq 'reinstall' -and ($refreshed.reasons -contains 'tunnel_release_pin_uncertain')) `
        'an uncertain pin must force the tunnel reinstall instead of skipping against a tree the live process may never have run'

    # A RECORDED activated_at does not rescue that either while `current` still
    # resolves somewhere else. switch-stock-release.ps1 moves the junction
    # before it writes state and swallows a failed revert with a warning, so
    # active_release/activated_at can describe a release `current` has
    # demonstrably left; taken as exact, that stale instant makes a tunnel which
    # started from the OTHER tree look like a relaunch from the active one and
    # buys a skip it has not earned.
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{
        active_release = 'rel-001'; previous_release = 'rel-000'; tunnel_release = 'rel-000'
        activated_at = ([DateTimeOffset]::Now.AddDays(-1).ToString('o'))
        tunnel_ssh_target_sha256 = (Get-StockTunnelSshTargetHash -PlatformRoot $planSandbox) })
    $staleActivated = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($staleActivated.activation_instant_source -ne 'activated_at' -and $staleActivated.tunnel_release_pin_uncertain) `
        'a recorded activated_at must not be treated as exact while the `current` junction does not resolve to the active release'
    Assert-True ($staleActivated.decision -eq 'reinstall' -and ($staleActivated.reasons -contains 'tunnel_release_pin_uncertain')) `
        'a superseded activated_at must buy a reinstall, not a skip'

    # With the junction really moved and activated_at recorded the same refresh
    # is exact, and the skip it enables is the one this whole gate exists for.
    [void](Set-StockCurrentRelease -PlatformRoot $planSandbox -ReleaseId 'rel-001')
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{
        active_release = 'rel-001'; previous_release = 'rel-000'; tunnel_release = 'rel-000'
        activated_at = ([DateTimeOffset]::Now.AddDays(-1).ToString('o'))
        tunnel_ssh_target_sha256 = (Get-StockTunnelSshTargetHash -PlatformRoot $planSandbox) })
    $exactRefresh = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($exactRefresh.activation_instant_source -eq 'activated_at' -and (-not $exactRefresh.tunnel_release_pin_uncertain)) `
        'release-state.json''s activated_at must be preferred over the release id''s own timestamp'
    Assert-True ($exactRefresh.tunnel_release -eq 'rel-001' -and $exactRefresh.decision -eq 'skip') `
        'a refresh judged against the moment the junction actually moved must still allow the skip'
    # An unrelated state write must not erase when the junction last moved, or
    # every later gate evaluation would fall back to an approximate instant.
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{ active_release = 'rel-001'; previous_release = 'rel-000' })
    Assert-True ([string](Get-StockReleaseState -PlatformRoot $planSandbox).activated_at -ne '') `
        'activated_at must survive a state write that does not mention it'
    # Without activated_at, the junction's own creation time is still exact.
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{
        active_release = 'rel-001'; previous_release = 'rel-000'; tunnel_release = 'rel-000'; activated_at = $null
        tunnel_ssh_target_sha256 = (Get-StockTunnelSshTargetHash -PlatformRoot $planSandbox) })
    $fromJunctionTime = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($fromJunctionTime.activation_instant_source -eq 'current_junction_created' -and (-not $fromJunctionTime.tunnel_release_pin_uncertain)) `
        'a state file written before activated_at existed must fall back to the junction''s creation time, which is exact, not to the release id stamp'
    [void](Set-StockCurrentRelease -PlatformRoot $planSandbox -ReleaseId 'rel-000')
    [IO.File]::WriteAllText($runtimeStatePath, '{"status":"healthy"}', [Text.UTF8Encoding]::new($false))
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{
        active_release = 'rel-000'; previous_release = $null; tunnel_release = 'rel-000'; activated_at = $null
        tunnel_ssh_target_sha256 = (Get-StockTunnelSshTargetHash -PlatformRoot $planSandbox) })

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

    # tunnel_release_not_retained must actually be REACHABLE against a real
    # platform root. It was not before: the gate asked
    # Get-StockReleaseRetentionState, which pins tunnel_release out of the same
    # state file, so Keep always contained the very id being checked.
    foreach ($extra in 'rel-002', 'rel-003') {
        New-Item -ItemType Directory -Force -Path (Join-Path $releasesRoot "$extra\app") | Out-Null
        Start-Sleep -Milliseconds 20
    }
    [void](Set-StockReleaseState -PlatformRoot $planSandbox -State @{
        active_release = 'rel-003'; previous_release = 'rel-002'; tunnel_release = 'rel-000'
        tunnel_ssh_target_sha256 = (Get-StockTunnelSshTargetHash -PlatformRoot $planSandbox) })
    $unretained = Resolve-StockTunnelReinstallPlan @resolveArgs
    Assert-True ($unretained.tunnel_release_state -eq 'not_retained') `
        'a tunnel release outside the {active, previous} + fill keep set must be reported as not retained; deriving that set from release-state.json''s own tunnel_release makes the check unreachable'
    Assert-True ($unretained.reasons -contains 'tunnel_release_not_retained') `
        'the decision must record that the live tunnel has drifted further behind `current` than retention would keep on its own'
    Assert-True ($unretained.decision -eq 'reinstall') 'an unretained tunnel release must force the tunnel reinstall'
    # Retention itself must still keep that directory: that pin is what makes a
    # skip safe while the release IS retained.
    $realPlan = Get-StockReleaseRetentionState -PlatformRoot $planSandbox -RetainCount 3
    Assert-True ($realPlan.Keep -contains 'rel-000') `
        'Remove-ExpiredStockReleases must still pin the live tunnel''s release even when the gate refuses to skip on it'
} finally {
    $resolvedPlanSandbox = [IO.Path]::GetFullPath($planSandbox)
    $tempPlanRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ($resolvedPlanSandbox.StartsWith($tempPlanRoot + '\trading-hareness-release-safety-', [StringComparison]::OrdinalIgnoreCase)) {
        $planCurrent = Join-Path $planSandbox 'current'
        if (Test-Path -LiteralPath $planCurrent) { Remove-Item -LiteralPath $planCurrent -Force -ErrorAction SilentlyContinue }
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
# Everything up to and including the release-state write can still throw into
# the catch, and that rollback path reinstalls the tunnel unconditionally. Only
# past `$activated = $true` is the skip a fact.
$publishStateWriteIndex = $publishSource.IndexOf('Set-StockReleaseState -PlatformRoot $platform -State @{')
# The STATEMENT, not a mention of it in a comment.
$activatedMatch = [regex]::Match($publishSource, '(?m)^\s*\$activated = \$true\s*$')
Assert-True $activatedMatch.Success 'publish-stock-release.ps1 must record successful activation in $activated'
$publishActivatedIndex = $activatedMatch.Index
Assert-True ($publishStateWriteIndex -ge 0 -and $publishActivatedIndex -gt $publishStateWriteIndex) `
    'publish-stock-release.ps1 must set $activated only after the release-state write'
Assert-True ($publishSkipEventIndex -gt $publishActivatedIndex) `
    'publish-stock-release.ps1 must write the tunnel_reinstall_skipped event only after $activated = $true; a failure in the release-state write would otherwise roll back and reinstall the tunnel while a skip receipt was already on disk'
Assert-True ($publishSource.Contains("`$tunnelOutcome = 'reinstall_after_degraded_verification_failed'")) `
    'publish-stock-release.ps1 must report a distinct outcome when the degraded-verification repair reinstall itself threw and nothing was reinstalled'
$degradedCatchIndex = $publishSource.IndexOf("`$tunnelOutcome = 'reinstall_after_degraded_verification_failed'")
$degradedOutcomeIndex = $publishSource.IndexOf("`$tunnelOutcome = 'reinstalled_after_degraded_verification'")
Assert-True ($degradedOutcomeIndex -ge 0 -and $degradedCatchIndex -gt $degradedOutcomeIndex) `
    'the failed-repair outcome must overwrite the optimistic one inside the catch, so the receipt matches what happened'
Assert-True ($publishSource.Contains('-TaskLogonType $TaskLogonType')) `
    'publish-stock-release.ps1 must tell the gate which task principal a reinstall would register, so a skip cannot leave a stale one'
Assert-True ($publishSource.Contains('Get-StockScheduledTaskLogonType')) `
    'publish-stock-release.ps1 must derive the logon type through the shared helper the gate compares against'
Assert-True ($publishSource.Contains('Install-SharedPeerTunnelTask -RuntimeRoot $layout.CurrentPath')) `
    'publish-stock-release.ps1 must reinstall the shared-peer tunnel when post-switch shared-runtime verification is degraded after a skip'
Assert-True ($publishSource -match "(?s)if \(\`$keepTunnel -and \[string\]\`$healthVerification\.shared_runtime -ne 'ok'\)") `
    'publish-stock-release.ps1 must treat a degraded shared runtime as a trigger to reinstall the tunnel the gate spared'
Assert-True ($publishSource.Contains('tunnel_release = $tunnelReleaseAfter')) `
    'publish-stock-release.ps1 must persist the release the tunnel is running from in release-state.json'
# The release id's own yyyyMMddTHHmmss prefix is stamped before the test suite
# runs; `current` only moves tens of minutes later, and everything in between
# still resolves to the PREVIOUS release. The pin refresh needs the real moment.
$publishJunctionIndex = $publishSource.IndexOf('Set-StockCurrentRelease -PlatformRoot $platform -ReleaseId $releaseId')
$publishActivatedStampIndex = $publishSource.IndexOf('$activatedAt = [DateTimeOffset]::Now')
Assert-True ($publishJunctionIndex -ge 0 -and $publishActivatedStampIndex -gt $publishJunctionIndex) `
    'publish-stock-release.ps1 must take the activation instant after Set-StockCurrentRelease returns, not from the release id'
Assert-True ($publishSource.Contains('activated_at = $activatedAt')) `
    'publish-stock-release.ps1 must record activated_at in release-state.json'
Assert-True ($publishActivatedStampIndex -lt $publishSource.IndexOf('activated_at = $activatedAt')) `
    'the recorded activated_at must be the stamp taken at the junction move'

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
# ...and only after the release-state write, which is what
# Write-StockTunnelReinstallSkipEvent's own contract requires and what publish
# already did: that write can still throw (a locked state file, a full disk) and
# this script's catch then reverts, possibly reinstalling the tunnel, so an
# earlier receipt would be one the revert's own events contradict. Both paths:
# the forward one (first occurrence) and the revert one (last).
$switchStateWrite = 'Set-StockReleaseState -PlatformRoot $platform -State @{'
$switchStateWriteIndex = $switchSource.IndexOf($switchStateWrite)
$switchRevertStateWriteIndex = $switchSource.LastIndexOf($switchStateWrite)
$switchRevertSkipEventIndex = $switchSource.LastIndexOf('Write-StockTunnelReinstallSkipEvent -PlatformRoot')
Assert-True ($switchStateWriteIndex -ge 0 -and $switchSkipEventIndex -gt $switchStateWriteIndex) `
    'switch-stock-release.ps1 must write the tunnel_reinstall_skipped event only after its own Set-StockReleaseState, not before it'
Assert-True ($switchRevertStateWriteIndex -gt $switchStateWriteIndex -and $switchRevertSkipEventIndex -gt $switchRevertStateWriteIndex) `
    'the revert path must write its tunnel_reinstall_skipped event after the revert''s own Set-StockReleaseState too'
Assert-True ($switchSource.Contains('activated_at = $activatedAt') -and $switchSource.Contains('activated_at = $revertActivatedAt')) `
    'switch-stock-release.ps1 must record when the junction actually moved on both the forward and the revert path, or the pin refresh has only the release id''s own timestamp to compare against'
$switchJunctionIndex = $switchSource.IndexOf('Set-StockCurrentRelease -PlatformRoot $platform -ReleaseId $ReleaseId')
$switchActivatedStampIndex = $switchSource.IndexOf('$activatedAt = [DateTimeOffset]::Now')
Assert-True ($switchJunctionIndex -ge 0 -and $switchActivatedStampIndex -gt $switchJunctionIndex) `
    'the recorded activation instant must be taken after Set-StockCurrentRelease returns, or it is not the moment the junction moved'
Assert-True ($switchSource.Contains("`$tunnelOutcome = 'reinstalled_after_degraded_verification'")) `
    'switch-stock-release.ps1 must reinstall the spared tunnel and re-verify when shared-runtime verification fails after a skip'
Assert-True ($switchSource.Contains('tunnel_release = if ($tunnelRelease)')) `
    'switch-stock-release.ps1 must persist the release the tunnel is running from'
Assert-True ($switchSource.Contains('-TaskLogonType $TaskLogonType')) `
    'switch-stock-release.ps1 must tell the gate which task principal a reinstall would register'
Assert-True ($switchSource.Contains('Get-StockScheduledTaskLogonType')) `
    'switch-stock-release.ps1 must derive the same logon type publish does, instead of leaving install-shared-tunnel-task.ps1''s S4U default in place, or the principal observation could never match'
Assert-True ($switchSource -notmatch "install-shared-tunnel-task\.ps1'\) -ScriptPath") `
    'switch-stock-release.ps1 must install the tunnel through one helper that forwards the logon type, not by three ad-hoc invocations'
Assert-True ($switchSource.Contains('if ($keepTunnel -and [string]$tunnelPlan.tunnel_release) { $tunnelRelease = [string]$tunnelPlan.tunnel_release }')) `
    'switch-stock-release.ps1 must persist the pin the gate resolved (refreshed when the tunnel had relaunched from `current`), not the stale record'

# --- the status view must not claim the tunnel will run from the pin ---------
$statusSource = Get-Content -LiteralPath (Join-Path $windowsScripts 'get-stock-release-status.ps1') -Raw -Encoding UTF8
Assert-True ($statusSource.Contains('shared_peer_tunnel_installed_from')) `
    'get-stock-release-status.ps1 must name the per-release flag "installed from", because the tunnel relaunches from `current`, not from the pin'
Assert-True ($statusSource.Contains('last_installed_from') -and $statusSource.Contains('relaunches_from')) `
    'get-stock-release-status.ps1 must show both where the tunnel was last installed from and where it relaunches from'

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
    tunnel_skip_requires_current_junction_equality = $true
    tunnel_pin_refreshed_when_runtime_relaunched_from_current = $true
    tunnel_task_action_paths_must_exist = $true
    tunnel_task_principal_observed = $true
    tunnel_release_retention_check_is_independent = $true
    phantom_release_pin_cannot_consume_retention_slot = $true
    tunnel_skip_event_written_only_after_activation = $true
    failed_degraded_repair_reported_distinctly = $true
    background_host_build_receipt_hashed_not_presence = $true
    chain_parser_follows_expandable_strings = $true
    chain_parser_resolves_paths_beside_the_mentioning_file = $true
    chain_parser_fails_loudly_on_a_missing_referenced_file = $true
    pin_refresh_compares_against_the_recorded_junction_move = $true
    uncertain_pin_forces_reinstall = $true
    switch_skip_event_written_after_its_release_state_write = $true
}
