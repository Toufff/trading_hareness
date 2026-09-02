param(
    [string]$SshAlias = "lightServer1",
    [int]$RemoteDatabasePort = 15432,
    [int]$RemoteApiPort = 15681,
    [int]$LocalDatabasePort = 55432,
    [int]$LocalApiPort = 5681,
    [string]$PlatformRoot = 'G:\StockPlatform'
)

$ErrorActionPreference = "Stop"
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\runtime-observability.psm1') -Force
$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$arguments = @(
    "-NT",
    "-o", "BatchMode=yes",
    "-o", "ExitOnForwardFailure=yes",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-R", "127.0.0.1:$RemoteDatabasePort`:127.0.0.1:$LocalDatabasePort",
    "-R", "127.0.0.1:$RemoteApiPort`:127.0.0.1:$LocalApiPort",
    $SshAlias
)

$run = Start-RuntimeSupervisor -PlatformRoot $PlatformRoot -RepositoryRoot $repository -Service 'shared-peer-tunnels' `
    -Executable $ssh -WorkingDirectory $repository -Arguments $arguments -Metadata @{
        ssh_alias = $SshAlias
        remote_database_port = $RemoteDatabasePort
        remote_api_port = $RemoteApiPort
        local_database_port = $LocalDatabasePort
        local_api_port = $LocalApiPort
    }
while (Get-Process -Id ([int]$run.supervisor_pid) -ErrorAction SilentlyContinue) { Start-Sleep -Seconds 2 }
$state = Get-RuntimeState -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels'
if ($state -and $state.PSObject.Properties['exit_code']) { exit ([int]$state.exit_code) }
exit 125
