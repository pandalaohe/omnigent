param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Preflight', 'Install', 'CheckOwned', 'Remove')]
    [string]$Action,
    [Parameter(Mandatory = $true)][string]$PublicKeyPath,
    [Parameter(Mandatory = $true)][string]$Username,
    [string]$EnrollmentId = '',
    [Parameter(Mandatory = $true)][string]$ResultPath,
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
$sshDirectory = Join-Path $env:ProgramData 'ssh'
$marker = Join-Path $env:ProgramData 'Omnigent\admin-terminal-installed'
$capability = 'OpenSSH.Server~~~~0.0.1.0'
$systemSsh = Join-Path $env:SystemRoot 'System32\OpenSSH'
$hostKeyNames = @('ssh_host_ed25519_key', 'ssh_host_ecdsa_key', 'ssh_host_rsa_key')

function Assert-Fresh {
    $service = Get-Service sshd -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $marker) {
        $refusal = "Existing Omnigent enrollment marker at $marker; refusing to take ownership."
        try { Assert-Owned } catch { throw "$refusal $($_.Exception.Message)" }
        throw "$refusal Run `"omni host admin-terminal remove`" to remove this enrollment before retrying."
    }
    $sshEntries = @(if (Test-Path -LiteralPath $sshDirectory) {
        Get-ChildItem -LiteralPath $sshDirectory -Force
    })
    if ($service -and $sshEntries.Count -eq 0) {
        throw ('An existing sshd service is not Omnigent-owned: the enrollment marker is absent ' +
            'and ProgramData\ssh has no entries. Refusing to take ownership. ' +
            'If this is your Windows OpenSSH Server capability and you intend to remove it, ' +
            "run Remove-WindowsCapability -Online -Name $capability in elevated PowerShell " +
            'and restart Windows if asked.')
    }
    if ($sshEntries.Count) {
        $leftovers = "Existing ProgramData\ssh entries: $($sshEntries.Name -join ', ')."
        if ($service) { $leftovers = "Existing sshd service. $leftovers" }
        throw "$leftovers The Omnigent enrollment marker is absent; refusing to take ownership."
    }
    $shell = (Get-ItemProperty 'HKLM:\SOFTWARE\OpenSSH' -ErrorAction SilentlyContinue).DefaultShell
    if ($shell -and $shell -ine (Join-Path $env:SystemRoot 'System32\cmd.exe')) {
        throw "Existing OpenSSH DefaultShell '$shell' is not cmd.exe; refusing to change it."
    }
}

function Assert-Owned {
    if (-not (Test-Path -LiteralPath $marker)) {
        throw 'Omnigent enrollment marker is missing; refusing removal.'
    }
    try {
        $markerText = Get-Content -LiteralPath $marker -Raw
    } catch {
        throw "Omnigent enrollment marker at $marker is not readable; refusing removal. $($_.Exception.Message)"
    }
    try {
        $record = ConvertFrom-Json -InputObject $markerText
    } catch {
        throw 'Omnigent enrollment marker is not valid JSON; refusing removal.'
    }
    if ($record.kind -cne 'omnigent-admin-terminal-v1') {
        throw 'Enrollment marker kind is not omnigent-admin-terminal-v1; refusing removal.'
    }
    if ($record.id -isnot [string] -or $record.id -notmatch '^[a-f0-9]{32}$') {
        throw 'Enrollment marker id is missing or invalid; refusing removal.'
    }
    if ($EnrollmentId -and $record.id -cne $EnrollmentId) {
        throw 'Enrollment marker id does not match this installation; refusing removal.'
    }
}

function Protect-File([string]$Path) {
    $acl = [Security.AccessControl.FileSecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
    foreach ($sid in @('S-1-5-18', 'S-1-5-32-544')) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid), 'FullControl', 'Allow'))
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

function Write-Failure([string]$Message) {
    # Open only an existing result file and verify its marker before writing.
    $name = [IO.Path]::GetFileName($ResultPath)
    if ($name -cnotmatch '^\.omnigent-admin-terminal-[a-z0-9_]+\.result$') { return }
    $file = [IO.File]::Open($ResultPath, 'Open', 'ReadWrite', 'None')
    try {
        $markerBytes = [byte[]]::new($name.Length)
        if ($file.Length -ne $markerBytes.Length -or
            $file.Read($markerBytes, 0, $markerBytes.Length) -ne $markerBytes.Length -or
            [Text.Encoding]::ASCII.GetString($markerBytes) -cne $name) { return }
        $bytes = [Text.Encoding]::UTF8.GetBytes($Message)
        $file.Position = 0
        $file.Write($bytes, 0, $bytes.Length)
        $file.SetLength($bytes.Length)
    } finally {
        $file.Dispose()
    }
}

function Invoke-OpenSsh([string]$Name, [string[]]$Arguments) {
    $executable = Get-Command -Name (Join-Path $systemSsh $Name) -CommandType Application
    # Windows PowerShell treats redirected native stderr as PowerShell errors.
    $ErrorActionPreference = 'Continue'
    $output = & $executable.Path @Arguments 2>&1
    if ($LASTEXITCODE -ne 0) {
        $message = ($output -join "`n").Trim()
        throw "$Name exited with code $LASTEXITCODE. $message"
    }
}

