# Windows admin terminals

The host owner enables this feature from a local Windows terminal with
`omni host admin-terminal install`. The command asks before it creates keys or
requests UAC. It refuses an existing sshd service or files in ProgramData's ssh
directory. Other operating systems keep their existing elevation method.

This is an administrator terminal for the host's Windows account. It runs over
loopback OpenSSH using an encrypted Ed25519 key. A user types that key's
passphrase inside the terminal. Processes running as the same Windows user can
capture an authorized unlock or take over an open terminal; the app's agent
access switch does not provide an OS process boundary.

## Sub-features

- Explicit host-owner enrollment with encrypted keys and pinned loopback SSH.
- Admin terminals in sessions and standalone host terminals.
- Per-instance agent access, idle expiry, removal and credential recovery.

## How to get to it (user POV)

Enroll locally with `omni host admin-terminal install`, then open Admin beside
Shell in a host-bound session or on the standalone host terminals page.

## Driving it with the repro environment

Preconditions: use an isolated instance for app checks. Real enrollment needs a
Windows host and separate permission for its service and credential changes.

### Local component checks

`tests/host/test_windows_admin_terminal.py` uses synthetic key envelopes and fake
Windows process boundaries. It exercises opt-in, encryption metadata, pinned SSH
arguments, the shared PowerShell token probe, case-insensitive environment
exclusions, install failure cleanup, removal, and ConPTY registry lifecycle.
Run it with the project's isolated pytest wrapper, alongside
`tests/host/test_elevated_terminals.py`, `tests/runner/test_elevated_terminals.py`,
`tests/runner/test_host_terminals.py` and `tests/terminals/test_conpty.py`.
These checks do not install or prove a Windows service.

### Install on a fresh Windows host

Prerequisites: OpenSSH client, PowerShell, a Windows account in Administrators,
and permission to install OpenSSH Server and create this host's credentials.
Run these actions yourself in a local terminal. Obtain approval for this host's
installation and credentials before starting.
The packaged installer uses a process-only script execution setting, including
its UAC child, and does not change the host's saved execution policy. Managed
Group Policy restrictions still apply; if they block the script, contact the
host administrator before retrying.

1. Run `omni host admin-terminal install` and accept its confirmation. Choose a
   nonempty passphrase when ssh-keygen asks; keep it in your password manager.
   An empty passphrase or invalid key stops before service authorization and
   preserves the key files for inspection. Resolve those files before retrying;
   the installer does not overwrite an existing key.
2. Approve UAC when asked. OpenSSH Server installs without a network-facing
   firewall rule. The command checks that the sole port-22 listener belongs to
   sshd and binds to `127.0.0.1`.
3. Type the passphrase for the install check. It uses the same selected
   PowerShell, `-NoLogo -NoProfile`, quoting and SSH transport as terminal opens.
   It checks High integrity and enabled Administrators inside that shell, then
   checks that a separate wrong key cannot authenticate. Only success writes
   `host.elevated_terminals: windows_ssh` to the effective host config.
4. Restart the host using its existing service controls. The app should then
   offer Admin beside Shell in both a session and the standalone host terminals
   page. A missing or unencrypted replacement key removes the advertised
   capability and refuses new launches.

On a failed shell/token check, the installer separately attempts to clear opt-in
and remove its marked service and keys. A config write failure cannot prevent
service removal. If cleanup fails, it names the state still needing attention;
resolve it before retrying, and reboot if Windows requests it. No alternative
elevation mechanism is enabled automatically.

### Exercise both app entry points

Use the host owner's app admin login. Open Admin from a host-bound session and
from the standalone host terminals page. Each open must prompt for the key
passphrase, and a correct passphrase must open PowerShell. Inside it, run
`whoami /groups`: expect High Mandatory Level (`S-1-16-12288`) and Administrators
enabled. Run `ping -t 127.0.0.1`, press Ctrl+C and expect the prompt to return.
Three incorrect passphrases should end SSH and remove that terminal.

For a session terminal, read/send by the agent must fail before its instance's
access grant, succeed after a grant and fail again after revocation. A standalone
terminal has no session agent. In either surface, output and resize without
input must not keep an admin terminal alive beyond the 15-minute input limit.

The port-squat trial stops sshd and substitutes another loopback SSH listener;
it changes a real service and needs its own approval. Expect host-key rejection
before any passphrase prompt. The install check with PowerShell under Program
Files exercises a path with spaces. A missing or broken selected PowerShell
must fail enrollment rather than pass via a direct `whoami` SSH command.

Return the failed step, app entry point, exit code or HTTP/WS close code, and a
redacted screenshot or error line. Do not send a key, passphrase, terminal input
recording or credential-bearing config.

## Gotchas

### Remove, maintain, or revoke

Removal requires permission for this host's service stop and credential
deletion. Run `omni host admin-terminal remove` locally and confirm. It requires
the enrollment marker, removes the opt-in, closes its SSH sessions, removes the
Windows capability and its fixed enrollment files, and preserves unrelated
files. Restart the host using its existing controls, and reboot Windows if the
capability removal requires it. Expect port 22 closed, no enrollment marker and
no Admin offer after restart.

Changing a passphrase with `ssh-keygen -p -a 64 -f
"$env:USERPROFILE\.omnigent\admin-terminal\id_ed25519"` is maintenance. It does
not revoke an old copy of that key and passphrase.

For loss or suspected exposure, obtain permission to remove and recreate this
host's credentials. Run removal, then a fresh install with a new independent
passphrase. This closes existing sessions and replaces the authorized keypair.
Before declaring recovery complete, verify that the old key copy cannot make a
new authentication. Forget and re-enroll trusted browsers after their unlock
feature is available; offline browsers can retain stale encrypted records.
Signing out and changing the passphrase alone do not revoke a copied key.

After real installation, removal or recovery, update the host's SSH access
record and installed-tool inventory through their owners.
