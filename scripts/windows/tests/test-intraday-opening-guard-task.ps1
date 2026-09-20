$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$installer = Join-Path $root 'scripts\windows\install-intraday-opening-guard-task.ps1'
$runner = Join-Path $root 'scripts\windows\run-intraday-opening-guard.ps1'
$cli = Join-Path $root 'scripts\intraday-opening-guard.py'
foreach ($path in @($installer,$runner,$cli)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing $path" }
}
$installerSource = [IO.File]::ReadAllText($installer,[Text.Encoding]::UTF8)
$runnerSource = [IO.File]::ReadAllText($runner,[Text.Encoding]::UTF8)
$cliSource = [IO.File]::ReadAllText($cli,[Text.Encoding]::UTF8)
foreach ($path in @($installer,$runner)) {
    $errors = $null
    [void][Management.Automation.Language.Parser]::ParseFile($path,[ref]$null,[ref]$errors)
    if ($errors -and $errors.Count) { throw "Parse error in ${path}: $($errors[0].Message)" }
}
function Assert-True([bool]$Condition,[string]$Message) {
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

Assert-True ($installerSource.Contains("'trading-hareness-intraday-opening-guard'")) 'task identity must be stable'
Assert-True ($installerSource.Contains("[string]`$PreOpenTime = '09:25'") -and
             $installerSource.Contains("[string]`$LiveTime = '09:32'")) 'both opening gates must be explicit'
Assert-True ($installerSource.Contains('New-HiddenPowerShellTaskAction')) 'task must use the console-free host'
Assert-True ($installerSource.Contains('-Hidden -MultipleInstances IgnoreNew')) 'task must be hidden and single-instance'
Assert-True (-not $installerSource.Contains('-StartWhenAvailable')) 'a late login must not replay an obsolete opening receipt'
Assert-True ($runnerSource.Contains("'trading-hareness-dashboard-runtime'")) 'one bounded recovery must target the production runtime'
Assert-True (($runnerSource | Select-String 'Start-ScheduledTask -TaskName' -AllMatches).Matches.Count -eq 1) 'recovery must not loop'
Assert-True ($runnerSource.Contains('intraday-opening-guard.jsonl')) 'every task run must leave a durable receipt'
Assert-True ($cliSource.Contains('--notify') -and $cliSource.Contains('post_feishu_alert_card')) 'final verdict must use the real Feishu transport'
Assert-True ($cliSource.Contains('sse_calendar_status')) 'weekends and holidays must be calendar-gated'
[pscustomobject]@{ passed=$true; scope='opening guard schedule, hidden execution, bounded recovery and real notification contract' }
