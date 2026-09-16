[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

# backup-stock-database.ps1's decision functions are pure (no filesystem or
# database I/O), so they are extracted from the script's own AST and
# defined in this process rather than duplicated or requiring a real
# PostgreSQL instance to exercise.
$scriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'backup-stock-database.ps1'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw "Missing $scriptPath" }
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$null, [ref]$parseErrors)
if ($parseErrors -and $parseErrors.Count -gt 0) { throw "Failed to parse $scriptPath" }
foreach ($name in 'Select-StockBackupRetentionRemovals', 'ConvertTo-Bytes') {
    $functionAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if (-not $functionAst) { throw "$name function not found in $scriptPath" }
    . ([scriptblock]::Create($functionAst.Extent.Text))
}

# --- ConvertTo-Bytes ---
Assert-True ((ConvertTo-Bytes -Value '' -Default 123) -eq 123) 'ConvertTo-Bytes must fall back to the default when no value is given'
Assert-True ((ConvertTo-Bytes -Value '5GB' -Default 0) -eq 5GB) 'ConvertTo-Bytes must parse a GB-suffixed value'
Assert-True ((ConvertTo-Bytes -Value '512' -Default 0) -eq 512) 'ConvertTo-Bytes must parse a bare byte count'

# --- Select-StockBackupRetentionRemovals ---
# Fixed "now" so week-boundary math is deterministic regardless of when the
# test runs. 2026-09-02 is a Wednesday (ISO week 36).
$now = [DateTime]::ParseExact('2026-09-02', 'yyyy-MM-dd', $null)

# All of the last 14 days must be kept.
$recentDays = 0..13 | ForEach-Object { $now.AddDays(-$_).ToString('yyyy-MM-dd') }
$removed = @(Select-StockBackupRetentionRemovals -DayNames $recentDays -Now $now -DailyRetentionDays 14 -WeeklyRetentionWeeks 8)
Assert-True ($removed.Count -eq 0) 'every backup within the daily retention window must be kept'

# A backup older than the daily window but within a kept ISO week must survive as that week's earliest entry.
$oldButWithinWeek = @($now.AddDays(-20).ToString('yyyy-MM-dd'))
$removed = @(Select-StockBackupRetentionRemovals -DayNames $oldButWithinWeek -Now $now -DailyRetentionDays 14 -WeeklyRetentionWeeks 8)
Assert-True ($removed.Count -eq 0) 'the earliest backup of a kept ISO week must be retained even outside the daily window'

# A backup far older than both the daily window and the weekly window must be pruned.
$veryOld = @($now.AddDays(-400).ToString('yyyy-MM-dd'))
$removed = @(Select-StockBackupRetentionRemovals -DayNames $veryOld -Now $now -DailyRetentionDays 14 -WeeklyRetentionWeeks 8)
Assert-True ($removed.Count -eq 1 -and $removed[0] -eq $veryOld[0]) 'a backup outside both the daily and weekly retention windows must be pruned'

# Within one ISO week that has two backups outside the daily window, only the earliest must survive.
$sameWeekPair = @($now.AddDays(-21).ToString('yyyy-MM-dd'), $now.AddDays(-20).ToString('yyyy-MM-dd'))
$removed = @(Select-StockBackupRetentionRemovals -DayNames $sameWeekPair -Now $now -DailyRetentionDays 14 -WeeklyRetentionWeeks 8)
Assert-True ($removed.Count -eq 1 -and $removed[0] -eq $sameWeekPair[1]) 'only the earliest backup in a kept ISO week must be retained, later same-week duplicates pruned'

# --- incremental export decisions (stock-incremental-backup.psm1) ---
Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'stock-incremental-backup.psm1') -Force

$defaultSpecs = @(Get-StockIncrementalTableSpecs -Value '')
Assert-True ($defaultSpecs.Count -eq 1 -and $defaultSpecs[0].Table -eq 'quant.raw_market_observations' -and
    $defaultSpecs[0].CreatedColumn -eq 'created_at' -and $defaultSpecs[0].UpdatedColumn -eq 'updated_at') 'the default incremental spec covers raw_market_observations with update tracking'
