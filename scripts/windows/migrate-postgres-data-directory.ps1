[CmdletBinding()]
param(
    [string]$PlatformRoot = 'G:\StockPlatform',
    # The NVMe hot tier. The cold tablespace, the backups and everything else
    # the platform owns stay on G: (docs/OWNER_DATABASE_STORAGE.md).
    [string]$TargetDataDir = 'F:\StockPlatformDB\postgresql16',
    [string]$RuntimeEnv = '',
    [string]$PostgresVersion = '16.15',
    [int]$Port = 55432,
    [int]$ApiPort = 5681,
    # The old directory is renamed, never deleted: it is the rollback copy and
    # the only full pre-migration image until the operator removes it by hand.
    [string]$KeepOldName = '',
    [int]$FreeSpaceMarginGB = 50,
    # Print every step and touch nothing.
    [switch]$WhatIf,
    # Run inside a trading session anyway. Refused by default.
    [switch]$Force,
    # Put PGDATA_DIR back and start the cluster from the kept old directory.
    [switch]$Rollback,
    [string]$RollbackDataDir = ''
)

# Move the PostgreSQL cluster from the G: HDD to the NVMe hot tier.
#
# The safety sequence is the whole point of this script and is asserted by
# scripts/windows/tests/test-postgres-data-migration-contract.ps1:
#
#   1. preflight, refusing to run during an exchange session without -Force;
#   2. stop the watcher task FIRST -- trading-hareness-dashboard-runtime
#      restarts PostgreSQL on its own and would happily start a second server
#      on the half-copied directory;
#   3. stop PostgreSQL and prove no postgres.exe survives;
#   4. copy with robocopy and VERIFY the copy (exit code, recursive file and
#      byte totals, SHA-256 of global\pg_control) BEFORE anything points at it;
#   5. only then switch PGDATA_DIR and regenerate the managed configuration;
#   6. start from the new directory and verify identity, settings and row counts;
#   7. rename the old directory -- never remove it -- and write a receipt;
#   8. bring the platform back up.
#
# Every step prints; with -WhatIf nothing is changed and no file is written.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'postgres-managed-config.psm1') -Force

