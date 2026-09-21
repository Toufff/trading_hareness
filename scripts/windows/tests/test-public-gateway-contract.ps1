$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..')).TrimEnd('\')
$snippet = Get-Content (Join-Path $repository 'deploy\stockbrain-local-gateway.nginx.conf') -Raw
$deploy = Get-Content (Join-Path $repository 'scripts\deploy-stock-dashboard.ps1') -Raw

$configMatch = [regex]::Match($snippet, '(?ms)^location = /api/config \{(?<body>.*?)^\}')
if (-not $configMatch.Success) { throw 'Public gateway must define an exact /api/config route' }
if ($configMatch.Groups['body'].Value -notmatch 'proxy_pass http://127\.0\.0\.1:15680;') {
    throw '/api/config must route to the dashboard adapter relay'
}

$nativePosition = $snippet.IndexOf('location ^~ /api/v1/')
if ($nativePosition -lt 0 -or $configMatch.Index -gt $nativePosition) {
    throw 'The exact adapter config route must remain explicit before native API routing'
}

if ($deploy -notmatch [regex]::Escape('add_header Cache-Control "public, max-age=2592000, immutable";')) {
    throw 'Dashboard deploy must make only content-hashed assets publicly cacheable at the edge'
}
if ($deploy -notmatch 'expected exactly one public immutable /assets/ cache header') {
    throw 'Dashboard deploy must fail closed when the expected /assets/ cache location drifts'
}
if ($deploy -notmatch 'cp "`\$\{previous_site\}" "`\$\{site\}"') {
    throw 'Dashboard deploy must restore the Nginx site when validation or reload fails'
}

Write-Output 'public gateway contract tests passed'
