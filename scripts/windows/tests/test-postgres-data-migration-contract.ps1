[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

# The data-directory migration is a one-shot, irreversible-looking operation on
# the authoritative database, so its safety sequence is asserted statically
# instead of by running it: the script is parsed, its pure guard is executed,
# and the order of the dangerous operations in its body is checked.
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$scriptPath = Join-Path $root 'scripts\windows\migrate-postgres-data-directory.ps1'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw "Missing $scriptPath" }
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$null, [ref]$parseErrors)
if ($parseErrors -and $parseErrors.Count -gt 0) {
    throw "Failed to parse $scriptPath : $(($parseErrors | ForEach-Object { $_.Message }) -join '; ')"
}
$source = [IO.File]::ReadAllText($scriptPath, [Text.Encoding]::UTF8)

# --- the declared switches --------------------------------------------------
$parameters = $ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }
foreach ($name in 'WhatIf', 'Rollback', 'Force', 'TargetDataDir', 'KeepOldName') {
    Assert-True ($parameters -contains $name) "-$name must be a declared parameter"
}

# --- the trading-session guard is pure and is exercised here ----------------
$guardAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-TradingSession' }, $true)
Assert-True ($null -ne $guardAst) 'Test-TradingSession must exist'
. ([scriptblock]::Create($guardAst.Extent.Text))
function At([string]$Date, [string]$Time) { return [DateTime]::ParseExact("$Date $Time", 'yyyy-MM-dd HH:mm', $null) }
$wednesday = '2026-09-16'
$saturday = '2026-09-19'
Assert-True (Test-TradingSession -Now (At $wednesday '09:00')) 'the session guard must trigger at the open'
Assert-True (Test-TradingSession -Now (At $wednesday '11:15')) 'the session guard must trigger mid-morning'
Assert-True (Test-TradingSession -Now (At $wednesday '15:29')) 'the session guard must trigger until the close'
Assert-True (-not (Test-TradingSession -Now (At $wednesday '15:30'))) 'the session guard ends at 15:30'
Assert-True (-not (Test-TradingSession -Now (At $wednesday '08:59'))) 'the session guard does not cover the pre-open'
Assert-True (-not (Test-TradingSession -Now (At $saturday '11:15'))) 'there is no session at the weekend'

# The refusal itself must be wired to that guard and must require -Force.
Assert-True ($source -match '(?s)if \(Test-TradingSession[^}]*if \(-not \$Force\)[^}]*throw') `
    'a migration inside the exchange session must throw unless -Force is passed'

# --- ordering of the dangerous operations -----------------------------------
function Get-Position([string]$Pattern, [string]$What) {
    $match = [regex]::Match($source, $Pattern)
    Assert-True ($match.Success) "expected to find $What in the migration script"
    return $match.Index
}

# The functions are defined above the body they are called from, so ordering is
# asserted on the CALLS in the migration flow, not on the definitions.
$body = $source.Substring((Get-Position 'step 1/8 preflight' 'the preflight step marker'))
function Get-BodyPosition([string]$Pattern, [string]$What) {
    $match = [regex]::Match($body, $Pattern)
    Assert-True ($match.Success) "expected to find $What in the migration flow"
    return $match.Index
}

$stopRuntimes = Get-BodyPosition 'Stop-PlatformRuntimes' 'the runtime/watcher stop call'
$stopPostgres = Get-BodyPosition 'Stop-PostgresCluster -DataDirectory' 'the PostgreSQL stop call'
$robocopy = Get-BodyPosition 'robocopy\.exe \$currentDataDir \$target' 'the robocopy call'
$copyVerified = Get-BodyPosition 'Copy verification failed' 'the copy verification'
$envSwitch = Get-BodyPosition "Set-StockPlatformEnvValue -Path \`$RuntimeEnv -Name 'PGDATA_DIR' -Value \`$target" 'the PGDATA_DIR switch'
$startPostgres = Get-BodyPosition 'Start-PostgresCluster -DataDirectory \$target' 'the start from the new directory'
$rename = Get-BodyPosition 'Rename-Item -LiteralPath \$currentDataDir' 'the rename of the old data directory'

Assert-True ($stopRuntimes -lt $stopPostgres) 'the dashboard watcher task must be stopped before PostgreSQL, or it restarts the server mid-migration'
Assert-True ($stopPostgres -lt $robocopy) 'the cluster must be stopped before it is copied'
Assert-True ($robocopy -lt $copyVerified) 'the copy must be verified after it is made'
Assert-True ($copyVerified -lt $envSwitch) 'the copy must be verified before anything points PGDATA_DIR at it'
Assert-True ($envSwitch -lt $startPostgres) 'the configuration must be switched before the new cluster starts'
Assert-True ($startPostgres -lt $rename) 'the old directory is only renamed once the new one has started and verified'

# The watcher task is the specific hazard: it must be named explicitly.
Assert-True ($source -match 'trading-hareness-dashboard-runtime') 'the dashboard watcher task must be handled by name'
Assert-True ($source -match 'Stop-ScheduledTask -TaskName \$task') 'the watcher tasks must actually be stopped, not only disabled'

# The migration may only run outside the exchange session, which is the same
# 04:00-08:00 maintenance window the nightly database jobs were moved into.
# Each of them would run against a stopped or half-copied cluster.
foreach ($job in 'trading-hareness-post-close-pipeline', 'trading-hareness-storage-tiers', 'trading-hareness-stock-backup', 'trading-hareness-stock-backup-offsite') {
    Assert-True ($source -match [regex]::Escape($job)) "the maintenance-window job $job must be stopped for the migration"
}
Assert-True ($source -match "State -ne 'Disabled'") 'a task that was already disabled must not be recorded for re-enabling'
Assert-True ($source -match '\$script:DisabledTasks') 'step 8 must re-enable only the tasks this run disabled'

# --- the old data directory is never removed --------------------------------
foreach ($destructive in 'Remove-Item[^\r\n]*\$currentDataDir', 'rm -r', 'Remove-Item[^\r\n]*-Recurse[^\r\n]*data') {
    Assert-True ($source -notmatch $destructive) "the migration must never delete the old data directory ($destructive)"
}
Assert-True ($source -match 'Rename-Item -LiteralPath \$currentDataDir -NewName \$KeepOldName') 'the old directory must be renamed to the kept name'
Assert-True ($source -match 'kept for rollback') 'the rename must say why the old directory survives'

# --- the copy is verified on three independent signals ----------------------
Assert-True ($source -match '/COPY:DAT') 'robocopy must preserve data, attributes and timestamps'
Assert-True ($source -match '\$robocopyExit -ge 8') 'robocopy exit codes 8 and above are failures'
Assert-True ($source -match 'pg_control') 'the copy must be checked against the cluster control file checksum'
Assert-True ($source -match "Refusing to switch PGDATA_DIR before the copy is verified") 'the switch must fail closed when verification did not run'

# --- startup verification and the row-count smoke check ---------------------
foreach ($check in 'SHOW data_directory', 'SHOW work_mem', 'SHOW shared_preload_libraries') {
    Assert-True ($source -match [regex]::Escape($check)) "the restarted server must be verified with `"$check`""
}
foreach ($table in 'quant.instruments', 'quant.canonical_bars_daily', 'quant.raw_market_observations') {
    Assert-True ($source -match [regex]::Escape($table)) "the row-count smoke check must cover $table"
}
Assert-True ($source -match 'Row count for \$table changed across the migration') 'a row-count difference must abort the migration'

