"""Host-load startup gates and durable release through existing consumer seams."""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.datastructures import Headers

from omnigent.errors import OmnigentError
from omnigent.host.frames import HostHelloFrame
from omnigent.server.host_load_queue import LABEL, HostLoadQueue, held_host, start_allowed
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes import sessions
from omnigent.server.routes._sessions import helpers, orchestration
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.server.schemas import SessionEventInput
from omnigent.server.startup_inputs import ManagedStartupInputs
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import build_agent_bundle, create_test_agent
from tests.server.test_host_registry import FakeWebSocket

HOST = "11111111111111111111111111111111"
OTHER = "22222222222222222222222222222222"
RUNNER = "33333333333333333333333333333333"
MESSAGE = {
    "type": "message",
    "data": {"role": "user", "content": [{"type": "input_text", "text": "first"}]},
}


@pytest.fixture
def queue_env(db_uri):
    store = SqlAlchemyConversationStore(db_uri)
    hosts = HostRegistry()
    connection = hosts.register(
        HOST,
        FakeWebSocket(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="sample-host"),
        owner=None,
        registered_with_managed_token=True,
    )
    inputs = ManagedStartupInputs(store, hosts)
    existing = ManagedStartupInputs(store, hosts)
    queue = HostLoadQueue(store)
    app = SimpleNamespace(
        state=SimpleNamespace(
            host_load_inputs=inputs, startup_inputs=existing, host_registry=hosts
        )
    )
    parent = store.create_conversation(
        host_id=HOST, workspace="/opt/work/project", runner_id=RUNNER
    )
    return SimpleNamespace(
        store=store,
        hosts=hosts,
        connection=connection,
        inputs=inputs,
        existing=existing,
        queue=queue,
        app=app,
        parent=parent,
    )


def child(env, *, marker=None, host=HOST):
    return env.store.create_conversation(
        parent_conversation_id=env.parent.id,
        kind="sub_agent",
        host_id=host,
        workspace="/opt/work/project" if host else None,
        runner_id=RUNNER,
        labels={LABEL: marker or f"queued@{HOST}"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "releasing"])
async def test_shared_worker_annotation_and_admission_saved_delivery_are_blocked(queue_env, state):
    """G10–G15: shared _deliver fences all its callers, including admission and annotations."""
    e = queue_env
    row = child(e, marker=f"{state}@{HOST}@1" if state == "releasing" else f"queued@{HOST}")
    item = await e.inputs.enqueue(
        row.id, SessionEventInput.model_validate(MESSAGE), user_id=None, headers=Headers()
    )
    e.existing.dispatch = AsyncMock()
    assert not await e.existing._recover_session(e.app, row.id, e.connection)
    for annotation in (False, True):
        assert (
            await e.existing._deliver(e.app, row.id, item, annotation=annotation)
            == "not_dispatched"
        )
    e.existing.dispatch.assert_not_awaited()
    assert e.store.get_item(row.id, item.id).status == "in_progress"
    assert not e.inputs._workers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry", ["initializer", "lazy_init", "host_launch", "resource_wake", "event_dispatch"]
)
async def test_shared_runtime_start_entries_refuse_held_rows(queue_env, entry):
    """G08,G09,G16–G18,G21,G22,G27: shared boundaries reject before contacting a runtime."""
    e = queue_env
    row = child(e)
    client = AsyncMock()
    with pytest.raises(OmnigentError, match="queued"):
        if entry == "initializer":
            await RunnerSessionInitializer.initialize(Mock(), row, client, timeout=1)
        elif entry == "lazy_init":
            await orchestration._ensure_runner_session_initialized(row.id, row, client, e.store)
        elif entry == "host_launch":
            await helpers._launch_runner_on_host_impl(row, e.store, e.hosts, e.connection)
        elif entry == "resource_wake":
            await orchestration.ensure_runner_connected(
                session_id=row.id,
                conv=row,
                app_state=e.app.state,
                conversation_store=e.store,
                runner_router=None,
            )
        else:
            await orchestration._dispatch_session_event_to_runner_impl(
                row.id,
                row,
                SessionEventInput.model_validate(MESSAGE),
                e.store,
                client,
                agent_name=None,
                file_store=None,
                artifact_store=None,
            )
    assert not client.mock_calls


