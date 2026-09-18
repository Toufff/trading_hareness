[CmdletBinding()]
param(
    [string]$PlatformRoot = 'G:\StockPlatform',
    # Print the decision and the per-process plan without touching any process
    # priority or writing the log. Used by the contract checks and by an
    # operator verifying the window boundaries.
    [switch]$WhatIf,
    # Force a mode instead of deriving it from the clock (operator override for
    # a one-off backfill); the scheduled task never passes it.
    [ValidateSet('', 'normal', 'background')][string]$Mode = ''
)

# Keep PostgreSQL out of the operator's way outside the hours that matter.
#
# The workstation is also the owner's desktop machine. During the exchange
# session, the evening review and the 04:00-08:00 maintenance window the
# database must run at full speed; the rest of the day a long backfill or a
# tiering pass should yield CPU and, more importantly, disk I/O rather than
# make the machine feel slow. Windows has no per-database I/O scheduler, so the
# knobs are the process priority class and the (undocumented but stable)
# process I/O priority.
#
# Idempotent: it only changes what is not already right and only logs when
# something actually changed.

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Get-PostgresIoWindowMode {
    <#
        Pure clock rule, half-open intervals [start, end):

          normal      Mon-Fri 09:00-15:40  exchange session and post-close cross
                      daily    16:30-23:00  post-close pipeline and owner review
                      daily    04:00-08:00  backup / off-site / tiering window
          background  everything else

        Times are the machine's local time, which is Asia/Shanghai on this host
        (the same assumption run-post-close-pipeline.ps1 makes).
    #>
    param([Parameter(Mandatory)][DateTime]$Now)
    $minutes = $Now.Hour * 60 + $Now.Minute
    $weekday = [int]$Now.DayOfWeek -ge 1 -and [int]$Now.DayOfWeek -le 5
    if ($weekday -and $minutes -ge 540 -and $minutes -lt 940) { return 'normal' }   # 09:00-15:40
    if ($minutes -ge 990 -and $minutes -lt 1380) { return 'normal' }                # 16:30-23:00
    if ($minutes -ge 240 -and $minutes -lt 480) { return 'normal' }                 # 04:00-08:00
    return 'background'
}

function Get-PostgresIoWindowTargets {
    param([Parameter(Mandatory)][ValidateSet('normal', 'background')][string]$WindowMode)
    if ($WindowMode -eq 'normal') {
        return [pscustomobject]@{ PriorityClass = 'Normal'; IoPriority = 2 }
    }
    return [pscustomobject]@{ PriorityClass = 'BelowNormal'; IoPriority = 1 }
}

function Set-ProcessIoPriority {
    <#
        ProcessIoPriority (class 33) is undocumented but stable across every
        Windows release this platform runs on. A failure here is not fatal: the
        priority class alone already covers most of the interactive impact.
    #>
    param([Parameter(Mandatory)][IntPtr]$Handle, [Parameter(Mandatory)][int]$Priority)
    if (-not ('StockPlatform.NativeProcessPriority' -as [type])) {
        Add-Type -Namespace 'StockPlatform' -Name 'NativeProcessPriority' -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("ntdll.dll", SetLastError = true)]
public static extern int NtSetInformationProcess(System.IntPtr processHandle, int processInformationClass, ref int processInformation, int processInformationLength);
'@ | Out-Null
    }
    $value = $Priority
    $status = [StockPlatform.NativeProcessPriority]::NtSetInformationProcess($Handle, 33, [ref]$value, 4)
    if ($status -ne 0) { throw "NtSetInformationProcess(ProcessIoPriority) returned 0x$($status.ToString('X8'))" }
}

$platform = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
$logPath = Join-Path $platform 'logs\postgres-io-window.jsonl'
$now = Get-Date
$windowMode = if ($Mode) { $Mode } else { Get-PostgresIoWindowMode -Now $now }
$targets = Get-PostgresIoWindowTargets -WindowMode $windowMode

