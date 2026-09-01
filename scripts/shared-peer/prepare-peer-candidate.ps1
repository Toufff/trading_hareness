[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$QuantDump,
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$RepositoryRoot = 'F:\AIWorkflow\trading_hareness',
    [string]$CandidateDatabase = 'trading_hareness_candidate',
    [string]$StockBrainDatabase = 'F:\AIWorkflow\stock-brain\db\brain.db',
    [string]$AdjustedBars = 'F:\AIWorkflow\stock-brain\daily\cache\market_history_qfq.parquet'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Read-EnvFile([string]$Path) {
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $values[$Matches[1]] = $Matches[2] }
    }
    return $values
}

if ($CandidateDatabase -notmatch '^trading_hareness_[a-z0-9_]+$') {
    throw 'CandidateDatabase must stay under the trading_hareness_* namespace'
}
$dump = (Resolve-Path -LiteralPath $QuantDump).Path
$runtime = Read-EnvFile $RuntimeEnv
$postgresRoot = Get-ChildItem -LiteralPath 'G:\StockPlatform\runtime' -Directory -Filter 'postgresql-*' |
    Sort-Object Name -Descending | Select-Object -First 1
if (-not $postgresRoot) { throw 'PostgreSQL runtime not found' }
$psql = Join-Path $postgresRoot.FullName 'bin\psql.exe'
$createdb = Join-Path $postgresRoot.FullName 'bin\createdb.exe'
$dropdb = Join-Path $postgresRoot.FullName 'bin\dropdb.exe'
$pgRestore = Join-Path $postgresRoot.FullName 'bin\pg_restore.exe'
$python = Join-Path $RepositoryRoot '.venv\Scripts\python.exe'

$hashFile = "$dump.sha256"
if (Test-Path -LiteralPath $hashFile) {
    $expected = ((Get-Content -LiteralPath $hashFile -Raw).Trim() -split '\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $dump -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($expected -ne $actual) { throw 'Quant dump SHA-256 mismatch' }
}
& $pgRestore --list $dump *> $null
if ($LASTEXITCODE -ne 0) { throw 'pg_restore could not read the supplied dump' }

$env:PGPASSWORD = $runtime.PGADMINPASSWORD
try {
    $exists = & $psql -w -At -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGADMINUSER `
        -d postgres -c "SELECT 1 FROM pg_database WHERE datname='$CandidateDatabase'"
    if ($exists -eq '1') {
        & $dropdb -w --force -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGADMINUSER $CandidateDatabase
        if ($LASTEXITCODE -ne 0) { throw 'Could not replace stale candidate database' }
    }
    & $createdb -w -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGADMINUSER `
        -O $runtime.PGUSER $CandidateDatabase
    if ($LASTEXITCODE -ne 0) { throw 'Could not create candidate database' }
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}

$env:PGPASSWORD = $runtime.PGPASSWORD
try {
    & $pgRestore --exit-on-error --clean --if-exists --no-owner --no-privileges --jobs=2 `
        -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGUSER -d $CandidateDatabase $dump
    if ($LASTEXITCODE -ne 0) { throw 'Candidate restore failed' }

    $env:PGHOST = $runtime.PGHOST
    $env:PGPORT = $runtime.PGPORT
    $env:PGDATABASE = $CandidateDatabase
    $env:PGUSER = $runtime.PGUSER
    $env:QUANT_LEGACY_SCHEMA_BOOTSTRAP = 'false'
    Push-Location (Join-Path $RepositoryRoot 'quant-service')
    try {
        & $python '.\database_bootstrap.py'
        if ($LASTEXITCODE -ne 0) { throw 'Alembic upgrade on candidate failed' }
    }
    finally { Pop-Location }

    if (Test-Path -LiteralPath $StockBrainDatabase -PathType Leaf) {
        & $python (Join-Path $RepositoryRoot 'scripts\import-stock-brain-database.py') `
            --source $StockBrainDatabase --archive-root 'G:\StockPlatform\data\imports\stock-brain-candidate'
        if ($LASTEXITCODE -ne 0) { throw 'Stock-brain archive import into candidate failed' }
    }
    if (Test-Path -LiteralPath $AdjustedBars -PathType Leaf) {
        & $python (Join-Path $RepositoryRoot 'scripts\import-adjusted-research-bars.py') `
            --source $AdjustedBars --platform-root 'G:\StockPlatform'
        if ($LASTEXITCODE -ne 0) { throw 'Adjusted research-bar import into candidate failed' }
    }

    $revision = (& $psql -w -At -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGUSER `
        -d $CandidateDatabase -c 'SELECT version_num FROM quant.alembic_version').Trim()
    $tableCount = (& $psql -w -At -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGUSER `
        -d $CandidateDatabase -c "SELECT count(*) FROM pg_tables WHERE schemaname='quant'").Trim()
    $instrumentCount = (& $psql -w -At -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGUSER `
        -d $CandidateDatabase -c 'SELECT count(*) FROM quant.instruments').Trim()
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
    foreach ($name in 'PGHOST','PGPORT','PGDATABASE','PGUSER','QUANT_LEGACY_SCHEMA_BOOTSTRAP') {
        Remove-Item "Env:$name" -ErrorAction SilentlyContinue
    }
}

[pscustomobject]@{
    status = 'candidate_ready'
    database = $CandidateDatabase
    alembic_revision = $revision
    quant_tables = [int]$tableCount
    instruments = [int]$instrumentCount
    production_untouched = $true
}
