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
foreach ($name in 'Select-StockBackupRetentionRemovals', 'ConvertTo-Bytes', 'Get-StockBackupExcludedTableData', 'Resolve-StockBackupExcludedTableData') {
    $functionAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if (-not $functionAst) { throw "$name function not found in $scriptPath" }
    . ([scriptblock]::Create($functionAst.Extent.Text))
}

# --- ConvertTo-Bytes ---
Assert-True ((ConvertTo-Bytes -Value '' -Default 123) -eq 123) 'ConvertTo-Bytes must fall back to the default when no value is given'
Assert-True ((ConvertTo-Bytes -Value '5GB' -Default 0) -eq 5GB) 'ConvertTo-Bytes must parse a GB-suffixed value'
Assert-True ((ConvertTo-Bytes -Value '512' -Default 0) -eq 512) 'ConvertTo-Bytes must parse a bare byte count'

# --- Get-StockBackupExcludedTableData ---
# STOCK_BACKUP_EXCLUDE_TABLE_DATA names the tables whose data the nightly dump
# skips (production: the cold-tier twins).  An unset value must exclude nothing,
# so a deployment that has not been migrated yet keeps dumping everything.
Assert-True (@(Get-StockBackupExcludedTableData -Value $null).Count -eq 0) 'an unset exclusion list excludes nothing'
Assert-True (@(Get-StockBackupExcludedTableData -Value '   ').Count -eq 0) 'a blank exclusion list excludes nothing'
$excluded = @(Get-StockBackupExcludedTableData -Value 'quant.raw_market_observations_cold; quant.edge_evidence_changes_cold ;')
Assert-True ($excluded.Count -eq 2 -and $excluded[0] -eq 'quant.raw_market_observations_cold' -and
    $excluded[1] -eq 'quant.edge_evidence_changes_cold') 'a semicolon list parses, trims and ignores empty entries'
$deduplicated = @(Get-StockBackupExcludedTableData -Value 'quant.a_cold;quant.a_cold;quant.b_cold')
Assert-True ($deduplicated.Count -eq 2 -and $deduplicated[0] -eq 'quant.a_cold' -and $deduplicated[1] -eq 'quant.b_cold') 'a repeated table is collapsed, keeping the first position'
foreach ($bad in 'raw_market_observations_cold', 'quant.a.b', 'quant.A_cold', 'Quant.a_cold', 'quant.', '.a_cold', 'quant.a-cold', 'quant.a_cold;bad') {
    $rejected = $false
    try { [void](Get-StockBackupExcludedTableData -Value $bad) } catch { $rejected = $true }
    Assert-True $rejected "invalid excluded table '$bad' must be rejected"
}

# --- Resolve-StockBackupExcludedTableData -----------------------------------
# The rule that decides what may be left out of the nightly dump. A cold twin's
# rows are only recoverable through the incremental chunk chain of the HOT table
# they were moved out of, so a twin whose hot table has no chain must never be
# excluded: the last dump that still carried it falls out of retention about two
# months later and the live cold tablespace on G: becomes its only copy.
$none = Resolve-StockBackupExcludedTableData -IncrementalTables @() -IncrementalSucceeded $true -ConfiguredExclusions @()
Assert-True (@($none.Excluded).Count -eq 0 -and @($none.Refused).Count -eq 0) 'without an incremental chain nothing is excluded'

$one = Resolve-StockBackupExcludedTableData -IncrementalTables @('quant.raw_market_observations') -IncrementalSucceeded $true -ConfiguredExclusions @()
Assert-True (@($one.Excluded) -contains 'quant.raw_market_observations') 'a table in the chain is excluded from the dump'
Assert-True (@($one.Excluded) -contains 'quant.raw_market_observations_cold') "the twin of a table in the chain is excluded too"
Assert-True (@($one.Excluded).Count -eq 2) 'nothing else is excluded'

# A failed export degrades to a full dump for the hot table, but the twin's rows
# were captured by EARLIER runs of the chain, so the twin stays excluded.
$failed = Resolve-StockBackupExcludedTableData -IncrementalTables @('quant.raw_market_observations') -IncrementalSucceeded $false -ConfiguredExclusions @()
Assert-True (@($failed.Excluded) -notcontains 'quant.raw_market_observations') "a failed incremental export must dump the hot table's data"
Assert-True (@($failed.Excluded).Count -eq 1 -and @($failed.Excluded)[0] -eq 'quant.raw_market_observations_cold') 'the twin stays excluded when tonight''s export failed'

# The operator override: honoured, except for a twin with no chain behind it.
# This is the shipped-and-reviewed blocker: five twins were seeded statically
# while only raw_market_observations had a chain.
$policyTwins = @('quant.raw_market_observations_cold', 'quant.tushare_raw_records_cold', 'quant.intraday_quote_observations_cold',
    'quant.intraday_rule_input_snapshots_cold', 'quant.edge_evidence_changes_cold')
$override = Resolve-StockBackupExcludedTableData -IncrementalTables @('quant.raw_market_observations') -IncrementalSucceeded $true -ConfiguredExclusions $policyTwins
Assert-True (@($override.Refused).Count -eq 4) "a twin without a chunk chain must be refused (got $(@($override.Refused) -join ','))"
Assert-True (@($override.Refused) -notcontains 'quant.raw_market_observations_cold') 'the twin that does have a chain is not refused'
foreach ($twin in $policyTwins | Where-Object { $_ -ne 'quant.raw_market_observations_cold' }) {
    Assert-True (@($override.Excluded) -notcontains $twin) "$twin has no chunk chain and must stay in the dump"
    Assert-True (@($override.Refused) -contains $twin) "$twin must be reported as a refused exclusion"
}
Assert-True (@($override.Excluded).Count -eq 2) 'the override adds nothing beyond the chain and its twin'

# All five in the chain: all five twins may be excluded, and the list is the
# same one the retired static default used to carry.
$allFive = @('quant.raw_market_observations', 'quant.tushare_raw_records', 'quant.intraday_quote_observations',
    'quant.intraday_rule_input_snapshots', 'quant.edge_evidence_changes')
$full = Resolve-StockBackupExcludedTableData -IncrementalTables $allFive -IncrementalSucceeded $true -ConfiguredExclusions @()
Assert-True (((@($full.Excluded) | Where-Object { $_.EndsWith('_cold') } | Sort-Object) -join ';') -eq (($policyTwins | Sort-Object) -join ';')) `
    'with every tiered table in the chain the rule yields exactly the five cold twins'
Assert-True (@($full.Refused).Count -eq 0) 'nothing is refused when every twin has a chain'

# A non-twin override is the operator's own call and passes through; a repeat of
# something the rule already produced is collapsed.
$other = Resolve-StockBackupExcludedTableData -IncrementalTables @('quant.raw_market_observations') -IncrementalSucceeded $true `
    -ConfiguredExclusions @('quant.scratch_table', 'quant.raw_market_observations_cold')
Assert-True (@($other.Excluded) -contains 'quant.scratch_table') 'a non-twin override is honoured'
Assert-True (@($other.Excluded | Where-Object { $_ -eq 'quant.raw_market_observations_cold' }).Count -eq 1) 'an override that repeats the computed exclusion is collapsed'

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
    excluded_table_data_parsed = $true
    cold_twin_exclusion_requires_chunk_chain = $true
    cold_twin_exclusion_without_chain_refused = $true
    retention_keeps_daily_window = $true
    retention_keeps_one_per_weekly_window = $true
    retention_prunes_beyond_both_windows = $true
}
