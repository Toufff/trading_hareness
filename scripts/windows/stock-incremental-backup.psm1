Set-StrictMode -Version Latest

# Incremental export for large, mostly append-only tables.
#
# The nightly pg_dump grew with raw_market_observations (9 GB, millions of
# rows a day) and competed with the live collectors for the one HDD that holds
# both the database and the backups.  Such tables are instead exported by
# window: each run copies the rows created -- or, when the table has an
# update-stamp column, modified -- in [lower, upper) into a gzip chunk,
# verifies the row count against the database, and only then advances a
# watermark.  The nightly pg_dump excludes those tables' data, so a restore is
#   base dump (schema + every other table) + every chunk of each table in order,
# where a later chunk's version of a row replaces an earlier one.
#
# Chunks are never pruned by the base-dump retention: together they are the
# only copy of the table's history.  A row deleted from the live table stays
# in the chunks, so a restore can hold more history than the database had.

$script:IdentifierPattern = '^[a-z_][a-z0-9_]*$'

if (-not ('StockBackupStreams' -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.IO;
public static class StockBackupStreams {
    // Copies source to destination and returns { bytes, rows }.  COPY text
    // format escapes embedded newlines, so each 0x0A byte ends exactly one row.
    public static long[] CopyCountingRows(Stream source, Stream destination) {
        var buffer = new byte[1 << 20];
        long bytes = 0, rows = 0;
        int read;
        while ((read = source.Read(buffer, 0, buffer.Length)) > 0) {
            destination.Write(buffer, 0, read);
            bytes += read;
            rows += new ReadOnlySpan<byte>(buffer, 0, read).Count((byte)10);
        }
        return new long[] { bytes, rows };
    }
}
'@
}

function Get-StockIncrementalTableSpecs {
    # Pure.  Parses STOCK_BACKUP_INCREMENTAL_TABLES:
    #   "schema.table:created_column[:updated_column];..."   explicit list
    #   "" / $null                                           the documented default
    #   "none"                                               disable incremental export
    [CmdletBinding()]
    param([string]$Value)
    $text = if ($null -eq $Value) { '' } else { $Value.Trim() }
    if (-not $text) { $text = 'quant.raw_market_observations:created_at:updated_at' }
    if ($text -ieq 'none') { return @() }
    $specs = [Collections.Generic.List[object]]::new()
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($entry in $text.Split(';')) {
        $item = $entry.Trim()
        if (-not $item) { continue }
        $parts = @($item.Split(':') | ForEach-Object { $_.Trim() })
        if ($parts.Count -notin 2, 3) { throw "Invalid incremental table spec '$item': expected schema.table:created_column[:updated_column]" }
        $nameParts = $parts[0].Split('.')
        $identifiers = @($nameParts) + @($parts[1..($parts.Count - 1)])
        if ($nameParts.Count -ne 2 -or @($identifiers | Where-Object { $_ -cnotmatch $script:IdentifierPattern }).Count -gt 0) {
            throw "Invalid incremental table spec '$item': identifiers must be lower-case schema.table:column[:column]"
        }
        $table = "$($nameParts[0]).$($nameParts[1])"
        if (-not $seen.Add($table)) { throw "Duplicate incremental table spec for $table" }
        $specs.Add([pscustomobject]@{
            Schema = $nameParts[0]; Name = $nameParts[1]; Table = $table
            CreatedColumn = $parts[1]; UpdatedColumn = if ($parts.Count -eq 3) { $parts[2] } else { $null }
        })
    }
    return $specs.ToArray()
}

function ConvertTo-StockDateTimeOffset {
    # Pure.  ConvertFrom-Json turns ISO strings into DateTime on some
    # PowerShell versions and leaves them as strings on others.
    param($Value)
    if ($null -eq $Value -or ($Value -is [string] -and -not $Value)) { return $null }
    if ($Value -is [DateTimeOffset]) { return $Value }
    if ($Value -is [DateTime]) { return [DateTimeOffset]::new($Value.ToUniversalTime(), [TimeSpan]::Zero) }
    return [DateTimeOffset]::Parse([string]$Value, [Globalization.CultureInfo]::InvariantCulture,
        [Globalization.DateTimeStyles]::AssumeUniversal)
}

function Get-StockIncrementalWindows {
    # Pure.  Splits [start, Upper) into windows ending on local (exchange
    # timezone) midnights, so one chunk holds at most one local day.  Start is
    # the stored watermark or, on the first run, the local midnight at or
    # before the earliest row.  Returns nothing when there is nothing to export.
    [CmdletBinding()]
    param(
        $Lower,
        [Parameter(Mandatory)][DateTimeOffset]$Upper,
        $Earliest,
        [TimeSpan]$LocalOffset = [TimeSpan]::FromHours(8)
    )
    $lowerValue = ConvertTo-StockDateTimeOffset $Lower
    $earliestValue = ConvertTo-StockDateTimeOffset $Earliest
    if ($null -ne $lowerValue) {
        $start = $lowerValue
    } elseif ($null -ne $earliestValue) {
        $start = [DateTimeOffset]::new($earliestValue.ToOffset($LocalOffset).Date, $LocalOffset)
    } else {
        return @()
    }
    $windows = [Collections.Generic.List[object]]::new()
    while ($start -lt $Upper) {
        $nextMidnight = [DateTimeOffset]::new($start.ToOffset($LocalOffset).Date.AddDays(1), $LocalOffset)
        $end = if ($nextMidnight -lt $Upper) { $nextMidnight } else { $Upper }
        $windows.Add([pscustomobject]@{ Lower = $start.ToUniversalTime(); Upper = $end.ToUniversalTime() })
        $start = $end
    }
    return $windows.ToArray()
}

function Get-StockIncrementalChunkName {
    # Pure.  UTC bounds at PostgreSQL's microsecond precision; sortable and
    # filename-safe, so name order is restore order.
    [CmdletBinding()]
    param([Parameter(Mandatory)][DateTimeOffset]$Lower, [Parameter(Mandatory)][DateTimeOffset]$Upper)
    $format = 'yyyyMMdd\THHmmss\.ffffff\Z'
    return '{0}_{1}' -f $Lower.UtcDateTime.ToString($format), $Upper.UtcDateTime.ToString($format)
}

function ConvertTo-StockSqlTimestamp {
    # Pure.  An unambiguous timestamptz literal.
    param([Parameter(Mandatory)][DateTimeOffset]$Value)
    return "'" + $Value.UtcDateTime.ToString('yyyy-MM-dd\THH:mm:ss.ffffff') + "+00:00'::timestamptz"
}

function Get-StockIncrementalSelectSql {
    # Pure.  Rows created in the window, plus rows created before it and
    # modified in it.  Identifiers were validated by Get-StockIncrementalTableSpecs.
    param([Parameter(Mandatory)]$Spec, [Parameter(Mandatory)][string]$Columns,
          [Parameter(Mandatory)][DateTimeOffset]$Lower, [Parameter(Mandatory)][DateTimeOffset]$Upper)
    $lowerSql = ConvertTo-StockSqlTimestamp $Lower
    $upperSql = ConvertTo-StockSqlTimestamp $Upper
    $created = $Spec.CreatedColumn
    $where = "($created >= $lowerSql AND $created < $upperSql)"
    if ($Spec.UpdatedColumn) {
        $updated = $Spec.UpdatedColumn
        $where += " OR ($updated >= $lowerSql AND $updated < $upperSql AND $created < $lowerSql)"
    }
    return "SELECT $Columns FROM $($Spec.Table) WHERE $where"
}

function Get-StockUtcTextSql {
    # Pure.  Renders a timestamptz expression as ISO-8601 UTC text.
    param([Parameter(Mandatory)][string]$Expression)
    return "to_char(($Expression) AT TIME ZONE 'UTC', 'YYYY-MM-DD`"T`"HH24:MI:SS.US`"+00:00`"')"
}

function Start-StockPsqlProcess {
    param([Parameter(Mandatory)][hashtable]$Connection, [string]$Sql, [string[]]$ExtraArguments = @(),
          [string]$Options = '-c statement_timeout=0', [switch]$RedirectInput)
    # Windows passes command-line arguments to psql in the ANSI code page while
    # the session speaks UTF-8, so non-ASCII SQL text would reach the server
    # as invalid bytes.  Every statement built here is ASCII; fail loudly if not.
    if ($Sql -and $Sql -match '[^\x00-\x7F]') { throw 'psql -c text must be ASCII; send non-ASCII data through COPY' }
    $info = [Diagnostics.ProcessStartInfo]::new($Connection.Psql)
    $arguments = @('-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
        '-h', $Connection.Host, '-p', [string]$Connection.Port, '-U', $Connection.User, '-d', $Connection.Database) + $ExtraArguments
    if ($Sql) { $arguments += @('-c', $Sql) }
    foreach ($argument in $arguments) { $info.ArgumentList.Add($argument) }
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.RedirectStandardInput = [bool]$RedirectInput
    $info.Environment['PGPASSWORD'] = $Connection.Password
    $info.Environment['PGCLIENTENCODING'] = 'UTF8'
    $info.Environment['PGOPTIONS'] = $Options
    return [Diagnostics.Process]::Start($info)
}

function Invoke-StockPsqlScalar {
    param([Parameter(Mandatory)][hashtable]$Connection, [Parameter(Mandatory)][string]$Sql)
    $process = Start-StockPsqlProcess -Connection $Connection -Sql $Sql
    try {
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $stdout = $process.StandardOutput.ReadToEnd()
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "psql failed ($($process.ExitCode)): $($stderrTask.Result.Trim())" }
        return $stdout.Trim()
    } finally { $process.Dispose() }
}