$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
if (-not $platform.StartsWith('G:\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "The platform root must remain on G:, got $platform"
}
$RuntimeEnv = if ($RuntimeEnv) { $RuntimeEnv } else { Join-Path $platform 'config\runtime.env' }
$logs = Join-Path $platform 'logs'
$pgBin = Join-Path $platform "runtime\postgresql-$PostgresVersion\bin"
$pgCtl = Join-Path $pgBin 'pg_ctl.exe'
$pgIsReady = Join-Path $pgBin 'pg_isready.exe'
$psql = Join-Path $pgBin 'psql.exe'
$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
if (-not $KeepOldName) { $KeepOldName = 'postgresql16.pre-nvme-' + (Get-Date).ToString('yyyyMMdd') }
$smokeTables = @('quant.instruments', 'quant.canonical_bars_daily', 'quant.raw_market_observations')
$watcherTasks = @('trading-hareness-dashboard-runtime', 'trading-hareness-post-close-pipeline', 'trading-hareness-storage-tiers')

function Write-Step {
    param([Parameter(Mandatory)][string]$Message, [string]$Level = 'info')
    $prefix = if ($WhatIf) { '[what-if] ' } else { '' }
    $line = '[{0}] {1}{2}' -f [DateTimeOffset]::Now.ToString('HH:mm:ss'), $prefix, $Message
    if ($Level -eq 'warning') { Write-Warning $line } else { Write-Host $line }
}

function Test-TradingSession {
    <#
        Mon-Fri 09:00-15:30 exchange time. Stopping the database then would
        destroy the intraday observation ledger for the day.
    #>
    param([Parameter(Mandatory)][DateTime]$Now)
    $minutes = $Now.Hour * 60 + $Now.Minute
    $weekday = [int]$Now.DayOfWeek -ge 1 -and [int]$Now.DayOfWeek -le 5
    return ($weekday -and $minutes -ge 540 -and $minutes -lt 930)
}

function Get-DirectoryFootprint {
    param([Parameter(Mandatory)][string]$Path)
    $files = 0
    $bytes = [long]0
    foreach ($item in Get-ChildItem -LiteralPath $Path -Recurse -Force -File -ErrorAction SilentlyContinue) {
        $files++
        $bytes += $item.Length
    }
    return [pscustomobject]@{ Path = $Path; Files = $files; Bytes = $bytes }
}

function Invoke-AdminPsql {
    <#
        Read-only helper. The password is passed through the process
        environment and is never written to the command line, the console or
        the receipt.
    #>
    param([Parameter(Mandatory)][string]$Sql, [string]$Database = 'trading_hareness', [int]$TimeoutMs = 600000)
    $env:PGPASSWORD = $script:adminPassword
    $env:PGCONNECT_TIMEOUT = '10'
    # The statement timeout travels in PGOPTIONS, not as a second -c: psql
    # prints the command tag of every -c it runs, so "-c 'SET ...' -tAc <query>"
    # returns "SET`n<value>" and every comparison below would fail on a healthy
    # server (observed against the live cluster on 2026-09-19).
    $env:PGOPTIONS = "-c statement_timeout=$TimeoutMs"
    try {
        $output = & $psql -w -h 127.0.0.1 -p $Port -U $script:adminUser -d $Database -v ON_ERROR_STOP=1 -tAc $Sql
        if ($LASTEXITCODE -ne 0) { throw "psql failed with exit code $LASTEXITCODE" }
        return (([string]($output -join "`n")).Trim())
    } finally {
        Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
        Remove-Item Env:PGCONNECT_TIMEOUT -ErrorAction SilentlyContinue
        Remove-Item Env:PGOPTIONS -ErrorAction SilentlyContinue
    }
}

function Get-RowCountSnapshot {
    $snapshot = [ordered]@{}
    foreach ($table in $smokeTables) {
        $snapshot[$table] = [long](Invoke-AdminPsql -Sql "SELECT count(*) FROM $table")
    }
    return $snapshot
}

function Stop-PlatformRuntimes {
    # The watcher task is stopped BEFORE PostgreSQL: it starts the database on
    # its own schedule and would race the copy.
    foreach ($task in $watcherTasks) {
        Write-Step "stopping scheduled task $task"
        if (-not $WhatIf) {
            Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
            Disable-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue | Out-Null
        }
    }
    $stop = Join-Path $PSScriptRoot 'stop-stock-dashboard.ps1'
    if (Test-Path -LiteralPath $stop -PathType Leaf) {
        Write-Step 'stopping dashboard adapter, tunnel and owner API (stop-stock-dashboard.ps1)'
        if (-not $WhatIf) { & $stop -PlatformRoot $platform | Out-Null }
    } else {
        Write-Step "graceful stop script not found at $stop" 'warning'
    }
}

function Start-PlatformRuntimes {
    foreach ($task in $watcherTasks) {
        Write-Step "re-enabling scheduled task $task"
        if (-not $WhatIf) { Enable-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue | Out-Null }
    }
    Write-Step 'starting trading-hareness-dashboard-runtime'
    if (-not $WhatIf) { Start-ScheduledTask -TaskName 'trading-hareness-dashboard-runtime' -ErrorAction SilentlyContinue }
    if ($WhatIf) { return 'skipped' }
    $deadline = [DateTime]::UtcNow.AddMinutes(5)
    while ([DateTime]::UtcNow -lt $deadline) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 5 -NoProxy
            if ($health) { return 'healthy' }
        } catch { Start-Sleep -Seconds 5 }
    }
    return 'unverified'
}

