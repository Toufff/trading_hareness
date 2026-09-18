[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "Assertion failed: $Message" }
}

# The data-directory migration is a one-shot, irreversible-looking operation on
# the authoritative database, so its safety sequence is asserted statically
# instead of by running it: the script is parsed, its pure guard is executed,
# and the order of the dangerous operations in its body is checked.
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$scriptPath = Join-Path $root 'scripts\windows\migrate-postgres-data-directory.ps1'
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw "Missing $scriptPath" }
$parseErrors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$null, [ref]$parseErrors)
if ($parseErrors -and $parseErrors.Count -gt 0) {
    throw "Failed to parse $scriptPath : $(($parseErrors | ForEach-Object { $_.Message }) -join '; ')"
}
$source = [IO.File]::ReadAllText($scriptPath, [Text.Encoding]::UTF8)

# --- the declared switches --------------------------------------------------
$parameters = $ast.ParamBlock.Parameters | ForEach-Object { $_.Name.VariablePath.UserPath }
foreach ($name in 'WhatIf', 'Rollback', 'Force', 'TargetDataDir', 'KeepOldName', 'AcceptDataLoss') {
    Assert-True ($parameters -contains $name) "-$name must be a declared parameter"
}

# --- the trading-session guard is pure and is exercised here ----------------
$guardAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Test-TradingSession' }, $true)
Assert-True ($null -ne $guardAst) 'Test-TradingSession must exist'
. ([scriptblock]::Create($guardAst.Extent.Text))
function At([string]$Date, [string]$Time) { return [DateTime]::ParseExact("$Date $Time", 'yyyy-MM-dd HH:mm', $null) }
$wednesday = '2026-09-16'
$saturday = '2026-09-19'
Assert-True (Test-TradingSession -Now (At $wednesday '09:00')) 'the session guard must trigger at the open'
Assert-True (Test-TradingSession -Now (At $wednesday '11:15')) 'the session guard must trigger mid-morning'
Assert-True (Test-TradingSession -Now (At $wednesday '15:29')) 'the session guard must trigger until the close'
Assert-True (-not (Test-TradingSession -Now (At $wednesday '15:30'))) 'the session guard ends at 15:30'
Assert-True (-not (Test-TradingSession -Now (At $wednesday '08:59'))) 'the session guard does not cover the pre-open'
Assert-True (-not (Test-TradingSession -Now (At $saturday '11:15'))) 'there is no session at the weekend'

