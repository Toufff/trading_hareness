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

Export-ModuleMember -Function Get-ContractValue, Test-EquityDateReady
