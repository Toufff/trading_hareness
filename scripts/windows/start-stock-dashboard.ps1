[CmdletBinding()]
param(
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RepositoryRoot = 'F:\AIWorkflow\trading_hareness',
    [int]$AdapterPort = 5680,
    [int]$ApiPort = 5681,
    [int]$RemotePort = 15680,
    [string]$SshHost = 'lightServer1'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Read-EnvFile([string]$Path) {
    $result = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { $result[$parts[0]] = $parts[1] }
    }
    return $result
}

function Test-Listener([int]$Port) {
    return $null -ne (Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function Test-ProcessFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $value = 0
    [void][int]::TryParse(([IO.File]::ReadAllText($Path).Trim()), [ref]$value)
    return $value -gt 0 -and $null -ne (Get-Process -Id $value -ErrorAction SilentlyContinue)
}

$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
if (-not $platform.StartsWith('G:\', [StringComparison]::OrdinalIgnoreCase)) {
    throw "Authoritative stock data must remain on G:, got $platform"
}
$repository = [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
$envPath = Join-Path $platform 'config\runtime.env'
$logs = Join-Path $platform 'logs'
$runtime = Join-Path $platform 'runtime'
$pgData = Join-Path $platform 'data\postgresql16'
$pgBin = Join-Path $runtime 'postgresql-16.15\bin'
$config = Read-EnvFile $envPath
foreach ($required in 'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD', 'QUANT_WRITE_API_KEY') {
    if (-not $config[$required]) { throw "Missing $required in $envPath" }
}
New-Item -ItemType Directory -Force -Path $logs, $runtime | Out-Null

$pgIsReady = Join-Path $pgBin 'pg_isready.exe'
$pgCtl = Join-Path $pgBin 'pg_ctl.exe'
& $pgIsReady -h $config.PGHOST -p $config.PGPORT -q
if ($LASTEXITCODE -ne 0) {
    & $pgCtl start -D $pgData -l (Join-Path $logs 'postgresql-startup.log') -w
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL failed to start from the G: data directory' }
}

& (Join-Path $repository 'scripts\windows\start-stock-platform.ps1') `
    -PlatformRoot $platform -RepositoryRoot $repository -ApiPort $ApiPort | Out-Null

$adapterPid = Join-Path $logs 'dashboard-adapter.pid'
if (-not (Test-Listener $AdapterPort)) {
    if (Test-Path -LiteralPath $adapterPid) { Remove-Item -LiteralPath $adapterPid -Force }
    $environment = @{
        PGHOST = $config.PGHOST; PGPORT = $config.PGPORT; PGDATABASE = $config.PGDATABASE
        PGUSER = $config.PGUSER; PGPASSWORD = $config.PGPASSWORD
        FEISHU_APP_ID = 'dashboard-local'; FEISHU_APP_SECRET = 'dashboard-local'
        N8N_TEXT_WEBHOOK_URL = 'http://127.0.0.1:9/text'
        N8N_MEDIA_PART_WEBHOOK_URL = 'http://127.0.0.1:9/part'
        N8N_MEDIA_FINALIZE_WEBHOOK_URL = 'http://127.0.0.1:9/final'
        QUANT_SERVICE_URL = "http://127.0.0.1:$ApiPort"
        QUANT_WRITE_API_KEY = $config.QUANT_WRITE_API_KEY
        DASHBOARD_HOST = '127.0.0.1'; DASHBOARD_PORT = [string]$AdapterPort
        FRONTEND_DIST = Join-Path $repository 'frontend\dist'; FRONTEND_MODE = 'spa'
        SOURCE_REGISTRY_FILE = Join-Path $repository 'config\source-registry.json'
        INGESTION_STORAGE_DIR = Join-Path $platform 'data\adapter'
        FEISHU_LONG_CONNECTION_ENABLED = 'false'; FEISHU_GROUP_RELAY_ENABLED = 'false'
        FEISHU_SUMMARY_LISTENER_ENABLED = 'false'; WECHAT_GROUP_RELAY_ENABLED = 'false'
        BAIDU_PAN_ENABLED = 'false'; BAIDU_PAN_MARKET_ARCHIVE_ENABLED = 'false'
    }
    $adapter = Start-Process -FilePath 'C:\Program Files\nodejs\node.exe' -PassThru -WindowStyle Hidden `
        -WorkingDirectory (Join-Path $repository 'feishu-adapter') -ArgumentList @('index.mjs') `
        -Environment $environment -RedirectStandardOutput (Join-Path $logs 'dashboard-adapter.stdout.log') `
        -RedirectStandardError (Join-Path $logs 'dashboard-adapter.stderr.log')
    [IO.File]::WriteAllText($adapterPid, [string]$adapter.Id, [Text.UTF8Encoding]::new($false))
}

$deadline = [DateTime]::UtcNow.AddSeconds(30)
while (-not (Test-Listener $AdapterPort) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
if (-not (Test-Listener $AdapterPort)) { throw "Dashboard adapter did not listen on $AdapterPort" }
$localHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$AdapterPort/health" -TimeoutSec 5
if ($localHealth.status -ne 'ok') { throw 'Dashboard adapter health check failed' }

$tunnelPid = Join-Path $logs 'dashboard-tunnel.pid'
$remoteHealthy = $false
try {
    $remoteCode = & ssh -o BatchMode=yes -o ConnectTimeout=8 $SshHost "curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:$RemotePort/health"
    $remoteHealthy = $remoteCode -eq '200'
} catch { $remoteHealthy = $false }
if (-not $remoteHealthy) {
    if (Test-ProcessFile $tunnelPid) {
        $oldPid = [int]([IO.File]::ReadAllText($tunnelPid).Trim())
        Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $tunnelPid -Force -ErrorAction SilentlyContinue
    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    $tunnel = Start-Process -FilePath $ssh -PassThru -WindowStyle Hidden -ArgumentList @(
        '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
        '-R', "127.0.0.1:$RemotePort`:127.0.0.1:$AdapterPort", $SshHost
    ) -RedirectStandardOutput (Join-Path $logs 'dashboard-tunnel.stdout.log') `
      -RedirectStandardError (Join-Path $logs 'dashboard-tunnel.stderr.log')
    [IO.File]::WriteAllText($tunnelPid, [string]$tunnel.Id, [Text.UTF8Encoding]::new($false))
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $remoteCode = & ssh -o BatchMode=yes -o ConnectTimeout=8 $SshHost "curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:$RemotePort/health"
    } while ($remoteCode -ne '200' -and [DateTime]::UtcNow -lt $deadline)
    if ($remoteCode -ne '200') { throw "Reverse dashboard tunnel failed its server-side health check ($remoteCode)" }
}

[pscustomobject]@{
    status = 'ready'
    database_root = $pgData
    api = "http://127.0.0.1:$ApiPort"
    adapter = "http://127.0.0.1:$AdapterPort"
    server_tunnel = "127.0.0.1:$RemotePort"
}
