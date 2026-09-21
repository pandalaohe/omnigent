"""Precise terminal-close outcomes and the native-pane reaper's use of them.

``close_terminal`` collapses every failure to ``False``, so the reaper once
read an already-absent pane as a generation change: it raised and skipped the
app-server teardown plus the terminal-deleted event. These tests pin the
disambiguated contract.
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


def _fake_instance(tmp_path: Path) -> Any:
    """A terminal instance the real registry can close without tmux."""

    async def _close() -> None:
        return None

    return SimpleNamespace(
        running=True,
        socket_path=tmp_path / "tmux.sock",
        close=_close,
        # Real instances carry one; the close path stamps it into telemetry.
        diagnostic_id="term_diag_fake",
    )


async def test_detailed_close_reports_closed_and_wrapper_stays_true(
    tmp_path: Path,
) -> None:
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    instance = _fake_instance(tmp_path)
    terminal_registry._by_conversation.setdefault("conv_a", {})[("codex", "main")] = instance
    terminal_id = terminal_resource_id("codex", "main")

    assert (
        await registry.close_terminal_detailed("conv_a", terminal_id, expected_instance=instance)
        == "closed"
    )

    terminal_registry._by_conversation.setdefault("conv_a", {})[("codex", "main")] = instance
    assert await registry.close_terminal("conv_a", terminal_id) is True


async def test_detailed_close_reports_generation_changed() -> None:
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    current = SimpleNamespace(running=True)
    terminal_registry._by_conversation.setdefault("conv_a", {})[("codex", "main")] = current
    terminal_id = terminal_resource_id("codex", "main")
    stale = object()

    assert (
        await registry.close_terminal_detailed(
            "conv_a",
            terminal_id,
            expected_instance=stale,  # type: ignore[arg-type]
        )
        == "generation_changed"
    )
    assert (
        await registry.close_terminal(
            "conv_a",
            terminal_id,
            expected_instance=stale,  # type: ignore[arg-type]
        )
        is False
    )
    # The newer generation is left alone.
    assert terminal_registry._by_conversation["conv_a"][("codex", "main")] is current


async def test_detailed_close_reports_absent() -> None:
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    terminal_id = terminal_resource_id("codex", "main")

    assert (
        await registry.close_terminal_detailed(
            "conv_gone", terminal_id, expected_instance=object()
        )
        == "absent"
    )
    assert await registry.close_terminal("conv_gone", terminal_id) is False


async def test_detailed_close_reports_no_registry() -> None:
    registry = SessionResourceRegistry(terminal_registry=None)

    assert (
        await registry.close_terminal_detailed(
            "conv_a", "terminal_codex_main", expected_instance=object()
        )
        == "no_registry"
    )
    assert await registry.close_terminal("conv_a", "terminal_codex_main") is False


async def test_detailed_close_reports_close_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal_registry = TerminalRegistry()
    registry = SessionResourceRegistry(terminal_registry=terminal_registry)
    current = SimpleNamespace(running=True, diagnostic_id="term_diag_fake")
    terminal_registry._by_conversation.setdefault("conv_a", {})[("codex", "main")] = current
    terminal_id = terminal_resource_id("codex", "main")

    async def _refuse(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(terminal_registry, "close", _refuse)

    assert (
        await registry.close_terminal_detailed(
            "conv_a",
            terminal_id,
            expected_instance=current,  # type: ignore[arg-type]
        )
        == "close_failed"
    )
    assert await registry.close_terminal("conv_a", terminal_id) is False


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


async def test_reaping_absent_pane_retires_session_pieces_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
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
        ),
    )

    await reaper._reap(pane)

    assert calls["teardown"] == ["conv_gone"]
    assert [event["conversation_id"] for event in calls["events"]] == ["conv_gone"]


async def test_reaping_superseded_pane_keeps_successor_signal_and_pieces(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: dict[str, list[Any]] = {"teardown": [], "events": []}
    app, terminal_registry = _build_app_with_reaper(monkeypatch, calls)
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    current = SimpleNamespace(running=True, socket_path=tmp_path / "tmux.sock")
    terminal_registry._by_conversation.setdefault("conv_live", {})[("codex", "main")] = current
    pane = PaneRef(
        "conv_live",
        terminal_resource_id("codex", "main"),
        "codex",
        tmp_path / "tmux.sock",
        instance=object(),
    )

    with pytest.raises(
        RuntimeError, match="native pane generation changed before retention release"
    ):
        await reaper._reap(pane)

    assert calls["teardown"] == []
    assert calls["events"] == []
    assert terminal_registry._by_conversation["conv_live"][("codex", "main")] is current
