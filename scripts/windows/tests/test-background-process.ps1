$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$scripts = Split-Path -Parent $PSScriptRoot
Import-Module (Join-Path $scripts 'background-process.psm1') -Force
function Assert-True([bool]$Value, [string]$Message) { if (-not $Value) { throw $Message } }
$pwsh = (Get-Command pwsh.exe).Source
$info = New-ConsoleFreeStartInfo -FilePath $pwsh -Arguments @('-NoProfile', '-Command', 'exit 0')
Assert-True $info.CreateNoWindow 'Background processes must use CREATE_NO_WINDOW'
Assert-True (-not $info.UseShellExecute) 'Shell execution must be disabled'
$result = Invoke-ConsoleFreeCommand -FilePath $pwsh -Arguments @('-NoProfile', '-Command', "[Console]::OutputEncoding=[Text.UTF8Encoding]::new(`$false); [Console]::Out.Write('space and 中文'); [Console]::Error.Write('error preserved'); exit 7")
Assert-True ($result.ExitCode -eq 7) 'Exit code lost'
Assert-True ($result.Stdout -eq 'space and 中文') 'Argument boundaries or stdout lost'
Assert-True ($result.Stderr -eq 'error preserved') 'Stderr lost'
$timedOut = $false
try { Invoke-ConsoleFreeCommand -FilePath $pwsh -Arguments @('-NoProfile', '-Command', 'Start-Sleep 30') -TimeoutSeconds 1 | Out-Null }
catch { $timedOut = $_.Exception.Message -match 'timed out' }
Assert-True $timedOut 'A hung subprocess must have a bounded timeout'
$python = Join-Path $scripts '..\..\.venv\Scripts\python.exe'
$timer = [Diagnostics.Stopwatch]::StartNew()
$drain = Invoke-ConsoleFreeCommand -FilePath $python -Arguments @('-c', 'import subprocess,sys; subprocess.Popen([sys.executable,"-c","import time; time.sleep(8)"]); print("parent exited",flush=True)') -TimeoutSeconds 2
Assert-True ($timer.Elapsed.TotalSeconds -lt 6) 'Exited parent with inherited output handles must not hang the watchdog'
Assert-True ($drain.OutputComplete -eq $false) 'An inherited pipe timeout must be explicit'
$action = New-HiddenPowerShellTaskAction -RepositoryRoot ([IO.Path]::GetFullPath((Join-Path $scripts '..\..'))) -ScriptPath (Join-Path $scripts 'watch-stock-dashboard.ps1') -ScriptArguments @('-PlatformRoot', 'G:\Stock Platform')
Assert-True ($action.Execute -match 'stock-background-host.exe$') 'The scheduler must launch the native GUI host'
Assert-True ($action.Arguments -notmatch 'run-hidden.vbs|run-background-task.ps1') 'No hidden-style console launcher may remain in the task entry chain'
Assert-True ($action.Arguments -notmatch 'codex-runtimes') 'Production tasks must not depend on the assistant runtime cache'
$installer = Get-Content (Join-Path $scripts '..\shared-peer\install-shared-tunnel-task.ps1') -Raw
Assert-True ($installer -match 'StopAtDurationEnd\s*=\s*\$false') 'A repetition boundary must not terminate a live tunnel'
Assert-True ($installer -notmatch 'Stop-Process -Id \(\[int\]\$state\.launcher_pid\)') 'A stale state PID must not kill an unrelated reused PID'
foreach($name in @('start-stock-platform.ps1','start-stock-dashboard.ps1')) {
    $source=Get-Content (Join-Path $scripts $name) -Raw
    Assert-True ($source -notmatch '(?m)^\s*&\s+(\$pg\w+|\$python|ssh(?:\.exe)?)\b|\(&\s+ssh') "Bare native command can allocate a console in $name"
}
[pscustomobject]@{passed=$true;no_console=$true;argument_roundtrip=$true;timeout=$timedOut;gui_task_host=$true;no_boundary_kill=$true}
