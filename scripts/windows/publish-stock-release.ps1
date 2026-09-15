[CmdletBinding()]
param(
    [string]$SourceRoot = '',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [int]$RetainCount = 3,
    [switch]$AllowDirty,
    [switch]$SkipTests,
    # Logon type for the two scheduled tasks re-registered on activation.
    # S4U needs an elevated session (Register-ScheduledTask otherwise fails
    # with access denied *after* the old runtime is stopped, forcing an
    # automatic rollback); Interactive works unelevated but only while the
    # operator is logged on.  Empty = pick S4U when elevated, else Interactive.
    [ValidateSet('', 'S4U', 'Interactive')][string]$TaskLogonType = '',
    [int]$PublishLockTimeoutSeconds = 900,
    # Used for a UI-disruption repair: never restart a known-noisy old task
    # merely because activation/health verification of the new release failed.
    [switch]$KeepStoppedOnFailure
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

if (-not $TaskLogonType) {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $elevated = ([Security.Principal.WindowsPrincipal]$identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $TaskLogonType = if ($elevated) { 'S4U' } else { 'Interactive' }
    if (-not $elevated) {
        Write-Verbose 'Using Interactive logon with the console-free GUI launcher; S4U requires elevation.'
    }
}

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
    # Graceful stop first, Stop-ScheduledTask second: Stop-ScheduledTask
    # terminates the scheduled task's whole job object (the watchdog, the
    # supervisor, and the supervised process) immediately, which never gives
    # stop-stock-dashboard.ps1's Request-RuntimeStop a chance to write its
    # stop marker or the supervisor a chance to record a clean 'stopped'
    # status -- the runtime-state file is then left saying 'healthy' for a
    # PID that is already dead. Running the graceful stop script first lets
    # it write the marker and kill the actual listener PIDs itself, so the
    # supervisor observes an expected exit and records it correctly; by the
    # time Stop-ScheduledTask runs afterward there is normally nothing left
    # for it to do.
    param([string]$RuntimeRoot)
    $stop = Join-Path $RuntimeRoot 'scripts\windows\stop-stock-dashboard.ps1'
    if (Test-Path -LiteralPath $stop -PathType Leaf) { & $stop -PlatformRoot $platform | Out-Null }
    Stop-ScheduledTask -TaskName 'trading-hareness-shared-peer-tunnels' -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName 'trading-hareness-dashboard-runtime' -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName 'trading-hareness-post-close-pipeline' -ErrorAction SilentlyContinue
}

function Get-LogonTypeArguments {
    # Older releases' task installers have no -LogonType parameter; the
    # rollback path runs *their* copies, so only forward it when declared.
    param([string]$Installer)
    $command = Get-Command -Name $Installer -ErrorAction Stop
    if ($command.Parameters.ContainsKey('LogonType')) { return @{ LogonType = $TaskLogonType } }
    return @{}
}

function Enter-ProductionPublishLock {
    param([Parameter(Mandatory)][string]$PlatformRoot, [int]$TimeoutSeconds = 900)
    $lockRoot = Join-Path $PlatformRoot 'state'
    New-Item -ItemType Directory -Force -Path $lockRoot | Out-Null
    $lockPath = Join-Path $lockRoot 'production-publish.lock'
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(1, $TimeoutSeconds))
    do {
        try {
            $stream = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            $receipt = [Text.Encoding]::UTF8.GetBytes((@{
                pid = $PID
                source = $source
                acquired_at = [DateTimeOffset]::Now.ToString('o')
            } | ConvertTo-Json -Compress))
            $stream.SetLength(0)
            $stream.Write($receipt, 0, $receipt.Length)
            $stream.Flush($true)
            return $stream
        } catch [IO.IOException] {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw "Timed out waiting for the production publish lock at $lockPath"
            }
            Start-Sleep -Seconds 2
        }
    } while ($true)
}