# The refusal itself must be wired to that guard and must require -Force.
Assert-True ($source -match '(?s)if \(Test-TradingSession[^}]*if \(-not \$Force\)[^}]*throw') `
    'a migration inside the exchange session must throw unless -Force is passed'

# --- ordering of the dangerous operations -----------------------------------
function Get-Position([string]$Pattern, [string]$What) {
    $match = [regex]::Match($source, $Pattern)
    Assert-True ($match.Success) "expected to find $What in the migration script"
    return $match.Index
}

# The functions are defined above the body they are called from, so ordering is
# asserted on the CALLS in the migration flow, not on the definitions.
$body = $source.Substring((Get-Position 'step 1/8 preflight' 'the preflight step marker'))
function Get-BodyPosition([string]$Pattern, [string]$What) {
    $match = [regex]::Match($body, $Pattern)
    Assert-True ($match.Success) "expected to find $What in the migration flow"
    return $match.Index
}

$stopRuntimes = Get-BodyPosition 'Stop-PlatformRuntimes' 'the runtime/watcher stop call'
$snapshotTaken = Get-BodyPosition '\$snapshot = Get-RowCountSnapshot' 'the row-count snapshot'
$stopPostgres = Get-BodyPosition 'Stop-PostgresCluster -DataDirectory' 'the PostgreSQL stop call'
$robocopy = Get-BodyPosition 'robocopy\.exe \$currentDataDir \$target' 'the robocopy call'
$copyVerified = Get-BodyPosition 'Copy verification failed' 'the copy verification'
$envSwitch = Get-BodyPosition "Set-StockPlatformEnvValue -Path \`$RuntimeEnv -Name 'PGDATA_DIR' -Value \`$target" 'the PGDATA_DIR switch'
$startPostgres = Get-BodyPosition 'Start-PostgresCluster -DataDirectory \$target' 'the start from the new directory'
$rename = Get-BodyPosition 'Rename-Item -LiteralPath \$currentDataDir' 'the rename of the old data directory'

Assert-True ($stopRuntimes -lt $stopPostgres) 'the dashboard watcher task must be stopped before PostgreSQL, or it restarts the server mid-migration'
# The smoke snapshot belongs INSIDE the outage. Taken in the preflight it counts
# rows while the owner API, the dashboard runtime, the shared-peer tunnels and
# the 04:10/05:10/06:00 jobs are all still writing, and any row they legitimately
# insert before the shutdown aborts a migration that worked -- in exactly the
# window this script's own header says those jobs fire in.
Assert-True ($stopRuntimes -lt $snapshotTaken) 'the row-count snapshot must be taken after the platform runtimes are stopped'
Assert-True ($snapshotTaken -lt $stopPostgres) 'the row-count snapshot must be taken immediately before the cluster stops, while it can still be queried'
$preflight = $source.Substring(0, (Get-Position 'step 2/8' 'the step 2 marker'))
Assert-True ($preflight -notmatch '\$snapshot = Get-RowCountSnapshot') 'the preflight must not snapshot row counts while the platform is still writing'
Assert-True ($stopPostgres -lt $robocopy) 'the cluster must be stopped before it is copied'
Assert-True ($robocopy -lt $copyVerified) 'the copy must be verified after it is made'
Assert-True ($copyVerified -lt $envSwitch) 'the copy must be verified before anything points PGDATA_DIR at it'
Assert-True ($envSwitch -lt $startPostgres) 'the configuration must be switched before the new cluster starts'
Assert-True ($startPostgres -lt $rename) 'the old directory is only renamed once the new one has started and verified'

# The watcher task is the specific hazard: it must be named explicitly.
Assert-True ($source -match 'trading-hareness-dashboard-runtime') 'the dashboard watcher task must be handled by name'
Assert-True ($source -match 'Stop-ScheduledTask -TaskName \$task') 'the watcher tasks must actually be stopped, not only disabled'

# --- the platform shutdown order --------------------------------------------
# Stop-ScheduledTask kills the task's whole job object at once, so running it
# before the graceful stop script means Request-RuntimeStop never writes its
# stop marker and the runtime-state file keeps claiming 'healthy' for a dead
# PID. publish-stock-release.ps1's Stop-ProductionRuntime documents the rule;
# the order here must be Disable -> graceful stop -> Stop (backstop).
$stopFunction = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Stop-PlatformRuntimes' }, $true)
Assert-True ($null -ne $stopFunction) 'Stop-PlatformRuntimes must exist'
$stopBody = $stopFunction.Extent.Text
function Get-StopPosition([string]$Pattern, [string]$What) {
    $match = [regex]::Match($stopBody, $Pattern)
    Assert-True ($match.Success) "expected to find $What in Stop-PlatformRuntimes"
    return $match.Index
}
$disableCall = Get-StopPosition 'Disable-ScheduledTask -TaskName \$task' 'the Disable-ScheduledTask call'
$gracefulCall = Get-StopPosition '& \$stop -PlatformRoot \$platform' 'the graceful stop-stock-dashboard.ps1 call'
$stopTaskCall = Get-StopPosition 'Stop-ScheduledTask -TaskName \$task' 'the Stop-ScheduledTask backstop'
Assert-True ($disableCall -lt $gracefulCall) 'the tasks must be disabled before the graceful stop, so nothing restarts behind it'
Assert-True ($gracefulCall -lt $stopTaskCall) 'the graceful stop must run before Stop-ScheduledTask, or the runtime state is left claiming healthy for a dead PID'
# The shared-peer tunnels are deliberately left alone: stopping PostgreSQL
# already severs every peer session and the tunnels reconnect by themselves.
Assert-True ($source -match 'trading-hareness-shared-peer-tunnels[^\r\n]*\r?\n') 'the decision to leave the shared-peer tunnels running must be written down'
Assert-True ($source -notmatch "'trading-hareness-shared-peer-tunnels',") 'the shared-peer tunnels must not be in the disabled-task list'

# The migration may only run outside the exchange session, which is the same
# 04:00-08:00 maintenance window the nightly database jobs were moved into.
# Each of them would run against a stopped or half-copied cluster.
foreach ($job in 'trading-hareness-post-close-pipeline', 'trading-hareness-storage-tiers', 'trading-hareness-stock-backup', 'trading-hareness-stock-backup-offsite') {
    Assert-True ($source -match [regex]::Escape($job)) "the maintenance-window job $job must be stopped for the migration"
}
Assert-True ($source -match "State -ne 'Disabled'") 'a task that was already disabled must not be recorded for re-enabling'
Assert-True ($source -match '\$script:DisabledTasks') 'step 8 must re-enable only the tasks this run disabled'

# --- the old data directory is never removed --------------------------------
foreach ($destructive in 'Remove-Item[^\r\n]*\$currentDataDir', 'rm -r', 'Remove-Item[^\r\n]*-Recurse[^\r\n]*data') {
    Assert-True ($source -notmatch $destructive) "the migration must never delete the old data directory ($destructive)"
}
Assert-True ($source -match 'Rename-Item -LiteralPath \$currentDataDir -NewName \$KeepOldName') 'the old directory must be renamed to the kept name'
Assert-True ($source -match 'kept for rollback') 'the rename must say why the old directory survives'

# --- the copy is verified on three independent signals ----------------------
Assert-True ($source -match '/COPY:DAT') 'robocopy must preserve data, attributes and timestamps'
# Without /XJ robocopy follows <PGDATA>\pg_tblspc\<oid> and copies the whole
# cold tier from the G: HDD onto the 500 GB hot volume.
Assert-True ($source -match '/E /XJ /COPY:DAT') 'robocopy must exclude junctions with /XJ, or a second migration copies the cold tier onto the hot volume'
Assert-True ($source -match 'New-Item -ItemType Junction') 'the tablespace junctions skipped by /XJ must be recreated at the target'
Assert-True ($source -match 'Failed to recreate the tablespace junction') 'a junction that could not be recreated must fail the migration, not the cluster start'
Assert-True ($source -match '\$robocopyExit -ge 8') 'robocopy exit codes 8 and above are failures'
Assert-True ($source -match 'pg_control') 'the copy must be checked against the cluster control file checksum'
Assert-True ($source -match "Refusing to switch PGDATA_DIR before the copy is verified") 'the switch must fail closed when verification did not run'

# --- startup verification and the row-count smoke check ---------------------
foreach ($check in 'SHOW data_directory', 'SHOW work_mem', 'SHOW shared_preload_libraries') {
    Assert-True ($source -match [regex]::Escape($check)) "the restarted server must be verified with `"$check`""
}
foreach ($table in 'quant.instruments', 'quant.canonical_bars_daily', 'quant.raw_market_observations') {
    Assert-True ($source -match [regex]::Escape($table)) "the row-count smoke check must cover $table"
}
Assert-True ($source -match 'Row count for \$table changed across the migration') 'a row-count difference must abort the migration'