# --- -WhatIf must not write anything ----------------------------------------
foreach ($guarded in @(
    'if \(-not \$WhatIf\) \{ Set-StockPlatformEnvValue -Path \$RuntimeEnv',
    'if \(-not \$WhatIf\) \{\s*\r?\n?\s*Rename-Item',
    'if \(-not \$WhatIf\) \{\s*\r?\n?\s*New-Item -ItemType Directory -Force -Path \$logs'
)) {
    Assert-True ($source -match $guarded) "every mutation must be guarded by -WhatIf ($guarded)"
}
Assert-True ($source -match 'if \(\$WhatIf\) \{ return \}') 'the cluster start must be a no-op under -WhatIf'

# --- credentials never reach the console, the command line or the receipt ---
Assert-True ($source -notmatch 'Write-Host[^\r\n]*PGADMINPASSWORD') 'the admin password must never be printed'
Assert-True ($source -notmatch '--password') 'the password must never be passed on a command line'
Assert-True ($source -match '\$env:PGPASSWORD = \$script:adminPassword') 'psql must take the password from the process environment'
Assert-True ($source -match "Remove-Item Env:PGPASSWORD") 'the password must be cleared from the environment after use'
# A second -c makes psql print that statement's command tag ahead of the value,
# which silently broke every SHOW/count comparison against the live server.
Assert-True ($source -match '\$env:PGOPTIONS = "-c statement_timeout=') 'the statement timeout must travel in PGOPTIONS'
Assert-True ($source -notmatch '-c "SET statement_timeout') 'the statement timeout must not be a second -c command'
Assert-True ($source -match 'Remove-Item Env:PGOPTIONS') 'PGOPTIONS must be cleared after use'

# --- rollback ---------------------------------------------------------------
Assert-True ($source -match '(?s)if \(\$Rollback\)') 'the -Rollback switch must have its own flow'
Assert-True ($source -match 'Rollback target is not a PostgreSQL data directory') 'rollback must refuse a target that is not a cluster'
Assert-True ($source -match 'rollback_command') 'the receipt must state how to roll the migration back'

[pscustomobject]@{
    passed = $true
    scope = 'Static contract for migrate-postgres-data-directory.ps1 plus its pure trading-session guard; nothing was stopped, copied or migrated'
    ordering_verified = 'watcher stop -> pg_ctl stop -> robocopy -> copy verification -> PGDATA_DIR switch -> start+verify -> rename old'
}