function Read-StockJsonFile {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $null }
    return Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
}

function Write-StockJsonAtomically {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)]$Value)
    $temp = "$Path.tmp"
    [IO.File]::WriteAllText($temp, (ConvertTo-Json -InputObject $Value -Depth 6), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $Path -Force
}

function Get-StockTableColumns {
    param([Parameter(Mandatory)][hashtable]$Connection, [Parameter(Mandatory)]$Spec)
    $columns = Invoke-StockPsqlScalar -Connection $Connection -Sql (
        "SELECT string_agg(quote_ident(column_name), ',' ORDER BY ordinal_position) FROM information_schema.columns " +
        "WHERE table_schema='$($Spec.Schema)' AND table_name='$($Spec.Name)'")
    if (-not $columns) { throw "Incremental table $($Spec.Table) not found" }
    foreach ($required in @($Spec.CreatedColumn, $Spec.UpdatedColumn) | Where-Object { $_ }) {
        if ($required -notin $columns.Split(',')) { throw "Incremental table $($Spec.Table) has no column $required" }
    }
    return $columns
}

function Export-StockIncrementalWindow {
    # Streams COPY ... TO STDOUT straight into a gzip file (no uncompressed
    # temp file on the HDD), counts rows on the way, and compares the count
    # with the database before the chunk gets its final name.
    param([Parameter(Mandatory)][hashtable]$Connection, [Parameter(Mandatory)]$Spec,
          [Parameter(Mandatory)][string]$Columns, [Parameter(Mandatory)]$Window,
          [Parameter(Mandatory)][string]$Directory)
    $name = Get-StockIncrementalChunkName -Lower $Window.Lower -Upper $Window.Upper
    $chunkPath = Join-Path $Directory "$name.copy.gz"
    $tempPath = "$chunkPath.partial"
    $select = Get-StockIncrementalSelectSql -Spec $Spec -Columns $Columns -Lower $Window.Lower -Upper $Window.Upper

    $process = Start-StockPsqlProcess -Connection $Connection -Sql "COPY ($select) TO STDOUT"
    try {
        $stderrTask = $process.StandardError.ReadToEndAsync()
        $file = [IO.File]::Create($tempPath)
        try {
            $gzip = [IO.Compression.GZipStream]::new($file, [IO.Compression.CompressionLevel]::Fastest)
            try { $counts = [StockBackupStreams]::CopyCountingRows($process.StandardOutput.BaseStream, $gzip) }
            finally { $gzip.Dispose() }
        } finally { $file.Dispose() }
        $process.WaitForExit()
        if ($process.ExitCode -ne 0) { throw "COPY export of $($Spec.Table) failed ($($process.ExitCode)): $($stderrTask.Result.Trim())" }
        $rows = $counts[1]
        $expected = [int64](Invoke-StockPsqlScalar -Connection $Connection -Sql "SELECT count(*) FROM ($select) exported")
        if ($expected -ne $rows) {
            throw "Row count mismatch for $($Spec.Table) window ${name}: exported $rows, database has $expected"
        }
        Move-Item -LiteralPath $tempPath -Destination $chunkPath -Force
    } catch {
        Remove-Item -LiteralPath $tempPath -Force -ErrorAction SilentlyContinue
        throw
    } finally {
        $process.Dispose()
    }

    $manifest = [ordered]@{
        schema_version = 1
        table = $Spec.Table
        created_column = $Spec.CreatedColumn
        updated_column = $Spec.UpdatedColumn
        lower = $Window.Lower.UtcDateTime.ToString('yyyy-MM-dd\THH:mm:ss.ffffff\Z')
        upper = $Window.Upper.UtcDateTime.ToString('yyyy-MM-dd\THH:mm:ss.ffffff\Z')
        columns = $Columns
        rows = $rows
        uncompressed_bytes = $counts[0]
        chunk_file = Split-Path -Leaf $chunkPath
        sha256 = (Get-FileHash -LiteralPath $chunkPath -Algorithm SHA256).Hash.ToLowerInvariant()
        size_bytes = (Get-Item -LiteralPath $chunkPath).Length
        exported_at = (Get-Date).ToString('o')
    }
    Write-StockJsonAtomically -Path (Join-Path $Directory "$name.json") -Value $manifest
    return [pscustomobject]$manifest
}

