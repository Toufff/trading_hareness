[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'stock-incremental-backup.psm1') -Force

function Read-EnvFile([string]$Path) {
    $result = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { $result[$parts[0]] = $parts[1] }
    }
    return $result
}

function Select-StockBackupRetentionRemovals {
    # Pure decision function (no filesystem I/O) so it can be unit tested in
    # isolation: given the set of existing "yyyy-MM-dd" backup day names,
    # returns which ones should be removed. Keeps every day within
    # DailyRetentionDays, plus one backup per ISO week (the earliest
    # available that week) for the WeeklyRetentionWeeks most recent weeks
    # beyond that.
    [CmdletBinding()]
    param(
        [string[]]$DayNames,
        [Parameter(Mandatory)][DateTime]$Now,
        [Parameter(Mandatory)][int]$DailyRetentionDays,
        [Parameter(Mandatory)][int]$WeeklyRetentionWeeks
    )
    $names = @($DayNames | Where-Object { $_ -match '^\d{4}-\d{2}-\d{2}$' })
    $keep = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($name in $names) {
        $date = [DateTime]::ParseExact($name, 'yyyy-MM-dd', $null)
        if (($Now.Date - $date.Date).TotalDays -le $DailyRetentionDays) { [void]$keep.Add($name) }
    }
    # Group by ISO week, then keep the earliest backup of each week that
    # falls within the last WeeklyRetentionWeeks calendar weeks -- measured
    # from $Now, not just "the N most recent weeks present in the data" (a
    # sparse or gappy backup history must not make an ancient backup look
    # recent just because nothing newer happens to exist in that list).
    $weekGroups = $names | Group-Object {
        $date = [DateTime]::ParseExact($_, 'yyyy-MM-dd', $null)
        '{0}-W{1:D2}' -f [System.Globalization.ISOWeek]::GetYear($date), [System.Globalization.ISOWeek]::GetWeekOfYear($date)
    }
    foreach ($group in $weekGroups) {
        $earliest = @($group.Group | Sort-Object)[0]
        $earliestDate = [DateTime]::ParseExact($earliest, 'yyyy-MM-dd', $null)
        $weeksAgo = [Math]::Floor(($Now.Date - $earliestDate.Date).TotalDays / 7)
        if ($weeksAgo -le $WeeklyRetentionWeeks) { [void]$keep.Add($earliest) }
    }
    return @($names | Where-Object { -not $keep.Contains($_) })
}

function Get-StockBackupExcludedTableData {
    # Pure decision function.  Parses STOCK_BACKUP_EXCLUDE_TABLE_DATA, a
    # semicolon separated list of "schema.table" whose *data* the nightly dump
    # leaves out (the schema is still dumped, so a restore recreates the empty
    # table).  This is the OPERATOR OVERRIDE only: the cold-tier twins are
    # computed at dump time by Resolve-StockBackupExcludedTableData, which is
    # what decides whether an exclusion is safe.  Production seeds no value.
    # Identifiers must be lower-case schema.table -- a typo must fail the run
    # rather than silently widen what the backup omits.  Repeats are collapsed
    # (listing a table twice is harmless, unlike an unparseable name).
    [CmdletBinding()]
    param([string]$Value)
    $text = if ($null -eq $Value) { '' } else { $Value.Trim() }
    if (-not $text) { return @() }
    $pattern = '^[a-z_][a-z0-9_]*$'
    $tables = [Collections.Generic.List[string]]::new()
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($entry in $text.Split(';')) {
        $item = $entry.Trim()
        if (-not $item) { continue }
        $parts = $item.Split('.')
        if ($parts.Count -ne 2 -or @($parts | Where-Object { $_ -cnotmatch $pattern }).Count -gt 0) {
            throw "Invalid excluded table '$item': expected lower-case schema.table"
        }
        if ($seen.Add($item)) { $tables.Add($item) }
    }
    return $tables.ToArray()
}