function Start-ProductionRuntime {
    param([string]$RuntimeRoot)
    # Migrations can legitimately take minutes on the archival HDD. Finish
    # them under the lifecycle lock before starting a watchdog/HTTP deadline;
    # otherwise the watchdog repeatedly aborts a healthy index build at 90s.
    $platformStarter = Join-Path $RuntimeRoot 'scripts\windows\start-stock-platform.ps1'
    & $platformStarter -RepositoryRoot $RuntimeRoot -PlatformRoot $platform | Out-Null
    $dashboardInstaller = Join-Path $RuntimeRoot 'scripts\windows\install-stock-dashboard-task.ps1'
    $tunnelInstaller = Join-Path $RuntimeRoot 'scripts\shared-peer\install-shared-tunnel-task.ps1'
    $postCloseInstaller = Join-Path $RuntimeRoot 'scripts\windows\install-post-close-pipeline-task.ps1'
    $dashboardExtra = Get-LogonTypeArguments -Installer $dashboardInstaller
    $tunnelExtra = Get-LogonTypeArguments -Installer $tunnelInstaller
    $postCloseExtra = Get-LogonTypeArguments -Installer $postCloseInstaller
    & $dashboardInstaller -RepositoryRoot $RuntimeRoot -PlatformRoot $platform @dashboardExtra | Out-Null
    $sharedPeerStartupError = $null
    try {
        & $tunnelInstaller -ScriptPath (Join-Path $RuntimeRoot 'scripts\shared-peer\start-shared-tunnels.ps1') `
            -PlatformRoot $platform @tunnelExtra | Out-Null
    } catch {
        $sharedPeerStartupError = $_.Exception.Message
        Write-Warning "Local runtime was installed, but shared-peer tunnel startup is degraded: $sharedPeerStartupError"
    }
    & $postCloseInstaller -RepositoryRoot $RuntimeRoot -PlatformRoot $platform @postCloseExtra | Out-Null
    $newsInstaller = Join-Path $RuntimeRoot 'scripts\windows\install-event-research-delivery-task.ps1'
    if (Test-Path -LiteralPath $newsInstaller) {
        $newsExtra = Get-LogonTypeArguments -Installer $newsInstaller
        & $newsInstaller -RepositoryRoot $RuntimeRoot -PlatformRoot $platform @newsExtra | Out-Null
    } else {
        Get-ScheduledTask -TaskName 'trading-hareness-event-research-delivery' -ErrorAction SilentlyContinue |
            Disable-ScheduledTask | Out-Null
    }
    return [pscustomobject]@{ shared_peer_startup_error = $sharedPeerStartupError }
}

function Wait-ProductionHealth {
    param([string]$RuntimeRoot, [int]$TimeoutSeconds = 150, [string]$SharedPeerStartupError = '')
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
    # verify-shared-runtime.ps1 is a PowerShell script, not a native exe: on
    # failure it throws (propagated by $ErrorActionPreference = 'Stop' in both
    # this script and the callee), so $LASTEXITCODE here would only reflect
    # whatever native command the script happened to run last, not its own
    # success/failure.
    if ($SharedPeerStartupError) {
        return [pscustomobject]@{
            local_api = 'ok'
            dashboard_adapter = 'ok'
            shared_runtime = 'degraded'
            remote_owner_api = 'unavailable'
            remote_peer_api = 'unavailable'
            shared_error = $SharedPeerStartupError
        }
    }
    try {
        $shared = & (Join-Path $RuntimeRoot 'scripts\shared-peer\verify-shared-runtime.ps1')
        return [pscustomobject]@{
            local_api = 'ok'
            dashboard_adapter = 'ok'
            shared_runtime = 'ok'
            remote_owner_api = $shared.remote_owner_api
            remote_peer_api = $shared.remote_peer_api
            shared_error = $null
        }
    } catch {
        # The friend-facing reverse tunnel is an independent optional surface.
        # It must remain observable, but an outage on lightServer must not roll
        # back a release whose local API and dashboard are already healthy.
        Write-Warning "Local release is healthy, but shared-peer verification is degraded: $($_.Exception.Message)"
        return [pscustomobject]@{
            local_api = 'ok'
            dashboard_adapter = 'ok'
            shared_runtime = 'degraded'
            remote_owner_api = 'unavailable'
            remote_peer_api = 'unavailable'
            shared_error = $_.Exception.Message
        }
    }
}

$publishLock = Enter-ProductionPublishLock -PlatformRoot $platform -TimeoutSeconds $PublishLockTimeoutSeconds
try {
$gitStatus = @(& git -C $source status --porcelain=v1)
if ($LASTEXITCODE -ne 0) { throw 'git status failed' }
$dirty = $gitStatus.Count -gt 0
if ($dirty -and -not $AllowDirty) {
    throw 'The source checkout is dirty. Commit it first or explicitly pass -AllowDirty to capture a manifest-backed working-tree release.'
}
$head = (& git -C $source rev-parse HEAD).Trim()
$shortHead = $head.Substring(0, 12)
$branch = (@(& git -C $source branch --show-current) -join '').Trim()
if (-not $branch) { $branch = 'DETACHED' }
$stamp = [DateTimeOffset]::Now.ToString('yyyyMMddTHHmmss')
$releaseId = "$stamp-$shortHead-$(if ($dirty) { 'dirty' } else { 'clean' })"
$layout = Get-StockReleaseLayout -PlatformRoot $platform
New-Item -ItemType Directory -Force -Path $layout.ReleasesRoot | Out-Null
$stagingRoot = Join-Path $layout.ReleasesRoot ".staging-$releaseId"
$finalRoot = Join-Path $layout.ReleasesRoot $releaseId
if ((Test-Path -LiteralPath $stagingRoot) -or (Test-Path -LiteralPath $finalRoot)) { throw "Release already exists: $releaseId" }

if (-not $SkipTests) {
    foreach ($test in 'test-post-close-contract.ps1','test-live-runtime-state.ps1','test-stock-release-management.ps1','test-stock-release-safety.ps1','test-event-delivery-task.ps1','test-public-gateway-contract.ps1') {
        Invoke-Checked -FilePath (Get-Command pwsh.exe -ErrorAction Stop).Source `
            -Arguments @('-NoProfile','-File',(Join-Path $source "scripts\windows\tests\$test")) -WorkingDirectory $source
    }
    & (Join-Path $source 'scripts\windows\build-background-task-host.ps1') | Out-Null
    Invoke-Checked -FilePath (Get-Command pwsh.exe -ErrorAction Stop).Source `
        -Arguments @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', (Join-Path $source 'scripts\windows\tests\test-background-process.ps1')) `
        -WorkingDirectory $source
    Invoke-Checked -FilePath (Get-Command pwsh.exe -ErrorAction Stop).Source `
        -Arguments @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', (Join-Path $source 'scripts\windows\tests\test-runtime-isolation.ps1')) `
        -WorkingDirectory $source
    Invoke-Checked -FilePath (Get-Command pwsh.exe -ErrorAction Stop).Source `
        -Arguments @('-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $source 'scripts\windows\tests\test-runtime-observability.ps1')) `
        -WorkingDirectory $source
    # Pytest executes both unittest.TestCase suites and pytest-style functions.
    # Running unittest discovery first duplicates nearly the entire backend
    # suite and can exhaust Windows ephemeral sockets before the authoritative
    # pytest pass reaches its final tests.
    Invoke-Checked -FilePath (Join-Path $source '.venv\Scripts\python.exe') `
        -Arguments @('-m', 'pytest', 'tests', '-q', '--disable-warnings') -WorkingDirectory (Join-Path $source 'quant-service')
    Invoke-Checked -FilePath (Get-Command npm.cmd -ErrorAction Stop).Source -Arguments @('run', 'test') -WorkingDirectory (Join-Path $source 'frontend')
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
$activationAttempted = $false

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
    Copy-DirectorySnapshot -Source (Join-Path $source 'scripts\windows\bin') -Destination (Join-Path $app 'scripts\windows\bin')

    # A plain directory copy of .venv does not rewrite the absolute paths
    # baked into it at creation time: pyvenv.cfg's `home`, and (more
    # concretely, per the audit) the embedded shebang path inside every
    # console-script launcher under .venv\Scripts (pip.exe, alembic.exe,
    # uvicorn.exe, ...), which still points at $source's own .venv\Scripts.
    # Rebuilding the venv in-place with `python -m venv --copies` plus a
    # pinned wheelhouse would fix this properly; that is a larger change, so
    # for now this only detects and records it as a manifest warning.
    $venvWarnings = @()
    $venvPyvenvCfg = Join-Path $app '.venv\pyvenv.cfg'
    if (Test-Path -LiteralPath $venvPyvenvCfg -PathType Leaf) {
        $homeLine = Get-Content -LiteralPath $venvPyvenvCfg -Encoding UTF8 | Where-Object { $_ -match '^\s*home\s*=' } | Select-Object -First 1
        if ($homeLine) {
            $homeValue = ($homeLine -split '=', 2)[1].Trim()
            if ($homeValue.StartsWith($source, [StringComparison]::OrdinalIgnoreCase)) {
                $venvWarnings += "pyvenv.cfg home ($homeValue) still points into the F: development checkout ($source); console-script launchers copied under .venv\Scripts embed this path at creation time and may silently run the dev interpreter instead of this release's own copy if invoked directly (running them via the venv's own python.exe, as the runtime scripts do, is unaffected)."
            }
        } else {
            $venvWarnings += 'pyvenv.cfg is missing a home entry'
        }
    } else {
        $venvWarnings += 'Missing .venv\pyvenv.cfg in the snapshotted virtual environment'
    }
    foreach ($warning in $venvWarnings) { Write-Warning $warning }

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
        venv_warnings = $venvWarnings
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

    # Self-check the snapshot we just took against the manifest we just
    # wrote before this release can ever become `current`: catches copy
    # corruption (interrupted Copy-Item, concurrent writes to $source, a
    # flaky disk) independently of the same check run again at switch time.
    [void](Test-StockReleaseFileHashes -AppPath $app -ManifestPath (Join-Path $evidence 'files.sha256'))

    Move-Item -LiteralPath $stagingRoot -Destination $finalRoot
    $newApp = Join-Path $finalRoot 'app'
    $activationAttempted = $true
    Stop-ProductionRuntime -RuntimeRoot $fallbackRoot
    [void](Set-StockCurrentRelease -PlatformRoot $platform -ReleaseId $releaseId)
    $startup = Start-ProductionRuntime -RuntimeRoot $layout.CurrentPath
    $healthVerification = Wait-ProductionHealth -RuntimeRoot $layout.CurrentPath `
        -SharedPeerStartupError ([string]$startup.shared_peer_startup_error)
    $verification = [ordered]@{
        verified_at = [DateTimeOffset]::Now.ToString('o')
        local_api = $healthVerification.local_api
        dashboard_adapter = $healthVerification.dashboard_adapter
        shared_runtime = $healthVerification.shared_runtime
        remote_owner_api = $healthVerification.remote_owner_api
        remote_peer_api = $healthVerification.remote_peer_api
        shared_error = $healthVerification.shared_error
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
    if (-not $activated -and $activationAttempted) {
        try {
            # Stop whatever is currently active first -- if Set-StockCurrentRelease
            # above already flipped the junction to this (failing) release, that
            # is this release's own app path, so its own stop script is used to
            # tear down its own processes before anything is switched back.
            Stop-ProductionRuntime -RuntimeRoot $(if (Test-Path -LiteralPath $layout.CurrentPath) { $layout.CurrentPath } else { $fallbackRoot })
            if ($KeepStoppedOnFailure) {
                foreach ($taskName in 'trading-hareness-shared-peer-tunnels', 'trading-hareness-dashboard-runtime') {
                    Disable-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Out-Null
                }
            }
            $rollbackCompatible = $false
            if ($previousRelease -and (Test-Path -LiteralPath (Get-StockReleaseAppPath -PlatformRoot $platform -ReleaseId $previousRelease) -PathType Container)) {
                $candidateRuntime = Get-StockReleaseAppPath -PlatformRoot $platform -ReleaseId $previousRelease
                & (Join-Path $source '.venv\Scripts\python.exe') (Join-Path $source 'scripts\check-release-schema.py') --candidate-runtime $candidateRuntime --env-file (Join-Path $platform 'config\runtime.env') | Out-Null
                $rollbackCompatible = $LASTEXITCODE -eq 0
            }
            if ($rollbackCompatible) {
                [void](Test-StockReleaseIntegrity -PlatformRoot $platform -ReleaseId $previousRelease)
                [void](Set-StockCurrentRelease -PlatformRoot $platform -ReleaseId $previousRelease)
                if (-not $KeepStoppedOnFailure) {
                    $rollbackStartup = Start-ProductionRuntime -RuntimeRoot $layout.CurrentPath
                    [void](Wait-ProductionHealth -RuntimeRoot $layout.CurrentPath `
                        -SharedPeerStartupError ([string]$rollbackStartup.shared_peer_startup_error))
                }
                [void](Set-StockReleaseState -PlatformRoot $platform -State @{
                    active_release = $previousRelease
                    previous_release = if ($previousState.PSObject.Properties['previous_release']) { $previousState.previous_release } else { $null }
                    last_verification = @{ verified_at = [DateTimeOffset]::Now.ToString('o'); result = $(if ($KeepStoppedOnFailure) { 'disabled_after_failed_activation' } else { 'verified_after_automatic_rollback' }) }
                    last_failed_release = $releaseId
                    failure_message = $failure.Exception.Message
                })
            } elseif ($previousRelease -and (Test-Path -LiteralPath (Join-Path $finalRoot 'app') -PathType Container)) {
                # Never activate old code that cannot recognize an applied DB
                # migration. Keep the new tree addressable for forward repair.
                foreach ($taskName in 'trading-hareness-shared-peer-tunnels', 'trading-hareness-dashboard-runtime') {
                    Disable-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue | Out-Null
                }
                [void](Set-StockReleaseState -PlatformRoot $platform -State @{
                    active_release = $releaseId
                    previous_release = $previousRelease
                    last_failed_release = $releaseId
                    last_verification = @{ verified_at = [DateTimeOffset]::Now.ToString('o'); result = 'stopped_schema_incompatible_rollback' }
                    failure_message = $failure.Exception.Message
                })
                Write-Warning 'Rollback refused: prior code cannot recognize the database schema; runtime stopped for forward repair.'
            } else {
                # No previously-activated release to fall back to. Do not
                # start production from the F: development checkout -- leave
                # production stopped and fail loudly instead, so an operator
                # has to make a deliberate decision rather than "production"
                # silently running out of a developer's working tree.
                if (Test-Path -LiteralPath $layout.CurrentPath) {
                    $current = Get-Item -LiteralPath $layout.CurrentPath -Force
                    if (-not ($current.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw "Rollback refused to remove non-junction $($layout.CurrentPath)" }
                    Remove-Item -LiteralPath $layout.CurrentPath -Force
                }
                Write-Warning 'No previously-activated release is available to roll back to. Production is left stopped rather than started from the F: development checkout; publish a working release before restarting it.'
                [void](Set-StockReleaseState -PlatformRoot $platform -State @{
                    active_release = $null
                    previous_release = if ($previousState.PSObject.Properties['previous_release']) { $previousState.previous_release } else { $null }
                    last_verification = if ($previousState.PSObject.Properties['last_verification']) { $previousState.last_verification } else { $null }
                    last_failed_release = $releaseId
                    failure_message = $failure.Exception.Message
                })
            }
        } catch { Write-Warning "Automatic rollback also failed: $($_.Exception.Message)" }
        # The failed release must not be considered by the retention policy
        # or later reactivated by name; rename it out of the way (kept, not
        # deleted, for forensics). By this point the junction no longer
        # points at $finalRoot in either branch above, so this is safe.
        $stillCurrent = Get-StockCurrentReleaseTarget -PlatformRoot $platform
        if ((Test-Path -LiteralPath $finalRoot -PathType Container) -and -not ($stillCurrent -and $stillCurrent.StartsWith($finalRoot + '\', [StringComparison]::OrdinalIgnoreCase))) {
            $failedRoot = "$finalRoot.failed"
            if (Test-Path -LiteralPath $failedRoot) { Remove-Item -LiteralPath $failedRoot -Recurse -Force }
            Rename-Item -LiteralPath $finalRoot -NewName (Split-Path -Leaf $failedRoot) -ErrorAction SilentlyContinue
        }
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
} finally {
    if ($publishLock) { $publishLock.Dispose() }
}
