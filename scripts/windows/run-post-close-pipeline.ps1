[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$ApiBase = 'http://127.0.0.1:5681',
    [ValidatePattern('^$|^\d{4}-\d{2}-\d{2}$')][string]$TradeDate = '',
    # GenerateRequest defaults to 'core', which on this host has no members and
    # no maintainer: its only writer was a one-shot legacy bootstrap that ran
    # while quant.instruments was still empty, so the pipeline's last stage
    # died with 422 "universe core has no enabled symbols" after the market
    # data had already been committed. 'all_a' is the pool the settled close
    # observes each day without disabling symbols absent from an incomplete
    # provider response. Point a curated pool
    # here instead once one is actually maintained (POST /api/v1/universes/members).
    [string]$UniverseKey = 'all_a',
    # Earliest the exchange publishes a usable end-of-day cross-section. Before
    # this the daily bars exist but the limit pools do not, and a run would
    # record a partial date as done.
    [string]$AfterHHmm = '1630',
    [string]$UntilHHmm = '2330',
    # Run even outside the window / on a weekend / when the date already
    # landed. For operators backfilling by hand.
    [switch]$Force
)

# Post-close daily pipeline runner.
#
# The repository's original runner (scripts/run-post-close-pipeline.sh) targets
# launchd on a macOS host that no longer exists; nothing on this Windows host
# replaced it, so quant.canonical_bars_daily stopped advancing after
# 2026-09-01 while every service stayed green. This is the Windows equivalent:
# Ingestion and strategy publication have separate completion checks; retries
# only collect missing history and verify the persisted date/version.
#
# Exchange time is the machine's local time here (Asia/Shanghai), unlike the
# macOS original which had to convert from US Pacific.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot 'post-close-contract.psm1') -Force

function Read-EnvFile([string]$Path) {
    $result = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { $result[$parts[0]] = $parts[1] }
    }
    return $result
}

$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$now = [TimeZoneInfo]::ConvertTimeBySystemTimeZoneId([DateTimeOffset]::Now, 'China Standard Time')
$today = if ($TradeDate) { ([datetime]::ParseExact($TradeDate,'yyyy-MM-dd',$null)).ToString('yyyy-MM-dd') } else { $now.ToString('yyyy-MM-dd') }
if ($today -gt $now.ToString('yyyy-MM-dd')) { throw 'Future session cannot be collected' }
$executionId = [guid]::NewGuid().ToString('N')
$stage = 'preflight'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$reportDir = Join-Path $platform 'reports\short-term'

function Write-PipelineRecord {
    param([Parameter(Mandatory)][hashtable]$Record)
    $Record['recorded_at'] = (Get-Date).ToString('o')
    $Record['trading_date'] = $today
    $Record['execution_id'] = $executionId
    $Record['stage'] = $stage
    $logDir = Join-Path $platform 'logs'
    try {
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        [IO.File]::AppendAllText(
            (Join-Path $logDir 'post-close-pipeline.jsonl'),
            (ConvertTo-Json -InputObject $Record -Depth 8 -Compress) + [Environment]::NewLine,
            [Text.UTF8Encoding]::new($false))
    } catch {
        Write-Warning "Failed to write pipeline record: $($_.Exception.Message)"
    }
    [pscustomobject]$Record
}

function Read-RequestedEquityStatus {
    $json = & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'scripts\equity-readiness.py') --date $today --env-file $RuntimeEnv
    if ($LASTEXITCODE -ne 0) { throw 'Requested-date PostgreSQL readiness probe failed' }
    return ($json | ConvertFrom-Json)
}

