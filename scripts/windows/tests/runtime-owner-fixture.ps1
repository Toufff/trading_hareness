param([string]$PlatformRoot, [string]$RepositoryRoot)
$ErrorActionPreference = 'Stop'
Import-Module (Join-Path $RepositoryRoot 'scripts\windows\runtime-observability.psm1') -Force
$run = Start-RuntimeSupervisor -PlatformRoot $PlatformRoot -RepositoryRoot $RepositoryRoot -Service 'owner-test' `
    -Executable (Get-Command pwsh.exe).Source -WorkingDirectory $PlatformRoot `
    -Arguments @('-NoProfile', '-Command', 'Start-Sleep 300') -Metadata @{stop_with_owner=$true}
while ($true) { Start-Sleep -Seconds 1 }
