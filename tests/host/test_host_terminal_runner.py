"""Terminal-purpose daemon launch, codec and credential-free environment boundaries."""

from __future__ import annotations

import asyncio
import ctypes
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import omnigent.host.connect as host_connect
from omnigent.host.connect import HostProcess, _build_runner_env
from omnigent.host.frames import (
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostRunnerStatusFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.identity import HostIdentity
from omnigent.runner.identity import RUNNER_PURPOSE_ENV_VAR


@pytest.mark.parametrize(
    "frame",
    [
        HostHelloFrame("test", 1, "test-host", terminal_runner_id="runner-a"),
        HostLaunchRunnerFrame("request", "synthetic-binding", "~", purpose="host_terminals"),
        HostLaunchRunnerResultFrame("request", "exists", runner_id="runner-a", exists="runner-a"),
        HostRunnerStatusFrame("request", "runner-a", purpose="host_terminals"),
    ],
)
def test_terminal_frame_roundtrip(frame) -> None:
    assert decode_host_frame(encode_host_frame(frame)) == frame


def test_unknown_runner_purpose_is_rejected() -> None:
    raw = json.dumps(
        {
            "kind": "host.launch_runner",
            "request_id": "r",
            "binding_token": "synthetic",
            "workspace": "~",
            "purpose": "agent",
        }
    )
    with pytest.raises(ValueError, match="purpose"):
        decode_host_frame(raw)


def test_terminal_runner_drops_harness_and_provider_environment() -> None:
    env = _build_runner_env(
        {
            "PATH": "/usr/bin:/bin",
            "HOME": "/opt/work/home",
            "LC_ALL": "C",
            "ANTHROPIC_API_KEY": "synthetic-provider",
            "CUSTOM_PROVIDER_KEY": "synthetic-custom",
            "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "CUSTOM_PROVIDER_KEY",
            "OTEL_EXPORTER_OTLP_HEADERS": "synthetic-exporter-auth",
        },
        server_url="http://testserver",
        runner_id="runner-a",
        binding_token="synthetic-binding",
        workspace="/opt/work/home",
        parent_pid=123,
        purpose="host_terminals",
    )
    assert env[RUNNER_PURPOSE_ENV_VAR] == "host_terminals"
    assert env["HOME"] == "/opt/work/home" and env["LC_ALL"] == "C"
    assert "ANTHROPIC_API_KEY" not in env and "CUSTOM_PROVIDER_KEY" not in env
    assert "OTEL_EXPORTER_OTLP_HEADERS" not in env
    assert "OMNIGENT_RUNNER_INITIAL_AUTH_TOKEN" not in env


@pytest.mark.asyncio
async def test_daemon_serializes_terminal_launches_and_answers_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_ZYGOTE", "0")
    host = HostProcess(
        HostIdentity("11aa11aa11aa11aa11aa11aa11aa11aa", "test-host"),
        "http://testserver",
        interactive_shells=["sh"],
        config_path=tmp_path / "config.yaml",
    )
    monkeypatch.setattr(host, "_host_terminals_supported", lambda: True)
    monkeypatch.setattr(
        host, "_current_auth_token", Mock(side_effect=AssertionError("host bearer read"))
    )
    monkeypatch.setattr(host, "_watch_runner", AsyncMock())
    monkeypatch.setattr(host, "_watch_runner_connect", AsyncMock())
    monkeypatch.setenv("HOME", str(tmp_path))
    proc = SimpleNamespace(pid=987654, poll=lambda: None)
    spawn = Mock(return_value=(proc, tmp_path / "runner.log"))
    monkeypatch.setattr(host, "_spawn_runner_proc", spawn)
    sent = []
    monkeypatch.setattr(
        host,
        "_send_text",
        AsyncMock(side_effect=lambda _ws, raw: sent.append(decode_host_frame(raw))),
    )
    frames = [
        HostLaunchRunnerFrame(
            f"request-{i}", f"synthetic-binding-{i}", "/opt/work/ignored", purpose="host_terminals"
        )
        for i in range(2)
    ]
    await asyncio.gather(*(host._dispatch_host_frame(object(), frame) for frame in frames))
    await asyncio.gather(*host._watcher_tasks)
    assert sorted(frame.status for frame in sent) == ["exists", "launched"]
    assert spawn.call_count == 1
    assert spawn.call_args.args[2] == tmp_path
    assert len(host._runners) == 1
    assert sent[1].exists == sent[0].runner_id
    absent = await host._handle_runner_status(
        HostRunnerStatusFrame("status", "absent", purpose="host_terminals")
    )
    ordinary = await host._handle_runner_status(HostRunnerStatusFrame("status", "absent"))
    assert absent.status == "dead" and ordinary.status == "unknown"


@pytest.mark.asyncio
async def test_terminal_launch_refuses_session_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = HostProcess(
        HostIdentity("11aa11aa11aa11aa11aa11aa11aa11aa", "test-host"),
        "http://testserver",
        interactive_shells=["sh"],
        config_path=tmp_path / "config.yaml",
    )
    spawn = Mock(side_effect=AssertionError("spawned"))
    monkeypatch.setattr(host, "_spawn_runner_proc", spawn)
    result = await host._handle_launch(
        HostLaunchRunnerFrame(
            "r", "synthetic", "~", session_id="session", purpose="host_terminals"
        )
    )
    assert result.status == "failed"
    spawn.assert_not_called()


def test_terminal_runner_keeps_windows_constants_but_drops_secrets() -> None:
    """Host-purpose env keeps Windows process constants/TEMP and still drops secrets.

    ``SYSTEMROOT`` is mandatory for Winsock and ``PROGRAMDATA`` for Win32-OpenSSH;
    dropping them silently broke shells on Windows (#10926/#10934). Provider and
    exporter credentials remain filtered regardless of OS.
    """
    base_env = {
        "PATH": r"C:\Windows\System32",
        "TEMP": r"C:\Users\sample\AppData\Local\Temp",
        "TMP": r"C:\Users\sample\AppData\Local\Temp",
        "SYSTEMROOT": r"C:\Windows",
        "PROGRAMDATA": r"C:\ProgramData",
        "USERPROFILE": r"C:\Users\sample",
        "ANTHROPIC_API_KEY": "synthetic-provider",
        "CUSTOM_PROVIDER_KEY": "synthetic-custom",
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "CUSTOM_PROVIDER_KEY",
        "OTEL_EXPORTER_OTLP_HEADERS": "synthetic-exporter-auth",
        "SSH_AUTH_SOCK": "synthetic-agent-socket",
    }
    env = _build_runner_env(
        base_env,
        server_url="http://testserver",
        runner_id="runner-a",
        binding_token="synthetic-binding",
        workspace=r"C:\Users\sample",
        parent_pid=123,
        purpose="host_terminals",
    )
    assert env[RUNNER_PURPOSE_ENV_VAR] == "host_terminals"
    for name in ("PATH", "TEMP", "TMP", "SYSTEMROOT", "PROGRAMDATA", "USERPROFILE"):
        assert env[name] == base_env[name]
    for name in (
        "ANTHROPIC_API_KEY",
        "CUSTOM_PROVIDER_KEY",
        "OTEL_EXPORTER_OTLP_HEADERS",
        "SSH_AUTH_SOCK",
    ):
        assert name not in env


def _terminal_capability_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, shells: list[str]
) -> HostProcess:
    monkeypatch.setattr(host_connect, "IS_WINDOWS", True)
    monkeypatch.setattr(host_connect, "IS_POSIX", False)
    return HostProcess(
        HostIdentity("11aa11aa11aa11aa11aa11aa11aa11aa", "test-host"),
        "http://testserver",
        interactive_shells=shells,
        config_path=tmp_path / "config.yaml",
    )


