"""Synthetic key envelopes and process boundaries; no host enrollment or real keys."""

from __future__ import annotations

import base64
import shutil
import struct
import subprocess
import sys
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
    service_failures=None,
    locked_result_action=None,
):
    calls = []
    locked_result = None

    if locked_result_action is not None:
        unlink = Path.unlink

        def unlink_result(path, *args, **kwargs):
            if path == locked_result:
                raise PermissionError("synthetic locked result file")
            return unlink(path, *args, **kwargs)

        monkeypatch.setattr(Path, "unlink", unlink_result)

    def run(argv, **kwargs):
        nonlocal locked_result
        calls.append((argv, kwargs))
        code, output = 0, b""
        if "-Action" in argv:
            action = argv[argv.index("-Action") + 1]
            if action == locked_result_action:
                locked_result = Path(argv[argv.index("-ResultPath") + 1])
            failure = (service_failures or {}).get(action)
            if failure is not None:
                code = 1
                if failure and "-ResultPath" in argv:
                    receipt = Path(argv[argv.index("-ResultPath") + 1])
                    assert receipt.read_text(encoding="ascii") == receipt.name
                    receipt.write_text(failure, encoding="utf-8")
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
        service_failures={"Install": ""} if failure == "service" else None,
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
            receipt = Path(argv[argv.index("-ResultPath") + 1])
            assert receipt.parent == enrolled.key.parents[2]
            assert not receipt.exists()
        if Path(argv[0]).name == "ssh.exe":
            assert kwargs["env"].keys().isdisjoint(SSH_ENV_UNSET)
            assert argv[-1].startswith(
                f'"{enrolled.powershell}" -NoLogo -NoProfile -EncodedCommand '
            )


@pytest.mark.parametrize(
    ("install_failure", "remove_failure", "expected"),
    [
        ("sshd -t: 設定錯誤", None, "Install failed at sshd -t: 設定錯誤"),
        ("", None, "PowerShell exit 1). No diagnostic was returned."),
        (
            "listener check: listener absent",
            "file cleanup: directory is in use",
            "Install failed at listener check: listener absent",
        ),
    ],
)
def test_install_cli_reports_service_failure(
    enrolled, monkeypatch, install_failure, remove_failure, expected
):
    from omnigent.cli import cli

    monkeypatch.setattr("omnigent._platform.IS_WINDOWS", True)
    enrolled.key.unlink()
    enrolled.key.with_suffix(".pub").unlink()
    failures = {"Install": install_failure}
    if remove_failure is not None:
        failures["Remove"] = remove_failure
    calls = _process_boundary(monkeypatch, enrolled, service_failures=failures)

    result = CliRunner().invoke(cli, ["host", "admin-terminal", "install"], input="y\n")

    assert result.exit_code == 1, result.output
    assert expected in result.output
    if remove_failure is not None:
        assert "Automatic cleanup incomplete" in result.output
        assert remove_failure in result.output
    receipts = [
        Path(argv[argv.index("-ResultPath") + 1]) for argv, _ in calls if "-Action" in argv
    ]
    assert len(set(receipts)) == 3
    assert all(not receipt.exists() for receipt in receipts)


