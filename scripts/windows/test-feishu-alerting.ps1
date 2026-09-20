[CmdletBinding()]
param(
    [ValidateSet('Mock', 'Config', 'Live')]
    [string]$Mode = 'Mock',
    [ValidateSet('Both', 'CustomBot', 'App')]
    [string]$Transport = 'Both',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RepositoryRoot = '',
    [string]$SshHost = 'lightServer1',
    [switch]$SendTest,
    [switch]$RemoteEdge
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $RepositoryRoot) { $RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
$python = Join-Path $RepositoryRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { $python = (Get-Command python.exe -ErrorAction Stop).Source }

if ($Mode -eq 'Mock') {
    $checks = [Collections.Generic.List[object]]::new()
    if ($Transport -in @('Both', 'CustomBot')) {
        $output = & $python (Join-Path $RepositoryRoot 'deploy\intraday-edge\edge_feishu_custom_bot_mock_preflight.py') 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Custom-bot mock preflight failed: $output" }
        $checks.Add(($output | ConvertFrom-Json))
    }
    if ($Transport -in @('Both', 'App')) {
        $env:PYTHONPATH = Join-Path $RepositoryRoot 'quant-service'
        try {
            $output = & $python (Join-Path $RepositoryRoot 'deploy\intraday-edge\edge_feishu_mock_preflight.py') 2>&1
        } finally {
            Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
        }
        if ($LASTEXITCODE -ne 0) { throw "Application-bot mock preflight failed: $output" }
        $checks.Add(($output | ConvertFrom-Json))
    }
    [pscustomobject]@{ status = 'passed'; mode = 'mock'; checks = $checks } | ConvertTo-Json -Depth 4 -Compress
    exit 0
}

if ($Mode -eq 'Config') {
    $envPath = Join-Path ([IO.Path]::GetFullPath($PlatformRoot)) 'config\runtime.env'
    $output = & $python (Join-Path $RepositoryRoot 'deploy\intraday-edge\manage_feishu_env.py') status --env-file $envPath 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Feishu configuration status failed: $output" }
    $status = $output | ConvertFrom-Json
    if (-not $status.configured -or -not $status.credentials_present -or -not $status.enabled `
            -or $status.scan_interval_seconds -ne 30 -or -not $status.discipline_alerts_enabled `
            -or $status.discipline_alert_interval_seconds -ne 30 `
            -or -not $status.discipline_alert_account_configured `
            -or -not $status.intraday_advisory_enabled `
            -or -not $status.intraday_advisory_account_configured `
            -or $status.intraday_advisory_cadence.fetch_seconds -ne 5 `
            -or $status.intraday_advisory_cadence.local_tick_seconds -ne 1 `
            -or $status.intraday_advisory_cadence.deepseek_seconds -ne 600 `
            -or $status.intraday_advisory_cadence.codex_seconds -ne 1800) {
        throw 'Feishu configuration is incomplete; no credential values were printed'
    }
    $status | ConvertTo-Json -Compress
    exit 0
}

$appScript = '/opt/quant-intraday-edge/current/deploy/intraday-edge/edge_feishu_preflight.py'
$customBotScript = '/opt/quant-intraday-edge/current/deploy/intraday-edge/edge_feishu_custom_bot_preflight.py'
$appArgument = if ($SendTest) { ' --send-test' } else { '' }
if ($RemoteEdge) {
    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    $remote = "set -e; set -a; . /etc/quant-intraday-edge.env; set +a; case `"`${FEISHU_ALERT_TRANSPORT:-app}`" in custom_bot) if [ '$($SendTest.IsPresent.ToString().ToLowerInvariant())' != true ]; then echo 'custom-bot live verification requires -SendTest' >&2; exit 2; fi; test -f '$customBotScript'; /opt/quant-intraday-edge/.venv/bin/python '$customBotScript' ;; app) test -f '$appScript'; /opt/quant-intraday-edge/.venv/bin/python '$appScript'$appArgument ;; *) echo 'unsupported FEISHU_ALERT_TRANSPORT' >&2; exit 2 ;; esac"
    $output = & $ssh -o BatchMode=yes -o ConnectTimeout=8 $SshHost $remote 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Remote live Feishu preflight failed: $output" }
    $output
    exit 0
}

$envPath = Join-Path ([IO.Path]::GetFullPath($PlatformRoot)) 'config\runtime.env'
$settings = @{}
foreach ($line in [IO.File]::ReadAllLines($envPath, [Text.Encoding]::UTF8)) {
    if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
    $parts = $line.Split('=', 2)
    $settings[$parts[0]] = $parts[1]
}
$transportName = if ($settings.FEISHU_ALERT_TRANSPORT) { $settings.FEISHU_ALERT_TRANSPORT } else { 'app' }
if ($transportName -eq 'custom_bot' -and -not $SendTest) {
    throw 'Custom-bot live verification requires -SendTest because the webhook has no read-only target endpoint'
}
$localScript = if ($transportName -eq 'custom_bot') {
    Join-Path $RepositoryRoot 'deploy\intraday-edge\edge_feishu_custom_bot_preflight.py'
} elseif ($transportName -eq 'app') {
    Join-Path $RepositoryRoot 'deploy\intraday-edge\edge_feishu_preflight.py'
} else {
    throw 'Unsupported FEISHU_ALERT_TRANSPORT in the private runtime env'
}
$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = $python
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
[void]$start.ArgumentList.Add($localScript)
if ($transportName -eq 'app' -and $SendTest) { [void]$start.ArgumentList.Add('--send-test') }
$start.Environment['PYTHONPATH'] = Join-Path $RepositoryRoot 'quant-service'
foreach ($key in @('FEISHU_ALERT_TRANSPORT','FEISHU_CUSTOM_BOT_WEBHOOK_URL','FEISHU_CUSTOM_BOT_SIGNING_SECRET',
        'QUANT_FEISHU_DIRECT_ENABLED','FEISHU_APP_ID','FEISHU_APP_SECRET','FEISHU_ALERT_RECEIVE_ID','FEISHU_ALERT_RECEIVE_ID_TYPE')) {
    if ($settings.ContainsKey($key)) { $start.Environment[$key] = [string]$settings[$key] }
}
$process = [Diagnostics.Process]::new()
$process.StartInfo = $start
if (-not $process.Start()) { throw 'Failed to start local Feishu preflight' }
$stdout = $process.StandardOutput.ReadToEnd().Trim()
$stderr = $process.StandardError.ReadToEnd().Trim()
$process.WaitForExit()
if ($process.ExitCode -ne 0) { throw "Local live Feishu preflight failed: $stderr" }
$stdout
