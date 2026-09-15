param([string]$EvidenceRoot = 'G:\StockPlatform\logs\runtime\acceptance')
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
Import-Module (Join-Path $repo 'scripts\windows\runtime-observability.psm1') -Force
Import-Module (Join-Path $repo 'scripts\windows\background-process.psm1') -Force
New-Item -ItemType Directory -Path $EvidenceRoot -Force | Out-Null
$id = 'silent-task-' + [guid]::NewGuid().ToString('N')
$sandbox = Join-Path $EvidenceRoot $id
New-Item -ItemType Directory -Path $sandbox -Force | Out-Null
$taskName = 'trading-hareness-acceptance-' + $id
$auditFile = Join-Path $sandbox 'windows.json'
$ready = Join-Path $sandbox 'audit.ready'
$audit = [Diagnostics.Process]::Start((New-ConsoleFreeStartInfo -FilePath (Get-Command pwsh.exe).Source -Arguments @(
    '-NoProfile', '-File', (Join-Path $PSScriptRoot 'measure-background-windows.ps1'), '-Seconds', '105', '-OutputPath', $auditFile, '-ReadyPath', $ready)))
try {
    $deadline=(Get-Date).AddSeconds(10)
    while (-not (Test-Path $ready) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 100 }
    if (-not (Test-Path $ready)) { throw 'Window audit not ready' }
    $action = New-HiddenPowerShellTaskAction -RepositoryRoot $repo -ScriptPath (Join-Path $PSScriptRoot 'runtime-owner-fixture.ps1') `
        -ScriptArguments @('-PlatformRoot', $sandbox, '-RepositoryRoot', $repo)
    $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddSeconds(3) -RepetitionInterval (New-TimeSpan -Minutes 1) -RepetitionDuration (New-TimeSpan -Seconds 70)
    $trigger.Repetition.StopAtDurationEnd = $false
    $principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
    # Pass both a repetition tick and its end boundary with the same process.
    $deadline=(Get-Date).AddSeconds(15)
    do { $state=Get-RuntimeState -PlatformRoot $sandbox -Service 'owner-test'; if($state -and $state.status -eq 'process_started'){break};Start-Sleep -Milliseconds 100 } while((Get-Date) -lt $deadline)
    if (-not $state -or $state.status -ne 'process_started') { throw 'Actual scheduled task failed to launch fixture' }
    $firstRun=$state.run_id
    Start-Sleep -Seconds 40
    Start-ScheduledTask -TaskName $taskName # Explicit duplicate trigger.
    Start-Sleep -Seconds 40
    $after=Get-RuntimeState -PlatformRoot $sandbox -Service 'owner-test'
    if($after.run_id -ne $firstRun -or (Get-ScheduledTask -TaskName $taskName).State -ne 'Running'){throw 'Boundary/duplicate trigger terminated or replaced the task'}
    Stop-ScheduledTask -TaskName $taskName
    $deadline=(Get-Date).AddSeconds(10)
    do {$child=Get-Process -Id $state.launcher_pid -ErrorAction SilentlyContinue; if(-not $child){break};Start-Sleep -Milliseconds 200}while((Get-Date) -lt $deadline)
    if($child){throw 'Stopping the actual task orphaned its child'}
    # Native GUI host error-path must fail without a dialog, too.
    $bad=Invoke-ConsoleFreeCommand -FilePath $action.Execute -Arguments @('Z:\missing-stock-executable.exe', (Join-Path $PSScriptRoot 'runtime-owner-fixture.ps1')) -TimeoutSeconds 5
    if($bad.ExitCode -eq 0){throw 'Missing executable reported success'}
    $audit.WaitForExit()
    $windows=Get-Content $auditFile -Raw|ConvertFrom-Json
    if($windows.console_events -gt 0){throw "Console show/focus events observed; inspect $auditFile"}
    $result=[ordered]@{passed=$true;actual_task_host='stock-background-host.exe';duplicate_trigger_preserved_run=$true;repetition_boundary_survived=$true;task_stop_reaped_child=$true;error_path_no_dialog=$true;console_events=0;audit=$auditFile}
    [IO.File]::WriteAllText((Join-Path $sandbox 'result.json'), ($result|ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    [pscustomobject]$result
} finally {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction SilentlyContinue
    [void](Request-RuntimeStop -PlatformRoot $sandbox -Service 'owner-test' -Reason 'test_cleanup')
    if(-not $audit.HasExited){
        [IO.File]::WriteAllText($ready + '.stop', 'stop')
        if(-not $audit.WaitForExit(10000)){$audit.Kill()}
    }
}
