"""RPC lifecycle regressions for the native Codex app-server client."""

from __future__ import annotations

import asyncio
import contextlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from websockets.asyncio.client import ClientConnection
from websockets.asyncio.server import ServerConnection, serve

from omnigent.harnesses.codex_native.app_server import CodexAppServerClient, CodexNativeAppServer
from omnigent.harnesses.codex_native.side_chat import (
    fork_ephemeral_side_thread,
    fork_keep_warm_thread,
    open_side_chat_on_client,
)
from omnigent.runner.native import orchestration


@pytest.fixture
async def context_v2_native(tmp_path, monkeypatch):
    backend = CodexNativeAppServer(
        codex_path="/opt/work/bin/codex",
        socket_path=tmp_path / "server.sock",
        codex_home=tmp_path / "home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
    )
    backend.proc = SimpleNamespace(returncode=None)
    client = CodexAppServerClient(ws_url="ws://codex.test")
    websocket = AsyncMock(spec=ClientConnection)
    client._ws = websocket
    reader = asyncio.create_task(asyncio.Event().wait())
    client._reader_task = reader
    client._established_connection = (0, websocket, reader)
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", {"conversation": backend})
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    monkeypatch.setattr(orchestration, "_STARTING_NATIVE_BACKENDS", {})
    monkeypatch.setattr(orchestration, "_CLOSING_NATIVE_BACKENDS", {})
    monkeypatch.setattr(orchestration, "_RETIRED_NATIVE_CONTEXTS", [])
    monkeypatch.setattr(orchestration, "_native_context_history_overflow", False)
    monkeypatch.setattr(orchestration, "_CODEX_INVENTORY_CLIENTS", {})
    orchestration._bind_codex_inventory_client(
        "conversation", backend, client, incarnation_id=None
    )
    assert not orchestration._CODEX_INVENTORY_CLIENTS  # capability remains dormant
    try:
        yield backend, client, websocket
    finally:
        await client.close()


async def test_context_v2_native_explicit_side_and_maintenance_are_distinct(
    context_v2_native, monkeypatch
):
    backend, client, websocket = context_v2_native
    calls = []
    responses = iter(["primary", "side", "side", "maintenance", "unclassified"])

    async def send(raw):
        message = json.loads(raw)
        calls.append(message)
        client._pending_requests[message["id"]].set_result(
            {"result": {"thread": {"id": next(responses)}}}
        )

    websocket.send.side_effect = send
    await client.request("thread/start", {})
    assert await fork_ephemeral_side_thread(client, "primary") == "side"
    await client.request("thread/resume", {"threadId": "side"})
    assert await fork_keep_warm_thread(client.request, "primary") == ("maintenance", None)
    await client.request("thread/fork", {"threadId": "primary", "ephemeral": True})
    source = orchestration.native_context_source_v2("conversation")
    primary, side, maintenance, unclassified = source.contexts
    assert side.key != maintenance.key != primary.key
    assert side.operation.parent == maintenance.operation.parent == primary.key
    assert side.provenance == "managed_side_chat" and side.operation.action == "fork"
    assert maintenance.provenance == "maintenance" and unclassified.provenance == "unknown"
    assert side.execution_keys == ("side",) and not side.aliases
    assert source.coverage == "partial" and source.revision_authority == "observation_only"
    assert all("context_provenance" not in message["params"] for message in calls)
    await client.close()
    assert all(
        row.lifecycle == "unknown" for row in backend.context_source_v2("conversation").contexts
    )

    async def close():
        backend.proc = None
        backend.process_exit_confirmed = True

    monkeypatch.setattr(backend, "_close_locked", close)
    await orchestration._close_native_backend("conversation", "codex-native", backend)
    orchestration._AUTO_CODEX_APP_SERVERS.clear()
    retained = orchestration.native_context_source_v2("conversation")
    assert retained.contexts[0].key == primary.key
    assert all(row.lifecycle == "unknown" and row.stop_proof is None for row in retained.contexts)


@pytest.mark.parametrize("outcome", ["cancel", "generation", "close", "wrong-id"])
async def test_context_v2_native_pending_reply_cannot_bind_replacement(context_v2_native, outcome):
    backend, client, websocket = context_v2_native
    entered = asyncio.Event()
    requests = []

    async def send(raw):
        requests.append(json.loads(raw))
        entered.set()

    websocket.send.side_effect = send
    task = asyncio.create_task(client.request("thread/resume", {"threadId": "primary"}))
    closing = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        (before,) = backend.context_source_v2("conversation").contexts
        assert before.lifecycle == "starting"
        if outcome == "generation":
            backend._start_generation += 1
        elif outcome == "close":
            closing = backend.close()
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            client._pending_requests[requests[0]["id"]].set_result(
                {"result": {"thread": {"id": "different" if outcome == "wrong-id" else "primary"}}}
            )
            await task
        (after,) = backend.context_source_v2("conversation").contexts
        assert after.key == before.key
        assert after.lifecycle == ("closing" if outcome == "close" else "unknown")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if closing is not None:
            closing.close()


