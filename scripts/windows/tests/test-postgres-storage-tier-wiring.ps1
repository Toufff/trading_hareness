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

[pscustomobject]@{
    passed = $true
    scope = 'Generated PostgreSQL settings, PGDATA_DIR resolution, tier-job wiring and maintenance-window schedules; no database, no scheduled task, no production file was touched'
}
