param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Preflight', 'Install', 'CheckOwned', 'Remove')]
    [string]$Action,
    [Parameter(Mandatory = $true)][string]$PublicKeyPath,
    [Parameter(Mandatory = $true)][string]$Username,
    [string]$EnrollmentId = '',
    [switch]$Elevated
)

$ErrorActionPreference = 'Stop'
$sshDirectory = Join-Path $env:ProgramData 'ssh'
$marker = Join-Path $env:ProgramData 'Omnigent\admin-terminal-installed'
$capability = 'OpenSSH.Server~~~~0.0.1.0'
$systemSsh = Join-Path $env:SystemRoot 'System32\OpenSSH'

function Assert-Fresh {
    if ((Get-Service sshd -ErrorAction SilentlyContinue) -or
        (Test-Path -LiteralPath $marker) -or
        @(Get-ChildItem -LiteralPath $sshDirectory -Force -ErrorAction SilentlyContinue).Count) {
        throw 'Existing sshd service, enrollment marker or ProgramData\ssh files; refusing to take ownership.'
    }
    $shell = (Get-ItemProperty 'HKLM:\SOFTWARE\OpenSSH' -ErrorAction SilentlyContinue).DefaultShell
    if ($shell -and $shell -ine (Join-Path $env:SystemRoot 'System32\cmd.exe')) {
        throw 'Existing OpenSSH DefaultShell is not cmd.exe; refusing to change it.'
    }
}

function Assert-Owned {
    if (-not (Test-Path -LiteralPath $marker)) {
        throw 'Omnigent enrollment marker missing or invalid; refusing removal.'
    }
    $record = Get-Content -LiteralPath $marker -Raw | ConvertFrom-Json
    if ($record.kind -cne 'omnigent-admin-terminal-v1' -or
        ($EnrollmentId -and $record.id -cne $EnrollmentId)) {
        throw 'Enrollment marker does not belong to this installation; refusing removal.'
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

try {
    if ($Action -eq 'Preflight') { Assert-Fresh; exit 0 }
    if ($Action -eq 'CheckOwned') { Assert-Owned; exit 0 }
    if (-not $Elevated) {
        # Start-Process on Windows joins arguments; each path must retain its quotes.
        foreach ($value in @($PSCommandPath, $PublicKeyPath, $Username, $EnrollmentId)) {
            if ($value.Contains('"') -or $value.Contains("`n") -or $value.Contains("`r")) {
                throw 'Invalid enrollment argument.'
            }
        }
        $arguments = @('-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', ('"' + $PSCommandPath + '"'),
            '-Action', $Action, '-PublicKeyPath', ('"' + $PublicKeyPath + '"'),
            '-Username', ('"' + $Username + '"'), '-EnrollmentId', ('"' + $EnrollmentId + '"'), '-Elevated')
        $child = Start-Process -FilePath (Get-Process -Id $PID).Path -ArgumentList $arguments `
            -Verb RunAs -Wait -PassThru
        exit $child.ExitCode
    }
    $principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run the enrollment step with administrator permission.'
    }
    if ($Action -eq 'Install') {
        Assert-Fresh
        if ($EnrollmentId -notmatch '^[a-f0-9]{32}$') { throw 'Invalid enrollment id.' }
        if ($Username -notmatch '^[\w .-]+$' -or $Username.StartsWith('-')) { throw 'Invalid local username.' }
        $public = (Get-Content -LiteralPath $PublicKeyPath -Raw).Trim()
        if ($public -notmatch '^ssh-ed25519 [A-Za-z0-9+/=]+(?: [^\r\n]*)?$') { throw 'Invalid enrollment public key.' }
        New-Item -ItemType Directory -Path (Split-Path $marker) -Force | Out-Null
        $handle = [IO.File]::Open($marker, [IO.FileMode]::CreateNew)
        $handle.Dispose()
        Protect-File $marker
        & icacls.exe $marker /grant '*S-1-5-32-545:R' | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Could not make enrollment marker readable.' }
        @{ kind = 'omnigent-admin-terminal-v1'; id = $EnrollmentId } | ConvertTo-Json |
            Set-Content -LiteralPath $marker -Encoding ascii
        Add-WindowsCapability -Online -Name $capability | Out-Null
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
        Protect-File $authorization
        Protect-File $configPath
        & (Join-Path $systemSsh 'ssh-keygen.exe') -A
        if ($LASTEXITCODE -ne 0) { throw 'Could not generate SSH host keys.' }
        & (Join-Path $systemSsh 'sshd.exe') -t -f $configPath
        if ($LASTEXITCODE -ne 0) { throw 'sshd rejected the enrollment configuration.' }
        Remove-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
        Set-Service sshd -StartupType Automatic
        Start-Service sshd
        $listeners = @(Get-NetTCPConnection -LocalPort 22 -State Listen -ErrorAction Stop)
        $servicePid = (Get-CimInstance Win32_Service -Filter "Name='sshd'").ProcessId
        if ($listeners.Count -ne 1 -or $listeners[0].LocalAddress -ne '127.0.0.1' -or
            $listeners[0].OwningProcess -ne $servicePid) {
            throw 'sshd did not exclusively bind 127.0.0.1:22.'
        }
        exit 0
    }
    Assert-Owned
    Stop-Service sshd -ErrorAction SilentlyContinue
    if (Get-Service sshd -ErrorAction SilentlyContinue) { Set-Service sshd -StartupType Disabled }
    # This enrollment refused all pre-existing sshd state, so these are its sessions.
    Get-CimInstance Win32_Process -Filter "Name='sshd.exe'" | Where-Object {
        $_.ExecutablePath -ieq (Join-Path $systemSsh 'sshd.exe')
    } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Remove-WindowsCapability -Online -Name $capability | Out-Null
    Remove-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue
    foreach ($name in @('sshd_config', 'administrators_authorized_keys',
        'ssh_host_ed25519_key', 'ssh_host_ed25519_key.pub',
        'ssh_host_ecdsa_key', 'ssh_host_ecdsa_key.pub', 'ssh_host_rsa_key', 'ssh_host_rsa_key.pub')) {
        $path = Join-Path $sshDirectory $name
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Force }
    }
    Remove-Item -LiteralPath $marker -Force
} catch {
    Write-Error $_
    exit 1
}
