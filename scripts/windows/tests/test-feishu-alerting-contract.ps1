$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw "ASSERTION FAILED: $Message" }
}

$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..\..'))
$configure = Join-Path $repository 'scripts\windows\configure-feishu-alerting.ps1'
$disable = Join-Path $repository 'scripts\windows\disable-feishu-alerting.ps1'
$test = Join-Path $repository 'scripts\windows\test-feishu-alerting.ps1'
$configureSource = [IO.File]::ReadAllText($configure)
$disableSource = [IO.File]::ReadAllText($disable)
foreach ($source in @($configureSource, $disableSource)) {
    Assert-True ($source.Contains("Join-Path `$PlatformRoot 'current'")) 'runtime restart must resolve the published current release'
    Assert-True ($source.Contains('-RepositoryRoot $productionRepository')) 'runtime restart must launch from the immutable published release'
    Assert-True (-not $source.Contains('-RepositoryRoot $RepositoryRoot -ApiPort $ApiPort')) 'runtime restart must not launch from the development tree'
}
$root = Join-Path ([IO.Path]::GetFullPath($env:TEMP)) 'stock-platform-feishu-contract-test'
$safeTempRoot = [IO.Path]::GetFullPath($env:TEMP).TrimEnd('\') + '\'
$resolvedRoot = [IO.Path]::GetFullPath($root)
Assert-True ($resolvedRoot.StartsWith($safeTempRoot, [StringComparison]::OrdinalIgnoreCase)) 'test root must stay below TEMP'
if (Test-Path -LiteralPath $resolvedRoot) { Remove-Item -LiteralPath $resolvedRoot -Recurse -Force }
New-Item -ItemType Directory -Path (Join-Path $resolvedRoot 'config') -Force | Out-Null
$envPath = Join-Path $resolvedRoot 'config\runtime.env'
[IO.File]::WriteAllText($envPath, "PGHOST=127.0.0.1`n", [Text.UTF8Encoding]::new($false))

try {
    $customSecretText = 'mock-signing-secret-never-print'
    $customSecret = ConvertTo-SecureString $customSecretText -AsPlainText -Force
    $customOutput = (& $configure -Transport CustomBot `
        -WebhookUrl 'https://open.feishu.cn/open-apis/bot/v2/hook/mock-contract' `
        -SigningSecret $customSecret -PlatformRoot $resolvedRoot `
        -RepositoryRoot $repository -SkipLocalRestart | Out-String)
    Assert-True (-not $customOutput.Contains($customSecretText)) 'configure output must not expose signing secret'
    Assert-True (-not $customOutput.Contains('mock-contract')) 'configure output must not expose webhook token'
    $content = [IO.File]::ReadAllText($envPath)
    Assert-True ($content.Contains('FEISHU_ALERT_TRANSPORT=custom_bot')) 'custom bot transport must be stored'
    Assert-True ($content.Contains("FEISHU_CUSTOM_BOT_SIGNING_SECRET=$customSecretText")) 'custom bot secret must be stored in private env'
    Assert-True ($content.Contains('QUANT_FEISHU_DIRECT_ENABLED=false')) 'custom-bot mode must not accidentally select app-direct transport'
    Assert-True ($content.Contains('INTRADAY_SCAN_INTERVAL_SECONDS=30')) 'scan interval must be seeded to 30 seconds'
    Assert-True ($content.Contains('QUANT_DISCIPLINE_ALERTS_ENABLED=true')) 'discipline watcher must be enabled'
    Assert-True ($content.Contains('QUANT_DISCIPLINE_ALERT_INTERVAL_SECONDS=30')) 'discipline watcher interval must be 30 seconds'
    Assert-True ($content.Contains('QUANT_DISCIPLINE_ALERT_ACCOUNT_KEY=citics-primary')) 'discipline watcher account must be explicit'
    $configOutput = (& $test -Mode Config -PlatformRoot $resolvedRoot -RepositoryRoot $repository | Out-String)
    Assert-True (-not $configOutput.Contains($customSecretText)) 'status output must not expose signing secret'

    $appSecretText = 'mock-app-secret-never-print'
    $appSecret = ConvertTo-SecureString $appSecretText -AsPlainText -Force
    $appOutput = (& $configure -Transport App -AppId 'cli_mock_contract' -AppSecret $appSecret `
        -ReceiveId 'oc_mock_contract' -ReceiveIdType chat_id -PlatformRoot $resolvedRoot `
        -RepositoryRoot $repository -SkipLocalRestart | Out-String)
    Assert-True (-not $appOutput.Contains($appSecretText)) 'configure output must not expose app secret'
    Assert-True (-not $appOutput.Contains('oc_mock_contract')) 'configure output must not expose receive id'
    $content = [IO.File]::ReadAllText($envPath)
    Assert-True ($content.Contains('FEISHU_ALERT_TRANSPORT=app')) 'app transport must be stored'
    Assert-True ($content.Contains("FEISHU_APP_SECRET=$appSecretText")) 'app secret must be stored in private env'
    Assert-True ($content.Contains('FEISHU_CUSTOM_BOT_WEBHOOK_URL=')) 'inactive custom bot webhook must be cleared'
    Assert-True ($content.Contains('QUANT_FEISHU_DIRECT_ENABLED=true')) 'app mode must enable direct application transport'
    $appConfigOutput = (& $test -Mode Config -PlatformRoot $resolvedRoot -RepositoryRoot $repository | Out-String)
    Assert-True (-not $appConfigOutput.Contains($appSecretText)) 'app status output must not expose app secret'

    [void](& $disable -PlatformRoot $resolvedRoot -RepositoryRoot $repository -SkipLocalRestart)
    $content = [IO.File]::ReadAllText($envPath)
    Assert-True ($content.Contains('FEISHU_ALERTS_CONFIGURED=false')) 'disable must clear configured state'
    Assert-True ($content.Contains('QUANT_FEISHU_DIRECT_ENABLED=false')) 'disable must fence direct delivery'
    Assert-True ($content.Contains('QUANT_DISCIPLINE_ALERTS_ENABLED=false')) 'disable must stop the discipline watcher'

    $mockOutput = (& $test -Mode Mock -RepositoryRoot $repository | Out-String)
    Assert-True ($mockOutput.Contains('"status":"passed"')) 'both transport mocks must pass'
    Write-Output 'PASS: Feishu alerting configuration and mock contracts'
} finally {
    if (Test-Path -LiteralPath $resolvedRoot) { Remove-Item -LiteralPath $resolvedRoot -Recurse -Force }
}
