[CmdletBinding()]
param(
    [string]$PeerRoot = 'G:\StockPlatform\peer',
    [string]$Name = 'stockpeer_ed25519',
    [switch]$Rotate
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = [IO.Path]::GetFullPath($PeerRoot).TrimEnd('\') + '\'
$secretRoot = [IO.Path]::GetFullPath((Join-Path $PeerRoot 'secrets')).TrimEnd('\') + '\'
$keyPath = [IO.Path]::GetFullPath((Join-Path $secretRoot $Name))
if (-not $keyPath.StartsWith($secretRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Refusing to create a key outside the peer secret directory'
}
New-Item -ItemType Directory -Force -Path $secretRoot | Out-Null

if (Test-Path -LiteralPath $keyPath) {
    if (-not $Rotate) { throw "Key already exists; pass -Rotate to archive and replace it: $keyPath" }
    $suffix = Get-Date -Format 'yyyyMMdd_HHmmss'
    Move-Item -LiteralPath $keyPath -Destination "$keyPath.bak.$suffix"
    if (Test-Path -LiteralPath "$keyPath.pub") {
        Move-Item -LiteralPath "$keyPath.pub" -Destination "$keyPath.pub.bak.$suffix"
    }
}

$start = [Diagnostics.ProcessStartInfo]::new()
$start.FileName = (Get-Command ssh-keygen.exe -ErrorAction Stop).Source
$start.UseShellExecute = $false
$start.ArgumentList.Add('-q')
$start.ArgumentList.Add('-t')
$start.ArgumentList.Add('ed25519')
$start.ArgumentList.Add('-f')
$start.ArgumentList.Add($keyPath)
$start.ArgumentList.Add('-N')
$start.ArgumentList.Add('')
$start.ArgumentList.Add('-C')
$start.ArgumentList.Add('stockpeer@ultratouf')
$process = [Diagnostics.Process]::Start($start)
$process.WaitForExit()
if ($process.ExitCode -ne 0 -or -not (Test-Path -LiteralPath "$keyPath.pub")) {
    throw "ssh-keygen failed with exit code $($process.ExitCode)"
}
& icacls $keyPath /inheritance:r /grant:r "$env:USERNAME`:F" 'SYSTEM:F' | Out-Null

[pscustomobject]@{ status = 'ready'; private_key = $keyPath; public_key = "$keyPath.pub" }
