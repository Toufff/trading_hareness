[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ReleaseId,
    [string]$PlatformRoot = 'G:\StockPlatform'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\stock-release-management.psm1') -Force
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$layout = Get-StockReleaseLayout -PlatformRoot $platform
$target = Get-StockReleaseAppPath -PlatformRoot $platform -ReleaseId $ReleaseId
if (-not (Test-Path -LiteralPath $target -PathType Container)) { throw "Unknown release: $ReleaseId" }
$state = Get-StockReleaseState -PlatformRoot $platform
$oldRelease = if ($state.PSObject.Properties['active_release']) { [string]$state.active_release } else { '' }
$oldTarget = Get-StockCurrentReleaseTarget -PlatformRoot $platform
if ($oldRelease -eq $ReleaseId) { [pscustomobject]@{ status = 'already_active'; release_id = $ReleaseId; target = $target }; return }

try {
    Stop-ScheduledTask -TaskName 'trading-hareness-shared-peer-tunnels' -ErrorAction SilentlyContinue
    Stop-ScheduledTask -TaskName 'trading-hareness-dashboard-runtime' -ErrorAction SilentlyContinue
    if ($oldTarget) { & (Join-Path $oldTarget 'scripts\windows\stop-stock-dashboard.ps1') -PlatformRoot $platform | Out-Null }
    [void](Set-StockCurrentRelease -PlatformRoot $platform -ReleaseId $ReleaseId)
    & (Join-Path $layout.CurrentPath 'scripts\windows\install-stock-dashboard-task.ps1') -RepositoryRoot $layout.CurrentPath -PlatformRoot $platform | Out-Null
    & (Join-Path $layout.CurrentPath 'scripts\shared-peer\install-shared-tunnel-task.ps1') -ScriptPath (Join-Path $layout.CurrentPath 'scripts\shared-peer\start-shared-tunnels.ps1') -PlatformRoot $platform | Out-Null
    $deadline = [DateTime]::UtcNow.AddSeconds(150)
    do {
        Start-Sleep -Seconds 2
        try {
            $api = Invoke-RestMethod 'http://127.0.0.1:5681/health' -TimeoutSec 3
            $adapter = Invoke-RestMethod 'http://127.0.0.1:5680/health' -TimeoutSec 3
            if ($api.status -eq 'ok' -and $adapter.status -eq 'ok') { break }
        } catch { }
    } while ([DateTime]::UtcNow -lt $deadline)
    if ([DateTime]::UtcNow -ge $deadline) { throw 'Rollback target did not become healthy' }
    & (Join-Path $layout.CurrentPath 'scripts\shared-peer\verify-shared-runtime.ps1') | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Rollback target failed shared runtime verification' }
    [void](Set-StockReleaseState -PlatformRoot $platform -State @{
        active_release = $ReleaseId
        previous_release = if ($oldRelease) { $oldRelease } else { $null }
        last_verification = @{ verified_at = [DateTimeOffset]::Now.ToString('o'); result = 'verified_after_switch' }
        last_failed_release = $null
    })
    [pscustomobject]@{ status = 'switched'; release_id = $ReleaseId; previous_release = $oldRelease; target = $target }
} catch {
    if ($oldRelease -and (Test-Path -LiteralPath (Get-StockReleaseAppPath -PlatformRoot $platform -ReleaseId $oldRelease) -PathType Container)) {
        [void](Set-StockCurrentRelease -PlatformRoot $platform -ReleaseId $oldRelease)
        & (Join-Path $layout.CurrentPath 'scripts\windows\install-stock-dashboard-task.ps1') -RepositoryRoot $layout.CurrentPath -PlatformRoot $platform | Out-Null
        & (Join-Path $layout.CurrentPath 'scripts\shared-peer\install-shared-tunnel-task.ps1') -ScriptPath (Join-Path $layout.CurrentPath 'scripts\shared-peer\start-shared-tunnels.ps1') -PlatformRoot $platform | Out-Null
    }
    throw
}
