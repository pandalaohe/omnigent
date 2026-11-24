"""Host permission caches never recover execution by guessing a tier."""

from dataclasses import replace
from pathlib import Path

import pytest

from omnigent.permission_tiers import (
    ResolvedPath,
    Verdict,
    adapt_tool_call,
    decide_tier,
    host,
    load_rules,
)


def snapshot(workspace: Path) -> host.HostPermissionState:
    return host.HostPermissionState("session", "host", str(workspace), "auto", 3, "ready")


def test_host_cache_roundtrip_and_symlink_refusal(tmp_path: Path) -> None:
    path = tmp_path / host.HOST_PERMISSION_STATE_FILE
    state = snapshot(tmp_path)
    host.write_host_permission_state(path, state)
    assert host.read_host_permission_state(path) == state
    alias = tmp_path / "alias"
    alias.symlink_to(path)
    with pytest.raises(ValueError, match="symlink"):
        host.read_host_permission_state(alias)
    with pytest.raises(ValueError, match="symlink"):
        host.write_host_permission_state(alias, replace(state, tier="full"))
    assert host.read_host_permission_state(path) == state
    assert sorted(item.name for item in tmp_path.iterdir()) == [
        "alias",
        host.HOST_PERMISSION_STATE_FILE,
    ]


@pytest.mark.parametrize("payload", ["broken", "null", "[]", "{}", '{"tier":"full"}'])
def test_missing_or_malformed_cache_has_no_full_fallback(tmp_path: Path, payload: str) -> None:
    path = tmp_path / "state.json"
    with pytest.raises(FileNotFoundError):
        host.read_host_permission_state(path)
    path.write_text(payload)
    with pytest.raises((ValueError, TypeError)):
        host.read_host_permission_state(path)


def test_host_checks_paths_rules_and_blocked_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = snapshot(tmp_path)
    controls = (str(tmp_path / "control"),)
    result = host.evaluate_host_tool(
        state, "write", {"path": "new/file", "content": "x"}, control_paths=controls
    )
    assert result.action == Verdict.ALLOW
    assert (
        host.evaluate_host_tool(
            state, "bash", {"command": "mkfs.ext4 /dev/disk4"}, control_paths=controls
        ).action
        == Verdict.DENY
    )
    assert (
        host.evaluate_host_tool(
            replace(state, status="blocked"), "read", {"path": "a"}, control_paths=controls
        ).action
        == Verdict.DENY
    )
    assert (
        host.evaluate_host_tool(
            replace(state, tier="full", status="blocked"),
            "read",
            {"path": "a"},
            control_paths=controls,
        ).action
        == Verdict.DENY
    )

    def broken_rules():
        raise OSError("rules unavailable")

    monkeypatch.setattr(host, "load_rules", broken_rules)
    assert (
        host.evaluate_host_tool(state, "read", {"path": "a"}, control_paths=controls).action
        == Verdict.DENY
    )
    assert (
        host.evaluate_host_tool(
            replace(state, tier="full"), "read", {"path": "a"}, control_paths=controls
        ).action
        == Verdict.ALLOW
    )


def test_windows_nondefault_installation_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(host.sys, "platform", "win32")
    monkeypatch.setattr(host.Path, "home", lambda: Path("C:/opt/work"))
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("ProgramFiles", r"D:\Apps")
    monkeypatch.setenv("ProgramFiles(x86)", r"E:\LegacyApps")
    monkeypatch.setenv("USERPROFILE", r"C:\opt\work")
    monkeypatch.setattr(
        host,
        "resolve_local_path",
        lambda value: ResolvedPath(value, "directory" if value == r"C:\work" else "new-file"),
    )
    state = host.HostPermissionState("s", "h", r"C:\work", "auto", 1, "ready")
    context = host.local_host_context(state, control_paths=(r"C:\control",))
    assert context.environment["USERPROFILE"] == r"C:\opt\work"
    for path in (r"D:\Apps\Application\config", r"E:\LegacyApps\Application\config"):
        intent = adapt_tool_call("write", {"path": path, "content": "x"}, context)
        assert decide_tier("auto", intent, load_rules()).rule_id == "protected-path"


@pytest.fixture
def fresh_windows_shell_selection():
    from omnigent._platform import _windows_os_env_shell_selection

    _windows_os_env_shell_selection.cache_clear()
    yield
    _windows_os_env_shell_selection.cache_clear()


@pytest.mark.usefixtures("fresh_windows_shell_selection")
@pytest.mark.asyncio
@pytest.mark.parametrize("selection", ["bash", "comspec", "removed-bash"])
async def test_windows_os_shell_checks_the_selected_executor(tmp_path, monkeypatch, selection):
    from omnigent import _platform
    from omnigent.inner import os_env
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    comspec = r"C:\Windows\System32\cmd.exe"
    bash = tmp_path / "bash.exe"
    bash.touch()
    shell = None if selection == "comspec" else str(bash)
    monkeypatch.setattr(_platform, "IS_WINDOWS", True)
    monkeypatch.setattr(os_env, "IS_WINDOWS", True)
    monkeypatch.setattr(
        _platform, "_shell_on_path", lambda name: shell if name == "bash" else None
    )
    monkeypatch.setattr(_platform, "_git_for_windows_bash", lambda: None)
    monkeypatch.setattr(host.sys, "platform", "win32")
    monkeypatch.setattr(host.Path, "home", lambda: Path("C:/work/sample"))
    monkeypatch.setenv("COMSPEC", comspec)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setattr(
        host,
        "resolve_local_path",
        lambda value: ResolvedPath(value, "directory" if value == r"C:\work" else "new-file"),
    )
    executor = os_env.create_os_environment(
        OSEnvSpec(type="caller_process", cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert executor is not None
    assert executor.shell_path == (shell or comspec)
    if selection == "removed-bash":
        bash.unlink()
    monkeypatch.setattr(
        _platform,
        "_shell_on_path",
        lambda name: str(bash) if shell is None and name == "bash" else None,
    )
    state = host.HostPermissionState("s", "h", r"C:\work", "auto", 1, "ready")
    command = 'printf "%s" "$label"'
    result = host.evaluate_host_tool(
        state, "sys_os_shell", {"command": command}, control_paths=(r"C:\control",)
    )
    assert result.action == (Verdict.ALLOW if selection == "bash" else Verdict.DENY)
    monkeypatch.setattr(os_env, "_shell_impl", lambda **kwargs: {"shell": kwargs["shell_path"]})
    monkeypatch.setattr(
        type(executor._helper),
        "request",
        lambda helper, request, **kwargs: os_env._handle_helper_request(
            request=request, cwd=tmp_path, shell_path=helper.shell_path, sandbox=executor.sandbox
        ),
    )
    try:
        launched = await executor.shell("echo ok")
        assert launched["shell"] == (str(bash) if selection == "bash" else comspec)
    finally:
        executor.close()
    native = host.evaluate_host_tool(
        state, "run_command", {"CommandLine": command}, control_paths=(r"C:\control",)
    )
    assert native.action == Verdict.DENY
