$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot '..\post-close-contract.psm1') -Force
foreach ($json in @('{}','{"daily_control_plane":{"state":"absent"}}','{"daily_control_plane":{"trade_date":null}}')) {
    $health = $json | ConvertFrom-Json
    if (Test-EquityDateReady $health '2026-09-09') { throw 'Missing state must request ingestion, not claim ready' }
    if ($null -ne (Get-ContractValue $health 'daily_control_plane.trade_date')) { throw 'Absent date must stay null' }
}
$health = '{"daily_control_plane":{"trade_date":"2026-09-09","state":"blocked"}}' | ConvertFrom-Json
if (Test-EquityDateReady $health '2026-09-09') { throw 'Partial same-date cross section must be repaired' }
$health.daily_control_plane.state = 'ready'
if (-not (Test-EquityDateReady $health '2026-09-09')) { throw 'Complete date not recognized' }
if (Test-EquityDateReady $health '2026-09-10') { throw 'Stale date cannot skip ingestion' }
[pscustomobject]@{passed=$true; missing_and_partial_bootstrap=$true}