# --- -WhatIf must not write anything ----------------------------------------
foreach ($guarded in @(
    'if \(-not \$WhatIf\) \{\s*\r?\n?\s*Set-StockPlatformEnvValue -Path \$RuntimeEnv',
    'if \(-not \$WhatIf\) \{\s*\r?\n?\s*Rename-Item',
    'if \(-not \$WhatIf\) \{\s*\r?\n?\s*New-Item -ItemType Directory -Force -Path \$logs'
)) {
    Assert-True ($source -match $guarded) "every mutation must be guarded by -WhatIf ($guarded)"
}
Assert-True ($source -match 'if \(\$WhatIf\) \{ return \}') 'the cluster start must be a no-op under -WhatIf'

# --- credentials never reach the console, the command line or the receipt ---
Assert-True ($source -notmatch 'Write-Host[^\r\n]*PGADMINPASSWORD') 'the admin password must never be printed'
Assert-True ($source -notmatch '--password') 'the password must never be passed on a command line'
Assert-True ($source -match '\$env:PGPASSWORD = \$script:adminPassword') 'psql must take the password from the process environment'
Assert-True ($source -match "Remove-Item Env:PGPASSWORD") 'the password must be cleared from the environment after use'
# A second -c makes psql print that statement's command tag ahead of the value,
# which silently broke every SHOW/count comparison against the live server.
Assert-True ($source -match '\$env:PGOPTIONS = "-c statement_timeout=') 'the statement timeout must travel in PGOPTIONS'
Assert-True ($source -notmatch '-c "SET statement_timeout') 'the statement timeout must not be a second -c command'
Assert-True ($source -match 'Remove-Item Env:PGOPTIONS') 'PGOPTIONS must be cleared after use'

