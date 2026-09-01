[CmdletBinding()]
param(
    [string]$Server = 'lightServer1',
    [string]$RemoteRoot = '/srv/stockbrain',
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
if ($RemoteRoot -notmatch '^/[A-Za-z0-9._/-]+$') { throw "Unsafe RemoteRoot: $RemoteRoot" }
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$frontend = Join-Path $repository 'frontend'
$dist = Join-Path $frontend 'dist'
if (-not $SkipBuild) {
    & npm --prefix $frontend run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed' }
}
if (-not (Test-Path -LiteralPath (Join-Path $dist 'index.html') -PathType Leaf)) { throw 'Frontend dist is missing' }

$release = (Get-Date).ToUniversalTime().ToString('yyyyMMddHHmmss')
$remoteRelease = "$RemoteRoot/releases/$release"
& ssh $Server "mkdir -p '$remoteRelease'"
if ($LASTEXITCODE -ne 0) { throw 'Could not create server release directory' }
& scp -q -r "$dist/." "${Server}:$remoteRelease/"
if ($LASTEXITCODE -ne 0) { throw 'Static release upload failed' }
& scp -q (Join-Path $repository 'deploy\stockbrain-local-gateway.nginx.conf') "${Server}:/etc/nginx/snippets/stockbrain-local-gateway.conf.next"
if ($LASTEXITCODE -ne 0) { throw 'Nginx gateway snippet upload failed' }

$activate = @"
set -eu
root='$RemoteRoot'
release='$remoteRelease'
snippet='/etc/nginx/snippets/stockbrain-local-gateway.conf'
next_snippet="`${snippet}.next"
previous="`$(readlink -f "`${root}/current" 2>/dev/null || true)"
previous_snippet="`$(mktemp)"
had_snippet=0
if [ -f "`${snippet}" ]; then
  cp "`${snippet}" "`${previous_snippet}"
  had_snippet=1
fi
if ! grep -Rqs 'include /etc/nginx/snippets/stockbrain-local-gateway.conf' /etc/nginx/sites-enabled /etc/nginx/conf.d; then
  rm -f "`${previous_snippet}"
  echo 'active Nginx site does not include the stockbrain gateway snippet' >&2
  exit 1
fi
mv "`${next_snippet}" "`${snippet}"
ln -sfn "`${release}" "`${root}/current.next"
mv -Tf "`${root}/current.next" "`${root}/current"
if ! nginx -t; then
  if [ -n "`${previous}" ]; then ln -sfn "`${previous}" "`${root}/current"; fi
  if [ "`${had_snippet}" -eq 1 ]; then cp "`${previous_snippet}" "`${snippet}"; else rm -f "`${snippet}"; fi
  rm -f "`${previous_snippet}"
  exit 1
fi
systemctl reload nginx
rm -f "`${previous_snippet}"
printf '%s\n' "`${release}"
"@
$activatedRelease = (& ssh $Server $activate | Select-Object -Last 1)
if ($LASTEXITCODE -ne 0 -or $activatedRelease -ne $remoteRelease) { throw 'Remote release activation failed' }

[pscustomobject]@{ release = $release; remote_release = $remoteRelease; status = 'active' }
