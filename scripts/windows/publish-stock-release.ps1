[CmdletBinding()]
param(
    [string]$SourceRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [int]$RetainCount = 3,
    [switch]$AllowDirty,
    [switch]$SkipTests
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $SourceRoot) { $SourceRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
$source = [IO.Path]::GetFullPath($SourceRoot).TrimEnd('\')
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
if (-not $platform.StartsWith('G:\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Production releases must remain on G:, got $platform"
}
if (-not (Test-Path -LiteralPath (Join-Path $source '.git') -PathType Container)) { throw "Source root is not a Git checkout: $source" }
Import-Module (Join-Path $source 'scripts\windows\stock-release-management.psm1') -Force

function Invoke-Checked {
    param([string]$FilePath, [string[]]$Arguments, [string]$WorkingDirectory)
    Push-Location $WorkingDirectory
    try {
        & $FilePath @Arguments
        if ($LASTEXITCODE -ne 0) { throw "$FilePath failed with exit code $LASTEXITCODE" }
    } finally { Pop-Location }
}

function Copy-DirectorySnapshot {
    param([string]$Source, [string]$Destination)
    if (-not (Test-Path -LiteralPath $Source -PathType Container)) { throw "Missing release dependency directory: $Source" }
    New-Item -ItemType Directory -Force -Path $Destination | Out-Null
    Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $Destination -Recurse -Force
}

function Stop-ProductionRuntime {
    param([string]$RuntimeRoot)
    Stop-ScheduledTask -TaskName 'trading-hareness-shared-peer-tunnels' -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName 'trading-hareness-dashboard-runtime' -ErrorAction SilentlyContinue
    $stop = Join-Path $RuntimeRoot 'scripts\windows\stop-stock-dashboard.ps1'
    if (Test-Path -LiteralPath $stop -PathType Leaf) { & $stop -PlatformRoot $platform | Out-Null }
}

function Start-ProductionRuntime {
    param([string]$RuntimeRoot)
    & (Join-Path $RuntimeRoot 'scripts\windows\install-stock-dashboard-task.ps1') `
        -RepositoryRoot $RuntimeRoot -PlatformRoot $platform | Out-Null
    & (Join-Path $RuntimeRoot 'scripts\shared-peer\install-shared-tunnel-task.ps1') `
        -ScriptPath (Join-Path $RuntimeRoot 'scripts\shared-peer\start-shared-tunnels.ps1') -PlatformRoot $platform | Out-Null
}

function Wait-ProductionHealth {
    param([string]$RuntimeRoot, [int]$TimeoutSeconds = 150)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Seconds 2
        try {
            $api = Invoke-RestMethod 'http://127.0.0.1:5681/health' -TimeoutSec 3
            $adapter = Invoke-RestMethod 'http://127.0.0.1:5680/health' -TimeoutSec 3
            if ($api.status -eq 'ok' -and $adapter.status -eq 'ok') { break }
        } catch { }
    } while ([DateTime]::UtcNow -lt $deadline)
    if ([DateTime]::UtcNow -ge $deadline) { throw 'Production API and dashboard adapter did not become healthy before the deadline' }
    & (Join-Path $RuntimeRoot 'scripts\shared-peer\verify-shared-runtime.ps1') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Shared runtime verification failed with exit code $LASTEXITCODE" }
}

$gitStatus = @(& git -C $source status --porcelain=v1)
if ($LASTEXITCODE -ne 0) { throw 'git status failed' }
$dirty = $gitStatus.Count -gt 0
if ($dirty -and -not $AllowDirty) {
    throw 'The source checkout is dirty. Commit it first or explicitly pass -AllowDirty to capture a manifest-backed working-tree release.'
}
$head = (& git -C $source rev-parse HEAD).Trim()
$shortHead = $head.Substring(0, 12)
$branch = (& git -C $source branch --show-current).Trim()
$stamp = [DateTimeOffset]::Now.ToString('yyyyMMddTHHmmss')
$releaseId = "$stamp-$shortHead-$(if ($dirty) { 'dirty' } else { 'clean' })"
$layout = Get-StockReleaseLayout -PlatformRoot $platform
New-Item -ItemType Directory -Force -Path $layout.ReleasesRoot | Out-Null
$stagingRoot = Join-Path $layout.ReleasesRoot ".staging-$releaseId"
$finalRoot = Join-Path $layout.ReleasesRoot $releaseId
if ((Test-Path -LiteralPath $stagingRoot) -or (Test-Path -LiteralPath $finalRoot)) { throw "Release already exists: $releaseId" }

if (-not $SkipTests) {
    Invoke-Checked -FilePath (Get-Command pwsh.exe -ErrorAction Stop).Source `
        -Arguments @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $source 'scripts\windows\tests\test-runtime-observability.ps1')) `
        -WorkingDirectory $source
    Invoke-Checked -FilePath (Join-Path $source '.venv\Scripts\python.exe') `
        -Arguments @('-m', 'unittest', 'discover', '-s', 'tests', '-q') -WorkingDirectory (Join-Path $source 'quant-service')
    Invoke-Checked -FilePath (Get-Command npm.cmd -ErrorAction Stop).Source -Arguments @('run', 'typecheck') -WorkingDirectory (Join-Path $source 'frontend')
    Invoke-Checked -FilePath (Get-Command npm.cmd -ErrorAction Stop).Source -Arguments @('run', 'build') -WorkingDirectory (Join-Path $source 'frontend')
    & git -C $source diff --check
    if ($LASTEXITCODE -ne 0) { throw 'git diff --check failed' }
}

$previousState = Get-StockReleaseState -PlatformRoot $platform
$previousRelease = if ($previousState.PSObject.Properties['active_release']) { [string]$previousState.active_release } else { '' }
$previousTarget = Get-StockCurrentReleaseTarget -PlatformRoot $platform
$fallbackRoot = if ($previousTarget) { $previousTarget } else { $source }
$activated = $false

try {
    $app = Join-Path $stagingRoot 'app'
    $evidence = Join-Path $stagingRoot 'evidence'
    New-Item -ItemType Directory -Force -Path $app, $evidence | Out-Null
    $sourceFiles = @(& git -C $source ls-files --cached --others --exclude-standard)
    if ($LASTEXITCODE -ne 0) { throw 'git ls-files failed' }
    foreach ($relative in $sourceFiles) {
        if (-not $relative) { continue }
        $from = Join-Path $source $relative
        if (-not (Test-Path -LiteralPath $from -PathType Leaf)) { continue }
        $to = Join-Path $app $relative
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
        Copy-Item -LiteralPath $from -Destination $to -Force
    }
    Copy-DirectorySnapshot -Source (Join-Path $source '.venv') -Destination (Join-Path $app '.venv')
    Copy-DirectorySnapshot -Source (Join-Path $source 'feishu-adapter\node_modules') -Destination (Join-Path $app 'feishu-adapter\node_modules')
    Copy-DirectorySnapshot -Source (Join-Path $source 'frontend\dist') -Destination (Join-Path $app 'frontend\dist')

    $diff = @(& git -C $source diff --binary HEAD)
    [IO.File]::WriteAllLines((Join-Path $evidence 'working-tree.patch'), $diff, [Text.UTF8Encoding]::new($false))
    $untracked = @(& git -C $source ls-files --others --exclude-standard)
    [IO.File]::WriteAllLines((Join-Path $evidence 'untracked-files.txt'), $untracked, [Text.UTF8Encoding]::new($false))
    $pipFreeze = @(& (Join-Path $source '.venv\Scripts\python.exe') -m pip freeze)
    [IO.File]::WriteAllLines((Join-Path $evidence 'pip-freeze.txt'), $pipFreeze, [Text.UTF8Encoding]::new($false))

    $manifest = [ordered]@{
        schema_version = 1
        release_id = $releaseId
        created_at = [DateTimeOffset]::Now.ToString('o')
        git_head = $head
        git_branch = $branch
        dirty = $dirty
        git_status = $gitStatus
        source_root_at_build = $source
        production_root = $platform
        tests = if ($SkipTests) { 'skipped_by_operator' } else { 'passed' }
        python_version = (& (Join-Path $app '.venv\Scripts\python.exe') --version 2>&1 | Out-String).Trim()
        node_version = (& node --version | Out-String).Trim()
        requirements_sha256 = (Get-FileHash -LiteralPath (Join-Path $app 'quant-service\requirements.txt') -Algorithm SHA256).Hash.ToLowerInvariant()
        frontend_lock_sha256 = (Get-FileHash -LiteralPath (Join-Path $app 'frontend\package-lock.json') -Algorithm SHA256).Hash.ToLowerInvariant()
        adapter_package_sha256 = (Get-FileHash -LiteralPath (Join-Path $app 'feishu-adapter\package.json') -Algorithm SHA256).Hash.ToLowerInvariant()
        adapter_lock_sha256 = (Get-FileHash -LiteralPath (Join-Path $app 'feishu-adapter\package-lock.json') -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    [IO.File]::WriteAllText((Join-Path $app 'release-manifest.json'), ($manifest | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))

    $hashLines = foreach ($file in (Get-ChildItem -LiteralPath $app -Recurse -File | Where-Object Name -ne 'release-manifest.json' | Sort-Object FullName)) {
        $relative = $file.FullName.Substring($app.Length + 1).Replace('\', '/')
        '{0}  {1}' -f (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $relative
    }
    [IO.File]::WriteAllLines((Join-Path $evidence 'files.sha256'), $hashLines, [Text.UTF8Encoding]::new($false))
    $contentDigest = (Get-FileHash -LiteralPath (Join-Path $evidence 'files.sha256') -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifest['content_manifest_sha256'] = $contentDigest
    [IO.File]::WriteAllText((Join-Path $app 'release-manifest.json'), ($manifest | ConvertTo-Json -Depth 10), [Text.UTF8Encoding]::new($false))

    Move-Item -LiteralPath $stagingRoot -Destination $finalRoot
    $newApp = Join-Path $finalRoot 'app'
    Stop-ProductionRuntime -RuntimeRoot $fallbackRoot
    [void](Set-StockCurrentRelease -PlatformRoot $platform -ReleaseId $releaseId)
    Start-ProductionRuntime -RuntimeRoot $layout.CurrentPath
    Wait-ProductionHealth -RuntimeRoot $layout.CurrentPath
    $verification = [ordered]@{
        verified_at = [DateTimeOffset]::Now.ToString('o')
        local_api = 'ok'
        dashboard_adapter = 'ok'
        remote_owner_api = 200
        remote_peer_api = 200
    }
    [void](Set-StockReleaseState -PlatformRoot $platform -State @{
        active_release = $releaseId
        previous_release = if ($previousRelease -and $previousRelease -ne $releaseId) { $previousRelease } else { $null }
        last_verification = $verification
        last_failed_release = $null
        content_manifest_sha256 = $contentDigest
    })
    $activated = $true
    $removed = @(Remove-ExpiredStockReleases -PlatformRoot $platform -RetainCount $RetainCount)
    [pscustomobject]@{
        status = 'published'
        release_id = $releaseId
        current = $layout.CurrentPath
        target = $newApp
        previous_release = $previousRelease
        retained_release_count = [Math]::Max(2, $RetainCount)
        pruned_releases = $removed
        dirty_snapshot = $dirty
        tests = $manifest.tests
        content_manifest_sha256 = $contentDigest
    }
} catch {
    $failure = $_
    if (-not $activated) {
        try {
            Stop-ProductionRuntime -RuntimeRoot $(if (Test-Path -LiteralPath $layout.CurrentPath) { $layout.CurrentPath } else { $fallbackRoot })
            if ($previousRelease -and (Test-Path -LiteralPath (Get-StockReleaseAppPath -PlatformRoot $platform -ReleaseId $previousRelease) -PathType Container)) {
                [void](Set-StockCurrentRelease -PlatformRoot $platform -ReleaseId $previousRelease)
                Start-ProductionRuntime -RuntimeRoot $layout.CurrentPath
            } else {
                if (Test-Path -LiteralPath $layout.CurrentPath) {
                    $current = Get-Item -LiteralPath $layout.CurrentPath -Force
                    if (-not ($current.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Rollback refused to remove non-junction $($layout.CurrentPath)" }
                    Remove-Item -LiteralPath $layout.CurrentPath -Force
                }
                Start-ProductionRuntime -RuntimeRoot $source
            }
            [void](Set-StockReleaseState -PlatformRoot $platform -State @{
                active_release = if ($previousRelease) { $previousRelease } else { $null }
                previous_release = if ($previousState.PSObject.Properties['previous_release']) { $previousState.previous_release } else { $null }
                last_verification = if ($previousState.PSObject.Properties['last_verification']) { $previousState.last_verification } else { $null }
                last_failed_release = $releaseId
                failure_message = $failure.Exception.Message
            })
        } catch { Write-Warning "Automatic rollback also failed: $($_.Exception.Message)" }
    }
    throw $failure
} finally {
    if (Test-Path -LiteralPath $stagingRoot) {
        $resolvedStaging = [IO.Path]::GetFullPath($stagingRoot)
        $resolvedReleaseRoot = [IO.Path]::GetFullPath($layout.ReleasesRoot).TrimEnd('\')
        if ($resolvedStaging.StartsWith($resolvedReleaseRoot + '\.staging-', [StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolvedStaging -Recurse -Force
        }
    }
}
