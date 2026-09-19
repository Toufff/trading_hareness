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

# Get-ContractValue cannot answer 'is this field there?': PowerShell unrolls a
# returned empty array to $null, so a completed plan with missing_symbols=@()
# and a projection that never wrote the field look identical to the caller.
# Fail-closed decisions need that distinction, so this returns a hashtable
# (never unrolled) carrying presence alongside the value.
function Get-ContractSlot([object]$Value, [string]$Path) {
    foreach ($part in $Path.Split('.')) {
        if ($null -eq $Value) { return @{ present = $false; value = $null } }
        if ($Value -is [System.Collections.IDictionary]) {
            if (-not $Value.Contains($part)) { return @{ present = $false; value = $null } }
            $Value = $Value[$part]
        } else {
            $property = $Value.PSObject.Properties[$part]
            if (-not $property) { return @{ present = $false; value = $null } }
            $Value = $property.Value
        }
    }
    return @{ present = ($null -ne $Value); value = $Value }
}

function Test-EquityDateReady([object]$Health, [string]$TradeDate) {
    return ((Get-ContractValue $Health 'daily_control_plane.trade_date') -eq $TradeDate -and
        (Get-ContractValue $Health 'daily_control_plane.state') -eq 'ready')
}

# Datasets that land *after* the bars the readiness gate counts.
#
# The exchange cross-section is what the 16:00 floor waits for, and
# Test-EquityDateReady already fails closed on a half-fetched one. The close
# limit pool is different: quant-service derives it from those same bars
# (settled_limit_pool_repository.persist_settled_limit_pool), so it is not
# late relative to the vendor -- it is late relative to *us*, whenever the
# limit_ladder stage ran before the settled bars arrived or came back blocked.
# Nothing in the same-date skip condition looked at it, so a run that published
# lanes over a short pool was recorded 'completed' and every later repetition
# skipped the date for the rest of the evening.
#
# The expectation therefore comes from the bars we already hold -- how many
# symbols closed at their limit -- and never from the wall clock. A pool larger
# than expected is not a defect (an upsert never deletes, so a revised limit
# price can leave an extra row behind); only a short pool means work is owed.
function Get-PostCloseLateDatasetState {
    param(
        [Parameter(Mandatory)][AllowNull()][object]$Health,
        [Parameter(Mandatory)][ValidatePattern('^\d{4}-\d{2}-\d{2}$')][string]$TradeDate
    )
    $state = @{ ready = $false; reason = $null; trade_date = $TradeDate
        expected_limit_up_symbols = $null; stored_limit_pool_symbols = $null }
    $probeSlot = Get-ContractSlot $Health 'late_datasets'
    if (-not $probeSlot.present) {
        # An absent probe is not evidence that the pool landed: a runner paired
        # with an older equity-readiness.py must keep retrying, not seal a date
        # it cannot check.
        $state['reason'] = 'late-dataset probe is absent from the readiness payload'
        return $state
    }
    $probedDate = Get-ContractValue $probeSlot.value 'trade_date'
    if ($probedDate -ne $TradeDate) {
        $state['reason'] = "late-dataset probe reports $probedDate, not the requested session"
        return $state
    }
    $expectedSlot = Get-ContractSlot $probeSlot.value 'limit_pool.expected_symbols'
    $storedSlot = Get-ContractSlot $probeSlot.value 'limit_pool.stored_symbols'
    if (-not $expectedSlot.present -or -not $storedSlot.present) {
        $state['reason'] = 'late-dataset probe carries no limit-pool counts'
        return $state
    }
    $expected = [int]$expectedSlot.value
    $stored = [int]$storedSlot.value
    $state['expected_limit_up_symbols'] = $expected
    $state['stored_limit_pool_symbols'] = $stored
    if ($expected -gt 0 -and $stored -lt $expected) {
        $state['reason'] = "close limit pool holds $stored of the $expected symbols the settled bars show closing at their limit"
        return $state
    }
    $state['ready'] = $true
    return $state
}

function Test-PostCloseLateDatasetsReady {
    param(
        [Parameter(Mandatory)][AllowNull()][object]$Health,
        [Parameter(Mandatory)][ValidatePattern('^\d{4}-\d{2}-\d{2}$')][string]$TradeDate
    )
    return [bool]((Get-PostCloseLateDatasetState -Health $Health -TradeDate $TradeDate)['ready'])
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
    $missingSlot = Get-ContractSlot $ReviewCoverage 'missing_symbols'
    $plannedSlot = Get-ContractSlot $ReviewCoverage 'planned'
    $completedSlot = Get-ContractSlot $ReviewCoverage 'completed'
    if ($missingSlot.present) { $missing = @($missingSlot.value) }
    # Unknown coverage is not evidence of completed research: fail closed. A
    # present-but-unusable projection counts as unknown too -- an absent or JSON
    # null missing_symbols, an absent planned/completed pair, or a plan whose
    # completed count falls short is exactly the {planned: 9, completed: 0}
    # shape this change exists to stop recording as a finished round.
    $due = ($null -eq $ReviewCoverage) -or (-not $missingSlot.present) -or ($missing.Count -gt 0) -or
        (-not $plannedSlot.present) -or (-not $completedSlot.present) -or
        ([int]$completedSlot.value -lt [int]$plannedSlot.value)
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
    Get-PostCloseLateDatasetState, Test-PostCloseLateDatasetsReady,
    Get-PostClosePipelineCompletedStatus, Test-PostClosePipelineCompleted,
    New-PostCloseResearchStatus, Get-PostClosePipelineStatus
