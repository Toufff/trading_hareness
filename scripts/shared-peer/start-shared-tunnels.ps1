param(
    [string]$SshAlias = "lightServer1",
    [int]$RemoteDatabasePort = 15432,
    [int]$RemoteApiPort = 15681,
    [int]$LocalDatabasePort = 55432,
    [int]$LocalApiPort = 5681
)

$ErrorActionPreference = "Stop"
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

& $ssh @arguments
exit $LASTEXITCODE
