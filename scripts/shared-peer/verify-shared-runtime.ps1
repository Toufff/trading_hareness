[CmdletBinding()]
param(
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$ApiBase = 'http://127.0.0.1:5681',
    [string]$SshAlias = 'lightServer1',
    [int]$RemoteDatabasePort = 15432,
    [int]$RemoteApiPort = 15681,
    [int]$RemotePeerApiPort = 15682,
    [int]$RemoteHealthTimeoutSeconds = 90,
    [int]$RemoteHealthStableSamples = 2,
    [string]$PeerApiBase = '',
    # Per-check outcomes, written whether this script succeeds or throws.
    # publish-stock-release.ps1 reads it to tell "our tunnel is broken" from
    # "the peer application is down": on 2026-09-20 two of four publishes
    # reinstalled the shared tunnel because the peer app was down, which a
    # tunnel reinstall cannot fix and which cost the peer 5-7 s of database
    # connections each time. The failure that stops this script used to be
    # flattened into a single message with remote_owner_api and remote_peer_api
    # both reported as 'unavailable' -- true of neither.
    [string]$DiagnosticsPath = 'G:\StockPlatform\logs\runtime\shared-runtime-verification.json'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..')).TrimEnd('\')
Import-Module (Join-Path $repository 'scripts\windows\runtime-observability.psm1') -Force

#: Which side each check can implicate.  'owner' checks are ours and a tunnel
#: reinstall is a plausible repair; 'peer' checks run against the peer's own
#: application and nothing on this machine can fix them.
$script:CheckOwners = [ordered]@{
    local_database          = 'owner'
    local_api               = 'owner'
    licensed_quote          = 'owner'
    peer_contract           = 'owner'
    reverse_tunnel_ports    = 'owner'
    remote_owner_api        = 'owner'
    remote_peer_api         = 'peer'
    complete_stock_gateway  = 'peer'
    peer_api                = 'peer'
}

$script:Checks = [ordered]@{}
foreach ($name in $script:CheckOwners.Keys) {
    $script:Checks[$name] = [ordered]@{ state = 'not_reached'; side = $script:CheckOwners[$name]; detail = $null }
}

function Set-CheckResult {
    param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$State, $Detail = $null)
    $script:Checks[$Name] = [ordered]@{
        state = $State
        side = $script:CheckOwners[$Name]
        detail = if ($null -eq $Detail) { $null } else { [string]$Detail }
    }
}

function Write-VerificationDiagnostics {
    param([string]$Status, [string]$FailedCheck = '', [string]$ErrorMessage = '')
    if (-not $DiagnosticsPath) { return }
    try {
        $directory = Split-Path -Parent $DiagnosticsPath
        if ($directory -and -not (Test-Path -LiteralPath $directory)) {
            New-Item -ItemType Directory -Force -Path $directory | Out-Null
        }
        $payload = [ordered]@{
            schema_version = 1
            written_at = [DateTimeOffset]::Now.ToString('o')
            status = $Status
            failed_check = $FailedCheck
            failed_side = if ($FailedCheck -and $script:CheckOwners.Contains($FailedCheck)) { $script:CheckOwners[$FailedCheck] } else { '' }
            error = $ErrorMessage
            checks = $script:Checks
        }
        [IO.File]::WriteAllText($DiagnosticsPath, ($payload | ConvertTo-Json -Depth 6), [Text.UTF8Encoding]::new($false))
    } catch {
        # Diagnostics are an aid, never the thing that fails a verification.
        Write-Warning "Shared runtime diagnostics could not be written: $($_.Exception.Message)"
    }
}

function Read-EnvFile([string]$Path) {
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $values[$Matches[1]] = $Matches[2] }
    }
    return $values
}

