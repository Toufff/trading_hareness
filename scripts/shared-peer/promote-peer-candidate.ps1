[CmdletBinding(SupportsShouldProcess, ConfirmImpact='High')]
param(
    [Parameter(Mandatory)][switch]$Promote,
    [string]$RuntimeEnv = 'G:\StockPlatform\config\runtime.env',
    [string]$ProductionDatabase = 'trading_hareness',
    [string]$CandidateDatabase = 'trading_hareness_candidate'
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Read-EnvFile([string]$Path) {
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines($Path, [Text.Encoding]::UTF8)) {
        if ($line -match '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $values[$Matches[1]] = $Matches[2] }
    }
    return $values
}
if (-not $Promote) { throw 'Explicit -Promote is required' }
$runtime = Read-EnvFile $RuntimeEnv
$postgresRoot = Get-ChildItem -LiteralPath 'G:\StockPlatform\runtime' -Directory -Filter 'postgresql-*' |
    Sort-Object Name -Descending | Select-Object -First 1
$psql = Join-Path $postgresRoot.FullName 'bin\psql.exe'
$backupName = '{0}_rollback_{1}' -f $ProductionDatabase, (Get-Date -Format 'yyyyMMdd_HHmmss')
$env:PGPASSWORD = $runtime.PGADMINPASSWORD
try {
    $candidateExists = (& $psql -w -At -h $runtime.PGHOST -p $runtime.PGPORT -U $runtime.PGADMINUSER `
        -d postgres -c "SELECT 1 FROM pg_database WHERE datname='$CandidateDatabase'").Trim()
    if ($candidateExists -ne '1') { throw "Candidate database does not exist: $CandidateDatabase" }
    if (-not $PSCmdlet.ShouldProcess($ProductionDatabase, "rename to $backupName and promote $CandidateDatabase")) {
        return
    }
    $sql = @"
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
 WHERE datname IN ('$ProductionDatabase','$CandidateDatabase') AND pid <> pg_backend_pid();
ALTER DATABASE $ProductionDatabase RENAME TO $backupName;
ALTER DATABASE $CandidateDatabase RENAME TO $ProductionDatabase;
"@
    & $psql -w -v ON_ERROR_STOP=1 -h $runtime.PGHOST -p $runtime.PGPORT `
        -U $runtime.PGADMINUSER -d postgres -c $sql | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Atomic database promotion failed' }
}
finally { Remove-Item Env:PGPASSWORD -ErrorAction SilentlyContinue }

[pscustomobject]@{
    status = 'promoted'
    production = $ProductionDatabase
    rollback_database = $backupName
    restart_required = $true
}
