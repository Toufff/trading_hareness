Set-StrictMode -Version Latest

# Shared ownership of the generated PostgreSQL settings file and of the hot
# data-directory location.
#
# The managed settings used to live inline in initialize-stock-platform.ps1.
# They are now a function because two callers must produce byte-identical
# output: the initializer (first install / repair) and
# migrate-postgres-data-directory.ps1, which regenerates the file after the
# cluster moves to the NVMe hot tier. A second hand-maintained copy of the
# block would drift on the first tuning change.
#
# The generated file is included from <data>\postgresql.conf; it is never
# hand-edited in production (docs/OWNER_DATABASE_STORAGE.md).

$script:DefaultDataDirectoryLeaf = 'data\postgresql16'

function Read-StockPlatformEnvFile {
    <#
        "KEY=value" lines, '#' comments, everything after the first '=' is the
        value. Same shape as every other runtime.env reader in this repository;
        values are secrets and must never be echoed.
    #>
    param([Parameter(Mandatory)][string]$Path)
    $result = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $result }
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { $result[$parts[0].Trim()] = $parts[1] }
    }
    return $result
}

function Set-StockPlatformEnvValue {
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][AllowEmptyString()][string]$Value)
    $lines = [Collections.Generic.List[string]]::new()
    $replaced = $false
    if (Test-Path -LiteralPath $Path -PathType Leaf) {
        foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
            if ($line.StartsWith("$Name=")) {
                $lines.Add("$Name=$Value")
                $replaced = $true
            } else {
                $lines.Add($line)
            }
        }
    }
    if (-not $replaced) { $lines.Add("$Name=$Value") }
    [IO.File]::WriteAllLines($Path, $lines, [Text.UTF8Encoding]::new($false))
}

