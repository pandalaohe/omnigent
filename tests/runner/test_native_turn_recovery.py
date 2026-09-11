"""Native terminal recovery must settle the turn and remain interruptible."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import JSONResponse

from omnigent.harnesses.codex_native import bridge as codex_bridge
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import (
    _drain_session_event_queue,
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient, RunningFlagTerminalInstance


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["codex-native", "goose-native"])
@pytest.mark.parametrize("dead_pane", [False, True])
@pytest.mark.parametrize("stream", [False, True])
async def test_turn_recovers_native_pane_without_reacquiring_lifecycle_lock(
    harness: str, dead_pane: bool, stream: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = "a1b2c3d4e5f61234567890abcdef0123"
    terminal = harness.removesuffix("-native")
    monkeypatch.setattr(codex_bridge, "_BRIDGE_ROOT", tmp_path / "bridge")
    registry = TerminalRegistry()
    if dead_pane:
        pane = RunningFlagTerminalInstance(
            name=terminal,
            session_key="main",
            socket_path=tmp_path / "dead.sock",
            private_dir=tmp_path / "dead",
        )
        pane.running = False
        monkeypatch.setattr(pane, "close", AsyncMock())
        registry._by_conversation[sid] = {(terminal, "main"): pane}

    created = AsyncMock(return_value=JSONResponse({"id": f"terminal_{terminal}_main"}))
    monkeypatch.setattr("omnigent.runner.app._ensure_native_terminal", created)
    spec = AgentSpec(
        spec_version=1, name="test", executor=ExecutorSpec(config={"harness": harness})
    )
    finished = asyncio.Event()
    hc = _ScriptedHarnessClient(
        [
            _sse({"type": "response.created", "response": {"id": "resp_test"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_test"}}),
        ],
        stream_finished=finished,
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),
        spec_resolver=AsyncMock(return_value=spec),
        server_client=NullServerClient(),
        terminal_registry=registry,
    )
    async with _runner_client(app) as client:
        response = await asyncio.wait_for(
            client.post(
                f"/v1/sessions/{sid}/events",
                params={"stream": stream},
                json={
                    "type": "message",
                    "agent_id": "test-agent",
                    "harness_override": harness,
                    "content": [{"type": "input_text", "text": "continue"}],
                },
            ),
            timeout=2,
        )
        assert response.status_code == (200 if stream else 202)
        try:
            await asyncio.wait_for(finished.wait(), timeout=2)
        finally:
            for task in list(app.state.active_turns.values()):
                if isinstance(task, asyncio.Task):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
    assert created.await_count == 1
    assert len(hc.posted_bodies) == 1
    assert not app.state.cli_runtime_lifecycle.lock_for(sid).locked()


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("failure", ["rejected", "timeout"])
async def test_terminal_recovery_failure_settles_the_turn(
    stream: bool, failure: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    sid = "c1b2c3d4e5f61234567890abcdef0123"
    monkeypatch.setattr("omnigent.runner.app._NATIVE_TERMINAL_RECOVERY_TIMEOUT_S", 0.01)

    async def create_terminal(*args, **kwargs):
        if failure == "timeout":
            await asyncio.Event().wait()
        return JSONResponse({"error": "launch failed"}, status_code=503)

    monkeypatch.setattr("omnigent.runner.app._ensure_native_terminal", create_terminal)
    spec = AgentSpec(
        spec_version=1, name="test", executor=ExecutorSpec(config={"harness": "goose-native"})
    )
    hc = _ScriptedHarnessClient([])
    app = create_runner_app(
        process_manager=_FakeProcessManager(hc),
        spec_resolver=AsyncMock(return_value=spec),
        server_client=NullServerClient(),
        terminal_registry=TerminalRegistry(),
    )
    async with _runner_client(app) as client:
        response = await client.post(
            f"/v1/sessions/{sid}/events",
            params={"stream": stream},
            json={
                "type": "message",
                "agent_id": "test-agent",
                "harness_override": "goose-native",
                "content": [{"type": "input_text", "text": "continue"}],
            },
        )
        assert response.status_code == (503 if stream else 202)
        task = app.state.active_turns.get(sid)
        if task is not None:
            await asyncio.wait_for(task, timeout=2)
        events = _drain_session_event_queue(app.state.session_event_queues.get(sid))
        assert any(
            e.get("type") == "session.status" and e.get("status") == "failed" for e in events
        )
        assert sid not in app.state.active_turns
        assert not app.state.cli_runtime_lifecycle.lock_for(sid).locked()
        assert not hc.posted_bodies


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["interrupt", "stop_session"])
async def test_codex_startup_can_be_cancelled_before_native_turn_exists(
    event_type: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sid = "b1b2c3d4e5f61234567890abcdef0123"
    monkeypatch.setattr(codex_bridge, "_BRIDGE_ROOT", tmp_path / "bridge")
    entered, release = asyncio.Event(), asyncio.Event()

    async def create_terminal(*args, **kwargs):
        entered.set()
        await release.wait()
        return JSONResponse({"id": "terminal_codex_main"})

    monkeypatch.setattr("omnigent.runner.app._ensure_native_terminal", create_terminal)
    spec = AgentSpec(
        spec_version=1, name="test", executor=ExecutorSpec(config={"harness": "codex-native"})
    )
    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),
        spec_resolver=AsyncMock(return_value=spec),
        server_client=NullServerClient(),
        terminal_registry=TerminalRegistry(),
    )
    message = {
        "type": "message",
        "agent_id": "test-agent",
        "harness_override": "codex-native",
        "content": [{"type": "input_text", "text": "continue"}],
    }
    async with _runner_client(app) as client:
        try:
            assert (
                await client.post(f"/v1/sessions/{sid}/events", json=message)
            ).status_code == 202
            await asyncio.wait_for(entered.wait(), timeout=2)
            response = await client.post(f"/v1/sessions/{sid}/events", json={"type": event_type})
            assert response.status_code == 204
            assert sid not in app.state.active_turns
            assert not app.state.cli_runtime_lifecycle.lock_for(sid).locked()
            entered.clear()
            assert (
                await client.post(f"/v1/sessions/{sid}/events", json=message)
            ).status_code == 202
            await asyncio.wait_for(entered.wait(), timeout=2)
        finally:
            for task in list(app.state.active_turns.values()):
                if isinstance(task, asyncio.Task):
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