# --- the source directory must be copyable faithfully -----------------------
# Get-DirectoryFootprint and Assert-CopyableSource are pure; exercise them
# against a real junction instead of trusting the enumeration's default.
$footprintAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-DirectoryFootprint' }, $true)
$copyableAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Assert-CopyableSource' }, $true)
Assert-True ($null -ne $footprintAst -and $null -ne $copyableAst) 'Get-DirectoryFootprint and Assert-CopyableSource must exist'
. ([scriptblock]::Create($footprintAst.Extent.Text))
. ([scriptblock]::Create($copyableAst.Extent.Text))

$sandbox = Join-Path ([IO.Path]::GetTempPath()) ('pgdata-footprint-' + [guid]::NewGuid().ToString('N'))
try {
    $fakeData = Join-Path $sandbox 'pgdata'
    $cold = Join-Path $sandbox 'cold'
    New-Item -ItemType Directory -Force -Path (Join-Path $fakeData 'base\1'), (Join-Path $fakeData 'pg_tblspc'), (Join-Path $cold 'PG_16_202209061') | Out-Null
    [IO.File]::WriteAllBytes((Join-Path $fakeData 'base\1\16384'), (New-Object byte[] 100))
    [IO.File]::WriteAllBytes((Join-Path $cold 'PG_16_202209061\99999'), (New-Object byte[] 40960))
    New-Item -ItemType Junction -Path (Join-Path $fakeData 'pg_tblspc\16400') -Target $cold | Out-Null

    $footprint = Get-DirectoryFootprint -Path $fakeData
    Assert-True ($footprint.Files -eq 1 -and $footprint.Bytes -eq 100) `
        "the footprint must not follow the tablespace junction (got $($footprint.Files) files / $($footprint.Bytes) bytes)"
    Assert-True (@($footprint.ReparsePoints).Count -eq 1) 'the tablespace junction must be reported'
    Assert-True (@($footprint.ReparsePoints)[0].Relative -eq 'pg_tblspc\16400') 'the reparse point must be reported by its relative path'
    Assert-True (@($footprint.ReparsePoints)[0].Target -eq $cold) 'the junction target must be reported so it can be recreated'
    Assert-True (@(Assert-CopyableSource -Footprint $footprint).Count -eq 1) 'a pg_tblspc junction is copyable: the script recreates it'
    # A readable tree must report zero skipped entries -- the count is one half
    # of the copy verification and a silent default of "some" would be useless.
    Assert-True ($footprint.Skipped -eq 0 -and @($footprint.SkippedEntries).Count -eq 0) 'a fully readable directory must report no skipped entries'

    # Anything the enumeration cannot read must be COUNTED and NAMED, not
    # dropped. Dropped, it appears on one side of the step-4 file/byte
    # comparison and not the other, and the migration fails with "Copy
    # verification failed" and nothing to explain it. An absent path is the
    # deterministic way to make the enumeration error (a denied ACE depends on
    # whether the session is elevated).
    $unreadable = Get-DirectoryFootprint -Path (Join-Path $sandbox ('absent-' + [guid]::NewGuid().ToString('N')))
    Assert-True ($unreadable.Skipped -eq 1) "an unreadable path must be counted, not dropped (got $($unreadable.Skipped))"
    Assert-True ([bool]@($unreadable.SkippedEntries)[0].Path) 'a skipped entry must be reported by path'
    Assert-True ([bool]@($unreadable.SkippedEntries)[0].Error) 'a skipped entry must carry the reason it could not be read'
    Assert-True ($unreadable.Files -eq 0 -and $unreadable.Bytes -eq 0) 'nothing readable means nothing counted'
    # The counts are useless unless the failure message prints them.
    Assert-True ($source -match 'Copy verification failed:[^\r\n]*unreadable') 'the copy-verification failure must name the unreadable counts'
    Assert-True ($source -match 'unreadable_entries') 'the receipt must carry the unreadable entries'

    # Anything else would be silently dropped by /XJ.
    New-Item -ItemType Junction -Path (Join-Path $fakeData 'rogue') -Target $cold | Out-Null
    $refused = $false
    try { [void](Assert-CopyableSource -Footprint (Get-DirectoryFootprint -Path $fakeData)) } catch { $refused = $_.Exception.Message -match 'reparse points outside pg_tblspc' }
    Assert-True $refused 'a reparse point outside pg_tblspc must make the migration refuse the source'
} finally {
    foreach ($link in 'pgdata\pg_tblspc\16400', 'pgdata\rogue') {
        $path = Join-Path $sandbox $link
        if (Test-Path -LiteralPath $path) { [IO.Directory]::Delete($path) }
    }
    Remove-Item -LiteralPath $sandbox -Recurse -Force -ErrorAction SilentlyContinue
}

# --- dating a cluster image (exercised read-only against the live cluster) ---
$checkpointAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-ClusterCheckpointTime' }, $true)
Assert-True ($null -ne $checkpointAst) 'Get-ClusterCheckpointTime must exist'
. ([scriptblock]::Create($checkpointAst.Extent.Text))
$livePlatform = 'G:\StockPlatform'
$pgControlData = Join-Path $livePlatform 'runtime\postgresql-16.15\bin\pg_controldata.exe'
$liveData = ''
if (Test-Path -LiteralPath (Join-Path $livePlatform 'config\runtime.env') -PathType Leaf) {
    Import-Module (Join-Path (Split-Path -Parent $PSScriptRoot) 'postgres-managed-config.psm1') -Force
    $liveData = Resolve-StockPlatformDataDirectory -PlatformRoot $livePlatform
}
$checkpointExercised = $false
if ($liveData -and (Test-Path -LiteralPath (Join-Path $liveData 'global\pg_control') -PathType Leaf)) {
    $age = Get-ClusterCheckpointTime -DataDirectory $liveData
    Assert-True ($null -ne $age.CheckpointTime) 'the checkpoint time of a real cluster must be readable'
    Assert-True ($age.Source -in @('pg_controldata', 'pg_control_mtime')) "the checkpoint source must be named (got $($age.Source))"
    Assert-True ($age.CheckpointTime -gt [DateTime]::new(2020, 1, 1) -and $age.CheckpointTime -lt [DateTime]::Now.AddDays(1)) `
        "the checkpoint time must be plausible (got $($age.CheckpointTime))"
    $checkpointExercised = $true
}
# A directory that is not a cluster has no checkpoint at all: the rollback gate
# must see 'unavailable', never a silently wrong recent date.
$noCluster = Get-ClusterCheckpointTime -DataDirectory (Join-Path ([IO.Path]::GetTempPath()) ('absent-' + [guid]::NewGuid().ToString('N')))
Assert-True ($noCluster.Source -eq 'unavailable' -and $null -eq $noCluster.CheckpointTime) 'a missing cluster must report an unavailable checkpoint time'