@pytest.mark.asyncio
async def test_release_drains_in_order_and_again_after_label_removal(queue_env, monkeypatch):
    """T17/G29: hostless children work, including a message saved at label removal."""
    e = queue_env
    row = child(e, host=None)
    e.store.set_labels(
        row.id,
        {"omnigent.initial_message": json.dumps({"status": "queued", "reason": "host_load"})},
    )
    texts = []
    starts = []

    async def start(app, conv, host, was_queued):
        assert start_allowed(conv)
        assert not start_allowed(child(e))
        starts.append(conv.id)

    async def dispatch(context, session_id, event):
        assert await context.claim(e.store, session_id)
        texts.append(event.data["content"][0]["text"])
        # Return no item id: the saved-input owner writes its normal receipt.
        return {}

    e.inputs.dispatch = dispatch
    monkeypatch.setattr(e.queue, "_start", start)
    await e.inputs.enqueue(
        row.id, SessionEventInput.model_validate(MESSAGE), user_id=None, headers=Headers()
    )
    added = False

    async def drain(app, session_id):
        nonlocal added
        if not added and held_host(e.store.get_conversation(session_id)) is None:
            added = True
            second = SessionEventInput.model_validate(
                {
                    **MESSAGE,
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "second"}],
                    },
                }
            )
            await e.inputs.enqueue(
                session_id, second, user_id=None, headers=Headers(), first=False
            )
        return await ManagedStartupInputs.deliver_session(e.inputs, app, session_id)

    monkeypatch.setattr(e.inputs, "deliver_session", drain)
    assert await e.queue.release(e.app, row.id, HOST)
    assert not await e.queue.release(e.app, row.id, HOST)
    assert texts == ["first", "second"]
    assert starts == [row.id]
    assert held_host(e.store.get_conversation(row.id)) is None
    assert json.loads(e.store.get_conversation(row.id).labels["omnigent.initial_message"]) == {
        "status": "sent"
    }
    assert e.store.list_pending_inputs(row.id) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertain", [False, True])
