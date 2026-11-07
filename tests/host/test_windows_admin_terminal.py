"""Synthetic key envelopes and process boundaries; no host enrollment or real keys."""

from __future__ import annotations

import base64
import shutil
import struct
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from omnigent.host import admin_terminal
from omnigent.host.connect import HostProcess, _build_runner_env
from omnigent.host.frames import CAP_TERMINAL_ELEVATED
from omnigent.host.identity import HostIdentity, load_elevated_terminals_method
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.terminals import elevation
from omnigent.terminals.conpty import windows_terminal_environment
from omnigent.terminals.windows_ssh import (
    SSH_ENV_UNSET,
    TOKEN_PROBE,
    openssh_key_armor,
    validate_encrypted_key,
    windows_ssh_launcher,
)


def _string(value: bytes) -> bytes:
    return struct.pack(">I", len(value)) + value


def _envelope(*, cipher=b"aes256-ctr", kdf=b"bcrypt", rounds=64, count=1, trailing=b""):
    public = _string(b"ssh-ed25519") + _string(b"x" * 32)
    options = _string(b"s" * 16) + struct.pack(">I", rounds)
    body = (
        b"openssh-key-v1\0"
        + _string(cipher)
        + _string(kdf)
        + _string(options)
        + struct.pack(">I", count)
        + _string(public)
        + _string(b"e" * 144)
        + trailing
    )
    begin, end = openssh_key_armor()
    key = begin + b"\n" + base64.b64encode(body) + b"\n" + end + b"\n"
    return key, "ssh-ed25519 " + base64.b64encode(public).decode("ascii")


@pytest.fixture
def enrolled(tmp_path, monkeypatch):
    monkeypatch.setattr(elevation, "IS_WINDOWS", True)
    monkeypatch.setattr(elevation, "IS_POSIX", False)
    for name, path in {
        "SYSTEMROOT": tmp_path / "Windows",
        "PROGRAMDATA": tmp_path / "ProgramData",
        "USERPROFILE": tmp_path / "User Name",
        "OMNIGENT_CONFIG_HOME": tmp_path / "config",
    }.items():
        monkeypatch.setenv(name, str(path))
    monkeypatch.setenv("USERNAME", "Admin")
    powershell = tmp_path / "Program Files" / "PowerShell" / "pwsh.exe"
    powershell.parent.mkdir(parents=True)
    powershell.touch()
    which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *args, **kwargs: (
            str(powershell) if name == "pwsh" else which(name, *args, **kwargs)
        ),
    )
    launcher = windows_ssh_launcher()
    for path in (launcher.ssh, launcher.key, launcher.host_key):
        path.parent.mkdir(parents=True, exist_ok=True)
    launcher.ssh.touch()
    key, public = _envelope()
    launcher.key.write_bytes(key)
    launcher.key.with_suffix(".pub").write_text(public)
    launcher.host_key.write_text(public)
    return launcher


@pytest.mark.parametrize(
    "change",
    [
        {"cipher": b"none"},
        {"kdf": b"none"},
        {"rounds": 63},
        {"count": 2},
        {"cipher": b"unknown"},
        {"trailing": b"garbage"},
    ],
)
def test_invalid_encryption_metadata_is_rejected(change):
    key, public = _envelope(**change)
    with pytest.raises(ValueError, match="encrypted Ed25519"):
        validate_encrypted_key(key, public)


@pytest.mark.parametrize("kind", ["truncated", "bad-base64", "mismatched-public", "oversized"])
def test_malformed_or_mismatched_enrollment_is_rejected(kind):
    key, public = _envelope()
    if kind == "truncated":
        key = key[:100]
    elif kind == "bad-base64":
        key = key.replace(b"b3Bl", b"!!!!")
    elif kind == "mismatched-public":
        public = (
            "ssh-ed25519 "
            + base64.b64encode(_string(b"ssh-ed25519") + _string(b"y" * 32)).decode()
        )
    else:
        key += b" " * 16384
    with pytest.raises(ValueError):
        validate_encrypted_key(key, public)


