"""Runner peer branch of ``sys_session_send``: route → tool result mapping.

Covers the T4 peer branch in ``_send_to_existing_session``: flag off
keeps the child-only error; flag on POSTs the peer route and maps every
route answer to the tool contract; the reply poll returns replied /
terminal-state / timed-out outcomes. The server client is a scripted
``httpx.MockTransport`` fake — no server involved.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runner import tool_dispatch
from omnigent.runner.tool_dispatch import (
    _execute_subagent_tool,
    _send_to_existing_session,
)

_CALLER = "conv_caller"
_TARGET = "conv_peer"


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")


def _snapshot(*, parent: str | None) -> dict[str, Any]:
    return {
        "id": _TARGET,
        "title": "peer-title",
        "parent_session_id": parent,
        "labels": {},
        "agent_name": "peer-agent",
    }


def _receiver(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": _TARGET,
        "title": "Peer Title",
        "agent_name": "peer-agent",
        "status": "idle",
        "runner_online": True,
    }
    base.update(overrides)
    return base


def _send_response(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "disposition": "delivered",
        "reason": None,
        "peer_id": "peer_abc123",
        "ref": "ref_1",
        "receiver": _receiver(),
    }
    base.update(overrides)
    return base


def _record(state: str, **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "peer_id": "peer_abc123",
        "sender_session_id": _CALLER,
        "receiver_session_id": _TARGET,
        "correlation_id": None,
        "ref": "ref_1",
        "text": "hello peer",
        "state": state,
        "reason": None,
        "reply_peer_id": None,
        "replied_at": None,
        "created_at": 1,
        "updated_at": 1,
        "expires_at": 999,
    }
    base.update(overrides)
    return base


@pytest.mark.asyncio
async def test_flag_off_keeps_child_only_error() -> None:
    """Without the flag a non-child target still fails ``session_out_of_tree``."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{_TARGET}"
        return httpx.Response(200, json=_snapshot(parent="conv_other"))

    async with _client(handler) as client:
        out = json.loads(
            await _send_to_existing_session(
                _TARGET,
                "hi",
                server_client=client,
                conversation_id=_CALLER,
                peer_messaging_enabled=False,
            )
        )
    assert out["error"] == "session_out_of_tree"
    assert out["conversation_id"] == _TARGET


@pytest.mark.asyncio
async def test_flag_off_child_path_still_delivers() -> None:
    """Flag off changes nothing for a direct child (no peer POST issued)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/sessions/{_TARGET}":
            return httpx.Response(200, json=_snapshot(parent=_CALLER))
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": _TARGET})
        if request.method == "POST":
            assert request.url.path != f"/v1/sessions/{_TARGET}/peer-messages"
            return httpx.Response(200, json={"status": "accepted"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = json.loads(
            await _send_to_existing_session(
                _TARGET,
                "hi",
                server_client=client,
                conversation_id=_CALLER,
                peer_messaging_enabled=False,
            )
        )
    assert out["conversation_id"] == _TARGET
    assert out["status"] == "launching"


@pytest.mark.asyncio
async def test_snapshot_404_and_401_untouched_with_flag_on() -> None:
    """404/401 from the snapshot keep today's errors even with the flag on."""

    async def _run(status: int, expected: str) -> None:
        posted = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal posted
            if request.method == "POST":
                posted = True
            return httpx.Response(status, json={"error": "x"})

        async with _client(handler) as client:
            out = json.loads(
                await _send_to_existing_session(
                    _TARGET,
                    "hi",
                    server_client=client,
                    conversation_id=_CALLER,
                    peer_messaging_enabled=True,
                )
            )
        assert out == {"error": expected, "conversation_id": _TARGET}
        assert posted is False

    await _run(404, "session_not_found")
    await _run(401, "session_out_of_tree")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("disposition", "reason"),
    [
        pytest.param("delivered", None, id="delivered"),
        pytest.param("queued", None, id="queued"),
        pytest.param("pending", "offline", id="pending-offline"),
        pytest.param("held", None, id="held"),
        pytest.param("dropped", "duplicate", id="dropped-duplicate"),
        pytest.param("refused", "burst", id="refused-burst"),
        pytest.param("failed", "closed", id="failed-closed"),
    ],
)
async def test_disposition_mapping(disposition: str, reason: str | None) -> None:
    """Each route disposition maps to the peer tool result shape."""
    seen_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_snapshot(parent="conv_other"))
        assert request.url.path == f"/v1/sessions/{_TARGET}/peer-messages"
        seen_body.update(json.loads(request.content))
        return httpx.Response(200, json=_send_response(disposition=disposition, reason=reason))

    async with _client(handler) as client:
        out = json.loads(
            await _send_to_existing_session(
                _TARGET,
                "hello peer",
                server_client=client,
                conversation_id=_CALLER,
                peer_messaging_enabled=True,
                correlation_id="corr_1",
                wait_seconds=30,
            )
        )
    assert seen_body["sender_session_id"] == _CALLER
    assert seen_body["text"] == "hello peer"
    assert seen_body["correlation_id"] == "corr_1"
    assert seen_body["wait_seconds"] == 30
    assert out == {
        "peer": True,
        "peer_id": "peer_abc123",
        "conversation_id": _TARGET,
        "title": "Peer Title",
        "agent": "peer-agent",
        "disposition": disposition,
        **({"reason": reason} if reason is not None else {}),
        "ref": "ref_1",
        "receiver_state": {"status": "idle", "runner_online": True},
    }


