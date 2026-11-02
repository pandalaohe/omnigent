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
Check 'base interpreter with loaded tool module blocks update' {
    $row = [pscustomobject]@{
        ProcessId = 123; ParentProcessId = 1; Name = 'python.exe'
        ExecutablePath = 'C:\Python\python.exe'
        CommandLine = 'python -m omnigent.runner._entry'
        LoadedPaths = @('C:\Tools\omnigent\Lib\site-packages\worker.pyd')
    }
    $locking = @(Get-VenvLockingProcesses -Processes @($row) -ToolVenvPath 'C:\Tools\omnigent' -CliBinPath 'C:\Bin\omni.exe' -ExcludedPids @())
    if ($locking.Count -ne 1) { throw 'Loaded module was invisible to lock detection.' }
}
Check 'daemon entry is stopped even when its exe is outside the venv' {
    $row = [pscustomobject]@{
        ProcessId = 123; ParentProcessId = 1; Name = 'python.exe'
        ExecutablePath = 'C:\Python\python.exe'
        CommandLine = 'python -m omnigent.host._daemon_entry --non-interactive'
    }
    $tree = @(Get-StoppedHostTreePids -Processes @($row) -StoppedHostPids @(123) -ToolVenvPath 'C:\Tools\omnigent' -CliBinPath 'C:\Bin\omni.exe' -ExcludedPids @())
    if ($tree -notcontains 123) { throw 'Captured daemon PID was missed.' }
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
foreach ($mode in @('installer-fails', 'probe-fails', 'version-fails', 'success')) {
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
                Stdout = 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'; Stderr = ''
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
            version_argv = @($pwsh, '--version')
            status_argv = @($pwsh, '-NoProfile', '-Command', '''{"daemons":[{"host_id":"fixture","pid":222,"process":"online","host_status":"online"}]}''')
            previous_records = @(); host_id = 'fixture'
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
        $completed = Invoke-CapturedProcess $arguments
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
        )
        Write-Output $completed.Stdout
        if ($completed.ExitCode -ne 0) { throw $completed.Stderr }
    }
}
Write-Output "Fixtures retained: $FixtureRoot"
if ($failures.Count) { exit 1 }