@pytest.mark.parametrize("purpose", [None, "host_terminals"])
def test_windows_opt_in_uses_pinned_launcher_and_drops_replaced_key(
    enrolled, monkeypatch, tmp_path, purpose
):
    config = tmp_path / "config.yaml"
    config.write_text("host:\n  elevated_terminals: windows_ssh\n")
    host = HostProcess(
        HostIdentity("a" * 32, "host"),
        "http://localhost",
        config_path=config,
        interactive_shells=["pwsh"],
    )
    assert load_elevated_terminals_method(config) == "windows_ssh"
    assert CAP_TERMINAL_ELEVATED in host._terminal_capabilities()
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)
    kwargs = {
        "server_url": "http://server",
        "runner_id": "runner",
        "binding_token": "synthetic-token",
        "workspace": "/opt/work/project",
        "parent_pid": 1,
        "elevated_terminals": "windows_ssh",
        "purpose": purpose,
    }
    assert _build_runner_env({}, **kwargs)[elevation.ELEVATED_TERMINALS_ENV] == "windows_ssh"
    spec = elevation.elevated_terminal_spec(
        TerminalEnvSpec(command="untrusted", args=["untrusted"], env={"SSH_ASKPASS": "untrusted"}),
        method="windows_ssh",
    )
    assert spec.command == str(enrolled.ssh)
    assert spec.args[-1] == f'"{enrolled.powershell}" -NoLogo -NoProfile'
    assert spec.env == {} and set(SSH_ENV_UNSET) <= set(spec.env_unset)
    assert "StrictHostKeyChecking=yes" in spec.args and "IdentityAgent=none" in spec.args
    assert f'UserKnownHostsFile="{(enrolled.key.parent / "known_hosts").as_posix()}"' in spec.args
    assert "-F" in spec.args and spec.args[spec.args.index("-F") + 1] == "NUL"
    assert (enrolled.key.parent / "known_hosts").read_text().startswith("127.0.0.1 ssh-ed25519 ")
    enriched = windows_terminal_environment(
        {name.lower(): "registry" for name in SSH_ENV_UNSET},
        dict.fromkeys(SSH_ENV_UNSET, "runner"),
        spec.env,
        spec.env_unset,
        {},
    )
    assert not set(SSH_ENV_UNSET) & enriched.keys()
    enrolled.key.write_bytes(_envelope(cipher=b"none")[0])
    assert CAP_TERMINAL_ELEVATED not in host._terminal_capabilities()
    assert load_elevated_terminals_method(config) is None
    assert elevation.ELEVATED_TERMINALS_ENV not in _build_runner_env({}, **kwargs)
    with pytest.raises(ValueError, match="not enabled"):
        elevation.elevated_terminal_spec(TerminalEnvSpec(), method="windows_ssh")


@pytest.mark.parametrize("missing", ["ssh", "key", "host_key", "powershell"])
def test_missing_launcher_component_disables_capability(enrolled, missing):
    Path(getattr(enrolled, missing)).unlink()
    assert not elevation.elevated_method_supported("windows_ssh")


def test_probe_runs_inside_the_same_selected_powershell(enrolled):
    runtime, probe = enrolled.argv(), enrolled.argv(probe=True)
    assert probe[:-1] == runtime[:-1]
    remote, encoded = probe[-1].split(" -EncodedCommand ")
    assert remote == runtime[-1]
    assert base64.b64decode(encoded).decode("utf-16-le") == TOKEN_PROBE
    assert "whoami /groups" in TOKEN_PROBE
    with pytest.raises(ValueError, match="missing"):
        replace(enrolled, powershell=str(enrolled.key.parent / "missing.exe")).argv(probe=True)


def _process_boundary(
    monkeypatch,
    enrolled,
    *,
    encrypted=True,
    token_ok=True,
    wrong_key_rejected=True,
    install_ok=True,
):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        code, output = 0, b""
        if "-Action" in argv:
            action = argv[argv.index("-Action") + 1]
            if action == "Install" and not install_ok:
                code = 1
        elif Path(argv[0]).name == "ssh-keygen.exe" and "-a" in argv:
            key, public = _envelope(cipher=b"aes256-ctr" if encrypted else b"none")
            enrolled.key.write_bytes(key)
            enrolled.key.with_suffix(".pub").write_text(public)
        elif Path(argv[0]).name == "ssh.exe":
            if "BatchMode=yes" in argv:
                code = 255 if wrong_key_rejected else 0
            else:
                code = 0 if token_ok else 1
                output = b"OMNIGENT_ADMIN_TOKEN_OK\r\n" if token_ok else b""
        return subprocess.CompletedProcess(argv, code, stdout=output)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        admin_terminal, "registered_user_environment", lambda: {"sSh_AuTh_SoCk": "registry"}
    )
    return calls


