$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$name = 'trading-hareness-event-delivery-contract-' + [guid]::NewGuid().ToString('N')
try {
    & (Join-Path $root 'scripts\windows\install-event-research-delivery-task.ps1') -RepositoryRoot $root -TaskName $name | Out-Null
    $task = Get-ScheduledTask -TaskName $name
    if ([IO.Path]::GetFileName($task.Actions[0].Execute) -ne 'stock-background-host.exe') { throw 'Not console-free' }
    if ($task.Triggers.Count -ne 3) { throw 'Expected three daily calendar-gated candidate triggers' }
    $times = @($task.Triggers | ForEach-Object { ([datetime]$_.StartBoundary).ToString('HH:mm') })
    if (($times -join ',') -ne '08:55,11:55,21:55') { throw 'Incorrect target lead times' }
    if (@($task.Triggers | Where-Object DaysInterval -eq 1).Count -ne 3) { throw 'Expected daily candidates, not weekday guesses' }
    foreach ($trigger in $task.Triggers) {
        if ($trigger.Repetition.Interval -ne 'PT10M' -or $trigger.Repetition.Duration -ne 'PT40M') { throw 'Unbounded or wrong retry window' }
    }
    if ($task.Settings.ExecutionTimeLimit -ne 'PT8M') { throw 'Expected bounded execution' }
    Write-Output 'News delivery task contract passed (registered/read back only, no model invocation)'
} finally {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
}