@pytest.mark.asyncio
async def test_reply_to_passthrough() -> None:
    """A route ``reply_to`` (correlation-less reply link) rides the result."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_snapshot(parent="conv_other"))
        body = _send_response()
        body["reply_to"] = "peer_older"
        return httpx.Response(200, json=body)

    async with _client(handler) as client:
        out = json.loads(
            await _send_to_existing_session(
                _TARGET,
                "hi",
                server_client=client,
                conversation_id=_CALLER,
                peer_messaging_enabled=True,
            )
        )
    assert out["reply_to"] == "peer_older"


@pytest.mark.asyncio
async def test_feature_disabled_maps_to_peer_messaging_disabled() -> None:
    """``refused(feature_disabled)`` becomes the ``peer_messaging_disabled`` error."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_snapshot(parent="conv_other"))
        return httpx.Response(
            200,
            json={
                "disposition": "refused",
                "reason": "feature_disabled",
                "peer_id": None,
                "ref": "",
                "receiver": {"id": _TARGET},
            },
        )

    async with _client(handler) as client:
        out = json.loads(
            await _send_to_existing_session(
                _TARGET,
                "hi",
                server_client=client,
                conversation_id=_CALLER,
                peer_messaging_enabled=True,
            )
        )
    assert out["error"] == "peer_messaging_disabled"
    assert out["conversation_id"] == _TARGET


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 404, 500])
async def test_route_http_errors_map(status: int) -> None:
    """401 → ``peer_unauthorized``; 404 → ``session_not_found``; else failed."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=_snapshot(parent="conv_other"))
        return httpx.Response(status, json={"detail": "boom"})

    async with _client(handler) as client:
        out = json.loads(
            await _send_to_existing_session(
                _TARGET,
                "hi",
                server_client=client,
                conversation_id=_CALLER,
                peer_messaging_enabled=True,
            )
        )
    if status == 401:
        assert out["error"] == "peer_unauthorized"
    elif status == 404:
        assert out["error"] == "session_not_found"
    else:
        assert out["error"] == "peer_send_failed"
        assert out["status"] == status


@pytest.mark.asyncio
async def test_reply_poll_replied() -> None:
    """A set ``replied_at`` fetches the reply record's text."""
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        if request.method == "GET" and request.url.path == f"/v1/sessions/{_TARGET}":
            return httpx.Response(200, json=_snapshot(parent="conv_other"))
        if request.method == "POST":
            return httpx.Response(200, json=_send_response(disposition="queued"))
        if request.url.path == "/v1/peer-messages/peer_abc123":
            polls += 1
            if polls == 1:
                return httpx.Response(200, json=_record("queued"))
            return httpx.Response(
                200,
                json=_record("delivered", reply_peer_id="peer_reply1", replied_at=2),
            )
        if request.url.path == "/v1/peer-messages/peer_reply1":
            return httpx.Response(200, json=_record("delivered", text="got it"))
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = json.loads(
            await _send_to_existing_session(
                _TARGET,
                "hi",
                server_client=client,
                conversation_id=_CALLER,
                peer_messaging_enabled=True,
                wait_for_reply_seconds=30,
            )
        )
    assert out["reply"] == {"peer_id": "peer_reply1", "text": "got it"}
    assert polls >= 2


