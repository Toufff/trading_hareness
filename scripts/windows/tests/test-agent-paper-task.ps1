$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$name = 'trading-hareness-agent-paper-contract-' + [guid]::NewGuid().ToString('N')
$jevName = 'trading-hareness-agent-paper-jev-contract-' + [guid]::NewGuid().ToString('N')
$providerEnvFile = [IO.Path]::GetTempFileName()
try {
    & (Join-Path $root 'scripts\windows\install-agent-paper-trader-task.ps1') -RepositoryRoot $root -TaskName $name `
        -AccountKey 'agent-codex-sol-contract' -Backend codex_cli -Model 'gpt-6-sol' -ReasoningEffort high | Out-Null
    $task = Get-ScheduledTask -TaskName $name
    if ([IO.Path]::GetFileName($task.Actions[0].Execute) -ne 'stock-background-host.exe') { throw 'Not console-free' }
    if ($task.Triggers.Count -ne 1) { throw 'Expected one daily trigger' }
    if (([datetime]$task.Triggers[0].StartBoundary).ToString('HH:mm') -ne '09:20') { throw 'Expected 09:20 start' }
    if ($task.Triggers[0].Repetition.Interval -ne 'PT10M' -or $task.Triggers[0].Repetition.Duration -ne 'PT5H50M') { throw 'Wrong restart window' }
    if ($task.Settings.MultipleInstances -ne 'IgnoreNew') { throw 'Must not start a second day loop' }
    if ($task.Settings.ExecutionTimeLimit -ne 'PT6H') { throw 'Expected bounded execution' }
    if ($task.Principal.LogonType -ne 'Interactive') { throw 'CLI login requires the interactive user' }
    $arguments = $task.Actions[0].Arguments
    foreach ($expected in @('agent-codex-sol-contract','codex_cli','gpt-6-sol','high')) {
        if ($arguments -notlike "*$expected*") { throw "Task action omitted $expected" }
    }
    $missingProviderRejected = $false
    try {
        & (Join-Path $root 'scripts\windows\install-agent-paper-trader-task.ps1') -RepositoryRoot $root -TaskName $jevName `
            -AccountKey 'agent-jev-pilot' -Backend jev | Out-Null
    } catch {
        $missingProviderRejected = $_.Exception.Message -like '*ProviderEnvFile*'
    }
    if (-not $missingProviderRejected) { throw 'JEV task accepted an implicit provider configuration' }
    & (Join-Path $root 'scripts\windows\install-agent-paper-trader-task.ps1') -RepositoryRoot $root -TaskName $jevName `
        -AccountKey 'agent-jev-pilot' -Backend jev -ProviderEnvFile $providerEnvFile | Out-Null
    $jevTask = Get-ScheduledTask -TaskName $jevName
    if ([IO.Path]::GetFileName($jevTask.Actions[0].Execute) -ne 'stock-background-host.exe') { throw 'JEV task is not console-free' }
    if (([datetime]$jevTask.Triggers[0].StartBoundary).ToString('HH:mm') -ne '09:20') { throw 'Wrong JEV start' }
    if ($jevTask.Triggers[0].Repetition.Interval -ne 'PT10M') { throw 'Wrong JEV recovery cadence' }
    if ($jevTask.Settings.MultipleInstances -ne 'IgnoreNew') { throw 'JEV task can overlap' }
    foreach ($expected in @('agent-jev-pilot', 'jev', $providerEnvFile)) {
        if ($jevTask.Actions[0].Arguments -notlike "*$expected*") { throw "JEV task action omitted $expected" }
    }
    $runnerSource = Get-Content (Join-Path $root 'scripts\windows\run-agent-paper-trader.ps1') -Raw
    if ($runnerSource -notmatch "'--provider-env-file'" -or $runnerSource -notmatch '\$ProviderEnvFile') {
        throw 'JEV provider file is not forwarded to Python'
    }
    Write-Output 'Agent paper task contract passed (registered/read back only, no model invocation)'
} finally {
    Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $jevName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $providerEnvFile -Force -ErrorAction SilentlyContinue
}
