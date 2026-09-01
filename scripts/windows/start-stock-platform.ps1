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

if (Test-Path -LiteralPath $pidPath -PathType Leaf) {
    $existingPid = 0
    [void][int]::TryParse(([IO.File]::ReadAllText($pidPath).Trim()), [ref]$existingPid)
    if ($existingPid -gt 0 -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 3
            [pscustomobject]@{ status = 'already_running'; pid = $existingPid; health = $health.status; url = "http://127.0.0.1:$ApiPort" }
            return
        } catch {
            throw "PID $existingPid is alive but the quant API health check failed"
        }
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
    if ($process.HasExited) {
        $errorTail = if (Test-Path $stderr) { (Get-Content -LiteralPath $stderr -Tail 80) -join [Environment]::NewLine } else { '' }
        Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
        throw "quant API exited with code $($process.ExitCode): $errorTail"
    }
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 2
        [pscustomobject]@{ status = 'started'; pid = $process.Id; health = $health.status; url = "http://127.0.0.1:$ApiPort" }
        return
    } catch {
        Start-Sleep -Milliseconds 500
    }
} while ([DateTime]::UtcNow -lt $deadline)

throw "quant API did not become healthy within 75 seconds; inspect $stderr"
