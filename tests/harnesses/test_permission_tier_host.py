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
    monkeypatch.setattr(host.Path, "home", lambda: Path("C:/Users/sample"))
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("ProgramFiles", r"D:\Apps")
    monkeypatch.setenv("ProgramFiles(x86)", r"E:\LegacyApps")
    monkeypatch.setattr(
        host,
        "resolve_local_path",
        lambda value: ResolvedPath(value, "directory" if value == r"C:\work" else "new-file"),
    )
    state = host.HostPermissionState("s", "h", r"C:\work", "auto", 1, "ready")
    context = host.local_host_context(state, control_paths=(r"C:\control",))
    for path in (r"D:\Apps\Application\config", r"E:\LegacyApps\Application\config"):
        intent = adapt_tool_call("write", {"path": path, "content": "x"}, context)
        assert decide_tier("auto", intent, load_rules()).rule_id == "protected-path"