function Invoke-StockIncrementalBackup {
    # Exports every new window of one table, advancing the watermark after
    # each verified chunk so an interrupted run resumes where it stopped.
    # SafetyLag keeps the upper bound behind now(): a row stamped by a
    # transaction that is still open must not land behind the watermark.
    param([Parameter(Mandatory)][hashtable]$Connection, [Parameter(Mandatory)]$Spec,
          [Parameter(Mandatory)][string]$BackupRoot,
          [TimeSpan]$SafetyLag = [TimeSpan]::FromMinutes(30))
    $directory = Join-Path (Join-Path $BackupRoot 'incremental') $Spec.Table
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    $statePath = Join-Path $directory 'state.json'
    $state = Read-StockJsonFile -Path $statePath
    $columns = Get-StockTableColumns -Connection $Connection -Spec $Spec
    $lagSeconds = [int]$SafetyLag.TotalSeconds
    $upper = ConvertTo-StockDateTimeOffset (Invoke-StockPsqlScalar -Connection $Connection -Sql (
        'SELECT ' + (Get-StockUtcTextSql "now() - make_interval(secs => $lagSeconds)")))

    $lower = if ($state) { $state.watermark } else { $null }
    $earliest = $null
    if ($null -eq $lower) {
        $earliest = Invoke-StockPsqlScalar -Connection $Connection -Sql (
            'SELECT ' + (Get-StockUtcTextSql "min($($Spec.CreatedColumn))") + " FROM $($Spec.Table)")
    }
    $windows = @(Get-StockIncrementalWindows -Lower $lower -Upper $upper -Earliest $earliest)
    $chunks = [Collections.Generic.List[object]]::new()
    $watermark = if ($null -ne $lower) { (ConvertTo-StockDateTimeOffset $lower).UtcDateTime.ToString('yyyy-MM-dd\THH:mm:ss.ffffff\Z') } else { $null }
    foreach ($window in $windows) {
        $chunks.Add((Export-StockIncrementalWindow -Connection $Connection -Spec $Spec -Columns $columns -Window $window -Directory $directory))
        $watermark = $window.Upper.UtcDateTime.ToString('yyyy-MM-dd\THH:mm:ss.ffffff\Z')
        Write-StockJsonAtomically -Path $statePath -Value ([ordered]@{
            table = $Spec.Table; watermark = $watermark; updated_at = (Get-Date).ToString('o')
        })
    }
    if ($null -eq $watermark) {
        # Empty table: start the chain at the upper bound so a later first
        # row is never mistaken for history that predates the chain.
        $watermark = $upper.UtcDateTime.ToString('yyyy-MM-dd\THH:mm:ss.ffffff\Z')
        Write-StockJsonAtomically -Path $statePath -Value ([ordered]@{
            table = $Spec.Table; watermark = $watermark; updated_at = (Get-Date).ToString('o')
        })
    }
    return [pscustomobject]@{
        table = $Spec.Table
        chunks = $chunks.Count
        rows = [int64](($chunks | Measure-Object -Property rows -Sum).Sum)
        size_bytes = [int64](($chunks | Measure-Object -Property size_bytes -Sum).Sum)
        watermark = $watermark
    }
}

