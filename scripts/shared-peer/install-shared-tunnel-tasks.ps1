param(
    [string]$ScriptPath = (Join-Path $PSScriptRoot "start-shared-tunnels.ps1"),
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$SshAlias = 'lightServer1',
    [int]$RemoteDatabasePort = 15432,
    [int]$RemoteApiPort = 15681,
    [int]$RemoteBatchDatabasePort = 15433,
    [int]$LocalDatabasePort = 55432,
    [int]$LocalApiPort = 5681,
    [ValidateSet('S4U', 'Interactive')][string]$LogonType = 'S4U',
    # The batch tunnel is a throughput optimization, not a dependency of the
    # platform: intraday traffic, the dashboard and the peer gateway all work
    # without it. A failure to bring it up is therefore reported and recorded,
    # not raised, so it can never turn a good release into a failed one. Pass
    # -RequireBatch when the caller genuinely needs both (an acceptance run).
    [switch]$RequireBatch,
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$installer = Join-Path $PSScriptRoot 'install-shared-tunnel-task.ps1'
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\runtime-observability.psm1') -Force

$common = @{
    ScriptPath = $ScriptPath
    PlatformRoot = $PlatformRoot
    SshAlias = $SshAlias
    RemoteDatabasePort = $RemoteDatabasePort
    RemoteApiPort = $RemoteApiPort
    RemoteBatchDatabasePort = $RemoteBatchDatabasePort
    LocalDatabasePort = $LocalDatabasePort
    LocalApiPort = $LocalApiPort
    LogonType = $LogonType
}
if ($WhatIf) { $common['WhatIf'] = $true }

# Intraday first and unguarded: it is the connection everything else depends
# on, so its failure must still surface exactly as it did when this was the
# only task.
$intraday = & $installer -Profile intraday @common

$batch = $null
$batchError = $null
try {
    $batch = & $installer -Profile batch @common
} catch {
    $batchError = $_.Exception.Message
    if ($RequireBatch) { throw }
    Write-Warning "Batch tunnel install failed; intraday traffic is unaffected. $batchError"
    if (-not $WhatIf) {
        [void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service 'shared-peer-batch-tunnel' `
            -Event 'install_failed' -Level 'error' -Data @{
                error = $batchError
                required = [bool]$RequireBatch
            })
    }
}

[pscustomobject][ordered]@{
    intraday = $intraday
    batch = $batch
    batch_error = $batchError
    batch_required = [bool]$RequireBatch
}
