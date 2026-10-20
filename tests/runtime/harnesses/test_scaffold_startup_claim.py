"""A cold executor cannot continue on a missing or stale permission reply."""

import asyncio

import pytest

from omnigent.runtime.harnesses import _scaffold
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
