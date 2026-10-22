"""Runner cache reconciliation accepts only current, owned startup observations."""

import json
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import update
from starlette.datastructures import Headers

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import SqlUser, current_workspace_id
from omnigent.db.session_startup import SqlSessionStartup
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.host.frames import HostHelloFrame
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.server_config import StartupAdmissionPolicy
from omnigent.server.startup_admission import StartupAdmission
from omnigent.server.startup_inputs import StartupInputContext
from omnigent.server.startup_observations import reconcile_startup_observations
from omnigent.startup_protocol import StartupAttemptRef, StartupObservation
from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.session_startup_store import SessionStartupStore
from tests.server.test_host_registry import FakeWebSocket


def _startup(app, db_uri, *, owner=None, source="codex"):
    conversations = SqlAlchemyConversationStore(db_uri)
    startups = SessionStartupStore(db_uri)
    hosts = app.state.host_registry
    host_id = uuid4().hex
    if owner is not None:
        with startups._session("seed_observation_owner") as db:
            db.add(SqlUser(id=owner, account_generation="old-generation"))
    with account_authority_scope(owner, "old-generation" if owner else None):
        conn = hosts.register(
            host_id,
            FakeWebSocket(),
            HostHelloFrame(
                version="test",
                frame_protocol_version=1,
                name="sample-host",
                connection_id="generation",
                capabilities=["startup_admission_v1", f"startup_receipts:{source}"],
            ),
            owner=owner,
        )
    conversation = conversations.create_conversation()
    conversations.set_host_id(conversation.id, host_id, "/opt/work/project")
    runner_id = token_bound_runner_id("sample-binding")
    conversations.set_runner_id(conversation.id, runner_id)
    [item] = conversations.append(
        conversation.id,
        [
            NewConversationItem(
                type="message",
                response_id=PENDING_INPUT_RESPONSE_ID,
                status="in_progress",
                data=MessageData(role="user", content=[{"type": "input_text", "text": "work"}]),
            )
        ],
    )
    startups.configure(host_id, host_resident_limit=2, deployment_resident_limit=2)
    startups.register_host_generation(host_id, conn.startup_generation)
    queued = startups.enqueue(
        owner_user_id="owner",
        host_id=host_id,
        session_id=conversation.id,
        operation_key="create",
        input_id=item.id,
    )
    attempt = startups.reserve(host_id)
    assert startups.claim_boundary(
        queued.id,
        attempt.attempt_id,
        runtime_id="runtime",
        host_generation=conn.startup_generation,
        boundary="runner_prepare",
        claim_id="claim",
    )
    ref = StartupAttemptRef(
        startup_id=queued.id,
        attempt_id=attempt.attempt_id,
        input_id=item.id,
        runtime_id="runtime",
        host_generation=conn.startup_generation,
    )
    app.state.startup_store = startups
    return conversations, startups, hosts, conn, conversation, item, ref, runner_id


def _observation(ref, phase, *, source="codex"):
    return StartupObservation(
        attempt=ref,
        phase=phase,
        source=source,
        vendor_session_id="thread" if source in ("codex", "claude-sdk") else None,
        vendor_turn_id="turn" if source in ("codex", "claude-sdk") else None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["codex", "claude-sdk"])
