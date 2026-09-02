Set-StrictMode -Version Latest

function Get-RuntimeLogRoot {
    param([Parameter(Mandatory)][string]$PlatformRoot)
    $root = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
    $path = Join-Path $root 'logs\runtime'
    New-Item -ItemType Directory -Force -Path $path | Out-Null
    return $path
}

function Write-AtomicUtf8File {
    param(
        [Parameter(Mandatory)][string]$Path,
        [Parameter(Mandatory)][string]$Content
    )
    $parent = Split-Path -Parent $Path
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $temporary = "$Path.$PID.$([Guid]::NewGuid().ToString('N')).tmp"
    [IO.File]::WriteAllText($temporary, $Content, [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Get-RuntimeMutexName {
    param([Parameter(Mandatory)][string]$Value)
    $bytes = [Text.Encoding]::UTF8.GetBytes($Value.ToLowerInvariant())
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $hash = [Convert]::ToHexString($sha.ComputeHash($bytes)).Substring(0, 24) }
    finally { $sha.Dispose() }
    return "Local\trading-hareness-runtime-$hash"
}

function Write-RuntimeEvent {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$Service,
        [Parameter(Mandatory)][string]$Event,
        [string]$RunId = '',
        [ValidateSet('debug', 'info', 'warning', 'error')][string]$Level = 'info',
        [hashtable]$Data = @{}
    )
    $logRoot = Get-RuntimeLogRoot -PlatformRoot $PlatformRoot
    $now = [DateTimeOffset]::Now
    $record = [ordered]@{
        schema_version = 1
        timestamp = $now.ToString('o')
        timestamp_utc = $now.ToUniversalTime().ToString('o')
        level = $Level
        service = $Service
        event = $Event
        run_id = $RunId
        host = [Environment]::MachineName
        writer_pid = $PID
    }
    foreach ($key in ($Data.Keys | Sort-Object)) { $record[$key] = $Data[$key] }
    $line = ($record | ConvertTo-Json -Depth 10 -Compress) + [Environment]::NewLine
    $path = Join-Path $logRoot ("lifecycle-{0}.jsonl" -f $now.ToString('yyyy-MM-dd'))
    $mutex = [Threading.Mutex]::new($false, (Get-RuntimeMutexName -Value $logRoot))
    try {
        if (-not $mutex.WaitOne([TimeSpan]::FromSeconds(10))) {
            throw "Timed out waiting for the runtime event log lock: $path"
        }
        [IO.File]::AppendAllText($path, $line, [Text.UTF8Encoding]::new($false))
    } finally {
        try { [void]$mutex.ReleaseMutex() } catch { }
        $mutex.Dispose()
    }
    return $record
}

function Get-RuntimeStatePath {
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$Service
    )
    return Join-Path (Get-RuntimeLogRoot -PlatformRoot $PlatformRoot) "$Service.current.json"
}

function Get-RuntimeState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$Service
    )
    $path = Get-RuntimeStatePath -PlatformRoot $PlatformRoot -Service $Service
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    try { return Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { return $null }
}

function Set-RuntimeState {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$Service,
        [Parameter(Mandatory)][hashtable]$State
    )
    $payload = [ordered]@{
        schema_version = 1
        service = $Service
        updated_at = [DateTimeOffset]::Now.ToString('o')
    }
    foreach ($key in ($State.Keys | Sort-Object)) { $payload[$key] = $State[$key] }
    $path = Get-RuntimeStatePath -PlatformRoot $PlatformRoot -Service $Service
    Write-AtomicUtf8File -Path $path -Content ($payload | ConvertTo-Json -Depth 10)
    return [pscustomobject]$payload
}

function New-RuntimeRun {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$Service
    )
    $now = [DateTimeOffset]::Now
    $runId = '{0}-{1}' -f $now.ToString('yyyyMMddTHHmmssfff'), [Guid]::NewGuid().ToString('N').Substring(0, 8)
    $directory = Join-Path (Get-RuntimeLogRoot -PlatformRoot $PlatformRoot) ("services\$Service\{0}" -f $now.ToString('yyyy-MM-dd'))
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
    return [pscustomobject]@{
        RunId = $runId
        Directory = $directory
        Stdout = Join-Path $directory "$runId.stdout.log"
        Stderr = Join-Path $directory "$runId.stderr.log"
        Descriptor = Join-Path $directory "$runId.run.json"
        StopMarker = Join-Path $directory "$runId.stop.json"
    }
}

