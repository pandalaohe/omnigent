"""Ground-truth probe for absent panes at the native reaper.

``close_terminal_detailed`` returning ``"absent"`` no longer proves the pane
is gone: ``TerminalRegistry.close`` pops before awaiting the inner close, so
a concurrent closer's pop reads as absent while the pane is still alive. The
reaper must probe ``pane.instance.is_alive()`` before retiring anything.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.runner.app import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.terminals.pane_reaper import NativePaneReaper, PaneRef
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.helpers import NullServerClient


def _build_app_with_reaper(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]):
    from omnigent.runner import tool_dispatch
    from omnigent.runner.native import orchestration

    async def _fake_teardown(session_id: str) -> None:
        calls["teardown"].append(session_id)

    def _fake_publish_event(**kwargs: Any) -> None:
        calls["events"].append(kwargs)

    monkeypatch.setattr(orchestration, "teardown_codex_native_app_server", _fake_teardown)
    monkeypatch.setattr(tool_dispatch, "_publish_terminal_deleted_event", _fake_publish_event)
    terminal_registry = TerminalRegistry()
    app = create_runner_app(
        terminal_registry=terminal_registry,
        resource_registry=SessionResourceRegistry(terminal_registry=terminal_registry),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, terminal_registry


def _alive_instance(tmp_path: Path, *, alive: bool, probe_log: list[str]) -> Any:
    async def _is_alive() -> bool:
        probe_log.append("probed")
        return alive

    async def _close() -> None:
        return None

    return SimpleNamespace(
        running=True,
        socket_path=tmp_path / "tmux.sock",
        close=_close,
        is_alive=_is_alive,
    )


async def test_absent_but_alive_raises_without_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent + is_alive True: raise, no app-server teardown, no event."""
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _ = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    probe_log: list[str] = []
    pane = PaneRef(
        "conv_absent_alive",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
        instance=_alive_instance(tmp_path, alive=True, probe_log=probe_log),
    )
    with pytest.raises(RuntimeError, match="still alive"):
        await reaper._reap(pane)
    assert probe_log == ["probed"]
    assert calls["teardown"] == []
    assert calls["events"] == []


async def test_absent_and_dead_retires_session_pieces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Absent + is_alive False: retire both, as today, after probing."""
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _ = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    probe_log: list[str] = []
    pane = PaneRef(
        "conv_absent_dead",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
        instance=_alive_instance(tmp_path, alive=False, probe_log=probe_log),
    )
    await reaper._reap(pane)
    assert probe_log == ["probed"]
    assert calls["teardown"] == ["conv_absent_dead"]
    assert [e["conversation_id"] for e in calls["events"]] == ["conv_absent_dead"]


async def test_release_now_absent_but_alive_fails_and_stays_managed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Through the real caller: absent-but-alive release_now -> failed, stays managed."""
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _ = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    probe_log: list[str] = []
    pane = PaneRef(
        "conv_release_alive",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
        instance=_alive_instance(tmp_path, alive=True, probe_log=probe_log),
    )

    async def _not_busy(_pane: PaneRef) -> bool:
        return False

    releasing = NativePaneReaper(
        list_native_panes=lambda: [pane],
        is_busy=_not_busy,
        reap=reaper._reap,
    )
    releasing.manage("conv_release_alive")
    assert await releasing.release_now("conv_release_alive") == "failed"
    assert "conv_release_alive" in releasing._managed_conversations
    assert calls["teardown"] == []
    assert calls["events"] == []


async def _wait_until_empty(reg: TerminalRegistry, conv: str) -> None:
    for _ in range(500):
        if reg.list_for_conversation(conv) == []:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("close did not pop the instance in time")


async def test_popped_but_alive_pane_is_not_torn_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cross-generation: a pane another closer popped but still alive is not retired."""
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, terminal_registry = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_close() -> None:
        started.set()
        await release.wait()

    async def _still_alive() -> bool:
        return True

    instance = SimpleNamespace(
        running=True,
        socket_path=tmp_path / "tmux.sock",
        close=_slow_close,
        is_alive=_still_alive,
    )
    terminal_registry._by_conversation.setdefault("conv_race", {})[("codex", "main")] = instance
    terminal_registry._instance_locks[("conv_race", "codex", "main")] = threading.Lock()
    pane = PaneRef(
        "conv_race",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
        instance=instance,
    )
    task = asyncio.create_task(terminal_registry.close("conv_race", "codex", "main"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await _wait_until_empty(terminal_registry, "conv_race")
        with pytest.raises(RuntimeError, match="still alive"):
            await reaper._reap(pane)
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=5)
    assert calls["teardown"] == []
    assert calls["events"] == []
