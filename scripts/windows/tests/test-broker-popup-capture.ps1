$ErrorActionPreference = 'Stop'
$script = (Resolve-Path (Join-Path $PSScriptRoot '..\broker-popup-capture.ps1')).Path
$output = Join-Path $env:TEMP ("broker-popup-passive-{0}.png" -f [guid]::NewGuid())
$foreground = Add-Type -MemberDefinition @'
[DllImport("user32.dll")]
public static extern IntPtr GetForegroundWindow();
'@ -Name ForegroundProbe -Namespace BrokerPopupTest -PassThru
$hwnd = $foreground::GetForegroundWindow().ToInt64()
if ($hwnd -eq 0) { throw 'No foreground window available for passive test' }
$json = & $script -Hwnd $hwnd -OutputPath $output | ConvertFrom-Json
if (-not (Test-Path -LiteralPath $output)) { throw 'Passive screenshot was not created' }
if ($json.right_click) { throw 'Passive test must not right-click' }
if ([Int64]$json.foreground_before -ne [Int64]$json.foreground_after) { throw 'Passive inspection changed foreground window' }
if ([Int64]$json.target.hwnd -ne $hwnd) { throw 'Target HWND was not preserved' }
Remove-Item -LiteralPath $output -Force
'broker-popup-capture passive contract passed'
