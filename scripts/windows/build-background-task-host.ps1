$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
$compiler=Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if(-not (Test-Path $compiler)){throw 'Windows .NET Framework C# compiler is required for the console-free task host'}
$bin=Join-Path $PSScriptRoot 'bin'
New-Item -ItemType Directory -Path $bin -Force | Out-Null
$output=Join-Path $bin 'stock-background-host.exe'
$r=Invoke-ConsoleFreeCommand -FilePath $compiler -Arguments @('/nologo','/target:winexe','/optimize+',('/out:'+$output),
    (Join-Path $PSScriptRoot 'process-lifetime.cs'),(Join-Path $PSScriptRoot 'background-task-host.cs')) -TimeoutSeconds 30
if($r.ExitCode -ne 0){throw "Task host compilation failed: $($r.Stdout) $($r.Stderr)"}
$bytes=[IO.File]::ReadAllBytes($output)
$pe=[BitConverter]::ToInt32($bytes,0x3c)
if([BitConverter]::ToUInt16($bytes,$pe+24+68) -ne 2){throw 'Task host must be a Windows GUI executable, not a console executable'}
[pscustomobject]@{path=$output;subsystem='Windows GUI';sha256=(Get-FileHash $output).Hash.ToLowerInvariant()}
