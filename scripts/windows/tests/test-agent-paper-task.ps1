$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$name = 'trading-hareness-agent-paper-contract-' + [guid]::NewGuid().ToString('N')
try {
    & (Join-Path $root 'scripts\windows\install-agent-paper-trader-task.ps1') -RepositoryRoot $root -TaskName $name | Out-Null
    $task = Get-ScheduledTask -TaskName $name
    if ([IO.Path]::GetFileName($task.Actions[0].Execute) -ne 'stock-background-host.exe') { throw 'Not console-free' }
    if ($task.Triggers.Count -ne 1) { throw 'Expected one daily trigger' }
    if (([datetime]$task.Triggers[0].StartBoundary).ToString('HH:mm') -ne '09:20') { throw 'Expected 09:20 start' }
    if ($task.Triggers[0].Repetition.Interval -ne 'PT10M' -or $task.Triggers[0].Repetition.Duration -ne 'PT5H50M') { throw 'Wrong restart window' }
    if ($task.Settings.MultipleInstances -ne 'IgnoreNew') { throw 'Must not start a second day loop' }
    if ($task.Settings.ExecutionTimeLimit -ne 'PT6H') { throw 'Expected bounded execution' }
    if ($task.Principal.LogonType -ne 'Interactive') { throw 'CLI login requires the interactive user' }
    Write-Output 'Agent paper task contract passed (registered/read back only, no model invocation)'
} finally {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
}