@pytest.mark.asyncio
async def test_reply_poll_terminal_state() -> None:
    """A ``failed`` record ends the wait with that state (no timeout)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/sessions/{_TARGET}":
            return httpx.Response(200, json=_snapshot(parent="conv_other"))
        if request.method == "POST":
            return httpx.Response(200, json=_send_response(disposition="pending"))
        return httpx.Response(200, json=_record("failed", reason="closed"))

    async with _client(handler) as client:
        out = json.loads(
            await _send_to_existing_session(
                _TARGET,
                "hi",
                server_client=client,
                conversation_id=_CALLER,
                peer_messaging_enabled=True,
                wait_for_reply_seconds=30,
            )
        )
    assert out["reply"] == {"state": "failed", "reason": "closed"}


@pytest.mark.asyncio
async def test_reply_poll_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreplied record returns the ``timed_out`` marker at the budget."""
    monkeypatch.setattr(tool_dispatch, "_PEER_REPLY_POLL_S", 0.01)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/sessions/{_TARGET}":
            return httpx.Response(200, json=_snapshot(parent="conv_other"))
        if request.method == "POST":
            return httpx.Response(200, json=_send_response(disposition="held"))
        return httpx.Response(200, json=_record("held"))

    import time

    real_monotonic = time.monotonic
    calls = 0

    def _fast_clock() -> float:
        nonlocal calls
        calls += 1
        return real_monotonic() + (1000.0 if calls > 2 else 0.0)

    monkeypatch.setattr(time, "monotonic", _fast_clock)
    async with _client(handler) as client:
        out = json.loads(
            await _send_to_existing_session(
                _TARGET,
                "hi",
                server_client=client,
                conversation_id=_CALLER,
                peer_messaging_enabled=True,
                wait_for_reply_seconds=30,
            )
        )
    assert out["reply"] == {"reply": None, "reply_wait": "timed_out"}


