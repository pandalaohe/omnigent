param(
    [Parameter(Mandatory = $true)]
    [string]$InstructionPath,
    [Parameter(Mandatory = $true)]
    [string]$ResultPath,
    [Parameter(Mandatory = $true)]
    [string]$LogPath,
    [Parameter(Mandatory = $true)]
    [string]$LockPath,
    [Parameter(Mandatory = $true)]
    [string]$WrapperPath,
    [Parameter(Mandatory = $true)]
    [string]$OldCommit,
    [Parameter(Mandatory = $true)]
    [string]$TargetCommit,
    [Parameter(Mandatory = $true)]
    [string]$HostId
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

$result = [ordered]@{
    schema_version = 1
    status = 'starting'
    old_commit = $OldCommit
    target_commit = $TargetCommit
    helper_pid = $PID
    error = $null
    updated_at = [DateTimeOffset]::UtcNow.ToString('o')
}
$exitCode = 0
$instruction = $null

try {
    $instruction = Get-Content -LiteralPath $InstructionPath -Raw | ConvertFrom-Json
    if ([int]$instruction.schema_version -ne 1) {
        throw "Unsupported custom Host update instruction schema '$($instruction.schema_version)'."
    }
    $expected = [ordered]@{
        result_path = $ResultPath
        log_path = $LogPath
        lock_path = $LockPath
        wrapper_path = $WrapperPath
        old_commit = $OldCommit
        target_commit = $TargetCommit
        host_id = $HostId
    }
    foreach ($entry in $expected.GetEnumerator()) {
        if ([string]$instruction.($entry.Key) -ne [string]$entry.Value) {
            throw "Custom Host update instruction field '$($entry.Key)' does not match the launcher."
        }
    }

    $result.status = 'running'
    Write-JsonAtomic $ResultPath $result
    Add-UpdateLog $LogPath "Waiting for CLI process $($instruction.parent_pid) to exit."
    Wait-Process -Id ([int]$instruction.parent_pid) -Timeout 120 -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 500

    Write-JsonAtomic ([string]$instruction.rollback_path) ([ordered]@{
        schema_version = 1
        commit_sha = $OldCommit
    })

    $installArguments = @($instruction.install_argv | ForEach-Object { [string]$_ })
    Add-UpdateLog $LogPath "Running custom Host installer."
    $install = Invoke-CapturedProcess $installArguments @{
        OMNIGENT_SKIP_WEB_UI = 'true'
    }
    if ($install.Stdout) { Add-UpdateLog $LogPath $install.Stdout.Trim() }
    if ($install.Stderr) { Add-UpdateLog $LogPath $install.Stderr.Trim() }
    if ($install.ExitCode -ne 0) {
        throw "Installer exited with status $($install.ExitCode). Recovery: $($instruction.recovery_command)"
    }

    $probeArguments = @($instruction.probe_argv | ForEach-Object { [string]$_ })
    $probe = Invoke-CapturedProcess $probeArguments
    $installedCommit = $probe.Stdout.Trim().ToLowerInvariant()
    if ($probe.ExitCode -ne 0 -or $installedCommit -ne $TargetCommit) {
        throw "Installed commit '$installedCommit' did not match '$TargetCommit'."
    }

    Restart-HostSupervisor $WrapperPath
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
    Add-UpdateLog $LogPath "Custom Host update completed and reconnected."
}
catch {
    $exitCode = 1
    $result.status = 'failed'
    $result.error = $_.Exception.Message
    try {
        Add-UpdateLog $LogPath "FAILED: $($result.error)"
    }
    catch {
        [Console]::Error.WriteLine("Failed to write custom Host update log: $($_.Exception.Message)")
    }
    try {
        Restart-HostSupervisor $WrapperPath
    }
    catch {
        $result.error = "$($result.error) Additionally failed to restart Host: $($_.Exception.Message)"
        try {
            Add-UpdateLog $LogPath "RESTART FAILED: $($_.Exception.Message)"
        }
        catch {
            [Console]::Error.WriteLine("Failed to write custom Host restart error: $($_.Exception.Message)")
        }
    }
}
finally {
    $result.updated_at = [DateTimeOffset]::UtcNow.ToString('o')
    try {
        Write-JsonAtomic $ResultPath $result
    }
    catch {
        [Console]::Error.WriteLine("Failed to write custom Host update result: $($_.Exception.Message)")
    }
    Remove-Item -LiteralPath $LockPath -Force -ErrorAction SilentlyContinue
}

exit $exitCode
