[CmdletBinding()]
param(
    [ValidateSet('DeepSeek', 'Codex')]
    [string]$Provider = 'DeepSeek',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RepositoryRoot = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $RepositoryRoot) { $RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
$python = Join-Path $RepositoryRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { $python = (Get-Command python.exe -ErrorAction Stop).Source }
$envPath = Join-Path ([IO.Path]::GetFullPath($PlatformRoot)) 'config\runtime.env'
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) { throw "Missing private runtime env: $envPath" }
$script = Join-Path $RepositoryRoot 'deploy\intraday-edge\advisory_model_preflight.py'

$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $python
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.Environment['PYTHONPATH'] = Join-Path $RepositoryRoot 'quant-service'
foreach ($line in [IO.File]::ReadAllLines($envPath, [Text.Encoding]::UTF8)) {
    if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
    $parts = $line.Split('=', 2)
    $start.Environment[$parts[0]] = $parts[1]
}
[void]$start.ArgumentList.Add($script)
[void]$start.ArgumentList.Add('--provider')
[void]$start.ArgumentList.Add($Provider.ToLowerInvariant())
$process = [Diagnostics.Process]::new()
$process.StartInfo = $start
if (-not $process.Start()) { throw 'Failed to start advisory model preflight' }
$stdout = $process.StandardOutput.ReadToEnd().Trim()
$stderr = $process.StandardError.ReadToEnd().Trim()
if (-not $process.WaitForExit(300000)) { $process.Kill($true); throw 'Advisory model preflight timed out' }
if ($process.ExitCode -ne 0) { throw "Advisory model preflight failed: $stderr" }
$stdout
