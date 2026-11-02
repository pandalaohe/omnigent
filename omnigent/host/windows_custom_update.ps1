param(
    [Parameter(Mandatory = $true)]
    [string]$InstructionPath,
    [string]$ResultPath = '',
    [string]$LogPath = '',
    [string]$LockPath = '',
    [string]$WrapperPath = '',
    [string]$OldCommit = '',
    [string]$TargetCommit = '',
    [string]$HostId = '',
    [string]$ConstraintsDirectory = '',
    [switch]$RecoverOnly,
    [switch]$ResumeAutostart,
    [switch]$InstallerOnly,
    [string]$TransactionPath = ''
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
        [hashtable]$Environment = @{},
        [int]$TimeoutSeconds = 30
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $Arguments[0]
    foreach ($argument in ($Arguments | Select-Object -Skip 1)) {
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
    try {
        if (-not $process.WaitForExit([Math]::Max(1, $TimeoutSeconds) * 1000)) {
            try { $process.Kill($true) } catch [InvalidOperationException] {}
            if (-not $process.WaitForExit(5000)) { throw 'Timed-out native command did not exit.' }
            throw "Native command timed out after $TimeoutSeconds seconds: $($Arguments[0])"
        }
        if (-not $stdoutTask.Wait(5000) -or -not $stderrTask.Wait(5000)) {
            throw "Native command output did not close: $($Arguments[0])"
        }
        return [ordered]@{
            ExitCode = $process.ExitCode
            Stdout = $stdoutTask.GetAwaiter().GetResult()
            Stderr = $stderrTask.GetAwaiter().GetResult()
        }
    } finally {
        $process.Dispose()
    }
}

function Restart-HostSupervisor {
    param([string]$WrapperPath, [string]$Action = 'start')
    $output = Invoke-CapturedProcess @(
        (Get-Command pwsh).Source, '-NoProfile', '-Command',
        '& $env:OMNIGENT_HOST_SERVICE_WRAPPER $env:OMNIGENT_HOST_SERVICE_ACTION; exit $LASTEXITCODE'
    ) @{
        OMNIGENT_HOST_SERVICE_WRAPPER = $WrapperPath
        OMNIGENT_HOST_SERVICE_ACTION = $Action
    } -TimeoutSeconds 30
    if ($output.ExitCode -ne 0) {
        throw "Host supervisor $Action exited with status $($output.ExitCode): $($output.Stderr)"
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
    $createdByPid = @{}
    foreach ($process in Get-Process -ErrorAction SilentlyContinue) {
        try {
            $createdByPid[[int]$process.Id] = $process.StartTime.ToFileTimeUtc() / 10000000.0 - 11644473600.0
            $loadedByPid[[int]$process.Id] = @($process.Modules | ForEach-Object { $_.FileName })
        } catch {
            # Protected/exited processes may be uninspectable. Exclusive file
            # access below is the final gate, including non-module file handles.
        }
    }
    try {
        $rows = @(Get-CimInstance Win32_Process -OperationTimeoutSec 5 -ErrorAction Stop | ForEach-Object {
            [pscustomobject]@{
                ProcessId = [int]$_.ProcessId
                ParentProcessId = [int]$_.ParentProcessId
                Name = [string]$_.Name
                ExecutablePath = [string]$_.ExecutablePath
                CommandLine = [string]$_.CommandLine
                LoadedPaths = @($loadedByPid[[int]$_.ProcessId])
                CreatedAt = [double]$createdByPid[[int]$_.ProcessId]
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
                CreatedAt = [double]$createdByPid[[int]$_.Id]
            }
        })
    }
    if ($rows.Count -eq 0) {
        throw "Could not enumerate running Windows processes to verify that the tool environment is unlocked."
    }
    return $rows
}

function Test-HostLauncher {
    param([object]$Row, [string]$CliBinPath)
    $cmd = ([string]$Row.CommandLine).Replace('/', '\').ToLowerInvariant()
    $cli = ConvertTo-NormalizedWindowsPath $CliBinPath
    $exe = ConvertTo-NormalizedWindowsPath ([string]$Row.ExecutablePath)
    if ($exe -eq $cli -and $cmd -match '\s+host(?:\s|$)') { return $true }
    if (-not $cli.EndsWith('\.local\bin\omni.exe')) { return $false }
    $homePrefix = $cli.Substring(0, $cli.Length - '\.local\bin\omni.exe'.Length)
    return (([string]$Row.Name).ToLowerInvariant() -in @('pwsh.exe', 'python.exe', 'pythonw.exe')) -and
        ($cmd.Contains("$homePrefix\.omnigent\autostart\start-omnigenthostdefault.ps1") -or
         $cmd.Contains("$homePrefix\.omnigent\autostart\launch-omnigenthost.pyw"))
}

function Test-HostDaemon {
    param([object]$Row)
    return ([string]$Row.CommandLine) -match '(?i)(?:^|\s)-m\s+omnigent\.(host\._daemon_entry|cli\s+host)(?:\s|$)'
}

function Get-StoppedHostTreePids {
    param([object[]]$Processes, [int[]]$StoppedHostPids, [string]$ToolVenvPath,
          [string]$CliBinPath, [int[]]$ExcludedPids)
    $byPid = @{}
    foreach ($row in $Processes) { $byPid[[int]$row.ProcessId] = $row }
    $selected = [Collections.Generic.HashSet[int]]::new()
    foreach ($seed in $StoppedHostPids) {
        $row = $byPid[[int]$seed]
        if (-not $row -or $row.CreatedAt -le 0 -or $seed -in $ExcludedPids -or
            -not ((Test-HostDaemon $row) -or (Test-HostLauncher $row $CliBinPath))) { continue }
        [void]$selected.Add([int]$seed)
        $visited = [Collections.Generic.HashSet[int]]::new()
        while ($row -and $visited.Add([int]$row.ParentProcessId)) {
            $parent = $byPid[[int]$row.ParentProcessId]
            if (-not $parent -or $parent.CreatedAt -le 0 -or $parent.CreatedAt -gt $row.CreatedAt -or $parent.ProcessId -in $ExcludedPids -or
                -not (Test-HostLauncher $parent $CliBinPath)) { break }
            [void]$selected.Add([int]$parent.ProcessId)
            $row = $parent
        }
    }
    do {
        $changed = $false
        foreach ($row in $Processes) {
            if ($row.ProcessId -notin $ExcludedPids -and $selected.Contains([int]$row.ParentProcessId) -and
                $row.CreatedAt -ge $byPid[[int]$row.ParentProcessId].CreatedAt -and
                -not $selected.Contains([int]$row.ProcessId)) {
                [void]$selected.Add([int]$row.ProcessId)
                $changed = $true
            }
        }
    } while ($changed)
    return @($selected | Sort-Object)
}

function Get-ManagedHostSnapshot {
    param([object]$Instruction)
    $processes = @(Get-ProcessTable)
    $seeds = @($processes | Where-Object {
        (Test-HostDaemon $_) -and (Test-PathHoldsToolVenv $_ $Instruction.tool_venv_path $Instruction.cli_bin_path)
    } | ForEach-Object { [int]$_.ProcessId })
    $ids = @(Get-StoppedHostTreePids $processes $seeds $Instruction.tool_venv_path $Instruction.cli_bin_path @($PID))
    return @($processes | Where-Object { $_.ProcessId -in $ids -and $_.CreatedAt -gt 0 } |
        ForEach-Object { [pscustomobject]@{ pid = [int]$_.ProcessId; created_at = [double]$_.CreatedAt } })
}

function Stop-ProcessIdentity {
    param([object]$Identity)
    if ([double]$Identity.created_at -le 0) { return }
    $process = Get-Process -Id ([int]$Identity.pid) -ErrorAction SilentlyContinue
    if (-not $process) { return }
    try {
        $created = $process.StartTime.ToFileTimeUtc() / 10000000.0 - 11644473600.0
        if ([Math]::Abs($created - [double]$Identity.created_at) -gt 0.00001) { return }
        $process.Kill()
        if (-not $process.WaitForExit(5000)) { throw "Process $($Identity.pid) did not exit." }
    } finally { $process.Dispose() }
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
    param([object]$Instruction, [string]$LogPath, [int]$HelperPid,
          [int]$GraceSeconds = 2, [int]$TimeoutSeconds = 15, [object[]]$Identities = @())
    $toolVenvPath = Get-ToolVenvPath $Instruction
    $cliBinPath = Get-CliBinPath $Instruction
    if (-not $Identities.Count) {
        $Identities = @($Instruction.previous_processes | Where-Object { $null -ne $_ })
    }
    if (-not $Identities.Count -and @($Instruction.previous_records | Where-Object { $null -ne $_ }).Count -gt 0) {
        $processes = @(Get-ProcessTable)
        $seeds = @($Instruction.previous_records | ForEach-Object { [int]$_.pid })
        $ids = @(Get-StoppedHostTreePids $processes $seeds $toolVenvPath $cliBinPath @($HelperPid))
        $Identities = @($processes | Where-Object { $_.ProcessId -in $ids } | ForEach-Object {
            [pscustomobject]@{ pid = [int]$_.ProcessId; created_at = [double]$_.CreatedAt }
        })
    }
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        foreach ($identity in $Identities) {
            if ($identity.pid -ne $HelperPid) { Stop-ProcessIdentity $identity }
        }
        $locking = @(Get-VenvLockingProcesses @(Get-ProcessTable) $toolVenvPath $cliBinPath @($HelperPid))
        $exclusiveError = ''
        if (-not $locking.Count) {
            try {
                if (Test-Path -LiteralPath $toolVenvPath) { Assert-ToolFilesReleased $toolVenvPath }
                foreach ($launcher in Get-ToolLaunchers $cliBinPath) {
                    if (Test-Path -LiteralPath $launcher) {
                        $handle = [IO.File]::Open($launcher, 'Open', 'ReadWrite', 'None')
                        $handle.Dispose()
                    }
                }
                return
            } catch { $exclusiveError = $_.Exception.Message }
        }
        if ([DateTimeOffset]::UtcNow -ge $deadline) {
            $summary = ($locking | ForEach-Object { "$($_.Name) (pid $($_.ProcessId))" }) -join ', '
            throw "Installation files held by: $summary $exclusiveError. Unrelated holders were not terminated."
        }
        Start-Sleep -Milliseconds 100
    } while ($true)
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

function Save-Transaction {
    param([object]$Instruction, [Collections.IDictionary]$Result, [string]$Phase)
    $Result.phase = $Phase
    $Result.updated_at = [DateTimeOffset]::UtcNow.ToString('o')
    if ($Instruction.result_path) { Write-JsonAtomic $Instruction.result_path $Result }
    if ($Result.backup_path) {
        Write-JsonAtomic (Join-Path (Split-Path -Parent $Result.backup_path) 'transaction.json') $Result
    }
}

function Assert-TransactionPath {
    param([string]$Path, [object]$Instruction)
    $venv = [IO.Path]::GetFullPath([string]$Instruction.tool_venv_path)
    $absolute = [IO.Path]::GetFullPath($Path)
    if ((Split-Path -Parent $absolute) -ne (Split-Path -Parent $venv) -or
        (Split-Path -Leaf $absolute) -notmatch ('^' + [regex]::Escape((Split-Path -Leaf $venv)) + '\.update-[0-9a-f]{32}$') -or
        (Get-Item -LiteralPath $absolute -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Invalid transaction directory: $absolute"
    }
    return $absolute
}

function Enable-InstallerJob {
    # The guard, not the updater or Host, owns this kill-on-close job. A killed
    # guard cannot leave an installer child writing into the environment.
    Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class OmnigentInstallerJob {
    [StructLayout(LayoutKind.Sequential)] struct Basic {
        public long ProcessTime, JobTime;
        public uint Flags;
        public UIntPtr Min, Max;
        public uint Count;
        public UIntPtr Affinity;
        public uint Priority, Scheduling;
    }
    [StructLayout(LayoutKind.Sequential)] struct Extended {
        public Basic Basic;
        public ulong ReadOps, WriteOps, OtherOps, ReadBytes, WriteBytes, OtherBytes;
        public UIntPtr ProcessMemory, JobMemory, PeakProcess, PeakJob;
    }
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr a, string n);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr j, int c, ref Extended i, uint s);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr j, IntPtr p);
    [DllImport("kernel32.dll")] static extern IntPtr GetCurrentProcess();
    static IntPtr job;
    public static void Enable() {
        job = CreateJobObject(IntPtr.Zero, null);
        var info = new Extended();
        info.Basic.Flags = 0x2000;
        if (job == IntPtr.Zero || !SetInformationJobObject(job, 9, ref info, (uint)Marshal.SizeOf(info))
            || !AssignProcessToJobObject(job, GetCurrentProcess()))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error());
    }
}
'@
    [OmnigentInstallerJob]::Enable()
}

