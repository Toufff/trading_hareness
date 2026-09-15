# Explicit production acceptance. Briefly interrupts ONLY the owned SSH tunnel;
# never stops PostgreSQL, trades, changes remote configuration, or logs secrets.
param([switch]$AllowFaultInjection, [string]$PlatformRoot='G:\StockPlatform',
      [int]$StableSeconds=130, [string]$EvidencePath='G:\StockPlatform\logs\runtime\acceptance\shared-tunnel-recovery.json')
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest
if(-not $AllowFaultInjection){throw 'Use -AllowFaultInjection to authorize terminating the owned tunnel for this acceptance test'}
$repo=[IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
Import-Module (Join-Path $repo 'scripts\windows\runtime-observability.psm1') -Force
Import-Module (Join-Path $repo 'scripts\windows\background-process.psm1') -Force
$task='trading-hareness-shared-peer-tunnels'
function Owned-Ssh {
    return @(Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" | Where-Object {
        $_.CommandLine -match '127\.0\.0\.1:15432:127\.0\.0\.1:55432(?:\s|$)' -and
        $_.CommandLine -match '127\.0\.0\.1:15681:127\.0\.0\.1:5681(?:\s|$)'
    })
}
function Check-Remote {
    $r=Invoke-ConsoleFreeCommand -FilePath (Get-Command ssh.exe).Source -Arguments @('-o','BatchMode=yes','-o','ConnectTimeout=5','lightServer1',
        "curl --noproxy '*' -sS --max-time 8 -o /dev/null -w '%{http_code}' http://127.0.0.1:15681/health") -TimeoutSeconds 15
    return $r.ExitCode -eq 0 -and $r.Stdout.Trim() -eq '200'
}
$start=[DateTimeOffset]::Now
try {
    [xml]$definition=Export-ScheduledTask -TaskName $task
    if($definition.Task.Actions.Exec.Command -notmatch 'stock-background-host.exe$'){throw 'Production action does not use the native console-free host'}
    # Task Scheduler omits the XML element for its default false value. Read
    # the typed CIM field; absence of an XML node is not a runtime failure.
    $daily = (Get-ScheduledTask -TaskName $task).Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskDailyTrigger' }
    if(-not $daily -or $daily.Repetition.StopAtDurationEnd){throw 'Production task still kills processes at repetition end'}
    $before=Get-RuntimeState -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels'
    $owned=@(Owned-Ssh)
    if($owned.Count -ne 1 -or -not (Check-Remote)){throw 'Expected exactly one healthy SSH tunnel before the fault'}
    $beforePid=$owned[0].ProcessId
    Start-ScheduledTask -TaskName $task
    Start-ScheduledTask -TaskName $task
    Start-Sleep -Seconds 3
    $same=@(Owned-Ssh)
    if($same.Count -ne 1 -or $same[0].ProcessId -ne $beforePid){throw 'Repeated starts created/replaced a healthy tunnel'}
    Write-Output "Duplicate triggers preserved SSH PID $beforePid; injecting one disconnect."
    Stop-Process -Id $beforePid -Force
    $deadline=(Get-Date).AddSeconds(160)
    do {
        Start-Sleep -Seconds 3
        $owned=@(Owned-Ssh)
        if($owned.Count -gt 1){throw 'Recovery produced duplicate SSH processes'}
        $after=Get-RuntimeState -PlatformRoot $PlatformRoot -Service 'shared-peer-tunnels'
        if($owned.Count -eq 1 -and $owned[0].ProcessId -ne $beforePid -and $after.run_id -ne $before.run_id -and (Check-Remote)){break}
    }while((Get-Date) -lt $deadline)
    if((Get-Date) -ge $deadline){throw 'The real scheduled recovery did not restore the remote API within 160 seconds'}
    $recoveredAt=[DateTimeOffset]::Now
    $recoveredPid=$owned[0].ProcessId
    Write-Output "Scheduled recovery restored SSH PID $recoveredPid and remote HTTP 200; observing the next cadence."
    $until=(Get-Date).AddSeconds([Math]::Max(125,$StableSeconds))
    while((Get-Date) -lt $until){
        Start-Sleep -Seconds 5
        $active=@(Owned-Ssh)
        if($active.Count -ne 1 -or $active[0].ProcessId -ne $recoveredPid){throw 'Healthy tunnel restarted or duplicated during the stability window'}
        if((Get-ScheduledTask -TaskName $task).State -ne 'Running'){throw 'Recovered task stopped unexpectedly'}
    }
    if(-not (Check-Remote)){throw 'Final remote health failed'}
    $result=[ordered]@{passed=$true;started_at=$start.ToString('o');recovered_at=$recoveredAt.ToString('o');finished_at=[DateTimeOffset]::Now.ToString('o');
        original_pid=$beforePid;recovered_pid=$recoveredPid;final_ssh_count=1;duplicate_triggers_preserved_pid=$true;real_scheduled_recovery=$true;stable_seconds=$StableSeconds;remote_owner_http=200}
    [IO.File]::WriteAllText($EvidencePath,($result|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    [pscustomobject]$result
}catch{
    Disable-ScheduledTask -TaskName $task | Out-Null
    Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    [IO.File]::WriteAllText($EvidencePath,(@{passed=$false;error=$_.Exception.Message;task_left_disabled=$true}|ConvertTo-Json),[Text.UTF8Encoding]::new($false))
    throw
}
