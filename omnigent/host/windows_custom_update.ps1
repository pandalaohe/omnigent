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
    [string]$HostId,
    [string]$ConstraintsDirectory = ''
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

function ConvertTo-NormalizedWindowsPath {
    param([string]$RawPath)
    if ([string]::IsNullOrWhiteSpace($RawPath)) {
        return ''
    }
    return $RawPath.Trim().Trim('"').Replace('/', '\').TrimEnd('\').ToLowerInvariant()
}

function Get-ToolVenvPath {
    param([object]$Instruction)
    if ($null -ne $Instruction -and $Instruction.PSObject.Properties['tool_venv_path'] -and $Instruction.tool_venv_path) {
        return [string]$Instruction.tool_venv_path
    }
    return ''
}

function Get-CliBinPath {
    param([object]$Instruction)
    if ($null -ne $Instruction -and $Instruction.PSObject.Properties['cli_bin_path'] -and $Instruction.cli_bin_path) {
        return [string]$Instruction.cli_bin_path
    }
    return ''
}

function Format-RecoverySteps {
    param([object]$Instruction, [string]$WrapperPath)
    if ($null -ne $Instruction -and $Instruction.PSObject.Properties['recovery_steps'] -and $Instruction.recovery_steps) {
        return [string]$Instruction.recovery_steps
    }
    $toolVenv = Get-ToolVenvPath $Instruction
    if (-not $toolVenv) {
        $toolVenv = Join-Path $env:APPDATA 'uv\tools\omnigent'
    }
    $toolVenvPrefix = $toolVenv.TrimEnd('\', '/') + '\'
    $cliBin = Get-CliBinPath $Instruction
    if (-not $cliBin) {
        $cliBin = Join-Path $HOME '.local\bin\omni.exe'
    }
    $recoveryCmd = if ($null -ne $Instruction -and $Instruction.PSObject.Properties['recovery_command']) {
        [string]$Instruction.recovery_command
    } else {
        'uv tool install --force --reinstall omnigent'
    }
    return "(1) & `"$WrapperPath`" stop; (2) Get-Process | Where-Object { `$_.Path -and (`$_.Path.StartsWith(`"$toolVenvPrefix`", [System.StringComparison]::OrdinalIgnoreCase) -or `$_.Path.Equals(`"$cliBin`", [System.StringComparison]::OrdinalIgnoreCase)) } | Stop-Process -Force; (3) $recoveryCmd; (4) & `"$WrapperPath`" start"
}

