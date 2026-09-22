[CmdletBinding()]
param(
    [ValidateSet('Auto','PreOpen','Live')][string]$Stage = 'Auto',
    [string]$RepositoryRoot = 'G:\StockPlatform\current',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RuntimeEnv = '',
    [string]$BaseUrl = 'http://127.0.0.1:5681',
    [string]$AsOf = ''
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = [IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')
$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$RuntimeEnv = if ($RuntimeEnv) { $RuntimeEnv } else { Join-Path $platform 'config\runtime.env' }
$python = Join-Path $root '.venv\Scripts\python.exe'
$cli = Join-Path $root 'scripts\intraday-opening-guard.py'
foreach ($path in @($RuntimeEnv, $python, $cli)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing opening-guard dependency: $path" }
}

$effectiveNow = if ($AsOf) { [DateTimeOffset]::Parse($AsOf) } else { [DateTimeOffset]::Now }
$resolvedStage = if ($Stage -eq 'Auto') {
    if ($effectiveNow.TimeOfDay -lt [TimeSpan]::FromHours(9.5)) { 'preopen' } else { 'live' }
} else { $Stage.ToLowerInvariant() }

$logRoot = Join-Path $platform 'logs\runtime'
New-Item -ItemType Directory -Force -Path $logRoot | Out-Null
$logPath = Join-Path $logRoot 'intraday-opening-guard.jsonl'

function Invoke-Guard([bool]$Notify, [bool]$RecoveryAttempted) {
    $arguments = @($cli, '--stage', $resolvedStage, '--base-url', $BaseUrl, '--env-file', $RuntimeEnv)
    if ($AsOf) { $arguments += @('--as-of', $AsOf) }
    if ($Notify) { $arguments += '--notify' }
    if ($RecoveryAttempted) { $arguments += '--recovery-attempted' }
    $output = & $python @arguments 2>&1
    $exitCode = $LASTEXITCODE
    foreach ($line in @($output)) {
        [IO.File]::AppendAllText($logPath, ([string]$line).Trim() + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    }
    return [pscustomobject]@{ ExitCode = $exitCode; Output = ($output -join [Environment]::NewLine) }
}

# First pass is silent.  A failure receives exactly one bounded runtime restart,
# then the final pass only notifies an unrecovered fault or a previously
# announced fault's recovery. Successful routine checks remain silent.
$first = Invoke-Guard -Notify $false -RecoveryAttempted $false
$recoveryAttempted = $false
if ($first.ExitCode -ne 0) {
    $recoveryAttempted = $true
    Stop-ScheduledTask -TaskName 'trading-hareness-dashboard-runtime' -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName 'trading-hareness-dashboard-runtime' -ErrorAction Stop
    Start-Sleep -Seconds 20
}
$final = Invoke-Guard -Notify $true -RecoveryAttempted $recoveryAttempted
if ($final.Output) { Write-Output $final.Output }
exit $final.ExitCode