function Start-IndependentGovernance {
    # Governance is a bounded, independent sidecar AFTER publication. Never let
    # model availability, quota or governance errors invalidate market reports.
    try {
        $registry = Join-Path $platform 'config\governance-actors.json'
        if (-not (Test-Path -LiteralPath $registry)) {
            [void](Write-PipelineRecord -Record @{status='governance_waiting'; reason='Provisioned independent role registry is absent; no model started'})
            return
        }
        $python = Join-Path $root '.venv\Scripts\pythonw.exe'
        if (-not (Test-Path -LiteralPath $python)) { throw 'Console-free pythonw is missing; do not fall back to a console launcher' }
        $worker = Join-Path $root 'scripts\run-strategy-governance.py'
        $logDir = Join-Path $platform 'logs\governance'
        New-Item -ItemType Directory -Force -Path $logDir | Out-Null
        $arguments = @('"' + $worker + '"', '--execute', '--registry', '"' + $registry + '"',
            '--env-file', '"' + $RuntimeEnv + '"', '--job-root', '"' + (Join-Path $platform 'data\governance\jobs') + '"',
            '--max-jobs', '1', '--daily-jobs', '6', '--timeout-seconds', '180', '--cooldown-seconds', '3600',
            '--model', 'gpt-5.6-luna')
        $child = Start-Process -FilePath $python -ArgumentList $arguments -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $logDir "$executionId.stdout.log") `
            -RedirectStandardError (Join-Path $logDir "$executionId.stderr.log")
        [void](Write-PipelineRecord -Record @{status='governance_dispatched'; process_id=$child.Id;
            reason='Independent bounded worker launched; not a completed-governance assertion'; live_effect='none'})
    } catch {
        [void](Write-PipelineRecord -Record @{status='governance_dispatch_failed'; reason=$_.Exception.Message; live_effect='none'})
    }
}

trap {
    [void](Write-PipelineRecord -Record @{ status = 'failed'; error = $_.Exception.Message })
    break
}

if (-not $Force) {
    if ([int]$now.DayOfWeek -eq 0 -or [int]$now.DayOfWeek -eq 6) {
        return Write-PipelineRecord -Record @{ status = 'skipped'; reason = 'weekend in exchange time' }
    }
    $hhmm = $now.ToString('HHmm')
    if ([int]$hhmm -lt [int]$AfterHHmm) {
        return Write-PipelineRecord -Record @{ status = 'skipped'; reason = 'before the post-close window'; now = $hhmm }
    }
    if ([int]$hhmm -ge [int]$UntilHHmm) {
        return Write-PipelineRecord -Record @{ status = 'skipped'; reason = 'past the post-close window'; now = $hhmm }
    }
}

# The health probe's control-plane view is derived from the rows actually
# stored, so it cannot report a date as done that is not. That makes it a
# safer skip condition than a marker file, which survives a database restore
# that rolled the date back.
$health = Read-RequestedEquityStatus
$landed = Get-ContractValue $health 'daily_control_plane.trade_date'
$latest = Invoke-RestMethod -Uri "$ApiBase/api/v1/strategy/post-close/latest?as_of_date=$today" -TimeoutSec 30 -NoProxy
$laneState = $null
if (Get-ContractValue $latest 'run.summary') { $laneState = $latest.run.summary.PSObject.Properties['strategy_lanes'] }
$selectionState = if ($laneState) { $laneState.Value.PSObject.Properties['review_selection_version'] } else { $null }
$bundleState = if ($laneState) { $laneState.Value.PSObject.Properties['report_bundle'] } else { $null }
if (-not $Force -and (Test-EquityDateReady $health $today) -and $laneState -and
    $laneState.Value.as_of_date -eq $today -and $laneState.Value.status -eq 'completed' -and
    $laneState.Value.version -eq 'short-term-lanes-discovery-split-2026-09-13' -and $selectionState -and
    $selectionState.Value -eq 'scan-first-review-selection-2026-09-04' -and $bundleState -and
    $bundleState.Value.version -eq 'strategy-report-bundle-results-first-2026-09-11') {
    & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'scripts\verify-short-term-lanes.py') --date $today --base-url $ApiBase --report-dir $reportDir --reports-only
    if ($LASTEXITCODE -eq 0) {
        return Write-PipelineRecord -Record @{ status = 'skipped'; reason = 'same-date market, strategies and all report files verified'; trade_date = $landed }
    }
}

$config = Read-EnvFile $RuntimeEnv
if (-not $config['QUANT_WRITE_API_KEY']) { throw "Missing QUANT_WRITE_API_KEY in $RuntimeEnv" }

$started = Get-Date
$response = $null
$requestError = $null
try {
    $stage = 'equity_ingestion'
    [void](Write-PipelineRecord -Record @{status='running'})
    if (-not (Test-EquityDateReady $health $today)) {
      $response = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/pipeline/daily" `
        -Headers @{ 'X-Quant-Write-Key' = $config['QUANT_WRITE_API_KEY']; 'Content-Type' = 'application/json' } `
        -Body (ConvertTo-Json -InputObject @{ as_of_date = $today; universe_key = $UniverseKey } -Compress) `
        -TimeoutSec 2400 -NoProxy
    }
} catch {
    # A stage after ingestion can fail on its own (a curated universe that was
    # never seeded, say) long after the market data has been committed. Which
    # of the two happened decides whether this run has to be retried, so the
    # error is carried forward rather than thrown here.
    $requestError = $_.Exception.Message
    if ($_.ErrorDetails -and $_.ErrorDetails.Message) { $requestError = "$requestError $($_.ErrorDetails.Message)" }
}
$elapsed = [int]((Get-Date) - $started).TotalSeconds

