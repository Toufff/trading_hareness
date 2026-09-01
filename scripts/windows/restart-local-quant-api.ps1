[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$Repository = 'F:\AIWorkflow\trading_hareness',
    [int]$Port = 5681
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

foreach ($line in [IO.File]::ReadAllLines($RuntimeEnv, [Text.Encoding]::UTF8)) {
    if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
    Select-Object -First 1
if ($listener) {
    $existing = Get-CimInstance Win32_Process -Filter "ProcessId=$($listener.OwningProcess)"
    if ($existing.CommandLine -notmatch 'run_server\.py') {
        throw "Port $Port belongs to unexpected process $($existing.ProcessId)"
    }
    Stop-Process -Id $existing.ProcessId -Force
    Wait-Process -Id $existing.ProcessId -ErrorAction SilentlyContinue
}

$logDirectory = 'G:\StockPlatform\logs'
New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$python = Join-Path $Repository '.venv\Scripts\python.exe'
$serviceDirectory = Join-Path $Repository 'quant-service'
$start = @{
    FilePath = $python
    ArgumentList = @('.\run_server.py', '--host', '127.0.0.1', '--port', [string]$Port)
    WorkingDirectory = $serviceDirectory
    WindowStyle = 'Hidden'
    RedirectStandardOutput = (Join-Path $logDirectory 'quant-api.stdout.log')
    RedirectStandardError = (Join-Path $logDirectory 'quant-api.stderr.log')
    PassThru = $true
}
$process = Start-Process @start

$deadline = (Get-Date).AddSeconds(30)
$health = $null
do {
    Start-Sleep -Milliseconds 300
    try {
        $health = Invoke-RestMethod "http://127.0.0.1:$Port/health" -TimeoutSec 2
    }
    catch {
        $health = $null
    }
} while (-not $health -and (Get-Date) -lt $deadline)

if (-not $health) {
    throw "Quant API did not become healthy on port $Port"
}

[pscustomobject]@{
    pid = $process.Id
    port = $Port
    health = $health.status
}
