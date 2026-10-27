"""Release preserves saved input; archive is checked at the delivery boundary."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.server.server_config import StartupAdmissionPolicy
from omnigent.server.startup_admission import StartupAdmission
from omnigent.server.startup_inputs import ManagedStartupInputs, StartupInputContext
from omnigent.server.startup_observations import accept_observation
from omnigent.startup_protocol import StartupObservation
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.session_startup_store import SessionStartupStore
from tests.server.test_startup_observations import _startup
from tests.stores.test_startup_operator_release import opening, release


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["operator_released", "queued", "uncertain", "opened"])
async def test_disabled_queue_preserves_unconfirmed_old_input(app, db_uri, monkeypatch, state):
    from starlette.datastructures import Headers

    conversations = SqlAlchemyConversationStore(db_uri)
    startups, conv, item, attempt, body = opening(conversations)
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 180
    )
    if state == "operator_released":
        release(startups, conv.host_id, body)
    elif state == "opened":
        from omnigent.entities.session_startup import StartupReceipt

        assert startups.observe(
            StartupReceipt(
                startup_id=attempt.id,
                attempt_id=attempt.attempt_id,
                input_id=item.id,
                runtime_id=attempt.runtime_id,
                phase="opening_settled",
                source="runner",
            )
        )
    else:
        from omnigent.db.db_models import current_workspace_id
        from omnigent.db.session_startup import SqlSessionStartup

        with startups._session("seed_disabled_startup_state") as db:
            row = db.get(SqlSessionStartup, (current_workspace_id(), attempt.id))
            row.state = state
            if state == "queued":
                row.occupied = row.resident = False
                row.attempt_id = row.runtime_id = None
                row.admitted_at = row.first_dispatch_at = None
                row.host_generation = None
                row.cold_claims = {}
    assert app.state.startup_admission is None
    context = StartupInputContext(app, Headers({}), None, item.id)
    assert not await context.claim(conversations, conv.id)
    assert conversations.get_item(conv.id, item.id).status == "in_progress"
    assert await app.state.host_load_inputs.accepts(conversations.get_conversation(conv.id), app)
    inputs = app.state.host_load_inputs
    dispatch = AsyncMock()
    original_dispatch = inputs.dispatch
    inputs.dispatch = dispatch
    try:
        assert not await inputs._recover_session(
            app, conv.id, SimpleNamespace(host_id=conv.host_id)
        )
        dispatch.assert_not_awaited()
        assert not inputs._deliveries
    finally:
        inputs.dispatch = original_dispatch


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["owner", "manager", "other-owner"])
@pytest.mark.parametrize("session_level", [3, 4])
async def test_release_endpoint_requires_host_and_session_owner(
    auth_app, auth_client, db_uri, monkeypatch, actor, session_level
):
    conversations = SqlAlchemyConversationStore(db_uri)
    startups, conv, item, attempt, body = opening(conversations)
    permissions = SqlAlchemyPermissionStore(db_uri)
    for user, level in (("owner", session_level), ("manager", 3), ("other-owner", 4)):
        permissions.ensure_user(f"{user}@example.test")
        permissions.grant(f"{user}@example.test", conv.id, level)
    HostStore(db_uri).upsert_on_connect(
        conv.host_id, "sample-host", "owner@example.test", allow_host_id_reown=True
    )
    auth_app.state.startup_inputs = ManagedStartupInputs(
        conversations, auth_app.state.host_registry
    )
    admission = StartupAdmission(
        startups,
        conversations,
        auth_app.state.host_registry,
        StartupAdmissionPolicy(None, None, 2, "startup_only"),
    )
    admission.bind(auth_app)
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 180
    )
    route = f"/v1/hosts/{conv.host_id}/startup-releases"
    headers = {"X-Forwarded-Email": f"{actor}@example.test"}
    before = startups.get(attempt.id)
    preview = await auth_client.get(route, headers=headers)
    if actor != "owner":
        assert preview.status_code == 403, preview.text
    else:
        assert preview.status_code == 200, preview.text
        expected = (
            [{"target": body.targets[0].model_dump(), "title": conv.title, "work_hint": "unknown"}]
            if session_level == 4
            else []
        )
        assert preview.json() == {"current_host_generation": "generation", "targets": expected}
    assert startups.get(attempt.id) == before
    response = await auth_client.post(route, json=body.model_dump(), headers=headers)
    if actor != "owner" or session_level != 4:
        assert response.status_code == 403, response.text
        assert startups.get(attempt.id).occupied
    else:
        assert response.status_code == 200, response.text
        duplicate = await auth_client.post(
            route, json=body.model_dump(), headers={"X-Forwarded-Email": "owner@example.test"}
        )
        assert duplicate.json() == response.json()
        assert startups.get(attempt.id).state == "operator_released"
        assert conversations.get_item(conv.id, item.id).status == "in_progress"
        assert (await auth_client.get(route, headers=headers)).json()["targets"] == []
    await admission.shutdown()
    await auth_app.state.startup_inputs.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("hint", ["working", "unknown"])
async def test_preview_hints_do_not_veto_targets_or_expose_archived_sessions(
    app, client, db_uri, monkeypatch, hint
):
    conversations, startups, hosts, conn, conv, _item, ref, _runner = _startup(
        app, db_uri, mode="startup_only"
    )
    HostStore(db_uri).upsert_on_connect(conn.host_id, "sample-host", "local")
    app.state.startup_inputs = ManagedStartupInputs(conversations, hosts)
    admission = StartupAdmission(
        startups, conversations, hosts, StartupAdmissionPolicy(None, None, 2, "startup_only")
    )
    admission.bind(app)
    observation = SimpleNamespace(
        live_turn=True, live_tool=False, background_job=False, protected_interaction=False
    )
    inventory = SimpleNamespace(
        sessions=[
            SimpleNamespace(
                session_id=conv.id,
                runner=SimpleNamespace(inventory=SimpleNamespace(observations=[observation])),
            )
        ]
    )
    read = AsyncMock(return_value=inventory if hint == "working" else None)
    monkeypatch.setattr(
        "omnigent.server.resident_inventory.read_host_session_work_inventory_v2", read
    )
    attempt = startups.get(ref.startup_id)
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 179
    )
    route = f"/v1/hosts/{conn.host_id}/startup-releases"
    assert (await client.get(route)).json()["targets"] == []
    read.assert_not_awaited()
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 180
    )
    response = await client.get(route)
    assert response.status_code == 200, response.text
    preview = response.json()
    assert len(preview["targets"]) == 1
    assert preview["targets"][0]["work_hint"] == hint
    conversations.update_conversation(conv.id, archived=True)
    assert (await client.get(route)).json()["targets"] == []
    conversations.update_conversation(conv.id, archived=False)
    response = await client.post(
        route,
        json={
            "current_host_generation": preview["current_host_generation"],
            "targets": [item["target"] for item in preview["targets"]],
        },
    )
    assert response.status_code == 200, response.text
    assert startups.get(attempt.id).state == "operator_released"
    await admission.shutdown()
    await app.state.startup_inputs.shutdown()


@pytest.mark.asyncio
async def test_archive_after_release_preflight_does_not_veto_the_accounting_commit(
    app, client, db_uri, monkeypatch
):
    conversations = SqlAlchemyConversationStore(db_uri)
    startups, conv, _item, attempt, body = opening(conversations)
    app.state.startup_inputs = ManagedStartupInputs(conversations, app.state.host_registry)
    admission = StartupAdmission(
        startups,
        conversations,
        app.state.host_registry,
        StartupAdmissionPolicy(None, None, 2, "startup_only"),
    )
    admission.bind(app)
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 180
    )
    original = startups.release_startups

    def archive_then_release(*args, **kwargs):
        conversations.update_conversation(conv.id, archived=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(startups, "release_startups", archive_then_release)
    response = await client.post(
        f"/v1/hosts/{conv.host_id}/startup-releases", json=body.model_dump()
    )
    assert response.status_code == 200, response.text
    assert conversations.get_conversation(conv.id).archived
    assert not startups.get(attempt.id).occupied
    await admission.shutdown()
    await app.state.startup_inputs.shutdown()


@pytest.mark.asyncio
async def test_release_holds_original_and_later_input_until_exact_late_acceptance(
    app, client, db_uri, monkeypatch
):
    conversations, startups, hosts, conn, conv, item, ref, runner_id = _startup(
        app, db_uri, mode="startup_only"
    )
    HostStore(db_uri).upsert_on_connect(conn.host_id, "sample-host", "local")
    from omnigent.entities.session_startup import StartupReleaseRequest, StartupReleaseTarget

    attempt = startups.get(ref.startup_id)
    body = StartupReleaseRequest(
        current_host_generation=conn.startup_generation,
        targets=[
            StartupReleaseTarget(
                startup_id=attempt.id,
                session_id=conv.id,
                attempt_id=attempt.attempt_id,
                runtime_id=attempt.runtime_id,
                input_id=item.id,
                host_generation=attempt.host_generation,
                runner_id=runner_id,
            )
        ],
    )
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 180
    )
    release(startups, conn.host_id, body)
    [next_item] = conversations.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id=PENDING_INPUT_RESPONSE_ID,
                status="in_progress",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "next"}]),
            )
        ],
    )
    # A new store/service observes the durable hold; no in-memory release cache is needed.
    inputs = ManagedStartupInputs(conversations, hosts)
    app.state.startup_inputs = inputs
    admission = StartupAdmission(
        SessionStartupStore(db_uri),
        conversations,
        hosts,
        StartupAdmissionPolicy(None, None, 2, "startup_only"),
    )
    admission.bind(app)
    inputs.dispatch = AsyncMock()
    assert await inputs.accepts(conversations.get_conversation(conv.id), app)
    assert not await inputs._recover_session(app, conv.id, conn)
    inputs.dispatch.assert_not_awaited()
    observation = StartupObservation(
        attempt=ref,
        phase="input_accepted",
        source="codex",
        vendor_session_id="thread",
        vendor_turn_id="turn",
    )
    assert await accept_observation(app, conversations, conv.id, runner_id, observation)
    assert await accept_observation(app, conversations, conv.id, runner_id, observation)
    assert conversations.get_item(conv.id, item.id).response_id != PENDING_INPUT_RESPONSE_ID
    assert [saved.id for saved in conversations.list_pending_inputs(conv.id)] == [next_item.id]
    delivered = asyncio.Event()

    async def dispatch(context, session_id, event):
        assert context.input_id == next_item.id
        assert context.startup is None
        assert await context.claim(conversations, session_id)
        delivered.set()
        return {"pending_id": "next-pending"}

    inputs.dispatch = dispatch
    assert await inputs._recover_session(app, conv.id, conn)
    await asyncio.wait_for(delivered.wait(), timeout=3)
    assert startups.get(attempt.id).state == "operator_released"
    assert not startups.get(attempt.id).occupied
    await inputs.shutdown()
    await admission.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("archive_during_dispatch", [False, True])
async def test_archived_held_input_never_claims_or_forwards(
    db_uri, tmp_path, monkeypatch, split, archive_during_dispatch
):
    conversations = SqlAlchemyConversationStore(
        db_uri, f"sqlite:///{tmp_path}/conversations.db" if split else None
    )
    startups, conv, item, attempt, body = opening(conversations)
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 180
    )
    release(startups, conv.host_id, body)
    inputs = ManagedStartupInputs(conversations, None)
    forwarded = []

    async def dispatch(context, session_id, event):
        conversations.update_conversation(session_id, archived=True)
        if await context.claim(conversations, session_id):
            forwarded.append(event)
        return {}

    inputs.dispatch = dispatch
    if not archive_during_dispatch:
        conversations.update_conversation(conv.id, archived=True)
    result = await inputs._deliver(SimpleNamespace(state=SimpleNamespace()), conv.id, item)
    assert result == "not_dispatched"
    assert forwarded == []
    assert conversations.get_item(conv.id, item.id).status == "in_progress"
    assert not startups.get(attempt.id).occupied
    await inputs.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_live_annotation_obeys_release_hold_and_pending_head(db_uri, monkeypatch, accepted):
    from omnigent.entities.session_startup import StartupReceipt
    from omnigent.server.host_registry import HostRegistry
    from omnigent.server.startup_inputs import reconcile_accepted_startup_input
    from tests.server.test_annotation_batches import (
        GENERATION,
        GRANT_ID,
        SESSION_ID,
        USER_ID,
        _batch,
        _ready_store,
    )

    conversations = _ready_store(db_uri)
    startups, conv, item, attempt, body = opening(conversations, session_id=SESSION_ID)
    monkeypatch.setattr(
        "omnigent.stores.session_startup_store.now_epoch", lambda: attempt.admitted_at + 180
    )
    release(startups, conv.host_id, body)
    if accepted:
        assert startups.bind_dispatch(
            attempt.id,
            attempt.attempt_id,
            runtime_id="runtime",
            source="codex",
            vendor_session_id="thread",
            vendor_turn_id="turn",
        )
        assert startups.observe(
            StartupReceipt(
                startup_id=attempt.id,
                attempt_id=attempt.attempt_id,
                runtime_id="runtime",
                input_id=item.id,
                phase="input_accepted",
                source="codex",
                vendor_session_id="thread",
                vendor_turn_id="turn",
            )
        )
        await reconcile_accepted_startup_input(conversations, startups.get(attempt.id))
    [middle] = conversations.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id=PENDING_INPUT_RESPONSE_ID,
                status="in_progress",
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": "next before annotation"}]
                ),
            )
        ],
    )
    batch = _batch()
    receipt, _ = conversations.admit_annotation_batch(
        USER_ID, GRANT_ID, GENERATION, conv.id, batch, target_live=True
    )
    inputs = ManagedStartupInputs(conversations, HostRegistry())
    app = SimpleNamespace(state=SimpleNamespace(startup_store=startups))
    inputs.dispatch = AsyncMock()
    await inputs.dispatch_annotation(
        app,
        conv.id,
        receipt["item_id"],
        USER_ID,
        GRANT_ID,
        GENERATION,
        batch.batch_id,
        target_live=lambda _: True,
    )
    inputs.dispatch.assert_not_awaited()
    deferred = conversations.get_annotation_receipt(USER_ID, GRANT_ID, batch.batch_id)
    assert deferred["dispatch_phase"] == "ready" and deferred["retry_required"]
    assert conversations.get_item(conv.id, middle.id).status == "in_progress"
    if accepted:
        conversations.append(
            conv.id,
            [
                NewConversationItem(
                    type=middle.type,
                    response_id="middle-turn",
                    stable_id=middle.id,
                    replaces_pending_id=middle.id,
                    data=middle.data,
                )
            ],
        )

        async def dispatch(context, session_id, event):
            assert await context.claim(conversations, session_id)
            conversations.append(
                session_id,
                [
                    NewConversationItem(
                        type="message",
                        response_id="annotation-turn",
                        stable_id=context.input_id,
                        replaces_pending_id=context.input_id,
                        annotation_delivery_confirmed=True,
                        data=MessageData(role="user", content=event.data["content"]),
                        created_by=USER_ID,
                    )
                ],
            )
            return {"item_id": context.input_id}

        inputs.dispatch = dispatch
        await inputs.dispatch_annotation(
            app,
            conv.id,
            receipt["item_id"],
            USER_ID,
            GRANT_ID,
            GENERATION,
            batch.batch_id,
            target_live=lambda _: True,
            explicit_retry=True,
        )
        assert (
            conversations.get_annotation_receipt(USER_ID, GRANT_ID, batch.batch_id)[
                "dispatch_phase"
            ]
            == "confirmed"
        )
    await inputs.shutdown()