function Stop-PostgresCluster {
    param([Parameter(Mandatory)][string]$DataDirectory)
    Write-Step "pg_ctl stop -D $DataDirectory -m fast -w"
    if (-not $WhatIf) {
        & $pgCtl stop -D $DataDirectory -m fast -w -t 120
        if ($LASTEXITCODE -ne 0) { throw "pg_ctl stop failed with exit code $LASTEXITCODE" }
        $deadline = [DateTime]::UtcNow.AddSeconds(60)
        while ((Get-Process -Name 'postgres' -ErrorAction SilentlyContinue) -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Seconds 2
        }
        $survivors = @(Get-Process -Name 'postgres' -ErrorAction SilentlyContinue)
        if ($survivors.Count -gt 0) {
            throw "PostgreSQL did not stop: $($survivors.Count) postgres.exe still running (pids $($survivors.Id -join ','))"
        }
    }
    Write-Step 'verified: no postgres.exe remains'
}

function Start-PostgresCluster {
    param([Parameter(Mandatory)][string]$DataDirectory)
    $startupLog = Join-Path $logs 'postgresql-startup.log'
    Write-Step "pg_ctl start -D $DataDirectory -w -l $startupLog"
    if ($WhatIf) { return }
    $start = Start-Process -FilePath $pgCtl -WindowStyle Hidden -PassThru `
        -ArgumentList @('start', '-D', $DataDirectory, '-l', $startupLog, '-w', '-t', '120')
    if (-not $start.WaitForExit(130000)) {
        Stop-Process -Id $start.Id -Force -ErrorAction SilentlyContinue
        throw 'pg_ctl start timed out'
    }
    if ($start.ExitCode -ne 0) { throw "pg_ctl start failed with exit code $($start.ExitCode)" }
    & $pgIsReady -h 127.0.0.1 -p $Port -q
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL started but pg_isready did not report ready' }
}

# --------------------------------------------------------------------------
# credentials (never printed)
# --------------------------------------------------------------------------
$config = Read-StockPlatformEnvFile -Path $RuntimeEnv
if ($config.Count -eq 0) { throw "Missing or empty runtime environment file: $RuntimeEnv" }
$script:adminUser = if ($config.ContainsKey('PGADMINUSER') -and $config['PGADMINUSER']) { $config['PGADMINUSER'] } else { 'stock_admin' }
if (-not ($config.ContainsKey('PGADMINPASSWORD') -and $config['PGADMINPASSWORD'])) {
    throw "PGADMINPASSWORD is missing from $RuntimeEnv"
}
$script:adminPassword = $config['PGADMINPASSWORD']
$currentDataDir = Resolve-StockPlatformDataDirectory -PlatformRoot $platform -Config $config

# --------------------------------------------------------------------------
# rollback
# --------------------------------------------------------------------------
if ($Rollback) {
    $restoreTarget = if ($RollbackDataDir) { [IO.Path]::GetFullPath($RollbackDataDir).TrimEnd('\') } else { Join-Path (Join-Path $platform 'data') $KeepOldName }
    Write-Step "rollback: current PGDATA_DIR = $currentDataDir"
    Write-Step "rollback: restoring to $restoreTarget"
    if (-not (Test-Path -LiteralPath (Join-Path $restoreTarget 'PG_VERSION') -PathType Leaf)) {
        throw "Rollback target is not a PostgreSQL data directory: $restoreTarget"
    }
    Stop-PlatformRuntimes
    Stop-PostgresCluster -DataDirectory $currentDataDir
    Write-Step "PGDATA_DIR -> $restoreTarget"
    if (-not $WhatIf) { Set-StockPlatformEnvValue -Path $RuntimeEnv -Name 'PGDATA_DIR' -Value $restoreTarget }
    Write-Step 'regenerating the managed configuration for the restored directory'
    if (-not $WhatIf) {
        [void](Write-StockPlatformManagedConfig -DataDirectory $restoreTarget `
            -ManagedConfigPath (Join-Path $platform 'config\postgresql-stock-platform.conf') -Port $Port -LogDirectory $logs)
    }
    Start-PostgresCluster -DataDirectory $restoreTarget
    $runtimeState = Start-PlatformRuntimes
    Write-Step "rollback complete (platform $runtimeState); the NVMe copy at $currentDataDir was left in place for inspection"
    return [pscustomobject]@{
        action = 'rollback'; what_if = [bool]$WhatIf
        data_directory = $restoreTarget; abandoned_directory = $currentDataDir; platform = $runtimeState
    }
}

