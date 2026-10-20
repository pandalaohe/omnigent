"""Host admission composes the saved-input owner with durable permits."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from starlette.datastructures import Headers

from omnigent.entities.session_startup import StartupReceipt
from omnigent.host.frames import HostHelloFrame
from omnigent.server.host_registry import HostRegistry
from omnigent.server.schemas import SessionEventInput
from omnigent.server.server_config import StartupAdmissionPolicy
from omnigent.server.startup_admission import StartupAdmission, StartupQueued
from omnigent.server.startup_inputs import ManagedStartupInputs
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore
from tests.server.test_host_registry import FakeWebSocket


def test_preflight_discard_preserves_a_committed_cancellation(db_uri):
    store, host = SessionStartupStore(db_uri), uuid4().hex
    store.configure(host, host_resident_limit=8, deployment_resident_limit=8)
    startup = store.enqueue(
        owner_user_id="owner",
        host_id=host,
        session_id=uuid4().hex,
        operation_key="create",
        input_id=None,
    )
    attempt = store.reserve(host)
    assert store.claim(startup.id, attempt.attempt_id, runtime_id="context")
    assert (
        store.cancel_current(startup.id, expected_attempt_id=attempt.attempt_id).state
        == "cancelling"
    )
    assert store.discard_ungranted_attempt(startup.id, attempt.attempt_id, runtime_id="context")
    assert store.get(startup.id).state == "cancelled"


@pytest.mark.asyncio
async def test_saved_inputs_wait_for_compatible_host_then_only_two_dispatch(db_uri):
    conversations, startups = SqlAlchemyConversationStore(db_uri), SessionStartupStore(db_uri)
    registry, host = HostRegistry(), uuid4().hex
    conn = registry.register(
        host,
        FakeWebSocket(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="sample-host"),
        owner="owner",
    )
    inputs = ManagedStartupInputs(conversations, registry)
    admissions = StartupAdmission(
        startups, conversations, registry, StartupAdmissionPolicy(8, 8, 2)
    )
    delivered = []

    async def dispatch(context, session_id, event):
        ref = await admissions.admit_launch(conn, session_id)
        assert context.startup == ref
        assert await context.claim(conversations, session_id)
        delivered.append((session_id, ref, event.data["stable_id"]))
        return {"queued": True, "pending_id": "pending"}

    inputs.dispatch = dispatch
    app = SimpleNamespace(state=SimpleNamespace(startup_inputs=inputs))
    admissions.bind(app)
    sessions = []
    for _ in range(4):
        conv = conversations.create_conversation()
        conversations.set_host_id(conv.id, host, "/opt/work/project")
        item = await inputs.enqueue(
            conv.id,
            SessionEventInput(type="message", data={"role": "user", "content": []}),
            user_id="owner",
            headers=Headers(),
        )
        with pytest.raises(StartupQueued):
            await admissions.admit_launch(conn, conv.id)
        sessions.append((conv.id, item.id))
    await admissions.coordinator.advance(host)
    await admissions.coordinator.drain()
    assert delivered == []
    assert len(startups.list_queued(host)) == 4
    conn.hello.connection_id = "generation"
    conn.hello.capabilities = ["startup_admission_v1", "startup_receipts:test"]
    admissions.resolve_harness = AsyncMock(return_value="test")
    await admissions.coordinator.advance(host)
    await admissions.coordinator.drain()
    assert len(delivered) == 2
    for session_id, ref, input_id in delivered:
        assert ref.input_id == input_id == dict(sessions)[session_id]
        assert ref.host_generation == conn.startup_generation
        assert len(conversations.list_pending_inputs(session_id)) == 1
    replacement = StartupAdmission(
        startups, conversations, registry, StartupAdmissionPolicy(8, 8, 2)
    )
    replacement.bind(app)
    replacement.resolve_harness = AsyncMock(return_value="test")
    await replacement.coordinator.advance(host)
    await replacement.coordinator.drain()
    assert len(delivered) == 2
    await admissions.shutdown()
    await replacement.shutdown()
    await inputs.shutdown()


@pytest.mark.asyncio
async def test_admitted_scope_cannot_authorize_a_replaced_connection(db_uri):
    conversations, startups = SqlAlchemyConversationStore(db_uri), SessionStartupStore(db_uri)
    registry, host = HostRegistry(), uuid4().hex
    conn = registry.register(
        host,
        FakeWebSocket(),
        HostHelloFrame(
            version="test", frame_protocol_version=1, name="sample-host", connection_id="old"
        ),
        owner="owner",
    )
    admission = StartupAdmission(
        startups, conversations, registry, StartupAdmissionPolicy(8, 8, 2)
    )
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, host, "/opt/work/project")
    with pytest.raises(StartupQueued):
        await admission.admit_launch(conn, conv.id)
    replacement = registry.register(host, FakeWebSocket(), conn.hello, owner="owner")
    with pytest.raises(StartupQueued):
        await admission.admit_launch(conn, conv.id)
    assert registry.get(host) is replacement
    assert len(startups.list_queued(host)) == 1
    await admission.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("physical_grant", [False, True])
async def test_missing_dispatcher_releases_only_an_ungranted_attempt(db_uri, physical_grant):
    conversations, startups = SqlAlchemyConversationStore(db_uri), SessionStartupStore(db_uri)
    registry, host = HostRegistry(), uuid4().hex
    conn = registry.register(
        host,
        FakeWebSocket(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="sample-host",
            connection_id="generation",
            capabilities=["startup_admission_v1", "startup_receipts:test"],
        ),
        owner="owner",
    )
    inputs = ManagedStartupInputs(conversations, registry)
    admission = StartupAdmission(
        startups, conversations, registry, StartupAdmissionPolicy(8, 8, 2)
    )
    admission.bind(SimpleNamespace(state=SimpleNamespace(startup_inputs=inputs)))
    admission.resolve_harness = AsyncMock(return_value="test")
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, host, "/opt/work/project")
    item = await inputs.enqueue(
        conv.id,
        SessionEventInput(type="message", data={"role": "user", "content": []}),
        user_id="owner",
        headers=Headers(),
    )
    with pytest.raises(StartupQueued):
        await admission.admit_launch(conn, conv.id)
    if physical_grant:

        async def launch_without_input(context, session_id, event):
            ref = context.startup
            assert startups.claim_boundary(
                ref.startup_id,
                ref.attempt_id,
                runtime_id=ref.runtime_id,
                host_generation=ref.host_generation,
                boundary="runner_prepare",
                claim_id="claim",
            )
            return {"pending_id": "pending"}

        inputs.dispatch = launch_without_input
    await admission.coordinator.advance(host)
    await admission.coordinator.drain()
    assert startups.for_session(conv.id).state == ("uncertain" if physical_grant else "blocked")
    # No dispatcher ran, so the canonical input remains queued and can be repaired.
    assert conversations.get_item(conv.id, item.id).status == "in_progress"
    for _ in range(2):
        startups.enqueue(
            owner_user_id="owner",
            host_id=host,
            session_id=uuid4().hex,
            operation_key=uuid4().hex,
            input_id=None,
        )
    assert startups.reserve(host) is not None
    assert (startups.reserve(host) is None) == physical_grant
    await admission.shutdown()
    await inputs.shutdown()


@pytest.mark.asyncio
async def test_first_saved_input_rearms_empty_context_without_relaunch(db_uri):
    conversations, startups = SqlAlchemyConversationStore(db_uri), SessionStartupStore(db_uri)
    registry, host = HostRegistry(), uuid4().hex
    conn = registry.register(
        host,
        FakeWebSocket(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="sample-host",
            capabilities=["startup_admission_v1", "startup_receipts:test"],
        ),
        owner="owner",
    )
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, host, "/opt/work/project")
    inputs = ManagedStartupInputs(conversations, registry)
    admission = StartupAdmission(
        startups, conversations, registry, StartupAdmissionPolicy(1, 1, 2)
    )
    admission.bind(SimpleNamespace(state=SimpleNamespace(startup_inputs=inputs)))
    admission.resolve_harness = AsyncMock(return_value="test")
    empty = await admission.queue_session(conn, conv.id)
    attempt = startups.reserve(host)
    assert startups.claim_boundary(
        empty.id,
        attempt.attempt_id,
        runtime_id="existing-context",
        host_generation=conn.startup_generation,
        boundary="runner_prepare",
        claim_id="empty",
    )
    for phase in ("runtime_ready", "tools_ready"):
        assert startups.observe(
            StartupReceipt(
                startup_id=empty.id,
                attempt_id=attempt.attempt_id,
                runtime_id="existing-context",
                source="runner",
                phase=phase,
            )
        )
    item = await inputs.enqueue(
        conv.id,
        SessionEventInput(type="message", data={"role": "user", "content": []}),
        user_id="owner",
        headers=Headers(),
    )
    first = await admission.queue_session(conn, conv.id)
    assert first.id != empty.id and first.input_id == item.id
    assert (await admission.queue_session(conn, conv.id)).id == first.id
    delivered = []

    async def dispatch(context, session_id, event):
        ref = await admission.admit_launch(conn, session_id)
        assert ref.runtime_id == "existing-context"
        assert ref.input_id == item.id
        assert await context.claim(conversations, session_id)
        delivered.append(ref)
        return {"pending_id": "pending"}

    inputs.dispatch = dispatch
    await admission.coordinator.advance(host)
    await admission.coordinator.drain()
    assert len(delivered) == 1
    assert startups.for_session(conv.id).state in ("starting", "verifying")
    assert len(conversations.list_pending_inputs(conv.id)) == 1
    await admission.shutdown()
    await inputs.shutdown()
