[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

# set-postgres-io-window.ps1's window rule is pure (no process, no filesystem),
# so it is extracted from the script's own AST rather than duplicated here or
# exercised by actually re-prioritising the running database.
$scriptPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'set-postgres-io-window.ps1'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw "Missing $scriptPath" }
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$null, [ref]$parseErrors)
if ($parseErrors -and $parseErrors.Count -gt 0) { throw "Failed to parse $scriptPath" }
foreach ($name in 'Get-PostgresIoWindowMode', 'Get-PostgresIoWindowTargets') {
    $functionAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    if (-not $functionAst) { throw "$name function not found in $scriptPath" }
    . ([scriptblock]::Create($functionAst.Extent.Text))
}

function At([string]$Date, [string]$Time) {
    return [DateTime]::ParseExact("$Date $Time", 'yyyy-MM-dd HH:mm', $null)
}

# 2026-09-16 is a Wednesday, 2026-09-19 a Saturday, 2026-09-20 a Sunday.
$wednesday = '2026-09-16'
$saturday = '2026-09-19'
$sunday = '2026-09-20'

# --- weekday exchange session 09:00-15:40 ---------------------------------
Assert-True ((Get-PostgresIoWindowMode -Now (At $wednesday '08:59')) -eq 'background') '08:59 on a weekday is outside every window'
Assert-True ((Get-PostgresIoWindowMode -Now (At $wednesday '09:00')) -eq 'normal') 'the session window starts exactly at 09:00'
Assert-True ((Get-PostgresIoWindowMode -Now (At $wednesday '13:30')) -eq 'normal') 'mid-session must stay at normal priority'
Assert-True ((Get-PostgresIoWindowMode -Now (At $wednesday '15:39')) -eq 'normal') '15:39 is still inside the session window'
Assert-True ((Get-PostgresIoWindowMode -Now (At $wednesday '15:40')) -eq 'background') 'the session window ends at 15:40 (half-open)'
Assert-True ((Get-PostgresIoWindowMode -Now (At $wednesday '16:00')) -eq 'background') 'the gap between 15:40 and 16:30 yields to the operator'

# --- the session window is weekday-only -----------------------------------
foreach ($weekend in @($saturday, $sunday)) {
    Assert-True ((Get-PostgresIoWindowMode -Now (At $weekend '09:00')) -eq 'background') 'there is no exchange session at the weekend'
    Assert-True ((Get-PostgresIoWindowMode -Now (At $weekend '13:30')) -eq 'background') 'weekend afternoons belong to the operator'
}

# --- evening review window 16:30-23:00, every day -------------------------
foreach ($day in @($wednesday, $saturday, $sunday)) {
    Assert-True ((Get-PostgresIoWindowMode -Now (At $day '16:29')) -eq 'background') '16:29 is before the review window'
    Assert-True ((Get-PostgresIoWindowMode -Now (At $day '16:30')) -eq 'normal') 'the review window starts exactly at 16:30'
    Assert-True ((Get-PostgresIoWindowMode -Now (At $day '22:59')) -eq 'normal') '22:59 is still inside the review window'
    Assert-True ((Get-PostgresIoWindowMode -Now (At $day '23:00')) -eq 'background') 'the review window ends at 23:00 (half-open)'
}

# --- maintenance window 04:00-08:00, every day ----------------------------
foreach ($day in @($wednesday, $saturday, $sunday)) {
    Assert-True ((Get-PostgresIoWindowMode -Now (At $day '03:59')) -eq 'background') '03:59 is before the maintenance window'
    Assert-True ((Get-PostgresIoWindowMode -Now (At $day '04:00')) -eq 'normal') 'the maintenance window starts at 04:00 with the 04:10 backup'
    Assert-True ((Get-PostgresIoWindowMode -Now (At $day '06:00')) -eq 'normal') 'the 06:00 storage-tier job runs at full speed'
    Assert-True ((Get-PostgresIoWindowMode -Now (At $day '07:59')) -eq 'normal') '07:59 is still inside the maintenance window'
    Assert-True ((Get-PostgresIoWindowMode -Now (At $day '08:00')) -eq 'background') 'the maintenance window ends at 08:00 (half-open)'
    Assert-True ((Get-PostgresIoWindowMode -Now (At $day '00:30')) -eq 'background') 'the small hours are background time'
}

