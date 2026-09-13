"""Concurrent-close observability: "closing" vs genuinely "absent" at the reaper.

A terminal DELETE running concurrently with the native-pane reaper pops the
instance (invisible to ``list_for_conversation``) while the pane is still
alive. ``close_terminal_detailed`` must report that window as ``"closing"``
so ``_reap_native_pane`` leaves the session-level pieces alone; only a
genuinely gone terminal reports ``"absent"`` and retires them.
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
from omnigent.terminals.pane_reaper import PaneRef
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.helpers import NullServerClient


async def _wait_until_empty(reg: TerminalRegistry, conv: str) -> None:
    """Wait until the conversation lists no terminals (close popped it)."""
    for _ in range(500):
        if reg.list_for_conversation(conv) == []:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("close did not pop the instance in time")


async def test_detailed_close_reports_closing_during_concurrent_close(
    tmp_path: Path,
) -> None:
    """A concurrent in-flight close reads as "closing", not "absent"."""
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_close() -> None:
        started.set()
        await release.wait()

    instance = SimpleNamespace(
        running=True,
        socket_path=tmp_path / "tmux.sock",
        close=_slow_close,
    )
    terminal_registry._by_conversation.setdefault("conv_race", {})[("codex", "main")] = instance
    terminal_registry._instance_locks[("conv_race", "codex", "main")] = threading.Lock()
    terminal_id = terminal_resource_id("codex", "main")

    task = asyncio.create_task(terminal_registry.close("conv_race", "codex", "main"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await _wait_until_empty(terminal_registry, "conv_race")

        assert await registry.close_terminal_detailed("conv_race", terminal_id) == "closing"
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=5)

    assert await registry.close_terminal_detailed("conv_race", terminal_id) == "absent"


async def test_detailed_close_reports_absent_when_never_existed() -> None:
    """A terminal that genuinely never existed still reports "absent"."""
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    terminal_id = terminal_resource_id("codex", "main")

    assert await registry.close_terminal_detailed("conv_never", terminal_id) == "absent"


def _build_app_with_reaper(monkeypatch: pytest.MonkeyPatch, calls: dict[str, list[Any]]):
    """Build a runner app whose reaper teardown/event surface into *calls*."""
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


async def test_reaper_leaves_session_pieces_alone_while_closing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """On "closing" the reaper returns quietly: no teardown, no event, no raise."""
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, terminal_registry = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_close() -> None:
        started.set()
        await release.wait()

    instance = SimpleNamespace(
        running=True,
        socket_path=tmp_path / "tmux.sock",
        close=_slow_close,
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

    # A concurrent DELETE owns this terminal right now: pop it and hold the
    # inner close open so the reaper observes the in-flight window.
    task = asyncio.create_task(terminal_registry.close("conv_race", "codex", "main"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await _wait_until_empty(terminal_registry, "conv_race")

        await reaper._reap(pane)
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=5)

    assert calls["teardown"] == []
    assert calls["events"] == []


async def test_reaping_absent_pane_still_retires_session_pieces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """On "absent" the reaper still tears down the app-server and publishes."""
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _terminal_registry = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    pane = PaneRef(
        "conv_gone",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
        instance=object(),
    )

    await reaper._reap(pane)

    assert calls["teardown"] == ["conv_gone"]
    assert [event["conversation_id"] for event in calls["events"]] == ["conv_gone"]
