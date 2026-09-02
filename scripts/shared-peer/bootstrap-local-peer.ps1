param(
    [string]$RuntimeEnv = "G:\StockPlatform\config\runtime.env",
    [string]$PeerRoot = "G:\StockPlatform\peer",
    [string]$PeerRole = "stock_peer",
    [string]$PeerN8nDatabase = "trading_hareness_peer_n8n"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Read-EnvFile([string]$Path) {
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            $values[$Matches[1]] = $Matches[2]
        }
    }
    return $values
}

function New-Secret([int]$Bytes = 32) {
    $buffer = [byte[]]::new($Bytes)
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
    return [Convert]::ToBase64String($buffer).TrimEnd('=').Replace('+','-').Replace('/','_')
}

function Write-Utf8NoBomFile([string]$Path, [string[]]$Lines) {
    # No BOM, LF-only line endings so the file is safe to read from Linux
    # peers/containers as well as Windows.
    $content = (($Lines -join "`n")) + "`n"
    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $content, $encoding)
}

function Set-EnvValue([string]$Path, [string]$Name, [string]$Value) {
    $lines = @(Get-Content -LiteralPath $Path)
    $replacement = "$Name=$Value"
    $found = $false
    for ($index = 0; $index -lt $lines.Count; $index++) {
        if ($lines[$index] -match "^$([regex]::Escape($Name))=") {
            $lines[$index] = $replacement
            $found = $true
        }
    }
    if (-not $found) { $lines += $replacement }
    Write-Utf8NoBomFile -Path $Path -Lines $lines
}

$runtime = Read-EnvFile $RuntimeEnv
$postgresRoot = Get-ChildItem -LiteralPath "G:\StockPlatform\runtime" -Directory -Filter "postgresql-*" |
    Sort-Object Name -Descending | Select-Object -First 1
if (-not $postgresRoot) { throw "PostgreSQL runtime not found" }
$psql = Join-Path $postgresRoot.FullName "bin\psql.exe"
$createdb = Join-Path $postgresRoot.FullName "bin\createdb.exe"

$secrets = Join-Path $PeerRoot "secrets"
New-Item -ItemType Directory -Force -Path $secrets | Out-Null
$peerEnv = Join-Path $secrets "peer.env"
$existing = if (Test-Path $peerEnv) { Read-EnvFile $peerEnv } else { @{} }
$peerPassword = if ($existing.PEER_DB_PASSWORD) { $existing.PEER_DB_PASSWORD } else { New-Secret }
$readKey = if ($existing.QUANT_SHARED_READ_API_KEY) { $existing.QUANT_SHARED_READ_API_KEY } else { New-Secret }
$writeKey = if ($existing.PEER_QUANT_WRITE_API_KEY) { $existing.PEER_QUANT_WRITE_API_KEY } else { New-Secret }
$n8nKey = if ($existing.PEER_N8N_ENCRYPTION_KEY) { $existing.PEER_N8N_ENCRYPTION_KEY } else { New-Secret 48 }

$escapedPassword = $peerPassword.Replace("'", "''")
$env:PGPASSWORD = $runtime.PGADMINPASSWORD
try {
    # Idempotent, re-runnable role setup: peer role is read-only on the
    # authoritative quant database (no application-level write membership),
    # but keeps ownership/write access to its own n8n database (granted
    # separately via `createdb -O` below; default_transaction_read_only is
    # scoped to PGDATABASE only, not set globally on the role).
    $roleSql = @"
DO `$peer`$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$PeerRole') THEN
    CREATE ROLE $PeerRole LOGIN PASSWORD '$escapedPassword';
  ELSE
    ALTER ROLE $PeerRole LOGIN PASSWORD '$escapedPassword';
  END IF;
END
`$peer`$;
REVOKE quant_app FROM $PeerRole;
ALTER ROLE $PeerRole NOINHERIT NOCREATEDB NOCREATEROLE CONNECTION LIMIT 8;
GRANT CONNECT ON DATABASE $($runtime.PGDATABASE) TO $PeerRole;
GRANT USAGE ON SCHEMA quant, public TO $PeerRole;
GRANT SELECT ON ALL TABLES IN SCHEMA quant, public TO $PeerRole;
ALTER DEFAULT PRIVILEGES FOR ROLE quant_app IN SCHEMA quant GRANT SELECT ON TABLES TO $PeerRole;
ALTER DEFAULT PRIVILEGES FOR ROLE quant_app IN SCHEMA public GRANT SELECT ON TABLES TO $PeerRole;
ALTER ROLE $PeerRole IN DATABASE $($runtime.PGDATABASE) SET default_transaction_read_only = on;
ALTER ROLE $PeerRole IN DATABASE $($runtime.PGDATABASE) SET statement_timeout = '60s';
ALTER ROLE $PeerRole IN DATABASE $($runtime.PGDATABASE) SET idle_in_transaction_session_timeout = '120s';
"@
    $roleSql | & $psql -v ON_ERROR_STOP=1 -h $runtime.PGHOST -p $runtime.PGPORT `
        -U $runtime.PGADMINUSER -d $runtime.PGDATABASE -f - | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "psql peer role/grant setup failed with exit code $LASTEXITCODE" }

    $existsSql = "SELECT 1 FROM pg_database WHERE datname='$PeerN8nDatabase'"
    $exists = $existsSql | & $psql -At -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGADMINUSER `
        -d postgres -f -
    if ($LASTEXITCODE -ne 0) { throw "psql peer n8n database existence check failed with exit code $LASTEXITCODE" }
    if ($exists -ne "1") {
        & $createdb -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGADMINUSER `
            -O $PeerRole $PeerN8nDatabase
        if ($LASTEXITCODE -ne 0) { throw "createdb failed to create $PeerN8nDatabase with exit code $LASTEXITCODE" }
    }
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}

Set-EnvValue $RuntimeEnv "QUANT_SHARED_READ_API_KEY" $readKey
Write-Utf8NoBomFile -Path $peerEnv -Lines @(
    "PEER_DB_USER=$PeerRole",
    "PEER_DB_PASSWORD=$peerPassword",
    "PEER_QUANT_DATABASE=$($runtime.PGDATABASE)",
    "PEER_N8N_DATABASE=$PeerN8nDatabase",
    "PEER_QUANT_WRITE_API_KEY=$writeKey",
    "QUANT_SHARED_READ_API_KEY=$readKey",
    "PEER_N8N_ENCRYPTION_KEY=$n8nKey",
    "PEER_BACKGROUND_TASKS_ENABLED=false"
)

& icacls $secrets /inheritance:r /grant:r "$env:USERNAME`:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
if ($LASTEXITCODE -ne 0) { throw "icacls failed to secure $secrets with exit code $LASTEXITCODE" }
[pscustomobject]@{
    PeerRole = $PeerRole
    QuantDatabase = $runtime.PGDATABASE
    N8nDatabase = $PeerN8nDatabase
    SecretFile = $peerEnv
    SharedReadGatewayConfigured = $true
}