function Invoke-Installer {
    param([object]$Instruction, [string]$Transaction)
    $owner = Get-Process -Id $PID
    Write-JsonAtomic (Join-Path $Transaction 'installer-request.json') @{
        parent_pid = $PID
        parent_created_at = ($owner.StartTime.ToFileTimeUtc() / 10000000.0 - 11644473600.0)
        arguments = @($Instruction.install_argv)
    }
    $guard = Invoke-CapturedProcess @(
        (Get-Command pwsh).Source, '-NoProfile', '-File', $Instruction.helper_path,
        '-InstructionPath', $InstructionPath, '-InstallerOnly', '-TransactionPath', $Transaction
    ) -TimeoutSeconds 930
    $output = Join-Path $Transaction 'installer-output.json'
    if (-not (Test-Path -LiteralPath $output)) { throw "Installer guard failed: $($guard.Stderr)" }
    return Get-Content -LiteralPath $output -Raw | ConvertFrom-Json
}

function Stop-InstallerGuard {
    param([string]$Transaction)
    $identityPath = Join-Path $Transaction 'installer.json'
    if (Test-Path -LiteralPath $identityPath) {
        Stop-ProcessIdentity (Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json)
        return
    }
    foreach ($row in Get-ProcessTable) {
        $cmd = ([string]$row.CommandLine).Replace('/', '\').ToLowerInvariant()
        if ($cmd.Contains('-installeronly') -and
            $cmd.Contains((ConvertTo-NormalizedWindowsPath $Transaction)) -and
            $row.Name -eq 'pwsh.exe') {
            Stop-ProcessIdentity ([pscustomobject]@{ pid = $row.ProcessId; created_at = $row.CreatedAt })
        }
    }
}

function Recover-InterruptedTransaction {
    param([object]$Instruction, [Collections.IDictionary]$Result)
    if (-not $Result.backup_path -or $Result.phase -eq 'completed') { return }
    if ($Result.phase -eq 'restored') {
        if ($Result.status -eq 'running') {
            $Result.status = 'failed'
            $Result.error = 'Interrupted update had restored the previous installation.'
            Save-Transaction $Instruction $Result 'restored'
        }
        Remove-Item -LiteralPath $Instruction.lock_path -Force -ErrorAction SilentlyContinue
        return
    }
    $transaction = Assert-TransactionPath (Split-Path -Parent $Result.backup_path) $Instruction
    if ([IO.Path]::GetFullPath($Result.backup_path) -ne (Join-Path $transaction 'previous')) {
        throw 'Invalid backup path.'
    }
    $interruptedPhase = $Result.phase
    Stop-InstallerGuard $transaction
    $identities = @(Get-ManagedHostSnapshot $Instruction)
    Wait-CustomHostVenvReleased $Instruction $Instruction.log_path $PID -Identities $identities
    Restore-ToolInstallation $Instruction $Result
    $Result.status = 'failed'
    $Result.interrupted_phase = $interruptedPhase
    $Result.error = "Interrupted update recovered from phase '$interruptedPhase'."
    Save-Transaction $Instruction $Result 'restored'
    Remove-Item -LiteralPath $Instruction.lock_path -Force -ErrorAction SilentlyContinue
}

function Complete-Transaction {
    param([object]$Instruction, [Collections.IDictionary]$Result)
    Save-Transaction $Instruction $Result 'completed'
    if ($Instruction.constraints_dir -and (Test-Path -LiteralPath $Instruction.constraints_dir)) {
        $constraints = [IO.Path]::GetFullPath($Instruction.constraints_dir)
        if ((Split-Path -Parent $constraints) -ne (Split-Path -Parent $InstructionPath) -or
            (Split-Path -Leaf $constraints) -notlike 'locked-install-*') {
            throw 'Invalid constraints retention path.'
        }
        $transaction = Assert-TransactionPath (Split-Path -Parent $Result.backup_path) $Instruction
        Move-Item -LiteralPath $constraints -Destination (Join-Path $transaction 'constraints') -ErrorAction Stop
    }
    $venv = [IO.Path]::GetFullPath([string]$Instruction.tool_venv_path)
    $transactions = @(Get-ChildItem -LiteralPath (Split-Path -Parent $venv) -Directory -Force |
        Where-Object { $_.Name -match ('^' + [regex]::Escape((Split-Path -Leaf $venv)) + '\.update-[0-9a-f]{32}$') } |
        ForEach-Object {
            $journal = Join-Path $_.FullName 'transaction.json'
            if (Test-Path -LiteralPath $journal) {
                $receipt = Get-Content -LiteralPath $journal -Raw | ConvertFrom-Json
                if ($receipt.phase -eq 'completed' -and $receipt.status -eq 'complete') {
                    [pscustomobject]@{ Path = $_.FullName; Updated = $receipt.updated_at }
                }
            }
        })
    $current = [IO.Path]::GetFullPath((Split-Path -Parent $Result.backup_path))
    foreach ($old in ($transactions | Where-Object { [IO.Path]::GetFullPath($_.Path) -ne $current })) {
        $absolute = Assert-TransactionPath $old.Path $Instruction
        if (@(Get-ChildItem -LiteralPath $absolute -Recurse -Force -Attributes ReparsePoint).Count) {
            throw "Refusing retention cleanup through a reparse point: $absolute"
        }
        Assert-ToolFilesReleased $absolute
        # The resolved GUID directory is a verified successful transaction in
        # the same tool directory. Incomplete journals never enter this set.
        Remove-Item -LiteralPath $absolute -Recurse -Force -ErrorAction Stop
    }
}

function Restore-ToolInstallation {
    param([object]$Instruction, [System.Collections.IDictionary]$Result)
    $backup = [string]$Result.backup_path
    if (-not $backup) { return }
    $venv = [string]$Instruction.tool_venv_path
    $transaction = Split-Path -Parent $backup
    if (-not $Result.venv_restored) {
        if (-not (Test-Path -LiteralPath $backup)) {
            if ($Result.phase -in @('prepared', 'moving_old', 'restoring_venv') -and
                (Test-Path -LiteralPath $venv -PathType Container)) {
                $Result.venv_restored = $true
            } else { throw "Previous installation missing at $backup." }
        } else {
            if (Test-Path -LiteralPath $venv) {
                # Keep failed output for diagnosis. Never recursively remove a venv.
                if (Test-Path -LiteralPath (Join-Path $transaction 'failed')) {
                    throw 'Another replacement environment appeared during restoration.'
                }
                Move-Item -LiteralPath $venv -Destination (Join-Path $transaction 'failed') -ErrorAction Stop
            }
            Save-Transaction $Instruction $Result 'restoring_venv'
            Move-Item -LiteralPath $backup -Destination $venv -ErrorAction Stop
            $Result.venv_restored = $true
        }
        Save-Transaction $Instruction $Result 'restoring_launchers'
    }
    foreach ($launcher in Get-ToolLaunchers ([string]$Instruction.cli_bin_path)) {
        $saved = Join-Path $transaction (Split-Path -Leaf $launcher)
        if (Test-Path -LiteralPath $saved) {
            Copy-Item -LiteralPath $saved -Destination $launcher -Force -ErrorAction Stop
        }
    }
    $Result.restored = $true
    Save-Transaction $Instruction $Result 'restored'
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
    Save-Transaction $Instruction $Result 'prepared'
    foreach ($launcher in Get-ToolLaunchers ([string]$Instruction.cli_bin_path)) {
        if (Test-Path -LiteralPath $launcher) {
            Copy-Item -LiteralPath $launcher -Destination $transaction -ErrorAction Stop
        }
    }
    # If the rename is refused, uv never runs. The working venv stays untouched.
    Save-Transaction $Instruction $Result 'moving_old'
    Move-Item -LiteralPath $venv -Destination $Result.backup_path -ErrorAction Stop
    Save-Transaction $Instruction $Result 'old_moved'
    Add-UpdateLog $LogPath "Previous installation retained at $($Result.backup_path)."
    try {
        Add-UpdateLog $LogPath 'Running custom Host installer.'
        Save-Transaction $Instruction $Result 'installing'
        $install = Invoke-Installer $Instruction $transaction
        if ($install.Stdout) { Add-UpdateLog $LogPath $install.Stdout.Trim() }
        if ($install.Stderr) { Add-UpdateLog $LogPath $install.Stderr.Trim() }
        if ($install.ExitCode -ne 0) {
            throw "Installer exited with status $($install.ExitCode)."
        }
        Save-Transaction $Instruction $Result 'validating'
        $probeArguments = @($Instruction.probe_argv | ForEach-Object { [string]$_ })
        $probe = Invoke-CapturedProcess $probeArguments
        $installedCommit = $probe.Stdout.Trim().ToLowerInvariant()
        if ($probe.ExitCode -ne 0 -or $installedCommit -ne $TargetCommit) {
            throw "Installation import/commit check failed (exit $($probe.ExitCode), commit '$installedCommit'): $($probe.Stderr)"
        }
        $versionArguments = @($Instruction.version_argv | ForEach-Object { [string]$_ })
        if ($versionArguments.Count -eq 0) { throw 'Missing CLI version check.' }
        $version = Invoke-CapturedProcess $versionArguments
        $versionCommit = [regex]::Match($version.Stdout, '(?i)\(([0-9a-f]{8,40}),').Groups[1].Value
        if ($version.ExitCode -ne 0 -or -not $versionCommit -or
            -not $TargetCommit.StartsWith($versionCommit, [StringComparison]::OrdinalIgnoreCase)) {
            throw "CLI version check failed (exit $($version.ExitCode)): $($version.Stderr)"
        }
    } catch {
        $failure = $_.Exception.Message
        try {
            if (Test-Path -LiteralPath (Join-Path $transaction 'installer-request.json')) {
                Stop-InstallerGuard $transaction
                Wait-CustomHostVenvReleased $Instruction $LogPath $PID
            }
            Restore-ToolInstallation -Instruction $Instruction -Result $Result
        }
        catch { throw "$failure Restore incomplete; recovery files: '$transaction': $($_.Exception.Message)" }
        throw "$failure Previous installation restored."
    }
}

function Test-HostReconnected {
    param([object]$Instruction, [int]$TimeoutSeconds = 30)
    $statusArguments = @($Instruction.status_argv | ForEach-Object { [string]$_ })
    $status = Invoke-CapturedProcess $statusArguments -TimeoutSeconds $TimeoutSeconds
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
        $remaining = [Math]::Max(1, [Math]::Min(30, [Math]::Ceiling(($deadline - [DateTimeOffset]::UtcNow).TotalSeconds)))
        if (Test-HostReconnected $Instruction -TimeoutSeconds $remaining) { return $true }
        if ([DateTimeOffset]::UtcNow -ge $deadline) { return $false }
        Start-Sleep -Seconds 1
    } while ($true)
}

