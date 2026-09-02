[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

# start-stock-platform.ps1's Read-EnvFile is a private function (no module
# boundary), so it is extracted from the script's own AST and defined in
# this process rather than duplicated here -- this test breaks if the real
# function is ever renamed or removed, which is the point.
$scriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'start-stock-platform.ps1'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw "Missing $scriptPath" }
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$null, [ref]$parseErrors)
if ($parseErrors -and $parseErrors.Count -gt 0) { throw "Failed to parse $scriptPath" }
$functionAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Read-EnvFile' }, $true)
if (-not $functionAst) { throw "Read-EnvFile function not found in $scriptPath" }
. ([scriptblock]::Create($functionAst.Extent.Text))

$sentinelName = "TRADING_HARENESS_TEST_SENTINEL_$([Guid]::NewGuid().ToString('N').Substring(0, 8))"
$envFile = Join-Path ([IO.Path]::GetTempPath()) "trading-hareness-envfile-$([Guid]::NewGuid().ToString('N')).env"
try {
    [IO.File]::WriteAllLines($envFile, @("$sentinelName=sentinel-value", 'PGHOST=127.0.0.1', '# a comment', ''), [Text.UTF8Encoding]::new($false))
    Remove-Item -Path "Env:$sentinelName" -ErrorAction SilentlyContinue

    $result = Read-EnvFile $envFile

    Assert-True ($result -is [hashtable]) 'Read-EnvFile must return a hashtable instead of mutating the process environment'
    Assert-True ($result[$sentinelName] -eq 'sentinel-value') 'Read-EnvFile must parse KEY=VALUE lines into the returned hashtable'
    Assert-True ($result['PGHOST'] -eq '127.0.0.1') 'Read-EnvFile must parse every KEY=VALUE line'
    Assert-True (-not (Test-Path "Env:$sentinelName")) 'Read-EnvFile must not inject values into the current process environment (a full Set-Item Env: injection here would leak secrets like PGADMINPASSWORD into every later sibling process, e.g. an ssh tunnel launched without an explicit -Environment)'

    [pscustomobject]@{
        passed = $true
        returned_hashtable = $true
        parsed_sentinel = $result[$sentinelName]
        env_not_polluted = -not (Test-Path "Env:$sentinelName")
    }
} finally {
    Remove-Item -LiteralPath $envFile -Force -ErrorAction SilentlyContinue
    Remove-Item -Path "Env:$sentinelName" -ErrorAction SilentlyContinue
}