Assert-True (@(Get-StockIncrementalTableSpecs -Value 'none').Count -eq 0) '"none" disables incremental export'
$twoSpecs = @(Get-StockIncrementalTableSpecs -Value 'quant.a:created_at; quant.b:created_at:changed_at')
Assert-True ($twoSpecs.Count -eq 2 -and $null -eq $twoSpecs[0].UpdatedColumn -and $twoSpecs[1].UpdatedColumn -eq 'changed_at') 'explicit specs parse with and without an update column'
foreach ($bad in 'quant.a', 'a:created_at', 'quant.a;drop:created_at', 'quant.A:created_at', 'quant.a:created_at:x:y', 'quant.a:c;quant.a:c') {
    $rejected = $false
    try { [void](Get-StockIncrementalTableSpecs -Value $bad) } catch { $rejected = $true }
    Assert-True $rejected "invalid incremental spec '$bad' must be rejected"
}

$utc = [TimeSpan]::Zero
# First run: start at the Shanghai midnight before the earliest row, one window per local day, last window ends at upper.
$earliest = [DateTimeOffset]::new(2026, 9, 14, 3, 0, 0, $utc)          # 11:00 on 2026-09-14 in Shanghai
$upper = [DateTimeOffset]::new(2026, 9, 16, 12, 0, 0, $utc)            # 20:00 on 2026-09-16 in Shanghai
$windows = @(Get-StockIncrementalWindows -Lower $null -Upper $upper -Earliest $earliest)
Assert-True ($windows.Count -eq 3) 'a first run splits history into one window per local day'
Assert-True ($windows[0].Lower -eq [DateTimeOffset]::new(2026, 9, 13, 16, 0, 0, $utc)) 'the first window starts at the local midnight before the earliest row'
Assert-True ($windows[0].Upper -eq $windows[1].Lower -and $windows[1].Upper -eq $windows[2].Lower) 'windows are contiguous'
Assert-True ($windows[1].Upper -eq [DateTimeOffset]::new(2026, 9, 15, 16, 0, 0, $utc)) 'windows end on local midnights'
Assert-True ($windows[2].Upper -eq $upper) 'the last window ends at the upper bound'
# Later run: resume exactly at the watermark, whichever type ConvertFrom-Json produced for it.
$resumed = @(Get-StockIncrementalWindows -Lower '2026-09-16T12:00:00.000000Z' -Upper $upper.AddDays(1) -Earliest $null)
Assert-True ($resumed.Count -eq 2 -and $resumed[0].Lower -eq $upper) 'a later run resumes at the string watermark'
$resumedFromDate = @(Get-StockIncrementalWindows -Lower ([DateTime]::SpecifyKind([DateTime]'2026-09-16T12:00:00', 'Utc')) -Upper $upper.AddDays(1) -Earliest $null)
Assert-True ($resumedFromDate.Count -eq 2 -and $resumedFromDate[0].Lower -eq $upper) 'a later run resumes at a DateTime watermark'
Assert-True (@(Get-StockIncrementalWindows -Lower $upper -Upper $upper -Earliest $null).Count -eq 0) 'nothing new means no window'
Assert-True (@(Get-StockIncrementalWindows -Lower $null -Upper $upper -Earliest '').Count -eq 0) 'an empty table without a watermark means no window'
$microLower = [DateTimeOffset]::new(2026, 9, 16, 12, 0, 0, $utc).AddTicks(1234560)
Assert-True ((Get-StockIncrementalChunkName -Lower $microLower -Upper $upper.AddDays(1)) -eq '20260916T120000.123456Z_20260917T120000.000000Z') 'chunk names carry sortable UTC microsecond bounds'

$sql = Get-StockIncrementalSelectSql -Spec $twoSpecs[1] -Columns 'a,b' -Lower $upper -Upper $upper.AddDays(1)
Assert-True ($sql -eq "SELECT a,b FROM quant.b WHERE (created_at >= '2026-09-16T12:00:00.000000+00:00'::timestamptz AND created_at < '2026-09-17T12:00:00.000000+00:00'::timestamptz) OR (changed_at >= '2026-09-16T12:00:00.000000+00:00'::timestamptz AND changed_at < '2026-09-17T12:00:00.000000+00:00'::timestamptz AND created_at < '2026-09-16T12:00:00.000000+00:00'::timestamptz)") 'the window selects created rows plus older rows modified in it'
$appendOnlySql = Get-StockIncrementalSelectSql -Spec $twoSpecs[0] -Columns 'a' -Lower $upper -Upper $upper.AddDays(1)
Assert-True ($appendOnlySql -notmatch ' OR ') 'a spec without an update column selects created rows only'

[pscustomobject]@{
    passed = $true
    incremental_specs_validated = $true
    incremental_windows_contiguous_by_local_day = $true
    incremental_select_includes_modified_rows = $true
    convert_to_bytes_ok = $true
    retention_keeps_daily_window = $true
    retention_keeps_one_per_weekly_window = $true
    retention_prunes_beyond_both_windows = $true
}
