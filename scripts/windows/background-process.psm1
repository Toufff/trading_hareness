Set-StrictMode -Version Latest

function Get-InstalledPowerShell {
    # Never pin a production scheduled task to Codex's disposable runtime cache.
    $msi = Join-Path $env:ProgramFiles 'PowerShell\7\pwsh.exe'
    if (Test-Path -LiteralPath $msi) { return $msi }
    $package = Get-AppxPackage -Name Microsoft.PowerShell -ErrorAction SilentlyContinue | Sort-Object Version -Descending | Select-Object -First 1
    if ($package) {
        $path = Join-Path $package.InstallLocation 'pwsh.exe'
        if (Test-Path -LiteralPath $path) { return $path }
    }
    $command = Get-Command pwsh.exe -All -ErrorAction SilentlyContinue | Where-Object Source -NotMatch 'codex-runtimes|\\\.cache\\' | Select-Object -First 1
    if ($command) { return $command.Source }
    throw 'Install a persistent PowerShell 7 runtime before registering background tasks'
}

function New-ConsoleFreeStartInfo {
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments = @(),
          [string]$WorkingDirectory = '', [hashtable]$Environment = @{})
    $info = [Diagnostics.ProcessStartInfo]::new()
    $info.FileName = $FilePath
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    if ($WorkingDirectory) { $info.WorkingDirectory = $WorkingDirectory }
    foreach ($argument in $Arguments) { $info.ArgumentList.Add($argument) }
    foreach ($key in $Environment.Keys) { $info.Environment[$key] = [string]$Environment[$key] }
    return $info
}

function Invoke-ConsoleFreeCommand {
    param([Parameter(Mandatory)][string]$FilePath, [string[]]$Arguments = @(),
          [string]$WorkingDirectory = '', [int]$TimeoutSeconds = 30, [hashtable]$Environment = @{})
    $info = New-ConsoleFreeStartInfo -FilePath $FilePath -Arguments $Arguments -WorkingDirectory $WorkingDirectory -Environment $Environment
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $process = [Diagnostics.Process]::Start($info)
    try {
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit([Math]::Max(1, $TimeoutSeconds) * 1000)) {
            $process.Kill($true)
            [void]$process.WaitForExit(2000)
            throw "Background command timed out after $TimeoutSeconds seconds: $FilePath"
        }
        # pg_ctl and other daemon launchers can exit while a grandchild keeps
        # inherited stdout/stderr handles open. Process exit is NOT pipe EOF.
        # Never await those pipes indefinitely in the watchdog recovery loop.
        $complete = [Threading.Tasks.Task]::WaitAll([Threading.Tasks.Task[]]@($stdout,$stderr), 1000)
        return [pscustomobject]@{
            ExitCode=$process.ExitCode
            Stdout=if ($stdout.IsCompletedSuccessfully) { $stdout.GetAwaiter().GetResult() } else { '' }
            Stderr=if ($stderr.IsCompletedSuccessfully) { $stderr.GetAwaiter().GetResult() } else { '' }
            OutputComplete=$complete
        }
    } finally { $process.Dispose() }
}

function New-HiddenPowerShellTaskAction {
    param([Parameter(Mandatory)][string]$RepositoryRoot, [Parameter(Mandatory)][string]$ScriptPath,
          [string[]]$ScriptArguments = @(), [string]$HostRoot = '')
    # Tasks that run scripts from the development checkout pass the published release as
    # HostRoot: a long-lived host must not lock the checkout's bin, which publishing rebuilds.
    $taskHost = Join-Path $(if ($HostRoot) { $HostRoot } else { $RepositoryRoot }) 'scripts\windows\bin\stock-background-host.exe'
    foreach ($path in @($taskHost, $ScriptPath)) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Missing background task script: $path" }
    }
    $tokens = @((Get-InstalledPowerShell), $ScriptPath) + $ScriptArguments
    # The task boundary accepts file paths/flags, not arbitrary command text.
    $quoted = foreach ($token in $tokens) {
        if ($token.Contains('"')) { throw 'Embedded quotes are not supported in scheduled script arguments' }
        $trailing = $token.Length - $token.TrimEnd('\').Length
        '"' + $token + ('\' * $trailing) + '"'
    }
    return New-ScheduledTaskAction -Execute $taskHost -Argument ($quoted -join ' ') -WorkingDirectory $RepositoryRoot
}

Export-ModuleMember -Function Get-InstalledPowerShell, New-ConsoleFreeStartInfo, Invoke-ConsoleFreeCommand, New-HiddenPowerShellTaskAction