function Resolve-StockBackupExcludedTableData {
    # Pure decision function: which tables' data this run's dump may leave out.
    #
    # A cold-tier twin (quant.<table>_cold) holds rows that were moved out of
    # quant.<table>.  Leaving the twin's data out of the nightly dump is only
    # safe when those rows are carried by something else, and the only
    # something else is the hot table's incremental chunk chain: the rows were
    # exported by window while they were still hot, and chunks are never
    # pruned.  Excluding a twin whose hot table has no chain deletes the table
    # from the backup chain entirely -- roughly two months later (14 daily plus
    # 8 weekly dumps) its only copy is the live cold tablespace on the G: HDD.
    #
    # So the exclusion is COMPUTED here, at dump time, from the incremental
    # spec list rather than seeded as a static list by the installer:
    #   * every incremental table's own data (it is in the chain by definition,
    #     and only when this run's export succeeded -- a failed export degrades
    #     to a full dump instead of a hole);
    #   * the twin of every incremental table;
    #   * anything else the operator listed in STOCK_BACKUP_EXCLUDE_TABLE_DATA,
    #     which stays an override -- except a twin whose hot table has no
    #     chain, which is refused and reported rather than silently honoured.
    [CmdletBinding()]
    param(
        [string[]]$IncrementalTables = @(),
        [bool]$IncrementalSucceeded = $true,
        [string[]]$ConfiguredExclusions = @()
    )
    $chain = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    foreach ($table in @($IncrementalTables)) { [void]$chain.Add($table) }
    $excluded = [Collections.Generic.List[string]]::new()
    $refused = [Collections.Generic.List[string]]::new()
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::Ordinal)
    if ($IncrementalSucceeded) {
        foreach ($table in @($IncrementalTables)) { if ($seen.Add($table)) { $excluded.Add($table) } }
    }
    # The twins are excluded whether or not THIS run's export succeeded: their
    # rows were captured by earlier runs of the chain, not by tonight's.
    foreach ($table in @($IncrementalTables)) {
        $twin = "${table}_cold"
        if ($seen.Add($twin)) { $excluded.Add($twin) }
    }
    foreach ($entry in @($ConfiguredExclusions)) {
        if ($entry.EndsWith('_cold')) {
            $hot = $entry.Substring(0, $entry.Length - 5)
            if (-not $chain.Contains($hot)) {
                if (-not $refused.Contains($entry)) { $refused.Add($entry) }
                continue
            }
        }
        if ($seen.Add($entry)) { $excluded.Add($entry) }
    }
    return [pscustomobject]@{ Excluded = $excluded.ToArray(); Refused = $refused.ToArray() }
}

function ConvertTo-Bytes {
    # Accepts either a plain byte count or a "<number><unit>" string
    # (KB/MB/GB, binary units) so runtime.env can express sizes readably.
    param([string]$Value, [int64]$Default)
    if (-not $Value) { return $Default }
    $trimmed = $Value.Trim()
    $match = [regex]::Match($trimmed, '^(?<num>\d+(?:\.\d+)?)\s*(?<unit>[KMGT]?B)?$', [Text.RegularExpressions.RegexOptions]::IgnoreCase)
    if (-not $match.Success) { throw "Invalid byte size value: $Value" }
    $number = [double]$match.Groups['num'].Value
    $unit = $match.Groups['unit'].Value.ToUpperInvariant()
    $multiplier = switch ($unit) {
        'KB' { 1KB }
        'MB' { 1MB }
        'GB' { 1GB }
        'TB' { 1TB }
        default { 1 }
    }
    return [int64]($number * $multiplier)
}

$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
if (-not (Test-Path -LiteralPath $RuntimeEnv -PathType Leaf)) { throw "Missing runtime environment file: $RuntimeEnv" }
$config = Read-EnvFile $RuntimeEnv
foreach ($required in 'PGHOST', 'PGPORT', 'PGDATABASE') {
    if (-not $config[$required]) { throw "Missing $required in $RuntimeEnv" }
}
# The dump user needs to be able to read every table across schemas
# (quant, public, ...); the admin role that owns the cluster is used rather
# than the application role so a backup never silently misses a table the
# app role was not explicitly granted SELECT on.
$dumpUser = if ($config['PGADMINUSER']) { $config['PGADMINUSER'] } else { $config['PGUSER'] }
$dumpPassword = if ($config['PGADMINUSER']) { $config['PGADMINPASSWORD'] } else { $config['PGPASSWORD'] }
if (-not $dumpUser -or -not $dumpPassword) { throw "Missing PGADMINUSER/PGADMINPASSWORD (or PGUSER/PGPASSWORD) in $RuntimeEnv" }