@pytest.mark.parametrize("failure", [None, "unencrypted", "token", "wrong-key", "service"])
def test_install_enables_config_only_after_all_checks(enrolled, monkeypatch, failure):
    from omnigent.cli import cli
    from omnigent.host.identity import host_config_path

    monkeypatch.setattr("omnigent._platform.IS_WINDOWS", True)
    enrolled.key.unlink()
    enrolled.key.with_suffix(".pub").unlink()
    calls = _process_boundary(
        monkeypatch,
        enrolled,
        encrypted=failure != "unencrypted",
        token_ok=failure != "token",
        wrong_key_rejected=failure != "wrong-key",
        install_ok=failure != "service",
    )
    result = CliRunner().invoke(cli, ["host", "admin-terminal", "install"], input="y\n")
    assert result.exit_code == (0 if failure is None else 1), result.output
    config = yaml.safe_load(host_config_path().read_text()) if host_config_path().exists() else {}
    assert config.get("host", {}).get("elevated_terminals") == (
        "windows_ssh" if failure is None else None
    )
    actions = [argv[argv.index("-Action") + 1] for argv, _ in calls if "-Action" in argv]
    assert actions == (
        ["Preflight"]
        if failure == "unencrypted"
        else ["Preflight", "Install"] + (["Remove"] if failure else [])
    )
    if failure == "unencrypted":
        assert enrolled.key.exists()
    for argv, kwargs in calls:
        if "-Action" in argv:
            assert argv[argv.index("-ExecutionPolicy") + 1] == "Bypass"
        if Path(argv[0]).name == "ssh.exe":
            assert kwargs["env"].keys().isdisjoint(SSH_ENV_UNSET)
            assert argv[-1].startswith(
                f'"{enrolled.powershell}" -NoLogo -NoProfile -EncodedCommand '
            )


@pytest.mark.parametrize("operation", ["install", "remove"])
def test_config_write_failure_cannot_prevent_service_cleanup(enrolled, monkeypatch, operation):
    from omnigent.cli import cli
    from omnigent.host.identity import host_config_path

    monkeypatch.setattr("omnigent._platform.IS_WINDOWS", True)
    if operation == "install":
        enrolled.key.unlink()
        enrolled.key.with_suffix(".pub").unlink()
    config = host_config_path()
    config.parent.mkdir(parents=True)
    config.write_text("host:\n  name: kept\n")
    replace_file = admin_terminal.os.replace

    def unwritable_config(source, destination):
        if Path(destination) == config:
            raise PermissionError("synthetic config write failure")
        return replace_file(source, destination)

    monkeypatch.setattr(admin_terminal.os, "replace", unwritable_config)
    calls = _process_boundary(monkeypatch, enrolled)
    result = CliRunner().invoke(cli, ["host", "admin-terminal", operation], input="y\n")
    assert result.exit_code == 1
    assert [argv[argv.index("-Action") + 1] for argv, _ in calls if "-Action" in argv] == (
        ["Preflight", "Install", "Remove"] if operation == "install" else ["CheckOwned", "Remove"]
    )
    assert not enrolled.key.exists()
    assert yaml.safe_load(config.read_text()) == {"host": {"name": "kept"}}
    assert "host configuration" in result.output


