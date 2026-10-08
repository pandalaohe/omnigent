"""Tests for runner-local timer tool dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool


class _TimerPostRecorder:
    """
    ``httpx.MockTransport`` handler that records timer wake POSTs.

    The ``posts`` attribute stores dictionaries with ``url``,
    ``method``, ``json``, and ``headers`` keys, e.g.
    ``{"url": "/v1/sessions/...", "method": "POST"}``.
    """

    def __init__(self) -> None:
        """Initialize an empty call log."""
        self.posts: list[dict[str, Any]] = []
        self.post_seen = asyncio.Event()

    async def __call__(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        """
        Record a timer wake request and return an accepted response.

        :param request: HTTPX request, e.g. POST to
            ``"/v1/sessions/conv_x/events"``.
        :returns: HTTP 202 response matching the session event endpoint.
        """
        self.posts.append(
            {
                "url": request.url.path,
                "method": request.method,
                "json": json.loads(request.content),
                "headers": dict(request.headers),
            }
        )
        self.post_seen.set()
        return httpx.Response(202, json={"queued": True})


@pytest.mark.asyncio
async def test_timer_firing_posts_hidden_meta_message() -> None:
    """
    Timer firings wake the agent but stay hidden from user-facing UI.

    The timer POST must remain a ``role="user"`` message so the
    sessions event path starts or steers the next turn. Marking it
    ``is_meta=True`` is what makes existing web/TUI transcript
    rendering skip the synthetic ``[System: timer ... fired]`` row.
    """
    recorder = _TimerPostRecorder()
    transport = httpx.MockTransport(recorder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        output = await execute_tool(
            tool_name="sys_timer_set",
            arguments=json.dumps({"seconds": 0, "note": "check build"}),
            conversation_id="conv_parent",
            server_client=server_client,
        )

        result = json.loads(output)
        assert result["status"] == "scheduled"
        assert isinstance(result["timer_id"], str)

        await asyncio.wait_for(recorder.post_seen.wait(), timeout=1.0)

    # A non-repeating timer should produce exactly one wake POST:
    # zero means the firing never reached AP, more than one means it
    # accidentally behaved like a repeating timer.
    assert len(recorder.posts) == 1
    post = recorder.posts[0]
    assert post["method"] == "POST"
    assert post["url"] == "/v1/sessions/conv_parent/events"
    payload = post["json"]
    assert payload == {
        "type": "message",
        "data": {
            "role": "user",
            "is_meta": True,
            "content": [
                {
                    "type": "input_text",
                    "text": f"[System: timer {result['timer_id']} fired]\nnote: 'check build'",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_timer_set_rejects_invalid_args_via_shared_validator() -> None:
    """
    The runner dispatch path validates through the shared
    ``validate_timer_set_args`` helper, so a bad ``seconds`` returns the
    same message the in-process builtin surfaces and starts no timer
    task (no wake POST is ever made).
    """
    recorder = _TimerPostRecorder()
    transport = httpx.MockTransport(recorder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        output = await execute_tool(
            tool_name="sys_timer_set",
            arguments=json.dumps({"seconds": -1}),
            conversation_id="conv_parent",
            server_client=server_client,
        )

    assert json.loads(output) == {"error": "seconds must be non-negative"}
    assert recorder.posts == []


@pytest.mark.asyncio
async def test_timer_set_rejects_zero_delay_repeating() -> None:
    """
    ``repeat=true`` with ``seconds=0`` is rejected with the same error
    the builtin validator returns, and no wake POST is started.

    Without this guard the firing loop would busy-loop ``sleep(0)`` and
    hammer the sessions endpoint forever.
    """
    recorder = _TimerPostRecorder()
    transport = httpx.MockTransport(recorder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        output = await execute_tool(
            tool_name="sys_timer_set",
            arguments=json.dumps({"seconds": 0, "repeat": True}),
            conversation_id="conv_parent",
            server_client=server_client,
        )

    assert json.loads(output) == {"error": "seconds must be > 0 when repeat is true"}
    assert recorder.posts == []


@pytest.mark.asyncio
async def test_timer_delivery_logs_http_error_status(caplog: pytest.LogCaptureFixture) -> None:
    """
    HTTP 4xx on the wake POST is treated as delivery failure.

    ``httpx`` does not raise on error status codes by default; without an
    explicit check the timer would silently ignore a rejected firing. A
    4xx is a definitive refusal, so it is logged without a retry (5xx and
    connection failures retry instead — see the retry tests below).
    """

    class _ErrorResponder:
        """Mock transport that records the POST and returns HTTP 400."""

        def __init__(self) -> None:
            self.posts: list[dict[str, Any]] = []
            self.post_seen = asyncio.Event()

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            self.posts.append({"url": request.url.path, "method": request.method})
            self.post_seen.set()
            return httpx.Response(400, text="bad request")

    responder = _ErrorResponder()
    transport = httpx.MockTransport(responder)

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.tool_dispatch"):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://server"
        ) as server_client:
            output = await execute_tool(
                tool_name="sys_timer_set",
                arguments=json.dumps({"seconds": 0, "note": "boom"}),
                conversation_id="conv_parent",
                server_client=server_client,
            )
            result = json.loads(output)
            assert result["status"] == "scheduled"
            await asyncio.wait_for(responder.post_seen.wait(), timeout=1.0)
            # Let the one-shot loop finish after the failed POST.
            await asyncio.sleep(0.05)

    # The settings read (GET collab-settings) also hits the transport.
    assert len([post for post in responder.posts if post["method"] == "POST"]) == 1
    assert any(
        "firing persist failed" in record.getMessage()
        and result["timer_id"] in record.getMessage()
        for record in caplog.records
    )


class _RetryResponder:
    """
    Mock transport that returns scripted outcomes for fire POSTs.

    Each POST consumes the next entry of ``outcomes``; once the list is
    exhausted the last entry repeats. Non-POST reads (collab settings,
    archive snapshot) 404, which both flows read as their defaults.
    """

    def __init__(self, outcomes: list[httpx.Response | Exception]) -> None:
        """Store the scripted POST outcomes."""
        self.outcomes = outcomes
        self.posts = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/events"):
            self.posts += 1
            outcome = self.outcomes[min(self.posts - 1, len(self.outcomes) - 1)]
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return httpx.Response(404)


async def _run_timer_loop(
    monkeypatch: pytest.MonkeyPatch,
    responder: Callable[[httpx.Request], httpx.Response],
    *,
    seconds: float = 0.0,
) -> list[float]:
    """Run one one-shot timer loop, recording instead of awaiting sleeps."""
    from omnigent.runner import tool_dispatch

    sleeps: list[float] = []

    async def _record_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(tool_dispatch.asyncio, "sleep", _record_sleep)
    transport = httpx.MockTransport(responder)
    async with httpx.AsyncClient(transport=transport, base_url="http://server") as client:
        await tool_dispatch._timer_loop(
            timer_id="timer_retry",
            conversation_id="conv_timer",
            seconds=seconds,
            repeat=False,
            note=None,
            server_client=client,
        )
    return sleeps


@pytest.mark.asyncio
async def test_timer_firing_retries_connect_error_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refused connection retries once after the first delay, then stops."""
    responder = _RetryResponder(
        [httpx.ConnectError("connection refused"), httpx.Response(202, json={"queued": True})]
    )

    sleeps = await _run_timer_loop(monkeypatch, responder)

    assert responder.posts == 2
    # First entry is the timer delay; the rest are the retry waits.
    assert sleeps[1:] == [2.0]


