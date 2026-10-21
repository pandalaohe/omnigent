"""Keep-warm fork isolation tests for the Codex forwarder event loop."""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd

_PARENT_THREAD = "thread_parent"


class _QueueEventsClient:
    """App-server client stub whose event stream is fed one message at a time."""

    def __init__(self) -> None:
        """Initialize an empty event queue and empty request records."""
        self.events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.responses: list[tuple[int | str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Capture one JSON-RPC request and answer the resume handshake.

        :param method: JSON-RPC method, e.g. ``"thread/resume"``.
        :param params: JSON-RPC params.
        :returns: Codex-shaped response payload.
        """
        self.requests.append((method, params))
        return {"result": {"thread": {"id": _PARENT_THREAD}}}

    async def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        """
        Capture one JSON-RPC response sent to the fake app-server.

        :param request_id: JSON-RPC request id.
        :param result: Result payload.
        :returns: None.
        """
        self.responses.append((request_id, result))

    async def close(self) -> None:
        """
        Accept the forwarder's teardown.

        :returns: None.
        """

    async def iter_events(self) -> Any:
        """
        Yield queued app-server messages forever.

        :returns: Async iterator of message envelopes.
        """
        while True:
            yield await self.events.get()


def _fork_event() -> dict[str, Any]:
    """The keep-warm fork's ``thread/started`` notification."""
    return {
        "method": "thread/started",
        "params": {
            "thread": {
                "id": "thread_fork",
                "ephemeral": True,
                "threadSource": "omnigent-keep-warm",
                "forkedFromId": _PARENT_THREAD,
            }
        },
    }


def _parent_turn_started() -> dict[str, Any]:
    """A parent-thread ``turn/started`` the forwarder must still handle."""
    return {
        "method": "turn/started",
        "params": {
            "threadId": _PARENT_THREAD,
            "turn": {"id": "turn_parent", "status": "inProgress", "items": []},
        },
    }


def _posted_types(posted: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Return each posted body's ``type``."""
    return [body.get("type") for _path, body in posted]


@pytest.mark.asyncio
async def test_legacy_conversation_id_fork_request_is_skipped(tmp_path: Path) -> None:
    """A legacy approval request keyed only by ``conversationId`` stays invisible."""
    client = _QueueEventsClient()
    posted: list[tuple[str, dict[str, Any]]] = []
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record one Omnigent request and accept it."""
        requests.append((request.method, request.url.path))
        if request.content:
            posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(202, json={"queued": False})

    task = asyncio.create_task(
        fwd.supervise_forwarder(
            base_url="http://127.0.0.1:9",
            headers={},
            session_id="conv_parent",
            bridge_dir=tmp_path,
            app_server_url="ws://127.0.0.1:9",
            thread_id=_PARENT_THREAD,
            client=client,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(handler),
        )
    )
    try:
        for event in (
            _fork_event(),
            {
                "id": 77,
                "method": "applyPatchApproval",
                "params": {"conversationId": "thread_fork", "callId": "call_1"},
            },
            _parent_turn_started(),
        ):
            client.events.put_nowait(event)
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            if "external_session_status" in _posted_types(posted):
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # The legacy fork request never parked an elicitation card: the parent's
    # status is the only conversation traffic.
    assert len(posted) == 1
    assert posted[0][1]["type"] == "external_session_status"
    assert client.responses == []


@pytest.mark.asyncio
async def test_keep_warm_fork_stream_is_invisible_and_a_parent_event_still_lands(
    tmp_path: Path,
) -> None:
    """Every fork message is skipped and a following parent event is handled."""
    client = _QueueEventsClient()
    posted: list[tuple[str, dict[str, Any]]] = []
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record one Omnigent request and accept it."""
        requests.append((request.method, request.url.path))
        if request.content:
            posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(202, json={"queued": False})

    task = asyncio.create_task(
        fwd.supervise_forwarder(
            base_url="http://127.0.0.1:9",
            headers={},
            session_id="conv_parent",
            bridge_dir=tmp_path,
            app_server_url="ws://127.0.0.1:9",
            thread_id=_PARENT_THREAD,
            client=client,  # type: ignore[arg-type]
            ap_transport=httpx.MockTransport(handler),
        )
    )
    try:
        # The full fork stream plus one parent event; the parent's status post
        # proves the loop consumed (and skipped) everything before it.
        for event in (
            _fork_event(),
            {
                "method": "item/started",
                "params": {
                    "threadId": "thread_fork",
                    "turnId": "turn_fork",
                    "item": {"id": "item_1", "type": "commandExecution"},
                },
            },
            {
                "id": 77,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "thread_fork",
                    "turnId": "turn_fork",
                    "itemId": "item_1",
                },
            },
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread_fork",
                    "tokenUsage": {"last": {"inputTokens": 10, "cachedInputTokens": 8}},
                },
            },
            {
                "method": "turn/completed",
                "params": {"threadId": "thread_fork", "turn": {"id": "turn_fork"}},
            },
            _parent_turn_started(),
        ):
            client.events.put_nowait(event)
        deadline = asyncio.get_running_loop().time() + 5.0
        while asyncio.get_running_loop().time() < deadline:
            if "external_session_status" in _posted_types(posted):
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    # The parent's status is the only conversation traffic and it goes to the
    # parent session: nothing from the fork surfaced (no owner message,
    # elicitation, usage or child session).
    assert len(posted) == 1
    path, body = posted[0]
    assert path == "/v1/sessions/conv_parent/events"
    assert body["type"] == "external_session_status"
    # The loop never answered the fork's approval request itself.
    assert client.responses == []
    # No side-chat child session was registered for the fork.
    assert ("POST", "/v1/sessions") not in requests
    # The fork events did not stop the forwarder's own subscription.
    assert any(method == "thread/resume" for method, _ in client.requests)