@pytest.mark.parametrize("text", ["host: null\n", "host: [\n"])
def test_invalid_config_is_rejected_before_enrollment(enrolled, monkeypatch, text):
    from omnigent.cli import cli
    from omnigent.host.identity import host_config_path

    monkeypatch.setattr("omnigent._platform.IS_WINDOWS", True)
    enrolled.key.unlink()
    enrolled.key.with_suffix(".pub").unlink()
    config = host_config_path()
    config.parent.mkdir(parents=True)
    config.write_text(text)
    calls = _process_boundary(monkeypatch, enrolled)
    result = CliRunner().invoke(cli, ["host", "admin-terminal", "install"], input="y\n")
    assert result.exit_code == 1
    assert not calls and not enrolled.key.exists()
    assert config.read_text() == text


def test_remove_clears_config_and_only_enrolled_files(enrolled, monkeypatch):
    from omnigent.cli import cli
    from omnigent.host.identity import host_config_path

    monkeypatch.setattr("omnigent._platform.IS_WINDOWS", True)
    config = host_config_path()
    config.parent.mkdir(parents=True)
    config.write_text(
        "server: http://example.invalid\nhost:\n  name: kept\n  elevated_terminals: windows_ssh\n"
    )
    unrelated = enrolled.key.parent / "keep.txt"
    unrelated.write_text("keep")
    calls = _process_boundary(monkeypatch, enrolled)
    result = CliRunner().invoke(cli, ["host", "admin-terminal", "remove"], input="y\n")
    assert result.exit_code == 0, result.output
    assert yaml.safe_load(config.read_text()) == {
        "server": "http://example.invalid",
        "host": {"name": "kept"},
    }
    assert not enrolled.key.exists() and unrelated.read_text() == "keep"
    assert [argv[argv.index("-Action") + 1] for argv, _ in calls] == ["CheckOwned", "Remove"]


@pytest.mark.parametrize("action", ["install", "remove"])
def test_cli_refusal_does_not_run_any_host_command(enrolled, monkeypatch, action):
    from omnigent.cli import cli

    monkeypatch.setattr("omnigent._platform.IS_WINDOWS", True)
    calls = _process_boundary(monkeypatch, enrolled)
    result = CliRunner().invoke(cli, ["host", "admin-terminal", action], input="n\n")
    assert result.exit_code == 1 and not calls


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["exit", "idle"])
async def test_windows_admin_registry_launch_input_and_cleanup(enrolled, monkeypatch, ending):
    from omnigent.inner import terminal
    from omnigent.terminals import TerminalRegistry, conpty
    from tests._helpers.polling import poll_until_async
    from tests.terminals.test_conpty import _Process

    process = _Process()
    spawned = []

    def spawn(argv, cwd, env, cols, rows):
        spawned.append((argv, env))
        return process

    monkeypatch.setattr(terminal, "IS_WINDOWS", True)
    monkeypatch.setattr(conpty.PseudoConsoleProcess, "spawn", spawn)
    monkeypatch.setattr(
        conpty,
        "registered_user_environment",
        lambda: {name.swapcase(): "registry" for name in SSH_ENV_UNSET},
    )
    for name in SSH_ENV_UNSET:
        monkeypatch.setenv(name, "runner")
    monkeypatch.setenv(elevation.ELEVATED_TERMINALS_ENV, "windows_ssh")
    registry = TerminalRegistry()
    instance = await registry.launch("session", "shell", "admin", TerminalEnvSpec(), elevated=True)
    try:
        assert instance.elevated and not instance.agent_access and instance.backend == "conpty"
        argv, env = spawned[0]
        assert argv[0] == str(enrolled.ssh) and argv[-1].endswith(" -NoLogo -NoProfile")
        assert set(env).isdisjoint(SSH_ENV_UNSET)
        process.output.put(b"Enter passphrase for key: ")
        await poll_until_async(lambda: b"passphrase" in instance._output.snapshot())
        await instance.send(keys="C-c")
        assert process.writes == [b"\x03"]
        registry.set_agent_access("session", "shell", "admin", instance.instance_id, True)
        if ending == "exit":
            process.code = 255
            process.exited.set()
        else:
            instance.last_input_at = time.monotonic() - 901
            instance.resize(90, 25)
            process.output.put(b"output cannot renew the input idle deadline")
        await poll_until_async(
            lambda: registry.get("session", "shell", "admin") is None, timeout_s=4
        )
        assert not instance.running and not instance.agent_access and process.closed
    finally:
        await registry.close("session", "shell", "admin")