# --- the mode maps to one priority pair, never the other way round --------
$normal = Get-PostgresIoWindowTargets -WindowMode 'normal'
$background = Get-PostgresIoWindowTargets -WindowMode 'background'
Assert-True ($normal.PriorityClass -eq 'Normal' -and [int]$normal.IoPriority -eq 2) 'normal mode means Normal priority class and normal I/O priority'
Assert-True ($background.PriorityClass -eq 'BelowNormal' -and [int]$background.IoPriority -eq 1) 'background mode means BelowNormal priority class and low I/O priority'
foreach ($class in @($normal.PriorityClass, $background.PriorityClass)) {
    Assert-True ($null -ne ($class -as [Diagnostics.ProcessPriorityClass])) "$class must be a real ProcessPriorityClass value"
}

# --- the script never raises PostgreSQL above Normal ----------------------
$source = [IO.File]::ReadAllText($scriptPath, [Text.Encoding]::UTF8)
foreach ($forbidden in 'RealTime', 'AboveNormal', 'High') {
    Assert-True ($source -notmatch "PriorityClass\s*=\s*'$forbidden'") "the I/O window must never set $forbidden priority on a database process"
}
Assert-True ($source -match 'NtSetInformationProcess') 'the I/O priority call must be present'
Assert-True ($source -match 'try\s*\{[^}]*Set-ProcessIoPriority') 'the undocumented I/O priority call must be best-effort, wrapped in try/catch'

# --- the class and the I/O priority are decided independently --------------
# The class can be read back; the I/O priority cannot (there is no supported
# ProcessIoPriority query), so the only way to know it is right is to set it.
# Returning early whenever the class already matched is what let one failed
# NtSetInformationProcess persist forever: every later run saw BelowNormal,
# skipped the I/O call and reported 'changed: []'.
Assert-True ($source -notmatch "if \(\`$current -eq \`$targets\.PriorityClass\) \{ continue \}") `
    'a matching priority class must no longer skip the rest of the loop body'
Assert-True ($source -match "\`$classNeedsChange = \(\`$current -ne \`$targets\.PriorityClass\)") `
    'the priority-class decision must be a value the loop can act on separately'
