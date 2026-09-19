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
    # Wall-clock floor only: the earliest the licensed close cross-section has
    # ever been complete (15:58 on 2026-09-15, 16:02 on 2026-09-11, 16:04 on
    # 2026-09-17). It is not a readiness claim. Nothing downstream trusts the
    # clock -- Test-EquityDateReady fails closed on a half-fetched cross-section
    # and Test-PostCloseLateDatasetsReady fails closed while the derived close
    # limit pool is still shorter than those bars say it should be -- so an
    # early run records what is missing and the 30-minute repetition retries it
    # rather than sealing a partial date.
    [string]$AfterHHmm = '1600',
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

function Invoke-AdjustmentFactorLane {
    # The adjustment-factor lane is the one stage a same-date skip would
    # otherwise never reach. The skip returns before the market refresh, and
    # that refresh is the only caller of the non-gating `adjustment_factors`
    # post-close stage (app/post_close_refresh_service.py). So a session whose
    # ingestion and publication both landed on the first attempt -- the good
    # case -- kept its adj_factor NULL until the 04:30 maintenance task, and
    # every cross-session research window in between was answered by the
    # carry-forward rule instead of by a real factor.
    #
    # There is no HTTP entry point for that stage alone: app/main.py exposes
    # sync_adjustment_factors_post_close() only inside
    # POST /api/v1/market/post-close/refresh, which this branch deliberately
    # does not call. The real entry point is therefore the same CLI the 04:30
    # task uses, run with this repository's venv python.
    #
    # It is non-gating here exactly as it is inside the refresh: every failure
    # is caught and recorded as the lane's own receipt. Nothing it returns can
    # change the skip decision.
    $saved = @{}
    try {
        $python = Join-Path $root '.venv\Scripts\python.exe'
        $worker = Join-Path $root 'scripts\adjustment-factor-maintenance.py'
        foreach ($path in @($python, $worker, $RuntimeEnv)) {
            if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" }
        }
        # tushare is reached directly; an inherited desktop proxy turns a
        # working route into a "provider refused" that would be recorded as a
        # real lane failure (same reason as run-adjustment-factor-maintenance.ps1).
        foreach ($name in @('http_proxy','https_proxy','HTTP_PROXY','HTTPS_PROXY','all_proxy','ALL_PROXY')) {
            $saved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process')
            [Environment]::SetEnvironmentVariable($name, $null, 'Process')
        }
        # The lane resolves its work list from china_today(), so a one-day
        # window only contains the session that just closed. An operator
        # backfilling an older -TradeDate needs the window to actually reach
        # that date; it is still bounded by the lane's own post-close window.
        $age = [int]($now.Date - [datetime]::ParseExact($today,'yyyy-MM-dd',$null)).TotalDays
        $lookback = [Math]::Min(14, [Math]::Max(1, $age + 1))
        $output = & $python $worker sync --lookback-days $lookback --env-file $RuntimeEnv 2>&1
        $exitCode = $LASTEXITCODE
        $lastLine = [string]($output | Select-Object -Last 1)
        if ($exitCode -ne 0) { throw "adjustment factor lane exited ${exitCode}: $lastLine" }
        $lane = $lastLine | ConvertFrom-Json
        $pendingValue = Get-ContractValue $lane 'pending_dates'
        $pending = if ($null -eq $pendingValue) { @() } else { @($pendingValue) }
        $fetched = [int](Get-ContractValue $lane 'completed_dates')
        $skipped = [int](Get-ContractValue $lane 'skipped_dates')
        $reason = if ($pending.Count -eq 0) {
            'no settled date in the lookback window is missing a real cumulative factor'
        } else {
            "pending dates: $($pending -join ', ')"
        }
        return @{ status = [string]$lane.status; fetched = $fetched; skipped = $skipped;
                  lookback_days = $lookback; reason = $reason }
    } catch {
        # A factor route that is down is a condition for the 04:30 backlog
        # owner, never a reason to re-run a verified session.
        return @{ status = 'error'; fetched = 0; skipped = 0; reason = $_.Exception.Message }
    } finally {
        foreach ($name in $saved.Keys) { [Environment]::SetEnvironmentVariable($name, $saved[$name], 'Process') }
    }
}

