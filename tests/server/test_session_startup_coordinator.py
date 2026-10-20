"""The coordinator reserves durable input references before contacting a host."""

import asyncio
from uuid import uuid4

import pytest

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server.session_startup import StartupCoordinator
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore


@pytest.mark.asyncio
async def test_two_admissions_survive_coordinator_replacement_without_input_replay(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    startups = SessionStartupStore(db_uri)
    host = uuid4().hex
    startups.configure(host, host_resident_limit=8, deployment_resident_limit=8)
    dispatched = []

    async def eligible(_startup):
        return True

    async def launch(startup):
        dispatched.append(startup.id)

    coordinator = StartupCoordinator(startups, conversations, eligible=eligible, launch=launch)
    sessions = []
    for _ in range(6):
        session = conversations.create_conversation()
        conversations.set_host_id(session.id, host, "/opt/work/project")
        [item] = conversations.append(
            session.id,
            [
                NewConversationItem(
                    type="message",
                    response_id=PENDING_INPUT_RESPONSE_ID,
                    status="in_progress",
                    stable_id=uuid4().hex,
                    data=MessageData(
                        role="user", content=[{"type": "input_text", "text": "work"}]
                    ),
                )
            ],
        )
        await coordinator.enqueue(
            session.id, input_id=item.id, owner_user_id="owner", operation_key=session.id
        )
        sessions.append(session.id)
    await coordinator.advance(host)
    await coordinator.drain()
    assert len(dispatched) == 2
    replacement = StartupCoordinator(
        SessionStartupStore(db_uri), conversations, eligible=eligible, launch=launch
    )
    await replacement.advance(host)
    await replacement.drain()
    assert len(dispatched) == 2
    # Admission doesn't create or consume a second payload owner.
    assert all(len(conversations.list_pending_inputs(session)) == 1 for session in sessions)


@pytest.mark.asyncio
async def test_missing_pending_reference_and_ineligible_hosts_never_dispatch(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    startups = SessionStartupStore(db_uri)
    host = uuid4().hex
    startups.configure(host, host_resident_limit=8, deployment_resident_limit=8)
    session = conversations.create_conversation()
    conversations.set_host_id(session.id, host, "/opt/work/project")

    async def eligible(_startup):
        return False

    async def launch(_startup):
        pytest.fail("An unsupported or unbound startup must remain queued")

    coordinator = StartupCoordinator(startups, conversations, eligible=eligible, launch=launch)
    with pytest.raises(ValueError, match="pending input"):
        await coordinator.enqueue(
            session.id, input_id=uuid4().hex, owner_user_id="owner", operation_key="missing"
        )
    queued = await coordinator.enqueue(
        session.id, input_id=None, owner_user_id="owner", operation_key="empty"
    )
    await coordinator.advance(host)
    await coordinator.drain()
    assert startups.get(queued.id).state == "queued"


@pytest.mark.asyncio
async def test_lost_launch_response_keeps_permits_and_never_resubmits(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    startups = SessionStartupStore(db_uri)
    host = uuid4().hex
    startups.configure(host, host_resident_limit=8, deployment_resident_limit=8)
    calls = []

    async def eligible(_startup):
        return True

    async def launch(startup):
        calls.append(startup.id)
        raise ConnectionError("The command may have crossed the host boundary")

    coordinator = StartupCoordinator(startups, conversations, eligible=eligible, launch=launch)
    for _ in range(3):
        conv = conversations.create_conversation()
        conversations.set_host_id(conv.id, host, "/opt/work/project")
        await coordinator.enqueue(
            conv.id, input_id=None, owner_user_id="owner", operation_key=conv.id
        )
    await coordinator.advance(host)
    await coordinator.drain()
    assert len(calls) == 2
    assert all(startups.get(startup).state == "uncertain" for startup in calls)
    replacement = StartupCoordinator(
        SessionStartupStore(db_uri), conversations, eligible=eligible, launch=launch
    )
    await replacement.advance(host)
    await replacement.drain()
    assert len(calls) == 2
    assert len(startups.list_queued(host)) == 1


@pytest.mark.asyncio
async def test_shutdown_fences_an_admission_waiting_on_eligibility(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    startups = SessionStartupStore(db_uri)
    host = uuid4().hex
    startups.configure(host, host_resident_limit=8, deployment_resident_limit=8)
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, host, "/opt/work/project")
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def eligible(_startup):
        entered.set()
        await release.wait()
        return True

    async def launch(startup):
        calls.append(startup.id)

    coordinator = StartupCoordinator(startups, conversations, eligible=eligible, launch=launch)
    await coordinator.enqueue(
        conv.id, input_id=None, owner_user_id="owner", operation_key="create"
    )
    advance = asyncio.create_task(coordinator.advance(host))
    await entered.wait()
    shutdown = asyncio.create_task(coordinator.shutdown())
    await asyncio.sleep(0)
    assert not shutdown.done()
    release.set()
    await asyncio.gather(advance, shutdown)
    await coordinator.drain()
    assert calls == []


@pytest.mark.asyncio
async def test_cancelled_pending_input_is_not_handed_to_launch(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    startups = SessionStartupStore(db_uri)
    host = uuid4().hex
    startups.configure(host, host_resident_limit=8, deployment_resident_limit=8)
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, host, "/opt/work/project")
    [item] = conversations.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id=PENDING_INPUT_RESPONSE_ID,
                status="in_progress",
                stable_id=uuid4().hex,
                data=MessageData(role="user", content=[{"type": "input_text", "text": "work"}]),
            )
        ],
    )
    calls = []

    async def eligible(_startup):
        return True

    async def launch(startup):
        calls.append(startup.id)

    coordinator = StartupCoordinator(startups, conversations, eligible=eligible, launch=launch)
    queued = await coordinator.enqueue(
        conv.id, input_id=item.id, owner_user_id="owner", operation_key="cancelled-input"
    )
    assert conversations.transition_pending_input(
        conv.id, item.id, expected="in_progress", status=None
    )
    await coordinator.advance(host)
    await coordinator.drain()
    assert calls == []
    assert startups.get(queued.id).state == "cancelled"


