"""The actual host tunnel returns a fenced durable cold-start grant."""

import asyncio
import threading
from contextlib import asynccontextmanager
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI

from omnigent.host.frames import (
    HostStartupClaimFrame,
    HostStartupClaimResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.startup_protocol import StartupAttemptRef
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.session_startup_store import SessionStartupStore
from tests.server.integration.test_host_tunnel_route import (
    _HOST_ID,
    _TUNNEL_PATH,
    _connect_route,
    _make_hello,
    _send_hello_and_wait,
)


@pytest.mark.asyncio
async def test_host_tunnel_grants_once_and_rejects_replayed_physical_permission(db_uri):
    registry, hosts = HostRegistry(), HostStore(db_uri)
    registry.launch_authorizer = Mock()
    conversations, startups = SqlAlchemyConversationStore(db_uri), SessionStartupStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(
            registry, hosts, conversation_store=conversations, startup_store=startups
        ),
        prefix="/v1",
    )
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, _HOST_ID, "/opt/work/project")
    conversations.set_runner_id(conv.id, "bound-runner")
    startups.configure(_HOST_ID, host_resident_limit=7, deployment_resident_limit=20)
    startup = startups.enqueue(
        owner_user_id="owner",
        host_id=_HOST_ID,
        session_id=conv.id,
        operation_key="create",
        input_id=None,
    )
    attempt = startups.reserve(_HOST_ID)
    ref = StartupAttemptRef(
        startup_id=startup.id,
        attempt_id=attempt.attempt_id,
        input_id=None,
        runtime_id="context",
        host_generation="connection",
    )
    comm = await _connect_route(app, _TUNNEL_PATH)
    try:
        await _send_hello_and_wait(comm, registry, connection_id="connection")
        ref = ref.model_copy(update={"host_generation": registry.get(_HOST_ID).startup_generation})
        for expected in (True, False):
            request_id = uuid4().hex
            await comm.send_input(
                {
                    "type": "websocket.receive",
                    "text": encode_host_frame(
                        HostStartupClaimFrame(request_id, conv.id, "bound-runner", ref)
                    ),
                }
            )
            message = await comm.receive_output(timeout=2)
            result = decode_host_frame(message["text"])
            assert result == HostStartupClaimResultFrame(request_id, expected)
        replacement = await _connect_route(app, _TUNNEL_PATH)
        try:
            await _send_hello_and_wait(replacement, registry, connection_id="connection")

            async def wait_replaced():
                while registry.get(_HOST_ID).startup_generation == ref.host_generation:
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_replaced(), 3)
            assert registry.get(_HOST_ID).startup_generation != ref.host_generation
            assert not startups.claim_boundary(
                ref.startup_id,
                ref.attempt_id,
                runtime_id=ref.runtime_id,
                host_generation=ref.host_generation,
                boundary="runner_spawn",
                claim_id="stale",
                expected_runner_id="bound-runner",
            )
        finally:
            replacement.stop(exceptions=False)
            await asyncio.gather(replacement.future, return_exceptions=True)
    finally:
        comm.stop(exceptions=False)
        await asyncio.gather(comm.future, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_handshakes_publish_the_durable_generation_in_order(db_uri, monkeypatch):
    registry, hosts = HostRegistry(), HostStore(db_uri)
    startups = SessionStartupStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(registry, hosts, startup_store=startups), prefix="/v1"
    )
    committed, release = threading.Event(), threading.Event()
    second_waiting, published = asyncio.Event(), asyncio.Event()
    register_generation = startups.register_host_generation
    registration = registry.startup_registration
    register = registry.register
    arrivals = entries = publications = 0
    generations = []

    def pause_first_commit(host_id, generation):
        register_generation(host_id, generation)
        generations.append(generation)
        if len(generations) == 1:
            committed.set()
            assert release.wait(3)

    @asynccontextmanager
    async def trace_registration(host_id):
        nonlocal arrivals, entries
        arrivals += 1
        if arrivals == 2:
            second_waiting.set()
        async with registration(host_id):
            entries += 1
            yield

    def record_publication(*args, **kwargs):
        nonlocal publications
        conn = register(*args, **kwargs)
        publications += 1
        if publications == 2:
            published.set()
        return conn

    monkeypatch.setattr(startups, "register_host_generation", pause_first_commit)
    monkeypatch.setattr(registry, "startup_registration", trace_registration)
    monkeypatch.setattr(registry, "register", record_publication)
    first = await _connect_route(app, _TUNNEL_PATH)
    second = await _connect_route(app, _TUNNEL_PATH)
    try:
        await first.send_input({"type": "websocket.receive", "text": _make_hello()})
        assert await asyncio.to_thread(committed.wait, 3)
        await second.send_input({"type": "websocket.receive", "text": _make_hello()})
        await asyncio.wait_for(second_waiting.wait(), 3)
        assert entries == 1
        release.set()
        await asyncio.wait_for(published.wait(), 3)
        assert registry.get(_HOST_ID).startup_generation == generations[-1]
        assert len(set(generations)) == 2
    finally:
        release.set()
        for comm in (first, second):
            comm.stop(exceptions=False)
        await asyncio.gather(first.future, second.future, return_exceptions=True)