@pytest.mark.asyncio
async def test_reply_poll_bounds_request_timeout_and_sleep_by_remaining_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F9: the per-request timeout shrinks to what's left of the wait budget.

    Neither a 30 s request read-timeout nor a 2 s poll sleep may outlive the
    caller's own wait budget — otherwise a short ``wait_for_reply_seconds``
    could still block far longer than requested. A handler that eats real
    wall-clock time on each poll drains the budget for real (poll interval
    shrunk so the between-poll sleep itself stays cheap), so later requests'
    timeouts are observably smaller than the first's.
    """
    import time

    monkeypatch.setattr(tool_dispatch, "_PEER_REPLY_POLL_S", 0.1)
    from omnigent.runner.tool_dispatch import _poll_peer_reply

    seen_timeouts: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_timeouts.append(request.extensions.get("timeout", {}).get("read"))
        time.sleep(0.3)
        return httpx.Response(200, json=_record("pending"))

    async with _client(handler) as client:
        out = await _poll_peer_reply(client, "peer_abc123", 2)
    assert out == {"reply": None, "reply_wait": "timed_out"}
    assert len(seen_timeouts) >= 2
    first, second = seen_timeouts[0], seen_timeouts[1]
    assert first is not None and second is not None
    assert first == pytest.approx(2.0, abs=0.05)
    assert second < first
    assert all(t is not None and t <= 2.0 for t in seen_timeouts)


@pytest.mark.asyncio
async def test_reply_detected_near_budget_fetch_timeout_returns_without_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X4: a reply detected at 0.9 s of a 1 s budget must not overshoot the
    deadline fetching its text. The remaining ~0.1 s budget threads into the
    fetch's own timeout; a fetch that times out inside it still reports the
    reply as detected (text unavailable) rather than block past the caller's
    deadline or lose the detected-reply signal entirely.
    """
    import time as _time

    from omnigent.runner.tool_dispatch import _poll_peer_reply

    real_monotonic = _time.monotonic
    start = real_monotonic()
    calls = 0

    def _fake_clock() -> float:
        nonlocal calls
        calls += 1
        # Calls 1-2 (the deadline calc, then the loop's remaining check
        # before the GET) read as "just started"; call 3+ (remaining for
        # the text fetch, after the reply is detected) reads as 0.9 s in —
        # simulating a reply detected near the end of the wait budget
        # without a real sleep.
        return start if calls <= 2 else start + 0.9

    monkeypatch.setattr(_time, "monotonic", _fake_clock)

    seen_fetch_timeout: list[float | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/peer-messages/peer_abc123":
            return httpx.Response(
                200,
                json=_record("delivered", reply_peer_id="peer_reply1", replied_at=2),
            )
        if request.url.path == "/v1/peer-messages/peer_reply1":
            seen_fetch_timeout.append(request.extensions.get("timeout", {}).get("read"))
            raise httpx.ReadTimeout("simulated slow fetch", request=request)
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = await _poll_peer_reply(client, "peer_abc123", 1)
    assert out == {"peer_id": "peer_reply1", "text": None}
    assert seen_fetch_timeout, "the reply-text fetch was never attempted"
    fetch_timeout = seen_fetch_timeout[0]
    assert fetch_timeout is not None and fetch_timeout == pytest.approx(0.1, abs=0.02)


@pytest.mark.asyncio
async def test_no_poll_when_wait_is_zero() -> None:
    """Default (no ``wait_for_reply_seconds``) returns at once with no ``reply``."""
    gets = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal gets
        if request.method == "GET":
            if request.url.path.startswith("/v1/peer-messages/"):
                gets += 1
            else:
                return httpx.Response(200, json=_snapshot(parent="conv_other"))
        return httpx.Response(200, json=_send_response())

    async with _client(handler) as client:
        out = json.loads(
            await _send_to_existing_session(
                _TARGET,
                "hi",
                server_client=client,
                conversation_id=_CALLER,
                peer_messaging_enabled=True,
            )
        )
    assert "reply" not in out
    assert gets == 0


@pytest.mark.asyncio
async def test_flag_read_from_session_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_execute_subagent_tool`` picks the flag from the runner session cache."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/sessions/{_TARGET}":
            return httpx.Response(200, json=_snapshot(parent="conv_other"))
        if request.method == "POST" and request.url.path.endswith("/peer-messages"):
            return httpx.Response(200, json=_send_response())
        if request.method == "GET" and request.url.path == f"/v1/sessions/{_CALLER}":
            return httpx.Response(200, json={"labels": {}})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    from omnigent.runner.app import _session_inboxes_ref as _inboxes_ref
    from omnigent.runner.app import _session_peer_messaging_enabled_ref as _flag_cache

    monkeypatch.setitem(_flag_cache, _CALLER, True)
    import asyncio as _asyncio

    monkeypatch.setitem(_inboxes_ref, _CALLER, _asyncio.Queue())
    async with _client(handler) as client:
        out = json.loads(
            await _execute_subagent_tool(
                {"session_id": _TARGET, "args": "hello peer"},
                server_client=client,
                conversation_id=_CALLER,
            )
        )
    assert out["peer"] is True
    assert out["disposition"] == "delivered"


@pytest.mark.asyncio
async def test_peer_opts_reach_peer_route_by_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """``correlation_id`` on a by-id send to a non-child rides the peer route."""
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/sessions/{_TARGET}":
            return httpx.Response(200, json=_snapshot(parent="conv_other"))
        if request.method == "POST" and request.url.path.endswith("/peer-messages"):
            posted.append(json.loads(request.content))
            return httpx.Response(200, json=_send_response())
        if request.method == "GET" and request.url.path == f"/v1/sessions/{_CALLER}":
            return httpx.Response(200, json={"labels": {}})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    import asyncio as _asyncio

    from omnigent.runner.app import _session_inboxes_ref as _inboxes_ref
    from omnigent.runner.app import _session_peer_messaging_enabled_ref as _flag_cache

    monkeypatch.setitem(_flag_cache, _CALLER, True)
    monkeypatch.setitem(_inboxes_ref, _CALLER, _asyncio.Queue())
    async with _client(handler) as client:
        out = json.loads(
            await _execute_subagent_tool(
                {"session_id": _TARGET, "args": "hello peer", "correlation_id": "corr_1"},
                server_client=client,
                conversation_id=_CALLER,
            )
        )
    assert out["peer"] is True
    assert posted and posted[0].get("correlation_id") == "corr_1"


@pytest.mark.asyncio
async def test_peer_opts_rejected_in_child_mode() -> None:
    """``correlation_id`` on a direct-child send fails with the peer-only error."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"/v1/sessions/{_TARGET}":
            return httpx.Response(200, json=_snapshot(parent=_CALLER))
        if request.method == "GET" and request.url.path == f"/v1/sessions/{_CALLER}":
            return httpx.Response(200, json={"labels": {}})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    import asyncio as _asyncio

    from omnigent.runner.app import _session_inboxes_ref as _inboxes_ref

    monkeypatch_queue = _asyncio.Queue()
    _inboxes_ref[_CALLER] = monkeypatch_queue  # type: ignore[assignment]
    try:
        async with _client(handler) as client:
            out = await _execute_subagent_tool(
                {
                    "session_id": _TARGET,
                    "args": "hi",
                    "correlation_id": "corr_1",
                },
                server_client=client,
                conversation_id=_CALLER,
            )
    finally:
        _inboxes_ref.pop(_CALLER, None)
    assert "appl" in out and "peer" in out


@pytest.mark.asyncio
async def test_peer_opts_rejected_in_named_mode() -> None:
    """``wait_seconds`` on a named send fails with the peer-only error."""
    import asyncio as _asyncio

    from omnigent.runner.app import _session_inboxes_ref as _inboxes_ref

    _inboxes_ref[_CALLER] = _asyncio.Queue()  # type: ignore[assignment]
    try:

        async def _handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("no HTTP should be issued")

        async with _client(_handler) as client:
            from omnigent.spec.types import AgentSpec

            out = await _execute_subagent_tool(
                {
                    "agent": "researcher",
                    "title": "t",
                    "args": "hi",
                    "wait_seconds": 10,
                },
                server_client=client,
                conversation_id=_CALLER,
                agent_spec=AgentSpec(
                    spec_version=1,
                    name="main",
                    sub_agents=[],
                ),
            )
    finally:
        _inboxes_ref.pop(_CALLER, None)
    assert "peer" in out