@pytest.mark.asyncio
async def test_timer_firing_retries_503_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 503 certainly did not dispatch the firing, so it retries."""
    responder = _RetryResponder([httpx.Response(503), httpx.Response(202, json={"queued": True})])

    sleeps = await _run_timer_loop(monkeypatch, responder)

    assert responder.posts == 2
    assert sleeps[1:] == [2.0]


@pytest.mark.asyncio
async def test_timer_firing_does_not_retry_remote_protocol_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A lost response may mean the server accepted: one POST, logged, no retry."""
    responder = _RetryResponder(
        [
            httpx.RemoteProtocolError("server disconnected"),
            httpx.Response(202, json={"queued": True}),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.tool_dispatch"):
        sleeps = await _run_timer_loop(monkeypatch, responder)

    assert responder.posts == 1
    assert sleeps[1:] == []
    assert any("firing persist failed" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_timer_firing_does_not_retry_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 500 may have been raised after the firing landed: no retry."""
    responder = _RetryResponder([httpx.Response(500), httpx.Response(202, json={"queued": True})])

    sleeps = await _run_timer_loop(monkeypatch, responder)

    assert responder.posts == 1
    assert sleeps[1:] == []


@pytest.mark.asyncio
async def test_timer_firing_does_not_retry_504(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 504 gateway timeout may be a lost response: no retry."""
    responder = _RetryResponder([httpx.Response(504), httpx.Response(202, json={"queued": True})])

    sleeps = await _run_timer_loop(monkeypatch, responder)

    assert responder.posts == 1
    assert sleeps[1:] == []


@pytest.mark.asyncio
async def test_timer_firing_does_not_retry_4xx(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 4xx is a definitive refusal: one POST, logged, no retry."""
    responder = _RetryResponder([httpx.Response(400), httpx.Response(202, json={"queued": True})])

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.tool_dispatch"):
        sleeps = await _run_timer_loop(monkeypatch, responder)

    assert responder.posts == 1
    assert sleeps[1:] == []
    assert any("firing persist failed" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_timer_firing_does_not_retry_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read timeout may mean the server accepted: no retry, no double fire."""
    responder = _RetryResponder(
        [httpx.ReadTimeout("read timed out"), httpx.Response(202, json={"queued": True})]
    )

    sleeps = await _run_timer_loop(monkeypatch, responder)

    assert responder.posts == 1
    assert sleeps[1:] == []


@pytest.mark.asyncio
async def test_timer_firing_exhausts_retry_delays_then_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Four retries follow the module delays, then the failure is logged."""
    from omnigent.runner.tool_dispatch import _TIMER_FIRE_RETRY_DELAYS_S

    responder = _RetryResponder([httpx.Response(503)])

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.tool_dispatch"):
        sleeps = await _run_timer_loop(monkeypatch, responder)

    assert responder.posts == len(_TIMER_FIRE_RETRY_DELAYS_S) + 1
    assert sleeps[1:] == list(_TIMER_FIRE_RETRY_DELAYS_S)
    assert any("firing persist failed" in record.getMessage() for record in caplog.records)


class _SessionArchiveResponder:
    """Mock transport: settings read plus a session snapshot and fire POSTs."""

    def __init__(self, *, archived: bool) -> None:
        self.archived = archived
        self.posts = 0
        self.session_gets = 0
        self.get_seen = asyncio.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/collab-settings"):
            return httpx.Response(200, json={"enabled": True, "flow_timer_enabled": True})
        if request.method == "GET" and path == "/v1/sessions/conv_timer":
            self.session_gets += 1
            self.get_seen.set()
            return httpx.Response(200, json={"id": "conv_timer", "archived": self.archived})
        if request.method == "POST" and path.endswith("/events"):
            self.posts += 1
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(404)


@pytest.mark.asyncio
async def test_repeating_timer_stops_before_posting_when_self_archived() -> None:
    """A self-archived snapshot ends the repeating timer without a fire POST."""
    from omnigent.runner import app as runner_app

    responder = _SessionArchiveResponder(archived=True)
    transport = httpx.MockTransport(responder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        output = await execute_tool(
            tool_name="sys_timer_set",
            arguments=json.dumps({"seconds": 0.05, "repeat": True}),
            conversation_id="conv_timer",
            server_client=server_client,
        )
        result = json.loads(output)
        assert result["status"] == "scheduled"
        await asyncio.wait_for(responder.get_seen.wait(), timeout=1.0)
        for _ in range(100):
            if not runner_app._session_timers.get("conv_timer"):
                break
            await asyncio.sleep(0.02)

    assert responder.posts == 0
    assert responder.session_gets == 1
    assert runner_app._session_timers.get("conv_timer", {}) == {}


@pytest.mark.asyncio
async def test_repeating_timer_keeps_its_schedule_when_only_an_ancestor_archived() -> None:
    """An ancestor-only archive leaves the child's own schedule in place."""
    from omnigent.runner import app as runner_app

    responder = _SessionArchiveResponder(archived=False)
    transport = httpx.MockTransport(responder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        output = await execute_tool(
            tool_name="sys_timer_set",
            arguments=json.dumps({"seconds": 0.05, "repeat": True}),
            conversation_id="conv_timer",
            server_client=server_client,
        )
        result = json.loads(output)
        for _ in range(200):
            if responder.posts >= 2:
                break
            await asyncio.sleep(0.02)
        running = result["timer_id"] in runner_app._session_timers.get("conv_timer", {})
        runner_app.cancel_timer("conv_timer", result["timer_id"])

    assert responder.posts >= 2
    assert responder.session_gets >= 2
    assert running is True
