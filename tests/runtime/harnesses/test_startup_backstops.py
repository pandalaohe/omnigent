"""Delayed readiness survives the old cutoff while hangs and cancellation remain bounded."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.runtime.harnesses import _scaffold, process_manager
from omnigent.server.schemas import CreateResponseRequest
from omnigent.startup_protocol import StartupAttemptRef


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["ready", "hang", "stop"])
async def test_spawn_backstop_preserves_slow_live_process(monkeypatch, outcome):
    now = 0.0
    process = SimpleNamespace(returncode=None, kill=Mock(), wait=AsyncMock())

    async def sleep(delay):
        nonlocal now
        now += delay
        if outcome == "stop":
            raise asyncio.CancelledError

    async def can_connect():
        return outcome == "ready" and now >= 45

    endpoint = SimpleNamespace(can_connect=can_connect, harden=Mock())
    monkeypatch.setattr(
        process_manager,
        "asyncio",
        SimpleNamespace(get_running_loop=lambda: SimpleNamespace(time=lambda: now), sleep=sleep),
    )
    if outcome == "ready":
        await process_manager._wait_for_bind(process, endpoint, "test", "session")
        assert 30 < now < 180
        endpoint.harden.assert_called_once()
        process.kill.assert_not_called()
    elif outcome == "hang":
        with pytest.raises(RuntimeError, match=r"did not bind.*180s"):
            await process_manager._wait_for_bind(process, endpoint, "test", "session")
        assert 180 <= now < 181
        process.kill.assert_called_once()
    else:
        with pytest.raises(asyncio.CancelledError):
            await process_manager._wait_for_bind(process, endpoint, "test", "session")
        assert now < 1


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["startup", "input"])
async def test_claim_wait_allows_slow_permission_reply(monkeypatch, kind):
    budgets = []

    async def delayed_reply(future, *, timeout):
        budgets.append(timeout)
        # A server receipt arriving after the old 10+5 second cutoff is valid.
        if timeout < 45:
            raise TimeoutError
        future.cancel()
        if kind == "startup":
            return True
        return SimpleNamespace(state="accepted")

    monkeypatch.setattr(
        _scaffold,
        "asyncio",
        SimpleNamespace(
            get_running_loop=asyncio.get_running_loop,
            wait_for=delayed_reply,
            Queue=asyncio.Queue,
            Event=asyncio.Event,
        ),
    )
    ctx = _scaffold.TurnContext("response", asyncio.Queue(), asyncio.Event(), "session")
    if kind == "startup":
        attempt = StartupAttemptRef(
            startup_id="a" * 32,
            attempt_id="b" * 32,
            runtime_id="runtime",
            host_generation="generation",
            input_id=None,
        )
        assert await ctx.claim_startup(attempt, "executor_prepare")
    else:
        request = CreateResponseRequest(model="agent", input="work", input_id="c" * 32)
        assert await ctx.claim_input_execution(request)
    assert 120 <= budgets[0] <= 125
    assert not ctx._pending_startup_claims
    assert not ctx._pending_input_claims