# Backup root, retention and disk-space thresholds all come from
# runtime.env so nothing here is hardcoded per-deployment; the values below
# are only the documented defaults (matching the platform's own G:\ layout)
# used when an operator has not overridden them yet.
$backupRoot = if ($config['STOCK_BACKUP_ROOT']) { $config['STOCK_BACKUP_ROOT'] } else { Join-Path $platform 'backups' }
$dailyRetentionDays = if ($config['STOCK_BACKUP_DAILY_RETENTION_DAYS']) { [int]$config['STOCK_BACKUP_DAILY_RETENTION_DAYS'] } else { 14 }
$weeklyRetentionWeeks = if ($config['STOCK_BACKUP_WEEKLY_RETENTION_WEEKS']) { [int]$config['STOCK_BACKUP_WEEKLY_RETENTION_WEEKS'] } else { 8 }
$minimumFreeBytes = ConvertTo-Bytes -Value $config['STOCK_BACKUP_MIN_FREE_BYTES'] -Default 5GB

$postgresRoot = Get-ChildItem -LiteralPath (Join-Path $platform 'runtime') -Directory -Filter 'postgresql-*' -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
if (-not $postgresRoot) { throw "PostgreSQL runtime not found under $(Join-Path $platform 'runtime')" }
$pgDump = Join-Path $postgresRoot.FullName 'bin\pg_dump.exe'
if (-not (Test-Path -LiteralPath $pgDump -PathType Leaf)) { throw "Missing pg_dump.exe: $pgDump" }
$psql = Join-Path $postgresRoot.FullName 'bin\psql.exe'

# Large, mostly append-only tables are exported incrementally (see
# stock-incremental-backup.psm1) and their data is left out of the nightly
# dump -- but only after this run's incremental export fully succeeded, so a
# failed export degrades to a full dump instead of a hole in the backup.
$incrementalSpecs = @(Get-StockIncrementalTableSpecs -Value $config['STOCK_BACKUP_INCREMENTAL_TABLES'])
if ($incrementalSpecs.Count -gt 0 -and -not (Test-Path -LiteralPath $psql -PathType Leaf)) { throw "Missing psql.exe: $psql" }

$today = (Get-Date).ToString('yyyy-MM-dd')

# The scheduled task runs this script with no console attached, so the summary
# object below is the only account of what happened - and it went nowhere.
# Every outcome (success, skip and failure alike) is now appended to
# logs\stock-backup.jsonl, one JSON object per run, so an operator can tell a
# working nightly backup from one that has not run in weeks without waiting for
# a restore. Appending rather than overwriting keeps a same-day retry from
# erasing the evidence (size, SHA-256) of the run that actually took the dump.
function Write-BackupRecord {
    param([Parameter(Mandatory)][hashtable]$Record)
    $Record['recorded_at'] = (Get-Date).ToString('o')
    $logDir = Join-Path $platform 'logs'
    try {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        $line = (ConvertTo-Json -InputObject $Record -Depth 6 -Compress) + [Environment]::NewLine
        [IO.File]::AppendAllText(
            (Join-Path $logDir 'stock-backup.jsonl'), $line, [Text.UTF8Encoding]::new($false))
    } catch {
        Write-Warning "Failed to write backup record: $($_.Exception.Message)"
    }
}

trap {
    Write-BackupRecord -Record @{ status = 'failed'; error = $_.Exception.Message }
    break
}

$dayDir = Join-Path $backupRoot $today
New-Item -ItemType Directory -Force -Path $dayDir | Out-Null