if ($InstallerOnly) {
    try {
        $instruction = Get-Content -LiteralPath $InstructionPath -Raw | ConvertFrom-Json
        $TransactionPath = Assert-TransactionPath $TransactionPath $instruction
        Enable-InstallerJob
        $self = Get-Process -Id $PID
        Write-JsonAtomic (Join-Path $TransactionPath 'installer.json') @{
            pid = $PID; created_at = ($self.StartTime.ToFileTimeUtc() / 10000000.0 - 11644473600.0)
        }
        $request = Get-Content -LiteralPath (Join-Path $TransactionPath 'installer-request.json') -Raw | ConvertFrom-Json
        $owner = Get-Process -Id ([int]$request.parent_pid) -ErrorAction Stop
        $created = $owner.StartTime.ToFileTimeUtc() / 10000000.0 - 11644473600.0
        if ([Math]::Abs($created - [double]$request.parent_created_at) -gt 0.00001) {
            throw 'Installer owner has exited or changed identity.'
        }
        $output = Invoke-CapturedProcess @($request.arguments | ForEach-Object { [string]$_ }) @{
            OMNIGENT_SKIP_WEB_UI = 'true'
        } -TimeoutSeconds 900
    } catch {
        $output = @{ ExitCode = 1; Stdout = ''; Stderr = $_.Exception.Message }
    }
    Write-JsonAtomic (Join-Path $TransactionPath 'installer-output.json') $output
    exit 0
}