# --- rollback ---------------------------------------------------------------
Assert-True ($source -match '(?s)if \(\$Rollback\)') 'the -Rollback switch must have its own flow'
Assert-True ($source -match 'Rollback target is not a PostgreSQL data directory') 'rollback must refuse a target that is not a cluster'
Assert-True ($source -match 'rollback_command') 'the receipt must state how to roll the migration back'

$rollbackStart = Get-Position '(?s)if \(\$Rollback\) \{' 'the rollback flow'
$rollbackBody = $source.Substring($rollbackStart, (Get-Position 'step 1/8 preflight' 'the preflight step marker') - $rollbackStart)
function Get-RollbackPosition([string]$Pattern, [string]$What) {
    $match = [regex]::Match($rollbackBody, $Pattern)
    Assert-True ($match.Success) "expected to find $What in the rollback flow"
    return $match.Index
}
# Rollback starts an image frozen at the cutover. It must say how much it
# discards, refuse without an explicit acknowledgement, and never touch
# anything before both of those happened.
$gapPrinted = Get-RollbackPosition 'would discard about' 'the age gap the rollback would discard'
$acceptGate = Get-RollbackPosition '-not \$AcceptDataLoss' 'the -AcceptDataLoss gate'
$tablespaceGate = Get-RollbackPosition 'Get-TablespaceLinkMap -DataDirectory \$currentDataDir' 'the cold-tablespace check'
$rollbackStop = Get-RollbackPosition 'Stop-PostgresCluster -DataDirectory \$currentDataDir' 'the rollback stop of the live cluster'
Assert-True ($gapPrinted -lt $acceptGate) 'the age gap must be printed before the refusal, so the operator sees the number'
Assert-True ($tablespaceGate -lt $acceptGate) 'the cold-tablespace check must run before the -AcceptDataLoss gate'
Assert-True ($acceptGate -lt $rollbackStop) 'nothing may be stopped before -AcceptDataLoss has been accepted'
Assert-True ($rollbackBody -match 'Refusing to roll back to \$restoreTarget without -AcceptDataLoss') 'rollback must refuse by name without -AcceptDataLoss'
Assert-True ($rollbackBody -match 'predates their creation') 'rollback must refuse outright when the image predates the stock_cold tablespace'