$backupDrive = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($backupRoot))
$drive = Get-PSDrive -Name $backupDrive.TrimEnd('\', ':') -ErrorAction SilentlyContinue
$freeBytes = if ($drive) { $drive.Free } else { (New-Object IO.DriveInfo($backupDrive)).AvailableFreeSpace }
if ($freeBytes -lt $minimumFreeBytes) {
    throw "Refusing to start backup: only $freeBytes byte(s) free on $backupDrive, below the configured minimum of $minimumFreeBytes byte(s) (STOCK_BACKUP_MIN_FREE_BYTES)"
}

# Incremental export runs on every invocation, including a same-day retry
# after the dump already exists: its watermark makes it idempotent.
$incrementalResults = [Collections.Generic.List[object]]::new()
$incrementalError = $null
if ($incrementalSpecs.Count -gt 0) {
    $connection = @{
        Psql = $psql; Host = $config.PGHOST; Port = $config.PGPORT; Database = $config.PGDATABASE
        User = $dumpUser; Password = $dumpPassword
    }
    try {
        foreach ($spec in $incrementalSpecs) {
            $incrementalResults.Add((Invoke-StockIncrementalBackup -Connection $connection -Spec $spec -BackupRoot $backupRoot))
        }
    } catch {
        $incrementalError = $_.Exception.Message
    }
}
$exclusionDecision = Resolve-StockBackupExcludedTableData `
    -IncrementalTables @($incrementalSpecs | ForEach-Object Table) `
    -IncrementalSucceeded ($null -eq $incrementalError) `
    -ConfiguredExclusions @(Get-StockBackupExcludedTableData -Value $config['STOCK_BACKUP_EXCLUDE_TABLE_DATA'])
$excludedTableData = @($exclusionDecision.Excluded)
$refusedExclusions = @($exclusionDecision.Refused)
foreach ($table in $refusedExclusions) {
    # Loud, and in the run record: honouring this would take the table out of
    # every future dump while nothing else carries its rows.
    Write-Warning "Ignoring STOCK_BACKUP_EXCLUDE_TABLE_DATA entry ${table}: its hot table has no incremental chunk chain, so excluding it would leave the rows with no backup."
}

$dumpFile = Join-Path $dayDir "$($config['PGDATABASE'])-$today.dump"
if (Test-Path -LiteralPath $dumpFile) {
    # A daily job that is retried, or run by hand before the trigger fires,
    # must not fail: today's recovery point already exists, which is the
    # outcome the job exists to guarantee.
    $record = @{
        status = if ($incrementalError) { 'failed' } else { 'skipped' }
        reason = 'backup already exists for today'; dump_file = $dumpFile
        incremental = $incrementalResults.ToArray(); incremental_error = $incrementalError
        refused_table_data_exclusions = $refusedExclusions
    }
    Write-BackupRecord -Record $record
    [pscustomobject]$record
    if ($incrementalError) { exit 1 }
    return
}

$dumpArguments = @('-Fc', '-h', $config.PGHOST, '-p', $config.PGPORT, '-U', $dumpUser, '-d', $config.PGDATABASE, '-f', $dumpFile)
foreach ($table in $excludedTableData) { $dumpArguments += "--exclude-table-data=$table" }
$env:PGPASSWORD = $dumpPassword
try {
    & $pgDump @dumpArguments
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE" }
} finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}

$hash = (Get-FileHash -LiteralPath $dumpFile -Algorithm SHA256).Hash.ToLowerInvariant()
$sizeBytes = (Get-Item -LiteralPath $dumpFile).Length
[IO.File]::WriteAllText("$dumpFile.sha256", "$hash  $(Split-Path -Leaf $dumpFile)$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))
[IO.File]::WriteAllText("$dumpFile.excluded-table-data.json",
    (ConvertTo-Json -InputObject @{ excluded_table_data = $excludedTableData; incremental = $incrementalResults.ToArray() } -Depth 6),
    [Text.UTF8Encoding]::new($false))

# Retention: keep every daily backup within $dailyRetentionDays, plus one
# backup per ISO week (the earliest available in that week) for the last
# $weeklyRetentionWeeks weeks beyond that, so a corruption discovered weeks
# later still has a recovery point.  Only yyyy-MM-dd directories are
# considered; incremental chunks under backups\incremental are never pruned.
$allDayDirs = @(Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' })
$removed = @(Select-StockBackupRetentionRemovals -DayNames @($allDayDirs.Name) -Now (Get-Date) `
    -DailyRetentionDays $dailyRetentionDays -WeeklyRetentionWeeks $weeklyRetentionWeeks)
foreach ($name in $removed) {
    $entry = $allDayDirs | Where-Object Name -eq $name | Select-Object -First 1
    if ($entry) { Remove-Item -LiteralPath $entry.FullName -Recurse -Force }
}

$summary = @{
    # A failed incremental export still leaves a complete (full) dump, but
    # the run is recorded as degraded so it is not mistaken for a normal night.
    status = if ($incrementalError) { 'degraded_full_dump' } else { 'backed_up' }
    database = $config.PGDATABASE
    dump_file = $dumpFile
    sha256 = $hash
    size_bytes = $sizeBytes
    backup_root = $backupRoot
    free_bytes_after = $freeBytes - $sizeBytes
    pruned_days = $removed
    excluded_table_data = $excludedTableData
    refused_table_data_exclusions = $refusedExclusions
    incremental = $incrementalResults.ToArray()
    incremental_error = $incrementalError
}
Write-BackupRecord -Record $summary
[pscustomobject]$summary
if ($incrementalError) { exit 1 }
