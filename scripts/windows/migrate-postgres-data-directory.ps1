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
    [string]$RollbackDataDir = '',
    # Rollback starts a cluster image frozen at the moment of the cutover: every
    # write made since then is discarded. The switch must be passed explicitly.
    [switch]$AcceptDataLoss
)

# Move the PostgreSQL cluster from the G: HDD to the NVMe hot tier.
#
# The safety sequence is the whole point of this script and is asserted by
# scripts/windows/tests/test-postgres-data-migration-contract.ps1:
#
#   1. preflight, refusing to run during an exchange session without -Force and
#      refusing a source directory carrying reparse points other than the
#      pg_tblspc junctions this script recreates at the target;
#   2. stop the watcher task FIRST -- trading-hareness-dashboard-runtime
#      restarts PostgreSQL on its own and would happily start a second server
#      on the half-copied directory -- in the order Disable (no new start),
#      graceful stop script (clean runtime state), Stop-ScheduledTask (backstop);
#   3. stop PostgreSQL and prove no postgres.exe survives;
#   4. copy with robocopy /XJ and VERIFY the copy (exit code, recursive file and
#      byte totals, SHA-256 of global\pg_control) BEFORE anything points at it,
#      then recreate the tablespace junctions at the target;
#   5. only then switch PGDATA_DIR and regenerate the managed configuration;
#   6. start from the new directory and verify identity, settings and row counts;
#   7. rename the old directory -- never remove it -- and write a receipt;
#   8. bring the platform back up.
#
# Every step prints; with -WhatIf nothing is changed and no file is written.
#
# Any failure between step 2 and step 8 is caught: the run puts PGDATA_DIR back
# if it had already been switched, restarts PostgreSQL from the directory that
# still holds the authoritative cluster, re-enables every task this run
# disabled, writes a failure receipt and rethrows. A migration must never end
# with the database down and the platform disabled.

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
# Reads the control file of a STOPPED cluster; the only way to date a rollback
# image without starting it.
$pgControlData = Join-Path $pgBin 'pg_controldata.exe'
$stamp = (Get-Date).ToString('yyyyMMdd-HHmmss')
if (-not $KeepOldName) { $KeepOldName = 'postgresql16.pre-nvme-' + (Get-Date).ToString('yyyyMMdd') }
$smokeTables = @('quant.instruments', 'quant.canonical_bars_daily', 'quant.raw_market_observations')
# Every scheduled job that touches PostgreSQL and can fire while the migration
# runs. The migration is only allowed outside the exchange session, which in
# practice means the 04:00-08:00 maintenance window -- exactly when the nightly
# dump (04:10), the off-site upload (05:10) and the tier job (06:00) are
# scheduled. A backup starting against a stopped or half-copied cluster would
# fail the night and, worse, leave a truncated dump behind, so they are stopped
# and disabled with the watcher and restored afterwards.
#
# 'trading-hareness-shared-peer-tunnels' is deliberately NOT in this list. The
# tunnels carry peer sessions into PostgreSQL, and stopping the server already
# severs every one of them; the tunnel process reconnects on its own once the
# cluster is back. Leaving them running keeps one fewer task to disable, and
# therefore one fewer thing the failure recovery has to put back.
$watcherTasks = @(
    'trading-hareness-dashboard-runtime',
    'trading-hareness-post-close-pipeline',
    'trading-hareness-storage-tiers',
    'trading-hareness-stock-backup',
    'trading-hareness-stock-backup-offsite'
)
# Tasks this run disabled, so step 8 re-enables only those and never turns on a
# job the operator had deliberately left disabled.
$script:DisabledTasks = [System.Collections.Generic.List[string]]::new()
# How far the run got, so the failure recovery knows what it has to undo. Only
# the catch block reads these.
$script:RuntimesStopped = $false
$script:PostgresStopped = $false
$script:EnvSwitched = $false
$script:OldDirectoryRenamed = $false

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
    <#
        Recursive file count and byte total, plus every reparse point found.

        Reparse points are reported, never followed: <PGDATA>\pg_tblspc\<oid> is
        a directory junction to the cold tablespace on G:, and walking into it
        would count the whole cold tier as hot bytes (and make the free-space
        preflight demand room for it on the NVMe volume). PowerShell 7's
        Get-ChildItem -Recurse does not descend into a reparse point unless
        -FollowSymlink is passed; the explicit skip below documents that and
        keeps the behaviour if that default ever changes.
    #>
    param([Parameter(Mandatory)][string]$Path)
    $files = 0
    $bytes = [long]0
    $root = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    $links = [System.Collections.Generic.List[object]]::new()
    foreach ($item in Get-ChildItem -LiteralPath $Path -Recurse -Force -ErrorAction SilentlyContinue) {
        if ($item.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
            $target = if ($item.PSObject.Properties['LinkTarget'] -and $item.LinkTarget) { [string]$item.LinkTarget } else { [string]($item.Target | Select-Object -First 1) }
            $links.Add([pscustomobject]@{
                FullName = $item.FullName
                Relative = $item.FullName.Substring($root.Length).TrimStart('\')
                Target = $target
            })
            continue
        }
        if ($item -is [IO.FileInfo]) {
            $files++
            $bytes += $item.Length
        }
    }
    return [pscustomobject]@{ Path = $Path; Files = $files; Bytes = $bytes; ReparsePoints = $links.ToArray() }
}

