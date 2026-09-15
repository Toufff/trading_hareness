[CmdletBinding()]
param(
    [string]$BaseUrl = 'https://stock.toufai.top',
    [string]$AccountKey = 'citics-primary',
    [string]$Cookie = ''
)

$ErrorActionPreference = 'Stop'
$headers = @{}
if ($Cookie) { $headers.Cookie = $Cookie }

function Invoke-JsonGet {
    param([string]$Path)
    $response = Invoke-WebRequest -UseBasicParsing -Uri ($BaseUrl.TrimEnd('/') + $Path) -Headers $headers -TimeoutSec 30
    if ($response.StatusCode -ne 200) { throw "$Path returned HTTP $($response.StatusCode)" }
    try { return $response.Content | ConvertFrom-Json }
    catch { throw "$Path did not return JSON: $($response.Content.Substring(0, [Math]::Min(180, $response.Content.Length)))" }
}

$health = Invoke-JsonGet '/health'
$config = Invoke-JsonGet '/api/config'
$brief = Invoke-JsonGet ("/api/research/personal/decision-briefs/latest?account_key=" + [Uri]::EscapeDataString($AccountKey))
$native = Invoke-JsonGet '/api/v1/strategy/contracts'

if (-not $health.status) { throw '/health JSON has no status' }
if (-not $config.routes) { throw '/api/config JSON has no source routes' }
if (-not $brief.as_of_at) { throw 'decision brief JSON has no as_of_at' }
if (-not $native) { throw 'native /api/v1 response is empty' }

[pscustomobject]@{
    status = 'passed'
    health = [string]$health.status
    config_routes = @($config.routes).Count
    decision_brief_as_of = [string]$brief.as_of_at
    adapter_compatibility_route = 'passed'
    native_quant_route = 'passed'
}