$previousMode = ''
if (Test-Path -LiteralPath $logPath -PathType Leaf) {
    try {
        $lastLine = Get-Content -LiteralPath $logPath -Tail 1 -ErrorAction Stop
        if ($lastLine) { $previousMode = [string]((($lastLine | ConvertFrom-Json)).mode) }
    } catch { $previousMode = '' }
}

$changed = @()
$failures = @()
$ioPriorityErrors = @()
$ioPriorityApplied = 0
$inspected = 0
# The priority CLASS and the I/O priority are two independent knobs and are
# decided independently.
#
# The class can be read back, so it is only assigned when it differs. The I/O
# priority cannot: there is no supported way to query ProcessIoPriority, so the
# only way to know it is right is to set it. Skipping the call whenever the
# class already matched is what let a single failed NtSetInformationProcess --
# a hardened ntdll, a handle the run could not open, an OS update moving the
# information class -- persist silently forever: every later run saw the class
# already at BelowNormal, returned early, and reported nothing. Re-issuing it is
# one cheap, idempotent syscall per postgres.exe every fifteen minutes.
foreach ($process in @(Get-Process -Name 'postgres' -ErrorAction SilentlyContinue)) {
    $inspected++
    try {
        $current = [string]$process.PriorityClass
        $classNeedsChange = ($current -ne $targets.PriorityClass)
        if ($WhatIf) {
            if ($classNeedsChange) {
                $changed += [pscustomobject]@{ pid = $process.Id; from = $current; to = $targets.PriorityClass; applied = $false }
            }
            continue
        }
        if ($classNeedsChange) {
            $process.PriorityClass = [Diagnostics.ProcessPriorityClass]$targets.PriorityClass
        }
        $ioPriorityError = ''
        try {
            Set-ProcessIoPriority -Handle $process.Handle -Priority ([int]$targets.IoPriority)
            $ioPriorityApplied++
        } catch {
            $ioPriorityError = $_.Exception.Message
            $ioPriorityErrors += [pscustomobject]@{ pid = $process.Id; io_priority = $targets.IoPriority; error = $ioPriorityError }
        }
        # A run that only re-issued an I/O priority that was already right is not
        # a transition and stays out of the log; a run that failed to set it is,
        # and is carried by $ioPriorityErrors into $shouldLog below.
        if ($classNeedsChange -or $ioPriorityError) {
            $changed += [pscustomobject]@{ pid = $process.Id; from = $current; to = $targets.PriorityClass; applied = $true; io_priority_error = $ioPriorityError }
        }
    } catch {
        # A backend can exit between the enumeration and the assignment; that is
        # normal and must not fail the run.
        $failures += [pscustomobject]@{ pid = $process.Id; error = $_.Exception.Message }
    }
}

$record = [ordered]@{
    recorded_at = [DateTimeOffset]::Now.ToString('o')
    mode = $windowMode
    previous_mode = $previousMode
    priority_class = $targets.PriorityClass
    io_priority = $targets.IoPriority
    postgres_processes = $inspected
    io_priority_applied = $ioPriorityApplied
    io_priority_errors = @($ioPriorityErrors)
    changed = @($changed)
    failures = @($failures)
    forced_mode = [bool]$Mode
    what_if = [bool]$WhatIf
}

# Only a real transition is worth a line: this runs every 15 minutes and a
# steady-state entry every quarter hour would bury the transitions.
#
# io_priority_errors is one of the conditions on purpose. It is the only signal
# that the knob the header calls the point of this script is not being set at
# all, and it recurs every run rather than once, so an operator reading the log
# sees a standing failure instead of silence.
$shouldLog = (-not $WhatIf) -and (($changed.Count -gt 0) -or ($failures.Count -gt 0) -or
    ($ioPriorityErrors.Count -gt 0) -or ($previousMode -ne $windowMode))
if ($shouldLog) {
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $logPath) | Out-Null
        [IO.File]::AppendAllText($logPath, (ConvertTo-Json -InputObject $record -Depth 6 -Compress) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    } catch {
        Write-Warning "Failed to write postgres I/O window record: $($_.Exception.Message)"
    }
}

[pscustomobject]$record
