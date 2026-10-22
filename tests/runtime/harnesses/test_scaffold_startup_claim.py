"""A cold executor cannot continue on a missing or stale permission reply."""

import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner.executor import MockExecutor, TextChunk
from omnigent.runtime.harnesses import _executor_adapter, _scaffold
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.server.schemas import CreateResponseRequest
from omnigent.startup_evidence import StartupPermissionUnavailable
from omnigent.startup_protocol import StartupAttemptRef


@pytest.mark.asyncio
@pytest.mark.parametrize("granted", [False, True])
async def test_startup_claim_result_is_bound_to_the_pending_request(granted):
    queue = asyncio.Queue()
    ctx = _scaffold.TurnContext("response", queue, asyncio.Event(), "session")
    attempt = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="runtime",
        host_generation="generation",
    )
    waiting = asyncio.create_task(ctx.claim_startup(attempt, "executor_prepare"))
    await asyncio.sleep(0)
    event = queue.get_nowait()
    assert event.type == "startup.claim.requested" and event.attempt == attempt
    assert not ctx._has_progress
    assert not ctx._complete_startup_claim("stale", True)
    assert not waiting.done()
    assert ctx._complete_startup_claim(event.claim_id, granted)
    assert not ctx._complete_startup_claim(event.claim_id, not granted)
    assert await waiting is granted
    assert not ctx._complete_startup_claim(event.claim_id, True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_startup_claim_timeout_or_interrupt_never_grants_permission(monkeypatch, cancel):
    monkeypatch.setattr(_scaffold, "_STARTUP_CLAIM_TIMEOUT_S", 0.001)
    queue = asyncio.Queue()
    ctx = _scaffold.TurnContext("response", queue, asyncio.Event(), "session")
    attempt = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id=None,
        runtime_id="runtime",
        host_generation="generation",
    )
    waiting = asyncio.create_task(ctx.claim_startup(attempt, "executor_prepare"))
    await asyncio.sleep(0)
    event = queue.get_nowait()
    if cancel:
        ctx.cancelled.set()
        ctx._cancel_pending()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    else:
        assert not await waiting
    assert not ctx._complete_startup_claim(event.claim_id, True)


@pytest.mark.asyncio
async def test_unsupported_factory_is_refused_before_construction():
    factory = Mock(side_effect=AssertionError("unsupported factory must stay cold"))
    adapter = ExecutorAdapter(factory)
    ctx = _scaffold.TurnContext("response", asyncio.Queue(), asyncio.Event(), "session")
    with pytest.raises(RuntimeError, match="startup_proof_unavailable"):
        await adapter.run_turn(
            CreateResponseRequest(model="agent", input="work", startup_required=True), ctx
        )
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["codex", "claude-sdk"])
@pytest.mark.parametrize("granted", [False, True])
async def test_supported_factory_requires_its_own_cold_permission(source, granted):
    factory = Mock(side_effect=RuntimeError("factory reached"))
    adapter = ExecutorAdapter(factory, startup_source=source)
    ctx = _scaffold.TurnContext("response", asyncio.Queue(), asyncio.Event(), "session")
    ctx.claim_startup = AsyncMock(return_value=granted)
    attempt = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="runtime",
        host_generation="generation",
    )
    request = CreateResponseRequest(model="agent", input="work", startup_attempt=attempt)
    expected = "factory reached" if granted else "Startup permission unavailable"
    with pytest.raises(RuntimeError, match=expected):
        await adapter.run_turn(request, ctx)
    ctx.claim_startup.assert_awaited_once_with(attempt, "executor_construct")
    assert factory.call_count == int(granted)


@pytest.mark.asyncio
async def test_supported_factory_without_current_attempt_stays_cold():
    factory = Mock(side_effect=AssertionError("no current attempt"))
    adapter = ExecutorAdapter(factory, startup_source="claude-sdk")
    ctx = _scaffold.TurnContext("response", asyncio.Queue(), asyncio.Event(), "session")
    ctx.claim_startup = AsyncMock(return_value=True)
    with pytest.raises(RuntimeError, match="Startup permission unavailable"):
        await adapter.run_turn(
            CreateResponseRequest(model="agent", input="work", startup_required=True), ctx
        )
    factory.assert_not_called()
    ctx.claim_startup.assert_not_awaited()


@pytest.mark.asyncio
async def test_refused_cold_boundary_preserves_executor_and_finishes_trace(monkeypatch):
    class DeniedExecutor(MockExecutor):
        async def run_turn(self, *args, **kwargs):
            if False:
                yield TextChunk(text="")
            raise StartupPermissionUnavailable("Startup permission unavailable")

    executor = DeniedExecutor()
    adapter = ExecutorAdapter(lambda: executor)
    ctx = _scaffold.TurnContext("response", asyncio.Queue(), asyncio.Event(), "session")
    trace, span = Mock(), Mock()
    trace.start_agent_span.return_value = span
    monkeypatch.setattr(_executor_adapter, "is_tracing_enabled", lambda: True)
    monkeypatch.setattr(_executor_adapter, "TracingContext", lambda **kwargs: trace)
    with pytest.raises(StartupPermissionUnavailable):
        await adapter.run_turn(CreateResponseRequest(model="agent", input="work"), ctx)
    assert adapter._executor is executor and adapter._abandoned_executor_cleanup is None
    trace.end_agent_span.assert_called_once_with(span, response=None, error="unhandled exception")
