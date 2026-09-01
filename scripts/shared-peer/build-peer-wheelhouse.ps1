[CmdletBinding()]
param(
    [string]$RepositoryRoot = 'F:\AIWorkflow\trading_hareness',
    [string]$Destination = 'G:\StockPlatform\peer\staging\wheelhouse'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$python = Join-Path $RepositoryRoot '.venv\Scripts\python.exe'
$requirements = Join-Path $RepositoryRoot 'quant-service\requirements.txt'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) { throw "Python runtime not found: $python" }
New-Item -ItemType Directory -Force -Path $Destination | Out-Null

# AkShare depends on jsonpath, which publishes a universal wheel/sdist but no
# platform wheel metadata. Resolve the remaining graph for Linux explicitly,
# then add AkShare and jsonpath as platform-independent artifacts.
$akshareDependencies = @(
    'beautifulsoup4>=4.9.1', 'lxml>=4.2.1', 'curl_cffi>=0.13.0',
    'html5lib>=1.0.1', 'xlrd>=1.2.0', 'urllib3>=1.25.8', 'tqdm>=4.43.0',
    'openpyxl>=3.0.3', 'tabulate>=0.8.6', 'decorator>=4.4.2',
    'py-mini-racer>=0.6.0', 'akracer>=0.0.13',
    'uvloop>=0.14.0,!=0.15.0,!=0.15.1'
)
$generated = Join-Path (Split-Path $Destination -Parent) 'linux-wheelhouse-requirements.txt'
$base = Get-Content -LiteralPath $requirements | Where-Object { $_ -notmatch '^akshare==' }
[IO.File]::WriteAllLines($generated, @($base) + $akshareDependencies, [Text.UTF8Encoding]::new($false))

& $python -m pip download --dest $Destination 'akshare==1.18.93' --no-deps
if ($LASTEXITCODE -ne 0) { throw 'Could not download AkShare wheel' }
& $python -m pip wheel --wheel-dir $Destination --no-deps 'jsonpath==0.82.2'
if ($LASTEXITCODE -ne 0) { throw 'Could not build universal jsonpath wheel' }
& $python -m pip download --dest $Destination --requirement $generated `
    --platform manylinux_2_28_x86_64 --platform manylinux2014_x86_64 --platform manylinux1_x86_64 `
    --python-version 312 --implementation cp --abi cp312 --only-binary=:all:
if ($LASTEXITCODE -ne 0) { throw 'Could not build Linux CPython 3.12 wheelhouse' }

$files = Get-ChildItem -LiteralPath $Destination -File | Where-Object Name -ne 'SHA256SUMS'
$manifest = $files | Sort-Object Name | ForEach-Object {
    '{0}  {1}' -f (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant(), $_.Name
}
$manifestText = [string]::Join("`n", $manifest) + "`n"
[IO.File]::WriteAllText((Join-Path $Destination 'SHA256SUMS'), $manifestText, [Text.UTF8Encoding]::new($false))
[pscustomobject]@{
    status = 'ready'
    destination = $Destination
    files = $files.Count
    bytes = ($files | Measure-Object Length -Sum).Sum
}
