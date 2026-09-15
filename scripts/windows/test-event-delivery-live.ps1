[CmdletBinding()]
param([string]$PlatformRoot = 'G:\StockPlatform', [int]$TimeoutSeconds = 480)
$ErrorActionPreference = 'Stop'
$task = Get-ScheduledTask -TaskName 'trading-hareness-event-research-delivery'
if ($task.State -eq 'Running') { throw 'Scheduled delivery is already running; do not overlap acceptance' }
$name = 'trading-hareness-event-acceptance-' + [guid]::NewGuid().ToString('N')
$action = $task.Actions[0]
if ([IO.Path]::GetFileName($action.Execute) -ne 'stock-background-host.exe') { throw 'Host is not console-free' }
$copy = New-ScheduledTaskAction -Execute $action.Execute -Argument ($action.Arguments + ' "-Manual"') -WorkingDirectory $action.WorkingDirectory
$settings = New-ScheduledTaskSettingsSet -Hidden -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Seconds $TimeoutSeconds) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$started = Get-Date
try {
    Register-ScheduledTask -TaskName $name -Action $copy -Principal $task.Principal -Settings $settings | Out-Null
    Start-ScheduledTask -TaskName $name
    do {
        Start-Sleep -Seconds 3
        $current = Get-ScheduledTask -TaskName $name
        $info = Get-ScheduledTaskInfo -TaskName $name
        if ($info.LastRunTime -ge $started.AddSeconds(-1) -and $current.State -ne 'Running') { break }
    } while ((Get-Date) -lt $started.AddSeconds($TimeoutSeconds))
    $terminal = Get-Content (Join-Path $PlatformRoot 'logs\event-research-delivery.jsonl') -Tail 30 |
        ForEach-Object { $_ | ConvertFrom-Json } |
        Where-Object { $_.manual -and [datetimeoffset]$_.recorded_at -ge [datetimeoffset]$started } | Select-Object -Last 1
    $passed = ($current.State -ne 'Running' -and $info.LastTaskResult -eq 0 -and $terminal.status -eq 'completed')
    $path = Join-Path $PlatformRoot ('reports\event-delivery-acceptance-' + $started.ToString('yyyyMMddTHHmmss') + '.json')
    $result = [ordered]@{passed=$passed;tested_at=(Get-Date).ToString('o');task_exit_code=$info.LastTaskResult;terminal=$terminal;
        host=$action.Execute;scope='Actual scheduled host, fresh news/model/report and API readback; manual key does not complete scheduled slots'}
    [IO.File]::WriteAllText($path,($result | ConvertTo-Json -Depth 12),[Text.UTF8Encoding]::new($false))
    [pscustomobject]@{passed=$passed;evidence=$path;terminal=$terminal}
    if (-not $passed) { throw "Real news delivery failed: $path" }
} finally {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
}
