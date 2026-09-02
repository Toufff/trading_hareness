[CmdletBinding()]
param(
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RepositoryRoot = '',
    [int]$AdapterPort = 5680,
    [int]$ApiPort = 5681,
    [int]$RemotePort = 15680,
    [string]$SshHost = 'lightServer1'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $RepositoryRoot) { $RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
Import-Module (Join-Path $PSScriptRoot 'runtime-observability.psm1') -Force

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

function Get-Listener([int]$Port) {
    return Get-NetTCPConnection -LocalAddress 127.0.0.1 -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
}

function Test-ProcessFile([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $value = 0
    [void][int]::TryParse(([IO.File]::ReadAllText($Path).Trim()), [ref]$value)
    return $value -gt 0 -and $null -ne (Get-Process -Id $value -ErrorAction SilentlyContinue)
}

function Stop-StaleRuntime([string]$Service, [string]$Reason) {
    $state = Request-RuntimeStop -PlatformRoot $platform -Service $Service -Reason $Reason -RequestedBy 'start-stock-dashboard.ps1'
    if (-not $state) { return }
    foreach ($property in 'listener_pid', 'launcher_pid') {
        if ($state.PSObject.Properties[$property] -and [int]$state.$property -gt 0) {
            Stop-Process -Id ([int]$state.$property) -Force -ErrorAction SilentlyContinue
        }
    }
    if ($state.PSObject.Properties['supervisor_pid']) {
        $supervisorPid = [int]$state.supervisor_pid
        $supervisor = Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue
        if ($supervisor) {
            Wait-Process -Id $supervisorPid -Timeout 3 -ErrorAction SilentlyContinue
            if (Get-Process -Id $supervisorPid -ErrorAction SilentlyContinue) { Stop-Process -Id $supervisorPid -Force -ErrorAction SilentlyContinue }
        }
    }
}

function Record-LostRuntime([string]$Service, [string]$Reason) {
    $state = Get-RuntimeState -PlatformRoot $platform -Service $Service
    if (-not $state -or -not $state.PSObject.Properties['run_id']) { return }
    $status = if ($state.PSObject.Properties['status']) { [string]$state.status } else { '' }
    if ($status -in @('healthy', 'process_started', 'supervisor_started')) {
        [void](Write-RuntimeEvent -PlatformRoot $platform -Service $Service -Event 'health_lost' -RunId ([string]$state.run_id) -Level 'error' -Data @{
            previous_status = $status
            reason = $Reason
        })
    }
}

function Test-PostgresReady([int]$Attempts = 3, [int]$DelayMilliseconds = 500) {
    for ($attempt = 1; $attempt -le [Math]::Max(1, $Attempts); $attempt++) {
        & $pgIsReady -h $config.PGHOST -p $config.PGPORT -q
        if ($LASTEXITCODE -eq 0) { return $true }
        if ($attempt -lt $Attempts) { Start-Sleep -Milliseconds $DelayMilliseconds }
    }
    return $false
}

function Get-RemoteDashboardHealth {
    try {
        return (& ssh -o BatchMode=yes -o ConnectTimeout=8 $SshHost "curl -sS -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:$RemotePort/health" 2>$null)
    } catch { return '' }
}

function Test-RemoteDashboardListener {
    & ssh -o BatchMode=yes -o ConnectTimeout=8 $SshHost "ss -ltn 'sport = :$RemotePort' | tail -n +2 | grep -q ." 2>$null
    return $LASTEXITCODE -eq 0
}

function Remove-StaleRemoteDashboardListener {
    [void](Assert-ReservedRemoteTunnelPort -Port $RemotePort -AllowedPorts @(15680))
    [void](Write-RuntimeEvent -PlatformRoot $platform -Service 'dashboard-tunnel' -Event 'stale_remote_listener_cleanup_requested' -Level 'warning' -Data @{
        remote_port = $RemotePort
        ssh_host = $SshHost
    })
    & ssh -o BatchMode=yes -o ConnectTimeout=8 $SshHost "fuser -k $RemotePort/tcp >/dev/null 2>&1 || true"
    if ($LASTEXITCODE -ne 0) { throw "Failed to request cleanup of stale remote listener $RemotePort" }
    $deadline = [DateTime]::UtcNow.AddSeconds(8)
    while ((Test-RemoteDashboardListener) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    if (Test-RemoteDashboardListener) {
        throw "Remote dashboard port $RemotePort remains occupied after bounded cleanup"
    }
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
Invoke-RuntimeLogRetention -PlatformRoot $platform

$pgIsReady = Join-Path $pgBin 'pg_isready.exe'
$pgCtl = Join-Path $pgBin 'pg_ctl.exe'
$postgresReady = Test-PostgresReady
if (-not $postgresReady) {
    $pgStatusText = (& $pgCtl status -D $pgData 2>&1 | Out-String).Trim()
    $pgStatusExitCode = $LASTEXITCODE
    $startupAction = Resolve-PostgresStartupAction -Ready $false -PgCtlStatusExitCode $pgStatusExitCode
    [void](Write-RuntimeEvent -PlatformRoot $platform -Service 'postgresql' -Event 'readiness_failed' -Level 'warning' -Data @{
        pg_ctl_status_exit_code = $pgStatusExitCode
        startup_action = $startupAction
        status_text = $pgStatusText
    })
    if ($startupAction -eq 'wait_for_running_server') {
        if (-not (Test-PostgresReady -Attempts 20 -DelayMilliseconds 500)) {
            throw "PostgreSQL process is running but did not become ready: $pgStatusText"
        }
    } elseif ($startupAction -eq 'start_stopped_server') {
    & $pgCtl start -D $pgData -l (Join-Path $logs 'postgresql-startup.log') -w
    if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL failed to start from the G: data directory' }
        if (-not (Test-PostgresReady -Attempts 10 -DelayMilliseconds 500)) {
            throw 'PostgreSQL was started but did not pass readiness checks'
        }
    } else {
        throw "Cannot determine PostgreSQL state (pg_ctl exit $pgStatusExitCode): $pgStatusText"
    }
}

& (Join-Path $repository 'scripts\windows\start-stock-platform.ps1') `
    -PlatformRoot $platform -RepositoryRoot $repository -ApiPort $ApiPort | Out-Null

$adapterPid = Join-Path $logs 'dashboard-adapter.pid'
if (-not (Test-Listener $AdapterPort)) {
    Record-LostRuntime -Service 'dashboard-adapter' -Reason 'expected_listener_missing'
    Stop-StaleRuntime -Service 'dashboard-adapter' -Reason 'replace_unhealthy_runtime'
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
    $adapterRun = Start-RuntimeSupervisor -PlatformRoot $platform -RepositoryRoot $repository -Service 'dashboard-adapter' `
        -Executable 'C:\Program Files\nodejs\node.exe' -WorkingDirectory (Join-Path $repository 'feishu-adapter') `
        -Arguments @('index.mjs') -Environment $environment -Metadata @{ port = $AdapterPort }
}

$deadline = [DateTime]::UtcNow.AddSeconds(30)
while (-not (Test-Listener $AdapterPort) -and [DateTime]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 250 }
if (-not (Test-Listener $AdapterPort)) { throw "Dashboard adapter did not listen on $AdapterPort" }
$localHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$AdapterPort/health" -TimeoutSec 5
if ($localHealth.status -ne 'ok') { throw 'Dashboard adapter health check failed' }
$adapterListener = Get-Listener -Port $AdapterPort
$adapterState = Get-RuntimeState -PlatformRoot $platform -Service 'dashboard-adapter'
$adapterRunId = if ($adapterState -and $adapterState.PSObject.Properties['run_id']) { [string]$adapterState.run_id } else { "adopted-$($adapterListener.OwningProcess)" }
if (-not $adapterState -or -not $adapterState.PSObject.Properties['status'] -or $adapterState.status -ne 'healthy') {
    [void](Set-RuntimeState -PlatformRoot $platform -Service 'dashboard-adapter' -State @{
        status = 'healthy'
        run_id = $adapterRunId
        supervisor_pid = if ($adapterState -and $adapterState.PSObject.Properties['supervisor_pid']) { [int]$adapterState.supervisor_pid } else { $null }
        launcher_pid = if ($adapterState -and $adapterState.PSObject.Properties['launcher_pid']) { [int]$adapterState.launcher_pid } else { $null }
        listener_pid = $adapterListener.OwningProcess
        descriptor = if ($adapterState -and $adapterState.PSObject.Properties['descriptor']) { [string]$adapterState.descriptor } else { $null }
        stdout = if ($adapterState -and $adapterState.PSObject.Properties['stdout']) { [string]$adapterState.stdout } else { $null }
        stderr = if ($adapterState -and $adapterState.PSObject.Properties['stderr']) { [string]$adapterState.stderr } else { $null }
        health = [string]$localHealth.status
        healthy_at = [DateTimeOffset]::Now.ToString('o')
    })
    [void](Write-RuntimeEvent -PlatformRoot $platform -Service 'dashboard-adapter' -Event 'healthy' -RunId $adapterRunId -Data @{
        listener_pid = $adapterListener.OwningProcess
        health = [string]$localHealth.status
    })
}
[IO.File]::WriteAllText($adapterPid, [string]$adapterListener.OwningProcess, [Text.UTF8Encoding]::new($false))

$tunnelPid = Join-Path $logs 'dashboard-tunnel.pid'
$remoteCode = Get-RemoteDashboardHealth
$remoteHealthy = $remoteCode -eq '200'
if (-not $remoteHealthy) {
    Record-LostRuntime -Service 'dashboard-tunnel' -Reason 'remote_health_check_failed'
    Stop-StaleRuntime -Service 'dashboard-tunnel' -Reason 'replace_unhealthy_runtime'
    if (Test-ProcessFile $tunnelPid) {
        $oldPid = [int]([IO.File]::ReadAllText($tunnelPid).Trim())
        Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $tunnelPid -Force -ErrorAction SilentlyContinue
    # The local launcher may already be gone while sshd still owns the remote
    # reverse-forward listener.  Wait for normal teardown first, then clean the
    # one reserved dashboard port so ExitOnForwardFailure cannot loop forever.
    $releaseDeadline = [DateTime]::UtcNow.AddSeconds(5)
    while ((Test-RemoteDashboardListener) -and [DateTime]::UtcNow -lt $releaseDeadline) {
        Start-Sleep -Milliseconds 500
    }
    if (Test-RemoteDashboardListener) { Remove-StaleRemoteDashboardListener }
    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    $tunnelRun = Start-RuntimeSupervisor -PlatformRoot $platform -RepositoryRoot $repository -Service 'dashboard-tunnel' `
        -Executable $ssh -WorkingDirectory $repository -Arguments @(
        '-N', '-T', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
        '-R', "127.0.0.1:$RemotePort`:127.0.0.1:$AdapterPort", $SshHost
    ) -Metadata @{ remote_port = $RemotePort; adapter_port = $AdapterPort; ssh_host = $SshHost }
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    do {
        Start-Sleep -Milliseconds 500
        $remoteCode = Get-RemoteDashboardHealth
    } while ($remoteCode -ne '200' -and [DateTime]::UtcNow -lt $deadline)
    if ($remoteCode -ne '200') { throw "Reverse dashboard tunnel failed its server-side health check ($remoteCode)" }
    $tunnelState = Get-RuntimeState -PlatformRoot $platform -Service 'dashboard-tunnel'
    $tunnelLauncherPid = if ($tunnelState -and $tunnelState.PSObject.Properties['launcher_pid']) { [int]$tunnelState.launcher_pid } else { 0 }
    if ($tunnelLauncherPid -gt 0) { [IO.File]::WriteAllText($tunnelPid, [string]$tunnelLauncherPid, [Text.UTF8Encoding]::new($false)) }
    [void](Set-RuntimeState -PlatformRoot $platform -Service 'dashboard-tunnel' -State @{
        status = 'healthy'
        run_id = [string]$tunnelRun.run_id
        supervisor_pid = [int]$tunnelRun.supervisor_pid
        launcher_pid = if ($tunnelLauncherPid -gt 0) { $tunnelLauncherPid } else { $null }
        descriptor = [string]$tunnelRun.descriptor
        stdout = [string]$tunnelRun.stdout
        stderr = [string]$tunnelRun.stderr
        remote_port = $RemotePort
        health = 'http_200'
        healthy_at = [DateTimeOffset]::Now.ToString('o')
    })
    [void](Write-RuntimeEvent -PlatformRoot $platform -Service 'dashboard-tunnel' -Event 'healthy' -RunId ([string]$tunnelRun.run_id) -Data @{
        launcher_pid = $tunnelLauncherPid
        remote_port = $RemotePort
        health = 'http_200'
    })
}

[pscustomobject]@{
    status = 'ready'
    database_root = $pgData
    api = "http://127.0.0.1:$ApiPort"
    adapter = "http://127.0.0.1:$AdapterPort"
    server_tunnel = "127.0.0.1:$RemotePort"
}
