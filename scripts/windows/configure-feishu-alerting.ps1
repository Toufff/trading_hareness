[CmdletBinding()]
param(
    [ValidateSet('CustomBot', 'App')]
    [string]$Transport = 'CustomBot',
    [string]$WebhookUrl = '',
    [securestring]$SigningSecret,
    [switch]$PromptSigningSecret,
    [string]$AppId = '',
    [securestring]$AppSecret,
    [string]$ReceiveId = '',
    [ValidateSet('chat_id', 'open_id', 'union_id', 'user_id', 'email')]
    [string]$ReceiveIdType = 'chat_id',
    [string]$AccountKey = 'citics-primary',
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$RepositoryRoot = '',
    [string]$SshHost = 'lightServer1',
    [int]$ApiPort = 5681,
    [switch]$ApplyRemoteEdge,
    [switch]$SkipLocalRestart,
    [switch]$SkipRemoteRestart
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if (-not $RepositoryRoot) { $RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')) }
$helper = Join-Path $RepositoryRoot 'deploy\intraday-edge\manage_feishu_env.py'
$envPath = Join-Path ([IO.Path]::GetFullPath($PlatformRoot)) 'config\runtime.env'

function Resolve-Python {
    $venv = Join-Path $RepositoryRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $venv -PathType Leaf) { return $venv }
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $command) { $command = Get-Command python -ErrorAction Stop }
    return $command.Source
}

function ConvertFrom-Secret([securestring]$Value) {
    if ($null -eq $Value) { return '' }
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

function Invoke-RemoteInput([string]$InputText, [string]$RemoteCommand, [int]$TimeoutSeconds = 40) {
    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $ssh
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardInput = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    foreach ($argument in @('-o','BatchMode=yes','-o','ConnectTimeout=8',$SshHost,$RemoteCommand)) {
        [void]$start.ArgumentList.Add($argument)
    }
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $start
    if (-not $process.Start()) { throw 'Failed to start ssh.exe' }
    $process.StandardInput.Write($InputText)
    $process.StandardInput.Close()
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        $process.Kill($true)
        throw "Remote Feishu configuration timed out after $TimeoutSeconds seconds"
    }
    $stdout = $process.StandardOutput.ReadToEnd().Trim()
    $stderr = $process.StandardError.ReadToEnd().Trim()
    if ($process.ExitCode -ne 0) {
        throw "Remote Feishu configuration failed (exit $($process.ExitCode)): $stderr"
    }
    return $stdout
}

if (-not (Test-Path -LiteralPath $helper -PathType Leaf)) { throw "Missing env manager: $helper" }
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) { throw "Missing runtime env: $envPath" }

$payload = [ordered]@{
    transport = if ($Transport -eq 'CustomBot') { 'custom_bot' } else { 'app' }
    discipline_alert_account_key = $AccountKey
}
if ($Transport -eq 'CustomBot') {
    if (-not $WebhookUrl) { throw 'WebhookUrl is required for CustomBot transport' }
    if ($PromptSigningSecret -and $null -eq $SigningSecret) {
        $SigningSecret = Read-Host 'Feishu custom-bot signing secret (input is hidden)' -AsSecureString
    }
    $payload.webhook_url = $WebhookUrl
    $payload.signing_secret = ConvertFrom-Secret $SigningSecret
} else {
    if (-not $AppId -or -not $ReceiveId) { throw 'AppId and ReceiveId are required for App transport' }
    if ($null -eq $AppSecret) {
        $AppSecret = Read-Host 'Feishu app secret (input is hidden)' -AsSecureString
    }
    $payload.app_id = $AppId
    $payload.app_secret = ConvertFrom-Secret $AppSecret
    $payload.receive_id = $ReceiveId
    $payload.receive_id_type = $ReceiveIdType
}
$json = $payload | ConvertTo-Json -Compress

# G:\StockPlatform is the ACL-restricted source of truth and the current alert
# writer. A remote edge is optional and must be explicitly requested.
$localResult = $json | & (Resolve-Python) $helper configure --env-file $envPath 2>&1
if ($LASTEXITCODE -ne 0) { throw "Failed to update local Feishu configuration: $localResult" }
& icacls.exe (Split-Path -Parent $envPath) /inheritance:r /grant:r "${env:USERDOMAIN}\${env:USERNAME}:(OI)(CI)F" 'SYSTEM:(OI)(CI)F' | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Failed to restore runtime configuration ACLs' }

if (-not $SkipLocalRestart) {
    $productionRepository = [IO.Path]::GetFullPath((Join-Path $PlatformRoot 'current'))
    $productionStop = Join-Path $productionRepository 'scripts\windows\stop-stock-platform.ps1'
    $productionStart = Join-Path $productionRepository 'scripts\windows\start-stock-platform.ps1'
    foreach ($script in @($productionStop, $productionStart)) {
        if (-not (Test-Path -LiteralPath $script -PathType Leaf)) {
            throw "Published runtime script is missing; refusing to launch production from the development tree: $script"
        }
    }
    & $productionStop `
        -PlatformRoot $PlatformRoot -ApiPort $ApiPort | Out-Null
    & $productionStart `
        -PlatformRoot $PlatformRoot -RepositoryRoot $productionRepository -ApiPort $ApiPort | Out-Null
}

if ($ApplyRemoteEdge) {
    $remoteHelper = '/opt/quant-intraday-edge/current/deploy/intraday-edge/manage_feishu_env.py'
    $remoteEnv = '/etc/quant-intraday-edge.env'
    $configure = "set -e; test -f '$remoteHelper'; if [ `"`$(id -u)`" -eq 0 ]; then python3 '$remoteHelper' configure --env-file '$remoteEnv'; else sudo -n python3 '$remoteHelper' configure --env-file '$remoteEnv'; fi"
    [void](Invoke-RemoteInput -InputText $json -RemoteCommand $configure)
    if (-not $SkipRemoteRestart) {
        $restart = "set -e; if [ `"`$(id -u)`" -eq 0 ]; then systemctl restart quant-intraday-edge.service; else sudo -n systemctl restart quant-intraday-edge.service; fi; systemctl is-active --quiet quant-intraday-edge.service"
        [void](Invoke-RemoteInput -InputText '' -RemoteCommand $restart -TimeoutSeconds 90)
    }
}

# Never echo serialized payloads, IDs, webhook URLs, or secrets.
[pscustomobject]@{
    status = 'configured'
    transport = if ($Transport -eq 'CustomBot') { 'custom_bot' } else { 'app' }
    local_secret_source = $envPath
    local_service_restarted = -not $SkipLocalRestart
    remote_applied = $ApplyRemoteEdge.IsPresent
    scan_interval_seconds = 30
    discipline_alert_interval_seconds = 30
    discipline_alert_account_configured = $true
    intraday_advisory_enabled = $true
    quote_acquisition_seconds = 5
    local_evaluation_seconds = 1
    deepseek_analysis_seconds = 600
    codex_report_seconds = 1800
} | ConvertTo-Json -Compress