@pytest.mark.parametrize("method,status", [("turn/completed", "idle"), ("turn/failed", "failed")])
@pytest.mark.parametrize("fault", ["timeout", "503"])
@pytest.mark.asyncio
async def test_usage_completion_outage_preserves_lifecycle_and_retries_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, status: str, fault: str
) -> None:
    from omnigent.harnesses.codex_native import bridge

    monkeypatch.delenv("OMNIGENT_USAGE_EXPORT_CAPTURE", raising=False)
    bridge.write_bridge_state(
        tmp_path,
        bridge.CodexNativeBridgeState(
            "conv_parent",
            "ws://127.0.0.1:9",
            _PARENT_THREAD,
            "/opt/work/codex-home",
            active_turn_id="turn_parent",
            usage_admission_id="admitted",
            usage_turn_id="turn_parent",
        ),
    )
    lifecycle = asyncio.Event()
    attempted = asyncio.Event()
    release = asyncio.Event()
    acknowledged = asyncio.Event()
    recovered = False

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/usage-cutover/admissions":
            attempted.set()
            if not recovered:
                if fault == "timeout":
                    await release.wait()
                    raise httpx.ReadTimeout("usage offline", request=request)
                return httpx.Response(503)
            acknowledged.set()
            return httpx.Response(200, json={})
        if request.content:
            body = json.loads(request.content)
            if body.get("type") == "external_session_status" and body["data"]["status"] == status:
                lifecycle.set()
        return httpx.Response(202, json={"queued": False})

    def start(client):
        return asyncio.create_task(
            fwd.supervise_forwarder(
                base_url="http://127.0.0.1:9",
                headers={},
                session_id="conv_parent",
                bridge_dir=tmp_path,
                app_server_url="ws://127.0.0.1:9",
                thread_id=_PARENT_THREAD,
                client=client,
                ap_transport=httpx.MockTransport(handler),
            )
        )

    client = _QueueEventsClient()
    task = start(client)
    try:
        client.events.put_nowait(
            {
                "method": method,
                "params": {
                    "threadId": _PARENT_THREAD,
                    "turn": {
                        "id": "turn_parent",
                        "status": "completed" if status == "idle" else "failed",
                    },
                },
            }
        )
        await asyncio.wait_for(lifecycle.wait(), 2)
        assert bridge.read_bridge_state(tmp_path).active_turn_id is None
        await asyncio.wait_for(attempted.wait(), 2)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    assert bridge.pending_usage_completions(tmp_path) == [("conv_parent", "admitted")]
    recovered = True
    task = start(_QueueEventsClient())
    try:
        await asyncio.wait_for(acknowledged.wait(), 2)
        for _ in range(100):
            if not bridge.pending_usage_completions(tmp_path):
                break
            await asyncio.sleep(0.01)
        assert bridge.pending_usage_completions(tmp_path) == []
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
