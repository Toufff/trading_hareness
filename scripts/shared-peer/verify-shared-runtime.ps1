[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$ApiBase = 'http://127.0.0.1:5681',
    [string]$SshAlias = 'lightServer1',
    [int]$RemoteDatabasePort = 15432,
    [int]$RemoteApiPort = 15681,
    [int]$RemotePeerApiPort = 15682,
    [string]$PeerApiBase = ''
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
$runtime = Read-EnvFile $RuntimeEnv
$postgresRoot = Get-ChildItem -LiteralPath 'G:\StockPlatform\runtime' -Directory -Filter 'postgresql-*' |
    Sort-Object Name -Descending | Select-Object -First 1
$psql = Join-Path $postgresRoot.FullName 'bin\psql.exe'
$env:PGPASSWORD = $runtime.PGPASSWORD
try {
    $databaseIdentity = (& $psql -w -At -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGUSER `
        -d $runtime.PGDATABASE -c "SELECT current_database()||':'||version_num FROM quant.alembic_version").Trim()
}
finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }

$health = Invoke-RestMethod -Uri "$ApiBase/health" -TimeoutSec 5
$headers = @{ 'X-Quant-Read-Key' = $runtime.QUANT_SHARED_READ_API_KEY }
$quote = Invoke-RestMethod -Uri "$ApiBase/licensed/longhu/quotes?symbols=600664.SH" `
    -Headers $headers -TimeoutSec 35
if (@($quote.rows).Count -ne 1) { throw 'Licensed read gateway did not return the requested quote' }

$remotePorts = & ssh.exe -o BatchMode=yes $SshAlias `
    "ss -lnt | grep -E '127.0.0.1:($RemoteDatabasePort|$RemoteApiPort|$RemotePeerApiPort)' | wc -l"
if ([int]$remotePorts -lt 3) { throw 'Database, owner API, and peer API loopback ports are not all available on lightServer' }

$remoteOwnerCode = (& ssh.exe -o BatchMode=yes $SshAlias `
    "curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:$RemoteApiPort/health").Trim()
if ($remoteOwnerCode -ne '200') {
    throw "Remote owner API tunnel returned HTTP $remoteOwnerCode on 127.0.0.1:$RemoteApiPort"
}
$remotePeerCode = (& ssh.exe -o BatchMode=yes $SshAlias `
    "curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:$RemotePeerApiPort/health").Trim()
if ($remotePeerCode -ne '200') {
    throw "Remote peer API returned HTTP $remotePeerCode on 127.0.0.1:$RemotePeerApiPort"
}
$completeGatewayJson = (& ssh.exe -o BatchMode=yes $SshAlias `
    'python3 /home/stockpeer/trading_hareness/scripts/shared-peer/verify-complete-stock-api.py') -join [Environment]::NewLine
$completeGateway = $completeGatewayJson | ConvertFrom-Json
if (-not $completeGateway.passed) { throw 'Complete remote stock API acceptance probe failed' }

$peerHealth = $null
if ($PeerApiBase) {
    $peerHealth = Invoke-RestMethod -Uri "$($PeerApiBase.TrimEnd('/'))/health" -TimeoutSec 10
}

[pscustomobject]@{
    status = 'verified'
    local_database = $databaseIdentity
    local_api = $health.status
    licensed_quote_rows = @($quote.rows).Count
    reverse_tunnel_ports = [int]$remotePorts
    remote_owner_api = [int]$remoteOwnerCode
    remote_peer_api = [int]$remotePeerCode
    complete_stock_gateway = $completeGateway.passed
    peer_api = if ($peerHealth) { $peerHealth.status } else { 'not_requested' }
    secrets_printed = $false
}
