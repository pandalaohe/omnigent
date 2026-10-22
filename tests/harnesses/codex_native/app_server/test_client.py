"""Client tests for Codex app server."""

from __future__ import annotations

import asyncio
import json
import traceback
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from omnigent.harnesses.codex_native import app_server
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexAppServerResponseError,
)


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (-32600, "no active turn to steer", True),
        (-32600, "no active turn to interrupt", True),
        (-32600, " NO ACTIVE TURN TO INTERRUPT ", True),
        (-32600, "expected active turn id `turn_a` but found `turn_b`", True),
        (-32600, "expected active turn id turn_a but found turn_b", True),
        (-32600, "thread not found", False),
        (-32600, "invalid turn id", False),
        (-32600, "expected active turn id", False),
        (-32600, "cannot steer a review turn", False),
        (-32603, "no active turn to interrupt", False),
        (-32603, "expected active turn id turn_a but found turn_b", False),
        (-32600, None, False),
    ],
)
def test_is_stale_active_turn_error(code: int, message: str | None, expected: bool) -> None:
    error = CodexAppServerResponseError({"code": code, "message": message})
    assert app_server.is_stale_active_turn_error(error) is expected


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (-32600, "no active turn to steer", True),
        (-32600, "no active turn to interrupt", True),
        (-32600, " NO ACTIVE TURN TO INTERRUPT ", True),
        (-32600, "expected active turn id turn_a but found turn_b", False),
        (-32600, "expected active turn id `turn_a` but found `turn_b`", False),
        (-32600, "thread not found", False),
        (-32603, "no active turn to interrupt", False),
        (-32600, None, False),
    ],
)
def test_is_no_active_turn_error(code: int, message: str | None, expected: bool) -> None:
    """Only a genuinely-ended turn qualifies; a superseded turn does not."""
    error = CodexAppServerResponseError({"code": code, "message": message})
    assert app_server.is_no_active_turn_error(error) is expected


@pytest.mark.parametrize("method", ["turn/start", "turn/steer"])
async def test_rejected_request_traceback_identifies_rpc(method: str) -> None:
    """RPC errors keep their structured payload and add only request identity."""
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    client._ws = cast(ClientConnection, websocket)
    error = {"code": -32600, "message": "invalid turn id"}

    async def reject_request(raw: str) -> None:
        envelope = json.loads(raw)
        request_id = envelope["id"]
        client._pending_requests.pop(request_id).set_result({"id": request_id, "error": error})

    websocket.send.side_effect = reject_request
    params = {"input": [{"text": "private prompt"}]}
    with pytest.raises(CodexAppServerResponseError) as caught:
        await client.request(method, params)

    exc = caught.value
    assert exc.error is error
    assert exc.code == -32600
    assert exc.message == "invalid turn id"
    assert str(exc) == str(error)
    assert exc.__notes__ == [f"Codex app-server RPC: method={method} request_id=1"]
    rendered = "".join(traceback.format_exception(exc))
    assert exc.__notes__[0] in rendered
    assert "private prompt" not in rendered


@pytest.mark.parametrize(
    "reader_error", [ConnectionClosedError(None, None), ConnectionClosedOK(None, None)]
)
async def test_client_close_cleans_up_after_reader_disconnect(reader_error: Exception) -> None:
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    client._ws = cast(ClientConnection, websocket)

    async def fail_reader() -> None:
        raise reader_error

    reader = asyncio.create_task(fail_reader())
    client._reader_task = reader
    pending = asyncio.get_running_loop().create_future()
    client._pending_requests[1] = pending
    await asyncio.sleep(0)
    assert reader.done()

    await client.close()

    assert pending.cancelled()
    assert client._pending_requests == {}
    assert client._reader_task is None
    assert client._ws is None
    websocket.close.assert_awaited_once()
    await client.close()
    websocket.close.assert_awaited_once()


async def test_client_close_preserves_reader_bug_after_cleanup() -> None:
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    client._ws = cast(ClientConnection, websocket)

    async def fail_reader() -> None:
        raise ValueError("invalid event payload")

    reader = asyncio.create_task(fail_reader())
    client._reader_task = reader
    pending = asyncio.get_running_loop().create_future()
    client._pending_requests[1] = pending
    await asyncio.sleep(0)

    with pytest.raises(ValueError, match="invalid event payload"):
        await client.close()

    assert pending.cancelled()
    assert client._pending_requests == {}
    assert client._reader_task is None
    assert client._ws is None
    websocket.close.assert_awaited_once()