def test_windows_terminal_support_needs_conpty_and_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Windows needs the ConPTY export plus an inventory, never tmux."""
    monkeypatch.setattr(host_connect, "_windows_conpty_available", lambda: True)
    monkeypatch.setattr(
        host_connect.shutil, "which", Mock(side_effect=AssertionError("tmux probe on Windows"))
    )
    host = _terminal_capability_host(monkeypatch, tmp_path, ["pwsh"])
    assert host._host_terminals_supported() is True
    monkeypatch.setattr(host_connect, "_windows_conpty_available", lambda: False)
    assert host._host_terminals_supported() is False


def test_windows_terminal_support_keeps_an_empty_inventory_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An empty Windows inventory must not fall back to bash."""
    monkeypatch.setattr(host_connect, "_windows_conpty_available", lambda: True)
    host = _terminal_capability_host(monkeypatch, tmp_path, [])
    assert host._interactive_shells == []
    assert host._host_terminals_supported() is False


@pytest.mark.posix_only
def test_posix_terminal_support_still_requires_tmux_and_inventory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    host = HostProcess(
        HostIdentity("11aa11aa11aa11aa11aa11aa11aa11aa", "test-host"),
        "http://testserver",
        interactive_shells=["sh"],
        config_path=tmp_path / "config.yaml",
    )
    monkeypatch.setattr(
        host_connect.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None
    )
    assert host._host_terminals_supported() is True
    monkeypatch.setattr(host_connect.shutil, "which", lambda name: None)
    assert host._host_terminals_supported() is False
    host._interactive_shells = []
    monkeypatch.setattr(
        host_connect.shutil, "which", lambda name: "/usr/bin/tmux" if name == "tmux" else None
    )
    assert host._host_terminals_supported() is False


class _FakeKernel32:
    def __init__(self, exports: set[str]) -> None:
        self._exports = exports

    def __getattr__(self, name: str):
        if name in self._exports:
            return lambda *_args: None
        raise AttributeError(name)


def test_windows_conpty_probe_requires_the_kernel32_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(host_connect, "IS_WINDOWS", True)
    monkeypatch.setattr(
        ctypes,
        "WinDLL",
        lambda *_args, **_kwargs: _FakeKernel32({"CreatePseudoConsole"}),
        raising=False,
    )
    assert host_connect._windows_conpty_available() is True
    monkeypatch.setattr(
        ctypes, "WinDLL", lambda *_args, **_kwargs: _FakeKernel32(set()), raising=False
    )
    assert host_connect._windows_conpty_available() is False

    def broken_loader(*_args, **_kwargs):
        raise OSError("no kernel32")

    monkeypatch.setattr(ctypes, "WinDLL", broken_loader, raising=False)
    assert host_connect._windows_conpty_available() is False
    monkeypatch.setattr(host_connect, "IS_WINDOWS", False)
    assert host_connect._windows_conpty_available() is False
