param(
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$CredentialFile = "$env:USERPROFILE\.stockbrain\dashboard-credentials.json",
    [string]$PublicOrigin = 'https://stock.toufai.top'
)

$ErrorActionPreference = 'Stop'
$envPath = Join-Path ([IO.Path]::GetFullPath($PlatformRoot)) 'config\runtime.env'
if (-not (Test-Path -LiteralPath $envPath -PathType Leaf)) { throw "Missing runtime env: $envPath" }
if (-not (Test-Path -LiteralPath $CredentialFile -PathType Leaf)) { throw "Missing dashboard credential file: $CredentialFile" }

$credentials = Get-Content -LiteralPath $CredentialFile -Raw | ConvertFrom-Json
$accessPath = [string]$credentials.magic_access_path
if ($accessPath -notmatch '^[A-Za-z0-9_-]{24,128}$') { throw 'Dashboard credential contains an invalid magic_access_path' }
$origin = $PublicOrigin.Trim().TrimEnd('/')
if ($origin -notmatch '^https://') { throw 'PublicOrigin must use HTTPS' }
$setting = "QUANT_DASHBOARD_DECISION_URL=$origin/_access/$accessPath"

$lines = [Collections.Generic.List[string]]::new()
[IO.File]::ReadAllLines($envPath) | ForEach-Object { [void]$lines.Add($_) }
$replaced = $false
for ($index = 0; $index -lt $lines.Count; $index++) {
    if ($lines[$index] -match '^\s*QUANT_DASHBOARD_DECISION_URL=') {
        $lines[$index] = $setting
        $replaced = $true
    }
}
if (-not $replaced) { [void]$lines.Add($setting) }

$temp = "$envPath.$([guid]::NewGuid().ToString('N')).tmp"
try {
    [IO.File]::WriteAllLines($temp, $lines, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temp -Destination $envPath -Force
} finally {
    if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
}

[pscustomobject]@{
    configured = $true
    setting = 'QUANT_DASHBOARD_DECISION_URL'
    credential_source = $CredentialFile
    secret_value_printed = $false
} | ConvertTo-Json -Compress
