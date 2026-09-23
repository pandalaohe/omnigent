"""Project hand-off routes with durable SQLite stores and a fake host."""

from __future__ import annotations

import secrets
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.utils import generate_agent_id
from omnigent.errors import OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.routes._sessions.helpers import SessionLiveness
from omnigent.server.routes.sessions.routes_handoff import register_handoff_routes
from omnigent.server.routes.sessions.routes_peer import register_peer_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.session_handoff_store.sqlalchemy_store import SqlAlchemySessionHandoffStore

ALICE = "alice@example.com"


@pytest.fixture()
def handoff_env(db_uri: str) -> dict[str, Any]:
    agents = SqlAlchemyAgentStore(db_uri)
    agent_id = generate_agent_id()
    agents.create(agent_id, "test-agent", "test:///bundle")
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(ALICE)
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(
        uuid.uuid4().hex,
        "Target",
        ALICE,
        {"host_id": "1" * 32, "workspace": "/repo", "agent_id": agent_id},
    )
    sender_token = secrets.token_hex(16)
    receiver_token = secrets.token_hex(16)
    sender = conversations.create_conversation(
        agent_id=agent_id, title="sender", runner_id=token_bound_runner_id(sender_token)
    )
    receiver = conversations.create_conversation(
        agent_id=agent_id,
        title="receiver",
        runner_id=token_bound_runner_id(receiver_token),
        project_id=project.id,
        host_id="1" * 32,
        workspace="/repo",
    )
    for conv in (sender, receiver):
        permissions.grant(ALICE, conv.id, LEVEL_OWNER)
    peers = SqlAlchemyPeerMessageStore(db_uri)
    handoffs = SqlAlchemySessionHandoffStore(db_uri)
    host = SimpleNamespace(host_id="1" * 32, name="host-one", user_id=ALICE, deleted_at=None)
    host_store = SimpleNamespace(
        list_hosts=lambda user: [host] if user == ALICE else [],
        get_host=lambda hid: host if hid == host.host_id else None,
    )
    host_registry = SimpleNamespace(get=lambda hid: None)
    offline_ids: set[str] = set()
    app = FastAPI()
    app.state.peer_message_store = peers
    app.state.host_store = host_store
    app.state.project_host_binding_store = None
    app.state.host_registry = host_registry
    event_state: dict[str, Any] = {"outcome": {"queued": True}, "error": None}

    @app.exception_handler(OmnigentError)
    async def omnigent_error(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    async def post_event(
        _request: Request, _session_id: str, _body: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        if event_state["error"] is not None:
            raise event_state["error"]
        return {"item_id": uuid.uuid4().hex, **event_state["outcome"]}

    flags = resolve_feature_flags({"OMNIGENT_FEATURES": "session_peer_messaging"})
    router = APIRouter()
    peer = register_peer_routes(
        router,
        post_event_impl=post_event,
        conversation_store=conversations,
        permission_store=permissions,
        auth_provider=UnifiedAuthProvider(source="header"),
        liveness_lookup=lambda ids: {
            sid: SessionLiveness(False, False) for sid in ids if sid in offline_ids
        },
        runner_tunnel_tokens=None,
        feature_flags=flags,
        peer_message_store=peers,
        runner_router=None,
        agent_store=agents,
        app_state=app.state,
    )
    app.state.peer_routes = peer
    register_handoff_routes(
        router,
        peer=peer,
        handoff_store=handoffs,
        project_store=projects,
        conversation_store=conversations,
        agent_store=agents,
        runner_router=None,
        permission_store=permissions,
        auth_provider=UnifiedAuthProvider(source="header"),
        runner_tunnel_tokens=None,
        feature_flags=flags,
        host_registry=host_registry,
        agent_cache=None,
        file_store=None,
        artifact_store=None,
        background_title_coordinator=None,
        app_state=app.state,
    )
    app.include_router(router, prefix="/v1")
    return {
        "app": app,
        "sender": sender,
        "receiver": receiver,
        "sender_token": sender_token,
        "receiver_token": receiver_token,
        "handoffs": handoffs,
        "peers": peers,
        "project": project,
        "projects": projects,
        "agents": agents,
        "agent_id": agent_id,
        "host_store": host_store,
        "host_registry": host_registry,
        "event_state": event_state,
        "offline_ids": offline_ids,
        "conversations": conversations,
        "permissions": permissions,
    }


def _headers(token: str) -> dict[str, str]:
    return {RUNNER_TUNNEL_TOKEN_HEADER: token, "X-Forwarded-Email": ALICE}


async def _run_pass(env: dict[str, Any], now: int | None = None) -> None:
    from omnigent.db.utils import now_epoch

    handoff_pass = env["app"].state.peer_sweeper._handoff_pass
    assert handoff_pass is not None
    await handoff_pass(now if now is not None else now_epoch())


def _record(
    env: dict[str, Any],
    *,
    state: str = "open",
    sender_id: str | None = None,
    receiver_id: str | None = None,
    branch: str | None = None,
) -> Any:
    from omnigent.db.utils import now_epoch
    from omnigent.entities import SessionHandoff

    now = now_epoch()
    return SessionHandoff(
        id=uuid.uuid4().hex,
        owner_user_id=ALICE,
        sender_session_id=sender_id or env["sender"].id,
        receiver_session_id=receiver_id or uuid.uuid4().hex,
        create_session=False,
        project_id=env["project"].id,
        state=state,
        brief_hash=uuid.uuid4().hex,
        brief="[Hand-off] Task: Review code",
        allow_onward=False,
        brief_peer_id=uuid.uuid4().hex,
        created_at=now,
        updated_at=now,
        expires_at=now + 3600,
        host_id="1" * 32,
        root="/repo",
        git_branch=branch,
        git_plan={
            "agent_id": env["agent_id"],
            "project_name": "Target",
            "workspace": "/repo",
            "title": "Hand-off: Review code",
            "source": {"task": "Review code"},
        },
        disclosure={
            "reused": True,
            "branch": branch,
            "base_branch": None,
            "branch_generated": False,
            "dirty_paths": None,
        },
    )


@pytest.mark.asyncio
async def test_start_reuses_idle_session_and_retry_dedupes(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        url = f"/v1/sessions/{env['sender'].id}/handoffs"
        first = await client.post(
            url,
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        assert first.status_code == 200, first.text
        data = first.json()
        assert data["disposition"] == "started"
        assert data["session"]["id"] == env["receiver"].id
        assert data["disclosure"]["reused"] is True
        second = await client.post(
            url,
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        assert second.status_code == 200
        assert second.json()["disposition"] == "duplicate"
        assert second.json()["handoff_id"] == data["handoff_id"]
        records = env["handoffs"].list_for_sender(env["sender"].id, 0, 20)
        assert len(records) == 1
        assert env["peers"].get(records[0].brief_peer_id) is not None


@pytest.mark.asyncio
async def test_report_once_and_result_reaches_sender(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        assert start.status_code == 200, start.text
        hid = start.json()["handoff_id"]
        url = f"/v1/handoffs/{hid}/report"
        body = {"status": "completed", "summary": "Done", "done": ["reviewed"]}
        result = await client.post(url, json=body, headers=_headers(env["receiver_token"]))
        assert result.status_code == 200, result.text
        assert result.json()["state"] == "completed"
        again = await client.post(url, json=body, headers=_headers(env["receiver_token"]))
        assert again.status_code == 409
        assert again.json()["handoff_id"] == hid
        record = env["handoffs"].get(hid)
        assert record is not None and record.result_peer_id is not None
        assert env["peers"].get(record.result_peer_id) is not None


@pytest.mark.asyncio
async def test_offline_result_is_deferred_for_24_hours(handoff_env: dict[str, Any]) -> None:
    from omnigent.db.utils import now_epoch

    env = handoff_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        hid = start.json()["handoff_id"]
        env["offline_ids"].add(env["sender"].id)
        result = await client.post(
            f"/v1/handoffs/{hid}/report",
            json={"status": "completed", "summary": "Done"},
            headers=_headers(env["receiver_token"]),
        )
    assert result.status_code == 200, result.text
    record = env["handoffs"].get(hid)
    assert record is not None and record.result_state == "sent"
    peer = env["peers"].get(record.result_peer_id)
    assert peer is not None and peer.state == "pending"
    assert peer.expires_at >= now_epoch() + 86390


@pytest.mark.asyncio
async def test_closed_receiver_records_failed_stop(handoff_env: dict[str, Any]) -> None:
    from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

    env = handoff_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        hid = start.json()["handoff_id"]
        env["conversations"].set_labels(env["receiver"].id, {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE})
        stopped = await client.post(
            f"/v1/handoffs/{hid}/cancel", headers=_headers(env["sender_token"])
        )
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["stop_state"] == "failed"
    assert env["handoffs"].get(hid).stop_state == "failed"


@pytest.mark.asyncio
async def test_oversize_result_rejected_before_report_commit(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        hid = start.json()["handoff_id"]
        oversized = await client.post(
            f"/v1/handoffs/{hid}/report",
            json={"status": "completed", "summary": "Done", "done": ["x" * 500] * 32},
            headers=_headers(env["receiver_token"]),
        )
        unchanged = env["handoffs"].get(hid)
        retry = await client.post(
            f"/v1/handoffs/{hid}/report",
            json={"status": "completed", "summary": "Short result"},
            headers=_headers(env["receiver_token"]),
        )
    assert oversized.status_code == 400, oversized.text
    assert oversized.json()["error"]["code"] == "invalid_input"
    assert "16000" in oversized.json()["error"]["message"]
    assert unchanged is not None and unchanged.state == "delivered"
    assert unchanged.reported_at is None and unchanged.result_peer_id is None
    assert retry.status_code == 200 and retry.json()["result_state"] == "sent"


@pytest.mark.asyncio
async def test_completed_back_notice_omits_empty_reason(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = handoff_env
    notices: list[str] = []

    async def notice(_session_id: str, line: str) -> None:
        notices.append(line)

    monkeypatch.setattr(env["app"].state.peer_sweeper, "notify_line", notice)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        hid = start.json()["handoff_id"]
        report = await client.post(
            f"/v1/handoffs/{hid}/report",
            json={"status": "completed", "summary": "Done"},
            headers=_headers(env["receiver_token"]),
        )
    assert report.status_code == 200
    assert notices[-1] == (
        f'[System: hand-off {hid} to session {env["receiver"].id} "receiver" completed]'
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "project,reason", [("missing", "project_not_found"), ("Target", "host_required")]
)
async def test_start_needs_input(handoff_env: dict[str, Any], project: str, reason: str) -> None:
    env = handoff_env
    if reason == "host_required":
        env["project"].config["host_id"] = None
        env["project"].config["workspace"] = None
        from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

        SqlAlchemyProjectStore(env["handoffs"].storage_location).update(
            env["project"].id, user_id=ALICE, config=env["project"].config
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": project, "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200
    assert response.json()["disposition"] == "needs_input"
    assert response.json()["reason"] == reason


@pytest.mark.asyncio
async def test_terminal_record_does_not_dedupe(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    url = f"/v1/sessions/{env['sender'].id}/handoffs"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        first = await client.post(
            url,
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        assert first.status_code == 200
        hid = first.json()["handoff_id"]
        report = await client.post(
            f"/v1/handoffs/{hid}/report",
            json={"status": "completed", "summary": "Done"},
            headers=_headers(env["receiver_token"]),
        )
        assert report.status_code == 200
        second = await client.post(
            url,
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        assert second.status_code == 200
        assert second.json()["handoff_id"] != hid


@pytest.mark.asyncio
async def test_subagent_cannot_start(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    token = secrets.token_hex(16)
    child = env["conversations"].create_conversation(
        agent_id=env["agent_id"],
        parent_conversation_id=env["sender"].id,
        runner_id=token_bound_runner_id(token),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{child.id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(token),
        )
    assert response.status_code == 200
    assert response.json()["reason"] == "is_subagent"
    assert env["handoffs"].list_for_sender(child.id, 0, 20) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    [
        "project_ambiguous",
        "no_root",
        "agent_required",
        "base_branch_required",
        "branch_exists",
        "branch_in_use",
    ],
)
async def test_start_resolution_reasons(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, reason: str
) -> None:
    env = handoff_env
    body: dict[str, Any] = {"project": "Target", "task": "Review code"}
    if reason == "project_ambiguous":
        from dataclasses import replace

        other = replace(env["project"], id=uuid.uuid4().hex, name="target")
        monkeypatch.setattr(env["projects"], "list", lambda **_kwargs: [env["project"], other])
    if reason == "no_root":
        other_host = SimpleNamespace(
            host_id="2" * 32, name="host-two", user_id=ALICE, deleted_at=None
        )
        monkeypatch.setattr(env["host_store"], "list_hosts", lambda _user: [other_host])
        body["host"] = "host-two"
    if reason == "agent_required":
        config = dict(env["project"].config)
        config.pop("agent_id")
        env["projects"].update(env["project"].id, user_id=ALICE, config=config)
    if reason in ("base_branch_required", "branch_exists"):
        body["branch"] = "review-branch"
    if reason == "branch_exists":
        body["base_branch"] = "main"
        monkeypatch.setattr(env["host_registry"], "get", lambda _hid: SimpleNamespace())

        async def worktrees(**_kwargs: Any) -> list[dict[str, Any]]:
            return [{"branch": "review-branch", "path": "/repo-worktrees/review-branch"}]

        monkeypatch.setattr(
            "omnigent.server.routes.sessions.routes_handoff.list_worktrees_on_host", worktrees
        )
    if reason == "branch_in_use":
        body.update(branch="review-branch", existing_branch=True)
        other_agent = generate_agent_id()
        env["agents"].create(other_agent, "other-agent", "test:///bundle")
        in_use = env["conversations"].create_conversation(
            agent_id=other_agent,
            project_id=env["project"].id,
            host_id="1" * 32,
            workspace="/else",
            git_branch="review-branch",
        )
        env["permissions"].grant(ALICE, in_use.id, LEVEL_OWNER)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json=body,
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200, response.text
    assert response.json()["reason"] == reason
    assert response.json()["disposition"] == "needs_input"


@pytest.mark.asyncio
async def test_create_derived_session_once_and_advance_is_idempotent(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.db.utils import now_epoch

    env = handoff_env
    from omnigent.server.routes.sessions import routes_handoff

    calls: list[str] = []
    lease_seconds: list[int] = []
    real_create = env["handoffs"].create

    def capture_lease(record: Any) -> Any:
        lease_seconds.append(record.lease_until - now_epoch())
        return real_create(record)

    monkeypatch.setattr(env["handoffs"], "create", capture_lease)

    async def create_session(*_args: Any, conversation_id: str, **_kwargs: Any) -> Any:
        calls.append(conversation_id)
        env["conversations"].create_conversation(
            conversation_id=conversation_id,
            agent_id=env["agent_id"],
            project_id=env["project"].id,
            host_id="1" * 32,
            workspace="/repo",
            title="Hand-off: Review code",
        )
        return SimpleNamespace(id=conversation_id)

    monkeypatch.setattr(routes_handoff, "_create_session_from_existing_agent", create_session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code", "session": "new"},
            headers=_headers(env["sender_token"]),
        )
        assert response.status_code == 200, response.text
        hid = response.json()["handoff_id"]
        assert response.json()["session"]["id"] == routes_handoff.derived_handoff_session_id(hid)
        await _run_pass(env)
    record = env["handoffs"].get(hid)
    assert record is not None
    assert calls == [record.receiver_session_id]
    assert len(lease_seconds) == 1 and 290 <= lease_seconds[0] <= 300
    assert (
        env["conversations"]
        .get_conversation(record.receiver_session_id)
        .labels["omnigent.handoff.id"]
        == hid
    )
    assert env["peers"].get(record.brief_peer_id) is not None


@pytest.mark.asyncio
async def test_sweeper_advances_crashed_creating_record(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.db.utils import now_epoch
    from omnigent.entities import SessionHandoff
    from omnigent.server.routes.sessions import routes_handoff

    env = handoff_env
    hid = uuid.uuid4().hex
    now = now_epoch()
    record = SessionHandoff(
        id=hid,
        owner_user_id=ALICE,
        sender_session_id=env["sender"].id,
        receiver_session_id=routes_handoff.derived_handoff_session_id(hid),
        create_session=True,
        project_id=env["project"].id,
        state="creating",
        brief_hash="0" * 64,
        brief="[Hand-off] Task: Review code",
        allow_onward=False,
        brief_peer_id=uuid.uuid4().hex,
        created_at=now,
        updated_at=now,
        expires_at=now + 3600,
        host_id="1" * 32,
        root="/repo",
        lease_until=now - 1,
        git_plan={
            "agent_id": env["agent_id"],
            "project_name": "Target",
            "workspace": "/repo",
            "title": "Hand-off: Review code",
            "source": {"task": "Review code"},
        },
        disclosure={
            "reused": False,
            "branch": None,
            "base_branch": None,
            "branch_generated": False,
            "dirty_paths": None,
        },
    )
    env["handoffs"].create(record)

    async def create_session(*_args: Any, conversation_id: str, **_kwargs: Any) -> Any:
        env["conversations"].create_conversation(
            conversation_id=conversation_id,
            agent_id=env["agent_id"],
            project_id=env["project"].id,
            host_id="1" * 32,
            workspace="/repo",
            title="Hand-off: Review code",
        )
        return SimpleNamespace(id=conversation_id)

    monkeypatch.setattr(routes_handoff, "_create_session_from_existing_agent", create_session)
    await _run_pass(env, now)
    updated = env["handoffs"].get(hid)
    assert updated is not None and updated.state == "delivered"
    assert env["peers"].get(updated.brief_peer_id) is not None


@pytest.mark.asyncio
async def test_expired_creating_git_lease_fails_as_interrupted(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.db.utils import now_epoch
    from omnigent.server.routes.sessions import routes_handoff

    env = handoff_env
    record = _record(env, state="creating", branch="review-branch")
    record.create_session = True
    record.receiver_session_id = routes_handoff.derived_handoff_session_id(record.id)
    record.lease_until = now_epoch() - 1
    record.git_plan["git"] = {"branch_name": "review-branch", "base_branch": "main"}
    env["handoffs"].create(record)

    async def must_not_create(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("interrupted worktree creation must not run twice")

    monkeypatch.setattr(routes_handoff, "_create_session_from_existing_agent", must_not_create)
    await _run_pass(env)
    updated = env["handoffs"].get(record.id)
    assert updated is not None and updated.state == "failed"
    assert updated.reason.startswith("create_interrupted:")


@pytest.mark.asyncio
async def test_expired_creating_existing_worktree_bind_advances(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.db.utils import now_epoch
    from omnigent.server.routes.sessions import routes_handoff

    env = handoff_env
    record = _record(env, state="creating", branch="review-branch")
    record.create_session = True
    record.receiver_session_id = routes_handoff.derived_handoff_session_id(record.id)
    record.lease_until = now_epoch() - 1
    record.git_plan["git"] = {"branch_name": "review-branch", "existing_worktree": True}
    record.git_plan["workspace"] = "/repo-worktrees/review-branch"
    env["handoffs"].create(record)

    async def create_session(*args: Any, conversation_id: str, **_kwargs: Any) -> Any:
        body = args[3]
        env["conversations"].create_conversation(
            conversation_id=conversation_id,
            agent_id=env["agent_id"],
            project_id=env["project"].id,
            host_id="1" * 32,
            workspace=body.workspace,
            git_branch="review-branch",
        )
        return SimpleNamespace(id=conversation_id)

    monkeypatch.setattr(routes_handoff, "_create_session_from_existing_agent", create_session)
    await _run_pass(env)
    updated = env["handoffs"].get(record.id)
    assert updated is not None and updated.state == "delivered"
    assert env["peers"].get(updated.brief_peer_id) is not None


@pytest.mark.asyncio
async def test_cancel_held_brief_expires_peer_record(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    env["conversations"].set_labels(env["receiver"].id, {"peer_inbound": "hold"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        assert start.status_code == 200, start.text
        hid = start.json()["handoff_id"]
        assert start.json()["state"] == "open"
        cancelled = await client.post(
            f"/v1/handoffs/{hid}/cancel", headers=_headers(env["sender_token"])
        )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"
    record = env["handoffs"].get(hid)
    assert record is not None
    assert env["peers"].get(record.brief_peer_id).state == "expired"


@pytest.mark.asyncio
async def test_expiry_sends_stop_and_accepts_late_report(handoff_env: dict[str, Any]) -> None:
    from omnigent.db.utils import now_epoch

    env = handoff_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        assert start.status_code == 200
        hid = start.json()["handoff_id"]
        assert start.json()["state"] == "delivered"
        env["handoffs"].set_fields(hid, ("delivered",), expires_at=now_epoch() - 1)
        await _run_pass(env)
        expired = env["handoffs"].get(hid)
        assert expired is not None
        assert expired.state == "expired" and expired.reason == "no_report"
        assert expired.stop_state == "sent"
        report = await client.post(
            f"/v1/handoffs/{hid}/report",
            json={"status": "incomplete", "summary": "Still working"},
            headers=_headers(env["receiver_token"]),
        )
    assert report.status_code == 200, report.text
    assert report.json()["state"] == "incomplete"
    assert report.json()["result_state"] == "sent"


@pytest.mark.asyncio
async def test_receiver_revocation_requests_stop(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
    assert start.status_code == 200
    hid = start.json()["handoff_id"]
    env["conversations"].set_labels(env["receiver"].id, {"peer_inbound": "refuse"})
    await _run_pass(env)
    updated = env["handoffs"].get(hid)
    assert updated is not None
    assert updated.state == "cancel_requested"
    assert updated.reason == "revoked"
    assert updated.stop_state == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("flag_off", [False, True])
async def test_handoff_routes_are_404_when_disabled(
    handoff_env: dict[str, Any], flag_off: bool
) -> None:
    env = handoff_env
    app = FastAPI()
    router = APIRouter()
    register_handoff_routes(
        router,
        peer=env["app"].state.peer_routes,
        handoff_store=env["handoffs"] if flag_off else None,
        project_store=env["projects"],
        conversation_store=env["conversations"],
        agent_store=env["agents"],
        runner_router=None,
        permission_store=env["permissions"],
        auth_provider=UnifiedAuthProvider(source="header"),
        runner_tunnel_tokens=None,
        feature_flags=resolve_feature_flags(
            {"OMNIGENT_FEATURES": "" if flag_off else "session_peer_messaging"}
        ),
        host_registry=env["host_registry"],
        agent_cache=None,
        file_store=None,
        artifact_store=None,
        background_title_coordinator=None,
        app_state=app.state,
    )
    app.include_router(router, prefix="/v1")
    sid = env["sender"].id
    hid = uuid.uuid4().hex
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for method, path in [
            ("POST", f"/v1/sessions/{sid}/handoffs"),
            ("GET", f"/v1/sessions/{sid}/handoffs"),
            ("GET", f"/v1/handoffs/{hid}"),
            ("POST", f"/v1/handoffs/{hid}/cancel"),
            ("POST", f"/v1/handoffs/{hid}/report"),
        ]:
            payload = (
                {"project": "Target", "task": "Review code"}
                if path.endswith("/handoffs")
                else {"status": "completed", "summary": "Done"}
                if path.endswith("/report")
                else {}
            )
            response = await client.request(
                method, path, json=payload if method == "POST" else None
            )
            assert response.status_code == 404
        malformed = await client.post(f"/v1/sessions/{sid}/handoffs", json={})
        assert malformed.status_code == 404


@pytest.mark.asyncio
async def test_side_chat_explicit_target_refused(handoff_env: dict[str, Any]) -> None:
    from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY

    env = handoff_env
    env["conversations"].set_labels(env["receiver"].id, {SIDE_CHAT_LABEL_KEY: "true"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code", "session": env["receiver"].id},
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200
    assert response.json()["reason"] == "side_chat"


@pytest.mark.asyncio
async def test_branch_reservation_needs_input(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    env["handoffs"].create(_record(env, branch="review-branch"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={
                "project": "Target",
                "task": "Review code",
                "branch": "review-branch",
                "existing_branch": True,
            },
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200
    assert response.json()["reason"] == "branch_in_use"


@pytest.mark.asyncio
async def test_owner_cap_is_serialized_across_senders(handoff_env: dict[str, Any]) -> None:
    import asyncio

    env = handoff_env
    for _ in range(4):
        env["handoffs"].create(_record(env))
    token = secrets.token_hex(16)
    other = env["conversations"].create_conversation(
        agent_id=env["agent_id"], runner_id=token_bound_runner_id(token), title="other sender"
    )
    env["permissions"].grant(ALICE, other.id, LEVEL_OWNER)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        responses = await asyncio.gather(
            client.post(
                f"/v1/sessions/{env['sender'].id}/handoffs",
                json={"project": "Target", "task": "First task"},
                headers=_headers(env["sender_token"]),
            ),
            client.post(
                f"/v1/sessions/{other.id}/handoffs",
                json={"project": "Target", "task": "Second task"},
                headers=_headers(token),
            ),
        )
    assert all(response.status_code == 200 for response in responses)
    assert sorted(response.json()["disposition"] for response in responses) == [
        "refused",
        "started",
    ]
    assert (
        next(
            response.json()["reason"]
            for response in responses
            if response.json()["disposition"] == "refused"
        )
        == "cap"
    )
    assert env["handoffs"].count_unfinished(ALICE) == 5


@pytest.mark.asyncio
async def test_sender_burst_limit(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    for _ in range(30):
        env["handoffs"].create(_record(env, state="completed"))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200
    assert response.json()["reason"] == "burst"


@pytest.mark.asyncio
async def test_sweeper_sends_open_brief_once(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    record = _record(env, receiver_id=env["receiver"].id)
    env["handoffs"].create(record)
    await _run_pass(env)
    first = env["handoffs"].get(record.id)
    assert first is not None and first.state == "delivered"
    await _run_pass(env)
    assert env["peers"].get(record.brief_peer_id) is not None
    assert env["peers"].count_for_ref(record.id) == 1


@pytest.mark.asyncio
async def test_brief_exception_after_s1_delivery_still_delivers(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    env = handoff_env
    record = _record(env, receiver_id=env["receiver"].id)
    env["handoffs"].create(record)

    def reply_lookup_fails(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("reply lookup failed after delivery")

    monkeypatch.setattr(env["peers"], "list_for_session", reply_lookup_fails)
    await _run_pass(env)
    updated = env["handoffs"].get(record.id)
    brief = env["peers"].get(record.brief_peer_id)
    assert brief is not None and brief.state == "delivered"
    assert updated is not None and updated.state == "delivered"
    assert env["peers"].count_for_ref(record.id) == 1


@pytest.mark.asyncio
async def test_deferred_handoff_brief_requires_strict_init_only_for_brief(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.db.utils import now_epoch
    from omnigent.entities import SessionPeerMessage

    env = handoff_env
    handoff = _record(env, receiver_id=env["receiver"].id)
    env["handoffs"].create(handoff)
    now = now_epoch()
    records = [
        SessionPeerMessage(
            id=peer_id,
            sender_session_id=env["sender"].id,
            receiver_session_id=env["receiver"].id,
            ref=handoff.id,
            text="brief" if peer_id == handoff.brief_peer_id else "other message",
            state="pending",
            correlation_id=handoff.id,
            created_at=now,
            expires_at=now + 3600,
        )
        for peer_id in (handoff.brief_peer_id, uuid.uuid4().hex)
    ]
    sweeper = env["app"].state.peer_sweeper
    sweeper._app = env["app"]
    strict_flags: list[bool] = []

    async def deliver(*_args: Any, **kwargs: Any) -> tuple[str, None]:
        strict_flags.append(kwargs.get("require_init_success", False))
        return "delivered", None

    monkeypatch.setattr(sweeper, "_deliver", deliver)
    for peer_record in records:
        env["peers"].create(peer_record)
        await sweeper._process_due(peer_record, now)
    assert strict_flags == [True, False]


@pytest.mark.asyncio
async def test_deferred_s1_text_ref_delivers_without_strict_init(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.db.utils import now_epoch

    env = handoff_env
    env["offline_ids"].add(env["receiver"].id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        sent = await client.post(
            f"/v1/sessions/{env['receiver'].id}/peer-messages",
            json={
                "sender_session_id": env["sender"].id,
                "text": "ordinary message",
                "correlation_id": "scc-a5-5bec34",
                "wait_seconds": 60,
            },
            headers=_headers(env["sender_token"]),
        )
    assert sent.status_code == 200, sent.text
    assert sent.json()["disposition"] == "pending"
    peer_id = sent.json()["peer_id"]
    env["offline_ids"].remove(env["receiver"].id)
    sweeper = env["app"].state.peer_sweeper
    sweeper._app = env["app"]
    delivery_kwargs: list[dict[str, Any]] = []

    async def deliver(*_args: Any, **kwargs: Any) -> tuple[str, None]:
        delivery_kwargs.append(kwargs)
        return "delivered", None

    monkeypatch.setattr(sweeper, "_deliver", deliver)
    record = env["peers"].get(peer_id)
    assert record is not None and record.ref == "scc-a5-5bec34"
    await sweeper._process_due(record, now_epoch())
    assert env["peers"].get(peer_id).state == "delivered"
    assert len(delivery_kwargs) == 1
    assert "require_init_success" not in delivery_kwargs[0]


@pytest.mark.asyncio
async def test_sweeper_sends_report_left_pending(handoff_env: dict[str, Any]) -> None:
    from omnigent.db.utils import now_epoch

    env = handoff_env
    record = _record(env, state="completed", receiver_id=env["receiver"].id)
    record.outcome = {
        "status": "completed",
        "summary": "Done",
        "done": [],
        "not_done": [],
        "artifacts": [],
    }
    record.reported_at = now_epoch()
    record.result_state = "pending"
    record.result_peer_id = uuid.uuid4().hex
    env["handoffs"].create(record)
    await _run_pass(env)
    await _run_pass(env)
    updated = env["handoffs"].get(record.id)
    assert updated is not None and updated.result_state == "sent"
    assert env["peers"].get(record.result_peer_id) is not None
    assert env["peers"].count_for_ref(record.id) == 1


@pytest.mark.asyncio
async def test_raised_result_send_stays_pending_for_next_pass(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.db.utils import now_epoch

    env = handoff_env
    record = _record(env, state="completed", receiver_id=env["receiver"].id)
    record.outcome = {"status": "completed", "summary": "Done"}
    record.reported_at = now_epoch()
    record.result_state = "pending"
    record.result_peer_id = uuid.uuid4().hex
    env["handoffs"].create(record)
    real_get = env["peers"].get
    calls = {"n": 0}

    def flaky_get(peer_id: str) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("store blip")
        return real_get(peer_id)

    monkeypatch.setattr(env["peers"], "get", flaky_get)
    await _run_pass(env)
    after_blip = env["handoffs"].get(record.id)
    assert after_blip is not None and after_blip.result_state == "pending"
    await _run_pass(env)
    sent = env["handoffs"].get(record.id)
    assert sent is not None and sent.result_state == "sent"
    assert env["peers"].count_for_ref(record.id) == 1


@pytest.mark.asyncio
async def test_cancel_after_delivery_retains_receiver_report(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        hid = start.json()["handoff_id"]
        cancel = await client.post(
            f"/v1/handoffs/{hid}/cancel", headers=_headers(env["sender_token"])
        )
        assert cancel.status_code == 200 and cancel.json()["state"] == "cancel_requested"
        report = await client.post(
            f"/v1/handoffs/{hid}/report",
            json={"status": "incomplete", "summary": "Stopped"},
            headers=_headers(env["receiver_token"]),
        )
    assert report.status_code == 200
    assert report.json()["state"] == "cancelled"
    assert report.json()["outcome"]["status"] == "incomplete"


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome,reason", [({"forwarded": False}, "not_forwarded")])
async def test_definitive_rejection_fails_handoff(
    handoff_env: dict[str, Any], outcome: dict[str, Any], reason: str
) -> None:
    env = handoff_env
    env["event_state"]["outcome"] = outcome
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200
    assert response.json()["disposition"] == "failed"
    assert reason in response.json()["reason"]


@pytest.mark.asyncio
async def test_strict_init_failure_surfaces_reason(handoff_env: dict[str, Any]) -> None:
    from omnigent.errors import ErrorCode

    env = handoff_env
    env["event_state"]["error"] = OmnigentError(
        "The recovered runner did not finish session initialization.",
        code=ErrorCode.RUNNER_UNAVAILABLE,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200
    assert response.json()["disposition"] == "failed"
    assert "init_failed" in response.json()["reason"]


@pytest.mark.asyncio
async def test_report_bypasses_peer_thread_limit(handoff_env: dict[str, Any]) -> None:
    from omnigent.db.utils import now_epoch
    from omnigent.entities import SessionPeerMessage

    env = handoff_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        hid = start.json()["handoff_id"]
        now = now_epoch()
        for _ in range(20):
            env["peers"].create(
                SessionPeerMessage(
                    id=uuid.uuid4().hex,
                    sender_session_id=env["receiver"].id,
                    receiver_session_id=env["sender"].id,
                    ref=hid,
                    text="Progress",
                    state="delivered",
                    correlation_id=hid,
                    created_at=now,
                    expires_at=now + 3600,
                )
            )
        report = await client.post(
            f"/v1/handoffs/{hid}/report",
            json={"status": "completed", "summary": "Done"},
            headers=_headers(env["receiver_token"]),
        )
    assert report.status_code == 200
    assert report.json()["result_state"] == "sent"
    assert env["peers"].count_for_ref(hid) == 22


@pytest.mark.asyncio
async def test_fresh_branch_create_passes_git_plan(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes.sessions import routes_handoff

    env = handoff_env
    monkeypatch.setattr(env["host_registry"], "get", lambda _hid: SimpleNamespace())

    async def worktrees(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"branch": "main", "path": "/repo"}]

    monkeypatch.setattr(routes_handoff, "list_worktrees_on_host", worktrees)
    captured: list[Any] = []

    async def create_session(*args: Any, conversation_id: str, **_kwargs: Any) -> Any:
        body = args[3]
        captured.append(body)
        env["conversations"].create_conversation(
            conversation_id=conversation_id,
            agent_id=env["agent_id"],
            project_id=env["project"].id,
            host_id="1" * 32,
            workspace="/repo-worktrees/review-branch",
            git_branch="review-branch",
            title="Hand-off: Review code",
        )
        return SimpleNamespace(id=conversation_id)

    monkeypatch.setattr(routes_handoff, "_create_session_from_existing_agent", create_session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={
                "project": "Target",
                "task": "Review code",
                "branch": "review-branch",
                "base_branch": "main",
            },
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "delivered"
    assert captured[0].git.branch_name == "review-branch"
    assert captured[0].git.base_branch == "main"
    assert captured[0].workspace == "/repo"
    assert captured[0].project_id == env["project"].id
    record = env["handoffs"].get(response.json()["handoff_id"])
    assert record is not None
    assert "Workspace: /repo-worktrees/review-branch · branch review-branch" in record.brief
    assert record.brief in env["peers"].get(record.brief_peer_id).text


@pytest.mark.asyncio
async def test_maximum_accepted_brief_survives_workspace_rerender(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes.sessions import routes_handoff

    env = handoff_env
    branch = "review-branch"
    workspace = f"/repo-worktrees/{branch}"
    limit = 16000 - getattr(routes_handoff, "_BRIEF_WORKSPACE_ALLOWANCE", 0)
    sample = _record(env, branch=branch)
    sample.git_plan["source"] = {"task": "x" * 8000, "constraints": "x"}
    sample_brief = routes_handoff.format_handoff_brief(sample, "Target", "/repo", branch, None)
    constraints = "x" * (limit - len(sample_brief) + 1)
    assert len(constraints) > 0
    monkeypatch.setattr(env["host_registry"], "get", lambda _hid: SimpleNamespace())

    async def worktrees(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"branch": "main", "path": "/repo"}]

    async def create_session(*_args: Any, conversation_id: str, **_kwargs: Any) -> Any:
        env["conversations"].create_conversation(
            conversation_id=conversation_id,
            agent_id=env["agent_id"],
            project_id=env["project"].id,
            host_id="1" * 32,
            workspace=workspace,
            git_branch=branch,
        )
        return SimpleNamespace(id=conversation_id)

    monkeypatch.setattr(routes_handoff, "list_worktrees_on_host", worktrees)
    monkeypatch.setattr(routes_handoff, "_create_session_from_existing_agent", create_session)
    body = {
        "project": "Target",
        "task": "x" * 8000,
        "constraints": constraints,
        "branch": branch,
        "base_branch": "main",
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        oversized = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={**body, "constraints": constraints + "x"},
            headers=_headers(env["sender_token"]),
        )
        accepted = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json=body,
            headers=_headers(env["sender_token"]),
        )
    assert oversized.status_code == 400, oversized.text
    assert oversized.json()["error"]["code"] == "invalid_input"
    assert str(limit) in oversized.json()["error"]["message"]
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["state"] == "delivered"
    record = env["handoffs"].get(accepted.json()["handoff_id"])
    assert record is not None and len(record.brief) <= 16000
    assert env["peers"].get(record.brief_peer_id) is not None


@pytest.mark.asyncio
async def test_create_existing_branch_conflict_returns_needs_input(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes.sessions import routes_handoff

    env = handoff_env
    monkeypatch.setattr(env["host_registry"], "get", lambda _hid: SimpleNamespace())

    async def worktrees(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"branch": "main", "path": "/repo"}]

    async def branch_conflict(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("branch 'review-branch' already exists")

    monkeypatch.setattr(routes_handoff, "list_worktrees_on_host", worktrees)
    monkeypatch.setattr(routes_handoff, "_create_session_from_existing_agent", branch_conflict)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={
                "project": "Target",
                "task": "Review code",
                "branch": "review-branch",
                "base_branch": "main",
            },
            headers=_headers(env["sender_token"]),
        )
    assert start.status_code == 200, start.text
    assert start.json()["disposition"] == "needs_input"
    assert start.json()["reason"] == "branch_exists"
    assert env["handoffs"].get(start.json()["handoff_id"]).state == "failed"


@pytest.mark.asyncio
async def test_long_branch_create_conflict_returns_needs_input(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes.sessions import routes_handoff

    env = handoff_env
    branch = "b" * 80
    monkeypatch.setattr(env["host_registry"], "get", lambda _hid: SimpleNamespace())

    async def worktrees(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"branch": "main", "path": "/repo"}]

    async def branch_conflict(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"worktree creation failed: branch '{branch}' already exists")

    monkeypatch.setattr(routes_handoff, "list_worktrees_on_host", worktrees)
    monkeypatch.setattr(routes_handoff, "_create_session_from_existing_agent", branch_conflict)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={
                "project": "Target",
                "task": "Review code",
                "branch": branch,
                "base_branch": "main",
            },
            headers=_headers(env["sender_token"]),
        )
    assert start.status_code == 200, start.text
    assert start.json()["disposition"] == "needs_input"
    assert start.json()["reason"] == "branch_exists"
    record = env["handoffs"].get(start.json()["handoff_id"])
    assert record is not None and record.reason.startswith("create_failed: branch_exists:")


@pytest.mark.asyncio
async def test_existing_branch_uses_worktree_path(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes.sessions import routes_handoff

    env = handoff_env
    monkeypatch.setattr(env["host_registry"], "get", lambda _hid: SimpleNamespace())

    async def worktrees(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"branch": "review-branch", "path": "/repo-worktrees/review-branch"}]

    monkeypatch.setattr(routes_handoff, "list_worktrees_on_host", worktrees)
    captured: list[Any] = []

    async def create_session(*args: Any, conversation_id: str, **_kwargs: Any) -> Any:
        body = args[3]
        captured.append(body)
        env["conversations"].create_conversation(
            conversation_id=conversation_id,
            agent_id=env["agent_id"],
            project_id=env["project"].id,
            host_id="1" * 32,
            workspace=body.workspace,
            git_branch="review-branch",
            title="Hand-off: Review code",
        )
        return SimpleNamespace(id=conversation_id)

    monkeypatch.setattr(routes_handoff, "_create_session_from_existing_agent", create_session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={
                "project": "Target",
                "task": "Review code",
                "branch": "review-branch",
                "existing_branch": True,
            },
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200, response.text
    assert captured[0].workspace == "/repo-worktrees/review-branch"
    assert captured[0].git.existing_worktree is True


@pytest.mark.asyncio
async def test_expired_queued_brief_is_not_delivered(handoff_env: dict[str, Any]) -> None:
    from omnigent.db.utils import now_epoch

    env = handoff_env
    env["conversations"].set_labels(env["receiver"].id, {"peer_inbound": "hold"})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
    assert start.status_code == 200
    hid = start.json()["handoff_id"]
    record = env["handoffs"].get(hid)
    assert record is not None and record.state == "open"
    assert env["peers"].transition(
        record.brief_peer_id, "expired", "handoff_expired", expected_states=("held",)
    )
    env["handoffs"].set_fields(hid, ("open",), expires_at=now_epoch() - 1)
    await _run_pass(env)
    updated = env["handoffs"].get(hid)
    assert updated is not None
    assert updated.state == "expired"
    assert updated.reason == "not_delivered"
    assert updated.stop_peer_id is None


@pytest.mark.asyncio
async def test_agent_public_name_is_case_insensitive(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code", "agent": "TEST-AGENT"},
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "delivered"


@pytest.mark.asyncio
async def test_onward_requires_permission_and_inherits_deadline(
    handoff_env: dict[str, Any],
) -> None:
    from omnigent.db.utils import now_epoch

    env = handoff_env
    parent = _record(env, state="delivered", receiver_id=env["sender"].id)
    parent.expires_at = now_epoch() + 300
    env["handoffs"].create(parent)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        url = f"/v1/sessions/{env['sender'].id}/handoffs"
        blocked = await client.post(
            url,
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        assert blocked.status_code == 200
        assert blocked.json()["reason"] == "onward_not_permitted"
        env["handoffs"].set_fields(parent.id, ("delivered",), allow_onward=True)
        allowed = await client.post(
            url,
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
    assert allowed.status_code == 200, allowed.text
    record = env["handoffs"].get(allowed.json()["handoff_id"])
    assert record is not None
    assert record.parent_handoff_id == parent.id
    assert record.expires_at == parent.expires_at


@pytest.mark.asyncio
async def test_same_workspace_on_another_host_is_not_reused(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes.sessions import routes_handoff

    env = handoff_env
    other_host = SimpleNamespace(host_id="2" * 32, name="host-two", user_id=ALICE, deleted_at=None)
    monkeypatch.setattr(
        env["host_store"],
        "get_host",
        lambda hid: other_host if hid == other_host.host_id else None,
    )
    monkeypatch.setattr(env["host_store"], "list_hosts", lambda _user: [other_host])
    config = dict(env["project"].config)
    config["host_id"] = other_host.host_id
    env["projects"].update(env["project"].id, user_id=ALICE, config=config)

    async def create_session(*_args: Any, conversation_id: str, **_kwargs: Any) -> Any:
        env["conversations"].create_conversation(
            conversation_id=conversation_id,
            agent_id=env["agent_id"],
            project_id=env["project"].id,
            host_id=other_host.host_id,
            workspace="/repo",
            title="Hand-off: Review code",
        )
        return SimpleNamespace(id=conversation_id)

    monkeypatch.setattr(routes_handoff, "_create_session_from_existing_agent", create_session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200, response.text
    assert response.json()["session"]["id"] != env["receiver"].id
    assert response.json()["session"]["host_id"] == other_host.host_id
    assert response.json()["disclosure"]["reused"] is False


@pytest.mark.asyncio
async def test_cancel_delivering_brief_requests_stop(handoff_env: dict[str, Any]) -> None:
    env = handoff_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        start = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
        assert start.status_code == 200
        hid = start.json()["handoff_id"]
        record = env["handoffs"].get(hid)
        assert record is not None
        assert env["peers"].transition(
            record.brief_peer_id, "delivering", expected_states=("delivered",)
        )
        cancelled = await client.post(
            f"/v1/handoffs/{hid}/cancel", headers=_headers(env["sender_token"])
        )
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancel_requested"
    assert cancelled.json()["stop_state"] == "sent"


@pytest.mark.asyncio
async def test_generated_branch_needs_explicit_base(
    handoff_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes.sessions import routes_handoff

    env = handoff_env
    config = dict(env["project"].config)
    config["use_worktree"] = True
    env["projects"].update(env["project"].id, user_id=ALICE, config=config)
    monkeypatch.setattr(env["host_registry"], "get", lambda _hid: SimpleNamespace())

    async def worktrees(**_kwargs: Any) -> list[dict[str, Any]]:
        return [{"branch": "main", "path": "/repo"}]

    monkeypatch.setattr(routes_handoff, "list_worktrees_on_host", worktrees)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=env["app"]), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{env['sender'].id}/handoffs",
            json={"project": "Target", "task": "Review code"},
            headers=_headers(env["sender_token"]),
        )
    assert response.status_code == 200
    assert response.json()["reason"] == "base_branch_required"