$step = $Action
try {
    if ($Action -eq 'Preflight') { Assert-Fresh; exit 0 }
    if ($Action -eq 'CheckOwned') { Assert-Owned; exit 0 }
    if (-not $Elevated) {
        $step = 'elevation'
        # Start-Process on Windows joins arguments; each path must retain its quotes.
        foreach ($value in @($PSCommandPath, $PublicKeyPath, $Username, $EnrollmentId, $ResultPath)) {
            if ($value.Contains('"') -or $value.Contains("`n") -or $value.Contains("`r")) {
                throw 'Invalid enrollment argument.'
            }
        }
        $arguments = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', ('"' + $PSCommandPath + '"'),
            '-Action', $Action, '-PublicKeyPath', ('"' + $PublicKeyPath + '"'),
            '-Username', ('"' + $Username + '"'), '-EnrollmentId', ('"' + $EnrollmentId + '"'),
            '-ResultPath', ('"' + $ResultPath + '"'), '-Elevated')
        $child = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList $arguments `
            -Verb RunAs -Wait -PassThru
        exit $child.ExitCode
    }
    $step = 'elevation'
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run the enrollment step with administrator permission.'
    }
    if ($Action -eq 'Install') {
        $step = 'preflight'
        Assert-Fresh
        if ($EnrollmentId -notmatch '^[a-f0-9]{32}$') { throw 'Invalid enrollment id.' }
        if ($Username -notmatch '^[\w .-]+$' -or $Username.StartsWith('-')) { throw 'Invalid local username.' }
        $public = (Get-Content -LiteralPath $PublicKeyPath -Raw).Trim()
        if ($public -notmatch '^ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?$') { throw 'Invalid enrollment public key.' }
        $step = 'enrollment marker'
        New-Item -ItemType Directory -Path (Split-Path $marker) -Force | Out-Null
        $handle = [IO.File]::Open($marker, [IO.FileMode]::CreateNew)
        $handle.Dispose()
        Protect-File $marker
        & icacls.exe $marker /grant '*S-1-5-32-545:R' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not make enrollment marker readable.' }
        @{ kind = 'omnigent-admin-terminal-v1'; id = $EnrollmentId } | ConvertTo-Json |
            Set-Content -LiteralPath $marker -Encoding ascii
        $step = 'capability'
        Add-WindowsCapability -Online -Name $capability | Out-Null
        $step = 'acl'
        New-Item -ItemType Directory -Path $sshDirectory -Force | Out-Null
        $directoryAcl = [Security.AccessControl.DirectorySecurity]::new()
        $directoryAcl.SetAccessRuleProtection($true, $false)
        $directoryAcl.SetOwner([Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))
        foreach ($sid in @('S-1-5-18', 'S-1-5-32-544', 'S-1-5-32-545')) {
            $rights = if ($sid -eq 'S-1-5-32-545') { 'ReadAndExecute' } else { 'FullControl' }
            $directoryAcl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
                [Security.Principal.SecurityIdentifier]::new($sid), $rights,
                'ContainerInherit, ObjectInherit', 'None', 'Allow'))
        }
        Set-Acl -LiteralPath $sshDirectory -AclObject $directoryAcl
        $configPath = Join-Path $sshDirectory 'sshd_config'
        $authorization = Join-Path $sshDirectory 'administrators_authorized_keys'
        $step = 'config'
        @"
Port 22
ListenAddress 127.0.0.1
HostKey __PROGRAMDATA__/ssh/ssh_host_ed25519_key
PubkeyAuthentication yes
PasswordAuthentication no
AuthenticationMethods publickey
AllowUsers "$($Username.ToLowerInvariant())"
AllowTcpForwarding no
AllowAgentForwarding no
X11Forwarding no
PermitTunnel no
PermitUserEnvironment no
MaxAuthTries 3
LoginGraceTime 30
Match Group administrators
    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
"@ | Set-Content -LiteralPath $configPath -Encoding ascii
        ('from="127.0.0.1",no-port-forwarding,no-agent-forwarding,no-X11-forwarding ' + $public) |
            Set-Content -LiteralPath $authorization -Encoding ascii
        $step = 'acl'
        Protect-File $authorization
        Protect-File $configPath
        $step = 'host keys'
        Invoke-OpenSsh 'ssh-keygen.exe' @('-A')
        # Keygen runs as the installing administrator. The service loads keys as SYSTEM.
        foreach ($name in $hostKeyNames) {
            $path = Join-Path $sshDirectory $name
            if (Test-Path -LiteralPath $path -PathType Leaf) { Protect-File $path }
        }
        $step = 'sshd -t'
        Invoke-OpenSsh 'sshd.exe' @('-t', '-e', '-f', $configPath)
        $step = 'service start'
        Remove-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
        Set-Service sshd -StartupType Automatic
        Start-Service sshd
        $step = 'listener check'
        # Win32-OpenSSH reports Running before it binds its listening socket.
        $elapsed = [Diagnostics.Stopwatch]::StartNew()
        do {
            $listeners = @(Get-NetTCPConnection -ErrorAction Stop | Where-Object {
                $_.LocalPort -eq 22 -and $_.State -eq 'Listen'
            })
            if ($listeners.Count -or $elapsed.Elapsed.TotalSeconds -ge 5) { break }
            Start-Sleep -Milliseconds 200
        } while ($true)
        if (-not $listeners.Count) {
            throw "No listener on port 22 after 5 seconds (sshd status: $((Get-Service sshd).Status))."
        }
        $servicePid = (Get-CimInstance Win32_Service -Filter "Name='sshd'").ProcessId
        if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1' -or
            $listeners[0].OwningProcess -ne $servicePid) {
            throw 'sshd did not exclusively bind 127.0.0.1:22.'
        }
        exit 0
    }
    $step = 'ownership check'
    Assert-Owned
    $step = 'service removal'
    Stop-Service sshd -ErrorAction SilentlyContinue
    if (Get-Service sshd -ErrorAction SilentlyContinue) { Set-Service sshd -StartupType Disabled }
    # This enrollment refused all pre-existing sshd state, so these are its sessions.
    Get-CimInstance Win32_Process -Filter "Name='sshd.exe'" | Where-Object {
        $_.ExecutablePath -ieq (Join-Path $systemSsh 'sshd.exe')
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    $step = 'capability removal'
    Remove-WindowsCapability -Online -Name $capability | Out-Null
    Remove-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
    $step = 'file cleanup'
    $hostPublicKeyNames = $hostKeyNames | ForEach-Object { "$_.pub" }
    foreach ($name in @('sshd_config', 'administrators_authorized_keys') + $hostKeyNames + $hostPublicKeyNames) {
        $path = Join-Path $sshDirectory $name
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
    $logs = Join-Path $sshDirectory 'logs'
    if (Test-Path -LiteralPath $logs -PathType Container) { [IO.Directory]::Delete($logs, $true) }
    if ((Test-Path -LiteralPath $sshDirectory -PathType Container) -and
        -not @(Get-ChildItem -LiteralPath $sshDirectory -Force).Count) {
        [IO.Directory]::Delete($sshDirectory)
    }
    Remove-Item -LiteralPath $marker -Force
} catch {
    $failure = "${step}: $($_.Exception.Message)"
    try { Write-Failure $failure } catch { Write-Error $failure -ErrorAction Continue }
    exit 1
}