# --------------------------------------------------------------------------
# step 1: preflight (read-only)
# --------------------------------------------------------------------------
$target = [IO.Path]::GetFullPath($TargetDataDir).TrimEnd('\')
Write-Step "step 1/8 preflight: $currentDataDir -> $target"

if ($PSVersionTable.PSVersion.Major -lt 7) { throw "PowerShell 7 is required, got $($PSVersionTable.PSVersion)" }
if ($currentDataDir -eq $target) { throw "PGDATA_DIR already points at $target; nothing to migrate" }
if (Test-TradingSession -Now (Get-Date)) {
    if (-not $Force) {
        throw 'Refusing to migrate during the Mon-Fri 09:00-15:30 exchange session; pass -Force to override'
    }
    Write-Step 'exchange session in progress; continuing because -Force was passed' 'warning'
}
if (-not (Test-Path -LiteralPath (Join-Path $currentDataDir 'PG_VERSION') -PathType Leaf)) {
    throw "Current data directory is not a PostgreSQL cluster: $currentDataDir"
}
foreach ($binary in @($pgCtl, $pgIsReady, $psql)) {
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) { throw "PostgreSQL runtime is incomplete: $binary" }
}
if (-not (Get-Command robocopy.exe -ErrorAction SilentlyContinue)) { throw 'robocopy.exe is not available' }

& $pgCtl status -D $currentDataDir *> $null
if ($LASTEXITCODE -ne 0) { throw "PostgreSQL is not running from $currentDataDir (pg_ctl status exit $LASTEXITCODE)" }
Write-Step "verified: PostgreSQL is running from $currentDataDir"

if (Test-Path -LiteralPath $target) {
    $existing = @(Get-ChildItem -LiteralPath $target -Force -ErrorAction SilentlyContinue)
    if ($existing.Count -gt 0) { throw "Target data directory exists and is not empty: $target" }
}
try {
    [IO.File]::Open($RuntimeEnv, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::ReadWrite).Dispose()
} catch {
    throw "Runtime environment file is not writable: $RuntimeEnv"
}

