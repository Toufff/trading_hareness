[CmdletBinding()]
param(
    [string]$PlatformRoot = 'G:\StockPlatform',
    [int]$ApiPort = 5681,
    [int]$AdapterPort = 5680,
    [int]$RecentEventCount = 30
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'runtime-observability.psm1') -Force
Import-Module (Join-Path $PSScriptRoot 'runtime-health-policy.psm1') -Force

function Test-PidProperty {
    param([object]$State, [string]$Property)
    if (-not $State -or -not $State.PSObject.Properties[$Property]) { return $null }
    $value = [int]$State.$Property
    if ($value -le 0) { return $null }
    return [bool](Get-Process -Id $value -ErrorAction SilentlyContinue)
}

function Test-JsonHealth([string]$Url) {
    try {
        $response = Invoke-RestMethod -Uri $Url -TimeoutSec 5 -NoProxy
        return [ordered]@{ reachable = ([string]$response.status -eq 'ok'); status = [string]$response.status; error = $null }
    } catch {
        return [ordered]@{ reachable = $false; status = $null; error = $_.Exception.Message }
    }
}

$root = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$apiHealth = Test-JsonHealth -Url "http://127.0.0.1:$ApiPort/health"
$adapterHealth = Test-JsonHealth -Url "http://127.0.0.1:$AdapterPort/health"
$services = foreach ($service in 'quant-api', 'dashboard-adapter', 'dashboard-tunnel', 'shared-peer-tunnels') {
    $state = Get-RuntimeState -PlatformRoot $root -Service $service
    $alive = [bool](Test-PidProperty -State $state -Property 'supervisor_pid')
    $reachable = switch ($service) { 'quant-api' { $apiHealth.reachable }; 'dashboard-adapter' { $adapterHealth.reachable }; default { $null } }
    $recorded = if ($state -and $state.PSObject.Properties['status']) { [string]$state.status } else { '' }
    [ordered]@{
        service = $service
        effective_status = Resolve-LiveRuntimeState -RecordedStatus $recorded -SupervisorAlive $alive -Reachable $reachable
        recorded_status_is_historical = $true
        state = $state
        supervisor_alive = Test-PidProperty -State $state -Property 'supervisor_pid'
        launcher_alive = Test-PidProperty -State $state -Property 'launcher_pid'
        listener_alive = Test-PidProperty -State $state -Property 'listener_pid'
        stdout_exists = [bool]($state -and $state.PSObject.Properties['stdout'] -and $state.stdout -and (Test-Path -LiteralPath $state.stdout -PathType Leaf))
        stderr_exists = [bool]($state -and $state.PSObject.Properties['stderr'] -and $state.stderr -and (Test-Path -LiteralPath $state.stderr -PathType Leaf))
    }
}

$runtimeRoot = Get-RuntimeLogRoot -PlatformRoot $root
$events = @(
    Get-ChildItem -LiteralPath $runtimeRoot -File -Filter 'lifecycle-*.jsonl' -ErrorAction SilentlyContinue |
        Sort-Object Name -Descending |
        Select-Object -First 2 |
        ForEach-Object { Get-Content -LiteralPath $_.FullName -Encoding UTF8 } |
        Where-Object { $_ } |
        ForEach-Object {
            try { $_ | ConvertFrom-Json } catch { [pscustomobject]@{ event = 'malformed_lifecycle_line'; raw = $_ } }
        } |
        Sort-Object timestamp_utc -Descending |
        Select-Object -First ([Math]::Max(1, $RecentEventCount))
)

[ordered]@{
    checked_at = [DateTimeOffset]::Now.ToString('o')
    platform_root = $root
    api_health = $apiHealth
    adapter_health = $adapterHealth
    services = @($services)
    recent_events = $events
} | ConvertTo-Json -Depth 12