# Verify ingestion separately; the final success condition also requires a
# persisted same-date, complete multi-strategy result.
$after = Read-RequestedEquityStatus
$nowLanded = Get-ContractValue $after 'daily_control_plane.trade_date'

$record = @{
    seconds = $elapsed
    previous_trade_date = $landed
    trade_date_after = $nowLanded
}
if ($response) {
    $record['pipeline_status'] = [string]$response.status
    $record['market_sync'] = $response.market_sync
    if ([string]$response.status -ne 'completed') { $record['reason'] = $response.reason }
}
if ($requestError) { $record['error'] = $requestError }

if (Test-EquityDateReady $after $today) {
    # The refresh owns market/sector/event evidence. Candidate decision
    # closure must wait for the persisted nine-lane review plan below.
    $stage = 'market_evidence_refresh'
    [void](Write-PipelineRecord -Record @{status='running'})
    try {
    $fullRefresh = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/market/post-close/refresh" `
        -Headers @{ 'X-Quant-Write-Key' = $config['QUANT_WRITE_API_KEY']; 'Content-Type' = 'application/json' } `
        -Body (ConvertTo-Json -InputObject @{
            trade_date = $today
            include_macro_cross_asset = $true
            include_announcements = $true
            announcement_limit = 20
        } -Compress) -TimeoutSec 1800 -NoProxy
    $record['full_refresh_status'] = [string]$fullRefresh.status
    $record['refresh_deferred_stages'] = Get-ContractValue $fullRefresh 'deferred_stages'
    $record['refresh_stage_diagnostics'] = @{}
    foreach ($p in $fullRefresh.stages.PSObject.Properties) {
        if ((Get-ContractValue $p.Value 'status') -in @('blocked','failed','partial')) {
            $record['refresh_stage_diagnostics'][$p.Name] = @{ status=(Get-ContractValue $p.Value 'status'); reason=(Get-ContractValue $p.Value 'reason'); error=(Get-ContractValue $p.Value 'error') }
        }
    }
    } catch { $record['market_refresh_error'] = $_.Exception.Message }
} else {
    $record['ingestion_error'] = 'Requested equity cross-section incomplete; independent flow strategies still run against their own coverage gate'
    $record['equity_readiness'] = Get-ContractValue $after 'daily_control_plane'
}

try {
    # Persist the independently reviewable strategy lists and their reports.
    $stage = 'strategy_collection_and_screen'
    [void](Write-PipelineRecord -Record @{status='running'})
    $saved = @{}
    foreach ($key in $config.Keys) { $saved[$key] = [Environment]::GetEnvironmentVariable($key, 'Process'); [Environment]::SetEnvironmentVariable($key, $config[$key], 'Process') }
    try {
        Push-Location (Join-Path $root 'quant-service')
        try {
            & (Join-Path $root '.venv\Scripts\python.exe') -m app.short_term_lanes.service --date $today --collect-only
            if ($LASTEXITCODE -ne 0) { throw "Multi-strategy collection/screen failed: $LASTEXITCODE" }
        } finally { Pop-Location }
    } finally { foreach ($key in $saved.Keys) { [Environment]::SetEnvironmentVariable($key, $saved[$key], 'Process') } }
    $scan = Invoke-RestMethod -Method Post -Uri "$ApiBase/api/v1/strategy/post-close/run" `
        -Headers @{ 'X-Quant-Write-Key' = $config['QUANT_WRITE_API_KEY']; 'Content-Type' = 'application/json' } `
        -Body (ConvertTo-Json -InputObject @{ as_of_date = $today } -Compress) -TimeoutSec 180 -NoProxy
    $stage = 'publication_readback'
    $readback = Invoke-RestMethod -Uri "$ApiBase/api/v1/strategy/post-close/latest?as_of_date=$today" -TimeoutSec 30 -NoProxy
    if ($readback.run.as_of_date -ne $today -or $readback.run.summary.strategy_lanes.status -ne 'completed') {
        throw 'Persisted multi-strategy readback did not match requested date and completion'
    }
    # Export the persisted scan, not an independently recomputed CLI generation.
    $stage = 'publication_export'
    & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'scripts\export-strategy-publication.py') --date $today --expected-run-id $scan.run_id --base-url $ApiBase --report-dir $reportDir
    if ($LASTEXITCODE -ne 0) { throw 'Persisted strategy export failed; no recomputation fallback' }
    $stage = 'publication_readback'
    & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'scripts\verify-short-term-lanes.py') --date $today --base-url $ApiBase --report-dir $reportDir --reports-only
    if ($LASTEXITCODE -ne 0) {
        $receiptPath = Join-Path $reportDir ($today + '_report_publication.json')
        if (Test-Path -LiteralPath $receiptPath) {
            $receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
            $record['publication_failed_checks'] = @($receipt.checks.PSObject.Properties | Where-Object Value -EQ $false | ForEach-Object Name)
            $record['publication_differences'] = Get-ContractValue $receipt 'report_difference_paths'
            $record['publication_receipt'] = $receiptPath
        }
        throw 'Strategy report publication failed file / owner API / dashboard readback verification'
    }
    # The human review page is a projection of the same persisted run; its
    # failure must not turn a verified publication into a failed run.
    try {
        $pageOutput = & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'scripts\export-review-page.py') --date $today --expected-run-id $scan.run_id --base-url $ApiBase --report-dir $reportDir 2>&1
        if ($LASTEXITCODE -ne 0) { throw "review page export failed: $($pageOutput | Select-Object -Last 1)" }
        $record['review_page'] = ([string]($pageOutput | Select-Object -Last 1) | ConvertFrom-Json).file
    } catch { $record['review_page_error'] = $_.Exception.Message }
    $record['strategy_status'] = 'completed'
    $record['status'] = if ($record.ContainsKey('market_refresh_error') -or $record.ContainsKey('ingestion_error')) { 'partial' } else { 'completed' }
    $record['report_count'] = $readback.run.summary.strategy_lanes.report_bundle.reports.Count
    $record['strategy_run_id'] = $scan.run_id
    $record['lane_counts'] = @($readback.run.summary.strategy_lanes.lanes | ForEach-Object { @{ name=$_.label; matches=$_.total_matches } })
    $record['company_review_coverage'] = $readback.run.summary.strategy_lanes.review_coverage
    $record['research_status'] = if ($record['company_review_coverage'].missing_symbols.Count -eq 0) { 'complete' } else { 'screening_only' }
    # Independent scopes: a successful scan is not a published recommendation.
    $record['recommendation_status'] = Get-ContractValue $readback 'run.summary.recommendation_pool.status'
    $record['recommendation_decision_id'] = Get-ContractValue $readback 'run.summary.recommendation_pool.decision_id'
    if (-not $record['recommendation_status']) { $record['recommendation_status'] = 'unavailable' }
} catch {
    $record['status'] = 'failed'
    $record['strategy_error'] = $_.Exception.Message
}
[void](Write-PipelineRecord -Record $record)
if ($record.ContainsKey('strategy_status') -and $record['strategy_status'] -eq 'completed') {
    # The existing close job completes the intraday observation ledger too.
    # A failed comparison remains a separate diagnostic, never erases lanes.
    if ($today -eq (Get-Date).ToString('yyyy-MM-dd') -and (Get-Date).Hour -ge 15) {
        try {
            $intradayOutput = & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'scripts\stock-intraday-scan.py') 2>&1
            if ($LASTEXITCODE -ne 0) { throw "Intraday closing scan failed: $LASTEXITCODE" }
            $intradayReceipt = ([string]($intradayOutput | Select-Object -Last 1)) | ConvertFrom-Json
            $record['intraday_close_run_id'] = $intradayReceipt.run_id
            $record['intraday_reconciliation'] = $intradayReceipt.reconciliation
            if ((Get-ContractValue $intradayReceipt 'reconciliation.status') -eq 'failed') { throw 'Intraday comparison failed after successful close scan' }
        } catch {
            $record['intraday_reconciliation_error'] = $_.Exception.Message
            $record['status'] = 'partial'
        }
        [void](Write-PipelineRecord -Record $record)
    }
    Start-IndependentGovernance
}

# A failed scan/readback remains retryable even when market ingestion worked.
if ($record['status'] -ne 'completed') { exit 1 }
[pscustomobject]$record