$sourceFootprint = Get-DirectoryFootprint -Path $currentDataDir
$targetRoot = [IO.Path]::GetPathRoot($target)
$freeBytes = [IO.DriveInfo]::new($targetRoot).AvailableFreeSpace
$requiredBytes = $sourceFootprint.Bytes + ([long]$FreeSpaceMarginGB * 1GB)
Write-Step ('source: {0:N0} files, {1:N1} GB; target volume {2} free {3:N1} GB, required {4:N1} GB' -f `
    $sourceFootprint.Files, ($sourceFootprint.Bytes / 1GB), $targetRoot, ($freeBytes / 1GB), ($requiredBytes / 1GB))
if ($freeBytes -lt $requiredBytes) {
    throw "Target volume $targetRoot has $([math]::Round($freeBytes / 1GB, 1)) GB free, needs $([math]::Round($requiredBytes / 1GB, 1)) GB"
}

$snapshot = [ordered]@{}
if ($WhatIf) {
    Write-Step "would snapshot row counts of $($smokeTables -join ', ')"
} else {
    $snapshot = Get-RowCountSnapshot
    foreach ($entry in $snapshot.GetEnumerator()) { Write-Step "snapshot $($entry.Key) = $($entry.Value)" }
}

$transcript = Join-Path $logs "postgres-data-migration-$stamp.transcript.log"
if (-not $WhatIf) {
    New-Item -ItemType Directory -Force -Path $logs | Out-Null
    Start-Transcript -Path $transcript -Append | Out-Null
}
$startedAt = [DateTimeOffset]::Now
try {
    # ----------------------------------------------------------------------
    # step 2: stop the watcher task first, then the platform runtimes
    # ----------------------------------------------------------------------
    Write-Step 'step 2/8 stopping the runtime watchers and the platform processes'
    Stop-PlatformRuntimes

    # ----------------------------------------------------------------------
    # step 3: stop PostgreSQL
    # ----------------------------------------------------------------------
    Write-Step 'step 3/8 stopping PostgreSQL'
    Stop-PostgresCluster -DataDirectory $currentDataDir

    # ----------------------------------------------------------------------
    # step 4: copy and verify the copy
    # ----------------------------------------------------------------------
    $robocopyLog = Join-Path $logs "postgres-data-migration-$stamp.robocopy.log"
    Write-Step "step 4/8 robocopy $currentDataDir -> $target (log $robocopyLog)"
    $copyVerification = [ordered]@{ verified = $false }
    if ($WhatIf) {
        Write-Step 'would verify: robocopy exit code < 8, equal file and byte totals, equal SHA-256 of global\pg_control'
    } else {
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        & robocopy.exe $currentDataDir $target /E /COPY:DAT /DCOPY:DAT /R:2 /W:2 /MT:8 /NP "/LOG+:$robocopyLog" | Out-Null
        $robocopyExit = $LASTEXITCODE
        if ($robocopyExit -ge 8) { throw "robocopy failed with exit code $robocopyExit (see $robocopyLog)" }
        $copiedFootprint = Get-DirectoryFootprint -Path $target
        $sourceAfter = Get-DirectoryFootprint -Path $currentDataDir
        if ($copiedFootprint.Files -ne $sourceAfter.Files -or $copiedFootprint.Bytes -ne $sourceAfter.Bytes) {
            throw ("Copy verification failed: source {0} files / {1} bytes, copy {2} files / {3} bytes" -f `
                $sourceAfter.Files, $sourceAfter.Bytes, $copiedFootprint.Files, $copiedFootprint.Bytes)
        }
        $controlSource = (Get-FileHash -LiteralPath (Join-Path $currentDataDir 'global\pg_control') -Algorithm SHA256).Hash
        $controlTarget = (Get-FileHash -LiteralPath (Join-Path $target 'global\pg_control') -Algorithm SHA256).Hash
        if ($controlSource -ne $controlTarget) { throw 'Copy verification failed: global\pg_control checksum differs' }
        $copyVerification = [ordered]@{
            verified = $true; robocopy_exit_code = $robocopyExit
            files = $copiedFootprint.Files; bytes = $copiedFootprint.Bytes
            pg_control_sha256 = $controlTarget
        }
        Write-Step ('copy verified: {0:N0} files, {1:N1} GB, pg_control checksum equal' -f $copiedFootprint.Files, ($copiedFootprint.Bytes / 1GB))
    }

    # ----------------------------------------------------------------------
    # step 5: switch PGDATA_DIR and regenerate the managed configuration
    # ----------------------------------------------------------------------
    Write-Step "step 5/8 PGDATA_DIR -> $target and regenerate postgresql-stock-platform.conf"
    if (-not $WhatIf) {
        if (-not $copyVerification['verified']) { throw 'Refusing to switch PGDATA_DIR before the copy is verified' }
        Set-StockPlatformEnvValue -Path $RuntimeEnv -Name 'PGDATA_DIR' -Value $target
        [void](Write-StockPlatformManagedConfig -DataDirectory $target `
            -ManagedConfigPath (Join-Path $platform 'config\postgresql-stock-platform.conf') -Port $Port -LogDirectory $logs)
    }

    # ----------------------------------------------------------------------
    # step 6: start from the new directory and verify it
    # ----------------------------------------------------------------------
    Write-Step 'step 6/8 starting PostgreSQL from the new data directory and verifying it'
    Start-PostgresCluster -DataDirectory $target
    $verification = [ordered]@{}
    if ($WhatIf) {
        Write-Step 'would verify: SHOW data_directory, SHOW work_mem, pg_stat_statements in shared_preload_libraries, row counts'
    } else {
        $reportedDataDir = Invoke-AdminPsql -Sql 'SHOW data_directory'
        if ([IO.Path]::GetFullPath($reportedDataDir).TrimEnd('\').TrimEnd('/') -ne $target) {
            throw "Server reports data_directory '$reportedDataDir', expected '$target'"
        }
        $workMem = Invoke-AdminPsql -Sql 'SHOW work_mem'
        if ($workMem -ne '64MB') { throw "work_mem is '$workMem', expected 64MB" }
        $preload = Invoke-AdminPsql -Sql 'SHOW shared_preload_libraries'
        if ($preload -notmatch 'pg_stat_statements') { throw "shared_preload_libraries is '$preload', expected pg_stat_statements" }
        $after = Get-RowCountSnapshot
        foreach ($table in $smokeTables) {
            if ([long]$after[$table] -ne [long]$snapshot[$table]) {
                throw "Row count for $table changed across the migration: $($snapshot[$table]) -> $($after[$table])"
            }
        }
        $verification = [ordered]@{
            data_directory = $reportedDataDir; work_mem = $workMem
            shared_preload_libraries = $preload; row_counts = $after
        }
        Write-Step 'verified: data_directory, work_mem, pg_stat_statements and all row counts match'
    }

    # ----------------------------------------------------------------------
    # step 7: rename the old directory (never remove it) and write the receipt
    # ----------------------------------------------------------------------
    $keptPath = Join-Path (Split-Path -Parent $currentDataDir) $KeepOldName
    Write-Step "step 7/8 renaming $currentDataDir -> $keptPath (kept for rollback; delete by hand later)"
    if (-not $WhatIf) {
        Rename-Item -LiteralPath $currentDataDir -NewName $KeepOldName -Force
    }
    $receipt = [ordered]@{
        action = 'migrate-postgres-data-directory'
        started_at = $startedAt.ToString('o')
        finished_at = [DateTimeOffset]::Now.ToString('o')
        platform_root = $platform
        old_data_directory = $currentDataDir
        old_data_directory_renamed = $keptPath
        new_data_directory = $target
        source_footprint = @{ files = $sourceFootprint.Files; bytes = $sourceFootprint.Bytes }
        copy_verification = $copyVerification
        startup_verification = $verification
        row_count_snapshot = $snapshot
        forced = [bool]$Force
        what_if = [bool]$WhatIf
    }
    $receiptPath = Join-Path $logs "postgres-data-migration-$stamp.json"
    if (-not $WhatIf) {
        [IO.File]::WriteAllText($receiptPath, (ConvertTo-Json -InputObject $receipt -Depth 8), [Text.UTF8Encoding]::new($false))
        Write-Step "receipt written to $receiptPath"
    } else {
        Write-Step "would write the receipt to $receiptPath"
    }

    # ----------------------------------------------------------------------
    # step 8: bring the platform back
    # ----------------------------------------------------------------------
    Write-Step 'step 8/8 restarting the platform'
    $runtimeState = Start-PlatformRuntimes
    Write-Step "platform state: $runtimeState"

    $receipt['platform'] = $runtimeState
    $receipt['rollback_command'] = "pwsh -NoProfile -File $PSCommandPath -Rollback -RollbackDataDir $keptPath"
    [pscustomobject]$receipt
} finally {
    if (-not $WhatIf) { try { Stop-Transcript | Out-Null } catch { } }
}