async def test_client_close_clears_state_when_websocket_close_fails() -> None:
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    websocket.close.side_effect = ValueError("close failed")
    client._ws = cast(ClientConnection, websocket)
    reader = asyncio.create_task(asyncio.sleep(60))
    client._reader_task = reader
    pending = asyncio.get_running_loop().create_future()
    client._pending_requests[1] = pending

    with pytest.raises(ValueError, match="close failed"):
        await client.close()

    assert reader.cancelled()
    assert pending.cancelled()
    assert client._pending_requests == {}
    assert client._reader_task is None
    assert client._ws is None


@pytest.mark.parametrize(
    "case",
    [
        "empty",
        "single",
        "pages",
        "literal",
        "missing-result",
        "missing-data",
        "data-not-list",
        "invalid-id",
        "empty-id",
        "duplicate-id",
        "overlapping-pages",
        "oversized-page",
        "missing-cursor",
        "invalid-cursor",
        "empty-cursor",
        "repeated-cursor",
        "page-cap",
        "rpc-error",
        "disconnect",
        "reader-exit",
        "replaced-websocket",
        "replaced-reader",
        "connection-generation",
        "close",
        "timeout",
        "cancel",
        "no-connection",
        "no-reader",
        "dead-reader",
        "timeout-zero",
        "timeout-large",
        "timeout-nan",
    ],
)
async def test_loaded_thread_inventory_uses_only_current_connected_channel(case):
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    client._ws = websocket
    reader = asyncio.create_task(asyncio.Event().wait())
    client._reader_task = reader
    client._established_connection = (0, websocket, reader)
    sent = asyncio.Event()
    requests = []
    extra_reader = None
    client.connect = AsyncMock(side_effect=AssertionError("inventory must not connect"))

    async def respond(raw):
        nonlocal extra_reader
        message = json.loads(raw)
        requests.append(message)
        sent.set()
        index = len(requests)
        data, cursor = ["thread-main"], None
        if case == "empty":
            data = []
        elif case == "literal":
            data = [" thread-main ", "thread-main"]
        elif case in {"pages", "overlapping-pages"}:
            data = ["thread-main"] if index == 1 else ["thread-side"]
            if case == "overlapping-pages":
                data = ["thread-main"]
            cursor = "page-two" if index == 1 else None
        elif case == "data-not-list":
            data = "thread-main"
        elif case == "invalid-id":
            data = [7]
        elif case == "empty-id":
            data = [""]
        elif case == "duplicate-id":
            data = ["thread-main", "thread-main"]
        elif case == "oversized-page":
            data = [f"thread-{i}" for i in range(101)]
        elif case == "invalid-cursor":
            cursor = 7
        elif case == "empty-cursor":
            cursor = ""
        elif case in {"repeated-cursor", "page-cap"}:
            data, cursor = (
                [f"thread-{index}"],
                ("repeated" if case == "repeated-cursor" else f"page-{index}"),
            )
        elif case == "disconnect":
            raise ConnectionError("channel lost")
        elif case == "reader-exit":
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
        elif case == "replaced-websocket":
            client._ws = AsyncMock(spec=ClientConnection)
        elif case == "replaced-reader":
            extra_reader = asyncio.create_task(asyncio.Event().wait())
            client._reader_task = extra_reader
        elif case == "connection-generation":
            client._connection_generation += 1
        elif case == "close":
            await client.close()
        elif case in {"timeout", "cancel"}:
            return
        reply = {"id": message["id"], "result": {"data": data, "nextCursor": cursor}}
        if case == "missing-result":
            reply.pop("result")
        elif case == "missing-data":
            reply["result"].pop("data")
        elif case == "missing-cursor":
            reply["result"].pop("nextCursor")
        elif case == "rpc-error":
            reply = {"id": message["id"], "error": {"code": -32601, "message": "unsupported"}}
        pending = client._pending_requests.get(message["id"])
        if pending is not None and not pending.done():
            pending.set_result(reply)

    websocket.send.side_effect = respond
    if case == "no-connection":
        client._ws = None
    elif case == "no-reader":
        client._reader_task = None
    elif case == "dead-reader":
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    timeout = {"timeout-zero": 0, "timeout-large": 6, "timeout-nan": float("nan")}.get(
        case, 0.02 if case == "timeout" else 2
    )
    operation = None
    try:
        if case == "cancel":
            operation = asyncio.create_task(client.read_loaded_thread_ids(timeout_s=timeout))
            await asyncio.wait_for(sent.wait(), 1)
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            result = await client.read_loaded_thread_ids(timeout_s=timeout)
            expected = {
                "empty": (),
                "single": ("thread-main",),
                "pages": ("thread-main", "thread-side"),
                "literal": (" thread-main ", "thread-main"),
            }.get(case)
            assert result == expected
        assert client._pending_requests == {}
        client.connect.assert_not_called()
        if case != "close":
            websocket.close.assert_not_called()
        assert all(x["method"] == "thread/list/loaded" for x in requests)
        assert len(requests) <= 10
        if case == "pages":
            assert [x["params"] for x in requests] == [
                {"limit": 100},
                {"limit": 100, "cursor": "page-two"},
            ]
        elif case == "page-cap":
            assert len(requests) == 10
        elif case in {
            "no-connection",
            "no-reader",
            "dead-reader",
            "timeout-zero",
            "timeout-large",
            "timeout-nan",
        }:
            assert requests == []
    finally:
        if operation is not None:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        for task in (reader, extra_reader):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        await client.close()


