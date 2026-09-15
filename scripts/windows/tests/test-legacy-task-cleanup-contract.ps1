$ErrorActionPreference = 'Stop'
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$body = Get-Content -Raw -LiteralPath (Join-Path $root 'scripts\windows\remove-legacy-stock-brain-tasks.ps1')
foreach ($required in @(
    "`$prefix = 'stock-brain-'",
    '$enabled = @($tasks | Where-Object',
    'Export-ScheduledTask',
    'Unregister-ScheduledTask',
    'ForEach-Object { $_.TaskName }',
    "contract = 'legacy-stock-brain-task-cleanup-v1'",
    "status'] = if (`$Apply -and `$remaining.Count -eq 0) { 'removed' }"
)) {
    if (-not $body.Contains($required)) { throw "Missing cleanup invariant: $required" }
}
if ($body -match "TaskName\s+-like\s+'\*stock") { throw 'Cleanup scope is not exact-prefix bounded' }
'legacy task cleanup contract passed'
