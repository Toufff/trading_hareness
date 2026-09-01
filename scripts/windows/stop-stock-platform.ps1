[CmdletBinding()]
param([string]$PlatformRoot = 'G:\StockPlatform')

$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$pidPath = Join-Path $root 'logs\quant-api.pid'
if (-not (Test-Path -LiteralPath $pidPath -PathType Leaf)) {
    [pscustomobject]@{ status = 'not_running' }
    return
}
$pidValue = 0
[void][int]::TryParse(([IO.File]::ReadAllText($pidPath).Trim()), [ref]$pidValue)
if ($pidValue -gt 0) {
    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($process) {
        Stop-Process -Id $pidValue
        $process.WaitForExit(10000)
    }
}
Remove-Item -LiteralPath $pidPath -Force
[pscustomobject]@{ status = 'stopped'; pid = $pidValue }