function Start-RuntimeSupervisor {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$RepositoryRoot,
        [Parameter(Mandatory)][string]$Service,
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [Parameter(Mandatory)][string[]]$Arguments,
        [hashtable]$Environment = @{},
        [hashtable]$Metadata = @{}
    )
    $run = New-RuntimeRun -PlatformRoot $PlatformRoot -Service $Service
    $descriptor = [ordered]@{
        schema_version = 1
        service = $Service
        run_id = $run.RunId
        platform_root = [IO.Path]::GetFullPath($PlatformRoot).TrimEnd('\')
        executable = $Executable
        working_directory = $WorkingDirectory
        arguments = @($Arguments)
        stdout = $run.Stdout
        stderr = $run.Stderr
        stop_marker = $run.StopMarker
        requested_at = [DateTimeOffset]::Now.ToString('o')
        metadata = $Metadata
    }
    Write-AtomicUtf8File -Path $run.Descriptor -Content ($descriptor | ConvertTo-Json -Depth 10)
    [void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service $Service -Event 'start_requested' -RunId $run.RunId -Data @{
        executable = $Executable
        working_directory = $WorkingDirectory
        descriptor = $run.Descriptor
        stdout = $run.Stdout
        stderr = $run.Stderr
    })
    $supervisorScript = Join-Path ([IO.Path]::GetFullPath($RepositoryRoot).TrimEnd('\')) 'scripts\windows\supervise-runtime-process.ps1'
    if (-not (Test-Path -LiteralPath $supervisorScript -PathType Leaf)) { throw "Missing runtime supervisor: $supervisorScript" }
    $pwsh = (Get-Command pwsh.exe -ErrorAction Stop).Source
    $argumentLine = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$supervisorScript`" -DescriptorPath `"$($run.Descriptor)`""
    $start = @{
        FilePath = $pwsh
        ArgumentList = $argumentLine
        PassThru = $true
        WindowStyle = 'Hidden'
        WorkingDirectory = $WorkingDirectory
    }
    if ($Environment.Count -gt 0) { $start.Environment = $Environment }
    $supervisor = Start-Process @start
    $state = @{
        status = 'supervisor_started'
        run_id = $run.RunId
        supervisor_pid = $supervisor.Id
        descriptor = $run.Descriptor
        stdout = $run.Stdout
        stderr = $run.Stderr
        requested_at = $descriptor.requested_at
    }
    [void](Set-RuntimeState -PlatformRoot $PlatformRoot -Service $Service -State $state)
    [void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service $Service -Event 'supervisor_started' -RunId $run.RunId -Data @{ supervisor_pid = $supervisor.Id })
    return [pscustomobject]($state + @{ stop_marker = $run.StopMarker })
}

function Request-RuntimeStop {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [Parameter(Mandatory)][string]$Service,
        [string]$Reason = 'operator_requested',
        [string]$RequestedBy = 'runtime_script'
    )
    $state = Get-RuntimeState -PlatformRoot $PlatformRoot -Service $Service
    if (-not $state -or -not $state.PSObject.Properties['run_id'] -or -not $state.run_id) { return $null }
    $status = if ($state.PSObject.Properties['status']) { [string]$state.status } else { '' }
    if ($status -in @('stopped', 'unexpected_exit', 'supervisor_failed', 'start_failed')) {
        [void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service $Service -Event 'terminal_state_cleanup_requested' -RunId ([string]$state.run_id) -Data @{
            previous_status = $status
            reason = $Reason
            requested_by = $RequestedBy
        })
        return $state
    }
    if ($status -eq 'stop_requested') { return $state }
    $descriptor = $null
    if ($state.PSObject.Properties['descriptor'] -and $state.descriptor -and (Test-Path -LiteralPath $state.descriptor -PathType Leaf)) {
        $descriptor = Get-Content -LiteralPath $state.descriptor -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    if ($descriptor -and $descriptor.stop_marker) {
        $marker = [ordered]@{
            schema_version = 1
            run_id = [string]$state.run_id
            requested_at = [DateTimeOffset]::Now.ToString('o')
            reason = $Reason
            requested_by = $RequestedBy
            requester_pid = $PID
        }
        Write-AtomicUtf8File -Path ([string]$descriptor.stop_marker) -Content ($marker | ConvertTo-Json -Depth 5)
    }
    [void](Write-RuntimeEvent -PlatformRoot $PlatformRoot -Service $Service -Event 'stop_requested' -RunId ([string]$state.run_id) -Data @{
        reason = $Reason
        requested_by = $RequestedBy
    })
    $updatedState = @{}
    foreach ($property in $state.PSObject.Properties) {
        if ($property.Name -notin @('schema_version', 'service', 'updated_at')) { $updatedState[$property.Name] = $property.Value }
    }
    $updatedState.status = 'stop_requested'
    $updatedState.stop_requested_at = [DateTimeOffset]::Now.ToString('o')
    $updatedState.stop_reason = $Reason
    $updatedState.stop_requested_by = $RequestedBy
    [void](Set-RuntimeState -PlatformRoot $PlatformRoot -Service $Service -State $updatedState)
    return $state
}

function Invoke-RuntimeLogRetention {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$PlatformRoot,
        [int]$ServiceLogDays = 30,
        [int]$LifecycleDays = 90
    )
    $root = Get-RuntimeLogRoot -PlatformRoot $PlatformRoot
    $now = Get-Date
    Get-ChildItem -LiteralPath (Join-Path $root 'services') -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object LastWriteTime -lt $now.AddDays(-[Math]::Max(1, $ServiceLogDays)) |
        Remove-Item -Force -ErrorAction SilentlyContinue
    Get-ChildItem -LiteralPath $root -File -Filter 'lifecycle-*.jsonl' -ErrorAction SilentlyContinue |
        Where-Object LastWriteTime -lt $now.AddDays(-[Math]::Max(1, $LifecycleDays)) |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

function Resolve-OwnerTunnelSshTarget {
    # Owner-side automation (dashboard tunnel, shared-peer tunnels, runtime
    # verification) historically always connected via a root-capable ssh
    # config alias. When runtime.env carries all four
    # OWNER_TUNNEL_SSH_{USER,KEY,HOST,PORT} keys, connect instead with a
    # dedicated, restricted key (see scripts/shared-peer/install-owner-tunnel-key.sh
    # for the matching authorized_keys entry). When any key is missing, fall
    # back to the existing alias so unattended deployments keep working, and
    # emit a clear warning so the gap is visible in logs/output.
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][string]$RuntimeEnv,
        [Parameter(Mandatory)][string]$FallbackAlias
    )
    $config = @{}
    if (Test-Path -LiteralPath $RuntimeEnv -PathType Leaf) {
        foreach ($line in [IO.File]::ReadAllLines($RuntimeEnv, [Text.Encoding]::UTF8)) {
            if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $config[$Matches[1]] = $Matches[2] }
        }
    }
    $user = $config['OWNER_TUNNEL_SSH_USER']
    $key = $config['OWNER_TUNNEL_SSH_KEY']
    $sshHost = $config['OWNER_TUNNEL_SSH_HOST']
    $port = $config['OWNER_TUNNEL_SSH_PORT']
    if ($user -and $key -and $sshHost -and $port) {
        return [pscustomobject]@{
            Mode = 'restricted_key'
            ConnectionArguments = @('-i', $key, '-p', $port, '-o', 'StrictHostKeyChecking=yes', '-o', 'IdentitiesOnly=yes')
            Destination = "$user@$sshHost"
        }
    }
    Write-Warning ("OWNER_TUNNEL_SSH_USER/OWNER_TUNNEL_SSH_KEY/OWNER_TUNNEL_SSH_HOST/OWNER_TUNNEL_SSH_PORT are not " + `
        "all set in $RuntimeEnv; falling back to the ssh config alias '$FallbackAlias'. This alias is typically " + `
        "root-capable. Run scripts/shared-peer/install-owner-tunnel-key.sh to provision a restricted owner tunnel " + `
        "key and set the four OWNER_TUNNEL_SSH_* keys in runtime.env to stop using the root alias for unattended tunnels.")
    return [pscustomobject]@{
        Mode = 'alias_fallback'
        ConnectionArguments = @()
        Destination = $FallbackAlias
    }
}

function Resolve-PostgresStartupAction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][bool]$Ready,
        [Parameter(Mandatory)][int]$PgCtlStatusExitCode
    )
    if ($Ready) { return 'ready' }
    if ($PgCtlStatusExitCode -eq 0) { return 'wait_for_running_server' }
    if ($PgCtlStatusExitCode -eq 3) { return 'start_stopped_server' }
    return 'fail_unknown_status'
}

function Assert-ReservedRemoteTunnelPort {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory)][int]$Port,
        [int[]]$AllowedPorts = @(15680, 15681, 15682)
    )
    if ($Port -notin $AllowedPorts) {
        throw "Refusing remote listener cleanup outside reserved stock-platform ports: $Port"
    }
    return $Port
}

Export-ModuleMember -Function @(
    'Get-RuntimeLogRoot',
    'Write-RuntimeEvent',
    'Get-RuntimeState',
    'Set-RuntimeState',
    'New-RuntimeRun',
    'Start-RuntimeSupervisor',
    'Request-RuntimeStop',
    'Invoke-RuntimeLogRetention',
    'Resolve-PostgresStartupAction',
    'Assert-ReservedRemoteTunnelPort',
    'Resolve-OwnerTunnelSshTarget'
)
