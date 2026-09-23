"""Absent-pane handling at the reaper.

``close_terminal_detailed`` reports ``"absent"`` when no terminal with that
id is listed any more (genuinely gone, or popped by a concurrent close whose
await has not settled). ``_reap_native_pane`` probes ``is_alive()`` ground
truth before retiring anything; only a genuinely dead pane retires them.
"""

from __future__ import annotations

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


async def test_reaping_absent_pane_still_retires_session_pieces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """On "absent" with a dead pane the reaper tears down the app-server and publishes."""
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, _terminal_registry = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None

    async def _dead() -> bool:
        return False

    async def _close() -> None:
        return None

    pane = PaneRef(
        "conv_gone",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
        instance=SimpleNamespace(
            running=True,
            socket_path=tmp_path / "tmux.sock",
            close=_close,
            is_alive=_dead,
            probe_alive=_dead,
        ),
    )

    await reaper._reap(pane)

    assert calls["teardown"] == ["conv_gone"]
    assert [event["conversation_id"] for event in calls["events"]] == ["conv_gone"]