function Test-PathHoldsToolVenv {
    param(
        [object]$ProcessRow,
        [string]$ToolVenvPath,
        [string]$CliBinPath
    )
    $venvNorm = ConvertTo-NormalizedWindowsPath $ToolVenvPath
    $cliNorm = ConvertTo-NormalizedWindowsPath $CliBinPath
    $exeNorm = ConvertTo-NormalizedWindowsPath ([string]$ProcessRow.ExecutablePath)
    $cmdNorm = if ($ProcessRow.CommandLine) {
        ([string]$ProcessRow.CommandLine).Replace('/', '\').ToLowerInvariant()
    } else {
        ''
    }
    if ($exeNorm) {
        if ($venvNorm -and ($exeNorm -eq $venvNorm -or $exeNorm.StartsWith("$venvNorm\"))) {
            return $true
        }
        if ($cliNorm -and $exeNorm -eq $cliNorm) {
            return $true
        }
    }
    if ($venvNorm -and $cmdNorm.Contains("$venvNorm\")) {
        return $true
    }
    if ($cliNorm -and $cmdNorm.Contains($cliNorm)) {
        return $true
    }
    foreach ($loadedPath in $ProcessRow.LoadedPaths) {
        $loadedNorm = ConvertTo-NormalizedWindowsPath ([string]$loadedPath)
        if ($venvNorm -and $loadedNorm.StartsWith("$venvNorm\")) {
            return $true
        }
    }
    $nameNorm = if ($ProcessRow.Name) { ([string]$ProcessRow.Name).ToLowerInvariant() } else { '' }
    if (-not $exeNorm -and -not $cmdNorm -and $nameNorm -in @('omni.exe', 'omnigent.exe', 'omni', 'omnigent')) {
        return $true
    }
    return $false
}

function Get-ProcessTable {
    $rows = @()
    $loadedByPid = @{}
    foreach ($process in Get-Process -ErrorAction SilentlyContinue) {
        try {
            $loadedByPid[[int]$process.Id] = @($process.Modules | ForEach-Object { $_.FileName })
        } catch {
            # Protected/exited processes may be uninspectable. Exclusive file
            # access below is the final gate, including non-module file handles.
        }
    }
    try {
        $rows = @(Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object {
            [pscustomobject]@{
                ProcessId = [int]$_.ProcessId
                ParentProcessId = [int]$_.ParentProcessId
                Name = [string]$_.Name
                ExecutablePath = [string]$_.ExecutablePath
                CommandLine = [string]$_.CommandLine
                LoadedPaths = @($loadedByPid[[int]$_.ProcessId])
            }
        })
    }
    catch {
        $rows = @(Get-Process -ErrorAction SilentlyContinue | ForEach-Object {
            $exePath = ''
            try { $exePath = [string]$_.Path } catch {}
            [pscustomobject]@{
                ProcessId = [int]$_.Id
                ParentProcessId = 0
                Name = [string]$_.ProcessName
                ExecutablePath = $exePath
                CommandLine = ''
                LoadedPaths = @($loadedByPid[[int]$_.Id])
            }
        })
    }
    if ($rows.Count -eq 0) {
        throw "Could not enumerate running Windows processes to verify that the tool environment is unlocked."
    }
    return $rows
}

function Get-StoppedHostTreePids {
    param(
        [object[]]$Processes,
        [int[]]$StoppedHostPids,
        [string]$ToolVenvPath,
        [string]$CliBinPath,
        [int[]]$ExcludedPids
    )
    $byPid = @{}
    $childrenByPpid = @{}
    foreach ($proc in $Processes) {
        $pidVal = [int]$proc.ProcessId
        $ppidVal = [int]$proc.ParentProcessId
        $byPid[$pidVal] = $proc
        if (-not $childrenByPpid.ContainsKey($ppidVal)) {
            $childrenByPpid[$ppidVal] = [System.Collections.Generic.List[int]]::new()
        }
        $childrenByPpid[$ppidVal].Add($pidVal)
    }

    $excluded = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($id in $ExcludedPids) {
        if ($id -gt 0) { [void]$excluded.Add([int]$id) }
    }

    $roots = [System.Collections.Generic.HashSet[int]]::new()
    $deadSeeds = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($stoppedPid in $StoppedHostPids) {
        if ($stoppedPid -le 0 -or $excluded.Contains([int]$stoppedPid)) { continue }
        if (-not $byPid.ContainsKey([int]$stoppedPid)) {
            [void]$deadSeeds.Add([int]$stoppedPid)
            continue
        }
        $current = $byPid[[int]$stoppedPid]
        $cmdLower = ([string]$current.CommandLine).ToLowerInvariant()
        $nameLower = ([string]$current.Name).ToLowerInvariant()
        $isHostIdentity = $cmdLower.Contains(' host') -or
            $cmdLower.Contains('omnigent.host._daemon_entry') -or
            $nameLower -in @('omni.exe', 'omnigent.exe', 'omni', 'omnigent') -or
            (-not $cmdLower -and (Test-PathHoldsToolVenv -ProcessRow $current -ToolVenvPath $ToolVenvPath -CliBinPath $CliBinPath))
        if ($isHostIdentity) {
            [void]$roots.Add([int]$stoppedPid)
            $visited = [System.Collections.Generic.HashSet[int]]::new()
            while ($null -ne $current -and [int]$current.ParentProcessId -gt 0 -and -not $visited.Contains([int]$current.ParentProcessId)) {
                $parentPid = [int]$current.ParentProcessId
                [void]$visited.Add($parentPid)
                if (-not $byPid.ContainsKey($parentPid) -or $excluded.Contains($parentPid)) { break }
                $parent = $byPid[$parentPid]
                if (Test-PathHoldsToolVenv -ProcessRow $parent -ToolVenvPath $ToolVenvPath -CliBinPath $CliBinPath) {
                    [void]$roots.Add($parentPid)
                    $current = $parent
                    continue
                }
                break
            }
        }
    }

    $cliNorm = ConvertTo-NormalizedWindowsPath $CliBinPath
    $ownerHomePrefix = if ($cliNorm.EndsWith('\.local\bin\omni.exe')) {
        $cliNorm.Substring(0, $cliNorm.Length - '\.local\bin\omni.exe'.Length) + '\'
    } else {
        ''
    }
    foreach ($proc in $Processes) {
        $pidVal = [int]$proc.ProcessId
        if ($excluded.Contains($pidVal)) { continue }
        $cmdLower = ([string]$proc.CommandLine).Replace('/', '\').ToLowerInvariant()
        $isSupervisorScript = ($cmdLower.Contains('start-omnigenthostdefault.ps1') -or $cmdLower.Contains('launch-omnigenthost.pyw')) -and
            $ownerHomePrefix -and $cmdLower.Contains($ownerHomePrefix)
        if ($isSupervisorScript) {
            [void]$roots.Add($pidVal)
        }
        elseif ($deadSeeds.Contains([int]$proc.ParentProcessId) -and
            (Test-PathHoldsToolVenv -ProcessRow $proc -ToolVenvPath $ToolVenvPath -CliBinPath $CliBinPath)) {
            [void]$roots.Add($pidVal)
        }
    }

    $tree = [System.Collections.Generic.HashSet[int]]::new()
    $queue = [System.Collections.Generic.Queue[int]]::new()
    foreach ($rootPid in $roots) { $queue.Enqueue($rootPid) }
    while ($queue.Count -gt 0) {
        $currentPid = $queue.Dequeue()
        if ($tree.Contains($currentPid) -or $excluded.Contains($currentPid)) { continue }
        [void]$tree.Add($currentPid)
        if ($childrenByPpid.ContainsKey($currentPid)) {
            foreach ($childPid in $childrenByPpid[$currentPid]) {
                if (-not $tree.Contains($childPid) -and -not $excluded.Contains($childPid)) {
                    $queue.Enqueue($childPid)
                }
            }
        }
    }
    return @($tree | Where-Object { $byPid.ContainsKey($_) } | Sort-Object)
}

function Get-VenvLockingProcesses {
    param(
        [object[]]$Processes,
        [string]$ToolVenvPath,
        [string]$CliBinPath,
        [int[]]$ExcludedPids
    )
    $excluded = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($id in $ExcludedPids) {
        if ($id -gt 0) { [void]$excluded.Add([int]$id) }
    }
    return @($Processes | Where-Object {
        -not $excluded.Contains([int]$_.ProcessId) -and
        (Test-PathHoldsToolVenv -ProcessRow $_ -ToolVenvPath $ToolVenvPath -CliBinPath $CliBinPath)
    })
}

function Wait-CustomHostVenvReleased {
    param(
        [object]$Instruction,
        [string]$LogPath,
        [int]$HelperPid,
        [int]$GraceSeconds = 2,
        [int]$TimeoutSeconds = 15
    )
    $toolVenvPath = Get-ToolVenvPath $Instruction
    $cliBinPath = Get-CliBinPath $Instruction
    $stoppedHostPids = @()
    if ($null -ne $Instruction.previous_records) {
        foreach ($record in $Instruction.previous_records) {
            if ($null -ne $record.pid -and [int]$record.pid -gt 0) {
                $stoppedHostPids += [int]$record.pid
            }
        }
    }

    $graceDeadline = [DateTimeOffset]::UtcNow.AddSeconds($GraceSeconds)
    while ([DateTimeOffset]::UtcNow -lt $graceDeadline) {
        $processes = Get-ProcessTable
        $treePids = Get-StoppedHostTreePids -Processes $processes -StoppedHostPids $stoppedHostPids -ToolVenvPath $toolVenvPath -CliBinPath $cliBinPath -ExcludedPids @($HelperPid)
        if ($treePids.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 100
    }

    $processes = Get-ProcessTable
    $treePids = Get-StoppedHostTreePids -Processes $processes -StoppedHostPids $stoppedHostPids -ToolVenvPath $toolVenvPath -CliBinPath $cliBinPath -ExcludedPids @($HelperPid)
    if ($treePids.Count -gt 0) {
        Add-UpdateLog $LogPath "Stopping leftover Host tree process(es): $($treePids -join ', ')."
        foreach ($targetPid in $treePids) {
            Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
        }
    }

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    while ($true) {
        $processes = Get-ProcessTable
        $remainingTree = Get-StoppedHostTreePids -Processes $processes -StoppedHostPids $stoppedHostPids -ToolVenvPath $toolVenvPath -CliBinPath $cliBinPath -ExcludedPids @($HelperPid)
        foreach ($targetPid in $remainingTree) {
            Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
        }
        $locking = Get-VenvLockingProcesses -Processes $processes -ToolVenvPath $toolVenvPath -CliBinPath $cliBinPath -ExcludedPids @($HelperPid)
        if ($locking.Count -eq 0) {
            Assert-ToolFilesReleased $toolVenvPath
            foreach ($launcher in Get-ToolLaunchers $cliBinPath) {
                if (Test-Path -LiteralPath $launcher) {
                    $handle = [IO.File]::Open($launcher, 'Open', 'ReadWrite', 'None')
                    $handle.Dispose()
                }
            }
            return
        }
        if ([DateTimeOffset]::UtcNow -ge $deadline) {
            $summary = ($locking | ForEach-Object { "$($_.Name) (pid $($_.ProcessId))" }) -join ', '
            $recoverySteps = Format-RecoverySteps -Instruction $Instruction -WrapperPath ([string]$Instruction.wrapper_path)
            throw "Cannot update custom Host while tool files are still in use by: $summary. Aborted before modifying the installation. Recovery: $recoverySteps"
        }
        Start-Sleep -Milliseconds 100
    }
}

function Assert-ToolFilesReleased {
    param([string]$ToolVenvPath)
    if (-not $ToolVenvPath -or -not (Test-Path -LiteralPath $ToolVenvPath -PathType Container)) {
        throw 'Cannot verify the existing tool environment; installation was not modified.'
    }
    # ReadWrite + FileShare.None also refuses mapped images whose loader has
    # already closed its original file handle. Do not write through the handle.
    foreach ($file in Get-ChildItem -LiteralPath $ToolVenvPath -File -Recurse -Force -ErrorAction Stop) {
        try {
            $handle = [IO.File]::Open($file.FullName, 'Open', 'ReadWrite', 'None')
            $handle.Dispose()
        } catch {
            throw "Cannot release tool file '$($file.FullName)': $($_.Exception.Message). Installation was not modified."
        }
    }
}

function Get-ToolLaunchers {
    param([string]$CliBinPath)
    $directory = Split-Path -Parent $CliBinPath
    return @((Join-Path $directory 'omni.exe'), (Join-Path $directory 'omnigent.exe'))
}

function Restore-ToolInstallation {
    param([object]$Instruction, [System.Collections.IDictionary]$Result)
    $backup = [string]$Result.backup_path
    if (-not $backup) { return }
    $venv = [string]$Instruction.tool_venv_path
    $transaction = Split-Path -Parent $backup
    if (-not $Result.venv_restored) {
        if (-not (Test-Path -LiteralPath $backup)) { return }
        if (Test-Path -LiteralPath $venv) {
            # Keep failed output for diagnosis. Never recursively remove a venv.
            Move-Item -LiteralPath $venv -Destination (Join-Path $transaction 'failed') -ErrorAction Stop
        }
        Move-Item -LiteralPath $backup -Destination $venv -ErrorAction Stop
        $Result.venv_restored = $true
    }
    foreach ($launcher in Get-ToolLaunchers ([string]$Instruction.cli_bin_path)) {
        $saved = Join-Path $transaction (Split-Path -Leaf $launcher)
        if (Test-Path -LiteralPath $saved) {
            Copy-Item -LiteralPath $saved -Destination $launcher -Force -ErrorAction Stop
        }
    }
    $Result.restored = $true
}

function Invoke-TransactionalInstall {
    param(
        [object]$Instruction,
        [string]$TargetCommit,
        [string]$LogPath,
        [System.Collections.IDictionary]$Result
    )
    $venv = [IO.Path]::GetFullPath([string]$Instruction.tool_venv_path)
    Assert-ToolFilesReleased $venv
    $transaction = "$venv.update-$([guid]::NewGuid().ToString('N'))"
    New-Item -ItemType Directory -Path $transaction -ErrorAction Stop | Out-Null
    $Result.backup_path = Join-Path $transaction 'previous'
    $Result.restored = $false
    if ($Instruction.result_path) { Write-JsonAtomic ([string]$Instruction.result_path) $Result }
    foreach ($launcher in Get-ToolLaunchers ([string]$Instruction.cli_bin_path)) {
        if (Test-Path -LiteralPath $launcher) {
            Copy-Item -LiteralPath $launcher -Destination $transaction -ErrorAction Stop
        }
    }
    # If the rename is refused, uv never runs. The working venv stays untouched.
    Move-Item -LiteralPath $venv -Destination $Result.backup_path -ErrorAction Stop
    Add-UpdateLog $LogPath "Previous installation retained at $($Result.backup_path)."
    try {
        $installArguments = @($Instruction.install_argv | ForEach-Object { [string]$_ })
        Add-UpdateLog $LogPath 'Running custom Host installer.'
        $install = Invoke-CapturedProcess $installArguments @{ OMNIGENT_SKIP_WEB_UI = 'true' }
        if ($install.Stdout) { Add-UpdateLog $LogPath $install.Stdout.Trim() }
        if ($install.Stderr) { Add-UpdateLog $LogPath $install.Stderr.Trim() }
        if ($install.ExitCode -ne 0) {
            throw "Installer exited with status $($install.ExitCode)."
        }
        $probeArguments = @($Instruction.probe_argv | ForEach-Object { [string]$_ })
        $probe = Invoke-CapturedProcess $probeArguments
        $installedCommit = $probe.Stdout.Trim().ToLowerInvariant()
        if ($probe.ExitCode -ne 0 -or $installedCommit -ne $TargetCommit) {
            throw "Installation import/commit check failed (exit $($probe.ExitCode), commit '$installedCommit'): $($probe.Stderr)"
        }
        $versionArguments = @($Instruction.version_argv | ForEach-Object { [string]$_ })
        if ($versionArguments.Count -eq 0) { throw 'Missing CLI version check.' }
        $version = Invoke-CapturedProcess $versionArguments
        if ($version.ExitCode -ne 0) {
            throw "CLI version check failed (exit $($version.ExitCode)): $($version.Stderr)"
        }
    } catch {
        $failure = $_.Exception.Message
        try { Restore-ToolInstallation -Instruction $Instruction -Result $Result }
        catch { throw "$failure Restore incomplete; recovery files: '$transaction': $($_.Exception.Message)" }
        throw "$failure Previous installation restored."
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

function Wait-HostReconnected {
    param([object]$Instruction)
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds(60)
    do {
        if (Test-HostReconnected $Instruction) { return $true }
        if ([DateTimeOffset]::UtcNow -ge $deadline) { return $false }
        Start-Sleep -Seconds 1
    } while ($true)
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
$restartAttempted = $false

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
        constraints_dir = $ConstraintsDirectory
    }
    foreach ($entry in $expected.GetEnumerator()) {
        if ([string]$instruction.($entry.Key) -ne [string]$entry.Value) {
            throw "Custom Host update instruction field '$($entry.Key)' does not match the launcher."
        }
    }

    $result.status = 'running'
    Write-JsonAtomic $ResultPath $result
    Add-UpdateLog $LogPath "Waiting for CLI process $($instruction.parent_pid) to exit."
    if ([int]$instruction.parent_pid -gt 0) {
        Wait-Process -Id ([int]$instruction.parent_pid) -Timeout 120 -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
    Wait-CustomHostVenvReleased -Instruction $instruction -LogPath $LogPath -HelperPid $PID

    Write-JsonAtomic ([string]$instruction.rollback_path) ([ordered]@{
        schema_version = 1
        commit_sha = $OldCommit
    })

    Invoke-TransactionalInstall -Instruction $instruction -TargetCommit $TargetCommit -LogPath $LogPath -Result $result

    $restartAttempted = $true
    Restart-HostSupervisor $WrapperPath
    if (-not (Wait-HostReconnected $instruction)) {
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
    if ($instruction -and $result.backup_path -and -not $result.restored) {
        try {
            if ($restartAttempted) {
                $output = & $WrapperPath stop 2>&1
                if ($LASTEXITCODE -ne 0) { throw "Supervisor pause failed: $output" }
                $stop = Invoke-CapturedProcess @([string]$instruction.cli_bin_path, 'host', 'stop', '--all', '--daemon-only', '--force')
                if ($stop.ExitCode -ne 0) { throw "Updated Host stop failed (exit $($stop.ExitCode))." }
                Wait-CustomHostVenvReleased -Instruction $instruction -LogPath $LogPath -HelperPid $PID
            }
            Restore-ToolInstallation -Instruction $instruction -Result $result
        } catch {
            $result.error = "$($result.error) Restore failed: $($_.Exception.Message). Backup: $($result.backup_path)"
        }
    }
    try {
        Restart-HostSupervisor $WrapperPath
        if ($result.restored) {
            if (-not (Wait-HostReconnected $instruction)) {
                throw 'The restored Host did not reconnect within 60 seconds.'
            }
            $result.recovery_status = 'online'
        }
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
    # Keep constraints and transaction directories with the failure evidence.
}

exit $exitCode
