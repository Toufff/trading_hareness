[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform',
    # The tier job must run with the *published* release, not a development
    # checkout: it writes to the production database and its Python entry point
    # has to match the schema the running API expects.
    [string]$ReleaseRoot = 'G:\StockPlatform\current',
    [ValidateSet('apply', 'plan', 'status', 'install')][string]$Command = 'apply',
    # Hard stop for the nightly move, as a local HH:MM wall clock. The job
    # checks it between batches and between tables, finishes the batch in
    # flight, writes its receipt with status 'deadline_reached' and exits 0; the
    # move is idempotent and resumable, so the next night continues. Without it
    # a year-long first migration keeps issuing large DELETE/INSERT batches and
    # VACUUMs straight into the 09:30 opening auction until Task Scheduler kills
    # the process -- and a killed process writes no receipt at all.
    [string]$Deadline = '08:00',
    # Belt and braces for a run that starts late (StartWhenAvailable replays a
    # missed 06:00 trigger) or crosses midnight: never run longer than this
    # regardless of the wall clock. 06:00 -> 08:00 is two hours; the scheduled
    # task's ExecutionTimeLimit is 2h15m, so the job always stops itself first.
    [int]$MaxSeconds = 7200,
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

function Get-StorageTierHotDays {
    <#
        The tier hot window, taken from runtime.env rather than from the CLI's
        own default.

        Two jobs read STORAGE_TIER_HOT_DAYS and they must read the same number:
        this one decides which rows leave the hot table, and
        backup-stock-database.ps1 decides whether a cold twin's incremental
        chunk chain has caught up far enough for the nightly dump to keep
        excluding its data. Narrow the window here only -- by passing
        --hot-days on the command line and leaving the key alone -- and the
        backup side still measures freshness against 365 days, so it goes on
        excluding a twin holding rows the chain never exported. Unset, both
        sides use 365, which is the policy in scripts/database-storage-tiers.py.

        Pure: it takes the file's lines, reads this one key out of them and
        nothing else, so the credentials in runtime.env never reach this process
        or its command line.
    #>
    param([Parameter(Mandatory)][AllowEmptyCollection()][string[]]$Lines)
    $value = ''
    foreach ($line in $Lines) {
        $match = [regex]::Match($line, '^\s*STORAGE_TIER_HOT_DAYS\s*=\s*(.*?)\s*$')
        # Last assignment wins, the way a shell sourcing the file would read it.
        if ($match.Success) { $value = $match.Groups[1].Value.Trim([char]'"', [char]"'") }
    }
    if (-not $value) { return 365 }
    $days = 0
    if (-not [int]::TryParse($value, [ref]$days) -or $days -lt 1) {
        # Refuse rather than fall back to 365: a typo here would silently move a
        # year of history the operator meant to keep hot, or the reverse.
        throw "Invalid STORAGE_TIER_HOT_DAYS: $value"
    }
    return $days
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
$arguments = @($tierScript, $Command, '--env-file', $RuntimeEnv)
# The hot window is a runtime.env setting, not a CLI default, because the backup
# job reads the same key (see Get-StorageTierHotDays). Passed on every command
# so plan/status describe the window apply will actually use.
if (-not ($CliArguments -contains '--hot-days')) {
    $hotDays = Get-StorageTierHotDays -Lines ([IO.File]::ReadAllLines($RuntimeEnv))
    $arguments += @('--hot-days', [string]$hotDays)
}
# Only `apply` moves rows, and only an explicit caller value wins over the
# defaults (so `-Command apply --deadline 07:00` still works).
if ($Command -eq 'apply') {
    if ($Deadline -and -not ($CliArguments -contains '--deadline')) { $arguments += @('--deadline', $Deadline) }
    if ($MaxSeconds -gt 0 -and -not ($CliArguments -contains '--max-seconds')) { $arguments += @('--max-seconds', [string]$MaxSeconds) }
}
$arguments += $CliArguments

Write-TierLog "start: $Command (release $release)"
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'   # native stderr must be logged, not thrown
# The task host console is GBK; the CLI prints ASCII-only JSON, but a Python
# traceback can still carry UTF-8 text. PowerShell 7 decodes a native command's
# output with [Console]::OutputEncoding, so the producer has to be told to use
# the same encoding: redirected into a pipeline, Python otherwise encodes
# stdout/stderr with locale.getpreferredencoding() -- cp936 on this host -- and
# exactly the non-ASCII failure diagnostics arrive as mojibake in the tier log.
$env:PYTHONIOENCODING = 'utf-8'
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
