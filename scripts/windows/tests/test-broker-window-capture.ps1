[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$script = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\broker-window-capture.ps1'))
$folder = Join-Path ([IO.Path]::GetTempPath()) ('broker capture tests ' + [guid]::NewGuid())
[IO.Directory]::CreateDirectory($folder) | Out-Null
function Invoke-CaptureScript([string[]]$Arguments) {
    $parameters = @{}
    for ($i = 0; $i -lt $Arguments.Length; $i += 2) {
        $parameters[$Arguments[$i].TrimStart('-')] = $Arguments[$i + 1]
    }
    try {
        $output = & $script @parameters | Out-String
        return @{ Code = 0; Out = $output; Error = '' }
    } catch {
        return @{ Code = 1; Out = ''; Error = $_.Exception.Message }
    }
}
function Assert-Rejected([string]$Name, [string[]]$Arguments) {
    $path = Join-Path $folder ($Name + '.png')
    $result = Invoke-CaptureScript ($Arguments + @('-OutputPath', $path))
    if ($result.Code -eq 0) { throw "$Name incorrectly succeeded" }
    if (Test-Path -LiteralPath $path) { throw "$Name left a success image" }
    $receipt = Get-Content -LiteralPath "$path.receipt.json" -Raw | ConvertFrom-Json
    if ($receipt.result -ne 'failed' -or $receipt.fileHash) { throw "$Name misleading receipt" }
    Write-Output "$Name rejected with failure receipt"
}
$listed = Invoke-CaptureScript @('-Mode', 'list')
if ($listed.Code -ne 0) { throw $listed.Error }
$windows = @($listed.Out | ConvertFrom-Json)
if ($windows | Where-Object { $_.hwnd -le 0 -or $_.pid -le 0 }) { throw 'Invalid HWND/PID returned' }
Assert-Rejected 'invalid-hwnd' @('-Mode', 'capture', '-ProcessIds', '1', '-Hwnd', '1')
$target = @($windows | Where-Object title -eq '网上股票交易系统5.0')
if ($target.Count -eq 1) {
    Assert-Rejected 'wrong-pid' @('-Mode', 'capture', '-ProcessIds', '1', '-Hwnd', [string]$target[0].hwnd)
} else { Write-Output 'SKIP wrong-pid real-window test: no unique logged-in client window' }
Write-Output "Capture negative tests passed. Evidence: $folder"