@pytest.mark.parametrize(
    ("action", "stage", "diagnostic"),
    [
        ("install", "Preflight", "Existing sshd service; refusing to take ownership."),
        ("remove", "CheckOwned", "Omnigent enrollment marker is missing; refusing removal."),
    ],
)
def test_admin_terminal_failure_omits_stale_host_hint(
    enrolled, monkeypatch, capsys, action, stage, diagnostic
):
    from omnigent import cli as cli_module

    monkeypatch.setattr("omnigent._platform.IS_WINDOWS", True)
    monkeypatch.setattr(cli_module.click, "confirm", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(cli_module, "_load_global_config", dict)
    monkeypatch.setattr(cli_module, "_maybe_fast_backfill_install_ledger", lambda argv: None)
    monkeypatch.setattr("omnigent.cli_diagnostics.setup_cli_logging", lambda argv: None)
    monkeypatch.setattr(sys, "argv", ["omnigent", "host", "admin-terminal", action])
    calls = _process_boundary(monkeypatch, enrolled, service_failures={stage: diagnostic})

    with pytest.raises(SystemExit) as raised:
        cli_module.main()

    assert raised.value.code == 1
    error = capsys.readouterr().err
    assert f"Windows admin-terminal {stage} failed at {diagnostic}" in error
    assert "omnigent stop" not in error
    assert "stale host processes" not in error
    assert [argv[argv.index("-Action") + 1] for argv, _ in calls] == [stage]


def test_preflight_names_leftovers_and_gates_recovery_on_ownership():
    script = Path(admin_terminal.__file__).with_name("windows_admin_terminal.ps1").read_text()
    preflight = script.split("function Assert-Fresh {", 1)[1].split("function Assert-Owned {", 1)[
        0
    ]

    assert "Existing Omnigent enrollment marker at $marker" in preflight
    assert (
        preflight.index("try { Assert-Owned } catch { throw")
        < preflight.index("omni host admin-terminal remove")
        < preflight.index("if ($service -and $sshEntries.Count -eq 0)")
    )
    assert "An existing sshd service is not Omnigent-owned" in preflight
    assert '"run Remove-WindowsCapability -Online -Name $capability' in preflight
    assert "$capability = 'OpenSSH.Server~~~~0.0.1.0'" in script
    assert "restart Windows if asked" in preflight
    assert r"Existing ProgramData\ssh entries: $($sshEntries.Name -join" in preflight
    assert "Existing OpenSSH DefaultShell '$shell' is not cmd.exe" in preflight


def test_check_owned_distinguishes_marker_refusals():
    script = Path(admin_terminal.__file__).with_name("windows_admin_terminal.ps1").read_text()
    check_owned = script.split("function Assert-Owned {", 1)[1].split("function Protect-File", 1)[
        0
    ]

    assert "Omnigent enrollment marker is missing; refusing removal." in check_owned
    assert "Omnigent enrollment marker at $marker is not readable" in check_owned
    assert "Omnigent enrollment marker is not valid JSON; refusing removal." in check_owned
    assert "Enrollment marker kind is not omnigent-admin-terminal-v1" in check_owned
    assert "Enrollment marker id is missing or invalid" in check_owned
    assert "Enrollment marker id does not match this installation" in check_owned


def test_install_preserves_service_diagnostic_with_locked_result(enrolled, monkeypatch):
    from omnigent.cli import cli

    monkeypatch.setattr("omnigent._platform.IS_WINDOWS", True)
    enrolled.key.unlink()
    enrolled.key.with_suffix(".pub").unlink()
    calls = _process_boundary(
        monkeypatch,
        enrolled,
        service_failures={"Install": "sshd -t: invalid configuration"},
        locked_result_action="Install",
    )

    result = CliRunner().invoke(cli, ["host", "admin-terminal", "install"], input="y\n")

    assert result.exit_code == 1
    assert "Install failed at sshd -t: invalid configuration" in result.output
    assert [argv[argv.index("-Action") + 1] for argv, _ in calls if "-Action" in argv] == [
        "Preflight",
        "Install",
        "Remove",
    ]
    assert not enrolled.key.exists() and not enrolled.key.with_suffix(".pub").exists()


def test_remove_cleans_local_keys_with_locked_result(enrolled, monkeypatch):
    from omnigent.cli import cli

    monkeypatch.setattr("omnigent._platform.IS_WINDOWS", True)
    known_hosts = enrolled.key.parent / "known_hosts"
    known_hosts.touch()
    calls = _process_boundary(monkeypatch, enrolled, locked_result_action="Remove")

    result = CliRunner().invoke(cli, ["host", "admin-terminal", "remove"], input="y\n")

    assert result.exit_code == 0, result.output
    assert [argv[argv.index("-Action") + 1] for argv, _ in calls] == ["CheckOwned", "Remove"]
    assert not enrolled.key.exists()
    assert not enrolled.key.with_suffix(".pub").exists()
    assert not known_hosts.exists()


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
