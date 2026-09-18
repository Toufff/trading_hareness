$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
Import-Module (Join-Path $PSScriptRoot '..\post-close-contract.psm1') -Force
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$runner = Join-Path $root 'scripts\windows\run-post-close-pipeline.ps1'
$live = Join-Path $root 'scripts\windows\test-post-close-publication-live.ps1'
$source = [IO.File]::ReadAllText($runner, [Text.Encoding]::UTF8)

# --- research closure: function level -------------------------------------
$complete = New-PostCloseResearchStatus -ReviewCoverage ([pscustomobject]@{ planned = 9; completed = 9; missing_symbols = @() }) -TradeDate '2026-09-18'
if ($complete['research_status'] -ne 'complete') { throw 'Fully reviewed coverage must record complete' }
if ($complete.ContainsKey('research_deadline')) { throw 'A complete round must not carry an open deadline' }

$due = New-PostCloseResearchStatus -ReviewCoverage ([pscustomobject]@{ completed = 0; missing_symbols = @('600519.SH','300750.SZ') }) -TradeDate '2026-09-18'
if ($due['research_status'] -ne 'research_due') { throw 'Uncovered review plan must record research_due, never screening_only' }
if ($due['research_owner'] -ne 'stock-scan post-close session') { throw 'Owed research must name its owner' }
if (@($due['research_missing_symbols']).Count -ne 2 -or $due['research_missing_symbols'][0] -ne '600519.SH') {
    throw 'Owed research must carry the missing symbols from review_coverage'
}
$deadline = [DateTimeOffset]::Parse($due['research_deadline'])
if ($deadline.Offset -ne [TimeSpan]::FromHours(8)) { throw 'Deadline must be stated in Asia/Shanghai offset' }
if ($deadline.ToString('yyyy-MM-ddTHH:mm') -ne '2026-09-18T21:30') { throw 'Deadline must be the trade date at 21:30 exchange time' }

# An absent readback projection is not evidence that research finished.
$unknown = New-PostCloseResearchStatus -ReviewCoverage $null -TradeDate '2026-09-18'
if ($unknown['research_status'] -ne 'research_due') { throw 'Unknown coverage must fail closed' }

# Nor is a present-but-unusable one: the 2026-09-18 defect was a round with a
# nine-lane plan and zero completed reviews being recorded as finished.
$zero = New-PostCloseResearchStatus -ReviewCoverage ([pscustomobject]@{ planned = 9; completed = 0 }) -TradeDate '2026-09-18'
if ($zero['research_status'] -ne 'research_due') { throw 'planned=9/completed=0 must record research_due, never complete' }
if (-not $zero.ContainsKey('research_deadline')) { throw 'An owed round must carry its deadline' }
foreach ($shape in @(
    ([pscustomobject]@{ planned = 9; completed = 9 }),                      # missing_symbols absent
    ([pscustomobject]@{ planned = 9; completed = 9; missing_symbols = $null }),  # JSON null
    ([pscustomobject]@{ planned = 9; missing_symbols = @() }),              # completed absent
    ([pscustomobject]@{ completed = 9; missing_symbols = @() })             # planned absent
)) {
    $shaky = New-PostCloseResearchStatus -ReviewCoverage $shape -TradeDate '2026-09-18'
    if ($shaky['research_status'] -ne 'research_due') {
        throw 'An unusable review_coverage shape must fail closed, not report complete'
    }
}

# The shape the runner actually sees: strategy_lanes.review_coverage as parsed
# from the readback JSON. An empty missing_symbols list survives the round trip
# as *present and empty*, not as an absent field, so a closed round still closes.
$parsed = ('{"planned":9,"completed":9,"missing_symbols":[]}' | ConvertFrom-Json)
if ((New-PostCloseResearchStatus -ReviewCoverage $parsed -TradeDate '2026-09-18')['research_status'] -ne 'complete') {
    throw 'An empty missing_symbols list must not be read as an absent field'
}
$parsedOpen = ('{"planned":9,"completed":0,"missing_symbols":[]}' | ConvertFrom-Json)
if ((New-PostCloseResearchStatus -ReviewCoverage $parsedOpen -TradeDate '2026-09-18')['research_status'] -ne 'research_due') {
    throw 'An empty missing_symbols list must not override a plan with zero completed reviews'
}