# --- the shared cold tablespace ---------------------------------------------
# The link COUNT only catches the case where the target image has no pg_tblspc
# at all. Once both images carry junctions they point at the SAME
# G:\StockPlatform\data\pg-cold, which the migration neither copies nor
# versions: starting the older catalogue over cold files the newer cluster has
# already rewritten is not a revert, and no switch makes it one.
$sharedRefusal = Get-RollbackPosition '\$sharedTablespaces\.Count -gt 0 -and \$liveIsNewer' 'the shared-tablespace refusal'
Assert-True ($sharedRefusal -lt $acceptGate) 'the shared-tablespace refusal must run before the -AcceptDataLoss gate'
Assert-True ($rollbackBody -match 'shares \$\(\$sharedTablespaces\.Count\) tablespace') 'the refusal must name the shared tablespace locations'
Assert-True ($rollbackBody -match 'No switch') 'the shared-tablespace refusal must say that no switch overrides it'
Assert-True ($rollbackBody -notmatch '(?s)\$sharedTablespaces\.Count -gt 0[^\r\n]*AcceptDataLoss') 'the shared-tablespace refusal must not be gated behind -AcceptDataLoss'
# It is a refusal, so it must be a throw, not a warning the operator can miss.
$sharedBlock = $rollbackBody.Substring($sharedRefusal)
Assert-True ($sharedBlock -match '^[^\r\n]*\r?\n\s*throw ') 'the shared-tablespace refusal must throw'
# The comparison is on the tablespace LOCATION, not on the link count: both
# images carrying one junction each is exactly the case the count cannot see.
$sharedAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Get-SharedTablespaceLocation' }, $true)
Assert-True ($null -ne $sharedAst) 'Get-SharedTablespaceLocation must exist'
. ([scriptblock]::Create($sharedAst.Extent.Text))
$liveExample = @([pscustomobject]@{ Oid = '16400'; Location = 'G:\StockPlatform\data\pg-cold' })
$sameExample = @([pscustomobject]@{ Oid = '16400'; Location = 'g:\stockplatform\data\pg-cold' })
$otherExample = @([pscustomobject]@{ Oid = '16400'; Location = 'G:\StockPlatform\data\pg-cold-2026' })
Assert-True (@(Get-SharedTablespaceLocation -LiveLinks $liveExample -TargetLinks $sameExample).Count -eq 1) `
    'two images pointing at the same cold directory must be reported as sharing it, whatever the path case'
Assert-True (@(Get-SharedTablespaceLocation -LiveLinks $liveExample -TargetLinks $otherExample).Count -eq 0) `
    'a target with its own tablespace directory shares nothing'
Assert-True (@(Get-SharedTablespaceLocation -LiveLinks $liveExample -TargetLinks @()).Count -eq 0) `
    'a target with no tablespace at all is the other refusal, not this one'
Assert-True (@(Get-SharedTablespaceLocation -LiveLinks $liveExample -TargetLinks @([pscustomobject]@{ Oid = '16400'; Location = '' })).Count -eq 0) `
    'a junction whose target could not be read must not be reported as shared'