def test_context_v2_native_unbound_reads_allocate_no_registry(tmp_path):
    backend = CodexNativeAppServer(
        codex_path="/opt/work/bin/codex",
        socket_path=tmp_path / "server.sock",
        codex_home=tmp_path / "home",
        env={},
        config_overrides=[],
        cwd=tmp_path,
        bridge_dir=tmp_path / "bridge",
    )
    source = backend.context_source_v2("conversation")
    assert source.contexts == () and source.coverage == "partial"
    assert backend._context_inventory is None


def test_context_v2_history_budget_vetoes_instead_of_truncating():
    from omnigent.harnesses.codex_native.context_inventory import CodexContextInventory
    from omnigent.startup_protocol import native_context_scope_v2

    inventory = CodexContextInventory("owner")
    for _ in range(1001):
        inventory.begin("thread/start", {}, domain="owner", current=lambda: True)
    assert len(inventory.records) == 1000
    source = inventory.source(native_context_scope_v2("conversation"))
    assert source.contexts == () and source.coverage == "partial"
    assert "context_history_budget" in {u.reason for u in source.unresolved}


async def test_context_v2_missing_parent_does_not_discard_explicit_side_question(
    context_v2_native,
):
    backend, client, websocket = context_v2_native
    methods = []

    async def send(raw):
        message = json.loads(raw)
        methods.append(message["method"])
        result = (
            {"thread": {"id": "side"}}
            if message["method"] == "thread/fork"
            else {"turn": {"id": "turn"}}
        )
        client._pending_requests[message["id"]].set_result({"result": result})

    websocket.send.side_effect = send
    assert (
        await open_side_chat_on_client(client, parent_thread_id="unobserved", question="why?")
        == "side"
    )
    assert methods == ["thread/fork", "turn/start"]
    source = backend.context_source_v2("conversation")
    (record,) = source.contexts
    assert record.vendor_id.value == "side" and record.execution_keys == ("side",)
    assert record.operation.action == "fork" and record.operation.parent is None
    assert record.provenance == "unknown" and record.lifecycle == "live"
    assert "fork_parent_unobserved" in {u.reason for u in source.unresolved}


@pytest.mark.parametrize("outcome", ["normal", "abrupt", "malformed", "reply_then_close"])
async def test_pending_rpc_finishes_when_reader_exits(outcome: str) -> None:
    """A disconnected reader cannot leave its caller awaiting a response forever."""

    async def handle(websocket: ServerConnection) -> None:
        async for raw in websocket:
            message = json.loads(raw)
            if message["method"] == "initialize":
                await websocket.send(json.dumps({"id": message["id"], "result": {}}))
            elif message["method"] == "turn/start":
                if outcome == "reply_then_close":
                    await websocket.send(
                        json.dumps({"id": message["id"], "result": {"accepted": True}})
                    )
                elif outcome == "malformed":
                    await websocket.send("invalid json")
                if outcome == "abrupt":
                    websocket.transport.abort()
                else:
                    await websocket.close()
                return

    async with serve(handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        client = CodexAppServerClient(ws_url=f"ws://127.0.0.1:{port}")
        await client.connect()
        request = asyncio.create_task(client.request("turn/start", {"input": []}))
        try:
            done, _ = await asyncio.wait({request}, timeout=2)
            assert request in done, "RPC remained pending after app-server disconnect"
            if outcome == "reply_then_close":
                assert (await request)["result"] == {"accepted": True}
            else:
                with pytest.raises(ConnectionError, match="before responding"):
                    await request
            assert client._pending_requests == {}
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)
            with contextlib.suppress(ValueError):
                await client.close()


@pytest.mark.parametrize("outcome", ["cancelled", "send_failure"])
async def test_abandoned_rpc_releases_pending_request(outcome: str) -> None:
    """Failed sends and cancelled callers release their pending response slot."""
    client = CodexAppServerClient(ws_url="ws://127.0.0.1:12345")
    websocket = AsyncMock(spec=ClientConnection)
    sent = asyncio.Event()

    async def send(_raw: str) -> None:
        sent.set()
        if outcome == "send_failure":
            raise ConnectionError("connection lost during send")

    websocket.send.side_effect = send
    client._ws = websocket
    client._reader_task = asyncio.create_task(asyncio.Event().wait())
    request = asyncio.create_task(client.request("turn/start", {}))
    try:
        await asyncio.wait_for(sent.wait(), 2)
        if outcome == "cancelled":
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        else:
            with pytest.raises(ConnectionError):
                await request
        assert client._pending_requests == {}
        assert not client._reader_task.done()
    finally:
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await client.close()
