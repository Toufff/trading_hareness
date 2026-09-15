[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$DescriptorPath
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$module = Join-Path $PSScriptRoot 'runtime-observability.psm1'
Import-Module $module -Force
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force

$descriptor = Get-Content -LiteralPath $DescriptorPath -Raw -Encoding UTF8 | ConvertFrom-Json
$platformRoot = [string]$descriptor.platform_root
$service = [string]$descriptor.service
$runId = [string]$descriptor.run_id
$startedAt = [DateTimeOffset]::Now
$child = $null
$lock = $null
$streams = @()
$copies = @()
$exitCode = 125
$lifetime = $null

# OS-owned cross-process lock: duplicates cannot start a child or replace the
# active run's state. File existence is irrelevant; the open handle is the lock.
$lockPath = Join-Path (Get-RuntimeLogRoot -PlatformRoot $platformRoot) "$service.lock"
try { $lock = [IO.File]::Open($lockPath, 'OpenOrCreate', 'ReadWrite', 'None') }
catch [IO.IOException] {
    [void](Write-RuntimeEvent -PlatformRoot $platformRoot -Service $service -Event 'duplicate_start_skipped' -RunId $runId)
    exit 0
}

try {
    if (-not ('StockRuntime.ProcessLifetime' -as [type])) { Add-Type -Path (Join-Path $PSScriptRoot 'process-lifetime.cs') }
    $lifetime = [StockRuntime.ProcessLifetime]::new()
    $arguments = @($descriptor.arguments | ForEach-Object { [string]$_ })
    $info = New-ConsoleFreeStartInfo -FilePath ([string]$descriptor.executable) `
        -WorkingDirectory ([string]$descriptor.working_directory) -Arguments $arguments
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $streams = @([IO.File]::Open([string]$descriptor.stdout, 'Create', 'Write', 'ReadWrite'),
                 [IO.File]::Open([string]$descriptor.stderr, 'Create', 'Write', 'ReadWrite'))
    $child = [Diagnostics.Process]::Start($info)
    $lifetime.Attach($child)
    $copies = @($child.StandardOutput.BaseStream.CopyToAsync($streams[0]), $child.StandardError.BaseStream.CopyToAsync($streams[1]))
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
    $ownerBound = $descriptor.metadata.PSObject.Properties['stop_with_owner'] -and $descriptor.metadata.stop_with_owner
    while (-not $child.WaitForExit(500)) {
        if ($ownerBound) {
            $owner = Get-Process -Id ([int]$descriptor.owner_pid) -ErrorAction SilentlyContinue
            if (-not $owner -or $owner.StartTime.ToUniversalTime() -ne ([datetime]$descriptor.owner_started_at).ToUniversalTime()) {
                [void](Request-RuntimeStop -PlatformRoot $platformRoot -Service $service -Reason 'owner_process_exited' -RequestedBy 'supervisor')
                $child.Kill($true)
                $child.WaitForExit()
            }
        }
        if ((Test-Path -LiteralPath ([string]$descriptor.stop_marker)) -and -not $child.HasExited) {
            $child.Kill($true)
            $child.WaitForExit()
        }
    }
    foreach ($copy in $copies) { $copy.GetAwaiter().GetResult() }
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
    $exitCode = 125
} finally {
    if ($child -and -not $child.HasExited) { $child.Kill($true); $child.WaitForExit() }
    foreach ($copy in $copies) { try { $copy.GetAwaiter().GetResult() } catch {} }
    foreach ($stream in $streams) { $stream.Dispose() }
    if ($child) { $child.Dispose() }
    if ($lifetime) { $lifetime.Dispose() }
    if ($lock) { $lock.Dispose() }
}
exit $exitCode