async def test_release_failure_keeps_durable_state(queue_env, monkeypatch, uncertain):
    """T19: definite failure retries; timeout preserves releasing without a second start."""
    e = queue_env
    row = child(e)
    start = AsyncMock(
        side_effect=TimeoutError("lost response") if uncertain else RuntimeError("rejected")
    )
    monkeypatch.setattr(e.queue, "_start", start)
    assert await e.queue.release(e.app, row.id, HOST)
    marker = e.store.get_conversation(row.id).labels[LABEL]
    assert marker.startswith("releasing@" if uncertain else "queued@")
    if uncertain:
        assert not await e.queue.release(e.app, row.id, HOST)
    else:
        start.side_effect = None
        assert await e.queue.release(e.app, row.id, HOST)
        assert held_host(e.store.get_conversation(row.id)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", ["archived", "closed"])
async def test_archived_queued_child_cancels_inputs_without_start(
    queue_env, monkeypatch, cancelled
):
    """T18."""
    e = queue_env
    row = child(e)
    await e.inputs.enqueue(
        row.id, SessionEventInput.model_validate(MESSAGE), user_id=None, headers=Headers()
    )
    if cancelled == "archived":
        e.store.update_conversation(row.id, archived=True)
    else:
        from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

        e.store.set_labels(row.id, {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE})
    start = AsyncMock()
    monkeypatch.setattr(e.queue, "_start", start)
    assert not await e.queue.release(e.app, row.id, HOST)
    start.assert_not_awaited()
    assert held_host(e.store.get_conversation(row.id)) is None
    assert e.store.list_pending_inputs(row.id) == []


@pytest.mark.asyncio
async def test_tick_two_per_host_rollback_and_restart_scan(queue_env, monkeypatch):
    """T17/rollback: disabling new holds still drains rows found after a restart."""
    e = queue_env
    rows = [child(e) for _ in range(3)]
    hub = SimpleNamespace(
        host_load_keys=lambda: [(0, HOST)],
        set_queued_children=Mock(),
        queue_enabled=lambda workspace: False,
        host_load=lambda *a: {"level": "new_session_hold"},
    )
    start = AsyncMock()
    monkeypatch.setattr(e.queue, "_start", start)
    await e.queue.tick(hub, e.app)
    await asyncio.gather(*list(e.queue._tasks.values()))
    assert start.await_count == 2
    await e.queue.tick(hub, e.app)
    await asyncio.gather(*list(e.queue._tasks.values()))
    assert start.await_count == 3
    assert all(held_host(e.store.get_conversation(row.id)) is None for row in rows)


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["inherited", "explicit", "cross", "bundle"])
async def test_agent_create_held_without_notify_or_launch_and_first_inputs_pending(
    client, app, db_uri, monkeypatch, placement
):
    """T14,T15/G01–G05,G23–G26: real create, event, snapshot and registration consumers."""
    store = SqlAlchemyConversationStore(db_uri)
    agent = await create_test_agent(client, name="load-parent")
    parent = store.create_conversation(
        agent_id=agent["id"], host_id=HOST, workspace="/opt/work/project", runner_id=RUNNER
    )
    hub = SimpleNamespace(
        queue_enabled=lambda workspace: True,
        host_load=lambda *args: {
            "level": "new_session_hold",
            "load_per_core": 6,
            "lines": {"heavy": 4, "new_session": 5},
        },
    )
    app.state.system_status = hub
    app.state.host_store = HostStore(db_uri)
    app.state.host_store.upsert_on_connect(HOST, "parent-host", "local")
    app.state.host_store.upsert_on_connect(OTHER, "other-host", "local")
    monkeypatch.setattr(
        orchestration, "_validate_session_workspace", AsyncMock(return_value="/opt/work/project")
    )
    notify = AsyncMock(return_value=None)
    monkeypatch.setattr(sessions, "_get_runner_client", notify)
    monkeypatch.setattr(
        "omnigent.server.routes._host_launch.resolve_host_launch",
        Mock(side_effect=AssertionError("held create launched a host")),
    )
    app.state.runner_session_initializer.initialize = AsyncMock()
    headers = {"X-Omnigent-Create-Origin": "agent"}
    body = {"agent_id": agent["id"], "parent_session_id": parent.id}
    if placement in {"explicit", "cross"}:
        body.update(host_id=OTHER if placement == "cross" else HOST, workspace="/opt/work/project")
    if placement == "bundle":
        body.pop("agent_id")
        response = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps(body)},
            files={
                "bundle": ("agent.tar.gz", build_agent_bundle("load-child"), "application/gzip")
            },
            headers=headers,
        )
    else:
        body["initial_items"] = [MESSAGE]
        response = await client.post("/v1/sessions", json=body, headers=headers)
    assert response.status_code == 201, response.text
    data = response.json()
    session_id = data.get("id") or data["session_id"]
    target = OTHER if placement == "cross" else HOST
    assert data["labels"][LABEL] == f"queued@{target}"
    assert data["startup"]["status"] == "queued"
    app.state.runner_session_initializer.initialize.assert_not_awaited()
    assert len(store.list_pending_inputs(session_id)) == (0 if placement == "bundle" else 1)
    posted = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            **MESSAGE,
            "data": {"role": "user", "content": [{"type": "input_text", "text": "later"}]},
        },
    )
    assert posted.status_code == 202, posted.text
    assert posted.json()["queued"]
    snapshot = await client.get(f"/v1/sessions/{session_id}")
    assert snapshot.status_code == 200, snapshot.text
    assert snapshot.json()["startup_hold"]["host_id"] == target
    from omnigent.runner import subagent_work, tool_dispatch

    tool_data = {**data, "id": session_id}
    try:
        result = json.loads(
            await tool_dispatch._complete_created_session(
                tool_data,
                server_client=client,
                conversation_id=parent.id,
                agent_id=data["agent_id"],
                title="held",
                publish_event=None,
                message="tool first message",
            )
        )
        assert result["initial_message"] == {"status": "queued", "reason": "host_load"}
        assert result["startup"]["status"] == "queued"
        info = await tool_dispatch._session_info_item(
            session_id, client, include_host_readiness=False
        )
        assert info["startup_hold"]["host_id"] == target
    finally:
        subagent_work.unregister_child_session(session_id)
        subagent_work.unregister_subagent_work(session_id)
    forged = await client.patch(
        f"/v1/sessions/{session_id}", json={"labels": {LABEL: "queued@forged"}}
    )
    assert forged.status_code == 400, forged.text
    assert not app.state.host_load_inputs._workers


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_host", [False, True])
async def test_release_sends_skipped_start_only_once(queue_env, monkeypatch, cross_host):
    e = queue_env
    target = OTHER if cross_host else HOST
    e.hosts.register(
        target,
        FakeWebSocket(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="target-host"),
        owner=None,
    )
    row = e.store.create_conversation(
        parent_conversation_id=e.parent.id,
        kind="sub_agent",
        host_id=target,
        workspace="/opt/work/project",
        runner_id=None if cross_host else RUNNER,
        labels={LABEL: f"queued@{target}"},
    )
    client = object()
    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=None))
    monkeypatch.setattr(sessions, "_wait_for_runner_client", AsyncMock(return_value=client))

    async def launch(conv, *args):
        e.store.set_runner_id(conv.id, RUNNER)
        return SimpleNamespace(error=None, error_code=None)

    launch_mock = AsyncMock(side_effect=launch)
    monkeypatch.setattr(sessions, "_launch_runner_on_host", launch_mock)
    initialize = AsyncMock(
        return_value=httpx.Response(
            201, request=httpx.Request("POST", "http://runner/v1/sessions")
        )
    )
    e.app.state.runner_router = None
    e.app.state.tunnel_registry = None
    e.app.state.runner_session_initializer = SimpleNamespace(initialize=initialize)
    await e.queue.release(e.app, row.id, target)
    await e.queue.release(e.app, row.id, target)
    assert launch_mock.await_count == int(cross_host)
    assert initialize.await_count == 1


