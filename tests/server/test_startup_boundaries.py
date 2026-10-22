"""Durable cold-boundary grants cannot be replayed after a lost response."""

from uuid import uuid4

import pytest

from omnigent.stores.session_startup_store import SessionStartupStore


def test_physical_boundary_grants_are_once_only_and_generation_bound(db_uri):
    store = SessionStartupStore(db_uri)
    host = uuid4().hex
    store.configure(host, host_resident_limit=7, deployment_resident_limit=20)
    handle = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="create",
        input_id=None,
    )
    attempt = store.reserve(host)
    args = {
        "startup_id": handle.id,
        "attempt_id": attempt.attempt_id,
        "runtime_id": "context",
        "host_generation": "connection",
        "claim_id": uuid4().hex,
    }
    assert not store.claim_boundary(**args, boundary="runner_spawn")
    assert store.claim_boundary(**args, boundary="runner_prepare")
    assert not SessionStartupStore(db_uri).claim_boundary(**args, boundary="runner_prepare")
    assert not store.claim_boundary(
        **{**args, "host_generation": "replacement"}, boundary="runner_spawn"
    )
    assert store.claim_boundary(**{**args, "claim_id": uuid4().hex}, boundary="runner_spawn")
    assert not store.claim_boundary(**{**args, "claim_id": uuid4().hex}, boundary="runner_spawn")
    assert store.claim_boundary(**args, boundary="executor_construct")
    assert not SessionStartupStore(db_uri).claim_boundary(**args, boundary="executor_construct")
    assert store.claim_boundary(**args, boundary="executor_prepare")
    assert store.cancel(handle.id).state == "cancelling"
    assert not store.claim_boundary(**args, boundary="native_prepare")


@pytest.mark.asyncio
async def test_host_claim_checks_live_connection_binding_and_canonical_input(db_uri, monkeypatch):
    from unittest.mock import Mock

    from omnigent.host.frames import HostHelloFrame, HostStartupClaimFrame
    from omnigent.server.host_registry import HostRegistry
    from omnigent.server.startup_claims import authorize_host_startup_claim
    from omnigent.startup_protocol import StartupAttemptRef
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from tests.server.test_host_registry import FakeWebSocket

    conversations, store = SqlAlchemyConversationStore(db_uri), SessionStartupStore(db_uri)
    registry, host = HostRegistry(), uuid4().hex
    authority = Mock()
    registry.launch_authorizer = authority
    hello = HostHelloFrame(
        version="test", frame_protocol_version=1, name="sample-host", connection_id="connection"
    )
    conn = registry.register(
        host, FakeWebSocket(), hello, owner="owner", startup_generation="connection"
    )
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, host, "/opt/work/project")
    conversations.set_runner_id(conv.id, "bound-runner")
    store.configure(host, host_resident_limit=7, deployment_resident_limit=20)
    store.register_host_generation(host, conn.startup_generation)
    handle = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=conv.id,
        operation_key="create",
        input_id=None,
    )
    attempt = store.reserve(host)
    ref = StartupAttemptRef(
        startup_id=handle.id,
        attempt_id=attempt.attempt_id,
        input_id=None,
        runtime_id="context",
        host_generation="connection",
    )
    request = HostStartupClaimFrame("claim", conv.id, "wrong-runner", ref)
    assert not await authorize_host_startup_claim(
        request, conn=conn, registry=registry, conversations=conversations, startups=store
    )

    request.runner_id = "bound-runner"
    request.attempt = ref.model_copy(update={"input_id": uuid4().hex})
    assert not await authorize_host_startup_claim(
        request, conn=conn, registry=registry, conversations=conversations, startups=store
    )
    request.attempt = ref
    claim_boundary = store.claim_boundary

    def rebind_before_grant(*args, **kwargs):
        conversations.replace_runner_id(conv.id, "replacement")
        return claim_boundary(*args, **kwargs)

    monkeypatch.setattr(store, "claim_boundary", rebind_before_grant)
    assert not await authorize_host_startup_claim(
        request, conn=conn, registry=registry, conversations=conversations, startups=store
    )
    monkeypatch.setattr(store, "claim_boundary", claim_boundary)
    conversations.replace_runner_id(conv.id, "bound-runner")
    assert await authorize_host_startup_claim(
        request, conn=conn, registry=registry, conversations=conversations, startups=store
    )
    assert authority.call_count == 2
    request.boundary = "runner_spawn"
    registry.register(host, FakeWebSocket(), hello, owner="owner")
    assert not await authorize_host_startup_claim(
        request, conn=conn, registry=registry, conversations=conversations, startups=store
    )


@pytest.mark.asyncio
async def test_relaunch_forwards_admitted_attempt_to_physical_host(db_uri, monkeypatch):
    from unittest.mock import AsyncMock

    from omnigent.host.frames import HostHelloFrame, decode_host_frame
    from omnigent.server.host_registry import HostRegistry
    from omnigent.server.routes._sessions.helpers import _launch_runner_on_host_impl
    from omnigent.startup_protocol import StartupAttemptRef
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from tests.server.test_host_registry import FakeWebSocket

    conversations, registry = SqlAlchemyConversationStore(db_uri), HostRegistry()
    host = uuid4().hex
    conn = registry.register(
        host,
        FakeWebSocket(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="sample-host"),
        owner="owner",
    )
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, host, "/opt/work/project")
    conv = conversations.get_conversation(conv.id)
    startup = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id=None,
        runtime_id="context",
        host_generation="connection",
    )
    registry.startup_admitter = AsyncMock(return_value=startup)
    frames = []

    def capture(connection, data):
        frame = decode_host_frame(data)
        frames.append(frame)
        connection.pending_launches[frame.request_id].set_result({"status": "launched"})

    monkeypatch.setattr(registry, "send_text", capture)
    result = await _launch_runner_on_host_impl(conv, conversations, registry, conn)
    assert result.acknowledged
    assert len(frames) == 1
    assert frames[0].startup == startup and frames[0].startup_required
