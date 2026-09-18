[CmdletBinding()]
param(
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$PostgresVersion = '16.15',
    [int]$Port = 55432
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# The managed PostgreSQL settings and the hot data-directory resolution are
# shared with migrate-postgres-data-directory.ps1, which regenerates the same
# file after the cluster moves to the NVMe tier.
Import-Module (Join-Path $PSScriptRoot 'postgres-managed-config.psm1') -Force

function Assert-ChildPath {
    param([string]$Root, [string]$Path)
    $resolvedRoot = [IO.Path]::GetFullPath($Root).TrimEnd('\') + '\'
    $resolvedPath = [IO.Path]::GetFullPath($Path)
    if (-not $resolvedPath.StartsWith($resolvedRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe path outside platform root: $resolvedPath"
    }
    return $resolvedPath
}

function New-UrlSafeSecret {
    $bytes = [Security.Cryptography.RandomNumberGenerator]::GetBytes(48)
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function Read-EnvFile {
    param([string]$Path)
    $result = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { $result[$parts[0]] = $parts[1] }
    }
    return $result
}

function Set-EnvFileValue {
    param([string]$Path, [string]$Name, [string]$Value)
    $lines = [Collections.Generic.List[string]]::new()
    $replaced = $false
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if ($line.StartsWith("$Name=")) {
            $lines.Add("$Name=$Value")
            $replaced = $true
        } else {
            $lines.Add($line)
        }
    }
    if (-not $replaced) { $lines.Add("$Name=$Value") }
    [IO.File]::WriteAllLines($Path, $lines, [Text.UTF8Encoding]::new($false))
}

$root = [IO.Path]::GetFullPath($PlatformRoot)
$runtime = Assert-ChildPath $root (Join-Path $root "runtime\postgresql-$PostgresVersion")
$quantData = Assert-ChildPath $root (Join-Path $root 'data\quant')
$config = Assert-ChildPath $root (Join-Path $root 'config')
$logs = Assert-ChildPath $root (Join-Path $root 'logs')
$envPath = Assert-ChildPath $root (Join-Path $config 'runtime.env')
# Everything the platform owns stays under the G: root -- except the PostgreSQL
# hot data directory, which `PGDATA_DIR` in runtime.env points at the NVMe tier
# (production: F:\StockPlatformDB\postgresql16). It is therefore resolved
# through the shared helper instead of Assert-ChildPath; the cold tablespace,
# the backups and every other artifact remain G:-rooted and still are checked.
$data = Resolve-StockPlatformDataDirectory -PlatformRoot $root -RuntimeEnv $envPath
$initdb = Join-Path $runtime 'bin\initdb.exe'
$pgCtl = Join-Path $runtime 'bin\pg_ctl.exe'
$pgIsReady = Join-Path $runtime 'bin\pg_isready.exe'
$psql = Join-Path $runtime 'bin\psql.exe'
$createdb = Join-Path $runtime 'bin\createdb.exe'

foreach ($binary in @($initdb, $pgCtl, $pgIsReady, $psql, $createdb)) {
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
        throw "PostgreSQL runtime is incomplete: $binary"
    }
}
New-Item -ItemType Directory -Force -Path $data, $quantData, $config, $logs | Out-Null

if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) {
    $adminPassword = New-UrlSafeSecret
    $appPassword = New-UrlSafeSecret
    $writeKey = New-UrlSafeSecret
    $lines = @(
        'PGHOST=127.0.0.1'
        "PGPORT=$Port"
        'PGDATABASE=trading_hareness'
        'PGUSER=quant_app'
        "PGPASSWORD=$appPassword"
        'PGADMINUSER=stock_admin'
        "PGADMINPASSWORD=$adminPassword"
        "QUANT_WRITE_API_KEY=$writeKey"
        "QUANT_DATA_DIR=$quantData"
        'QUANT_BACKGROUND_TASKS_ENABLED=false'
        'QUANT_RUNTIME_PROFILE=research'
    )
    [IO.File]::WriteAllLines($envPath, $lines, [Text.UTF8Encoding]::new($false))
} else {
    $settings = Read-EnvFile $envPath
    $adminPassword = $settings.PGADMINPASSWORD
    $appPassword = $settings.PGPASSWORD
}

# Storage tiers (docs/OWNER_DATABASE_STORAGE.md). PGDATA_DIR is recorded so
# every other script resolves the same hot directory; the budget and the cold
# tablespace location are only seeded when absent, because re-running the
# initializer must not reset a deliberately tuned production value.
Set-StockPlatformEnvValue -Path $envPath -Name 'PGDATA_DIR' -Value $data
[void](Set-StockPlatformEnvDefault -Path $envPath -Name 'PGDATA_BUDGET_BYTES' -Value ([string]500GB))
[void](Set-StockPlatformEnvDefault -Path $envPath -Name 'PGDATA_COLD_TABLESPACE_DIR' -Value (Join-Path $root 'data\pg-cold'))

# Cold-tier rows and every backup stay on G:; only the hot cluster moves.
New-Item -ItemType Directory -Force -Path (Get-StockPlatformColdTablespaceDirectory -PlatformRoot $root -RuntimeEnv $envPath) | Out-Null

# Keep large authoritative data on G:, while credentials for the purchased
# feed remain in the user's external local config rather than this repository.
$longhuConfig = Join-Path $env:USERPROFILE '.stock-brain\longhu_vendor.json'
Set-EnvFileValue $envPath 'MARKET_SNAPSHOT_LICENSED_PROVIDERS' 'longhuvip_composite'
Set-EnvFileValue $envPath 'MARKET_SNAPSHOT_ENABLE_PUBLIC_BATCH' 'false'
if (Test-Path -LiteralPath $longhuConfig -PathType Leaf) {
    Set-EnvFileValue $envPath 'QUANT_LONGHU_CONFIG_PATH' $longhuConfig
}

