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

[pscustomobject]@{
    passed = $true
    convert_to_bytes_ok = $true
    retention_keeps_daily_window = $true
    retention_keeps_one_per_weekly_window = $true
    retention_prunes_beyond_both_windows = $true
}
