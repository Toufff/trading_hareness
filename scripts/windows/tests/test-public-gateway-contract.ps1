$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..')).TrimEnd('\')
$snippet = Get-Content (Join-Path $repository 'deploy\stockbrain-local-gateway.nginx.conf') -Raw

$configMatch = [regex]::Match($snippet, '(?ms)^location = /api/config \{(?<body>.*?)^\}')
if (-not $configMatch.Success) { throw 'Public gateway must define an exact /api/config route' }
if ($configMatch.Groups['body'].Value -notmatch 'proxy_pass http://127\.0\.0\.1:15680;') {
    throw '/api/config must route to the dashboard adapter relay'
}

$nativePosition = $snippet.IndexOf('location ^~ /api/v1/')
if ($nativePosition -lt 0 -or $configMatch.Index -gt $nativePosition) {
    throw 'The exact adapter config route must remain explicit before native API routing'
}

Write-Output 'public gateway contract tests passed'
