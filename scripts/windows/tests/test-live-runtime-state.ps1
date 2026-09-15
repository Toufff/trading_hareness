$ErrorActionPreference='Stop'
Import-Module (Join-Path $PSScriptRoot '..\runtime-health-policy.psm1') -Force
if ((Resolve-LiveRuntimeState -RecordedStatus healthy -SupervisorAlive $false -Reachable $false) -ne 'unavailable') { throw 'Old healthy file cannot make dead service healthy' }
if ((Resolve-LiveRuntimeState -RecordedStatus healthy -SupervisorAlive $true -Reachable $false) -ne 'unavailable') { throw 'Living supervisor does not prove working API' }
if ((Resolve-LiveRuntimeState -RecordedStatus healthy -SupervisorAlive $false -Reachable $true) -ne 'unsupervised') { throw 'Reachable orphan needs recovery diagnosis' }
if ((Resolve-LiveRuntimeState -RecordedStatus failed -SupervisorAlive $true -Reachable $true) -ne 'healthy') { throw 'Live recovery supersedes old failure' }
[pscustomobject]@{passed=$true; stale_healthy_rejected=$true}
