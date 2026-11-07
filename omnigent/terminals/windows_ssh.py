"""Pinned loopback SSH launcher for the explicitly enrolled Windows host."""

from __future__ import annotations

import base64
import binascii
import os
import re
import shutil
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path

SSH_ENV_UNSET = ["SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "SSH_AUTH_SOCK", "SSH_AGENT_PID"]
_MAGIC = b"openssh-key-v1\0"
_OPENSSH_LABEL = b"OPENSSH PRIVATE KEY"
_KEY_ERROR = "admin key must be encrypted Ed25519 with bcrypt rounds >= 64; recreate it"
TOKEN_PROBE = (
    "$groups = whoami /groups /fo csv /nh; "
    "$principal = [Security.Principal.WindowsPrincipal]::new("
    "[Security.Principal.WindowsIdentity]::GetCurrent()); "
    "if ($LASTEXITCODE -ne 0 -or "
    "-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) "
    "-or -not ($groups -match 'S-1-16-12288')) { exit 1 }; "
    "Write-Output 'OMNIGENT_ADMIN_TOKEN_OK'"
)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.offset = 0

    def number(self) -> int:
        if len(self.data) - self.offset < 4:
            raise ValueError(_KEY_ERROR)
        value = struct.unpack_from(">I", self.data, self.offset)[0]
        self.offset += 4
        return value

    def string(self) -> bytes:
        length = self.number()
        end = self.offset + length
        if end > len(self.data):
            raise ValueError(_KEY_ERROR)
        value = self.data[self.offset : end]
        self.offset = end
        return value

    def done(self) -> None:
        if self.offset != len(self.data):
            raise ValueError(_KEY_ERROR)


def ed25519_public_blob(text: str) -> bytes:
    """Validate the public-key shape without interpreting comments or options."""
    try:
        fields = text.strip().split()
        if len(fields) < 2 or fields[0] != "ssh-ed25519":
            raise ValueError(_KEY_ERROR)
        blob = base64.b64decode(fields[1], validate=True)
        reader = _Reader(blob)
        if reader.string() != b"ssh-ed25519" or len(reader.string()) != 32:
            raise ValueError(_KEY_ERROR)
        reader.done()
        return blob
    except (binascii.Error, UnicodeError) as exc:
        raise ValueError(_KEY_ERROR) from exc


def openssh_key_armor() -> tuple[bytes, bytes]:
    """Return the OpenSSH envelope boundaries without carrying an armored key."""
    return b"-----BEGIN " + _OPENSSH_LABEL + b"-----", b"-----END " + _OPENSSH_LABEL + b"-----"


def validate_encrypted_key(key: bytes, public_key: str) -> None:
    """Check PROTOCOL.key encryption metadata; never decrypt or return key bytes."""
    try:
        lines = key.strip().splitlines()
        begin, end = openssh_key_armor()
        if len(key) > 16384 or len(lines) < 3 or lines[0] != begin or lines[-1] != end:
            raise ValueError(_KEY_ERROR)
        decoded = base64.b64decode(b"".join(lines[1:-1]), validate=True)
        if not decoded.startswith(_MAGIC):
            raise ValueError(_KEY_ERROR)
        reader = _Reader(decoded[len(_MAGIC) :])
        cipher, kdf, options = reader.string(), reader.string(), reader.string()
        # ssh-keygen's default and its AES alternatives have 16-byte blocks.
        if (
            cipher
            not in {
                b"aes128-ctr",
                b"aes192-ctr",
                b"aes256-ctr",
                b"aes128-cbc",
                b"aes192-cbc",
                b"aes256-cbc",
            }
            or kdf != b"bcrypt"
        ):
            raise ValueError(_KEY_ERROR)
        bcrypt = _Reader(options)
        salt, rounds = bcrypt.string(), bcrypt.number()
        bcrypt.done()
        if len(salt) != 16 or rounds < 64 or reader.number() != 1:
            raise ValueError(_KEY_ERROR)
        if reader.string() != ed25519_public_blob(public_key):
            raise ValueError(_KEY_ERROR)
        encrypted = reader.string()
        reader.done()
        if len(encrypted) < 144 or len(encrypted) % 16:
            raise ValueError(_KEY_ERROR)
    except (binascii.Error, UnicodeError, struct.error) as exc:
        raise ValueError(_KEY_ERROR) from exc


@dataclass(frozen=True)
class WindowsSshLauncher:
    ssh: Path
    key: Path
    host_key: Path
    powershell: str
    username: str

    def validate(self) -> None:
        if not self.ssh.is_file():
            raise ValueError("Windows OpenSSH client is missing")
        if not Path(self.powershell).is_file():
            raise ValueError("admin PowerShell executable is missing")
        if re.search(r'["%!^&|<>\r\n]', self.powershell):
            raise ValueError("admin PowerShell path cannot be safely quoted for cmd.exe")
        if not re.fullmatch(r"[\w .-]+", self.username) or self.username.startswith("-"):
            raise ValueError("invalid local Windows admin username")
        try:
            if self.key.stat().st_size > 16384:
                raise ValueError(_KEY_ERROR)
            validate_encrypted_key(self.key.read_bytes(), self.key.with_suffix(".pub").read_text())
            ed25519_public_blob(self.host_key.read_text())
        except OSError as exc:
            raise ValueError("admin SSH enrollment files are missing or unreadable") from exc

    def argv(self, *, probe: bool = False) -> list[str]:
        self.validate()
        blob = ed25519_public_blob(self.host_key.read_text())
        known_hosts = self.key.parent / "known_hosts"
        fd, name = tempfile.mkstemp(dir=self.key.parent, prefix=".known-hosts-")
        try:
            with os.fdopen(fd, "w", encoding="ascii") as stream:
                stream.write(
                    "127.0.0.1 ssh-ed25519 " + base64.b64encode(blob).decode("ascii") + "\n"
                )
            os.replace(name, known_hosts)
        finally:
            Path(name).unlink(missing_ok=True)
        option_path = known_hosts.as_posix().replace("%", "%%")
        remote = f'"{self.powershell}" -NoLogo -NoProfile'
        if probe:
            encoded = base64.b64encode(TOKEN_PROBE.encode("utf-16-le")).decode("ascii")
            remote += " -EncodedCommand " + encoded
        return [
            str(self.ssh),
            "-F",
            "NUL",
            "-i",
            str(self.key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "PreferredAuthentications=publickey",
            "-o",
            f'UserKnownHostsFile="{option_path}"',
            "-o",
            "GlobalKnownHostsFile=NUL",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "NumberOfPasswordPrompts=3",
            "-o",
            "ConnectTimeout=10",
            "-tt",
            "-l",
            self.username.lower(),
            "127.0.0.1",
            remote,
        ]


def windows_ssh_launcher() -> WindowsSshLauncher:
    """Resolve the host's launcher afresh so replacement enrollment fails closed."""
    system = Path(os.environ.get("SYSTEMROOT", r"C:\Windows")) / "System32" / "OpenSSH"
    key = (
        Path(os.environ.get("USERPROFILE", str(Path.home())))
        / ".omnigent"
        / "admin-terminal"
        / "id_ed25519"
    )
    host_key = (
        Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "ssh" / "ssh_host_ed25519_key.pub"
    )
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if powershell is None:
        raise ValueError("admin terminals require PowerShell")
    return WindowsSshLauncher(
        system / "ssh.exe", key, host_key, powershell, os.environ.get("USERNAME", "")
    )
