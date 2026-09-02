param(
    [string]$TaskName = "trading-hareness-shared-peer-tunnels",
    [string]$ScriptPath = (Join-Path $PSScriptRoot "start-shared-tunnels.ps1"),
    [string]$PlatformRoot = 'G:\StockPlatform',
    [string]$SshAlias = 'lightServer1',
    [int]$RemoteDatabasePort = 15432,
    [int]$RemoteApiPort = 15681,
    [int]$LocalDatabasePort = 55432,
    [int]$LocalApiPort = 5681
)

$ErrorActionPreference = "Stop"
$resolved = (Resolve-Path -LiteralPath $ScriptPath).Path
$repository = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $resolved) '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\runtime-observability.psm1') -Force
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$state = Request-RuntimeStop -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels' -Reason 'task_reinstall' -RequestedBy 'install-shared-tunnel-task.ps1'
if ($state) {
    if ($state.PSObject.Properties['launcher_pid'] -and [int]$state.launcher_pid -gt 0) {
        Stop-Process -Id ([int]$state.launcher_pid) -Force -ErrorAction SilentlyContinue
    }
    if ($state.PSObject.Properties['supervisor_pid'] -and [int]$state.supervisor_pid -gt 0) {
        Wait-Process -Id ([int]$state.supervisor_pid) -Timeout 3 -ErrorAction SilentlyContinue
        if (Get-Process -Id ([int]$state.supervisor_pid) -ErrorAction SilentlyContinue) {
            Stop-Process -Id ([int]$state.supervisor_pid) -Force -ErrorAction SilentlyContinue
        }
    }
}
Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction SilentlyContinue |
    Where-Object {
        $_.CommandLine -match "127\.0\.0\.1:$RemoteDatabasePort`:127\.0\.0\.1:$LocalDatabasePort" -and
        $_.CommandLine -match "127\.0\.0\.1:$RemoteApiPort`:127\.0\.0\.1:$LocalApiPort"
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
$pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
$action = New-ScheduledTaskAction -Execute $pwsh -Argument (
    '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -PlatformRoot "{1}" -SshAlias "{2}" -RemoteDatabasePort {3} -RemoteApiPort {4} -LocalDatabasePort {5} -LocalApiPort {6}' -f `
        $resolved, $PlatformRoot, $SshAlias, $RemoteDatabasePort, $RemoteApiPort, $LocalDatabasePort, $LocalApiPort
)
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
$deadline = [DateTime]::UtcNow.AddSeconds(30)
do {
    Start-Sleep -Milliseconds 500
    $task = Get-ScheduledTask -TaskName $TaskName
} while ($task.State -ne 'Running' -and [DateTime]::UtcNow -lt $deadline)
if ($task.State -ne 'Running') {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    throw "Shared peer tunnel task did not stay running; last result $($info.LastTaskResult)"
}
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -TaskName $TaskName
if ($task.State -ne 'Running') {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    throw "Shared peer tunnel task exited during startup; last result $($info.LastTaskResult)"
}

$healthDeadline = [DateTime]::UtcNow.AddSeconds(30)
$remoteHealth = ''
do {
    $remoteHealth = (& ssh.exe -o BatchMode=yes -o ConnectTimeout=5 $SshAlias `
        "curl -sS --max-time 5 -o /dev/null -w '%{http_code}' http://127.0.0.1:$RemoteApiPort/health" 2>$null | Out-String).Trim()
    if ($remoteHealth -eq '200') { break }
    Start-Sleep -Seconds 1
} while ([DateTime]::UtcNow -lt $healthDeadline)
if ($remoteHealth -ne '200') {
    throw "Shared peer tunnel task is running, but remote API health returned '$remoteHealth' instead of 200"
}

$state = Get-RuntimeState -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels'
if (-not $state -or -not $state.PSObject.Properties['run_id']) {
    throw 'Shared peer tunnel became reachable without a supervised runtime state'
}
$healthyState = @{}
foreach ($property in $state.PSObject.Properties) {
    if ($property.Name -notin @('schema_version', 'service', 'updated_at')) {
        $healthyState[$property.Name] = $property.Value
    }
}
$healthyState.status = 'healthy'
$healthyState.health = 'remote_api_http_200'
$healthyState.verified_at = [DateTimeOffset]::Now.ToString('o')
$healthyState.remote_api_port = $RemoteApiPort
$healthyState.remote_database_port = $RemoteDatabasePort
[void](Set-RuntimeState -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels' -State $healthyState)
[void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels' -Event 'healthy' `
    -RunId ([string]$state.run_id) -Data @{
        health = 'remote_api_http_200'
        remote_api_port = $RemoteApiPort
        remote_database_port = $RemoteDatabasePort
    })
$task | Select-Object TaskName,State,@{Name='RemoteApiHealth';Expression={$remoteHealth}}