function Start-IndependentGovernance {
    # Governance is a bounded, independent sidecar AFTER publication. Never let
    # model availability, quota or governance errors invalidate market reports.
    try {
        $registry = Join-Path $platform 'config\governance-actors.json'
        if (-not (Test-Path -LiteralPath $registry)) {
            [void](Write-PipelineRecord -Record @{status='strategy_governance_waiting';
                reason='策略治理侧车，不是公司研究：Provisioned independent role registry is absent; no model started'})
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
        [void](Write-PipelineRecord -Record @{status='strategy_governance_dispatched'; process_id=$child.Id;
            reason='策略治理侧车，不是公司研究：Independent bounded worker launched; not a completed-governance assertion';
            live_effect='none'})
    } catch {
        [void](Write-PipelineRecord -Record @{status='strategy_governance_dispatch_failed';
            reason='策略治理侧车，不是公司研究：' + $_.Exception.Message; live_effect='none'})
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
$lateSkip = Get-PostCloseLateDatasetState -Health $health -TradeDate $today
if (-not $Force -and (Test-EquityDateReady $health $today) -and $lateSkip['ready'] -and $laneState -and
    $laneState.Value.as_of_date -eq $today -and $laneState.Value.status -eq 'completed' -and
    $laneState.Value.version -eq 'short-term-lanes-discovery-split-2026-09-13' -and $selectionState -and
    $selectionState.Value -eq 'shared-research-coverage-2026-09-16' -and $bundleState -and
    $bundleState.Value.version -eq 'strategy-report-bundle-decision-first-2026-09-16') {
    # These three literals must equal app.short_term_lanes.VERSION,
    # app.short_term_lanes.selection.VERSION and app.short_term_lanes.reports.VERSION
    # (tests/test_strategy_publication.py pins them). When they drift, no retry
    # ever skips: every half hour re-runs the market refresh, the same-day scan
    # evidence hash changes and the published recommendation decision goes stale
    # (observed 2026-09-17 and 2026-09-18).
    & (Join-Path $root '.venv\Scripts\python.exe') (Join-Path $root 'scripts\verify-short-term-lanes.py') --date $today --base-url $ApiBase --report-dir $reportDir --reports-only
    if ($LASTEXITCODE -eq 0) {
        # Run the non-gating factor lane BEFORE recording the skip: this branch
        # is the only path that never reaches the post-close refresh, and its
        # verdict must not be able to change the skip decision.
        $factorLane = Invoke-AdjustmentFactorLane
        return Write-PipelineRecord -Record @{ status = 'skipped'; reason = 'same-date market, strategies and all report files verified'; trade_date = $landed; factor_lane = $factorLane }
    }
}

$config = Read-EnvFile $RuntimeEnv
if (-not $config['QUANT_WRITE_API_KEY']) { throw "Missing QUANT_WRITE_API_KEY in $RuntimeEnv" }

$started = Get-Date
$response = $null
$requestError = $null
try {
    $stage = 'equity_ingestion'
    [void](Write-PipelineRecord -Record @{status='running'; late_datasets=$lateSkip})
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
    # Re-probe: $after was read before the refresh ran limit_ladder, so it
    # cannot see the pool this run just derived.
    $lateDatasets = Get-PostCloseLateDatasetState -Health (Read-RequestedEquityStatus) -TradeDate $today
} else {
    $record['ingestion_error'] = 'Requested equity cross-section incomplete; independent flow strategies still run against their own coverage gate'
    $record['equity_readiness'] = Get-ContractValue $after 'daily_control_plane'
    $lateDatasets = Get-PostCloseLateDatasetState -Health $after -TradeDate $today
}
$record['late_datasets'] = $lateDatasets

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
    $record['report_count'] = $readback.run.summary.strategy_lanes.report_bundle.reports.Count
    $record['strategy_run_id'] = $scan.run_id
    $record['lane_counts'] = @($readback.run.summary.strategy_lanes.lanes | ForEach-Object { @{ name=$_.label; matches=$_.total_matches } })
    $record['company_review_coverage'] = $readback.run.summary.strategy_lanes.review_coverage
    # Scan/publication success and company-research closure are separate
    # claims: strategy_status stays 'completed' while the round's research
    # obligation is recorded, owned and deadlined on the same record.
    foreach ($field in (New-PostCloseResearchStatus -ReviewCoverage $record['company_review_coverage'] -TradeDate $today).GetEnumerator()) {
        $record[$field.Key] = $field.Value
    }
    $record['strategy_status'] = 'completed'
    # A short close limit pool degrades the run even when lanes published: the
    # date must not be recorded as landed, so the half-hourly repetition sees
    # the skip condition fail and re-runs the refresh (and its limit_ladder
    # stage) instead of skipping the same partial evening away.
    $record['status'] = Get-PostClosePipelineStatus `
        -Degraded ($record.ContainsKey('market_refresh_error') -or $record.ContainsKey('ingestion_error') -or
            -not $lateDatasets['ready']) `
        -ResearchStatus $record['research_status']
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
# An owed company research round is not a retryable pipeline failure.
if (-not (Test-PostClosePipelineCompleted $record['status'])) { exit 1 }
[pscustomobject]$record