Assert-True ($source -match 'if \(\$classNeedsChange\) \{\s*\r?\n?\s*\$process\.PriorityClass =') `
    'the class must only be assigned when it actually differs'

# The I/O priority call must sit OUTSIDE that condition, so it is re-issued on
# every run of every process regardless of the class.
$classGuard = [regex]::Match($source, 'if \(\$classNeedsChange\) \{\s*\r?\n?\s*\$process\.PriorityClass =')
$ioCall = [regex]::Match($source, 'Set-ProcessIoPriority -Handle \$process\.Handle')
Assert-True ($ioCall.Success -and $ioCall.Index -gt $classGuard.Index) 'the I/O priority must be set after the class decision, not inside it'
$betweenGuardAndCall = $source.Substring($classGuard.Index, $ioCall.Index - $classGuard.Index)
Assert-True ($betweenGuardAndCall -match '\}') 'the class-change block must be closed before the I/O priority is re-issued'

# A persistent failure has to keep showing up. The log is written only on a
# transition, so io_priority_errors is one of the transition conditions.
Assert-True ($source -match '\$ioPriorityErrors\.Count -gt 0') 'a failed I/O priority must force a log line every run, not be swallowed as steady state'
$logCondition = [regex]::Match($source, '(?s)\$shouldLog = .*?\r?\n\s*if \(\$shouldLog\)').Value
Assert-True ($logCondition -match 'io_priority_error|ioPriorityErrors') 'io_priority_error must be part of the log condition'
Assert-True ($source -match 'io_priority_errors = @\(\$ioPriorityErrors\)') 'the run record must carry the per-process I/O priority failures'
Assert-True ($source -match 'io_priority_applied = \$ioPriorityApplied') 'the run record must say how many processes the I/O priority was re-issued on'

# --- the loop, executed against fake processes ------------------------------
# The decision the fix is about is "did this run reach the I/O call at all",
# which is a property of the loop rather than of a pure function, so the loop is
# reproduced here from the script's own source with the two side effects
# replaced. No real postgres.exe is touched.
$loopMatch = [regex]::Match($source, "(?s)foreach \(\`$process in @\(Get-Process -Name 'postgres'.*?\r?\n\}\r?\n")
Assert-True ($loopMatch.Success) 'the per-process loop must be extractable for exercise'
$loopBody = $loopMatch.Value -replace "@\(Get-Process -Name 'postgres' -ErrorAction SilentlyContinue\)", '$fakeProcesses'
# Safety: if that substitution ever stops matching, the extracted loop would run
# against the real postgres.exe processes of the live cluster and re-prioritise
# them from a test. Refuse rather than "pass".
Assert-True ($loopBody -notmatch 'Get-Process') 'the extracted loop must be detached from the real postgres processes before it is run'
Assert-True ($loopBody -match '\$fakeProcesses') 'the extracted loop must iterate the fake processes'

function Invoke-IoWindowLoop {
    param([Parameter(Mandatory)]$Processes, [Parameter(Mandatory)]$Targets, [bool]$AsWhatIf = $false, [bool]$IoFails = $false)
    $script:IoCalls = 0
    $targets = $Targets
    $WhatIf = $AsWhatIf
    $fakeProcesses = $Processes
    function Set-ProcessIoPriority {
        param($Handle, $Priority)
        $script:IoCalls++
        if ($script:IoShouldFail) { throw 'NtSetInformationProcess(ProcessIoPriority) returned 0xC0000022' }
    }
    $script:IoShouldFail = $IoFails
    $changed = @(); $failures = @(); $ioPriorityErrors = @(); $ioPriorityApplied = 0; $inspected = 0
    . ([scriptblock]::Create($loopBody))
    return [pscustomobject]@{
        Changed = @($changed); Failures = @($failures); IoErrors = @($ioPriorityErrors)
        IoApplied = $ioPriorityApplied; Inspected = $inspected; IoCalls = $script:IoCalls
    }
}

function New-FakeProcess([string]$Class) {
    # PriorityClass is settable; Handle is whatever the stub is handed.
    return [pscustomobject]@{ Id = 4242; PriorityClass = $Class; Handle = [IntPtr]::Zero }
}
$backgroundTargets = Get-PostgresIoWindowTargets -WindowMode 'background'

# Already at the target class: the old code returned here and never set the I/O
# priority again. It must now still be issued, and still not log anything.
$steady = Invoke-IoWindowLoop -Processes @((New-FakeProcess 'BelowNormal')) -Targets $backgroundTargets
Assert-True ($steady.IoCalls -eq 1) 'the I/O priority must be re-issued even when the priority class already matches'
Assert-True ($steady.IoApplied -eq 1) 'a successful re-issue must be counted'
Assert-True ($steady.Changed.Count -eq 0) 'a run that only re-issued a correct I/O priority is not a transition and must not be logged'

# The same steady state, but the I/O call keeps failing: it must be visible.
$broken = Invoke-IoWindowLoop -Processes @((New-FakeProcess 'BelowNormal')) -Targets $backgroundTargets -IoFails $true
Assert-True ($broken.IoCalls -eq 1 -and $broken.IoApplied -eq 0) 'a failed re-issue must not be counted as applied'
Assert-True ($broken.IoErrors.Count -eq 1) 'a persistent I/O priority failure must be recorded every run, not once'
Assert-True ($broken.Changed.Count -eq 1 -and $broken.Changed[0].io_priority_error) 'the failure must be attached to the process it happened on'

# A real class transition still assigns the class and sets the I/O priority.
$transition = Invoke-IoWindowLoop -Processes @((New-FakeProcess 'Normal')) -Targets $backgroundTargets
Assert-True ($transition.Changed.Count -eq 1 -and $transition.Changed[0].from -eq 'Normal' -and $transition.Changed[0].to -eq 'BelowNormal') `
    'a class transition must still be recorded with both ends'
Assert-True ($transition.IoCalls -eq 1) 'a class transition must set the I/O priority too'

# -WhatIf must reach neither knob.
$planned = Invoke-IoWindowLoop -Processes @((New-FakeProcess 'Normal')) -Targets $backgroundTargets -AsWhatIf $true
Assert-True ($planned.IoCalls -eq 0) '-WhatIf must not issue the I/O priority call'
Assert-True ($planned.Changed.Count -eq 1 -and -not $planned.Changed[0].applied) '-WhatIf must report the plan without applying it'
$plannedSteady = Invoke-IoWindowLoop -Processes @((New-FakeProcess 'BelowNormal')) -Targets $backgroundTargets -AsWhatIf $true
Assert-True ($plannedSteady.Changed.Count -eq 0 -and $plannedSteady.IoCalls -eq 0) '-WhatIf on an already-correct process plans nothing'

[pscustomobject]@{
    passed = $true
    scope = 'Pure window-mode rule extracted from set-postgres-io-window.ps1, plus its per-process loop exercised against fake processes with a stubbed I/O priority call; no process priority was changed and no database was touched'
    boundaries_checked = @('09:00', '15:40', '16:30', '23:00', '04:00', '08:00')
    io_priority_reissued_when_class_unchanged = $true
    io_priority_failure_logged_every_run = $true
}