# Printed on EVERY rollback attempt, refused or not: "rollback" does not include
# the cold tier, and an operator who assumes it does loses the difference.
$coldNotice = Get-RollbackPosition 'the stock_cold tablespace is NOT reverted' 'the stock_cold notice'
Assert-True ($coldNotice -lt $sharedRefusal -and $coldNotice -lt $acceptGate) 'the stock_cold notice must be printed before any refusal, on every attempt'
Assert-True ($rollbackBody -match 'stock_cold_reverted = \$false') 'the rollback receipt must record that stock_cold was not reverted'
# And the forward receipt has to carry the catalogue the gate compares against.
Assert-True ($source -match 'function Get-TablespaceCatalogue') 'the migration must read pg_tablespace from the running cluster'
Assert-True ($source -match "pg_tablespace_location\(oid\)") 'the catalogue must carry each tablespace location, not only its oid'
Assert-True ($source -match '(?m)^\s*tablespaces = \$tablespaces') 'the migration receipt must record the tablespace oids and locations'
Assert-True ($rollbackBody -match 'Get-ClusterCheckpointTime -DataDirectory \$restoreTarget') "the rollback target's age must come from its own control file"
Assert-True ($rollbackBody -match 'postgres-data-rollback-') 'a rollback must leave a receipt of its own'
# The receipt must not hand the operator a pre-armed data-loss command.
Assert-True ($source -notmatch 'rollback_command[^\r\n]*-AcceptDataLoss') 'the printed rollback command must not pre-arm -AcceptDataLoss'
Assert-True ($source -match 'rollback_note') 'the receipt must say what a rollback costs'

# --- failure recovery -------------------------------------------------------
# Steps 2-8 leave PostgreSQL stopped and five scheduled tasks disabled. A
# failure anywhere in there must put the platform back before it rethrows.
$recoveryAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Invoke-FailureRecovery' }, $true)
Assert-True ($null -ne $recoveryAst) 'Invoke-FailureRecovery must exist'
$recoveryBody = $recoveryAst.Extent.Text
function Get-RecoveryPosition([string]$Pattern, [string]$What) {
    $match = [regex]::Match($recoveryBody, $Pattern)
    Assert-True ($match.Success) "expected to find $What in Invoke-FailureRecovery"
    return $match.Index
}
$stopTargetStep = Get-RecoveryPosition 'Stop-PostgresClusterIfRunning -DataDirectory \$TargetDataDirectory' 'stopping whatever runs from the target'
$envRestoreStep = Get-RecoveryPosition "Set-StockPlatformEnvValue -Path \`$RuntimeEnv -Name 'PGDATA_DIR' -Value \`$authoritative" 'restoring PGDATA_DIR'
$configStep = Get-RecoveryPosition 'Write-StockPlatformManagedConfig -DataDirectory \$authoritative' 'regenerating the managed configuration'
$startStep = Get-RecoveryPosition 'Start-PostgresCluster -DataDirectory \$authoritative' 'restarting the original cluster'
$platformStep = Get-RecoveryPosition 'Start-PlatformRuntimes' 're-enabling the platform'
Assert-True ($stopTargetStep -lt $envRestoreStep) 'the half-migrated target must be stopped before PGDATA_DIR is put back'
Assert-True ($envRestoreStep -lt $configStep) 'PGDATA_DIR must be restored before the managed configuration is regenerated for it'
Assert-True ($configStep -lt $startStep) 'the configuration must point at the original directory before it is started'
Assert-True ($startStep -lt $platformStep) 'the database must be back before the platform tasks are re-enabled'
Assert-True ($recoveryBody -match '\$script:OldDirectoryRenamed') 'recovery must not abandon a target that already became authoritative'

