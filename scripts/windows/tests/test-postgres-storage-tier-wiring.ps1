[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$windows = Join-Path $root 'scripts\windows'
Import-Module (Join-Path $windows 'postgres-managed-config.psm1') -Force

# --- the generated PostgreSQL settings --------------------------------------
$settings = Get-StockPlatformManagedSettings -Port 55432 -LogDirectory 'G:\StockPlatform\logs'
$text = $settings -join "`n"
foreach ($expected in @(
    "port = 55432",
    "max_connections = 50",
    "shared_buffers = '4GB'",
    "work_mem = '64MB'",
    "temp_file_limit = '20GB'",
    'random_page_cost = 1.1',
    'checkpoint_completion_target = 0.9',
    "shared_preload_libraries = 'pg_stat_statements'",
    'log_lock_waits = on',
    'log_temp_files = 10240',
    'track_io_timing = on',
    'effective_io_concurrency = 0'
)) {
    Assert-True ($settings -contains $expected) "the managed configuration must set `"$expected`""
}
# Windows PostgreSQL has no posix_fadvise(): a non-zero value stops the server.
Assert-True ($text -notmatch 'effective_io_concurrency = [1-9]') 'effective_io_concurrency must stay 0 on Windows'
# One assignment per setting, or the last silently wins in the running server.
$assigned = $settings | Where-Object { $_ -match '^\s*([a-z_]+)\s*=' } | ForEach-Object { ($_ -split '=', 2)[0].Trim() }
Assert-True ($assigned.Count -eq ($assigned | Sort-Object -Unique).Count) 'no setting may be assigned twice in the managed configuration'
Assert-True ((Get-StockPlatformManagedSettings -Port 55432 -LogDirectory 'G:\StockPlatform\logs') -join "`n" -eq $text) 'the generated configuration must be deterministic'
Assert-True ($text -match "log_directory = 'G:/StockPlatform/logs'") 'the log directory must be written with forward slashes'
# The old HDD-era comment claimed the database lives on a spinning disk.
$moduleSource = [IO.File]::ReadAllText((Join-Path $windows 'postgres-managed-config.psm1'), [Text.Encoding]::UTF8)
Assert-True ($moduleSource -notmatch 'lives on an HDD') 'the HDD-era comment must be replaced by the tiered-layout description'
Assert-True ($moduleSource -match 'NVMe hot tier') 'the settings block must describe the tiered layout'

# --- PGDATA_DIR resolution ---------------------------------------------------
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('tier-wiring-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null
try {
    $envPath = Join-Path $sandbox 'runtime.env'
    [IO.File]::WriteAllLines($envPath, @('PGHOST=127.0.0.1', 'PGPORT=55432'), [Text.UTF8Encoding]::new($false))

    $default = Resolve-StockPlatformDataDirectory -PlatformRoot 'G:\StockPlatform' -RuntimeEnv $envPath
    Assert-True ($default -eq 'G:\StockPlatform\data\postgresql16') 'an old deployment without PGDATA_DIR keeps the G: data directory'
    $defaultCold = Get-StockPlatformColdTablespaceDirectory -PlatformRoot 'G:\StockPlatform' -RuntimeEnv $envPath
    Assert-True ($defaultCold -eq 'G:\StockPlatform\data\pg-cold') 'the cold tablespace defaults to the G: platform root'

    Set-StockPlatformEnvValue -Path $envPath -Name 'PGDATA_DIR' -Value 'F:\StockPlatformDB\postgresql16'
    $configured = Resolve-StockPlatformDataDirectory -PlatformRoot 'G:\StockPlatform' -RuntimeEnv $envPath
    Assert-True ($configured -eq 'F:\StockPlatformDB\postgresql16') 'PGDATA_DIR must be honoured even when it leaves the platform root'
    $reread = Read-StockPlatformEnvFile -Path $envPath
    Assert-True ($reread['PGHOST'] -eq '127.0.0.1' -and $reread['PGPORT'] -eq '55432') 'rewriting one key must preserve the others'

    # Rewriting the same key twice must not duplicate the line.
    Set-StockPlatformEnvValue -Path $envPath -Name 'PGDATA_DIR' -Value 'F:\StockPlatformDB\postgresql16'
    $lines = @([IO.File]::ReadAllLines($envPath) | Where-Object { $_.StartsWith('PGDATA_DIR=') })
    Assert-True ($lines.Count -eq 1) 'PGDATA_DIR must be replaced in place, never appended twice'

    # A tuned budget survives another initializer run; an absent one is seeded.
    Assert-True (Set-StockPlatformEnvDefault -Path $envPath -Name 'PGDATA_BUDGET_BYTES' -Value ([string]500GB)) 'an absent default must be written'
    Assert-True (-not (Set-StockPlatformEnvDefault -Path $envPath -Name 'PGDATA_BUDGET_BYTES' -Value '1')) 'an existing operator value must never be overwritten'
    Assert-True ((Read-StockPlatformEnvFile -Path $envPath)['PGDATA_BUDGET_BYTES'] -eq ([string]500GB)) 'the seeded budget must stay at 500 GB'

    $rejected = $false
    try { [void](Resolve-StockPlatformDataDirectory -PlatformRoot 'G:\StockPlatform' -Config @{ PGDATA_DIR = 'data\relative' }) } catch { $rejected = $true }
    Assert-True $rejected 'a relative PGDATA_DIR must be rejected rather than silently joined to a root'

    # --- generating the managed configuration is idempotent ------------------
    $fakeData = Join-Path $sandbox 'pgdata'
    New-Item -ItemType Directory -Force -Path $fakeData | Out-Null
    $clusterConf = Join-Path $fakeData 'postgresql.conf'
    [IO.File]::WriteAllLines($clusterConf, @('# stock cluster', "max_connections = 100"), [Text.UTF8Encoding]::new($false))
    $managedPath = Join-Path $sandbox 'postgresql-stock-platform.conf'
    $first = Write-StockPlatformManagedConfig -DataDirectory $fakeData -ManagedConfigPath $managedPath -Port 55432 -LogDirectory (Join-Path $sandbox 'logs')
    Assert-True (-not $first.include_already_present) 'the first generation must append the include line'
    $second = Write-StockPlatformManagedConfig -DataDirectory $fakeData -ManagedConfigPath $managedPath -Port 55432 -LogDirectory (Join-Path $sandbox 'logs')
    Assert-True ($second.include_already_present) 're-generating must reuse the existing include line'
    $includeLines = @([IO.File]::ReadAllLines($clusterConf) | Where-Object { $_ -match '^include_if_exists' })
    Assert-True ($includeLines.Count -eq 1) 'the cluster configuration must carry exactly one include line'
    Assert-True (@([IO.File]::ReadAllLines($managedPath)).Count -eq $settings.Count) 'the generated file must hold every managed setting'
    Assert-True (([IO.File]::ReadAllText($managedPath)) -match "work_mem = '64MB'") 'the generated file must carry the tuned work_mem'

    $missingCluster = $false
    try { [void](Write-StockPlatformManagedConfig -DataDirectory (Join-Path $sandbox 'absent') -ManagedConfigPath $managedPath -Port 55432 -LogDirectory $sandbox) } catch { $missingCluster = $true }
    Assert-True $missingCluster 'generating against a directory that is not a cluster must fail loudly'
} finally {
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

# --- every consumer reads the configured directory --------------------------
foreach ($entry in @(
    @{ File = 'scripts\windows\start-stock-dashboard.ps1'; Pattern = '\$pgData = Resolve-StockPlatformDataDirectory' },
    @{ File = 'scripts\windows\initialize-stock-platform.ps1'; Pattern = '\$data = Resolve-StockPlatformDataDirectory' },
    @{ File = 'scripts\windows\migrate-postgres-data-directory.ps1'; Pattern = 'Resolve-StockPlatformDataDirectory' }
)) {
    $path = Join-Path $root $entry.File
    $content = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    Assert-True ($content -match $entry.Pattern) "$($entry.File) must resolve the data directory through the shared helper"
    Assert-True ($content -notmatch "'data\\\\postgresql16'") "$($entry.File) must not hard-code the old data directory"
}
$contextSource = [IO.File]::ReadAllText((Join-Path $root 'scripts\stock-platform-context.py'), [Text.Encoding]::UTF8)
Assert-True ($contextSource -match "'authoritative_database':layout\['hot_data_directory'\]") 'the agent context must report the configured hot directory'
Assert-True ($contextSource -match 'PGDATA_COLD_TABLESPACE_DIR') 'the agent context must report the cold tier as well'

# --- the initializer records the tier settings ------------------------------
$initSource = [IO.File]::ReadAllText((Join-Path $windows 'initialize-stock-platform.ps1'), [Text.Encoding]::UTF8)
Assert-True ($initSource -match "Set-StockPlatformEnvValue -Path \`$envPath -Name 'PGDATA_DIR'") 'the initializer must record PGDATA_DIR for every other script'
Assert-True ($initSource -match "Set-StockPlatformEnvDefault -Path \`$envPath -Name 'PGDATA_BUDGET_BYTES'") 'the initializer must seed the hot-tier budget'
Assert-True ($initSource -match 'Write-StockPlatformManagedConfig') 'the initializer must generate the managed configuration through the shared module'
Assert-True ($initSource -notmatch "work_mem = '16MB'") 'the old inline settings block must be gone'

# --- maintenance window schedules -------------------------------------------
foreach ($entry in @(
    @{ File = 'install-stock-backup-task.ps1'; Time = '04:10'; Task = 'trading-hareness-stock-backup' },
    @{ File = 'install-stock-backup-offsite-task.ps1'; Time = '05:10'; Task = 'trading-hareness-stock-backup-offsite' },
    @{ File = 'install-storage-tiers-task.ps1'; Time = '06:00'; Task = 'trading-hareness-storage-tiers' }
)) {
    $path = Join-Path $windows $entry.File
    $content = [IO.File]::ReadAllText($path, [Text.Encoding]::UTF8)
    Assert-True ($content -match "\`$StartTime = '$($entry.Time)'") "$($entry.File) must default to $($entry.Time), inside the 04:00-08:00 maintenance window"
    Assert-True ($content -match "\`$TaskName = '$($entry.Task)'") "$($entry.File) must register $($entry.Task)"
    Assert-True ($content -match 'New-ScheduledTaskSettingsSet -Hidden') "$($entry.File) must register a hidden task"
    Assert-True ($content -match '-RunLevel Limited') "$($entry.File) must run Limited, never elevated"
}
$ioInstaller = [IO.File]::ReadAllText((Join-Path $windows 'install-postgres-io-window-task.ps1'), [Text.Encoding]::UTF8)
Assert-True ($ioInstaller -match "\`$TaskName = 'trading-hareness-postgres-io-window'") 'the I/O window task must keep its name'
Assert-True ($ioInstaller -match '\$IntervalMinutes = 15') 'the I/O window task must run every 15 minutes'
Assert-True ($ioInstaller -match 'New-HiddenPowerShellTaskAction') 'the I/O window task must use the console-free launcher'

# --- the tier runner drives the documented CLI ------------------------------
$runner = [IO.File]::ReadAllText((Join-Path $windows 'run-storage-tiers.ps1'), [Text.Encoding]::UTF8)
Assert-True ($runner -match 'Join-Path \$release ''\.venv\\Scripts\\python\.exe''') 'the tier job must run with the published release venv'
Assert-True ($runner -match 'scripts\\database-storage-tiers\.py') 'the tier job must call database-storage-tiers.py'
Assert-True ($runner -match '''--env-file'', \$RuntimeEnv') 'the tier job must pass --env-file rather than exporting credentials'
Assert-True ($runner -match 'logs\\storage-tiers') 'the tier job must log under logs\storage-tiers'
foreach ($proxy in 'http_proxy', 'https_proxy', 'all_proxy') {
    Assert-True ($runner -match "'$proxy'") "the tier job must clear $proxy before touching the local database"
}
# PowerShell 7 decodes native output with [Console]::OutputEncoding (UTF-8 here),
# so Python must be told to produce UTF-8 as well; left to the cp936 locale it is
# exactly the non-ASCII failure diagnostics that arrive as mojibake.
Assert-True ($runner -match "\`$env:PYTHONIOENCODING = 'utf-8'") 'the tier runner must pin the Python output encoding to match the console'

# The nightly move must stop by itself. Without a deadline it keeps issuing
# large DELETE/INSERT batches and VACUUMs into the opening auction until Task
# Scheduler kills the process -- and a killed process writes no receipt.
Assert-True ($runner -match "\`$Deadline = '08:00'") 'the tier runner must default the nightly deadline to 08:00'
Assert-True ($runner -match "'--deadline', \`$Deadline") 'the tier runner must pass --deadline to the CLI'
Assert-True ($runner -match "'--max-seconds', \[string\]\`$MaxSeconds") 'the tier runner must pass --max-seconds as well'
Assert-True ($runner -match '\$MaxSeconds = 7200') 'the wall-clock backstop must be two hours, inside the task time limit'
$tierInstaller = [IO.File]::ReadAllText((Join-Path $windows 'install-storage-tiers-task.ps1'), [Text.Encoding]::UTF8)
Assert-True ($tierInstaller -match 'New-TimeSpan -Hours 2 -Minutes 15') 'the tier task must allow 2h15m: fifteen minutes of slack over the 06:00-08:00 deadline, no more'

# Both new installers register an absolute Execute path; defaulting it to the
# checkout they happen to run from registers a task that dies with the worktree.
foreach ($installerName in 'install-storage-tiers-task.ps1', 'install-postgres-io-window-task.ps1') {
    $installer = [IO.File]::ReadAllText((Join-Path $windows $installerName), [Text.Encoding]::UTF8)
    Assert-True ($installer -match [regex]::Escape("`$RepositoryRoot = 'G:\StockPlatform\current'")) "$installerName must default -RepositoryRoot to the published release"
    Assert-True ($installer -match [regex]::Escape("`$HostRoot = 'G:\StockPlatform\current'")) "$installerName must default -HostRoot to the published release"
    Assert-True ($installer -match 'Execute = \$registeredAction\.Execute') "$installerName must print the Execute path it stored"
}

# Its window guard is pure; exercise it.
$runnerAst = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $windows 'run-storage-tiers.ps1'), [ref]$null, [ref]$null)
$guard = $runnerAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-StorageTierRunWindowDecision' }, $true)
Assert-True ($null -ne $guard) 'the tier runner must carry a pure window guard'
. ([scriptblock]::Create($guard.Extent.Text))
$wednesday = [DateTime]::ParseExact('2026-09-16 11:00', 'yyyy-MM-dd HH:mm', $null)
Assert-True ((Get-StorageTierRunWindowDecision -Now $wednesday) -eq 'skip_trading_session') 'a replayed run inside the session must be skipped'
Assert-True ((Get-StorageTierRunWindowDecision -Now $wednesday -Force $true) -eq 'run') '-Force must override the session guard'
Assert-True ((Get-StorageTierRunWindowDecision -Now ([DateTime]::ParseExact('2026-09-16 06:00', 'yyyy-MM-dd HH:mm', $null))) -eq 'run') 'the 06:00 scheduled run must proceed'
Assert-True ((Get-StorageTierRunWindowDecision -Now ([DateTime]::ParseExact('2026-09-19 11:00', 'yyyy-MM-dd HH:mm', $null))) -eq 'run') 'a weekend run has no session to protect'

# --- the runner and the Python CLI must agree -------------------------------
# They were written on separate branches; a command the runner offers but the
# CLI does not have fails only at 06:00 in production, so pin it here.
$tierScript = [IO.File]::ReadAllText((Join-Path $root 'scripts\database-storage-tiers.py'), [Text.Encoding]::UTF8)
# The argument list may be wrapped onto the next line (install is), so allow it.
$subcommands = @([regex]::Matches($tierScript, 'sub\.add_parser\(\s*"([a-z]+)"') | ForEach-Object { $_.Groups[1].Value })
Assert-True ($subcommands.Count -ge 4) 'the tier CLI must expose its subcommands through add_parser'
foreach ($expected in 'install', 'plan', 'apply', 'status') {
    Assert-True ($subcommands -contains $expected) "the tier CLI must keep the documented subcommand $expected"
}
$validateSet = [regex]::Match($runner, "\[ValidateSet\(([^)]+)\)\]").Groups[1].Value
$runnerCommands = [regex]::Matches($validateSet, "'([a-z]+)'") | ForEach-Object { $_.Groups[1].Value }
foreach ($command in $runnerCommands) {
    Assert-True ($subcommands -contains $command) "run-storage-tiers.ps1 offers -Command $command but database-storage-tiers.py has no such subcommand"
}
Assert-True ($runnerCommands -contains 'apply') 'the runner must be able to drive the nightly apply'
# --deadline and --max-seconds are in this list because the runner PASSES them:
# a flag the runner sends and the CLI does not declare makes argparse exit 2 at
# 06:00 with "unrecognized arguments", and no row is ever moved again.
foreach ($flag in '--env-file', '--hot-days', '--budget-bytes', '--batch', '--table',
    '--pgdata-dir', '--cold-dir', '--tablespace', '--timeout-ms', '--max-batches',
    '--max-space-days', '--deadline', '--max-seconds', '--log-file') {
    Assert-True ($tierScript -match [regex]::Escape("`"$flag`"")) "the tier CLI must keep the documented flag $flag"
}
# Whatever the runner hands to the CLI must be one of them, however it is spelled.
$runnerFlags = @([regex]::Matches($runner, "'(--[a-z-]+)'") | ForEach-Object { $_.Groups[1].Value } | Sort-Object -Unique)
foreach ($flag in $runnerFlags) {
    Assert-True ($tierScript -match [regex]::Escape("`"$flag`"")) "run-storage-tiers.ps1 passes $flag but database-storage-tiers.py declares no such flag"
}
Assert-True ($runnerFlags -contains '--deadline' -and $runnerFlags -contains '--max-seconds') 'the runner must still pass both stop conditions'
Assert-True ($tierScript -match [regex]::Escape('"--skip-role-settings"')) 'install must keep --skip-role-settings for scratch-database exercises'
# ALTER ROLE ... SET is cluster-wide; skipping it in production would silently
# leave stock_peer without the 15min/5min timeouts documented in section 3.
Assert-True ($runner -notmatch '--skip-role-settings') 'the production runner must never skip the role settings'

# Exit codes are a contract with Task Scheduler and with the operator: 0 is a
# normal night (including a deadline stop), 1 wants a human but the tier is
# intact, 2 means the 500 GB guard is not guarding. A runner that translated 2
# into a generic failure to retry would hammer an unmeasurable directory nightly.
foreach ($pair in @(
    @{ Status = 'ok'; Code = 0 },
    @{ Status = 'deadline_reached'; Code = 0 },
    @{ Status = 'partial'; Code = 1 },
    @{ Status = 'conflicts'; Code = 1 },
    @{ Status = 'schema_drift'; Code = 1 },
    @{ Status = 'degraded'; Code = 2 },
    @{ Status = 'failed'; Code = 2 }
)) {
    Assert-True ($tierScript -match ('"{0}":\s*{1},' -f $pair.Status, $pair.Code)) `
        "the tier CLI must map status $($pair.Status) to exit code $($pair.Code)"
}
Assert-True ($runner -match '\$exitCode = \$LASTEXITCODE') 'the runner must take the CLI exit code'
Assert-True ($runner -match 'exit \$exitCode') 'the runner must exit with the CLI code, never translate it'

# The runner writes a human log per day; the JSONL run record is the CLI's.
Assert-True ($tierScript -match 'storage-tiers\.jsonl') 'the tier CLI must default its run record to logs\storage-tiers.jsonl'
Assert-True ($runner -match "'yyyy-MM-dd'") 'the runner must keep one plain-text log per day beside the JSONL receipt'

# The quarantine table can hold the ONLY copy of a hot row, so it lives in the
# hot (dumped) tier and must never be spelled as a _cold twin -- the dynamic
# exclusion rule only ever drops names ending in _cold.
Assert-True ($tierScript -match 'quant\.storage_tier_conflicts') 'the CLI must quarantine a natural-key conflict into quant.storage_tier_conflicts'
Assert-True ($tierScript -notmatch 'storage_tier_conflicts_cold') 'the quarantine table must not be a cold twin: the nightly dump has to keep it'

# --- the dump exclusions are computed, never seeded --------------------------
# A cold twin may only be left out of the nightly dump when the hot table it was
# filled from has an incremental chunk chain; otherwise the last dump holding
# those rows falls out of retention about two months later and the live cold
# tablespace on the G: HDD becomes their only copy. The rule therefore lives in
# backup-stock-database.ps1 and is evaluated at dump time against
# STOCK_BACKUP_INCREMENTAL_TABLES, instead of being seeded as a static list.
$twins = [regex]::Matches($tierScript, 'TierPolicy\("([a-z_]+)",\s*"([a-z_]+)"') |
    ForEach-Object { "$($_.Groups[1].Value).$($_.Groups[2].Value)_cold" }
$hotTables = [regex]::Matches($tierScript, 'TierPolicy\("([a-z_]+)",\s*"([a-z_]+)"') |
    ForEach-Object { "$($_.Groups[1].Value).$($_.Groups[2].Value)" }
Assert-True ($twins.Count -eq 5) 'the tier policy must still describe five tiered tables'
Assert-True ($initSource -notmatch "Set-StockPlatformEnvDefault[^\r\n]*STOCK_BACKUP_EXCLUDE_TABLE_DATA") `
    'the initializer must not seed a static exclusion list: a twin whose hot table has no chunk chain would lose its only backup'
Assert-True ($initSource -match 'STOCK_BACKUP_EXCLUDE_TABLE_DATA is deliberately NOT seeded') 'the initializer must say why it seeds no exclusion list'

$backupSource = [IO.File]::ReadAllText((Join-Path $windows 'backup-stock-database.ps1'), [Text.Encoding]::UTF8)
$backupAst = [System.Management.Automation.Language.Parser]::ParseFile((Join-Path $windows 'backup-stock-database.ps1'), [ref]$null, [ref]$null)
$resolveAst = $backupAst.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Resolve-StockBackupExcludedTableData' }, $true)
Assert-True ($null -ne $resolveAst) 'backup-stock-database.ps1 must compute the exclusions through Resolve-StockBackupExcludedTableData'
. ([scriptblock]::Create($resolveAst.Extent.Text))
Assert-True ($backupSource -match 'Resolve-StockBackupExcludedTableData `?\r?\n?\s*-IncrementalTables') 'the nightly dump must call the rule with this run''s incremental tables'

# The rule, fed the tier policy's own tables, must yield exactly the twins the
# retired static default listed -- and nothing when no chain exists.
$decided = Resolve-StockBackupExcludedTableData -IncrementalTables $hotTables -IncrementalSucceeded $true -ConfiguredExclusions @()
$decidedTwins = @($decided.Excluded | Where-Object { $_.EndsWith('_cold') })
Assert-True ((($decidedTwins | Sort-Object) -join ';') -eq (($twins | Sort-Object) -join ';')) `
    'with every tiered table in the chunk chain the rule must exclude exactly the cold twins of the tier policy'
$defaultChain = @('quant.raw_market_observations')   # the shipped STOCK_BACKUP_INCREMENTAL_TABLES default
$shipped = Resolve-StockBackupExcludedTableData -IncrementalTables $defaultChain -IncrementalSucceeded $true -ConfiguredExclusions $twins
Assert-True (@($shipped.Refused).Count -eq 4) 'with only the default chain, the four twins without one must be refused rather than excluded'

[pscustomobject]@{
    passed = $true
    scope = 'Generated PostgreSQL settings, PGDATA_DIR resolution, tier-job wiring, runner/CLI agreement, dump exclusions and maintenance-window schedules; no database, no scheduled task, no production file was touched'
}
