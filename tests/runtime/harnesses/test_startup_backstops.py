"""Delayed readiness survives the old cutoff while hangs and cancellation remain bounded."""

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.entities.subagent_retry import InputExecutionReceipt
from omnigent.runner import startup_admission
from omnigent.runtime.harnesses import _scaffold, process_manager
from omnigent.server.schemas import CreateResponseRequest
from omnigent.startup_protocol import StartupAttemptRef


@pytest.mark.asyncio
@pytest.mark.parametrize("matched", [True, False])
async def test_input_receipt_ack_distinguishes_live_and_retired_claim(matched):
    app = _scaffold.HarnessApp()
    asgi = app.build()
    asgi.state.conversation_id = "session"
    ctx = _scaffold.TurnContext("response", asyncio.Queue(), asyncio.Event(), "session")
    app._in_flight["response"] = ctx
    future = asyncio.get_running_loop().create_future()
    if matched:
        ctx._pending_input_claims["claim"] = future
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=asgi), base_url="http://harness"
    ) as client:
        response = await client.post(
            "/v1/sessions/session/events",
            json={
                "type": "input_execution_result",
                "claim_id": "claim",
                "result": {"state": "accepted", "reason": "accepted"},
            },
        )
    assert response.status_code == 204
    assert response.headers["X-Input-Claim-Matched"] == str(matched).lower()
    assert future.done() is matched


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
@pytest.mark.parametrize("outcome", ["ready", "hang", "stop"])
async def test_startup_claim_allows_both_control_plane_steps(monkeypatch, outcome):
    budgets = []

    async def scaled_wait(future, *, timeout):
        budgets.append(timeout)
        return await asyncio.wait_for(future, timeout=timeout / 100)

    monkeypatch.setattr(
        _scaffold,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "wait_for": scaled_wait}),
    )
    inner_budgets = []

    def scaled_timeout(seconds):
        inner_budgets.append(seconds)
        return asyncio.timeout(seconds / 100)

    monkeypatch.setattr(startup_admission, "asyncio", SimpleNamespace(timeout=scaled_timeout))
    ctx = _scaffold.TurnContext("response", asyncio.Queue(), asyncio.Event(), "session")
    attempt = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        runtime_id="runtime",
        host_generation="generation",
        input_id=None,
        mode="startup_only",
    )
    started = asyncio.Event()
    paths = []
    errors = []

    async def handle(request):
        paths.append(request.url.path)
        started.set()
        if outcome != "ready":
            await asyncio.Future()
        # Each control-plane step takes 20 virtual seconds, beyond the old limits.
        await asyncio.sleep(0.2)
        body = (
            {"exempt": False}
            if request.url.path.endswith("recreate-exemption")
            else {"granted": True}
        )
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://ap"
    ) as client:
        admission = startup_admission.StartupAdmission(
            client, root_session_id="session", root_attempt=attempt
        )
        waiting = asyncio.create_task(ctx.claim_startup(attempt, "executor_prepare"))
        event = await ctx._event_queue.get()

        async def answer():
            try:
                await admission.native_admitter("session")()
            except RuntimeError as exc:
                errors.append(str(exc))
                ctx._complete_startup_claim(event.claim_id, False)
            else:
                ctx._complete_startup_claim(event.claim_id, True)

        replying = asyncio.create_task(answer())
        try:
            await started.wait()
            if outcome == "stop":
                ctx.cancelled.set()
                ctx._cancel_pending()
                with pytest.raises(asyncio.CancelledError):
                    await waiting
            else:
                assert await waiting is (outcome == "ready")
                assert len(paths) == 2
                assert sum(inner_budgets) < budgets[0]
                if outcome == "hang":
                    assert errors == ["Startup permission unavailable"]
        finally:
            waiting.cancel()
            replying.cancel()
            await asyncio.gather(waiting, replying, return_exceptions=True)
    assert not ctx._pending_startup_claims


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_deadline", [False, True])
@pytest.mark.parametrize("outcome", ["ready", "hang", "stop"])
async def test_input_claim_uses_remaining_deadline_and_stops(
    monkeypatch, explicit_deadline, outcome
):
    budgets = []

    async def scaled_wait(future, *, timeout):
        budgets.append(timeout)
        return await asyncio.wait_for(future, timeout=timeout / 100)

    monkeypatch.setattr(
        _scaffold, "asyncio", SimpleNamespace(**{**vars(asyncio), "wait_for": scaled_wait})
    )
    ctx = _scaffold.TurnContext("response", asyncio.Queue(), asyncio.Event(), "session")
    request = CreateResponseRequest(
        model="agent",
        input="work",
        input_id="c" * 32,
        input_admission_deadline=time.time() + 40 if explicit_deadline else None,
    )
    waiting = asyncio.create_task(ctx.claim_input_execution(request))
    event = await ctx._event_queue.get()
    if outcome == "stop":
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    elif outcome == "hang":
        with pytest.raises(RuntimeError, match="input_execution_acceptance_unknown") as caught:
            await waiting
        assert (
            _scaffold.HarnessApp()._build_error_detail(caught.value).code
            == "input_execution_acceptance_unknown"
        )
    else:
        # custom-lint: disable-next=sleep-then-assert -- inject a reply beyond the old deadline
        await asyncio.sleep(0.2)
        assert ctx._complete_input_claim(
            event.claim_id, InputExecutionReceipt(state="accepted", reason="accepted")
        )
        assert await waiting
    assert (40 < budgets[0] <= 45) if explicit_deadline else budgets == [125]
    assert not ctx._pending_input_claims
