$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
Import-Module (Join-Path $repo 'scripts\windows\runtime-observability.psm1') -Force
Import-Module (Join-Path $repo 'scripts\windows\background-process.psm1') -Force
$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('stock-isolation-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $sandbox | Out-Null
function Require([bool]$Value, [string]$Message) { if (-not $Value) { throw $Message } }
function Await-State([string]$Service, [string[]]$Status, [string]$RunId = '') {
    $end = (Get-Date).AddSeconds(12)
    do {
        $s = Get-RuntimeState -PlatformRoot $sandbox -Service $Service
        # A new supervisor's state file can be absent during atomic replacement.
        # Wait for the requested generation; never dereference a transient null
        # or accept the previous generation's process_started state.
        if ($s -and $s.PSObject.Properties['status'] -and $s.status -in $Status -and
            (-not $RunId -or ($s.PSObject.Properties['run_id'] -and $s.run_id -eq $RunId))) { return $s }
        Start-Sleep -Milliseconds 100
    } while ((Get-Date) -lt $end)
    throw "Timed out awaiting $Service : $Status"
}
$processes = [Collections.Generic.List[int]]::new()
try {
    $args = @{PlatformRoot=$sandbox; RepositoryRoot=$repo; Service='singleton-test'; Executable=(Get-Command pwsh.exe).Source;
        WorkingDirectory=$sandbox; Arguments=@('-NoProfile', '-Command', 'Start-Sleep 60')}
    $run = Start-RuntimeSupervisor @args
    $processes.Add($run.supervisor_pid)
    $s = Await-State 'singleton-test' @('process_started')
    $processes.Add($s.launcher_pid)
    $duplicate = Start-RuntimeSupervisor @args
    $processes.Add($duplicate.supervisor_pid)
    Wait-Process -Id $duplicate.supervisor_pid -Timeout 10 -ErrorAction SilentlyContinue
    $after = Get-RuntimeState -PlatformRoot $sandbox -Service 'singleton-test'
    Require ($s.run_id -eq $after.run_id -and $s.launcher_pid -eq $after.launcher_pid) 'Duplicate launch clobbered the active runtime'
    [void](Request-RuntimeStop -PlatformRoot $sandbox -Service 'singleton-test' -Reason 'test_stop')
    $stopped = Await-State 'singleton-test' @('stopped')
    Require ($null -eq (Get-Process -Id $s.launcher_pid -ErrorAction SilentlyContinue)) 'Stop marker left the child alive'

    $crashRun = Start-RuntimeSupervisor @args
    $processes.Add($crashRun.supervisor_pid)
    $crash=Await-State 'singleton-test' @('process_started') $crashRun.run_id
    Require ($crash.run_id -eq $crashRun.run_id -and $crash.status -eq 'process_started') 'Replacement did not start'
    $processes.Add($crash.launcher_pid)
    Stop-Process -Id $crash.supervisor_pid -Force
    $deadline=(Get-Date).AddSeconds(5)
    do {$orphan=Get-Process -Id $crash.launcher_pid -ErrorAction SilentlyContinue;if(-not $orphan){break};Start-Sleep -Milliseconds 100}while((Get-Date) -lt $deadline)
    Require (-not $orphan) 'Force-killing supervisor orphaned the child (job object broken)'

    $owner = [Diagnostics.Process]::Start((New-ConsoleFreeStartInfo -FilePath (Get-Command pwsh.exe).Source `
        -Arguments @('-NoProfile', '-File', (Join-Path $PSScriptRoot 'runtime-owner-fixture.ps1'), '-PlatformRoot', $sandbox, '-RepositoryRoot', $repo)))
    $processes.Add($owner.Id)
    $owned = Await-State 'owner-test' @('process_started')
    $processes.Add($owned.supervisor_pid)
    $processes.Add($owned.launcher_pid)
    $owner.Kill() # Reproduce the real incident: only the scheduled parent dies.
    $owner.WaitForExit()
    $deadOwner = Await-State 'owner-test' @('stopped')
    Require ($deadOwner.reason -eq 'owner_process_exited') 'Owner loss was not correctly classified'
    Require ($null -eq (Get-Process -Id $owned.launcher_pid -ErrorAction SilentlyContinue)) 'Owner loss orphaned its child'
    [pscustomobject]@{passed=$true;duplicate_suppressed=$true;active_state_preserved=$true;stop_marker_works=$true;owner_death_reaped_child=$true;supervisor_death_reaped_child=$true}
} finally {
    foreach ($id in $processes) { Stop-Process -Id $id -Force -ErrorAction SilentlyContinue }
    $resolved = [IO.Path]::GetFullPath($sandbox)
    $tempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\')
    if ($resolved.StartsWith($tempRoot + '\stock-isolation-', [StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
