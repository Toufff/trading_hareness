[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform',
    # The tier job must run with the *published* release, not a development
    # checkout: it writes to the production database and its Python entry point
    # has to match the schema the running API expects.
    [string]$ReleaseRoot = 'G:\StockPlatform\current',
    [ValidateSet('apply', 'plan', 'status', 'install')][string]$Command = 'apply',
    # Run even inside a trading session. The task is scheduled at 06:00; this
    # only matters when Windows replays a missed run (StartWhenAvailable) or an
    # operator triggers it by hand.
    [switch]$Force,
    # Extra options forwarded to database-storage-tiers.py verbatim, e.g.
    #   run-storage-tiers.ps1 -Command plan --table quant.raw_market_observations
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$CliArguments = @()
)

# Nightly hot/cold storage tiering for the owner PostgreSQL database.
#
# Moves rows older than the hot window into the `stock_cold` tablespace on the
# G: HDD so the NVMe hot tier stays inside its 500 GB budget
# (docs/OWNER_DATABASE_STORAGE.md). The policy itself lives in
# scripts/database-storage-tiers.py; this runner only supplies a clean
# environment, a log file and a window guard.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-StorageTierRunWindowDecision {
    <#
        Pure window guard. The maintenance window is 04:00-08:00; the job must
        never compete with the exchange session for the same disk, so a replayed
        or hand-started run inside Mon-Fri 09:00-15:30 is skipped unless the
        caller passes -Force.
    #>
    param([Parameter(Mandatory)][DateTime]$Now, [bool]$Force = $false)
    if ($Force) { return 'run' }
    $minutes = $Now.Hour * 60 + $Now.Minute
    $weekday = [int]$Now.DayOfWeek -ge 1 -and [int]$Now.DayOfWeek -le 5
    if ($weekday -and $minutes -ge 540 -and $minutes -lt 930) { return 'skip_trading_session' }
    return 'run'
}

$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$release = [IO.Path]::GetFullPath($ReleaseRoot).TrimEnd('\')
if (-not (Test-Path -LiteralPath $RuntimeEnv -PathType Leaf)) { throw "Missing runtime environment file: $RuntimeEnv" }

$python = Join-Path $release '.venv\Scripts\python.exe'
$tierScript = Join-Path $release 'scripts\database-storage-tiers.py'
foreach ($path in @($python, $tierScript)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" }
}

$logDir = Join-Path $platform 'logs\storage-tiers'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ((Get-Date).ToString('yyyy-MM-dd') + '.log')

function Write-TierLog([string]$Text) {
    $line = '[' + [DateTimeOffset]::Now.ToString('o') + '] ' + $Text
    Write-Output $line
    try {
        [IO.File]::AppendAllText($logFile, $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    } catch {
        Write-Warning "Failed to write storage-tier log record: $($_.Exception.Message)"
    }
}

$decision = Get-StorageTierRunWindowDecision -Now (Get-Date) -Force:$Force.IsPresent
if ($decision -ne 'run') {
    Write-TierLog "skipped: $decision"
    exit 0
}

# The scheduled-task host inherits whatever proxy variables the interactive
# profile set; PostgreSQL is local and any proxy here only produces confusing
# timeouts (the 2026 release-health false 502 had the same root cause).
foreach ($name in 'http_proxy', 'https_proxy', 'all_proxy', 'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY') {
    Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
}
# The credentials stay in the file: database-storage-tiers.py reads --env-file
# itself, so nothing secret ever reaches this process environment, the command
# line or the log.
$arguments = @($tierScript, $Command, '--env-file', $RuntimeEnv) + $CliArguments

Write-TierLog "start: $Command (release $release)"
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'   # native stderr must be logged, not thrown
# The task host console is GBK; the CLI prints ASCII-only JSON, but a Python
# traceback can still carry UTF-8 text.
try { [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false) } catch { }
$exitCode = 1
try {
    & $python @arguments 2>&1 | ForEach-Object {
        $text = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { [string]$_ }
        Write-TierLog $text
    }
    $exitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousErrorAction
}
Write-TierLog "finished: exit $exitCode"
exit $exitCode
