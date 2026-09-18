[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform'
)

# Restore drill for the incremental backup against the real PostgreSQL
# runtime.  Two throw-away databases are created and always dropped: rows are
# exported in windows, an old row is modified and new rows arrive, a base dump
# without the table's data is restored into the second database, the chunks
# are replayed, and both tables must be identical row for row.  Nothing in the
# platform database is read or written.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'stock-incremental-backup.psm1') -Force

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

$config = @{}
foreach ($line in [IO.File]::ReadAllLines($RuntimeEnv, [Text.Encoding]::UTF8)) {
    if (-not $line -or $line.StartsWith('#')) { continue }
    $parts = $line.Split('=', 2)
    if ($parts.Count -eq 2) { $config[$parts[0]] = $parts[1] }
}
$bin = Join-Path (Get-ChildItem -LiteralPath (Join-Path $PlatformRoot 'runtime') -Directory -Filter 'postgresql-*' |
    Sort-Object Name -Descending | Select-Object -First 1).FullName 'bin'
$suffix = "{0}_{1}" -f $PID, [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
$sourceDb = "stock_backup_drill_src_$suffix"
$targetDb = "stock_backup_drill_dst_$suffix"
$base = @{ Psql = (Join-Path $bin 'psql.exe'); Host = $config.PGHOST; Port = $config.PGPORT
           User = $config.PGADMINUSER; Password = $config.PGADMINPASSWORD }
$admin = $base.Clone(); $admin.Database = 'postgres'
$source = $base.Clone(); $source.Database = $sourceDb
$target = $base.Clone(); $target.Database = $targetDb
$backupRoot = Join-Path ([IO.Path]::GetTempPath()) "stock-incremental-drill-$suffix"
$spec = @(Get-StockIncrementalTableSpecs -Value 'quant.drill_observations:created_at:updated_at')[0]
$fingerprint = "SELECT count(*) || ':' || md5(coalesce(string_agg(t::text, '|' ORDER BY observation_id), '')) FROM quant.drill_observations t"

try {
    [void](Invoke-StockPsqlScalar -Connection $admin -Sql "CREATE DATABASE $sourceDb")
    [void](Invoke-StockPsqlScalar -Connection $admin -Sql "CREATE DATABASE $targetDb")
    [void](Invoke-StockPsqlScalar -Connection $source -Sql @'
CREATE SCHEMA quant;
CREATE TABLE quant.drill_runs (run_id integer PRIMARY KEY);
CREATE TABLE quant.drill_observations (
    observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id integer REFERENCES quant.drill_runs(run_id) ON DELETE SET NULL,
    available_at timestamptz NOT NULL,
    payload jsonb NOT NULL,
    note text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz
);
CREATE FUNCTION quant.drill_touch() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at := clock_timestamp(); RETURN NEW; END; $$;
CREATE TRIGGER drill_touch BEFORE UPDATE ON quant.drill_observations FOR EACH ROW EXECUTE FUNCTION quant.drill_touch();
-- The cold twin the storage tier creates for a tiered table. Its rows were
-- exported while they were still in the hot table, so the nightly dump leaves
-- its data out -- but it has no incremental chunk chain of its own, which is
-- exactly the case that used to make every such dump unrestorable.
CREATE TABLE quant.drill_observations_cold (LIKE quant.drill_observations INCLUDING DEFAULTS INCLUDING INDEXES);
INSERT INTO quant.drill_runs VALUES (1), (2);
INSERT INTO quant.drill_observations(run_id, available_at, payload, note, created_at)
SELECT CASE WHEN g % 2 = 0 THEN 1 ELSE 2 END,
       now() - make_interval(hours => g),
       jsonb_build_object('g', g, 'text', E'line1\nline2\ttab \\ backslash \u4E2D\u6587'),
       CASE WHEN g % 5 = 0 THEN NULL ELSE E'note\r\nwith \\. marker' END,
       now() - make_interval(hours => g)
  FROM generate_series(1, 80) g;
INSERT INTO quant.drill_observations_cold SELECT * FROM quant.drill_observations LIMIT 3;
'@)
    $lag = [TimeSpan]::FromSeconds(1)
    Start-Sleep -Seconds 2
    $first = Invoke-StockIncrementalBackup -Connection $source -Spec $spec -BackupRoot $backupRoot -SafetyLag $lag
    Assert-True ($first.rows -eq 80) "the first run exports every existing row (got $($first.rows))"
    Assert-True ($first.chunks -ge 4) "80 hourly rows span at least four local days (got $($first.chunks) chunks)"

    # An old row is modified, a parent is deleted (ON DELETE SET NULL touches children), new rows arrive.
    [void](Invoke-StockPsqlScalar -Connection $source -Sql @'
UPDATE quant.drill_observations SET available_at = now(), note = 'modified' WHERE payload->>'g' = '40';
DELETE FROM quant.drill_runs WHERE run_id = 2;
INSERT INTO quant.drill_observations(run_id, available_at, payload) SELECT 1, now(), jsonb_build_object('late', g) FROM generate_series(1, 5) g;
'@)
    Start-Sleep -Seconds 2
    $second = Invoke-StockIncrementalBackup -Connection $source -Spec $spec -BackupRoot $backupRoot -SafetyLag $lag
    Assert-True ($second.rows -eq 46) "the second run exports 5 new rows plus 41 modified rows (1 update, 40 set-null children) (got $($second.rows))"
    $third = Invoke-StockIncrementalBackup -Connection $source -Spec $spec -BackupRoot $backupRoot -SafetyLag $lag
    Assert-True ($third.rows -eq 0) 'an immediate re-run exports nothing new'

    # Base dump without the table's data, restored, then every chunk replayed.
    $dumpFile = Join-Path $backupRoot 'base.dump'
    $env:PGPASSWORD = $config.PGADMINPASSWORD
    try {
        & (Join-Path $bin 'pg_dump.exe') -Fc -h $config.PGHOST -p $config.PGPORT -U $config.PGADMINUSER -d $sourceDb -f $dumpFile "--exclude-table-data=$($spec.Table)"
        if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE" }
        & (Join-Path $bin 'pg_restore.exe') -h $config.PGHOST -p $config.PGPORT -U $config.PGADMINUSER -d $targetDb --exit-on-error $dumpFile
        if ($LASTEXITCODE -ne 0) { throw "pg_restore failed with exit code $LASTEXITCODE" }
    } finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
    Assert-True ((Invoke-StockPsqlScalar -Connection $target -Sql 'SELECT count(*) FROM quant.drill_observations') -eq '0') 'the base dump carries no rows of the incremental table'
    Assert-True ((Invoke-StockPsqlScalar -Connection $target -Sql 'SELECT count(*) FROM quant.drill_runs') -eq '1') 'the base dump carries the other tables'

    $restored = Import-StockIncrementalChunks -Connection $target -Spec $spec -BackupRoot $backupRoot
    Assert-True ($restored.rows_applied -eq 126) "every exported row version is applied (got $($restored.rows_applied))"
    $expected = Invoke-StockPsqlScalar -Connection $source -Sql $fingerprint
    $actual = Invoke-StockPsqlScalar -Connection $target -Sql $fingerprint
    Assert-True ($expected -eq $actual) "restored table must equal the source (source $expected, restored $actual)"
    Assert-True ($expected.StartsWith('85:')) "the source holds 85 rows (got $expected)"

    # Tampering with a chunk is detected before anything is loaded.
    $chunk = Get-ChildItem -LiteralPath (Join-Path (Join-Path $backupRoot 'incremental') $spec.Table) -Filter '*.copy.gz' | Select-Object -First 1
    [IO.File]::AppendAllText($chunk.FullName, 'x')
    $tamperRejected = $false
    try { [void](Import-StockIncrementalChunks -Connection $target -Spec $spec -BackupRoot $backupRoot) }
    catch { $tamperRejected = $_.Exception.Message -match 'SHA-256 mismatch' }
    Assert-True $tamperRejected 'a modified chunk must be rejected by its SHA-256'

    # The nightly script end to end, against the drill database and a
    # throw-away platform root (so its log and backups never touch production).
    $platformDrill = Join-Path $backupRoot 'platform'
    New-Item -ItemType Directory -Force -Path (Join-Path $platformDrill 'config') | Out-Null
    New-Item -ItemType Junction -Path (Join-Path $platformDrill 'runtime') -Target (Join-Path $PlatformRoot 'runtime') | Out-Null
    $drillEnv = Join-Path (Join-Path $platformDrill 'config') 'runtime.env'
    [IO.File]::WriteAllLines($drillEnv, [string[]]@(
        "PGHOST=$($config.PGHOST)", "PGPORT=$($config.PGPORT)", "PGDATABASE=$sourceDb",
        "PGADMINUSER=$($config.PGADMINUSER)", "PGADMINPASSWORD=$($config.PGADMINPASSWORD)",
        "STOCK_BACKUP_INCREMENTAL_TABLES=$($spec.Table):created_at:updated_at", 'STOCK_BACKUP_MIN_FREE_BYTES=1MB'
    ), [Text.UTF8Encoding]::new($false))
    [void](Invoke-StockPsqlScalar -Connection $source -Sql "INSERT INTO quant.drill_observations(run_id, available_at, payload, created_at) VALUES (1, now(), '{}'::jsonb, now() - interval '2 hours')")
    $nightly = & pwsh -NoLogo -NoProfile -NonInteractive -File (Join-Path (Split-Path -Parent $PSScriptRoot) 'backup-stock-database.ps1') -RuntimeEnv $drillEnv -PlatformRoot $platformDrill
    if ($LASTEXITCODE -ne 0) { throw "nightly backup script failed with exit code ${LASTEXITCODE}: $nightly" }
    $record = Get-Content -LiteralPath (Join-Path (Join-Path $platformDrill 'logs') 'stock-backup.jsonl') -Encoding UTF8 | Select-Object -Last 1 | ConvertFrom-Json
    Assert-True ($record.status -eq 'backed_up') "the nightly script records backed_up (got $($record.status): $($record.incremental_error))"
    Assert-True (@($record.excluded_table_data) -contains $spec.Table) 'the nightly dump excludes the incremental table data'
    # The exclusion is computed from the incremental spec list at dump time, so
    # the twin of a table WITH a chain is excluded and nothing else is.
    Assert-True (@($record.excluded_table_data) -contains "$($spec.Table)_cold") 'the nightly dump excludes the cold twin of the incremental table'
    Assert-True (@($record.excluded_table_data).Count -eq 2) "only the incremental table and its twin are excluded (got $(@($record.excluded_table_data) -join ','))"
    Assert-True (@($record.refused_table_data_exclusions).Count -eq 0) 'nothing was refused: every excluded twin has a chain behind it'
    Assert-True (@($record.incremental)[0].rows -eq 81) "a fresh chain exports every row older than the 30-minute safety lag: 80 history rows and the 2-hour-old one, not the 5 just inserted (got $(ConvertTo-Json -InputObject $record.incremental -Compress))"
    Assert-True (Test-Path -LiteralPath "$($record.dump_file).excluded-table-data.json" -PathType Leaf) 'the dump records which table data it excludes'
    $rerun = & pwsh -NoLogo -NoProfile -NonInteractive -File (Join-Path (Split-Path -Parent $PSScriptRoot) 'backup-stock-database.ps1') -RuntimeEnv $drillEnv -PlatformRoot $platformDrill
    if ($LASTEXITCODE -ne 0) { throw "nightly backup re-run failed with exit code ${LASTEXITCODE}: $rerun" }
    $rerunRecord = Get-Content -LiteralPath (Join-Path (Join-Path $platformDrill 'logs') 'stock-backup.jsonl') -Encoding UTF8 | Select-Object -Last 1 | ConvertFrom-Json
    Assert-True ($rerunRecord.status -eq 'skipped' -and @($rerunRecord.incremental)[0].rows -eq 0) 'a same-day re-run skips the dump and exports nothing twice'

    # The restore entry point on the nightly output: a new database, base dump,
    # then the chunk chain.  It must equal every source row older than the
    # watermark (the rows inside the safety lag are the next night's work).
    # An excluded table with no chunk directory (the cold twin) must be skipped
    # and reported, never thrown on: the throw used to land AFTER the restore
    # had created the database and replayed the base dump, which made every
    # dump taken with a cold twin excluded unrestorable.
    $nightlyBackupRoot = Join-Path $platformDrill 'backups'
    $twinSpec = @(Get-StockIncrementalTableSpecs -Value "$($spec.Table)_cold:created_at")[0]
    Assert-True (-not (Test-Path -LiteralPath (Join-Path (Join-Path $nightlyBackupRoot 'incremental') $twinSpec.Table))) 'the cold twin has no chunk directory'
    $twinImport = Import-StockIncrementalChunks -Connection $target -Spec $twinSpec -BackupRoot $nightlyBackupRoot
    Assert-True ($twinImport.status -eq 'no_chunk_chain') "an excluded table without a chunk chain must be skipped, not thrown on (got $($twinImport.status))"
    Assert-True ($twinImport.chunks -eq 0 -and $twinImport.rows_applied -eq 0) 'a skipped table applies nothing'

    $restoreDb = "stock_backup_drill_rst_$suffix"
    $restoreScript = Join-Path (Split-Path -Parent $PSScriptRoot) 'restore-stock-database.ps1'
    $restoreResult = & pwsh -NoLogo -NoProfile -NonInteractive -File $restoreScript -DumpFile $record.dump_file -TargetDatabase $restoreDb `
        -RuntimeEnv $drillEnv -PlatformRoot $platformDrill 2>&1
    if ($LASTEXITCODE -ne 0) { throw "restore script failed with exit code ${LASTEXITCODE}: $restoreResult" }
    $restoreText = ($restoreResult | Out-String)
    Assert-True ($restoreText -match 'no_chunk_chain') "the restore must report the skipped chain: $restoreText"
    Assert-True ($restoreText -match [regex]::Escape("$($spec.Table)_cold")) 'the restore must name the table it skipped'
    $restoreConnection = $base.Clone(); $restoreConnection.Database = $restoreDb
    Assert-True ((Invoke-StockPsqlScalar -Connection $restoreConnection -Sql "SELECT count(*) FROM $($spec.Table)_cold") -eq '0') `
        'the cold twin is restored as an empty table: its data was excluded from the dump and carried by the hot chain'
    $watermark = ConvertTo-StockSqlTimestamp (ConvertTo-StockDateTimeOffset @($rerunRecord.incremental)[0].watermark)
    $sourceBeforeWatermark = Invoke-StockPsqlScalar -Connection $source -Sql ($fingerprint.Replace(' FROM quant.drill_observations t', " FROM quant.drill_observations t WHERE created_at < $watermark"))
    $restoredByScript = Invoke-StockPsqlScalar -Connection $restoreConnection -Sql $fingerprint
    Assert-True ($sourceBeforeWatermark -eq $restoredByScript) "the restore script reproduces every row before the watermark (source $sourceBeforeWatermark, restored $restoredByScript)"
    $liveRefused = $false
    try { & pwsh -NoLogo -NoProfile -NonInteractive -File $restoreScript -DumpFile $record.dump_file -TargetDatabase $sourceDb -RuntimeEnv $drillEnv -PlatformRoot $platformDrill 2>&1 | Out-Null; $liveRefused = $LASTEXITCODE -ne 0 } catch { $liveRefused = $true }
    Assert-True $liveRefused 'the restore script refuses to restore into the configured platform database'

    [pscustomobject]@{
        passed = $true
        nightly_script_status = $record.status
        nightly_rerun_status = $rerunRecord.status
        restore_script_matches_source = $true
        restore_into_live_database_refused = $true
        first_run_rows = $first.rows
        first_run_chunks = $first.chunks
        second_run_rows = $second.rows
        rerun_rows = $third.rows
        restored_rows_applied = $restored.rows_applied
        restored_fingerprint_matches = $true
        tampered_chunk_rejected = $true
        cold_twin_excluded_from_dump = $true
        excluded_table_without_chunks_skipped = 'no_chunk_chain'
    }
} finally {
    foreach ($database in $sourceDb, $targetDb, "stock_backup_drill_rst_$suffix") {
        try { [void](Invoke-StockPsqlScalar -Connection $admin -Sql "DROP DATABASE IF EXISTS $database WITH (FORCE)") }
        catch { Write-Warning "Failed to drop drill database ${database}: $($_.Exception.Message)" }
    }
    $runtimeJunction = Join-Path (Join-Path $backupRoot 'platform') 'runtime'
    if (Test-Path -LiteralPath $runtimeJunction) { [IO.Directory]::Delete($runtimeJunction) }
    Remove-Item -LiteralPath $backupRoot -Recurse -Force -ErrorAction SilentlyContinue
}