# The runtime configuration is intentionally outside Git and readable only by
# the current user and LocalSystem.  It contains generated local-only secrets.
& icacls.exe $config /inheritance:r /grant:r "${env:USERDOMAIN}\${env:USERNAME}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to restrict runtime configuration ACLs' }

$pgVersionFile = Join-Path $data 'PG_VERSION'
if (-not (Test-Path -LiteralPath $pgVersionFile -PathType Leaf)) {
    $passwordFile = Join-Path $config 'initdb.pw.tmp'
    [IO.File]::WriteAllText($passwordFile, $adminPassword, [Text.UTF8Encoding]::new($false))
    try {
        & $initdb -D $data --username=stock_admin --pwfile=$passwordFile `
            --auth-host=scram-sha-256 --auth-local=scram-sha-256 `
            --encoding=UTF8 --locale=C --data-checksums
        if ($LASTEXITCODE -ne 0) { throw "initdb failed with exit code $LASTEXITCODE" }
    } finally {
        Remove-Item -LiteralPath $passwordFile -Force -ErrorAction SilentlyContinue
    }

}

# The tuned settings block is generated by postgres-managed-config.psm1 so the
# data-directory migration can regenerate exactly the same file (spec §3); the
# cluster's own postgresql.conf only carries the include line.
$managedConfig = Join-Path $config 'postgresql-stock-platform.conf'
[void](Write-StockPlatformManagedConfig -DataDirectory $data -ManagedConfigPath $managedConfig -Port $Port -LogDirectory $logs)

& $pgCtl status -D $data *> $null
if ($LASTEXITCODE -ne 0) {
    # Start-Process gives the long-lived postgres child a clean set of standard
    # handles. Invoking pg_ctl directly from an automation pipe can leave the
    # caller waiting on inherited handles even after pg_ctl reports success.
    $startupLog = Join-Path $logs 'postgresql-startup.log'
    $startProcess = Start-Process -FilePath $pgCtl -WindowStyle Hidden -PassThru `
        -ArgumentList @('start', '-D', $data, '-l', $startupLog, '-w', '-t', '60')
    if (-not $startProcess.WaitForExit(65000)) {
        Stop-Process -Id $startProcess.Id -Force -ErrorAction SilentlyContinue
        throw 'PostgreSQL pg_ctl start timed out after 65 seconds'
    }
    if ($startProcess.ExitCode -ne 0) {
        throw "PostgreSQL failed to start with exit code $($startProcess.ExitCode)"
    }
}

$env:PGPASSWORD = $adminPassword
$env:PGCONNECT_TIMEOUT = '10'
try {
    & $pgIsReady -h 127.0.0.1 -p $Port -U stock_admin -d postgres | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL did not become ready' }

    $roleExistsOutput = & $psql -w -h 127.0.0.1 -p $Port -U stock_admin -d postgres -tAc "SELECT 1 FROM pg_roles WHERE rolname='quant_app'"
    $roleExists = if ($null -eq $roleExistsOutput) { '' } else { ([string]$roleExistsOutput).Trim() }
    # Piped via stdin (-f -) rather than passed as a -c argument so the
    # generated password never appears in this process's command line
    # (visible to any other account via Get-Process/WMI Win32_Process).
    $escapedAppPassword = $appPassword.Replace("'", "''")
    if ($roleExists -ne '1') {
        "CREATE ROLE quant_app LOGIN PASSWORD '$escapedAppPassword'" |
            & $psql -w -h 127.0.0.1 -p $Port -U stock_admin -d postgres -v ON_ERROR_STOP=1 -f - | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create quant_app role' }
    } else {
        "ALTER ROLE quant_app PASSWORD '$escapedAppPassword'" |
            & $psql -w -h 127.0.0.1 -p $Port -U stock_admin -d postgres -v ON_ERROR_STOP=1 -f - | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Failed to refresh quant_app password' }
    }

    $databaseExistsOutput = & $psql -w -h 127.0.0.1 -p $Port -U stock_admin -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='trading_hareness'"
    $databaseExists = if ($null -eq $databaseExistsOutput) { '' } else { ([string]$databaseExistsOutput).Trim() }
    if ($databaseExists -ne '1') {
        & $createdb -w -h 127.0.0.1 -p $Port -U stock_admin -O quant_app trading_hareness | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Failed to create trading_hareness database' }
    }
} finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:PGCONNECT_TIMEOUT -ErrorAction SilentlyContinue
}

$env:PGPASSWORD = $appPassword
$env:PGCONNECT_TIMEOUT = '10'
try {
    $identity = (& $psql -w -h 127.0.0.1 -p $Port -U quant_app -d trading_hareness -tAc 'SELECT current_user || ''@'' || current_database()').Trim()
} finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    Remove-Item Env:PGCONNECT_TIMEOUT -ErrorAction SilentlyContinue
}

[pscustomobject]@{
    status = 'ready'
    postgres_version = $PostgresVersion
    data_directory = $data
    cold_tablespace_directory = (Get-StockPlatformColdTablespaceDirectory -PlatformRoot $root -RuntimeEnv $envPath)
    listen = "127.0.0.1:$Port"
    identity = $identity
    secrets = $envPath
}
