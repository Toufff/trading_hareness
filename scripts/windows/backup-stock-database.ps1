[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

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

$today = (Get-Date).ToString('yyyy-MM-dd')
$dayDir = Join-Path $backupRoot $today
New-Item -ItemType Directory -Force -Path $dayDir | Out-Null

$backupDrive = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($backupRoot))
$drive = Get-PSDrive -Name $backupDrive.TrimEnd('\', ':') -ErrorAction SilentlyContinue
$freeBytes = if ($drive) { $drive.Free } else { (New-Object IO.DriveInfo($backupDrive)).AvailableFreeSpace }
if ($freeBytes -lt $minimumFreeBytes) {
    throw "Refusing to start backup: only $freeBytes byte(s) free on $backupDrive, below the configured minimum of $minimumFreeBytes byte(s) (STOCK_BACKUP_MIN_FREE_BYTES)"
}

$dumpFile = Join-Path $dayDir "$($config['PGDATABASE'])-$today.dump"
if (Test-Path -LiteralPath $dumpFile) { throw "Backup already exists for today: $dumpFile" }

$env:PGPASSWORD = $dumpPassword
try {
    & $pgDump -Fc -h $config.PGHOST -p $config.PGPORT -U $dumpUser -d $config.PGDATABASE -f $dumpFile
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed with exit code $LASTEXITCODE" }
} finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}

$hash = (Get-FileHash -LiteralPath $dumpFile -Algorithm SHA256).Hash.ToLowerInvariant()
$sizeBytes = (Get-Item -LiteralPath $dumpFile).Length
[IO.File]::WriteAllText("$dumpFile.sha256", "$hash  $(Split-Path -Leaf $dumpFile)$([Environment]::NewLine)", [Text.UTF8Encoding]::new($false))

# Retention: keep every daily backup within $dailyRetentionDays, plus one
# backup per ISO week (the earliest available in that week) for the last
# $weeklyRetentionWeeks weeks beyond that, so a corruption discovered weeks
# later still has a recovery point.
$allDayDirs = @(Get-ChildItem -LiteralPath $backupRoot -Directory -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match '^\d{4}-\d{2}-\d{2}$' })
$removed = @(Select-StockBackupRetentionRemovals -DayNames @($allDayDirs.Name) -Now (Get-Date) `
    -DailyRetentionDays $dailyRetentionDays -WeeklyRetentionWeeks $weeklyRetentionWeeks)
foreach ($name in $removed) {
    $entry = $allDayDirs | Where-Object Name -eq $name | Select-Object -First 1
    if ($entry) { Remove-Item -LiteralPath $entry.FullName -Recurse -Force }
}

[pscustomobject]@{
    status = 'backed_up'
    database = $config.PGDATABASE
    dump_file = $dumpFile
    sha256 = $hash
    size_bytes = $sizeBytes
    backup_root = $backupRoot
    free_bytes_after = $freeBytes - $sizeBytes
    pruned_days = $removed
}