async def test_lost_receipts_reconcile_from_exact_runner_cache_once(
    app, client, db_uri, monkeypatch, source
):
    conversations, startups, hosts, conn, conv, item, ref, runner_id = _startup(
        app, db_uri, source=source
    )
    phases = (
        "input_dispatched",
        "runtime_ready",
        "tools_ready",
        "input_accepted",
        "work_started",
    )
    observations = [_observation(ref, phase, source=source).model_dump() for phase in phases]
    requests = []

    def runner_handler(request):
        requests.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"observations": observations})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_handler), base_url="http://runner"
    ) as runner_client:
        monkeypatch.setattr(
            app.state.runner_router,
            "client_for_existing_conversation",
            lambda _session_id: SimpleNamespace(runner_id=runner_id, client=runner_client),
        )
        admission = StartupAdmission(
            startups, conversations, hosts, StartupAdmissionPolicy(2, 2, 2)
        )
        admission.bind(app)
        assert not await admission.recover_session(conv.id, conn)
        first_dispatch = startups.get(ref.startup_id).first_dispatch_at
        assert first_dispatch is not None
        assert startups.get(ref.startup_id).state == "working"
        consumed = conversations.get_item(conv.id, item.id)
        assert consumed.response_id == f"turn_{ref.attempt_id}"
        assert not conversations.list_pending_inputs(conv.id)
        assert requests == [(f"/v1/sessions/{conv.id}/startup/observations", ref.model_dump())]
        assert await reconcile_startup_observations(
            app, conversations, hosts, startups, startups.get(ref.startup_id)
        )
        assert startups.get(ref.startup_id).first_dispatch_at == first_dispatch
        assert conversations.get_item(conv.id, item.id).response_id == consumed.response_id
        await admission.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["forged_source", "stale_connection", "wrong_attempt"])
async def test_reconciliation_rejects_unowned_observations(
    app, client, db_uri, monkeypatch, fault
):
    conversations, startups, hosts, conn, conv, item, ref, runner_id = _startup(app, db_uri)
    observation = _observation(ref, "input_dispatched")

    def runner_handler(_request):
        if fault == "stale_connection":
            replacement = hosts.register(conn.host_id, FakeWebSocket(), conn.hello, owner=None)
            startups.register_host_generation(conn.host_id, replacement.startup_generation)
        if fault == "forged_source":
            body = observation.model_copy(update={"source": "codex-native"}).model_dump()
        elif fault == "wrong_attempt":
            wrong = ref.model_copy(update={"attempt_id": "f" * 32})
            body = observation.model_copy(update={"attempt": wrong}).model_dump()
        else:
            body = observation.model_dump()
        return httpx.Response(200, json={"observations": [body]})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_handler), base_url="http://runner"
    ) as runner_client:
        monkeypatch.setattr(
            app.state.runner_router,
            "client_for_existing_conversation",
            lambda _session_id: SimpleNamespace(runner_id=runner_id, client=runner_client),
        )
        assert not await reconcile_startup_observations(
            app, conversations, hosts, startups, startups.get(ref.startup_id)
        )
    assert startups.get(ref.startup_id).first_dispatch_at is None
    assert conversations.get_item(conv.id, item.id).response_id == PENDING_INPUT_RESPONSE_ID


@pytest.mark.asyncio
async def test_post_receipt_binds_first_dispatch_and_rejects_other_runner(app, client, db_uri):
    _conversations, startups, _hosts, _conn, conv, _item, ref, _runner_id = _startup(app, db_uri)
    url = f"/v1/sessions/{conv.id}/startup/receipts"
    body = _observation(ref, "input_dispatched").model_dump()
    assert (await client.post(url, json=body)).status_code == 403
    assert (
        await client.post(url, json=body, headers={RUNNER_TUNNEL_TOKEN_HEADER: "wrong"})
    ).status_code == 403
    assert (
        await client.post(url, json=body, headers={RUNNER_TUNNEL_TOKEN_HEADER: "sample-binding"})
    ).json() == {"accepted": True}
    assert startups.get(ref.startup_id).first_dispatch_at is not None


@pytest.mark.asyncio
async def test_reconciliation_rejects_rotated_host_account_after_query(
    app, client, db_uri, monkeypatch
):
    conversations, startups, hosts, conn, conv, item, ref, runner_id = _startup(
        app, db_uri, owner="alice"
    )
    assert conn.account_generation == "old-generation"

    def runner_handler(_request):
        with startups._session("rotate_observation_owner") as db:
            db.execute(
                update(SqlUser)
                .where(SqlUser.workspace_id == current_workspace_id(), SqlUser.id == "alice")
                .values(account_generation="new-generation")
            )
        return httpx.Response(
            200,
            json={"observations": [_observation(ref, "input_dispatched").model_dump()]},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(runner_handler), base_url="http://runner"
    ) as runner_client:
        monkeypatch.setattr(
            app.state.runner_router,
            "client_for_existing_conversation",
            lambda _session_id: SimpleNamespace(runner_id=runner_id, client=runner_client),
        )
        assert not await reconcile_startup_observations(
            app, conversations, hosts, startups, startups.get(ref.startup_id)
        )
    snapshot = startups.get(ref.startup_id)
    assert snapshot.first_dispatch_at is None
    assert snapshot.proof_source is None
    assert snapshot.occupied
    assert conversations.get_item(conv.id, item.id).response_id == PENDING_INPUT_RESPONSE_ID