function Import-StockIncrementalChunks {
    # Restores every chunk of one table in order.  Each chunk's SHA-256 is
    # checked before loading; rows go through a staging table and are upserted
    # on the primary key, so a later chunk's version of a row wins, and the
    # upsert's row count must equal the manifest.  Foreign-key triggers are
    # disabled while loading (a parent row may have been removed after the
    # chunk was written); the constraints are reported for revalidation.
    param([Parameter(Mandatory)][hashtable]$Connection, [Parameter(Mandatory)]$Spec,
          [Parameter(Mandatory)][string]$BackupRoot)
    $directory = Join-Path (Join-Path $BackupRoot 'incremental') $Spec.Table
    $manifests = @(Get-ChildItem -LiteralPath $directory -Filter '*.json' -File |
        Where-Object Name -ne 'state.json' | Sort-Object Name |
        ForEach-Object { Read-StockJsonFile -Path $_.FullName })
    $primaryKey = Invoke-StockPsqlScalar -Connection $Connection -Sql (
        "SELECT string_agg(quote_ident(a.attname), ',' ORDER BY k.ord) FROM pg_index i " +
        "CROSS JOIN LATERAL unnest(i.indkey) WITH ORDINALITY k(attnum, ord) " +
        "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=k.attnum " +
        "WHERE i.indrelid='$($Spec.Table)'::regclass AND i.indisprimary")
    if (-not $primaryKey) { throw "Incremental restore needs a primary key on $($Spec.Table)" }
    $keyColumns = $primaryKey.Split(',')

    $previousUpper = $null
    $loaded = [int64]0
    foreach ($manifest in $manifests) {
        $lower = ConvertTo-StockDateTimeOffset $manifest.lower
        $upper = ConvertTo-StockDateTimeOffset $manifest.upper
        if ($null -ne $previousUpper -and $lower -ne $previousUpper) {
            throw "Gap or overlap in $($Spec.Table) chunks before $($manifest.chunk_file)"
        }
        $chunkPath = Join-Path $directory $manifest.chunk_file
        if ((Get-FileHash -LiteralPath $chunkPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $manifest.sha256) {
            throw "SHA-256 mismatch for $chunkPath"
        }
        $columns = [string]$manifest.columns
        $assignments = ($columns.Split(',') | Where-Object { $_ -notin $keyColumns } | ForEach-Object { "$_=EXCLUDED.$_" }) -join ','
        $conflict = if ($assignments) { "DO UPDATE SET $assignments" } else { 'DO NOTHING' }
        $header = "BEGIN;`nCREATE TEMP TABLE stock_restore_stage ON COMMIT DROP AS SELECT $columns FROM $($Spec.Table) WITH NO DATA;`n" +
            "COPY stock_restore_stage ($columns) FROM STDIN;`n"
        $footer = "\.`nINSERT INTO $($Spec.Table) ($columns) SELECT $columns FROM stock_restore_stage ON CONFLICT ($primaryKey) $conflict;`nCOMMIT;`n"

        $process = Start-StockPsqlProcess -Connection $Connection -ExtraArguments @('-f', '-') `
            -Options '-c statement_timeout=0 -c session_replication_role=replica' -RedirectInput
        try {
            $stderrTask = $process.StandardError.ReadToEndAsync()
            $stdoutTask = $process.StandardOutput.ReadToEndAsync()
            $stdin = $process.StandardInput.BaseStream
            $utf8 = [Text.UTF8Encoding]::new($false)
            $bytes = $utf8.GetBytes($header); $stdin.Write($bytes, 0, $bytes.Length)
            $file = [IO.File]::OpenRead($chunkPath)
            try {
                $gzip = [IO.Compression.GZipStream]::new($file, [IO.Compression.CompressionMode]::Decompress)
                try { [void][StockBackupStreams]::CopyCountingRows($gzip, $stdin) } finally { $gzip.Dispose() }
            } finally { $file.Dispose() }
            $bytes = $utf8.GetBytes($footer); $stdin.Write($bytes, 0, $bytes.Length)
            $process.StandardInput.Close()
            $process.WaitForExit()
            if ($process.ExitCode -ne 0) { throw "Restore of $chunkPath failed ($($process.ExitCode)): $($stderrTask.Result.Trim())" }
            $inserted = [regex]::Match($stdoutTask.Result, '(?m)^INSERT 0 (\d+)\s*$')
            if (-not $inserted.Success -or [int64]$inserted.Groups[1].Value -ne [int64]$manifest.rows) {
                throw "Restored row count mismatch for $($manifest.chunk_file): manifest $($manifest.rows), psql reported '$($stdoutTask.Result.Trim())'"
            }
        } finally { $process.Dispose() }
        $loaded += [int64]$manifest.rows
        $previousUpper = $upper
    }
    $foreignKeys = Invoke-StockPsqlScalar -Connection $Connection -Sql (
        "SELECT coalesce(string_agg(conname, ',' ORDER BY conname), '') FROM pg_constraint " +
        "WHERE contype='f' AND conrelid='$($Spec.Table)'::regclass")
    return [pscustomobject]@{
        table = $Spec.Table; chunks = $manifests.Count; rows_applied = $loaded
        foreign_keys_to_revalidate = $foreignKeys
    }
}

Export-ModuleMember -Function Get-StockIncrementalTableSpecs, ConvertTo-StockDateTimeOffset, Get-StockIncrementalWindows,
    Get-StockIncrementalChunkName, ConvertTo-StockSqlTimestamp, Get-StockIncrementalSelectSql,
    Invoke-StockIncrementalBackup, Import-StockIncrementalChunks, Invoke-StockPsqlScalar