function Set-StockPlatformEnvDefault {
    <#
        Add a documented default only when the operator has not chosen a value.
        Budget and tablespace location are tuning knobs: re-running the
        initializer must never silently reset a deliberate production setting.
    #>
    param([Parameter(Mandatory)][string]$Path, [Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Value)
    $config = Read-StockPlatformEnvFile -Path $Path
    if ($config.ContainsKey($Name) -and $config[$Name]) { return $false }
    Set-StockPlatformEnvValue -Path $Path -Name $Name -Value $Value
    return $true
}

function Resolve-StockPlatformDataDirectory {
    <#
        The PostgreSQL hot data directory. `PGDATA_DIR` in runtime.env wins;
        the default keeps existing deployments (G:\StockPlatform\data\postgresql16)
        working unchanged.

        Deliberately NOT constrained to the platform root: the hot tier lives on
        the NVMe volume (F:\StockPlatformDB\postgresql16) by design, while the
        platform root, the cold tablespace and every backup stay on G:.
    #>
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [string]$RuntimeEnv = '',
        [hashtable]$Config
    )
    $root = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
    if (-not $PSBoundParameters.ContainsKey('Config') -or $null -eq $Config) {
        $envPath = if ($RuntimeEnv) { $RuntimeEnv } else { Join-Path $root 'config\runtime.env' }
        $Config = Read-StockPlatformEnvFile -Path $envPath
    }
    $configured = ''
    if ($Config.ContainsKey('PGDATA_DIR')) { $configured = ([string]$Config['PGDATA_DIR']).Trim() }
    if (-not $configured) { return [IO.Path]::GetFullPath((Join-Path $root $script:DefaultDataDirectoryLeaf)) }
    if (-not [IO.Path]::IsPathRooted($configured)) {
        throw "PGDATA_DIR must be an absolute path, got '$configured'"
    }
    return [IO.Path]::GetFullPath($configured).TrimEnd('\')
}

function Get-StockPlatformColdTablespaceDirectory {
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [string]$RuntimeEnv = '',
        [hashtable]$Config
    )
    $root = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
    if (-not $PSBoundParameters.ContainsKey('Config') -or $null -eq $Config) {
        $envPath = if ($RuntimeEnv) { $RuntimeEnv } else { Join-Path $root 'config\runtime.env' }
        $Config = Read-StockPlatformEnvFile -Path $envPath
    }
    $configured = ''
    if ($Config.ContainsKey('PGDATA_COLD_TABLESPACE_DIR')) { $configured = ([string]$Config['PGDATA_COLD_TABLESPACE_DIR']).Trim() }
    if (-not $configured) { return [IO.Path]::GetFullPath((Join-Path $root 'data\pg-cold')) }
    return [IO.Path]::GetFullPath($configured).TrimEnd('\')
}

function Get-StockPlatformManagedSettings {
    <#
        The generated postgresql-stock-platform.conf body.

        Storage model (docs/OWNER_DATABASE_STORAGE.md): the cluster itself --
        tables, indexes, WAL and temp files -- lives on the NVMe hot tier under
        PGDATA_DIR with a 500 GB soft budget, while rows older than the hot
        window are moved by scripts/database-storage-tiers.py into the
        `stock_cold` tablespace on the G: HDD. Planner costs below describe the
        hot tier; the cold tablespace overrides random_page_cost back to 4.
    #>
    param(
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$LogDirectory
    )
    $logPath = ([IO.Path]::GetFullPath($LogDirectory)).Replace('\', '/')
    return [string[]]@(
        "listen_addresses = '127.0.0.1'"
        "port = $Port"
        # Operational budget, not a hardware ceiling. A 2026-09-23 isolated
        # PostgreSQL 16.15 / 4 GB shared_buffers probe sustained 224 read
        # clients with zero failures; the useful throughput knee was near 64.
        # 100 leaves room for the owner's 20-slot async pool, peer pools and
        # scheduled jobs without declaring 224 a safe production workload.
        'max_connections = 100'
        # Windows refuses large shared segments long before the box runs out of
        # RAM ("could not reserve shared memory region", error 487, already
        # ~83/day at 4GB); keep the value and let the OS file cache do the rest.
        "shared_buffers = '4GB'"
        "effective_cache_size = '32GB'"
        "maintenance_work_mem = '512MB'"
        # 16MB spilled ~251 GB of temp files in 3.5 days on the ranking/backfill
        # queries. 64MB keeps the common sorts in memory; temp_file_limit is the
        # per-session backstop that protects the hot-tier budget.
        "work_mem = '64MB'"
        "temp_file_limit = '20GB'"
        'wal_compression = on'
        "max_wal_size = '4GB'"
        "checkpoint_timeout = '15min'"
        'checkpoint_completion_target = 0.9'
        # The hot tier is NVMe: random reads cost nearly the same as sequential
        # ones. The cold tablespace carries its own random_page_cost = 4.
        'random_page_cost = 1.1'
        # Windows PostgreSQL builds lack posix_fadvise(), so the only valid value
        # is zero. Never "tune" this to a Linux value: the server refuses to start.
        'effective_io_concurrency = 0'
        "shared_preload_libraries = 'pg_stat_statements'"
        'track_io_timing = on'
        'log_lock_waits = on'
        # kB: a query spilling more than 10 MB of temp files is worth a log line
        # because temp files land on the budgeted hot tier.
        'log_temp_files = 10240'
        'logging_collector = on'
        "log_directory = '$logPath'"
        "log_filename = 'postgresql-%Y-%m-%d.log'"
        "log_rotation_age = '1d'"
        # The default prefix ('%m [%p] ') carries no role, so an error raised by
        # an external consumer such as stock_peer cannot be attributed and fed
        # back to whoever produced it. %q suppresses the session fields for
        # background workers. app/peer_error_feed.py parses exactly this shape.
        "log_line_prefix = '%m [%p] %q%u@%d app=%a %e '"
        'log_min_duration_statement = 2000'
        "timezone = 'Asia/Shanghai'"
        "log_timezone = 'Asia/Shanghai'"
    )
}

function Write-StockPlatformManagedConfig {
    <#
        Write the generated settings file and make sure the cluster's own
        postgresql.conf includes it. Idempotent; used by the initializer and by
        the data-directory migration.
    #>
    param(
        [Parameter(Mandatory)][string]$DataDirectory,
        [Parameter(Mandatory)][string]$ManagedConfigPath,
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$LogDirectory
    )
    $settings = Get-StockPlatformManagedSettings -Port $Port -LogDirectory $LogDirectory
    [IO.File]::WriteAllLines($ManagedConfigPath, $settings, [Text.UTF8Encoding]::new($false))
    $includeLine = "include_if_exists = '$($ManagedConfigPath.Replace('\', '/'))'"
    $postgresConfig = Join-Path $DataDirectory 'postgresql.conf'
    $included = $false
    if (Test-Path -LiteralPath $postgresConfig -PathType Leaf) {
        $baseConfigText = [IO.File]::ReadAllText($postgresConfig, [Text.Encoding]::UTF8)
        if ($baseConfigText.Contains($includeLine)) {
            $included = $true
        } else {
            [IO.File]::AppendAllText(
                $postgresConfig,
                [Environment]::NewLine + $includeLine + [Environment]::NewLine,
                [Text.UTF8Encoding]::new($false))
        }
    } else {
        throw "Cluster configuration is missing: $postgresConfig"
    }
    return [pscustomobject]@{
        managed_config = $ManagedConfigPath
        setting_count = $settings.Count
        include_line = $includeLine
        include_already_present = $included
    }
}

Export-ModuleMember -Function Read-StockPlatformEnvFile, Set-StockPlatformEnvValue, Set-StockPlatformEnvDefault,
    Resolve-StockPlatformDataDirectory, Get-StockPlatformColdTablespaceDirectory,
    Get-StockPlatformManagedSettings, Write-StockPlatformManagedConfig
