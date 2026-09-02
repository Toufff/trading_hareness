[CmdletBinding()]
param(
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RepositoryRoot = '',
    [int]$ApiPort = 5681
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $RepositoryRoot) { $RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
Import-Module (Join-Path $PSScriptRoot 'runtime-observability.psm1') -Force

function Read-EnvFile {
    # Returns a hashtable instead of injecting every key into the process
    # environment: a full Set-Item Env: injection here would leak DB admin
    # passwords and write keys into every later sibling process in this
    # pwsh session (ssh tunnels, watchdogs, ...), not just the intended
    # child. Callers pass only what a given child process needs via
    # Start-RuntimeSupervisor's -Environment parameter.
    param([string]$Path)
    $result = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { $result[$parts[0]] = $parts[1] }
    }
    return $result
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
Invoke-RuntimeLogRetention -PlatformRoot $root
$runtimeConfig = Read-EnvFile $envPath
$environment = @{}
foreach ($key in $runtimeConfig.Keys) { $environment[$key] = $runtimeConfig[$key] }
$environment['QUANT_BACKGROUND_TASKS_ENABLED'] = 'false'
$environment['QUANT_RUNTIME_PROFILE'] = 'research'
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
        $current = Get-RuntimeState -PlatformRoot $root -Service 'quant-api'
        $currentStatus = if ($current -and $current.PSObject.Properties['status']) { [string]$current.status } else { '' }
        $currentListenerPid = if ($current -and $current.PSObject.Properties['listener_pid']) { [int]$current.listener_pid } else { 0 }
        $currentRunId = if ($current -and $current.PSObject.Properties['run_id']) { [string]$current.run_id } else { '' }
        if (-not $current -or $currentStatus -ne 'healthy' -or $currentListenerPid -ne $listener.OwningProcess) {
            $runId = if ($currentRunId) { $currentRunId } else { "adopted-$($listener.OwningProcess)" }
            [void](Set-RuntimeState -PlatformRoot $root -Service 'quant-api' -State @{
                status = 'healthy'
                run_id = $runId
                listener_pid = $listener.OwningProcess
                health = [string]$health.status
                adopted = -not [bool]$currentRunId
            })
            [void](Write-RuntimeEvent -PlatformRoot $root -Service 'quant-api' -Event 'listener_adopted' -RunId $runId -Data @{
                listener_pid = $listener.OwningProcess
                health = [string]$health.status
            })
        }
        [pscustomobject]@{ status = 'already_running'; pid = $listener.OwningProcess; health = $health.status; url = "http://127.0.0.1:$ApiPort" }
        return
    }

    $previousState = Get-RuntimeState -PlatformRoot $root -Service 'quant-api'
    $previousStatus = if ($previousState -and $previousState.PSObject.Properties['status']) { [string]$previousState.status } else { '' }
    $previousRunId = if ($previousState -and $previousState.PSObject.Properties['run_id']) { [string]$previousState.run_id } else { '' }
    if ($previousState -and $previousStatus -in @('healthy', 'process_started', 'supervisor_started')) {
        [void](Write-RuntimeEvent -PlatformRoot $root -Service 'quant-api' -Event 'health_lost' -RunId $previousRunId -Level 'error' -Data @{
            previous_status = $previousStatus
            previous_listener_pid = if ($previousState.PSObject.Properties['listener_pid']) { [int]$previousState.listener_pid } else { $null }
            previous_launcher_pid = if ($previousState.PSObject.Properties['launcher_pid']) { [int]$previousState.launcher_pid } else { $null }
            reason = 'expected_listener_missing'
        })
    }

    if ($previousState) {
        [void](Request-RuntimeStop -PlatformRoot $root -Service 'quant-api' -Reason 'replace_unhealthy_runtime' -RequestedBy 'start-stock-platform.ps1')
        foreach ($property in 'listener_pid', 'launcher_pid') {
            $stalePid = 0
            if ($previousState.PSObject.Properties[$property]) { $stalePid = [int]$previousState.$property }
            if ($stalePid -gt 0) { Stop-Process -Id $stalePid -Force -ErrorAction SilentlyContinue }
        }
        if ($previousState.PSObject.Properties['supervisor_pid']) {
            $supervisorPid = [int]$previousState.supervisor_pid
            $supervisor = Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue
            if ($supervisor) {
                Wait-Process -Id $supervisorPid -Timeout 3 -ErrorAction SilentlyContinue
                if (Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue) { Stop-Process -Id $supervisorPid -Force -ErrorAction SilentlyContinue }
            }
        }
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue

Push-Location $serviceRoot
try {
    # database_bootstrap.py is invoked in-process via the call operator, which
    # has no equivalent of Start-Process -Environment, so the required keys
    # are set on the process environment only for the duration of this call
    # and removed immediately afterward in the finally block below.
    foreach ($key in $environment.Keys) { Set-Item -Path "Env:$key" -Value $environment[$key] }
    & $python '.\database_bootstrap.py' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "database bootstrap/upgrade failed with exit code $LASTEXITCODE" }
} finally {
    foreach ($key in $environment.Keys) { Remove-Item -Path "Env:$key" -ErrorAction SilentlyContinue }
    Pop-Location
}