if ($RecoverOnly) {
    if (-not (Test-Path -LiteralPath $InstructionPath)) { exit 0 }
    $instruction = Get-Content -LiteralPath $InstructionPath -Raw | ConvertFrom-Json
    if (-not (Test-Path -LiteralPath $instruction.result_path)) { exit 0 }
    $recoveryLock = [IO.File]::Open(
        "$($instruction.result_path).recovery.guard", 'OpenOrCreate', 'ReadWrite', 'None')
    try {
        $receipt = Get-Content -LiteralPath $instruction.result_path -Raw | ConvertFrom-Json -AsHashtable
        if ($receipt.phase -eq 'completed' -or -not $receipt.backup_path) { exit 0 }
        $activeOwner = $false
        $owner = Get-Process -Id ([int]$receipt.helper_pid) -ErrorAction SilentlyContinue
        if ($owner) {
            $created = $owner.StartTime.ToFileTimeUtc() / 10000000.0 - 11644473600.0
            if ([Math]::Abs($created - [double]$receipt.helper_created_at) -le 0.00001) {
                if ($receipt.phase -in @('starting_host', 'restored')) { exit 0 }
                $activeOwner = $true
                throw 'A custom Host update is still running.'
            }
        }
        Recover-InterruptedTransaction $instruction $receipt
        if ($ResumeAutostart -and $receipt.restored -and $instruction.disabled_marker) {
            $expected = Join-Path (Split-Path -Parent (Split-Path -Parent $InstructionPath)) 'autostart\disabled'
            if ([IO.Path]::GetFullPath($instruction.disabled_marker) -ne [IO.Path]::GetFullPath($expected)) {
                throw 'Invalid autostart marker path.'
            }
            Remove-Item -LiteralPath $instruction.disabled_marker -Force -ErrorAction SilentlyContinue
        }
    } catch {
        if ($receipt -and $receipt.backup_path -and -not $activeOwner) {
            $receipt.status = 'failed'
            $receipt.error = $_.Exception.Message
            Save-Transaction $instruction $receipt $receipt.phase
        }
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 1
    } finally {
        $recoveryLock.Dispose()
    }
    exit 0
}