@pytest.mark.parametrize("action", ["connect", "close"])
async def test_client_generation_changes_before_lifecycle_await(monkeypatch, action):
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    entered, release = asyncio.Event(), asyncio.Event()

    async def wait(*args, **kwargs):
        entered.set()
        await release.wait()

    if action == "connect":
        monkeypatch.setattr(app_server.websockets, "connect", wait)
    else:
        websocket = AsyncMock(spec=ClientConnection)
        websocket.close.side_effect = wait
        client._ws = websocket
    assert client._connection_generation == 0
    operation = asyncio.create_task(getattr(client, action)())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        assert client._connection_generation == 1
        assert client._established_connection is None
    finally:
        operation.cancel()
        await asyncio.gather(operation, return_exceptions=True)
        release.set()
        await client.close()


@pytest.mark.parametrize("transport", ["websocket", "unix"])
@pytest.mark.parametrize("outcome", ["ready", "failed", "cancelled"])
async def test_loaded_thread_inventory_rejects_connection_replacement_window(
    monkeypatch, transport, outcome
):
    client = (
        CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
        if transport == "websocket"
        else CodexAppServerClient(socket_path=Path("/opt/work/codex.sock"))
    )
    old = AsyncMock(spec=ClientConnection)
    replacement = AsyncMock(spec=ClientConnection)
    old_reader = asyncio.create_task(asyncio.Event().wait())
    client._ws, client._reader_task = old, old_reader
    client._established_connection = (0, old, old_reader)
    connecting, release_socket = asyncio.Event(), asyncio.Event()
    initializing, release_init = asyncio.Event(), asyncio.Event()

    async def open_socket(*args, **kwargs):
        connecting.set()
        await release_socket.wait()
        if outcome == "failed":
            raise ConnectionError("replacement failed")
        return replacement

    async def events():
        await asyncio.Event().wait()
        yield ""

    async def old_send(raw):
        message = json.loads(raw)
        assert message["method"] == "thread/list/loaded"
        client._pending_requests[message["id"]].set_result(
            {"result": {"data": ["old-thread"], "nextCursor": None}}
        )

    async def replacement_send(raw):
        message = json.loads(raw)
        if message["method"] == "initialize":
            initializing.set()
            await release_init.wait()
            result = {}
        elif message["method"] == "thread/list/loaded":
            result = {"data": ["new-thread"], "nextCursor": None}
        else:
            assert message["method"] == "initialized"
            return
        client._pending_requests[message["id"]].set_result({"result": result})

    monkeypatch.setattr(
        app_server.websockets,
        "connect" if transport == "websocket" else "unix_connect",
        open_socket,
    )
    old.send.side_effect = old_send
    replacement.send.side_effect = replacement_send
    replacement.__aiter__.side_effect = events
    operation = None
    try:
        assert await client.read_loaded_thread_ids() == ("old-thread",)
        operation = asyncio.create_task(client.connect())
        await asyncio.wait_for(connecting.wait(), 1)
        assert client._ws is old and client._reader_task is old_reader
        assert await client.read_loaded_thread_ids() is None
        assert old.send.await_count == 1
        if outcome == "cancelled":
            operation.cancel()
            with pytest.raises(asyncio.CancelledError):
                await operation
        else:
            release_socket.set()
            if outcome == "failed":
                with pytest.raises(ConnectionError, match="replacement failed"):
                    await operation
            else:
                await asyncio.wait_for(initializing.wait(), 1)
                assert client._ws is replacement
                assert await client.read_loaded_thread_ids() is None
                assert replacement.send.await_count == 1
                release_init.set()
                await asyncio.wait_for(operation, 1)
                assert await client.read_loaded_thread_ids() == ("new-thread",)
        if outcome != "ready":
            assert client._ws is old and not old_reader.done()
            assert await client.read_loaded_thread_ids() is None
            assert old.send.await_count == 1
        assert client._pending_requests == {}
    finally:
        if operation is not None:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
        old_reader.cancel()
        await asyncio.gather(old_reader, return_exceptions=True)
        await client.close()
