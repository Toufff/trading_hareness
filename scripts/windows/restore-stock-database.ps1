[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$DumpFile,
    [Parameter(Mandatory)][string]$TargetDatabase,
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$BackupRoot = ''
)

# Restores a nightly backup into a NEW database: the base dump first, then
# every incremental chunk of each table whose data the dump excluded (read
# from the dump's .excluded-table-data.json).  It never writes to the live
# platform database; point the platform at the restored database only after
# checking it.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'stock-incremental-backup.psm1') -Force

if ($TargetDatabase -cnotmatch '^[a-z_][a-z0-9_]*$') { throw "TargetDatabase must be a lower-case identifier: $TargetDatabase" }
$config = @{}
foreach ($line in [IO.File]::ReadAllLines($RuntimeEnv, [Text.Encoding]::UTF8)) {
    if (-not $line -or $line.StartsWith('#')) { continue }
    $parts = $line.Split('=', 2)
    if ($parts.Count -eq 2) { $config[$parts[0]] = $parts[1] }
}
if ($TargetDatabase -eq $config.PGDATABASE) { throw "Refusing to restore into the live platform database $TargetDatabase" }
$user = if ($config['PGADMINUSER']) { $config['PGADMINUSER'] } else { $config['PGUSER'] }
$password = if ($config['PGADMINUSER']) { $config['PGADMINPASSWORD'] } else { $config['PGPASSWORD'] }
if (-not $BackupRoot) { $BackupRoot = if ($config['STOCK_BACKUP_ROOT']) { $config['STOCK_BACKUP_ROOT'] } else { Join-Path $PlatformRoot 'backups' } }
$bin = Join-Path (Get-ChildItem -LiteralPath (Join-Path $PlatformRoot 'runtime') -Directory -Filter 'postgresql-*' |
    Sort-Object Name -Descending | Select-Object -First 1).FullName 'bin'

$hashFile = "$DumpFile.sha256"
if (Test-Path -LiteralPath $hashFile -PathType Leaf) {
    $expected = ([IO.File]::ReadAllText($hashFile).Split(' ')[0]).Trim()
    if ((Get-FileHash -LiteralPath $DumpFile -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) { throw "SHA-256 mismatch for $DumpFile" }
}
$excludedFile = "$DumpFile.excluded-table-data.json"
$excluded = if (Test-Path -LiteralPath $excludedFile -PathType Leaf) {
    @((Get-Content -LiteralPath $excludedFile -Raw -Encoding UTF8 | ConvertFrom-Json).excluded_table_data)
} else { @() }
# The chunk chain for an excluded table is replayed with that table's spec;
# only the table name matters for locating and applying the chunks.
$specs = @($excluded | ForEach-Object { @(Get-StockIncrementalTableSpecs -Value "${_}:created_at")[0] })

$connection = @{ Psql = (Join-Path $bin 'psql.exe'); Host = $config.PGHOST; Port = $config.PGPORT; User = $user; Password = $password }
$admin = $connection.Clone(); $admin.Database = 'postgres'
$target = $connection.Clone(); $target.Database = $TargetDatabase
if ((Invoke-StockPsqlScalar -Connection $admin -Sql "SELECT count(*) FROM pg_database WHERE datname='$TargetDatabase'") -ne '0') {
    throw "Target database $TargetDatabase already exists; restore only into a new database"
}
[void](Invoke-StockPsqlScalar -Connection $admin -Sql "CREATE DATABASE $TargetDatabase")

$env:PGPASSWORD = $password
try {
    & (Join-Path $bin 'pg_restore.exe') -h $config.PGHOST -p $config.PGPORT -U $user -d $TargetDatabase --exit-on-error $DumpFile
    if ($LASTEXITCODE -ne 0) { throw "pg_restore failed with exit code $LASTEXITCODE" }
} finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }

# Import-StockIncrementalChunks reports 'no_chunk_chain' instead of throwing for
# an excluded table that never had a chain of its own (the cold twins: their
# rows left through the hot table's chain). Those are surfaced here so a restore
# is never silently incomplete, but they must not abort a restore that has
# already created the database and replayed the base dump.
$tables = @(foreach ($spec in $specs) { Import-StockIncrementalChunks -Connection $target -Spec $spec -BackupRoot $BackupRoot })
$skipped = @($tables | Where-Object { $_.status -eq 'no_chunk_chain' } | ForEach-Object { $_.table })
foreach ($table in $skipped) {
    Write-Warning "No incremental chunk chain for excluded table $table under $BackupRoot\incremental; its rows are whatever the base dump and the other chains carry."
}
[pscustomobject]@{
    status = 'restored'
    target_database = $TargetDatabase
    dump_file = $DumpFile
    incremental_tables = $tables
    skipped_no_chunks = $skipped
}