@pytest.mark.asyncio
async def test_shutdown_during_dispatch_preflight_releases_only_unissued_permission(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    startups = SessionStartupStore(db_uri)
    host = uuid4().hex
    startups.configure(host, host_resident_limit=8, deployment_resident_limit=8)
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, host, "/opt/work/project")
    entered = asyncio.Event()

    async def eligible(_startup):
        return True

    async def launch(_startup):
        pytest.fail("A callback still in preflight must not be issued after shutdown")

    coordinator = StartupCoordinator(startups, conversations, eligible=eligible, launch=launch)
    queued = await coordinator.enqueue(
        conv.id, input_id=None, owner_user_id="owner", operation_key="shutdown-preflight"
    )
    valid_input = coordinator._valid_input

    async def delayed_preflight(startup):
        if startup.attempt_id is not None:
            entered.set()
            await asyncio.Future()
        return await valid_input(startup)

    coordinator._valid_input = delayed_preflight
    await coordinator.advance(host)
    await entered.wait()
    await coordinator.shutdown()
    current = startups.get(queued.id)
    assert current.state == "cancelled"
    assert not startups.claim(current.id, current.attempt_id, runtime_id="late-runtime")


@pytest.mark.asyncio
async def test_failed_dispatch_preflight_does_not_hold_a_phantom_runtime(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    startups = SessionStartupStore(db_uri)
    host = uuid4().hex
    startups.configure(host, host_resident_limit=1, deployment_resident_limit=1)
    conv = conversations.create_conversation()
    conversations.set_host_id(conv.id, host, "/opt/work/project")

    async def eligible(_startup):
        return True

    async def launch(_startup):
        pytest.fail("A failed preflight must not issue a launch")

    coordinator = StartupCoordinator(startups, conversations, eligible=eligible, launch=launch)
    queued = await coordinator.enqueue(
        conv.id, input_id=None, owner_user_id="owner", operation_key="preflight-failure"
    )
    valid_input = coordinator._valid_input

    async def failed_preflight(startup):
        if startup.attempt_id is not None:
            raise ConnectionError("input storage unavailable")
        return await valid_input(startup)

    coordinator._valid_input = failed_preflight
    await coordinator.advance(host)
    await coordinator.drain()
    assert startups.get(queued.id).state == "blocked"
    other = conversations.create_conversation()
    conversations.set_host_id(other.id, host, "/opt/work/project")
    replacement = await coordinator.enqueue(
        other.id, input_id=None, owner_user_id="owner", operation_key="next"
    )
    assert startups.reserve(host).id == replacement.id
