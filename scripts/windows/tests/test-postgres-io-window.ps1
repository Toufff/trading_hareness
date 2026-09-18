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

[pscustomobject]@{
    passed = $true
    scope = 'Pure window-mode rule extracted from set-postgres-io-window.ps1; no process priority was changed and no database was touched'
    boundaries_checked = @('09:00', '15:40', '16:30', '23:00', '04:00', '08:00')
}
