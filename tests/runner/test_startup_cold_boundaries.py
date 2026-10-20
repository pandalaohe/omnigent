"""Cold admission precedes configuration, teardown and physical adapter work."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.runner.native import _ensure_native_terminal, _launch_native_terminal
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager, _SubprocessEntry
from tests.runner.test_app_sessions_native_workflow_init import (
    _ensure_ctx,
    _FakeEnsureRegistry,
    _launch_ctx,
    _terminal_view,
)


@pytest.mark.asyncio
async def test_native_cold_refusal_precedes_context_probes(monkeypatch):
    denied, build = AsyncMock(side_effect=RuntimeError("startup not admitted")), AsyncMock()
    ctx = _launch_ctx(startup_required=True, admit_cold=denied)
    with pytest.raises(RuntimeError, match="startup not admitted"):
        await _launch_native_terminal(
            "pi-native", ctx, ensure_locks={}, build_context=build, reraise=True
        )
    build.assert_not_awaited()
    denied.assert_awaited_once()


@pytest.mark.asyncio
async def test_harness_shutdown_during_admission_prevents_spawn(tmp_path, monkeypatch):
    manager = HarnessProcessManager(tmp_parent=tmp_path)
    manager._started = True

    async def shutdown_while_admitting(*_args):
        manager._shutting_down = True

    manager.set_startup_admitter(shutdown_while_admitting)
    spawn = AsyncMock()
    monkeypatch.setattr(manager, "_spawn_entry", spawn)
    with pytest.raises(RuntimeError, match="stopped during startup admission"):
        await manager.get_client("session", "pi")
    spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_replacement_refusal_preserves_existing_terminal():
    registry = _FakeEnsureRegistry(existing=_terminal_view("existing"))
    denied, build = AsyncMock(side_effect=RuntimeError("startup not admitted")), AsyncMock()
    ctx = replace(_ensure_ctx(registry), startup_required=True, admit_cold=denied)
    response = await _ensure_native_terminal(
        "goose",
        ctx,
        ensure_locks={},
        build_context=build,
        is_owned=lambda *_: False,
    )
    assert response.status_code >= 400
    assert registry.closed == []
    build.assert_not_awaited()
    denied.assert_awaited_once()


@pytest.mark.asyncio
async def test_warm_native_terminal_needs_no_new_start_permission():
    registry = _FakeEnsureRegistry(existing=_terminal_view("existing"))
    denied = AsyncMock(side_effect=RuntimeError("startup not admitted"))
    ctx = replace(_ensure_ctx(registry), startup_required=True, admit_cold=denied)
    assert (await _ensure_native_terminal("goose", ctx, ensure_locks={})).status_code == 200
    denied.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["cold", "exited", "agent", "model", "acp"])
async def test_harness_refusal_precedes_spawn_and_replacement_teardown(
    tmp_path, monkeypatch, reason
):
    manager = HarnessProcessManager(tmp_parent=tmp_path)
    manager._started = True
    harness, env = "pi", None
    if reason != "cold":
        entry = _SubprocessEntry(
            process=SimpleNamespace(returncode=0 if reason == "exited" else None),
            client=object(),
            endpoint=SimpleNamespace(socket_path=None),
            harness="claude-sdk" if reason == "agent" else "pi",
            model="previous",
        )
        manager._entries["session"] = entry
    if reason == "model":
        env = {"HARNESS_PI_MODEL": "next"}
    elif reason == "acp":
        manager._entries["session"].harness = "acp"
        harness, env = "acp", {"HARNESS_ACP_COMMAND": "next"}
    close, spawn = AsyncMock(), AsyncMock()
    monkeypatch.setattr(manager, "_close_entry", close)
    monkeypatch.setattr(manager, "_spawn_entry", spawn)
    denied = AsyncMock(side_effect=RuntimeError("startup not admitted"))
    manager.set_startup_admitter(denied)
    with pytest.raises(RuntimeError, match="startup not admitted"):
        await manager.get_client("session", harness, env=env)
    close.assert_not_awaited()
    spawn.assert_not_awaited()
    denied.assert_awaited_once_with("session", harness)
