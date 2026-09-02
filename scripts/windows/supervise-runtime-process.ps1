[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$DescriptorPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$module = Join-Path $PSScriptRoot 'runtime-observability.psm1'
Import-Module $module -Force

$descriptor = Get-Content -LiteralPath $DescriptorPath -Raw -Encoding UTF8 | ConvertFrom-Json
$platformRoot = [string]$descriptor.platform_root
$service = [string]$descriptor.service
$runId = [string]$descriptor.run_id
$startedAt = [DateTimeOffset]::Now
$child = $null

try {
    $arguments = @($descriptor.arguments | ForEach-Object { [string]$_ })
    $child = Start-Process -FilePath ([string]$descriptor.executable) -PassThru -WindowStyle Hidden `
        -WorkingDirectory ([string]$descriptor.working_directory) -ArgumentList $arguments `
        -RedirectStandardOutput ([string]$descriptor.stdout) -RedirectStandardError ([string]$descriptor.stderr)
    [void](Set-RuntimeState -PlatformRoot $platformRoot -Service $service -State @{
        status = 'process_started'
        run_id = $runId
        supervisor_pid = $PID
        launcher_pid = $child.Id
        descriptor = $DescriptorPath
        stdout = [string]$descriptor.stdout
        stderr = [string]$descriptor.stderr
        started_at = $startedAt.ToString('o')
    })
    [void](Write-RuntimeEvent -PlatformRoot $platformRoot -Service $service -Event 'process_started' -RunId $runId -Data @{
        supervisor_pid = $PID
        launcher_pid = $child.Id
    })
    $child.WaitForExit()
    $exitCode = $child.ExitCode
    $endedAt = [DateTimeOffset]::Now
    $expected = Test-Path -LiteralPath ([string]$descriptor.stop_marker) -PathType Leaf
    $stopReason = if ($expected) {
        try { [string]((Get-Content -LiteralPath ([string]$descriptor.stop_marker) -Raw -Encoding UTF8 | ConvertFrom-Json).reason) }
        catch { 'stop_marker_present' }
    } else { 'process_exited_without_stop_request' }
    $status = if ($expected) { 'stopped' } else { 'unexpected_exit' }
    [void](Set-RuntimeState -PlatformRoot $platformRoot -Service $service -State @{
        status = $status
        run_id = $runId
        supervisor_pid = $PID
        launcher_pid = $child.Id
        exit_code = $exitCode
        expected_exit = $expected
        reason = $stopReason
        descriptor = $DescriptorPath
        stdout = [string]$descriptor.stdout
        stderr = [string]$descriptor.stderr
        started_at = $startedAt.ToString('o')
        ended_at = $endedAt.ToString('o')
        runtime_ms = [int64]($endedAt - $startedAt).TotalMilliseconds
    })
    [void](Write-RuntimeEvent -PlatformRoot $platformRoot -Service $service -Event 'process_exited' -RunId $runId `
        -Level $(if ($expected) { 'info' } else { 'error' }) -Data @{
            launcher_pid = $child.Id
            exit_code = $exitCode
            expected_exit = $expected
            reason = $stopReason
            runtime_ms = [int64]($endedAt - $startedAt).TotalMilliseconds
            stderr = [string]$descriptor.stderr
        })
    exit $exitCode
} catch {
    $endedAt = [DateTimeOffset]::Now
    [void](Set-RuntimeState -PlatformRoot $platformRoot -Service $service -State @{
        status = 'supervisor_failed'
        run_id = $runId
        supervisor_pid = $PID
        launcher_pid = if ($child) { $child.Id } else { $null }
        reason = $_.Exception.Message
        descriptor = $DescriptorPath
        stdout = [string]$descriptor.stdout
        stderr = [string]$descriptor.stderr
        started_at = $startedAt.ToString('o')
        ended_at = $endedAt.ToString('o')
    })
    [void](Write-RuntimeEvent -PlatformRoot $platformRoot -Service $service -Event 'supervisor_failed' -RunId $runId -Level 'error' -Data @{
        error_type = $_.Exception.GetType().FullName
        message = $_.Exception.Message
    })
    exit 125
}
