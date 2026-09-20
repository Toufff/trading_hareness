[CmdletBinding()]
param(
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
$python = Join-Path $RepositoryRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { $python = (Get-Command python.exe -ErrorAction Stop).Source }

$result = & $python $helper disable --env-file $envPath 2>&1
if ($LASTEXITCODE -ne 0) { throw "Failed to disable local Feishu configuration: $result" }

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
    $ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
    $remoteHelper = '/opt/quant-intraday-edge/current/deploy/intraday-edge/manage_feishu_env.py'
    $remoteEnv = '/etc/quant-intraday-edge.env'
    $remote = "set -e; test -f '$remoteHelper'; if [ `"`$(id -u)`" -eq 0 ]; then python3 '$remoteHelper' disable --env-file '$remoteEnv'; else sudo -n python3 '$remoteHelper' disable --env-file '$remoteEnv'; fi"
    $output = & $ssh -o BatchMode=yes -o ConnectTimeout=8 $SshHost $remote 2>&1
    if ($LASTEXITCODE -ne 0) { throw "Failed to disable remote Feishu configuration: $output" }
    if (-not $SkipRemoteRestart) {
        $restart = "set -e; if [ `"`$(id -u)`" -eq 0 ]; then systemctl restart quant-intraday-edge.service; else sudo -n systemctl restart quant-intraday-edge.service; fi; systemctl is-active --quiet quant-intraday-edge.service"
        $output = & $ssh -o BatchMode=yes -o ConnectTimeout=8 $SshHost $restart 2>&1
        if ($LASTEXITCODE -ne 0) { throw "Remote service restart failed: $output" }
    }
}

[pscustomobject]@{
    status = 'disabled'
    local_service_restarted = -not $SkipLocalRestart
    remote_applied = $ApplyRemoteEdge.IsPresent
} | ConvertTo-Json -Compress