function Wait-RemoteHttp200([int]$Port) {
    $deadline = [DateTime]::UtcNow.AddSeconds([Math]::Max(10, $RemoteHealthTimeoutSeconds))
    $required = [Math]::Max(1, $RemoteHealthStableSamples)
    $consecutive = 0
    $lastCode = 'unavailable'
    do {
        $lastCode = (& ssh.exe @($controlTarget.ConnectionArguments) -o BatchMode=yes $controlTarget.Destination `
            "curl --noproxy '*' -sS -o /dev/null -w '%{http_code}' --max-time 10 http://127.0.0.1:$Port/health").Trim()
        if ($lastCode -eq '200') {
            $consecutive++
            if ($consecutive -ge $required) { return 200 }
        } else {
            $consecutive = 0
        }
        Start-Sleep -Seconds 2
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Remote API did not reach $required consecutive HTTP 200 samples on 127.0.0.1:$Port before timeout; last code: $lastCode"
}

$currentCheck = ''
try {
    $runtime = Read-EnvFile $RuntimeEnv
    $tunnelTarget = Resolve-OwnerTunnelSshTarget -RuntimeEnv $RuntimeEnv -FallbackAlias $SshAlias
    $controlTarget = Resolve-OwnerTunnelControlSshTarget -FallbackAlias $SshAlias

    $currentCheck = 'local_database'
    $postgresRoot = Get-ChildItem -LiteralPath 'G:\StockPlatform\runtime' -Directory -Filter 'postgresql-*' |
        Sort-Object Name -Descending | Select-Object -First 1
    $psql = Join-Path $postgresRoot.FullName 'bin\psql.exe'
    $env:PGPASSWORD = $runtime.PGPASSWORD
    try {
        $databaseIdentity = (& $psql -w -At -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGUSER `
            -d $runtime.PGDATABASE -c "SELECT current_database()||':'||version_num FROM quant.alembic_version").Trim()
    }
    finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }
    Set-CheckResult -Name 'local_database' -State 'ok' -Detail $databaseIdentity

    $currentCheck = 'local_api'
    $health = Invoke-RestMethod -Uri "$ApiBase/health" -TimeoutSec 5
    Set-CheckResult -Name 'local_api' -State 'ok' -Detail $health.status

    $currentCheck = 'licensed_quote'
    $headers = @{ 'X-Quant-Read-Key' = $runtime.QUANT_SHARED_READ_API_KEY }
    $quote = Invoke-RestMethod -Uri "$ApiBase/licensed/longhu/quotes?symbols=600664.SH" `
        -Headers $headers -TimeoutSec 35
    if (@($quote.rows).Count -ne 1) { throw 'Licensed read gateway did not return the requested quote' }
    Set-CheckResult -Name 'licensed_quote' -State 'ok' -Detail (@($quote.rows).Count)

    # The peer's whole lifeline: the contract it starts against and the feed
    # that tells it what it broke. /health cannot speak for either -- on
    # 2026-09-20 a release shipped a contract endpoint that returned 500 to
    # every call while /health stayed green, and it was found by hand. The
    # contract introspects the live cluster, so a 200 here also means the
    # catalog reads behind it still work.
    $currentCheck = 'peer_contract'
    $contract = Invoke-RestMethod -Uri "$ApiBase/api/v1/peer/contract" -Headers $headers -TimeoutSec 30
    if (-not $contract.contract_version) { throw 'Peer contract returned no contract_version' }
    if (-not $contract.objects) { throw 'Peer contract published no objects' }
    if (-not $contract.derived_rules) { throw 'Peer contract published no derived_rules' }
    $errorFeed = Invoke-RestMethod -Uri "$ApiBase/api/v1/peer/errors" -Headers $headers -TimeoutSec 60
    if ($null -eq $errorFeed.groups) { throw 'Peer error feed returned no groups field' }
    Set-CheckResult -Name 'peer_contract' -State 'ok' -Detail $contract.contract_version

    # The tunnel's own liveness: these loopback listeners exist on lightServer
    # only while our tunnel processes are connected. This is the check a tunnel
    # reinstall actually repairs.
    $currentCheck = 'reverse_tunnel_ports'
    $remotePorts = & ssh.exe @($controlTarget.ConnectionArguments) -o BatchMode=yes $controlTarget.Destination `
        "ss -lnt | grep -E '127.0.0.1:($RemoteDatabasePort|$RemoteApiPort|$RemotePeerApiPort)' | wc -l"
    if ([int]$remotePorts -lt 3) { throw 'Database, owner API, and peer API loopback ports are not all available on lightServer' }
    Set-CheckResult -Name 'reverse_tunnel_ports' -State 'ok' -Detail ([int]$remotePorts)

    $currentCheck = 'remote_owner_api'
    $remoteOwnerCode = Wait-RemoteHttp200 -Port $RemoteApiPort
    Set-CheckResult -Name 'remote_owner_api' -State 'ok' -Detail ([int]$remoteOwnerCode)

    # From here on the checks exercise the peer's own application. A failure
    # says nothing about our tunnel, and restarting ours cannot repair it.
    $currentCheck = 'remote_peer_api'
    $remotePeerCode = Wait-RemoteHttp200 -Port $RemotePeerApiPort
    Set-CheckResult -Name 'remote_peer_api' -State 'ok' -Detail ([int]$remotePeerCode)

    $currentCheck = 'complete_stock_gateway'
    $completeGatewayJson = (& ssh.exe @($controlTarget.ConnectionArguments) -o BatchMode=yes $controlTarget.Destination `
        'python3 /home/stockpeer/trading_hareness/scripts/shared-peer/verify-complete-stock-api.py') -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0) { throw "Remote complete stock API probe exited $LASTEXITCODE; owner health alone does not prove peer compatibility" }
    $completeGateway = $completeGatewayJson | ConvertFrom-Json
    if (-not $completeGateway -or -not $completeGateway.PSObject.Properties['passed'] -or -not $completeGateway.passed) {
        throw 'Complete remote stock API acceptance probe returned no positive receipt'
    }
    Set-CheckResult -Name 'complete_stock_gateway' -State 'ok' -Detail $completeGateway.passed

    $currentCheck = 'peer_api'
    $peerHealth = $null
    if ($PeerApiBase) {
        $peerHealth = Invoke-RestMethod -Uri "$($PeerApiBase.TrimEnd('/'))/health" -TimeoutSec 10
        Set-CheckResult -Name 'peer_api' -State 'ok' -Detail $peerHealth.status
    } else {
        Set-CheckResult -Name 'peer_api' -State 'not_requested'
    }

    $currentCheck = ''
    Write-VerificationDiagnostics -Status 'verified'

    [pscustomobject]@{
        status = 'verified'
        local_database = $databaseIdentity
        local_api = $health.status
        licensed_quote_rows = @($quote.rows).Count
        peer_contract_version = $contract.contract_version
        peer_error_groups = @($errorFeed.groups).Count
        reverse_tunnel_ports = [int]$remotePorts
        remote_owner_api = [int]$remoteOwnerCode
        remote_peer_api = [int]$remotePeerCode
        complete_stock_gateway = $completeGateway.passed
        peer_api = if ($peerHealth) { $peerHealth.status } else { 'not_requested' }
        ssh_tunnel_target_mode = $tunnelTarget.Mode
        ssh_control_target_mode = $controlTarget.Mode
        diagnostics_path = $DiagnosticsPath
        secrets_printed = $false
    }
} catch {
    if ($currentCheck) { Set-CheckResult -Name $currentCheck -State 'failed' -Detail $_.Exception.Message }
    Write-VerificationDiagnostics -Status 'failed' -FailedCheck $currentCheck -ErrorMessage $_.Exception.Message
    throw
}