$runtimeRun = Start-RuntimeSupervisor -PlatformRoot $root -RepositoryRoot $repository -Service 'quant-api' `
    -Executable $python -WorkingDirectory $serviceRoot `
    -Arguments @('.\run_server.py', '--host', '127.0.0.1', '--port', "$ApiPort") `
    -Environment $environment -Metadata @{ port = $ApiPort; profile = 'research' }
$stderr = [string]$runtimeRun.stderr

$deadline = [DateTime]::UtcNow.AddSeconds(75)
do {
    try {
        $health = Invoke-RestMethod -Uri "http://127.0.0.1:$ApiPort/health" -TimeoutSec 2
        $listener = Get-ApiListener -Port $ApiPort
        if (-not $listener -or -not (Test-ExpectedApiProcess -ProcessId $listener.OwningProcess -Repository $repository -Port $ApiPort)) {
            throw 'health endpoint responded without the expected listener process'
        }
        [IO.File]::WriteAllText($pidPath, [string]$listener.OwningProcess, [Text.UTF8Encoding]::new($false))
        $state = Get-RuntimeState -PlatformRoot $root -Service 'quant-api'
        [void](Set-RuntimeState -PlatformRoot $root -Service 'quant-api' -State @{
            status = 'healthy'
            run_id = [string]$runtimeRun.run_id
            supervisor_pid = [int]$runtimeRun.supervisor_pid
            launcher_pid = if ($state -and $state.PSObject.Properties['launcher_pid']) { [int]$state.launcher_pid } else { $null }
            listener_pid = $listener.OwningProcess
            health = [string]$health.status
            descriptor = [string]$runtimeRun.descriptor
            stdout = [string]$runtimeRun.stdout
            stderr = [string]$runtimeRun.stderr
            started_at = if ($state -and $state.PSObject.Properties['started_at']) { [string]$state.started_at } else { $null }
            healthy_at = [DateTimeOffset]::Now.ToString('o')
        })
        [void](Write-RuntimeEvent -PlatformRoot $root -Service 'quant-api' -Event 'healthy' -RunId ([string]$runtimeRun.run_id) -Data @{
            listener_pid = $listener.OwningProcess
            health = [string]$health.status
        })
        [pscustomobject]@{ status = 'started'; pid = $listener.OwningProcess; health = $health.status; url = "http://127.0.0.1:$ApiPort" }
        return
    } catch {
        Start-Sleep -Milliseconds 500
    }
} while ([DateTime]::UtcNow -lt $deadline)

$errorTail = if (Test-Path $stderr) { (Get-Content -LiteralPath $stderr -Tail 80) -join [Environment]::NewLine } else { '' }
Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
[void](Write-RuntimeEvent -PlatformRoot $root -Service 'quant-api' -Event 'start_failed' -RunId ([string]$runtimeRun.run_id) -Level 'error' -Data @{
    reason = 'health_timeout'
    stderr = $stderr
})
throw "quant API did not become healthy within 75 seconds; inspect $stderr`n$errorTail"
}
finally {
    [void]$mutex.ReleaseMutex()
    $mutex.Dispose()
}