function Assert-CopyableSource {
    <#
        robocopy runs with /XJ, so junctions are skipped rather than followed
        (without it a second migration would copy the entire cold tier onto the
        500 GB hot volume). The only reparse points a PostgreSQL data directory
        is allowed to carry are the pg_tblspc tablespace junctions, which this
        script recreates at the target; anything else would be silently dropped
        by the copy, so the run refuses instead.
    #>
    param([Parameter(Mandatory)]$Footprint)
    $links = @($Footprint.ReparsePoints)
    $unsupported = @($links | Where-Object { $_.Relative -notmatch '^pg_tblspc\\[^\\]+$' -or -not $_.Target })
    if ($unsupported.Count -gt 0) {
        throw ("Refusing to copy a data directory with reparse points outside pg_tblspc: " +
            (($unsupported | ForEach-Object { $_.Relative }) -join ', '))
    }
    return $links
}

function Get-TablespaceLinkCount {
    param([Parameter(Mandatory)][string]$DataDirectory)
    $path = Join-Path $DataDirectory 'pg_tblspc'
    if (-not (Test-Path -LiteralPath $path -PathType Container)) { return 0 }
    return @(Get-ChildItem -LiteralPath $path -Force -ErrorAction SilentlyContinue).Count
}

function Get-ClusterCheckpointTime {
    <#
        The "Time of latest checkpoint" from pg_controldata, which is how old a
        stopped cluster image is. The field is rendered with the C library's
        locale-dependent date format, so an unparseable value falls back to the
        control file's own timestamp rather than failing the check that exists
        to protect the operator.
    #>
    param([Parameter(Mandatory)][string]$DataDirectory)
    $value = $null
    $source = 'unavailable'
    if (Test-Path -LiteralPath $pgControlData -PathType Leaf) {
        try {
            $output = & $pgControlData -D $DataDirectory 2>$null
            $line = @($output | Where-Object { $_ -match '^Time of latest checkpoint:' }) | Select-Object -First 1
            if ($line) {
                $text = ($line -split ':', 2)[1].Trim()
                [DateTime]$parsed = [DateTime]::MinValue
                if ([DateTime]::TryParse($text, [Globalization.CultureInfo]::InvariantCulture,
                        [Globalization.DateTimeStyles]::None, [ref]$parsed)) {
                    $value = $parsed
                    $source = 'pg_controldata'
                }
            }
        } catch { }
    }
    if ($null -eq $value) {
        $control = Join-Path $DataDirectory 'global\pg_control'
        if (Test-Path -LiteralPath $control -PathType Leaf) {
            $value = (Get-Item -LiteralPath $control).LastWriteTime
            $source = 'pg_control_mtime'
        }
    }
    return [pscustomobject]@{ DataDirectory = $DataDirectory; CheckpointTime = $value; Source = $source }
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
    <#
        The watcher tasks are handled BEFORE PostgreSQL: the dashboard runtime
        starts the database on its own schedule and would race the copy.

        The three moves are ordered, and the order is asserted by
        scripts/windows/tests/test-postgres-data-migration-contract.ps1:

        1. Disable-ScheduledTask -- stops a new instance from starting without
           killing anything that is running;
        2. stop-stock-dashboard.ps1 -- the graceful stop. It writes the runtime
           stop marker and kills the listener PIDs itself, so the supervisor
           observes an expected exit and records 'stopped';
        3. Stop-ScheduledTask -- the backstop for anything the graceful stop did
           not reach.

        Doing 3 before 2 (as this script originally did) terminates the task's
        whole job object -- watchdog, supervisor and supervised process at once
        -- so Request-RuntimeStop never runs and the runtime-state file is left
        claiming 'healthy' for a dead PID. publish-stock-release.ps1's
        Stop-ProductionRuntime documents the same rule.
    #>
    foreach ($task in $watcherTasks) {
        Write-Step "disabling scheduled task $task (no new instance may start)"
        if (-not $WhatIf) {
            $existing = Get-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
            if (-not $existing) { continue }
            if ($existing.State -ne 'Disabled') {
                Disable-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue | Out-Null
                $script:DisabledTasks.Add($task)
            }
        }
    }
    $stop = Join-Path $PSScriptRoot 'stop-stock-dashboard.ps1'
    if (Test-Path -LiteralPath $stop -PathType Leaf) {
        Write-Step 'graceful stop of dashboard adapter, tunnel and owner API (stop-stock-dashboard.ps1)'
        if (-not $WhatIf) { & $stop -PlatformRoot $platform | Out-Null }
    } else {
        Write-Step "graceful stop script not found at $stop" 'warning'
    }
    foreach ($task in $watcherTasks) {
        Write-Step "stopping scheduled task $task (backstop)"
        if (-not $WhatIf) { Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue }
    }
    $script:RuntimesStopped = $true
}

