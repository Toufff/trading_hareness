param(
    [string]$RuntimeEnv = "G:\StockPlatform\config\runtime.env",
    [string]$PeerRoot = "G:\StockPlatform\peer",
    [string]$PeerRole = "stock_peer",
    [string]$PeerN8nDatabase = "trading_hareness_peer_n8n"
)

$ErrorActionPreference = "Stop"

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
    Set-Content -LiteralPath $Path -Value $lines -Encoding utf8
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
    $roleSql = @"
DO `$peer`$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$PeerRole') THEN
    CREATE ROLE $PeerRole LOGIN INHERIT PASSWORD '$escapedPassword';
  ELSE
    ALTER ROLE $PeerRole LOGIN INHERIT PASSWORD '$escapedPassword';
  END IF;
END
`$peer`$;
GRANT quant_app TO $PeerRole;
GRANT CONNECT ON DATABASE $($runtime.PGDATABASE) TO $PeerRole;
"@
    & $psql -v ON_ERROR_STOP=1 -h $runtime.PGHOST -p $runtime.PGPORT `
        -U $runtime.PGADMINUSER -d $runtime.PGDATABASE -c $roleSql | Out-Null
    $exists = & $psql -At -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGADMINUSER `
        -d postgres -c "SELECT 1 FROM pg_database WHERE datname='$PeerN8nDatabase'"
    if ($exists -ne "1") {
        & $createdb -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGADMINUSER `
            -O $PeerRole $PeerN8nDatabase
    }
}
finally {
    Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue
}

Set-EnvValue $RuntimeEnv "QUANT_SHARED_READ_API_KEY" $readKey
@(
    "PEER_DB_USER=$PeerRole",
    "PEER_DB_PASSWORD=$peerPassword",
    "PEER_QUANT_DATABASE=$($runtime.PGDATABASE)",
    "PEER_N8N_DATABASE=$PeerN8nDatabase",
    "PEER_QUANT_WRITE_API_KEY=$writeKey",
    "QUANT_SHARED_READ_API_KEY=$readKey",
    "PEER_N8N_ENCRYPTION_KEY=$n8nKey",
    "PEER_BACKGROUND_TASKS_ENABLED=false"
) | Set-Content -LiteralPath $peerEnv -Encoding utf8

& icacls $secrets /inheritance:r /grant:r "$env:USERNAME`:(OI)(CI)F" "Administrators:(OI)(CI)F" | Out-Null
[pscustomobject]@{
    PeerRole = $PeerRole
    QuantDatabase = $runtime.PGDATABASE
    N8nDatabase = $PeerN8nDatabase
    SecretFile = $peerEnv
    SharedReadGatewayConfigured = $true
}
