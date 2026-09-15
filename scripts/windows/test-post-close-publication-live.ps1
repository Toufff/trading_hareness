[CmdletBinding()]
param(
    [Parameter(Mandatory)][ValidatePattern('^\d{4}-\d{2}-\d{2}$')][string]$TradeDate,
    [string]$PlatformRoot = 'G:\StockPlatform',
    [int]$TimeoutSeconds = 900
)
# Real production entry-point acceptance, not a helper that repairs files before checking.
# Uses a disposable non-recurring task; the normal schedule is never edited.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$original = Get-ScheduledTask -TaskName 'trading-hareness-post-close-pipeline'
if ($original.State -eq 'Running') { throw 'Production close task is running; avoid concurrent publication' }
$action = $original.Actions[0]
if ([IO.Path]::GetFileName($action.Execute) -ne 'stock-background-host.exe') {
    throw 'Production task does not use the required console-free host'
}
$name = 'trading-hareness-acceptance-publication-' + [guid]::NewGuid().ToString('N')
$copy = New-ScheduledTaskAction -Execute $action.Execute -Argument ($action.Arguments + ' "-TradeDate" "' + $TradeDate + '" "-Force"')
$settings = New-ScheduledTaskSettingsSet -Hidden -ExecutionTimeLimit (New-TimeSpan -Seconds $TimeoutSeconds) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$started = Get-Date
$evidence = Join-Path $PlatformRoot ('reports\publication-acceptance-' + $started.ToString('yyyyMMddTHHmmss') + '.json')
try {
    Register-ScheduledTask -TaskName $name -Action $copy -Principal $original.Principal -Settings $settings | Out-Null
    Start-ScheduledTask -TaskName $name
    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        Start-Sleep -Seconds 3
        $task = Get-ScheduledTask -TaskName $name
        $info = Get-ScheduledTaskInfo -TaskName $name
        if ($info.LastRunTime -ge $started.AddSeconds(-1) -and $task.State -ne 'Running') { break }
    } while ((Get-Date) -lt $deadline)
    if ($task.State -eq 'Running') { throw 'Full production entry-point acceptance timed out' }
    $records = @(Get-Content (Join-Path $PlatformRoot 'logs\post-close-pipeline.jsonl') -Tail 100 |
        ForEach-Object { $_ | ConvertFrom-Json } | Where-Object {
            $_.trading_date -eq $TradeDate -and [datetimeoffset]$_.recorded_at -ge [datetimeoffset]$started
        })
    $terminal = @($records | Where-Object { $_.status -in @('completed','failed','partial') }) | Select-Object -Last 1
    $receiptPath = Join-Path $PlatformRoot ('reports\short-term\' + $TradeDate + '_report_publication.json')
    $receipt = if (Test-Path $receiptPath) { Get-Content $receiptPath -Raw | ConvertFrom-Json } else { $null }
    $passed = ($info.LastTaskResult -eq 0 -and $null -ne $terminal -and $terminal.status -eq 'completed' -and
        $null -ne $receipt -and $receipt.passed -and (Get-Item $receiptPath).LastWriteTime -ge $started)
    $result = [ordered]@{ passed=$passed; tested_at=(Get-Date).ToString('o'); trade_date=$TradeDate;
        actual_scheduled_host=$action.Execute; task_exit_code=$info.LastTaskResult;
        production_schedule_unchanged=$true; terminal=$terminal; publication=$receipt;
        scope='Full production scheduled entry using settled historical date; not next scheduled execution or profitability' }
    [IO.File]::WriteAllText($evidence, ($result | ConvertTo-Json -Depth 20), [Text.UTF8Encoding]::new($false))
    [pscustomobject]@{passed=$passed; task_exit_code=$info.LastTaskResult; evidence=$evidence}
    if (-not $passed) { throw "Full production entry-point acceptance failed: $evidence" }
} finally {
    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
}