# --- top-level status: function level -------------------------------------
if ((Get-PostClosePipelineStatus -Degraded $false -ResearchStatus 'complete') -ne 'completed') { throw 'Closed round must stay completed' }
if ((Get-PostClosePipelineStatus -Degraded $false -ResearchStatus 'research_due') -ne 'completed_research_due') {
    throw 'A published round owing company research must not be recorded as plain completed'
}
if ((Get-PostClosePipelineStatus -Degraded $true -ResearchStatus 'complete') -ne 'partial') { throw 'Ingestion/refresh damage still wins' }
if ((Get-PostClosePipelineStatus -Degraded $true -ResearchStatus 'research_due') -ne 'partial') { throw 'Ingestion/refresh damage still wins' }

# --- downstream equality on the terminal status ---------------------------
foreach ($status in @('completed','completed_research_due')) {
    if (-not (Test-PostClosePipelineCompleted $status)) { throw "Terminal success '$status' must not be read as a failure" }
}
foreach ($status in @('failed','partial','skipped','running','')) {
    if (Test-PostClosePipelineCompleted $status) { throw "'$status' is not terminal success" }
}
if ((Get-PostClosePipelineCompletedStatus).Count -ne 2) { throw 'Completed-status set drifted' }

# --- static assertions on the runner and its live acceptance --------------
if ($source -match 'screening_only') { throw 'screening_only must be gone from the pipeline record' }
if ($source -notmatch "New-PostCloseResearchStatus -ReviewCoverage \`$record\['company_review_coverage'\] -TradeDate \`$today") {
    throw 'The runner must derive research closure from the persisted review coverage'
}
if ($source -notmatch "\`$record\['strategy_status'\] = 'completed'") { throw 'strategy_status stays decoupled from research' }
if ($source -notmatch "\`$record\['status'\] = Get-PostClosePipelineStatus") { throw 'Top-level status must come from the shared rule' }
if ($source -notmatch 'Test-PostClosePipelineCompleted \$record\[''status''\]') { throw 'Exit gate must accept an owed-research completion' }
if ($source -match "if \(\`$record\['status'\] -ne 'completed'\)") { throw 'A raw completed equality survived in the exit gate' }

foreach ($name in @('strategy_governance_dispatched','strategy_governance_dispatch_failed','strategy_governance_waiting')) {
    if ($source -notmatch [regex]::Escape($name)) { throw "Governance sidecar status '$name' is missing" }
}
$block = ($source -split 'function Start-IndependentGovernance \{', 2)[1]
$block = ($block -split "`ntrap \{", 2)[0]
if (([regex]::Matches($block, '策略治理侧车，不是公司研究')).Count -ne 3) {
    throw 'Every governance sidecar record must say it is strategy governance, not company research'
}
if ($block -match "\`$record\['status'\]") { throw 'The governance sidecar must never reclassify the scan' }

$liveSource = [IO.File]::ReadAllText($live, [Text.Encoding]::UTF8)
if ($liveSource -match "\`$terminal\.status -eq 'completed'") { throw 'Publication acceptance still uses a raw completed equality' }
if ($liveSource -notmatch 'Test-PostClosePipelineCompleted \$terminal\.status') { throw 'Publication acceptance must accept completed_research_due' }
if ($liveSource -notmatch 'Get-PostClosePipelineCompletedStatus') { throw 'Terminal-record filter must use the shared status set' }

[pscustomobject]@{passed=$true; scope='Static and function-level pipeline record contract; no pipeline execution, no database, no scheduled task'}
