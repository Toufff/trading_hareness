$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Static contract for the adjustment-factor maintenance task.
#
# No scheduled task is registered, no database is touched and no provider is
# called: this asserts the properties that decide whether the installed task
# behaves correctly at 04:30 on a host nobody is watching.

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$installer = Join-Path $root 'scripts\windows\install-adjustment-factor-task.ps1'
$runner = Join-Path $root 'scripts\windows\run-adjustment-factor-maintenance.ps1'
$cli = Join-Path $root 'scripts\adjustment-factor-maintenance.py'
$lane = Join-Path $root 'quant-service\app\adjustment_factor_maintenance.py'
foreach ($path in @($installer, $runner, $cli, $lane)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" }
}
$installerSource = [IO.File]::ReadAllText($installer, [Text.Encoding]::UTF8)
$runnerSource = [IO.File]::ReadAllText($runner, [Text.Encoding]::UTF8)
$cliSource = [IO.File]::ReadAllText($cli, [Text.Encoding]::UTF8)
$laneSource = [IO.File]::ReadAllText($lane, [Text.Encoding]::UTF8)

$receipts = [ordered]@{}
function Assert-True([string]$Name, [bool]$Condition, [string]$Message) {
    $receipts[$Name] = $Condition
    if (-not $Condition) { throw "Assertion failed ($Name): $Message" }
}

# --- both files parse -------------------------------------------------------
foreach ($path in @($installer, $runner)) {
    $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($path, [ref]$null, [ref]$errors)
    if ($errors -and $errors.Count) { throw "Parse errors in ${path}: $($errors[0].Message)" }
}
Assert-True 'both_scripts_parse' $true 'PowerShell AST parse produced no errors'

# --- the installed task's identity and schedule -----------------------------
$parameters = (Get-Command $installer).Parameters
$defaults = @{}
$ast = [Management.Automation.Language.Parser]::ParseFile($installer, [ref]$null, [ref]$null)
foreach ($p in $ast.ParamBlock.Parameters) {
    $defaults[$p.Name.VariablePath.UserPath] = if ($p.DefaultValue) { $p.DefaultValue.ToString().Trim("'") } else { $null }
}
Assert-True 'task_name_is_pinned' ($defaults['TaskName'] -eq 'trading-hareness-adjustment-factors') `
    "TaskName default is '$($defaults['TaskName'])'"
Assert-True 'runs_daily_at_0430' ($defaults['StartTime'] -eq '04:30' -and $installerSource -match 'New-ScheduledTaskTrigger -Daily -At \$StartTime') `
    'The trigger must be a plain daily trigger at 04:30 (inside the maintenance window)'
Assert-True 'lookback_defaults_to_30_days' ($defaults['LookbackDays'] -eq '30') `
    "LookbackDays default is '$($defaults['LookbackDays'])'"
Assert-True 'roots_default_to_the_published_release' (
    $defaults['RepositoryRoot'] -eq 'G:\StockPlatform\current' -and
    $defaults['HostRoot'] -eq 'G:\StockPlatform\current') `
    'RepositoryRoot/HostRoot must default to the published release, never a development worktree'
Assert-True 'installer_declares_repository_and_host_root' ($parameters.ContainsKey('RepositoryRoot') -and $parameters.ContainsKey('HostRoot')) `
    'Both roots must be overridable'

# --- hidden launcher pattern, same as every other installed task ------------
Assert-True 'uses_the_shared_hidden_launcher' (
    $installerSource -match 'Import-Module \(Join-Path \$PSScriptRoot ''background-process\.psm1''\) -Force' -and
    $installerSource -match 'New-HiddenPowerShellTaskAction -RepositoryRoot \$RepositoryRoot -HostRoot \$HostRoot -ScriptPath \$runner') `
    'The action must be built by New-HiddenPowerShellTaskAction (console-free background host)'
Assert-True 'task_settings_are_hidden_and_single_instance' (
    $installerSource -match '-Hidden' -and $installerSource -match '-MultipleInstances IgnoreNew') `
    'A maintenance-window task must be hidden and must never run twice at once'
Assert-True 'no_restart_on_failure' (
    $installerSource -notmatch '-RestartCount' -and
    $installerSource -notmatch '-RestartInterval' -and
    $installerSource -notmatch '\.Repetition') `
    'No restart-on-failure and no repetition: a down provider must not be hammered every few minutes'
Assert-True 'execution_time_limit_is_bounded' ($installerSource -match '-ExecutionTimeLimit \(New-TimeSpan -Minutes \d+\)') `
    'The task must carry a bounded execution time limit'

# --- what the runner actually launches --------------------------------------
Assert-True 'runner_is_what_the_task_launches' ($installerSource -match 'run-adjustment-factor-maintenance\.ps1') `
    'The installer must point at the runner in this repository'
Assert-True 'runner_uses_the_current_venv_python' (
    $runnerSource.Contains('Join-Path $root ''.venv\Scripts\python.exe''')) `
    'The runner must use the venv of the tree it was launched from (G:\StockPlatform\current for the task)'
Assert-True 'runner_calls_the_documented_cli_contract' (
    $runnerSource.Contains('''scripts\adjustment-factor-maintenance.py''') -and
    $runnerSource.Contains('''sync'', ''--lookback-days''') -and
    $runnerSource.Contains('''--env-file'', $RuntimeEnv')) `
    'The runner must call scripts/adjustment-factor-maintenance.py sync --lookback-days N --env-file ...'
Assert-True 'runner_loads_runtime_env_by_path_only' (
    $runnerSource.Contains('$RuntimeEnv = ') -and ($runnerSource -notmatch 'Get-Content.*runtime\.env')) `
    'The runner passes the env file PATH to Python, which loads it; no credential is read into PowerShell'

# --- the two operational properties that make it safe unattended ------------
Assert-True 'runner_clears_inherited_proxy_variables' (
    $runnerSource.Contains('foreach ($name in @(''http_proxy''') -and
    $runnerSource.Contains('SetEnvironmentVariable($name, $null, ''Process'')')) `
    'An inherited desktop proxy turns a working longhu route into a nightly false failure'
Assert-True 'runner_logs_to_a_dated_file_under_the_platform_logs' (
    $runnerSource.Contains('Join-Path $platform ''logs\adjustment-factors''') -and
    $runnerSource.Contains('(Get-Date).ToString(''yyyy-MM-dd'') + ''.log''')) `
    'Logs must land in G:\StockPlatform\logs\adjustment-factors\<date>.log'
Assert-True 'runner_propagates_the_cli_exit_code' (
    $runnerSource.Contains('if ($exitCode -ne 0) { exit $exitCode }')) `
    'The task must see the CLI exit code, not a swallowed success'

# --- the exit-code rule the schedule depends on -----------------------------
Assert-True 'coverage_blocked_dates_do_not_fail_the_run' (
    $cliSource.Contains('return 1 if result.get("status") == FAILED_STATUS else 0') -and
    $laneSource.Contains('COVERAGE_BLOCK_REASON') -and
    $laneSource.Contains('MAX_CONSECUTIVE_BLOCKED_RUNS = 5')) `
    'Only a provider error or exception may exit non-zero; a coverage-blocked date is skipped and retires after 5 runs'

[pscustomobject]@{
    passed = $true
    scope = 'Static contract for install-adjustment-factor-task.ps1 / run-adjustment-factor-maintenance.ps1; no task registered, no database, no provider call'
    receipts = [pscustomobject]$receipts
}
