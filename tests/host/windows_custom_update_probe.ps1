param(
    [Parameter(Mandatory = $true)][string]$HelperPath,
    [Parameter(Mandatory = $true)][string]$FixtureRoot,
    [string]$PythonPath = '',
    [string]$UvPath = ''
)
$ErrorActionPreference = 'Stop'
# Load functions without executing the updater's top-level entry point.
$parseErrors = $null
$tokens = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    $HelperPath, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw ($parseErrors | Out-String) }
foreach ($definition in $ast.FindAll({
    param($node)
    $node -is [System.Management.Automation.Language.FunctionDefinitionAst]
}, $false)) {
    . ([scriptblock]::Create($definition.Extent.Text))
}
New-Item -ItemType Directory -Path $FixtureRoot -Force | Out-Null
$failures = [System.Collections.Generic.List[string]]::new()
function Check {
    param([string]$Name, [scriptblock]$Body)
    try {
        & $Body
        Write-Output "PASS $Name"
    } catch {
        $failures.Add($Name)
        Write-Output "FAIL ${Name}: $($_.Exception.Message)"
    }
}
Check 'base interpreter with a mapped native tool module blocks update' {
    $row = [pscustomobject]@{
        ProcessId = 123; ParentProcessId = 1; Name = 'python.exe'; CreatedAt = 1.0
        ExecutablePath = 'C:\Python\python.exe'
        CommandLine = 'python -m omnigent.runner._entry'
        LoadedPaths = @('C:\Tools\omnigent\Lib\site-packages\worker.pyd')
    }
    $locking = @(Get-VenvLockingProcesses -Processes @($row) -ToolVenvPath 'C:\Tools\omnigent' -CliBinPath 'C:\Bin\omni.exe' -ExcludedPids @())
    if ($locking.Count -ne 1) { throw 'Loaded module was invisible to lock detection.' }
}
Check 'daemon entry is stopped even when its exe is outside the venv' {
    $row = [pscustomobject]@{
        ProcessId = 123; ParentProcessId = 1; Name = 'python.exe'; CreatedAt = 1.0
        ExecutablePath = 'C:\Python\python.exe'
        CommandLine = 'python -m omnigent.host._daemon_entry --non-interactive'
    }
    $tree = @(Get-StoppedHostTreePids -Processes @($row) -StoppedHostPids @(123) -ToolVenvPath 'C:\Tools\omnigent' -CliBinPath 'C:\Bin\omni.exe' -ExcludedPids @())
    if ($tree -notcontains 123) { throw 'Captured daemon PID was missed.' }
}
Check 'a holder controller and its unrelated worker are not kill roots' {
    $rows = @(
        [pscustomobject]@{ ProcessId = 10; ParentProcessId = 1; Name = 'python.exe'; CreatedAt = 1.0; ExecutablePath = 'C:\Python\python.exe'; CommandLine = 'controller.py'; LoadedPaths = @('C:\Tools\omnigent\Lib\worker.pyd') },
        [pscustomobject]@{ ProcessId = 11; ParentProcessId = 10; Name = 'python.exe'; CreatedAt = 2.0; ExecutablePath = 'C:\Python\python.exe'; CommandLine = 'python -m omnigent.host._daemon_entry' },
        [pscustomobject]@{ ProcessId = 12; ParentProcessId = 10; Name = 'python.exe'; CreatedAt = 3.0; ExecutablePath = 'C:\Python\python.exe'; CommandLine = 'unrelated.py' }
    )
    $tree = @(Get-StoppedHostTreePids $rows @(11) 'C:\Tools\omnigent' 'C:\Bin\omni.exe' @())
    if ($tree.Count -ne 1 -or $tree[0] -ne 11) { throw "Controller incorrectly authorized: $tree" }
}
Check 'script mentions do not authorize Host or installer termination' {
    $cli = 'C:\Profiles\fixture\.local\bin\omni.exe'
    $script = 'C:\Profiles\fixture\.omnigent\autostart\Start-OmnigentHostDefault.ps1'
    foreach ($command in @(
        "pwsh.exe -Command Get-Content '$script'",
        "pwsh.exe -File `"$script.bak`"",
        "pwsh.exe -File `"$script`".bak",
        "pwsh.exe -Command Write-Output -File '$script'"
    )) {
        $row = [pscustomobject]@{
            Name = 'pwsh.exe'; ExecutablePath = 'C:\PowerShell\pwsh.exe'
            CommandLine = $command
        }
        if (Test-HostLauncher $row $cli) { throw "Incidental text authorized termination: $command" }
    }
}
Check 'installer fallback requires the exact invoked helper and argument positions' {
    $scenario = Join-Path $FixtureRoot 'installer-selector'
    New-Item -ItemType Directory -Force -Path $scenario | Out-Null
    $InstructionPath = Join-Path $scenario 'instruction.json'
    $transaction = Join-Path $scenario 'transaction'
    $instruction = [pscustomobject]@{ helper_path = $HelperPath }
    $rows = @(
        [pscustomobject]@{
            ProcessId = 100; CreatedAt = 1.0; Name = 'pwsh.exe'
            ExecutablePath = 'C:\PowerShell\pwsh.exe'
            CommandLine = "pwsh.exe -Command Get-Content '$HelperPath' '-InstallerOnly' '$transaction'"
        },
        [pscustomobject]@{
            ProcessId = 101; CreatedAt = 2.0; Name = 'pwsh.exe'
            ExecutablePath = 'C:\PowerShell\pwsh.exe'
            CommandLine = "pwsh.exe -NoProfile -File `"$HelperPath`" -InstructionPath `"$InstructionPath`" -InstallerOnly -TransactionPath `"$transaction`""
        }
    )
    $selected = [Collections.Generic.List[int]]::new()
    function Get-ProcessTable { return $rows }
    function Stop-ProcessIdentity { param([object]$Identity) $selected.Add([int]$Identity.pid) }
    Stop-InstallerGuard $transaction $instruction
    if ($selected.Count -ne 1 -or $selected[0] -ne 101) { throw "Wrong installer kill authority: $selected" }
}
Check 'reconciled success resumes login without restoring the previous installation' {
    $scenario = Join-Path $FixtureRoot 'reconciled-login'
    $updates = Join-Path $scenario '.omnigent\updates'
    $autostart = Join-Path $scenario '.omnigent\autostart'
    $venv = Join-Path $scenario 'tools\omnigent'
    $backup = "$venv.update-$('d' * 32)\previous"
    New-Item -ItemType Directory -Path $updates, $autostart, $venv, $backup -Force | Out-Null
    $marker = Join-Path $autostart 'disabled'
    $current = Join-Path $venv 'working.txt'
    $previous = Join-Path $backup 'working.txt'
    [IO.File]::WriteAllText($marker, '')
    [IO.File]::WriteAllText($current, 'healthy')
    [IO.File]::WriteAllText($previous, 'previous')
    $instructionPath = Join-Path $updates 'instruction.json'
    $resultPath = Join-Path $updates 'result.json'
    Write-JsonAtomic $instructionPath @{
        result_path = $resultPath; disabled_marker = $marker
        tool_venv_path = $venv
    }
    Write-JsonAtomic $resultPath @{
        status = 'complete'; phase = 'rolling_back'; backup_path = $backup
    }
    $recoveryArguments = @(
        (Get-Command pwsh).Source, '-NoProfile', '-File', ([IO.Path]::GetFullPath($HelperPath)),
        '-InstructionPath', $instructionPath, '-RecoverOnly', '-ResumeAutostart'
    )
    foreach ($attempt in 1..2) {
        $recovery = Invoke-CapturedProcess $recoveryArguments
        if ($recovery.ExitCode -ne 0) { throw "Reconciled login failed: $($recovery.Stderr)" }
        if (Test-Path -LiteralPath $marker) { throw 'Reconciled success left login autostart disabled.' }
    }
    [IO.File]::WriteAllText($marker, '')
    $heldMarker = [IO.File]::Open($marker, 'Open', 'Read', 'None')
    try {
        $blocked = Invoke-CapturedProcess $recoveryArguments
        if ($blocked.ExitCode -eq 0) { throw 'A held disabled marker was reported as removed.' }
        $saved = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
        if ($saved.status -ne 'complete') { throw 'Marker cleanup failure invalidated a healthy install.' }
    } finally { $heldMarker.Dispose() }
    $retried = Invoke-CapturedProcess $recoveryArguments
    if ($retried.ExitCode -ne 0 -or (Test-Path -LiteralPath $marker)) {
        throw 'Marker cleanup did not recover after the lock was released.'
    }
    if ([IO.File]::ReadAllText($current) -ne 'healthy' -or
        [IO.File]::ReadAllText($previous) -ne 'previous') {
        throw 'Terminal recovery replaced the healthy install or changed its backup.'
    }
    $saved = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
    if ($saved.status -ne 'complete') { throw 'Successful reconciliation receipt was changed.' }
}
Check 'startup recovery protects its caller but not the replacement Host' {
    $cli = 'C:\Profiles\fixture\.local\bin\omni.exe'
    $startupPid = $PID + 1
    $replacementPid = $PID + 2
    $rows = @(
        [pscustomobject]@{
            ProcessId = $startupPid; ParentProcessId = 1; CreatedAt = 1.0
            Name = 'pwsh.exe'; ExecutablePath = 'C:\PowerShell\pwsh.exe'
            CommandLine = 'pwsh.exe -File "C:\Profiles\fixture\.omnigent\autostart\Start-OmnigentHostDefault.ps1"'
        },
        [pscustomobject]@{
            ProcessId = $PID; ParentProcessId = $startupPid; CreatedAt = 2.0
            Name = 'pwsh.exe'; ExecutablePath = 'C:\PowerShell\pwsh.exe'
            CommandLine = 'pwsh.exe -File "C:\Data\update.ps1" -RecoverOnly'
        },
        [pscustomobject]@{
            ProcessId = $replacementPid; ParentProcessId = 1; CreatedAt = 1.0
            Name = 'python.exe'; ExecutablePath = 'C:\Tools\omnigent\Scripts\python.exe'
            CommandLine = 'python.exe -m omnigent.host._daemon_entry'
        }
    )
    function Get-ProcessTable { return $rows }
    $protected = @(Get-UpdaterAncestorPids $rows $PID)
    if ($protected -notcontains $startupPid -or $protected -contains $replacementPid) { throw 'Wrong protected caller chain.' }
    $snapshot = @(Get-ManagedHostSnapshot ([pscustomobject]@{ tool_venv_path = 'C:\Tools\omnigent'; cli_bin_path = $cli }))
    if ($snapshot.Count -ne 1 -or $snapshot[0].pid -ne $replacementPid) { throw 'Recovery selected its own startup caller.' }
}
Check 'native timeout terminates and reaps its child tree' {
    $hang = Join-Path $FixtureRoot 'hang.ps1'
    $childFile = Join-Path $FixtureRoot 'hanging-child.pid'
    [IO.File]::WriteAllText($hang, @'
param([string]$PidPath)
$info = [Diagnostics.ProcessStartInfo]::new((Get-Command pwsh).Source)
$info.ArgumentList.Add('-NoProfile')
$info.ArgumentList.Add('-Command')
$info.ArgumentList.Add('Start-Sleep -Seconds 120')
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$child = [Diagnostics.Process]::Start($info)
[IO.File]::WriteAllText($PidPath, [string]$child.Id)
$child.WaitForExit()
'@)
    $timedOut = $false
    try { Invoke-CapturedProcess @((Get-Command pwsh).Source, '-NoProfile', '-File', $hang, $childFile) -TimeoutSeconds 2 }
    catch { $timedOut = $_.Exception.Message -like '*timed out*' }
    if (-not $timedOut) { throw 'Hanging native command did not time out.' }
    if (-not (Test-Path $childFile)) { throw 'Timeout fixture did not start its child.' }
    $child = Get-Process -Id ([int](Get-Content $childFile)) -ErrorAction SilentlyContinue
    if ($child -and -not $child.HasExited) { throw 'Timed-out command child survived.' }
}
Check 'an unrelated native holder blocks without being killed and PID reuse is refused' {
    $venv = Join-Path $FixtureRoot 'unrelated-holder'
    New-Item -ItemType Directory -Path $venv -Force | Out-Null
    $file = Join-Path $venv 'held.txt'
    [IO.File]::WriteAllText($file, 'held')
    $holder = Join-Path $FixtureRoot 'holder.ps1'
    $ready = Join-Path $FixtureRoot 'holder.ready'
    [IO.File]::WriteAllText($holder, @'
param([string]$File, [string]$Ready)
$handle = [IO.File]::Open($File, 'Open', 'Read', 'Read')
[IO.File]::WriteAllText($Ready, 'ready')
Start-Sleep -Seconds 120
'@)
    $info = [Diagnostics.ProcessStartInfo]::new((Get-Command pwsh).Source)
    foreach ($arg in @('-NoProfile', '-File', $holder, $file, $ready)) { $info.ArgumentList.Add($arg) }
    $info.UseShellExecute = $false
    $info.CreateNoWindow = $true
    $process = [Diagnostics.Process]::Start($info)
    try {
        $deadline = [DateTimeOffset]::UtcNow.AddSeconds(5)
        while (-not (Test-Path $ready) -and [DateTimeOffset]::UtcNow -lt $deadline) { Start-Sleep -Milliseconds 20 }
        if (-not (Test-Path $ready)) { throw 'Holder fixture did not become ready.' }
        $created = $process.StartTime.ToFileTimeUtc() / 10000000.0 - 11644473600.0
        Stop-ProcessIdentity ([pscustomobject]@{ pid = $process.Id; created_at = $created - 100 })
        if ($process.HasExited) { throw 'Wrong creation identity authorized a kill.' }
        $instruction = [pscustomobject]@{ tool_venv_path = $venv; cli_bin_path = (Join-Path $FixtureRoot 'omni.exe'); previous_records = @() }
        $blocked = $false
        try { Wait-CustomHostVenvReleased $instruction (Join-Path $FixtureRoot 'holder.log') $PID -TimeoutSeconds 0 }
        catch { $blocked = $true }
        if (-not $blocked -or $process.HasExited) { throw 'Unrelated holder did not safely block installation.' }
    } finally {
        if (-not $process.HasExited) { $process.Kill($true); [void]$process.WaitForExit(5000) }
        $process.Dispose()
    }
}
Check 'retention keeps one successful backup and all incomplete transactions' {
    $directory = Join-Path $FixtureRoot 'retention'
    $venv = Join-Path $directory 'tool'
    New-Item -ItemType Directory -Path $venv -Force | Out-Null
    $updates = Join-Path $directory 'updates'
    $InstructionPath = Join-Path $updates 'instruction.json'
    $constraints = Join-Path $updates 'locked-install-fixture'
    New-Item -ItemType Directory -Path $constraints -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $constraints 'constraints.txt'), 'fixture')
    $instruction = [pscustomobject]@{
        tool_venv_path = $venv; result_path = (Join-Path $directory 'result.json')
        constraints_dir = $constraints
    }
    $old = "$venv.update-$('a' * 32)"
    $incomplete = "$venv.update-$('b' * 32)"
    $latest = "$venv.update-$('c' * 32)"
    foreach ($path in @($old, $incomplete, $latest)) { New-Item -ItemType Directory -Path (Join-Path $path 'previous') -Force | Out-Null }
    Write-JsonAtomic (Join-Path $old 'transaction.json') @{ phase = 'completed'; status = 'complete'; updated_at = '2999-01-01T00:00:00Z' }
    Write-JsonAtomic (Join-Path $incomplete 'transaction.json') @{ phase = 'installing'; status = 'running'; updated_at = '1999-01-01T00:00:00Z' }
    $receipt = [ordered]@{ backup_path = (Join-Path $latest 'previous'); status = 'complete' }
    Complete-Transaction $instruction $receipt
    if ((Test-Path $old) -or -not (Test-Path $latest) -or -not (Test-Path $incomplete)) {
        throw 'Successful/incomplete retention boundary failed.'
    }
    if ((Test-Path $constraints) -or -not (Test-Path (Join-Path $latest 'constraints/constraints.txt'))) {
        throw 'Successful constraints were not retained with the single previous transaction.'
    }
}
Check 'an open file fails the final release gate' {
    $venv = Join-Path $FixtureRoot 'held'
    New-Item -ItemType Directory -Path $venv -Force | Out-Null
    $file = Join-Path $venv 'held.pyd'
    [IO.File]::WriteAllText($file, 'old')
    $handle = [IO.File]::Open($file, 'Open', 'Read', 'Read')
    try {
        $blocked = $false
        try { Assert-ToolFilesReleased $venv } catch { $blocked = $true }
        if (-not $blocked) { throw 'A held file passed the release gate.' }
    } finally { $handle.Dispose() }
    Assert-ToolFilesReleased $venv
}
Check 'a real loaded DLL outside exe and argv blocks update' {
    $venv = Join-Path $FixtureRoot 'mapped'
    New-Item -ItemType Directory -Path $venv -Force | Out-Null
    $library = Join-Path $venv 'fixture-version.dll'
    Copy-Item -LiteralPath "$env:SystemRoot\System32\version.dll" -Destination $library
    $module = [Runtime.InteropServices.NativeLibrary]::Load($library)
    try {
        $row = @(Get-ProcessTable | Where-Object { $_.ProcessId -eq $PID })
        $locking = @(Get-VenvLockingProcesses -Processes $row -ToolVenvPath $venv -CliBinPath (Join-Path $FixtureRoot 'omni.exe') -ExcludedPids @())
        if ($locking.Count -ne 1) { throw 'Real loaded DLL was missed.' }
        $blocked = $false
        try { Assert-ToolFilesReleased $venv } catch { $blocked = $true }
        if (-not $blocked) { throw 'Mapped image passed exclusive-access check.' }
    } finally { [Runtime.InteropServices.NativeLibrary]::Free($module) }
    Assert-ToolFilesReleased $venv
}
foreach ($mode in @('installer-fails', 'probe-fails', 'version-fails', 'version-mismatch', 'success')) {
    Check "transaction $mode" {
        $scenario = Join-Path $FixtureRoot $mode
        $venv = Join-Path $scenario 'omnigent'
        $bin = Join-Path $scenario 'bin'
        New-Item -ItemType Directory -Path $venv, $bin -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $venv 'working.txt'), 'old')
        $cli = Join-Path $bin 'omni.exe'
        [IO.File]::WriteAllText($cli, 'old-wrapper')
        $instruction = [pscustomobject]@{
            tool_venv_path = $venv; cli_bin_path = $cli
            install_argv = @('fixture-installer', $mode, $venv, $cli)
            probe_argv = @('fixture-probe')
            version_argv = @('fixture-version')
        }
        function Invoke-Installer {
            param([object]$Instruction, [string]$Transaction)
            return Invoke-CapturedProcess $Instruction.install_argv
        }
        # Simulate uv's destructive partial install at the process boundary.
        function Invoke-CapturedProcess {
            param([string[]]$Arguments, [hashtable]$Environment = @{})
            if ($Arguments[0] -eq 'fixture-installer') {
                New-Item -ItemType Directory -Path $venv -Force | Out-Null
                [IO.File]::WriteAllText((Join-Path $venv 'partial.txt'), 'new')
                [IO.File]::WriteAllText($cli, 'new-wrapper')
                return @{ ExitCode = $(if ($mode -eq 'installer-fails') { 2 } else { 0 }); Stdout = ''; Stderr = '' }
            }
            return @{
                ExitCode = $(if (($mode -eq 'probe-fails' -and $Arguments[0] -eq 'fixture-probe') -or ($mode -eq 'version-fails' -and $Arguments[0] -eq 'fixture-version')) { 3 } else { 0 })
                Stdout = $(if ($Arguments[0] -eq 'fixture-version') {
                    if ($mode -eq 'version-mismatch') { 'omnigent fixture (aaaaaaaa, built fixture)' }
                    else { 'omnigent fixture (bbbbbbbb, built fixture)' }
                } else { 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' }); Stderr = ''
            }
        }
        $receipt = [ordered]@{}
        $failed = $false
        try {
            Invoke-TransactionalInstall -Instruction $instruction -TargetCommit ('b' * 40) -LogPath (Join-Path $scenario 'update.log') -Result $receipt
        } catch { $failed = $true }
        if ($mode -eq 'success') {
            if ($failed -or -not (Test-Path (Join-Path $venv 'partial.txt'))) { throw 'New installation not committed.' }
        } else {
            if (-not $failed) { throw 'Injected failure was swallowed.' }
            if ([IO.File]::ReadAllText((Join-Path $venv 'working.txt')) -ne 'old') { throw 'Old installation not restored.' }
            if ([IO.File]::ReadAllText($cli) -ne 'old-wrapper') { throw 'Old launcher not restored.' }
        }
        if (-not $receipt.backup_path) { throw 'Missing recovery location in result.' }
    }
}
Check 'launcher restore retries after venv has already returned' {
    $scenario = Join-Path $FixtureRoot 'restore-retry'
    $venv = Join-Path $scenario 'tool'
    $transaction = Join-Path $scenario 'transaction'
    $backup = Join-Path $transaction 'previous'
    $bin = Join-Path $scenario 'bin'
    New-Item -ItemType Directory -Path $venv, $backup, $bin -Force | Out-Null
    [IO.File]::WriteAllText((Join-Path $backup 'working.txt'), 'old')
    $cli = Join-Path $bin 'omni.exe'
    [IO.File]::WriteAllText($cli, 'new-launcher')
    [IO.File]::WriteAllText((Join-Path $transaction 'omni.exe'), 'old-launcher')
    $instruction = [pscustomobject]@{ tool_venv_path = $venv; cli_bin_path = $cli }
    $receipt = [ordered]@{ backup_path = $backup; restored = $false }
    $handle = [IO.File]::Open($cli, 'Open', 'Read', 'Read')
    try {
        $blocked = $false
        try { Restore-ToolInstallation -Instruction $instruction -Result $receipt } catch { $blocked = $true }
        if (-not $blocked -or $receipt.restored) { throw 'Incomplete launcher restore reported success.' }
        if (-not (Test-Path (Join-Path $venv 'working.txt'))) { throw 'Old venv not restored.' }
    } finally { $handle.Dispose() }
    Restore-ToolInstallation -Instruction $instruction -Result $receipt
    if (-not $receipt.restored -or [IO.File]::ReadAllText($cli) -ne 'old-launcher') {
        throw 'Restoration did not resume after returning the venv.'
    }
}
foreach ($mode in @('installer-fails', 'success')) {
    Check "detached helper $mode" {
        $scenario = Join-Path $FixtureRoot "helper-$mode"
        $venv = Join-Path $scenario 'tool'
        $bin = Join-Path $scenario '.local\bin'
        New-Item -ItemType Directory -Path $venv, $bin -Force | Out-Null
        [IO.File]::WriteAllText((Join-Path $venv 'working.txt'), 'old')
        $wrapper = Join-Path $scenario 'supervisor.cmd'
        [IO.File]::WriteAllText($wrapper, "@echo off`r`n>>`"%~dp0actions.txt`" echo %1`r`nexit /b 0`r`n")
        $installer = Join-Path $scenario 'install.ps1'
        $installerText = @'
param([string]$Venv, [string]$Mode)
New-Item -ItemType Directory -Path $Venv -Force | Out-Null
[IO.File]::WriteAllText((Join-Path $Venv 'new.txt'), 'new')
if ($Mode -eq 'installer-fails') { exit 2 }
'@
        [IO.File]::WriteAllText($installer, $installerText)
        $instructionPath = Join-Path $scenario 'instruction.json'
        $resultPath = Join-Path $scenario 'result.json'
        $logPath = Join-Path $scenario 'update.log'
        $lockPath = Join-Path $scenario 'update.lock'
        $pwsh = (Get-Command pwsh).Source
        $instruction = @{
            schema_version = 1; parent_pid = 0; constraints_dir = ''
            tool_venv_path = $venv; cli_bin_path = (Join-Path $bin 'omni.exe')
            install_argv = @($pwsh, '-NoProfile', '-File', $installer, $venv, $mode)
            probe_argv = @($pwsh, '-NoProfile', '-Command', "'$('b' * 40)'")
            version_argv = @($pwsh, '-NoProfile', '-Command', "'omnigent fixture (bbbbbbbb, built fixture)'")
            status_argv = @($pwsh, '-NoProfile', '-Command', '''{"daemons":[{"host_id":"fixture","pid":222,"process":"online","host_status":"online"}]}''')
            previous_records = @(); host_id = 'fixture'; helper_path = ([IO.Path]::GetFullPath($HelperPath))
            old_commit = ('a' * 40); target_commit = ('b' * 40)
            wrapper_path = $wrapper; result_path = $resultPath
            log_path = $logPath; lock_path = $lockPath
            rollback_path = (Join-Path $scenario 'rollback.json')
        }
        $instruction | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $instructionPath
        [IO.File]::WriteAllText($lockPath, '')
        $arguments = @(
            $pwsh, '-NoProfile', '-File', ([IO.Path]::GetFullPath($HelperPath)),
            '-InstructionPath', $instructionPath, '-ResultPath', $resultPath,
            '-LogPath', $logPath, '-LockPath', $lockPath, '-WrapperPath', $wrapper,
            '-OldCommit', ('a' * 40), '-TargetCommit', ('b' * 40), '-HostId', 'fixture'
        )
        $completed = Invoke-CapturedProcess $arguments -TimeoutSeconds 90
        $receipt = Get-Content -LiteralPath $resultPath -Raw | ConvertFrom-Json
        $expected = if ($mode -eq 'success') { 'complete' } else { 'failed' }
        if ($receipt.status -ne $expected) { throw "Unexpected result: $($receipt | ConvertTo-Json -Compress); $($completed.Stderr)" }
        if ($mode -eq 'installer-fails' -and -not (Test-Path (Join-Path $venv 'working.txt'))) {
            throw 'Failed installer did not restore old environment.'
        }
        if (-not (Test-Path (Join-Path $scenario 'actions.txt'))) { throw 'Supervisor not restarted.' }
        if (Test-Path $lockPath) { throw 'Helper lock not released.' }
    }
}
if ($PythonPath -and $UvPath) {
    Check 'real uv wheel, receipt and rollback transactions' {
        $completed = Invoke-CapturedProcess @(
            $PythonPath, (Join-Path $PSScriptRoot 'windows_custom_update_uv_probe.py'),
            ([IO.Path]::GetFullPath($HelperPath)), (Join-Path $FixtureRoot 'real-uv'),
            $UvPath, (Get-Command pwsh).Source
        ) -TimeoutSeconds 1800
        Write-Output $completed.Stdout
        if ($completed.ExitCode -ne 0) { throw $completed.Stderr }
    }
}
Write-Output "Fixtures retained: $FixtureRoot"
if ($failures.Count) { exit 1 }
