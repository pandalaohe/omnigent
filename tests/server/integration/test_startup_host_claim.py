"""The actual host tunnel returns a fenced durable cold-start grant."""

import asyncio
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
    finally:
        comm.stop(exceptions=False)
        await asyncio.gather(comm.future, return_exceptions=True)
