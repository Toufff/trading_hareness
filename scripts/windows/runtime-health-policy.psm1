function Resolve-LiveRuntimeState {
    param([string]$RecordedStatus, [bool]$SupervisorAlive, [AllowNull()][object]$Reachable)
    if ($null -ne $Reachable -and -not [bool]$Reachable) { return 'unavailable' }
    if (-not $SupervisorAlive) {
        if ($Reachable) { return 'unsupervised' }
        return 'not_running'
    }
    if ($Reachable) { return 'healthy' }
    # SSH process existence alone cannot establish remote forwarding health.
    return 'unverified'
}
Export-ModuleMember -Function Resolve-LiveRuntimeState
