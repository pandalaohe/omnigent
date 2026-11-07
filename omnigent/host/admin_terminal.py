"""User-invoked enrollment and removal; never run from daemon discovery."""

from __future__ import annotations

import os
import subprocess
import tempfile
import uuid
from pathlib import Path

import yaml

from omnigent.host.identity import host_config_path
from omnigent.terminals.conpty import registered_user_environment, windows_terminal_environment
from omnigent.terminals.windows_ssh import SSH_ENV_UNSET, WindowsSshLauncher, windows_ssh_launcher


def _read_host_config(path: Path) -> tuple[dict[str, object], dict[str, object]]:
    try:
        config = yaml.safe_load(path.read_text()) if path.exists() else {}
    except yaml.YAMLError as exc:
        raise ValueError("host config must contain valid YAML") from exc
    if config is None:
        config = {}
    if not isinstance(config, dict) or not isinstance(config.get("host", {}), dict):
        raise ValueError("host config must be a mapping")
    return config, config.setdefault("host", {})


def _configure(enabled: bool) -> None:
    path = host_config_path()
    config, host = _read_host_config(path)
    if enabled:
        host["elevated_terminals"] = "windows_ssh"
    else:
        host.pop("elevated_terminals", None)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".admin-terminal-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as stream:
            yaml.safe_dump(config, stream, sort_keys=False)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _service(action: str, launcher: WindowsSshLauncher, *, enrollment_id: str = "") -> None:
    script = Path(__file__).with_name("windows_admin_terminal.ps1")
    result = subprocess.run(
        [
            launcher.powershell,
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-Action",
            action,
            "-PublicKeyPath",
            str(launcher.key.with_suffix(".pub")),
            "-Username",
            launcher.username,
            "-EnrollmentId",
            enrollment_id,
        ],
        check=False,
    )
    if result.returncode:
        raise RuntimeError(
            f"Windows admin-terminal {action} failed; see the host output. "
            "If Group Policy blocks scripts, contact your administrator; "
            "this command does not change persistent execution policy."
        )


def _check_launcher(launcher: WindowsSshLauncher) -> None:
    env = windows_terminal_environment(
        registered_user_environment(), os.environ, {}, SSH_ENV_UNSET, {}
    )
    result = subprocess.run(
        launcher.argv(probe=True), env=env, stdout=subprocess.PIPE, check=False
    )
    if result.returncode or b"OMNIGENT_ADMIN_TOKEN_OK" not in result.stdout.split():
        raise RuntimeError(
            "selected PowerShell did not prove High integrity and enabled Administrators"
        )
    with tempfile.TemporaryDirectory(prefix="omnigent-admin-check-") as directory:
        wrong_key = Path(directory) / "wrong_key"
        subprocess.run(
            [
                str(launcher.ssh.with_name("ssh-keygen.exe")),
                "-t",
                "ed25519",
                "-N",
                "",
                "-q",
                "-f",
                str(wrong_key),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        argv = launcher.argv(probe=True)
        argv[argv.index("-i") + 1] = str(wrong_key)
        # No prompt and no credential fallback in the deliberately wrong-key trial.
        argv[1:1] = ["-o", "BatchMode=yes"]
        refused = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if refused.returncode != 255:
            raise RuntimeError("wrong-key SSH authentication was not refused")


def install_windows_admin_terminal() -> None:
    _read_host_config(host_config_path())
    launcher = windows_ssh_launcher()
    _service("Preflight", launcher)
    if launcher.key.exists() or launcher.key.with_suffix(".pub").exists():
        raise ValueError(
            "admin key already exists; preserve it and resolve enrollment before retrying"
        )
    launcher.key.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            str(launcher.ssh.with_name("ssh-keygen.exe")),
            "-t",
            "ed25519",
            "-a",
            "64",
            "-f",
            str(launcher.key),
        ],
        check=True,
    )
    from omnigent.terminals.windows_ssh import validate_encrypted_key

    validate_encrypted_key(launcher.key.read_bytes(), launcher.key.with_suffix(".pub").read_text())
    enrollment_id = uuid.uuid4().hex
    try:
        _service("Install", launcher, enrollment_id=enrollment_id)
        _check_launcher(launcher)
        _configure(True)
    except BaseException:
        incomplete = []
        try:
            _configure(False)
        except (OSError, ValueError):
            incomplete.append("host configuration")
        try:
            _service("Remove", launcher, enrollment_id=enrollment_id)
            _remove_key_files(launcher)
        except (OSError, RuntimeError):
            incomplete.append("SSH service or key files")
        if incomplete:
            raise RuntimeError(
                "Installation failed; automatic cleanup incomplete for "
                + ", ".join(incomplete)
                + ". Resolve this state before retrying."
            ) from None
        raise


def _remove_key_files(launcher: WindowsSshLauncher) -> None:
    for path in (
        launcher.key,
        launcher.key.with_suffix(".pub"),
        launcher.key.parent / "known_hosts",
    ):
        path.unlink(missing_ok=True)
    if launcher.key.parent.exists() and not any(launcher.key.parent.iterdir()):
        launcher.key.parent.rmdir()


def remove_windows_admin_terminal() -> None:
    launcher = windows_ssh_launcher()
    _service("CheckOwned", launcher)
    config_error = None
    try:
        _configure(False)
    except (OSError, ValueError) as exc:
        config_error = exc
    _service("Remove", launcher)
    _remove_key_files(launcher)
    if config_error is not None:
        raise RuntimeError(
            "SSH enrollment removed; host configuration still needs cleanup."
        ) from config_error
