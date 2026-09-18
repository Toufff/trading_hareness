Set-StrictMode -Version Latest

function Get-ContractValue([object]$Value, [string]$Path) {
    foreach ($part in $Path.Split('.')) {
        if ($null -eq $Value) { return $null }
        if ($Value -is [System.Collections.IDictionary]) { $Value = $Value[$part] }
        else {
            $property = $Value.PSObject.Properties[$part]
            if (-not $property) { return $null }
            $Value = $property.Value
        }
    }
    return $Value
}

function Test-EquityDateReady([object]$Health, [string]$TradeDate) {
    return ((Get-ContractValue $Health 'daily_control_plane.trade_date') -eq $TradeDate -and
        (Get-ContractValue $Health 'daily_control_plane.state') -eq 'ready')
}

# A published scan whose company research is still owed is a *successful* run
# with an open obligation, not a failure and not a silent 'completed'. Every
# reader of post-close-pipeline.jsonl has to treat both spellings as terminal
# success, so the set lives here once instead of in each downstream equality.
function Get-PostClosePipelineCompletedStatus {
    return @('completed', 'completed_research_due')
}

function Test-PostClosePipelineCompleted([string]$Status) {
    return $Status -in (Get-PostClosePipelineCompletedStatus)
}

# Company research (nine-lane review plan closure) is a separate scope from the
# strategy scan. 'screening_only' was read as a legitimate terminal state, so
# the value is now 'research_due' and it carries who owes what and by when.
function New-PostCloseResearchStatus {
    param(
        [Parameter(Mandatory)][AllowNull()][object]$ReviewCoverage,
        [Parameter(Mandatory)][ValidatePattern('^\d{4}-\d{2}-\d{2}$')][string]$TradeDate
    )
    $missing = @()
    if ($null -ne $ReviewCoverage) {
        $raw = Get-ContractValue $ReviewCoverage 'missing_symbols'
        if ($null -ne $raw) { $missing = @($raw) }
    }
    # Unknown coverage is not evidence of completed research: fail closed.
    $due = ($null -eq $ReviewCoverage) -or ($missing.Count -gt 0)
    $result = @{ research_status = $(if ($due) { 'research_due' } else { 'complete' }) }
    if ($due) {
        $deadline = [datetime]::ParseExact($TradeDate, 'yyyy-MM-dd', $null).AddHours(21).AddMinutes(30)
        $offset = [TimeZoneInfo]::FindSystemTimeZoneById('China Standard Time').GetUtcOffset($deadline)
        $result['research_deadline'] = ([DateTimeOffset]::new($deadline, $offset)).ToString('o')
        $result['research_missing_symbols'] = $missing
        $result['research_owner'] = 'stock-scan post-close session'
    }
    return $result
}

function Get-PostClosePipelineStatus {
    param([bool]$Degraded, [Parameter(Mandatory)][string]$ResearchStatus)
    if ($Degraded) { return 'partial' }
    if ($ResearchStatus -eq 'research_due') { return 'completed_research_due' }
    return 'completed'
}

Export-ModuleMember -Function Get-ContractValue, Test-EquityDateReady,
    Get-PostClosePipelineCompletedStatus, Test-PostClosePipelineCompleted,
    New-PostCloseResearchStatus, Get-PostClosePipelineStatus