function Start-PlatformRuntimes {
    $toEnable = if ($WhatIf) { $watcherTasks } else { @($script:DisabledTasks) }
    foreach ($task in $toEnable) {
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
    $script:PostgresStopped = $true
    Write-Step 'verified: no postgres.exe remains'
}

function Stop-PostgresClusterIfRunning {
    param([Parameter(Mandatory)][string]$DataDirectory)
    if ($WhatIf) { return $false }
    if (-not (Test-Path -LiteralPath (Join-Path $DataDirectory 'PG_VERSION') -PathType Leaf)) { return $false }
    & $pgCtl status -D $DataDirectory *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    Stop-PostgresCluster -DataDirectory $DataDirectory
    return $true
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
    $script:PostgresStopped = $false
}

function Invoke-FailureRecovery {
    <#
        Put the platform back after a failure anywhere in steps 2-8.

        The authoritative cluster is whichever directory the run has not
        finished replacing: before the PGDATA_DIR switch that is the source
        directory, after it the target -- unless the switch happened but the new
        cluster never came up, in which case the source is still the only good
        copy and PGDATA_DIR has to be put back with it.

        Every action is best effort and recorded; the caller rethrows the
        original failure afterwards, so a recovery that itself fails never hides
        the reason the migration stopped.
    #>
    param(
        [Parameter(Mandatory)][string]$SourceDataDirectory,
        [Parameter(Mandatory)][string]$TargetDataDirectory,
        [Parameter(Mandatory)][AllowEmptyString()][string]$RenamedSourceDirectory
    )
    $steps = [System.Collections.Generic.List[object]]::new()
    function Add-RecoveryStep([string]$Name, [scriptblock]$Action) {
        if ($WhatIf) { $steps.Add([ordered]@{ step = $Name; result = 'skipped_what_if' }); return }
        try {
            & $Action | Out-Null
            Write-Step "recovery: $Name"
            $steps.Add([ordered]@{ step = $Name; result = 'ok' })
        } catch {
            Write-Step "recovery FAILED: $Name -- $($_.Exception.Message)" 'warning'
            $steps.Add([ordered]@{ step = $Name; result = 'failed'; error = $_.Exception.Message })
        }
    }

    if ($script:OldDirectoryRenamed) {
        # The new cluster was started and verified before the rename; the data
        # lives on the target now and must not be abandoned. Only the platform
        # has to come back.
        Write-Step 'recovery: the new data directory is already authoritative; restoring the platform only' 'warning'
    } else {
        $authoritative = $SourceDataDirectory
        Add-RecoveryStep 'stop whatever runs from the target directory' { [void](Stop-PostgresClusterIfRunning -DataDirectory $TargetDataDirectory) }
        if ($script:EnvSwitched) {
            Add-RecoveryStep "PGDATA_DIR back to $authoritative" {
                Set-StockPlatformEnvValue -Path $RuntimeEnv -Name 'PGDATA_DIR' -Value $authoritative
            }
            Add-RecoveryStep 'regenerate the managed configuration for the original directory' {
                [void](Write-StockPlatformManagedConfig -DataDirectory $authoritative `
                    -ManagedConfigPath (Join-Path $platform 'config\postgresql-stock-platform.conf') -Port $Port -LogDirectory $logs)
            }
            $script:EnvSwitched = $false
        }
        Add-RecoveryStep "start PostgreSQL from $authoritative" {
            & $pgCtl status -D $authoritative *> $null
            if ($LASTEXITCODE -ne 0) { Start-PostgresCluster -DataDirectory $authoritative }
        }
    }
    $platformState = 'not_attempted'
    try {
        $platformState = Start-PlatformRuntimes
        $steps.Add([ordered]@{ step = 're-enable and restart the platform'; result = $platformState })
    } catch {
        Write-Step "recovery FAILED: restarting the platform -- $($_.Exception.Message)" 'warning'
        $steps.Add([ordered]@{ step = 're-enable and restart the platform'; result = 'failed'; error = $_.Exception.Message })
    }
    return [pscustomobject]@{ steps = $steps.ToArray(); platform = $platformState; renamed_source = $RenamedSourceDirectory }
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
    if ([IO.Path]::GetFullPath($restoreTarget).TrimEnd('\') -eq $currentDataDir) {
        throw "Rollback target is the running data directory: $restoreTarget"
    }

    # How much is thrown away. The rollback image is a byte copy frozen at the
    # cutover: every intraday observation, post-close run and research row
    # written since then exists only in the live cluster.
    $liveAge = Get-ClusterCheckpointTime -DataDirectory $currentDataDir
    $targetAge = Get-ClusterCheckpointTime -DataDirectory $restoreTarget
    $gapHours = $null
    if ($null -ne $liveAge.CheckpointTime -and $null -ne $targetAge.CheckpointTime) {
        $gapHours = [math]::Round(($liveAge.CheckpointTime - $targetAge.CheckpointTime).TotalHours, 1)
    }
    Write-Step ("rollback target last checkpoint {0} (source {1}); live cluster {2} (source {3})" -f `
        $targetAge.CheckpointTime, $targetAge.Source, $liveAge.CheckpointTime, $liveAge.Source)
    if ($null -ne $gapHours) {
        Write-Step ("rollback would discard about {0} hour(s) -- {1} day(s) -- of writes" -f $gapHours, [math]::Round($gapHours / 24, 1)) 'warning'
    } else {
        Write-Step 'the age of the rollback target could not be determined; assume it discards everything since the cutover' 'warning'
    }

    # A rollback image taken before `database-storage-tiers.py install` knows
    # nothing about the stock_cold tablespace: its pg_tblspc is empty, so every
    # relation that has since been moved into the cold tier would be missing
    # from the cluster it starts. That is not recoverable by restarting, so it
    # is refused outright -- not behind a switch.
    $liveTablespaces = Get-TablespaceLinkCount -DataDirectory $currentDataDir
    $targetTablespaces = Get-TablespaceLinkCount -DataDirectory $restoreTarget
    Write-Step "tablespace links: live $liveTablespaces, rollback target $targetTablespaces"
    if ($liveTablespaces -gt 0 -and $targetTablespaces -eq 0) {
        throw ("Refusing to roll back to $restoreTarget : the live cluster has $liveTablespaces tablespace(s) " +
            '(stock_cold) and the target image predates their creation, so every row moved into the cold tier ' +
            'would be missing from the cluster this would start. Restore from a backup instead.')
    }

    if (-not $AcceptDataLoss) {
        $refusal = "Refusing to roll back to $restoreTarget without -AcceptDataLoss: it discards every write since " +
            "$($targetAge.CheckpointTime) (about $gapHours hour(s)). Rollback is only safe immediately after the migration."
        if ($WhatIf) {
            Write-Step $refusal 'warning'
            return [pscustomobject]@{
                action = 'rollback'; what_if = $true; refused = 'accept_data_loss_required'
                data_directory = $restoreTarget; live_data_directory = $currentDataDir
                discarded_hours = $gapHours
                live_checkpoint = $liveAge; rollback_checkpoint = $targetAge
            }
        }
        throw $refusal
    }

    $rollbackStamp = [DateTimeOffset]::Now
    try {
        Stop-PlatformRuntimes
        Stop-PostgresCluster -DataDirectory $currentDataDir
        Write-Step "PGDATA_DIR -> $restoreTarget"
        if (-not $WhatIf) {
            Set-StockPlatformEnvValue -Path $RuntimeEnv -Name 'PGDATA_DIR' -Value $restoreTarget
            $script:EnvSwitched = $true
        }
        Write-Step 'regenerating the managed configuration for the restored directory'
        if (-not $WhatIf) {
            [void](Write-StockPlatformManagedConfig -DataDirectory $restoreTarget `
                -ManagedConfigPath (Join-Path $platform 'config\postgresql-stock-platform.conf') -Port $Port -LogDirectory $logs)
        }
        Start-PostgresCluster -DataDirectory $restoreTarget
        $runtimeState = Start-PlatformRuntimes
        Write-Step "rollback complete (platform $runtimeState); the NVMe copy at $currentDataDir was left in place for inspection"
        $rollbackReceipt = [ordered]@{
            action = 'rollback'; what_if = [bool]$WhatIf
            started_at = $rollbackStamp.ToString('o'); finished_at = [DateTimeOffset]::Now.ToString('o')
            data_directory = $restoreTarget; abandoned_directory = $currentDataDir; platform = $runtimeState
            accepted_data_loss = $true; discarded_hours = $gapHours
            live_checkpoint = $liveAge; rollback_checkpoint = $targetAge
            tablespace_links = @{ live = $liveTablespaces; rollback_target = $targetTablespaces }
        }
        if (-not $WhatIf) {
            $rollbackPath = Join-Path $logs "postgres-data-rollback-$stamp.json"
            New-Item -ItemType Directory -Force -Path $logs | Out-Null
            [IO.File]::WriteAllText($rollbackPath, (ConvertTo-Json -InputObject $rollbackReceipt -Depth 8), [Text.UTF8Encoding]::new($false))
            Write-Step "rollback receipt written to $rollbackPath"
            $rollbackReceipt['receipt'] = $rollbackPath
        }
        return [pscustomobject]$rollbackReceipt
    } catch {
        # The same recovery as the forward migration: whatever happens, the
        # platform must not be left down with its tasks disabled.
        $recovery = Invoke-FailureRecovery -SourceDataDirectory $currentDataDir -TargetDataDirectory $restoreTarget -RenamedSourceDirectory ''
        Write-Step "rollback failed: $($_.Exception.Message); recovery platform state $($recovery.platform)" 'warning'
        throw
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
# Refuses a source the copy cannot reproduce faithfully, and returns the
# pg_tblspc junctions step 4 recreates at the target.
$tablespaceLinks = @(Assert-CopyableSource -Footprint $sourceFootprint)
foreach ($link in $tablespaceLinks) {
    Write-Step "tablespace junction $($link.Relative) -> $($link.Target) (skipped by /XJ, recreated at the target)"
}
$targetRoot = [IO.Path]::GetPathRoot($target)
$freeBytes = [IO.DriveInfo]::new($targetRoot).AvailableFreeSpace
$requiredBytes = $sourceFootprint.Bytes + ([long]$FreeSpaceMarginGB * 1GB)
Write-Step ('source: {0:N0} files, {1:N1} GB, {2} tablespace junction(s) not counted; target volume {3} free {4:N1} GB, required {5:N1} GB' -f `
    $sourceFootprint.Files, ($sourceFootprint.Bytes / 1GB), $tablespaceLinks.Count, $targetRoot, ($freeBytes / 1GB), ($requiredBytes / 1GB))
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
        Write-Step 'would copy with /XJ so the pg_tblspc junctions are never followed onto the hot volume'
        Write-Step 'would verify: robocopy exit code < 8, equal file and byte totals, equal SHA-256 of global\pg_control'
        Write-Step "would recreate $($tablespaceLinks.Count) tablespace junction(s) under $target\pg_tblspc"
    } else {
        New-Item -ItemType Directory -Force -Path $target | Out-Null
        # /XJ: without it robocopy follows <PGDATA>\pg_tblspc\<oid> and copies the
        # whole cold tier from the G: HDD onto the 500 GB hot volume (and the
        # free-space preflight, which does not follow junctions either, would
        # have undercounted it). The junctions are recreated below instead.
        & robocopy.exe $currentDataDir $target /E /XJ /COPY:DAT /DCOPY:DAT /R:2 /W:2 /MT:8 /NP "/LOG+:$robocopyLog" | Out-Null
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
        # The cluster refuses to start when a tablespace link is missing, so the
        # junctions are recreated (never copied) and then proven to exist.
        $recreatedLinks = [System.Collections.Generic.List[object]]::new()
        foreach ($link in $tablespaceLinks) {
            $linkPath = Join-Path $target $link.Relative
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $linkPath) | Out-Null
            if (-not (Test-Path -LiteralPath $linkPath)) {
                New-Item -ItemType Junction -Path $linkPath -Target $link.Target | Out-Null
            }
            $recreated = Get-Item -LiteralPath $linkPath -Force
            if (-not $recreated.Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)) {
                throw "Failed to recreate the tablespace junction $($link.Relative) at $linkPath"
            }
            $recreatedLinks.Add([ordered]@{ relative = $link.Relative; target = $link.Target })
            Write-Step "recreated tablespace junction $($link.Relative) -> $($link.Target)"
        }
        $copyVerification = [ordered]@{
            verified = $true; robocopy_exit_code = $robocopyExit
            files = $copiedFootprint.Files; bytes = $copiedFootprint.Bytes
            pg_control_sha256 = $controlTarget
            tablespace_links = $recreatedLinks.ToArray()
        }
        Write-Step ('copy verified: {0:N0} files, {1:N1} GB, pg_control checksum equal, {2} tablespace junction(s) recreated' -f `
            $copiedFootprint.Files, ($copiedFootprint.Bytes / 1GB), $recreatedLinks.Count)
    }

    # ----------------------------------------------------------------------
    # step 5: switch PGDATA_DIR and regenerate the managed configuration
    # ----------------------------------------------------------------------
    Write-Step "step 5/8 PGDATA_DIR -> $target and regenerate postgresql-stock-platform.conf"
    if (-not $WhatIf) {
        if (-not $copyVerification['verified']) { throw 'Refusing to switch PGDATA_DIR before the copy is verified' }
        Set-StockPlatformEnvValue -Path $RuntimeEnv -Name 'PGDATA_DIR' -Value $target
        $script:EnvSwitched = $true
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
        $script:OldDirectoryRenamed = $true
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
    # Deliberately printed WITHOUT -AcceptDataLoss: run as shown, the rollback
    # prints how many hours of writes it would discard and then refuses. Adding
    # the switch is the operator's explicit acknowledgement of that number.
    $receipt['rollback_command'] = "pwsh -NoProfile -File $PSCommandPath -Rollback -RollbackDataDir $keptPath"
    $receipt['rollback_note'] = 'Rollback discards every write made since this migration finished. The command above refuses unless -AcceptDataLoss is added, and refuses outright once the stock_cold tablespace exists but the image predates it. After the first hours, restore from a backup instead.'
    [pscustomobject]$receipt
} catch {
    # Steps 2-8 leave the database stopped and the nightly tasks disabled; the
    # run must never end there. Recover first, record what happened, then let
    # the original failure propagate.
    $failure = $_
    Write-Step "migration FAILED: $($failure.Exception.Message)" 'warning'
    $keptPathForReceipt = if ($script:OldDirectoryRenamed) { Join-Path (Split-Path -Parent $currentDataDir) $KeepOldName } else { '' }
    $recovery = Invoke-FailureRecovery -SourceDataDirectory $currentDataDir -TargetDataDirectory $target -RenamedSourceDirectory $keptPathForReceipt
    if (-not $WhatIf) {
        try {
            $failureReceipt = [ordered]@{
                action = 'migrate-postgres-data-directory'
                status = 'failed'
                started_at = $startedAt.ToString('o')
                failed_at = [DateTimeOffset]::Now.ToString('o')
                error = $failure.Exception.Message
                failed_step = ($failure.InvocationInfo.ScriptLineNumber)
                old_data_directory = $currentDataDir
                old_data_directory_renamed = $keptPathForReceipt
                new_data_directory = $target
                progress = [ordered]@{
                    runtimes_stopped = $script:RuntimesStopped
                    postgres_stopped = $script:PostgresStopped
                    pgdata_dir_switched = $script:EnvSwitched
                    old_directory_renamed = $script:OldDirectoryRenamed
                }
                recovery = $recovery
            }
            $failurePath = Join-Path $logs "postgres-data-migration-$stamp.failure.json"
            [IO.File]::WriteAllText($failurePath, (ConvertTo-Json -InputObject $failureReceipt -Depth 8), [Text.UTF8Encoding]::new($false))
            Write-Step "failure receipt written to $failurePath" 'warning'
        } catch {
            Write-Step "could not write the failure receipt: $($_.Exception.Message)" 'warning'
        }
    }
    throw
} finally {
    if (-not $WhatIf) { try { Stop-Transcript | Out-Null } catch { } }
}
