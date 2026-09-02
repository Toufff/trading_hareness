[CmdletBinding()]
param(
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RepositoryRoot = 'F:\AIWorkflow\trading_hareness',
    [int]$ApiPort = 5681
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Read-EnvFile {
    param([string]$Path)
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { Set-Item -Path "Env:$($parts[0])" -Value $parts[1] }
    }
}

function Get-ApiListener {
    param([int]$Port)
    return Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::1', '::') } |
        Select-Object -First 1
}

function Test-ExpectedApiProcess {
    param([int]$ProcessId, [string]$Repository, [int]$Port)
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
    if (-not $process) { return $false }
    $command = [string]$process.CommandLine
    return (
        $command -match 'run_server\.py' -and
        $command -match "--port\s+$Port(?:\s|$)" -and
        $command -match [regex]::Escape($Repository)
    )
}

$root = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$repository = [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
$envPath = Join-Path $root 'config\runtime.env'
$logs = Join-Path $root 'logs'
$pidPath = Join-Path $logs 'quant-api.pid'
$python = Join-Path $repository '.venv\Scripts\python.exe'
$serviceRoot = Join-Path $repository 'quant-service'
foreach ($path in @($envPath, $python, (Join-Path $serviceRoot 'database_bootstrap.py'))) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing platform prerequisite: $path" }
}
New-Item -ItemType Directory -Force -Path $logs | Out-Null
Read-EnvFile $envPath
$env:QUANT_BACKGROUND_TASKS_ENABLED = 'false'
$env:QUANT_RUNTIME_PROFILE = 'research'
# Public-market routing is deliberately opt-in through
# QUANT_PUBLIC_HTTP_PROXY in runtime.env.  Inheriting a desktop-wide proxy
# here made otherwise reachable Chinese quote hosts fail inside the service.

$mutex = [Threading.Mutex]::new($false, "Local\trading-hareness-quant-api-$ApiPort")
if (-not $mutex.WaitOne([TimeSpan]::FromSeconds(30))) {
    $mutex.Dispose()
    throw "Timed out waiting for the quant API lifecycle lock on port $ApiPort"
}

try {
    $listener = Get-ApiListener -Port $ApiPort
    if ($listener) {
        if (-not (Test-ExpectedApiProcess -ProcessId $listener.OwningProcess -Repository $repository -Port $ApiPort)) {
            throw "Port $ApiPort belongs to an unexpected process $($listener.OwningProcess)"
        }
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 5
        [IO.File]::WriteAllText($pidPath, [string]$listener.OwningProcess, [Text.UTF8Encoding]::new($false))
        [pscustomobject]@{ status = 'already_running'; pid = $listener.OwningProcess; health = $health.status; url = "http://127.0.0.1:$ApiPort" }
        return
    }

if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $existingPid = 0
    [void][int]::TryParse(([IO.File]::ReadAllText($pidPath).Trim()), [ref]$existingPid)
    if (
        $existingPid -gt 0 -and
        (Test-ExpectedApiProcess -ProcessId $existingPid -Repository $repository -Port $ApiPort)
    ) {
        Stop-Process -Id $existingPid -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pidPath -Force
}

Push-Location $serviceRoot
try {
    & $python '.\database_bootstrap.py' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "database bootstrap/upgrade failed with exit code $LASTEXITCODE" }
} finally {
    Pop-Location
}

$stdout = Join-Path $logs 'quant-api.stdout.log'
$stderr = Join-Path $logs 'quant-api.stderr.log'
$process = Start-Process -FilePath $python -WindowStyle Hidden -PassThru -WorkingDirectory $serviceRoot `
    -RedirectStandardOutput $stdout -RedirectStandardError $stderr `
    -ArgumentList @('.\run_server.py','--host','127.0.0.1','--port',"$ApiPort")
[IO.File]::WriteAllText($pidPath, [string]$process.Id, [Text.UTF8Encoding]::new($false))

$deadline = [DateTime]::UtcNow.AddSeconds(75)
do {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 2
        $listener = Get-ApiListener -Port $ApiPort
        if (-not $listener -or -not (Test-ExpectedApiProcess -ProcessId $listener.OwningProcess -Repository $repository -Port $ApiPort)) {
            throw 'health endpoint responded without the expected listener process'
        }
        [IO.File]::WriteAllText($pidPath, [string]$listener.OwningProcess, [Text.UTF8Encoding]::new($false))
        [pscustomobject]@{ status = 'started'; pid = $listener.OwningProcess; health = $health.status; url = "http://127.0.0.1:$ApiPort" }
        return
    } catch {
        Start-Sleep -Milliseconds 500
    }
} while ([DateTime]::UtcNow -lt $deadline)

$errorTail = if (Test-Path $stderr) { (Get-Content -LiteralPath $stderr -Tail 80) -join [Environment]::NewLine } else { '' }
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
throw "quant API did not become healthy within 75 seconds; inspect $stderr`n$errorTail"
}
finally {
    [void]$mutex.ReleaseMutex()
    $mutex.Dispose()
}