@pytest.mark.asyncio
async def test_direct_receipts_reject_rotated_host_account(app, client, db_uri):
    conversations, startups, _hosts, _conn, conv, item, ref, _runner_id = _startup(
        app, db_uri, owner="alice"
    )
    with startups._session("rotate_direct_receipt_owner") as db:
        db.execute(
            update(SqlUser)
            .where(SqlUser.workspace_id == current_workspace_id(), SqlUser.id == "alice")
            .values(account_generation="new-generation")
        )
    url = f"/v1/sessions/{conv.id}/startup/receipts"
    for phase in ("input_dispatched", "runtime_ready"):
        response = await client.post(
            url,
            json=_observation(
                ref, phase, source="codex" if phase == "input_dispatched" else "runner"
            ).model_dump(),
            headers={RUNNER_TUNNEL_TOKEN_HEADER: "sample-binding"},
        )
        assert response.json() == {"accepted": False}
    snapshot = startups.get(ref.startup_id)
    assert snapshot.proof_source is None
    assert snapshot.first_dispatch_at is None
    assert snapshot.occupied
    assert conversations.get_item(conv.id, item.id).response_id == PENDING_INPUT_RESPONSE_ID


@pytest.mark.asyncio
async def test_runner_claim_rejects_rotated_host_account(app, client, db_uri):
    _conversations, startups, hosts, _conn, conv, _item, ref, _runner_id = _startup(
        app, db_uri, owner="alice"
    )
    hosts.launch_authorizer = Mock()
    with startups._session("rotate_runner_claim_owner") as db:
        db.execute(
            update(SqlUser)
            .where(SqlUser.workspace_id == current_workspace_id(), SqlUser.id == "alice")
            .values(account_generation="new-generation")
        )
    response = await client.post(
        f"/v1/sessions/{conv.id}/startup/claims",
        json={"attempt": ref.model_dump(), "boundary": "executor_prepare", "claim_id": "cold"},
        headers={RUNNER_TUNNEL_TOKEN_HEADER: "sample-binding"},
    )
    assert response.json() == {"granted": False}
    with startups._session("check_rejected_runner_claim") as db:
        row = db.get(SqlSessionStartup, (current_workspace_id(), ref.startup_id))
        assert "executor_prepare" not in row.cold_claims


@pytest.mark.asyncio
async def test_saved_input_claim_rejects_revoked_host_account(app, client, db_uri):
    conversations, startups, hosts, _conn, conv, item, ref, _runner = _startup(
        app, db_uri, owner="alice"
    )
    app.state.startup_admission = SimpleNamespace(hosts=hosts)
    with startups._session("rotate_saved_input_claim_owner") as db:
        db.execute(
            update(SqlUser)
            .where(SqlUser.workspace_id == current_workspace_id(), SqlUser.id == "alice")
            .values(account_generation="new-generation")
        )
    context = StartupInputContext(app, Headers(), "alice", item.id, startup=ref)
    with pytest.raises(OmnigentError, match="account authority has been revoked"):
        await context.claim(conversations, conv.id)
    assert conversations.get_item(conv.id, item.id).status == "in_progress"
    with startups._session("read_rejected_saved_input_claim") as db:
        row = db.get(SqlSessionStartup, (current_workspace_id(), ref.startup_id))
        assert "input_dispatch" not in row.cold_claims
