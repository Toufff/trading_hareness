[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True {
    param([bool]$Condition, [string]$Message)
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

$windowsScripts = Split-Path -Parent $PSScriptRoot
$module = Join-Path $windowsScripts 'runtime-observability.psm1'
$supervisorScript = Join-Path $windowsScripts 'supervise-runtime-process.ps1'
$sandbox = Join-Path ([IO.Path]::GetTempPath()) "trading-hareness-observability-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null

try {
    Import-Module $module -Force
    [void](Write-RuntimeEvent -PlatformRoot $sandbox -Service 'unit-service' -Event 'unit_event' -RunId 'unit-run' -Data @{ value = 7 })
    $lifecycle = @(Get-ChildItem -LiteralPath (Join-Path $sandbox 'logs\runtime') -Filter 'lifecycle-*.jsonl' -File)
    Assert-True ($lifecycle.Count -eq 1) 'one daily lifecycle log must be created'
    $event = Get-Content -LiteralPath $lifecycle[0].FullName -Raw -Encoding UTF8 | ConvertFrom-Json
    Assert-True ($event.event -eq 'unit_event') 'the lifecycle event name must round-trip'
    Assert-True ($event.value -eq 7) 'structured event data must round-trip'

    $pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
    $runtimeRun = New-RuntimeRun -PlatformRoot $sandbox -Service 'exit-seven'
    $descriptor = [ordered]@{
        schema_version = 1
        service = 'exit-seven'
        run_id = $runtimeRun.RunId
        platform_root = $sandbox
        executable = $pwsh
        working_directory = $sandbox
        arguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-Command', "[Console]::Out.WriteLine('stdout-preserved'); [Console]::Error.WriteLine('stderr-preserved'); exit 7")
        stdout = $runtimeRun.Stdout
        stderr = $runtimeRun.Stderr
        stop_marker = $runtimeRun.StopMarker
        requested_at = [DateTimeOffset]::Now.ToString('o')
        metadata = @{}
    }
    [IO.File]::WriteAllText($runtimeRun.Descriptor, ($descriptor | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    $process = Start-Process -FilePath $pwsh -PassThru -WindowStyle Hidden -Wait -ArgumentList (
        "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$supervisorScript`" -DescriptorPath `"$($runtimeRun.Descriptor)`""
    )
    Assert-True ($process.ExitCode -eq 7) 'the supervisor must preserve the child exit code'
    Assert-True ((Get-Content -LiteralPath $runtimeRun.Stdout -Raw).Contains('stdout-preserved')) 'stdout must be preserved in a per-run file'
    Assert-True ((Get-Content -LiteralPath $runtimeRun.Stderr -Raw).Contains('stderr-preserved')) 'stderr must be preserved in a per-run file'
    $state = Get-RuntimeState -PlatformRoot $sandbox -Service 'exit-seven'
    Assert-True ($state.status -eq 'unexpected_exit') 'an unrequested exit must be classified as unexpected'
    Assert-True ($state.exit_code -eq 7) 'the state must retain the exit code'
    Assert-True (-not $state.expected_exit) 'the state must distinguish unexpected exits'
    [void](Request-RuntimeStop -PlatformRoot $sandbox -Service 'exit-seven' -Reason 'late_cleanup' -RequestedBy 'unit_test')
    Assert-True (-not (Test-Path -LiteralPath $runtimeRun.StopMarker)) 'cleanup after a terminal exit must not rewrite history with a late stop marker'

    $expectedRun = New-RuntimeRun -PlatformRoot $sandbox -Service 'expected-exit'
    $expectedDescriptor = [ordered]@{
        schema_version = 1
        service = 'expected-exit'
        run_id = $expectedRun.RunId
        platform_root = $sandbox
        executable = $pwsh
        working_directory = $sandbox
        arguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-Command', 'exit 0')
        stdout = $expectedRun.Stdout
        stderr = $expectedRun.Stderr
        stop_marker = $expectedRun.StopMarker
        requested_at = [DateTimeOffset]::Now.ToString('o')
        metadata = @{}
    }
    [IO.File]::WriteAllText($expectedRun.Descriptor, ($expectedDescriptor | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($expectedRun.StopMarker, '{"reason":"unit_test_stop"}', [Text.UTF8Encoding]::new($false))
    $expectedProcess = Start-Process -FilePath $pwsh -PassThru -WindowStyle Hidden -Wait -ArgumentList (
        "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$supervisorScript`" -DescriptorPath `"$($expectedRun.Descriptor)`""
    )
    Assert-True ($expectedProcess.ExitCode -eq 0) 'an expected child exit code must be preserved'
    $expectedState = Get-RuntimeState -PlatformRoot $sandbox -Service 'expected-exit'
    Assert-True ($expectedState.status -eq 'stopped') 'a marked exit must be classified as stopped'
    Assert-True ($expectedState.expected_exit) 'a marked exit must be classified as expected'

    $retainedStdout = $runtimeRun.Stdout
    $secondRun = New-RuntimeRun -PlatformRoot $sandbox -Service 'exit-seven'
    [IO.File]::WriteAllText($secondRun.Stdout, 'second run', [Text.UTF8Encoding]::new($false))
    Assert-True ((Test-Path -LiteralPath $retainedStdout -PathType Leaf)) 'starting another run must not overwrite an earlier log'
    Assert-True ($secondRun.Stdout -ne $retainedStdout) 'each run must have unique log paths'

    Assert-True ((Resolve-PostgresStartupAction -Ready $true -PgCtlStatusExitCode 3) -eq 'ready') 'ready PostgreSQL must never be started again'
    Assert-True ((Resolve-PostgresStartupAction -Ready $false -PgCtlStatusExitCode 0) -eq 'wait_for_running_server') 'a running PostgreSQL process must be awaited, not started twice'
    Assert-True ((Resolve-PostgresStartupAction -Ready $false -PgCtlStatusExitCode 3) -eq 'start_stopped_server') 'only a confirmed stopped PostgreSQL server may be started'
    Assert-True ((Resolve-PostgresStartupAction -Ready $false -PgCtlStatusExitCode 1) -eq 'fail_unknown_status') 'unknown pg_ctl states must fail closed'
    Assert-True ((Assert-ReservedRemoteTunnelPort -Port 15680) -eq 15680) 'the dashboard tunnel port must be accepted'
    $unsafePortRejected = $false
    try { [void](Assert-ReservedRemoteTunnelPort -Port 22) } catch { $unsafePortRejected = $true }
    Assert-True $unsafePortRejected 'remote cleanup must reject ports outside the explicit allowlist'

    [pscustomobject]@{
        passed = $true
        lifecycle = $lifecycle[0].FullName
        captured_exit_code = $state.exit_code
        unexpected_exit_classified = $state.status
        expected_exit_classified = $expectedState.status
        late_stop_marker_rejected = -not (Test-Path -LiteralPath $runtimeRun.StopMarker)
        old_log_retained = (Test-Path -LiteralPath $retainedStdout)
        postgres_state_machine = $true
        unsafe_remote_cleanup_rejected = $unsafePortRejected
    }
} finally {
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}