@pytest.mark.asyncio
async def test_human_unknown_and_queue_disabled_creates_are_not_held(
    client, app, db_uri, monkeypatch
):
    store = SqlAlchemyConversationStore(db_uri)
    agent = await create_test_agent(client, name="normal-parent")
    parent = store.create_conversation(
        agent_id=agent["id"], host_id=HOST, workspace="/opt/work/project"
    )
    for origin, enabled, level in [
        ("human", True, "new_session_hold"),
        ("agent", True, "unknown"),
        ("agent", False, "new_session_hold"),
    ]:
        app.state.system_status = SimpleNamespace(
            queue_enabled=lambda workspace, enabled=enabled: enabled,
            host_load=lambda *a, level=level: {
                "level": level,
                "load_per_core": None,
                "lines": {"heavy": 4, "new_session": 5},
            },
        )
        result = await client.post(
            "/v1/sessions",
            json={"agent_id": agent["id"], "parent_session_id": parent.id},
            headers={"X-Omnigent-Create-Origin": origin},
        )
        assert result.status_code == 201, result.text
        assert LABEL not in result.json()["labels"]


@pytest.mark.asyncio
async def test_input_saved_before_unhold_is_caught_by_final_drain(queue_env, monkeypatch):
    from omnigent.entities import NewConversationItem
    from omnigent.entities.conversation import MessageData
    from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID

    e = queue_env
    row = child(e, host=None)
    delivered = []
    monkeypatch.setattr(e.queue, "_start", AsyncMock())
    original = e.store.compare_and_set_label

    def save_then_delete(session_id, key, expected, value):
        if key == LABEL and value is None:
            e.store.append(
                session_id,
                [
                    NewConversationItem(
                        type="message",
                        response_id=PENDING_INPUT_RESPONSE_ID,
                        status="in_progress",
                        data=MessageData(
                            role="user", content=[{"type": "input_text", "text": "before delete"}]
                        ),
                    )
                ],
                while_label=LABEL,
            )
        return original(session_id, key, expected, value)

    async def dispatch(context, session_id, event):
        assert held_host(e.store.get_conversation(session_id)) is None
        assert await context.claim(e.store, session_id)
        delivered.append(event.data["content"][0]["text"])
        return {}

    e.inputs.dispatch = dispatch
    monkeypatch.setattr(e.store, "compare_and_set_label", save_then_delete)
    await e.queue.release(e.app, row.id, HOST)
    assert delivered == ["before delete"]
    assert not e.store.list_pending_inputs(row.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("read_held", [False, True])
async def test_event_after_unhold_waits_for_older_final_drain(
    client, app, db_uri, monkeypatch, read_held
):
    from omnigent.entities import NewConversationItem
    from omnigent.entities.conversation import MessageData
    from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
    from tests.server.integration.test_sessions_web_send_stable_id import _stub_runner

    store = SqlAlchemyConversationStore(db_uri)
    agent = await create_test_agent(client, name="late-save")
    parent = store.create_conversation(agent_id=agent["id"])
    row = store.create_conversation(
        agent_id=agent["id"],
        parent_conversation_id=parent.id,
        kind="sub_agent",
        labels={LABEL: f"queued@{HOST}"},
    )
    app.state.startup_inputs = None
    inputs = app.state.host_load_inputs
    enqueue_entered, enqueue_proceed = asyncio.Event(), asyncio.Event()
    final_entered, final_proceed, gate_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    enqueue, drain, barrier = inputs.enqueue, inputs.deliver_session, inputs.session_barrier
    original_cas = store.compare_and_set_label
    forwarded = []

    async def delayed_enqueue(*args, **kwargs):
        enqueue_entered.set()
        await enqueue_proceed.wait()
        return await enqueue(*args, **kwargs)

    drain_calls = 0

    async def delayed_drain(*args):
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 2:
            final_entered.set()
            await final_proceed.wait()
        return await drain(*args)

    @asynccontextmanager
    async def observed_barrier(session_id):
        if held_host(store.get_conversation(session_id)) is None:
            gate_entered.set()
        async with barrier(session_id):
            yield

    def save_then_delete(session_id, key, expected, value):
        if key == LABEL and value is None:
            store.append(
                session_id,
                [
                    NewConversationItem(
                        type="message",
                        response_id=PENDING_INPUT_RESPONSE_ID,
                        status="in_progress",
                        data=MessageData(
                            role="user", content=[{"type": "input_text", "text": "older"}]
                        ),
                    )
                ],
                while_label=LABEL,
            )
        return original_cas(session_id, key, expected, value)

    async def dispatch(context, session_id, event):
        assert await context.claim(store, session_id)
        forwarded.append({"type": event.type, **event.data})
        return {}

    monkeypatch.setattr(inputs, "enqueue", delayed_enqueue)
    monkeypatch.setattr(inputs, "deliver_session", delayed_drain)
    monkeypatch.setattr(inputs, "session_barrier", observed_barrier)
    monkeypatch.setattr(store, "compare_and_set_label", save_then_delete)
    # The queue and event route share the store instance through the app.
    monkeypatch.setattr(app.state.host_load_queue, "conversations", store)
    monkeypatch.setattr(app.state.host_load_queue, "_start", AsyncMock())
    inputs.dispatch = dispatch
    runner = _stub_runner(monkeypatch, forwarded)
    request_task = release_task = None
    try:
        if read_held:
            request_task = asyncio.create_task(
                client.post(f"/v1/sessions/{row.id}/events", json=MESSAGE)
            )
            await asyncio.wait_for(enqueue_entered.wait(), 5)
        release_task = asyncio.create_task(app.state.host_load_queue.release(app, row.id, HOST))
        await asyncio.wait_for(final_entered.wait(), 5)
        assert not held_host(store.get_conversation(row.id))
        if request_task is None:
            request_task = asyncio.create_task(
                client.post(f"/v1/sessions/{row.id}/events", json=MESSAGE)
            )
        enqueue_proceed.set()
        await asyncio.wait_for(gate_entered.wait(), 5)
        assert not request_task.done()
        assert forwarded == []
        final_proceed.set()
        await asyncio.wait_for(release_task, 5)
        result = await asyncio.wait_for(request_task, 5)
        assert result.status_code == 202, result.text
        assert [
            event["content"][0]["text"] for event in forwarded if event.get("type") == "message"
        ] == ["older", "first"]
        assert not store.list_pending_inputs(row.id)
    finally:
        enqueue_proceed.set()
        final_proceed.set()
        await asyncio.gather(
            *(task for task in (request_task, release_task) if task), return_exceptions=True
        )
        await runner.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("hostless", [False, True])
@pytest.mark.parametrize("at_capacity", [False, True])
async def test_release_hands_off_at_admission_capacity_and_delivers_in_order(
    queue_env, db_uri, monkeypatch, hostless, at_capacity
):
    from omnigent.entities.session_startup import StartupReceipt
    from omnigent.server.server_config import StartupAdmissionPolicy
    from omnigent.server.startup_admission import StartupAdmission
    from omnigent.stores.session_startup_store import SessionStartupStore

    e = queue_env
    target = HOST if hostless else OTHER
    conn = e.hosts.register(
        target,
        FakeWebSocket(),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name="admission-target",
            capabilities=["startup_admission_v1", "startup_receipts:test"],
        ),
        owner=None,
    )
    startups = SessionStartupStore(db_uri)
    admission = StartupAdmission(startups, e.store, e.hosts, StartupAdmissionPolicy(8, 8, 1))
    admission.bind(e.app)
    admission.resolve_harness = AsyncMock(return_value="test")
    monkeypatch.setattr(admission, "inventory_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(e.existing, "host_changed", Mock())
    e.app.state.runner_router = None
    e.app.state.tunnel_registry = None
    # A ready parent on the original host satisfies the admission dependency.
    startups.configure(HOST, host_resident_limit=8, deployment_resident_limit=8, startup_limit=1)
    parent_start = startups.enqueue(
        owner_user_id="__local__",
        host_id=HOST,
        session_id=e.parent.id,
        operation_key="parent",
        input_id=None,
    )
    parent_attempt = startups.reserve(HOST)
    assert startups.claim(parent_start.id, parent_attempt.attempt_id, runtime_id="parent-runtime")
    for phase in ("runtime_ready", "tools_ready"):
        assert startups.observe(
            StartupReceipt(
                startup_id=parent_start.id,
                attempt_id=parent_attempt.attempt_id,
                input_id=None,
                runtime_id="parent-runtime",
                source="runner",
                phase=phase,
            )
        )
    row = e.store.create_conversation(
        parent_conversation_id=e.parent.id,
        kind="sub_agent",
        host_id=None if hostless else target,
        workspace=None if hostless else "/opt/work/project",
        runner_id=RUNNER if hostless else None,
        labels={
            LABEL: f"queued@{target}",
            "omnigent.initial_message": json.dumps({"status": "queued", "reason": "host_load"}),
        },
    )
    for text in ("first", "second"):
        await e.inputs.enqueue(
            row.id,
            SessionEventInput(
                type="message",
                data={"role": "user", "content": [{"type": "input_text", "text": text}]},
            ),
            user_id=None,
            headers=Headers(),
            first=False,
        )
    startups.configure(target, host_resident_limit=8, deployment_resident_limit=8, startup_limit=1)
    if at_capacity:
        blocker = e.store.create_conversation(host_id=target, workspace="/opt/work/project")
        blocked = startups.enqueue(
            owner_user_id="__local__",
            host_id=target,
            session_id=blocker.id,
            operation_key="blocker",
            input_id=None,
        )
        blocker_attempt = startups.reserve(target)
        assert blocker_attempt.id == blocked.id
    launches, delivered = [], []
    queue_session = admission.queue_session

    async def queue_then_try_advance(connection, session_id):
        queued = await queue_session(connection, session_id)
        # A host worker can advance before release deletes its label, even with free capacity.
        await admission.advance(target)
        await admission.coordinator.drain()
        pending = startups.for_session(session_id)
        assert pending.state == "queued" and not pending.occupied and not pending.resident
        assert launches == delivered == []
        return queued

    monkeypatch.setattr(admission, "queue_session", queue_then_try_advance)

    async def launcher(conv, store, hosts, connection):
        ref = await hosts.admit_launch(connection, conv.id)
        launches.append(conv.id)
        assert startups.claim_boundary(
            ref.startup_id,
            ref.attempt_id,
            runtime_id=ref.runtime_id,
            host_generation=ref.host_generation,
            boundary="runner_prepare",
            claim_id="runner",
        )
        return SimpleNamespace(error=None, error_code=None)

    monkeypatch.setattr(
        sessions, "_get_runner_client", AsyncMock(return_value=object() if hostless else None)
    )
    monkeypatch.setattr(sessions, "_launch_runner_on_host", launcher)

    async def dispatch(context, session_id, event):
        if context.startup is not None:
            await launcher(e.store.get_conversation(session_id), e.store, e.hosts, conn)
        assert await context.claim(e.store, session_id)
        delivered.append(event.data["content"][0]["text"])
        return {}

    e.existing.dispatch = dispatch
    try:
        await e.queue.release(e.app, row.id, target)
        assert held_host(e.store.get_conversation(row.id)) is None
        assert e.store.get_conversation(row.id).host_id == target
        queued = startups.for_session(row.id)
        assert queued is not None and queued.state == "queued"
        if at_capacity:
            await admission.advance(target)
            await admission.coordinator.drain()
            assert launches == delivered == []
            assert startups.confirm_stopped(blocked.id, blocker_attempt.attempt_id)
        await admission.advance(target)
        await admission.coordinator.drain()
        assert launches == [row.id] and delivered == ["first"]
        current = startups.for_session(row.id)
        assert startups.bind_dispatch(
            current.id,
            current.attempt_id,
            runtime_id=current.runtime_id,
            source="test",
            vendor_session_id="session",
            vendor_turn_id="turn",
        )
        for phase in ("runtime_ready", "tools_ready", "input_accepted", "work_started"):
            assert startups.observe(
                StartupReceipt(
                    startup_id=current.id,
                    attempt_id=current.attempt_id,
                    input_id=current.input_id,
                    runtime_id=current.runtime_id,
                    source="test",
                    vendor_session_id="session",
                    vendor_turn_id="turn",
                    phase=phase,
                )
            )
        assert startups.for_session(row.id).state == "working"
        assert await e.existing._recover_session(e.app, row.id, conn)
        await asyncio.gather(*list(e.existing._deliveries.values()))
        assert delivered == ["first", "second"]
        assert launches == [row.id]
        assert not e.store.list_pending_inputs(row.id)
        assert json.loads(e.store.get_conversation(row.id).labels["omnigent.initial_message"]) == {
            "status": "sent"
        }
    finally:
        await admission.shutdown()
        await e.inputs.shutdown()
        await e.existing.shutdown()


@pytest.mark.asyncio
async def test_model_wake_and_admission_launch_keep_the_entry_guard(queue_env):
    from omnigent.server.routes.sessions.routes_core import _wake_runner_for_model_change
    from omnigent.server.startup_admission import StartupAdmission

    e = queue_env
    row = child(e)
    with pytest.raises(OmnigentError, match="queued"):
        await _wake_runner_for_model_change(Mock(), row, e.store, None)
    service = SimpleNamespace(conversations=e.store, hosts=Mock(), startups=Mock())
    await StartupAdmission._launch(
        service, SimpleNamespace(session_id=row.id, id="startup", attempt_id="attempt")
    )
    service.hosts.get.assert_not_called()
    service.startups.cancel_unissued.assert_called_once_with("startup", "attempt")


def test_direct_host_launch_resolver_requires_unheld_row(queue_env, db_uri):
    from omnigent.server.routes._host_launch import resolve_host_launch

    e = queue_env
    row = child(e)
    hosts = HostStore(db_uri)
    hosts.upsert_on_connect(HOST, "resolve-host", "local")
    args = {
        "user_id": None,
        "host_id": HOST,
        "session_id": row.id,
        "host_store": hosts,
        "host_registry": e.hosts,
        "conversation_store": e.store,
        "permission_store": None,
    }
    with pytest.raises(OmnigentError, match="queued"):
        resolve_host_launch(**args)
    e.store.delete_label(row.id, LABEL)
    assert resolve_host_launch(**args).conv.id == row.id


@pytest.mark.asyncio
async def test_startup_registered_only_clears_registration_barrier(client, app, db_uri):
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.server.startup_registration import STARTUP_REGISTRATION_LABEL_KEY

    store = SqlAlchemyConversationStore(db_uri)
    agent = await create_test_agent(client, name="registered-held")
    binding = "host-load-registration-test-binding"
    parent = store.create_conversation(
        agent_id=agent["id"], runner_id=token_bound_runner_id(binding)
    )
    row = store.create_conversation(
        agent_id=agent["id"],
        parent_conversation_id=parent.id,
        kind="sub_agent",
        labels={LABEL: f"queued@{HOST}", STARTUP_REGISTRATION_LABEL_KEY: "pending"},
    )
    initialize = AsyncMock()
    app.state.runner_session_initializer.initialize = initialize
    result = await client.post(
        f"/v1/sessions/{row.id}/startup/registered", headers={RUNNER_TUNNEL_TOKEN_HEADER: binding}
    )
    assert result.status_code == 200, result.text
    assert store.get_conversation(row.id).labels == {LABEL: f"queued@{HOST}"}
    initialize.assert_not_awaited()
    assert not app.state.host_load_inputs._workers


@pytest.mark.asyncio
async def test_human_child_notifies_existing_runner_at_high_load(client, app, db_uri, monkeypatch):
    store = SqlAlchemyConversationStore(db_uri)
    agent = await create_test_agent(client, name="human-start")
    parent = store.create_conversation(
        agent_id=agent["id"], runner_id=RUNNER, host_id=HOST, workspace="/opt/work/project"
    )
    app.state.system_status = SimpleNamespace(
        queue_enabled=lambda workspace: True, host_load=lambda *args: {"level": "new_session_hold"}
    )
    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=object()))
    initialize = AsyncMock()
    app.state.runner_session_initializer.initialize = initialize
    result = await client.post(
        "/v1/sessions", json={"agent_id": agent["id"], "parent_session_id": parent.id}
    )
    assert result.status_code == 201, result.text
    assert LABEL not in result.json()["labels"]
    initialize.assert_awaited_once()


@pytest.mark.asyncio
async def test_stale_releasing_is_retried_and_connection_replacement_is_fenced(
    queue_env, monkeypatch
):
    e = queue_env
    stale = child(e, marker=f"releasing@{HOST}@1")
    start = AsyncMock()
    monkeypatch.setattr(e.queue, "_start", start)
    await e.queue.release(e.app, stale.id, HOST)
    assert held_host(e.store.get_conversation(stale.id)) is None
    start.assert_awaited_once()
    queued = child(e)
    e.hosts.register(
        HOST,
        FakeWebSocket(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="replacement"),
        owner=None,
    )
    await e.queue._release_host(e.app, 0, HOST, [queued], e.connection)
    assert held_host(e.store.get_conversation(queued.id)) == HOST
    start.assert_awaited_once()
