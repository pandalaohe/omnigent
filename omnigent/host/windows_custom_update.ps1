param(
    [Parameter(Mandatory = $true)]
    [string]$InstructionPath
)

$ErrorActionPreference = 'Stop'

function Write-JsonAtomic {
    param([string]$Path, [object]$Value)
    $temporary = "$Path.tmp"
    $Value | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $temporary -Encoding utf8
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Add-UpdateLog {
    param([string]$Path, [string]$Message)
    $timestamp = [DateTimeOffset]::UtcNow.ToString('o')
    Add-Content -LiteralPath $Path -Value "[$timestamp] $Message" -Encoding utf8
}

function Invoke-CapturedProcess {
    param(
        [string[]]$Arguments,
        [hashtable]$Environment = @{}
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Arguments[0]
    foreach ($argument in $Arguments[1..($Arguments.Count - 1)]) {
        $startInfo.ArgumentList.Add($argument)
    }
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    foreach ($entry in $Environment.GetEnumerator()) {
        $startInfo.Environment[$entry.Key] = $entry.Value
    }
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Failed to start $($Arguments[0])"
    }
    $stdoutTask = $process.StandardOutput.ReadToEndAsync()
    $stderrTask = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    return [ordered]@{
        ExitCode = $process.ExitCode
        Stdout = $stdoutTask.GetAwaiter().GetResult()
        Stderr = $stderrTask.GetAwaiter().GetResult()
    }
}

function Restart-HostSupervisor {
    param([string]$WrapperPath)
    $output = & $WrapperPath start 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Host supervisor restart exited with status ${LASTEXITCODE}: $output"
    }
}

function Test-HostReconnected {
    param([object]$Instruction)
    $statusArguments = @($Instruction.status_argv | ForEach-Object { [string]$_ })
    $status = Invoke-CapturedProcess $statusArguments
    if ($status.ExitCode -ne 0) {
        return $false
    }
    try {
        $payload = $status.Stdout | ConvertFrom-Json
    }
    catch {
        return $false
    }
    $previous = @{}
    foreach ($record in $Instruction.previous_records) {
        $previous[[string]$record.target] = [int]$record.pid
    }
    foreach ($daemon in $payload.daemons) {
        if ([string]$daemon.host_id -ne [string]$Instruction.host_id) { continue }
        if ($previous.Count -gt 0 -and -not $previous.ContainsKey([string]$daemon.target)) {
            continue
        }
        if ($previous.ContainsKey([string]$daemon.target) -and
            [int]$daemon.pid -eq $previous[[string]$daemon.target]) {
            continue
        }
        if ([string]$daemon.process -eq 'online' -and
            [string]$daemon.host_status -eq 'online') {
            return $true
        }
    }
    return $false
}

$instruction = Get-Content -LiteralPath $InstructionPath -Raw | ConvertFrom-Json
$resultPath = [string]$instruction.result_path
$logPath = [string]$instruction.log_path
$lockPath = [string]$instruction.lock_path
$result = [ordered]@{
    schema_version = 1
    status = 'running'
    old_commit = [string]$instruction.old_commit
    target_commit = [string]$instruction.target_commit
    helper_pid = $PID
    error = $null
    updated_at = [DateTimeOffset]::UtcNow.ToString('o')
}
Write-JsonAtomic $resultPath $result
Add-UpdateLog $logPath "Waiting for CLI process $($instruction.parent_pid) to exit."

try {
    Wait-Process -Id ([int]$instruction.parent_pid) -Timeout 120 -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500

    Write-JsonAtomic ([string]$instruction.rollback_path) ([ordered]@{
        schema_version = 1
        commit_sha = [string]$instruction.old_commit
    })

    $installArguments = @($instruction.install_argv | ForEach-Object { [string]$_ })
    Add-UpdateLog $logPath "Running custom Host installer."
    $install = Invoke-CapturedProcess $installArguments @{
        OMNIGENT_SKIP_WEB_UI = 'true'
    }
    if ($install.Stdout) { Add-UpdateLog $logPath $install.Stdout.Trim() }
    if ($install.Stderr) { Add-UpdateLog $logPath $install.Stderr.Trim() }
    if ($install.ExitCode -ne 0) {
        throw "Installer exited with status $($install.ExitCode). Recovery: $($instruction.recovery_command)"
    }

    $probeArguments = @($instruction.probe_argv | ForEach-Object { [string]$_ })
    $probe = Invoke-CapturedProcess $probeArguments
    $installedCommit = $probe.Stdout.Trim().ToLowerInvariant()
    if ($probe.ExitCode -ne 0 -or $installedCommit -ne [string]$instruction.target_commit) {
        throw "Installed commit '$installedCommit' did not match '$($instruction.target_commit)'."
    }

    Restart-HostSupervisor ([string]$instruction.wrapper_path)
    $reconnected = $false
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(60)
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        if (Test-HostReconnected $instruction) {
            $reconnected = $true
            break
        }
        Start-Sleep -Seconds 1
    }
    if (-not $reconnected) {
        throw "The updated Host did not reconnect on its managed target within 60 seconds."
    }

    $result.status = 'complete'
    Add-UpdateLog $logPath "Custom Host update completed and reconnected."
}
catch {
    $result.status = 'failed'
    $result.error = $_.Exception.Message
    Add-UpdateLog $logPath "FAILED: $($result.error)"
    try {
        Restart-HostSupervisor ([string]$instruction.wrapper_path)
    }
    catch {
        $result.error = "$($result.error) Additionally failed to restart Host: $($_.Exception.Message)"
        Add-UpdateLog $logPath "RESTART FAILED: $($_.Exception.Message)"
    }
}
finally {
    $result.updated_at = [DateTimeOffset]::UtcNow.ToString('o')
    Write-JsonAtomic $resultPath $result
    Remove-Item -LiteralPath $lockPath -Force -ErrorAction SilentlyContinue
}
