[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$ArchiveRoot = 'G:\StockPlatform\backups\legacy-cutover-20260910\tasks',
    [switch]$Apply
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$prefix = 'stock-brain-'
$tasks = @(Get-ScheduledTask | Where-Object { $_.TaskName.StartsWith($prefix, [StringComparison]::Ordinal) })
$enabled = @($tasks | Where-Object { $_.State -ne 'Disabled' })
if ($enabled.Count) {
    throw 'Refusing cleanup because one or more legacy stock-brain tasks are enabled: ' + (($enabled.TaskName | Sort-Object) -join ', ')
}

$stamp = Get-Date -Format 'yyyyMMddTHHmmss'
$archive = Join-Path ([IO.Path]::GetFullPath($ArchiveRoot)) $stamp
$records = @()
foreach ($task in ($tasks | Sort-Object TaskName)) {
    $info = Get-ScheduledTaskInfo -TaskName $task.TaskName -TaskPath $task.TaskPath
    $records += [ordered]@{
        task_name = $task.TaskName
        task_path = $task.TaskPath
        state = [string]$task.State
        last_run_time = $info.LastRunTime.ToString('o')
        last_task_result = $info.LastTaskResult
        xml_file = ($task.TaskName + '.xml')
    }
}

$receipt = [ordered]@{
    contract = 'legacy-stock-brain-task-cleanup-v1'
    generated_at = (Get-Date).ToString('o')
    apply_requested = [bool]$Apply
    prefix = $prefix
    count = $records.Count
    tasks = $records
}

if ($Apply -and $tasks.Count) {
    New-Item -ItemType Directory -Force -Path $archive | Out-Null
    foreach ($task in ($tasks | Sort-Object TaskName)) {
        Export-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath |
            Set-Content -LiteralPath (Join-Path $archive ($task.TaskName + '.xml')) -Encoding utf8
    }
    $receipt | ConvertTo-Json -Depth 5 |
        Set-Content -LiteralPath (Join-Path $archive 'manifest.json') -Encoding utf8
    foreach ($task in $tasks) {
        if ($PSCmdlet.ShouldProcess($task.TaskName, 'Unregister disabled legacy scheduled task')) {
            Unregister-ScheduledTask -TaskName $task.TaskName -TaskPath $task.TaskPath -Confirm:$false
        }
    }
}

$remaining = @(Get-ScheduledTask | Where-Object { $_.TaskName.StartsWith($prefix, [StringComparison]::Ordinal) })
$receipt['archive'] = if ($Apply -and $tasks.Count) { $archive } else { $null }
$receipt['remaining'] = @($remaining | ForEach-Object { $_.TaskName } | Sort-Object)
$receipt['status'] = if ($Apply -and $remaining.Count -eq 0) { 'removed' } elseif ($Apply) { 'failed_readback' } else { 'preview' }
$receipt | ConvertTo-Json -Depth 5
if ($Apply -and $remaining.Count) { exit 2 }