# A failed run leaves a half-filled target directory, and the preflight of the
# retry refuses a target that "exists and is not empty" -- the script blocking
# itself with its own debris. The recovery must take it away again, and only
# after the platform is provably back on the source.
$cleanupStep = Get-RecoveryPosition 'Remove-PartialTargetDirectory -Path \$TargetDataDirectory' 'removing the partial copy'
Assert-True ($startStep -lt $cleanupStep) 'the source cluster must be running again before the partial copy is removed'
$removeAst = $ast.Find({ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Remove-PartialTargetDirectory' }, $true)
Assert-True ($null -ne $removeAst) 'Remove-PartialTargetDirectory must exist'
$removeBody = $removeAst.Extent.Text
# Three independent guards on the only deletion in this script.
Assert-True ($removeBody -match 'if \(-not \$script:TargetDirectoryOwned\)') 'the removal must refuse a directory this run did not create'
Assert-True ($removeBody -match 'if \(\$script:OldDirectoryRenamed\)') 'the removal must refuse once the target has become the authoritative cluster'
Assert-True ($removeBody -match 'it is the source cluster') 'the removal must refuse a path that resolves to the source'
# A recursive delete that followed a recreated pg_tblspc junction would take the
# live cold tablespace on G: with it.
Assert-True ($removeBody -match 'ReparsePoint') 'the removal must unlink the tablespace junctions before deleting recursively'
$junctionUnlink = [regex]::Match($removeBody, 'ReparsePoint').Index
$recursiveDelete = [regex]::Match($removeBody, 'Remove-Item -LiteralPath \$full -Recurse').Index
Assert-True ($junctionUnlink -lt $recursiveDelete) 'the junctions must be unlinked before the recursive delete, never after'
Assert-True ($source -match '\$script:TargetDirectoryOwned = \$true') 'step 4 must take ownership of the target before it writes into it'
$ownershipTaken = Get-BodyPosition '\$script:TargetDirectoryOwned = \$true' 'the ownership marker'
$robocopyCall = Get-BodyPosition 'robocopy\.exe \$currentDataDir \$target' 'the robocopy call'
Assert-True ($ownershipTaken -lt $robocopyCall) 'ownership must be recorded before the first byte is written to the target'

# The main flow must use it, and rethrow afterwards.
$catchStart = Get-BodyPosition '\} catch \{' 'the recovery catch block'
$finallyStart = Get-BodyPosition '\} finally \{' 'the transcript finally block'
Assert-True ($catchStart -lt $finallyStart) 'the catch must come before the finally'
$catchBody = $body.Substring($catchStart, $finallyStart - $catchStart)
Assert-True ($catchBody -match 'Invoke-FailureRecovery -SourceDataDirectory \$currentDataDir -TargetDataDirectory \$target') 'the catch must run the recovery'
Assert-True ($catchBody -match '\.failure\.json') 'a failed migration must leave a failure receipt'
$recoveryCall = [regex]::Match($catchBody, 'Invoke-FailureRecovery').Index
$receiptWrite = [regex]::Match($catchBody, '\$failurePath').Index
$rethrow = [regex]::Match($catchBody, '(?m)^\s*throw\s*$').Index
Assert-True ($recoveryCall -lt $receiptWrite) 'the platform must be recovered before the receipt is written'
Assert-True ($receiptWrite -lt $rethrow) 'the receipt must be written before the failure propagates'
Assert-True ($rethrow -gt 0) 'the original failure must be rethrown after the recovery'

[pscustomobject]@{
    passed = $true
    scope = 'Static contract for migrate-postgres-data-directory.ps1, plus its pure trading-session guard, footprint/reparse handling exercised against a real junction, and the control-file reader exercised read-only against the live cluster; nothing was stopped, copied or migrated'
    ordering_verified = 'disable tasks -> graceful stop -> stop tasks -> pg_ctl stop -> robocopy /XJ -> copy verification -> junction recreation -> PGDATA_DIR switch -> start+verify -> rename old'
    recovery_verified = 'stop target -> PGDATA_DIR back -> regenerate conf -> start source -> remove the partial copy -> re-enable platform -> failure receipt -> rethrow'
    rollback_gate_verified = 'age gap printed -> stock_cold-not-reverted notice -> missing-tablespace refusal -> shared-tablespace refusal (no override) -> -AcceptDataLoss -> stop'
    snapshot_taken_inside_the_outage = $true
    footprint_reports_unreadable_entries = $true
    live_checkpoint_exercised = $checkpointExercised
}
