[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('init-key', 'authorize', 'status', 'nightly', 'upload', 'prune', 'fetch', 'selftest')]
    [string]$Command = 'nightly',

    # Extra CLI arguments, e.g. -CommandArguments '--dest','G:\restore','--day','2026-09-16'
    [Parameter(Position = 1, ValueFromRemainingArguments = $true)]
    [string[]]$CommandArguments = @(),

    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$PlatformRoot = 'G:\StockPlatform'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Read-EnvFile([string]$Path) {
    # Same shape as scripts/windows/backup-stock-database.ps1: "KEY=value"
    # lines, '#' comments, everything after the first '=' is the value.
    $result = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if (-not $line -or $line.StartsWith('#')) { continue }
        $parts = $line.Split('=', 2)
        if ($parts.Count -eq 2) { $result[$parts[0]] = $parts[1] }
    }
    return $result
}

$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
if (-not (Test-Path -LiteralPath $RuntimeEnv -PathType Leaf)) { throw "Missing runtime environment file: $RuntimeEnv" }
$config = Read-EnvFile $RuntimeEnv

# The Node CLI reads its configuration from the process environment; nothing
# is passed on the command line, so credentials never show up in the process
# list or in a scheduled-task definition. Only the variables the CLI and the
# ledger actually consume are forwarded.
$forwarded = @(
    'PGHOST', 'PGPORT', 'PGDATABASE', 'PGUSER', 'PGPASSWORD',
    'BAIDU_PAN_APP_KEY', 'BAIDU_PAN_SECRET_KEY',
    'STOCK_BACKUP_ROOT', 'STOCK_BACKUP_OFFSITE_REMOTE_ROOT', 'STOCK_BACKUP_OFFSITE_KEY_FILE',
    'STOCK_BACKUP_LOCAL_RETENTION_DAYS', 'STOCK_BACKUP_OFFSITE_PART_BYTES', 'STOCK_BACKUP_OFFSITE_SLICE_BYTES'
)
foreach ($name in $forwarded) {
    if ($config.ContainsKey($name) -and $config[$name]) { Set-Item -Path "Env:$name" -Value $config[$name] }
}

# Defaults mirror scripts/stock-backup-offsite.mjs so a manual run behaves the
# same whether or not runtime.env spells them out.
if (-not $env:STOCK_BACKUP_ROOT) { Set-Item -Path 'Env:STOCK_BACKUP_ROOT' -Value (Join-Path $platform 'backups') }
if (-not $env:STOCK_BACKUP_OFFSITE_KEY_FILE) { Set-Item -Path 'Env:STOCK_BACKUP_OFFSITE_KEY_FILE' -Value (Join-Path $platform 'config\backup-offsite.key') }

$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
$cli = Join-Path $repository 'scripts\stock-backup-offsite.mjs'
if (-not (Test-Path -LiteralPath $cli -PathType Leaf)) { throw "Missing $cli" }

$node = (Get-Command node.exe -ErrorAction Stop).Source

$logDir = Join-Path $platform 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir 'stock-backup-offsite.jsonl'

# The scheduled task has no console, so the JSON lines the CLI emits are the
# only account of a run; they are echoed as well as appended so a manual run
# still shows them. The pipeline must stay streaming: `authorize` prints its
# device code and then polls for minutes, so buffering the whole run (which
# `$output = & node ...` does) would hide the code the operator has to type.
$previousErrorAction = $ErrorActionPreference
$ErrorActionPreference = 'Continue'   # native stderr must be logged, not thrown
$exitCode = 1
try {
    & $node $cli $Command @CommandArguments 2>&1 | ForEach-Object {
        $text = if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.ToString() } else { [string]$_ }
        Write-Output $text
        try {
            [IO.File]::AppendAllText($logFile, ($text + [Environment]::NewLine), [Text.UTF8Encoding]::new($false))
        } catch {
            Write-Warning "Failed to write off-site log record: $($_.Exception.Message)"
        }
    }
    $exitCode = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previousErrorAction
}
exit $exitCode