$result = [ordered]@{
    schema_version = 1
    status = 'starting'
    old_commit = $OldCommit
    target_commit = $TargetCommit
    helper_pid = $PID
    helper_created_at = ((Get-Process -Id $PID).StartTime.ToFileTimeUtc() / 10000000.0 - 11644473600.0)
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

    Save-Transaction $instruction $result 'starting_host'
    $restartAttempted = $true
    Restart-HostSupervisor $WrapperPath
    if (-not (Wait-HostReconnected $instruction)) {
        throw "The updated Host did not reconnect on its managed target within 60 seconds."
    }

    $result.status = 'complete'
    try { Complete-Transaction $instruction $result }
    catch {
        $result.retention_error = $_.Exception.Message
        Add-UpdateLog $LogPath "Retention diagnostic: $($result.retention_error)"
    }
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
                $identities = @(Get-ManagedHostSnapshot $instruction)
                $result.recovery_processes = $identities
                Save-Transaction $instruction $result 'rolling_back'
                try { Restart-HostSupervisor $WrapperPath 'stop' }
                catch { Add-UpdateLog $LogPath "Supervisor stop diagnostic: $($_.Exception.Message)" }
                try {
                    $stop = Invoke-CapturedProcess @([string]$instruction.cli_bin_path, 'host', 'stop', '--all', '--daemon-only', '--force')
                    if ($stop.ExitCode -ne 0) { Add-UpdateLog $LogPath "Host stop diagnostic: exit $($stop.ExitCode)." }
                } catch { Add-UpdateLog $LogPath "Host stop diagnostic: $($_.Exception.Message)" }
                Wait-CustomHostVenvReleased -Instruction $instruction -LogPath $LogPath -HelperPid $PID -Identities $identities
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
