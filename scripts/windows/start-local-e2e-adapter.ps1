param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$RepositoryRoot = 'F:\AIWorkflow\trading_hareness',
    [int]$Port = 5680
)

$ErrorActionPreference = 'Stop'
$config = @{}
Get-Content -LiteralPath $RuntimeEnv -Encoding UTF8 | ForEach-Object {
    if ($_ -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        $config[$matches[1]] = $matches[2]
    }
}
foreach ($required in 'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD') {
    if (-not $config[$required]) { throw "Missing $required in runtime environment" }
}

$existing = Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($existing) {
    Write-Output "adapter already listening on 127.0.0.1:$Port"
    exit 0
}

$environment = @{
    PGHOST = $config.PGHOST
    PGPORT = $config.PGPORT
    PGDATABASE = $config.PGDATABASE
    PGUSER = $config.PGUSER
    PGPASSWORD = $config.PGPASSWORD
    FEISHU_APP_ID = 'local-e2e'
    FEISHU_APP_SECRET = 'local-e2e'
    N8N_TEXT_WEBHOOK_URL = 'http://127.0.0.1:9/text'
    N8N_MEDIA_PART_WEBHOOK_URL = 'http://127.0.0.1:9/part'
    N8N_MEDIA_FINALIZE_WEBHOOK_URL = 'http://127.0.0.1:9/final'
    QUANT_SERVICE_URL = 'http://127.0.0.1:5681'
    DASHBOARD_HOST = '127.0.0.1'
    DASHBOARD_PORT = [string]$Port
    FRONTEND_DIST = Join-Path $RepositoryRoot 'frontend\dist'
    FRONTEND_MODE = 'spa'
    SOURCE_REGISTRY_FILE = Join-Path $RepositoryRoot 'config\source-registry.json'
    INGESTION_STORAGE_DIR = 'G:\StockPlatform\data\adapter-e2e'
    FEISHU_LONG_CONNECTION_ENABLED = 'false'
    FEISHU_GROUP_RELAY_ENABLED = 'false'
    FEISHU_SUMMARY_LISTENER_ENABLED = 'false'
    WECHAT_GROUP_RELAY_ENABLED = 'false'
    BAIDU_PAN_ENABLED = 'false'
    BAIDU_PAN_MARKET_ARCHIVE_ENABLED = 'false'
}

$runtime = 'G:\StockPlatform\runtime'
$null = New-Item -ItemType Directory -Force -Path $runtime
Start-Process -FilePath 'C:\Program Files\nodejs\node.exe' `
    -ArgumentList @('index.mjs') `
    -WorkingDirectory (Join-Path $RepositoryRoot 'feishu-adapter') `
    -Environment $environment `
    -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $runtime 'adapter-e2e.out.log') `
    -RedirectStandardError (Join-Path $runtime 'adapter-e2e.err.log')

Write-Output "started local E2E adapter on 127.0.0.1:$Port"
