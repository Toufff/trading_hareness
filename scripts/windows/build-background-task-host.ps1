$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSScriptRoot 'background-process.psm1') -Force
$compiler=Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if(-not (Test-Path $compiler)){throw 'Windows .NET Framework C# compiler is required for the console-free task host'}
$bin=Join-Path $PSScriptRoot 'bin'
New-Item -ItemType Directory -Path $bin -Force | Out-Null
$output=Join-Path $bin 'stock-background-host.exe'
$sources=@((Join-Path $PSScriptRoot 'process-lifetime.cs'),(Join-Path $PSScriptRoot 'background-task-host.cs'))
$r=Invoke-ConsoleFreeCommand -FilePath $compiler -Arguments (@('/nologo','/target:winexe','/optimize+',('/out:'+$output))+$sources) -TimeoutSeconds 30
if($r.ExitCode -ne 0){throw "Task host compilation failed: $($r.Stdout) $($r.Stderr)"}
$bytes=[IO.File]::ReadAllBytes($output)
$pe=[BitConverter]::ToInt32($bytes,0x3c)
if([BitConverter]::ToUInt16($bytes,$pe+24+68) -ne 2){throw 'Task host must be a Windows GUI executable, not a console executable'}
# Build receipt for the shared-peer tunnel reinstall gate.
#
# The executable itself cannot be hashed: csc.exe embeds a fresh module GUID on
# every build, so two builds of identical sources differ byte for byte and the
# gate would reinstall the tunnel on every publish. Bare presence is too weak in
# the other direction: with -SkipTests this script never runs, while the publish
# still copies whatever sits in the source tree's bin, so a truncated or stale
# executable would pass as "present". This receipt is deterministic for
# deterministic sources (input SHA-256s plus the output's length), so the gate
# hashes it instead and checks the recorded length against the file on disk.
$receiptPath=Join-Path $bin 'stock-background-host.build.json'
$inputs=[ordered]@{}
foreach($sourceFile in ($sources|Sort-Object)){
    $inputs[[IO.Path]::GetFileName($sourceFile)]=(Get-FileHash -LiteralPath $sourceFile -Algorithm SHA256).Hash.ToLowerInvariant()
}
$receipt=[ordered]@{
    schema_version=1
    output_name=[IO.Path]::GetFileName($output)
    output_length=[int64]$bytes.LongLength
    inputs=$inputs
}
[IO.File]::WriteAllText($receiptPath,($receipt|ConvertTo-Json -Depth 5),[Text.UTF8Encoding]::new($false))
[pscustomobject]@{path=$output;subsystem='Windows GUI';sha256=(Get-FileHash $output).Hash.ToLowerInvariant();receipt=$receiptPath}
