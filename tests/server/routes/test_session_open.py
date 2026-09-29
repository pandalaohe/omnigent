"""Session-open routes with durable SQLite stores and a fake host.

Covers every refusal of the resolver chain, the immediate open (top-level,
owned, on the named host), ``from_ref`` worktree options, the first
message as an ordinary peer send, and the pending-open registry's fire /
expire paths.
"""

from __future__ import annotations

import asyncio
import secrets
import time
import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.utils import generate_agent_id
from omnigent.entities import Agent
from omnigent.errors import OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server import session_open_rate
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.routes._sessions.helpers import SessionLiveness
from omnigent.server.routes.sessions import routes_open
from omnigent.server.routes.sessions.routes_open import register_open_routes
from omnigent.server.routes.sessions.routes_peer import register_peer_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_host_binding_store.sqlalchemy_store import (
    SqlAlchemyProjectHostBindingStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE

ALICE = "alice@example.com"
BOB = "bob@example.com"
HOST_ID = "1" * 32
HOST_NAME = "host-one"
HOST2_ID = "2" * 32
HOST2_NAME = "host-two"


class _FakePreferences:
    """Minimal ``user_preferences_store`` for the collab namespace."""

    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self._settings = settings

    def get(self, _user_id: str) -> dict[str, Any] | None:
        if self._settings is None:
            return None
        return {"version": 1, "settings": {"session_collab": self._settings}}


class _PeerProxy:
    """Delegating peer routes so tests can spy on ``send``."""

    def __init__(self, real: Any) -> None:
        self._real = real
        self.send = real.send
        self.true_state = real.true_state


@pytest.fixture()
def open_env(db_uri: str) -> dict[str, Any]:
    session_open_rate._OPEN_TIMESTAMPS.clear()
    agents = SqlAlchemyAgentStore(db_uri)
    agent_id = generate_agent_id()
    agents.create(agent_id, "test-agent", "test:///bundle")
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(ALICE)
    permissions.ensure_user(BOB)
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(
        uuid.uuid4().hex,
        "Target",
        ALICE,
        {"host_id": HOST_ID, "workspace": "/repo", "agent_id": agent_id},
    )
    sender_token = secrets.token_hex(16)
    sender = conversations.create_conversation(
        agent_id=agent_id, title="sender", runner_id=token_bound_runner_id(sender_token)
    )
    permissions.grant(ALICE, sender.id, LEVEL_OWNER)
    peers = SqlAlchemyPeerMessageStore(db_uri)
    host = SimpleNamespace(host_id=HOST_ID, name=HOST_NAME, user_id=ALICE, deleted_at=None)
    host2 = SimpleNamespace(host_id=HOST2_ID, name=HOST2_NAME, user_id=ALICE, deleted_at=None)
    host_store = SimpleNamespace(
        list_hosts=lambda user: [host, host2] if user == ALICE else [],
        get_host=lambda hid: {HOST_ID: host, HOST2_ID: host2}.get(hid),
    )
    online = {"on": True}
    host_registry = SimpleNamespace(
        get=lambda hid: SimpleNamespace() if online["on"] and hid == HOST_ID else None
    )
    app = FastAPI()
    app.state.peer_message_store = peers
    app.state.host_store = host_store
    bindings_store = SqlAlchemyProjectHostBindingStore(db_uri)
    app.state.project_host_binding_store = bindings_store
    app.state.host_registry = host_registry

    @app.exception_handler(OmnigentError)
    async def omnigent_error(_request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    async def post_event(
        _request: Request, _session_id: str, _body: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        return {"item_id": uuid.uuid4().hex, "queued": True}

    flags = resolve_feature_flags({"OMNIGENT_FEATURES": "session_peer_messaging"})
    router = APIRouter()
    real_peer = register_peer_routes(
        router,
        post_event_impl=post_event,
        conversation_store=conversations,
        permission_store=permissions,
        auth_provider=UnifiedAuthProvider(source="header"),
        liveness_lookup=lambda ids: {sid: SessionLiveness(False, False) for sid in ids},
        runner_tunnel_tokens=None,
        feature_flags=flags,
        peer_message_store=peers,
        runner_router=None,
        agent_store=agents,
        app_state=app.state,
    )
    proxy = _PeerProxy(real_peer)
    register_open_routes(
        router,
        peer=proxy,  # type: ignore[arg-type]
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
        "sender_token": sender_token,
        "peers": peers,
        "project": project,
        "projects": projects,
        "agents": agents,
        "agent_id": agent_id,
        "host_store": host_store,
        "host_registry": host_registry,
        "bindings_store": bindings_store,
        "conversations": conversations,
        "permissions": permissions,
        "peer": proxy,
        "online": online,
    }


def _headers(token: str, user: str = ALICE) -> dict[str, str]:
    return {RUNNER_TUNNEL_TOKEN_HEADER: token, "X-Forwarded-Email": user}


async def _client(env: dict[str, Any]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=env["app"]), base_url="http://test")


def _body(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"project": "Target", "host": HOST_NAME, "agent": "test-agent"}
    base.update(overrides)
    return base


async def _post(
    client: httpx.AsyncClient, sender_id: str, token: str, **overrides: Any
) -> dict[str, Any]:
    response = await client.post(
        f"/v1/sessions/{sender_id}/open",
        json=_body(**overrides),
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


def _patch_create(
    env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    *,
    error: Exception | None = None,
    persist_before_error: bool = False,
) -> dict[str, Any]:
    """Replace the create orchestration and record its call.

    :param error: Exception the replacement raises, or ``None`` to succeed.
    :param persist_before_error: Write the conversation row before raising,
        modelling a create that fails after its row is durable.
    """
    captured: dict[str, Any] = {}

    async def create_session(*args: Any, **kwargs: Any) -> Any:
        body = args[3]
        sid = kwargs["conversation_id"]
        captured["body"] = body
        captured["kwargs"] = kwargs
        if error is not None and not persist_before_error:
            raise error
        conv = env["conversations"].create_conversation(
            conversation_id=sid,
            agent_id=body.agent_id,
            title=body.title or "opened",
            host_id=body.host_id,
            workspace=body.workspace,
            project_id=body.project_id,
            runner_id=token_bound_runner_id(secrets.token_hex(16)),
        )
        if error is not None:
            raise error
        return SimpleNamespace(id=sid), conv

    monkeypatch.setattr(routes_open, "_create_session_from_existing_agent", create_session)
    return captured


def _patch_create_gated(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[asyncio.Event, dict[str, Any]]:
    """Replace the create orchestration; the first call waits for the event."""
    gate = asyncio.Event()
    captured: dict[str, Any] = {"calls": 0, "sids": []}

    async def create_session(*args: Any, **kwargs: Any) -> Any:
        body = args[3]
        sid = kwargs["conversation_id"]
        captured["calls"] += 1
        captured["sids"].append(sid)
        if captured["calls"] == 1:
            await gate.wait()
        conv = env["conversations"].create_conversation(
            conversation_id=sid,
            agent_id=body.agent_id,
            title=body.title or "opened",
            host_id=body.host_id,
            workspace=body.workspace,
            project_id=body.project_id,
            runner_id=token_bound_runner_id(secrets.token_hex(16)),
        )
        return SimpleNamespace(id=sid), conv

    monkeypatch.setattr(routes_open, "_create_session_from_existing_agent", create_session)
    return gate, captured


async def _wait_for(predicate: Any, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


@pytest.mark.asyncio
async def test_runner_token_is_required(open_env: dict[str, Any]) -> None:
    """A token not bound to the sender's runner is refused with 403."""
    async with await _client(open_env) as client:
        response = await client.post(
            f"/v1/sessions/{open_env['sender'].id}/open",
            json=_body(),
            headers=_headers(secrets.token_hex(16)),
        )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_child_sender_refused(open_env: dict[str, Any]) -> None:
    """A child session may not open another session."""
    env = open_env
    child_token = secrets.token_hex(16)
    child = env["conversations"].create_conversation(
        agent_id=env["agent_id"],
        title="child",
        parent_conversation_id=env["sender"].id,
        runner_id=token_bound_runner_id(child_token),
    )
    env["permissions"].grant(ALICE, child.id, LEVEL_OWNER)
    async with await _client(env) as client:
        data = await _post(client, child.id, child_token)
    assert data["state"] == "refused"
    assert data["reason"] == "is_subagent"


@pytest.mark.asyncio
async def test_sender_without_owner_refused(open_env: dict[str, Any]) -> None:
    """A sender with no owner grant is not the same owner."""
    env = open_env
    token = secrets.token_hex(16)
    orphan = env["conversations"].create_conversation(
        agent_id=env["agent_id"],
        title="orphan",
        runner_id=token_bound_runner_id(token),
    )
    async with await _client(env) as client:
        data = await _post(client, orphan.id, token)
    assert data["state"] == "refused"
    assert data["reason"] == "not_same_owner"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("project", "reason"),
    [("missing", "project_not_found"), ("Other", "project_ambiguous")],
)
async def test_project_resolution_refusals(
    open_env: dict[str, Any], project: str, reason: str
) -> None:
    """An unknown or ambiguous project is ``needs_input`` with candidates."""
    env = open_env
    if reason == "project_ambiguous":
        env["projects"].create(uuid.uuid4().hex, "Other", ALICE, {})
        env["projects"].create(uuid.uuid4().hex, "other", ALICE, {})
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], project=project)
    assert data["state"] == "needs_input"
    assert data["reason"] == reason
    assert data["candidates"]


@pytest.mark.asyncio
async def test_host_not_found_lists_rooted_hosts(open_env: dict[str, Any]) -> None:
    """An unknown host is refused with the hosts that have a root."""
    env = open_env
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], host="nope")
    assert data["state"] == "needs_input"
    assert data["reason"] == "host_not_found"
    assert data["candidates"] == [{"id": HOST_ID, "name": HOST_NAME}]


@pytest.mark.asyncio
async def test_known_host_without_root_refused(open_env: dict[str, Any]) -> None:
    """A host the project has no directory on is ``no_root``."""
    env = open_env
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], host=HOST2_NAME)
    assert data["state"] == "needs_input"
    assert data["reason"] == "no_root"


@pytest.mark.asyncio
async def test_from_ref_requires_a_project_entry(open_env: dict[str, Any]) -> None:
    """A config-only root cannot host a branch worktree."""
    env = open_env
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], from_ref="main")
    assert data["state"] == "needs_input"
    assert data["reason"] == "no_entry_for_worktree"


@pytest.mark.asyncio
async def test_agent_not_found(open_env: dict[str, Any]) -> None:
    """An unknown agent is ``needs_input/agent_not_found``."""
    env = open_env
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], agent="nope")
    assert data["state"] == "needs_input"
    assert data["reason"] == "agent_not_found"


@pytest.mark.asyncio
async def test_read_shared_session_agent_of_another_owner_refused(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """READ sharing a session-scoped agent is not ownership."""
    env = open_env
    shared_agent_id = generate_agent_id()
    bob_conv = env["conversations"].create_conversation(
        agent_id=env["agent_id"], title="bob", runner_id=token_bound_runner_id("bob")
    )
    env["permissions"].grant(BOB, bob_conv.id, LEVEL_OWNER)
    env["permissions"].grant(ALICE, bob_conv.id, LEVEL_READ)
    shared_agent = Agent(
        id=shared_agent_id,
        created_at=1,
        name="shared-agent",
        bundle_location="test:///shared",
        session_id=bob_conv.id,
    )
    real_get = env["agents"].get
    monkeypatch.setattr(
        env["agents"],
        "get",
        lambda aid: shared_agent if aid == shared_agent_id else real_get(aid),
    )
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], agent=shared_agent_id)
    assert data["state"] == "needs_input"
    assert data["reason"] == "agent_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("archived", [False, True])
async def test_directory_in_use_counts_archived(open_env: dict[str, Any], archived: bool) -> None:
    """An occupied directory refuses; an archived holder still counts."""
    env = open_env
    holder = env["conversations"].create_conversation(
        agent_id=env["agent_id"],
        title="holder",
        host_id=HOST_ID,
        workspace="/repo",
        runner_id=token_bound_runner_id(secrets.token_hex(16)),
    )
    env["permissions"].grant(ALICE, holder.id, LEVEL_OWNER)
    if archived:
        env["conversations"].update_conversation(holder.id, archived=True)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"])
    assert data["state"] == "refused"
    assert data["reason"] == "directory_in_use"
    assert "from_ref" in data["message"]
    assert {"id": holder.id, "name": "holder"} in data["candidates"]


@pytest.mark.asyncio
async def test_closed_session_does_not_occupy(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A closed session leaves the directory free for a no-ref open."""
    env = open_env
    holder = env["conversations"].create_conversation(
        agent_id=env["agent_id"],
        title="closed-holder",
        host_id=HOST_ID,
        workspace="/repo",
        runner_id=token_bound_runner_id(secrets.token_hex(16)),
    )
    env["conversations"].set_labels(holder.id, {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE})
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"])
    assert data["state"] == "opened"


@pytest.mark.asyncio
@pytest.mark.parametrize("from_ref", ["", "   "])
async def test_blank_from_ref_counts_as_no_ref(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, from_ref: str
) -> None:
    """A blank ``from_ref`` does not dodge the directory check."""
    env = open_env
    holder = env["conversations"].create_conversation(
        agent_id=env["agent_id"],
        title="holder",
        host_id=HOST_ID,
        workspace="/repo",
        runner_id=token_bound_runner_id(secrets.token_hex(16)),
    )
    env["permissions"].grant(ALICE, holder.id, LEVEL_OWNER)
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], from_ref=from_ref)
    assert data["state"] == "refused"
    assert data["reason"] == "directory_in_use"


@pytest.mark.asyncio
async def test_stalled_open_reserves_the_root(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A no-ref open still creating refuses a second root open meanwhile."""
    env = open_env
    gate, captured = _patch_create_gated(env, monkeypatch)
    async with await _client(env) as client:
        first = asyncio.create_task(
            client.post(
                f"/v1/sessions/{env['sender'].id}/open",
                json=_body(),
                headers=_headers(env["sender_token"]),
            )
        )
        assert await _wait_for(lambda: captured["calls"] == 1)
        sid = captured["sids"][0]
        second = await asyncio.wait_for(
            _post(client, env["sender"].id, env["sender_token"]), timeout=5
        )
        assert second["state"] == "refused"
        assert second["reason"] == "directory_in_use"
        assert f"{sid} (opening)" in second["message"]
        assert {"id": sid, "name": f"opening {sid[:8]}"} in second["candidates"]
        gate.set()
        response = await first
        assert response.status_code == 200
        first_data = response.json()
        assert first_data["state"] == "opened"
        assert first_data["session_id"] == sid
        third = await _post(client, env["sender"].id, env["sender_token"])
    assert third["state"] == "refused"
    assert third["reason"] == "directory_in_use"
    assert {"id": sid, "name": "opened"} in third["candidates"]
    assert all(not candidate["name"].startswith("opening ") for candidate in third["candidates"])


@pytest.mark.asyncio
async def test_stalled_open_does_not_block_a_worktree_open(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stalled no-ref open does not hold the lock for a from_ref open."""
    env = open_env
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    gate, captured = _patch_create_gated(env, monkeypatch)
    async with await _client(env) as client:
        first = asyncio.create_task(
            client.post(
                f"/v1/sessions/{env['sender'].id}/open",
                json=_body(),
                headers=_headers(env["sender_token"]),
            )
        )
        assert await _wait_for(lambda: captured["calls"] == 1)
        worktree = await asyncio.wait_for(
            _post(client, env["sender"].id, env["sender_token"], from_ref="main"), timeout=5
        )
        assert worktree["state"] == "opened"
        gate.set()
        response = await first
    assert response.status_code == 200
    first_data = response.json()
    assert first_data["state"] == "opened"
    assert first_data["session_id"] != worktree["session_id"]


@pytest.mark.asyncio
async def test_failed_create_releases_the_reservation(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create that raises before writing a row frees the root and drops the grant."""
    env = open_env
    captured = _patch_create(env, monkeypatch, error=Exception("boom"))
    async with await _client(env) as client:
        failed = await _post(client, env["sender"].id, env["sender_token"])
        assert failed["state"] == "failed"
        assert failed["reason"] == "create_failed"
        sid = captured["kwargs"]["conversation_id"]
        assert env["conversations"].get_conversation(sid) is None
        assert env["permissions"].get(ALICE, sid) is None
        _patch_create(env, monkeypatch)
        opened = await _post(client, env["sender"].id, env["sender_token"])
    assert opened["state"] == "opened"


@pytest.mark.asyncio
async def test_failed_create_after_row_keeps_the_session_owned(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A create that fails after persisting leaves the row owned and occupying."""
    env = open_env
    captured = _patch_create(env, monkeypatch, error=Exception("boom"), persist_before_error=True)
    async with await _client(env) as client:
        failed = await _post(client, env["sender"].id, env["sender_token"])
        assert failed["state"] == "failed"
        assert failed["reason"] == "create_failed"
        sid = captured["kwargs"]["conversation_id"]
        assert env["conversations"].get_conversation(sid) is not None
        assert env["permissions"].get(ALICE, sid) is not None
        second = await _post(client, env["sender"].id, env["sender_token"])
    assert second["state"] == "refused"
    assert second["reason"] == "directory_in_use"
    assert sid in second["message"]
    assert {"id": sid, "name": "opened"} in second["candidates"]


@pytest.mark.asyncio
async def test_stalled_pending_fire_reserves_the_root(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fire that is still creating refuses a second root open meanwhile."""
    env = open_env
    env["online"]["on"] = False
    lines: list[str] = []

    async def notify_line(_sender_id: str, line: str) -> None:
        lines.append(line)

    monkeypatch.setattr(env["app"].state.peer_sweeper, "notify_line", notify_line)
    gate, captured = _patch_create_gated(env, monkeypatch)
    async with await _client(env) as client:
        waiting = await _post(client, env["sender"].id, env["sender_token"], wait_for_host=True)
        sid = waiting["session_id"]
        env["online"]["on"] = True
        env["app"].state.pending_session_opens.trigger(HOST_ID)
        assert await _wait_for(lambda: captured["calls"] == 1)
        refused = await asyncio.wait_for(
            _post(client, env["sender"].id, env["sender_token"]), timeout=5
        )
        assert refused["state"] == "refused"
        assert refused["reason"] == "directory_in_use"
        assert f"{sid} (opening)" in refused["message"]
        assert {"id": sid, "name": f"opening {sid[:8]}"} in refused["candidates"]
        gate.set()
        assert await _wait_for(lambda: len(lines) == 1)
    assert lines == [f"[System: session {sid} opened on host {HOST_NAME}]"]


@pytest.mark.asyncio
async def test_rate_limits_the_sixth_open(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sixth open inside the window is refused with the setting text."""
    env = open_env
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        for _ in range(5):
            data = await _post(client, env["sender"].id, env["sender_token"], from_ref="main")
            assert data["state"] == "opened"
        refused = await _post(client, env["sender"].id, env["sender_token"], from_ref="main")
    assert refused["state"] == "refused"
    assert refused["reason"] == "open_rate"
    assert refused["message"] == (
        "Opening sessions too fast (setting: 5 per 1 minute; "
        "Settings > General > Session collaboration)"
    )


@pytest.mark.asyncio
async def test_changed_open_rate_setting_is_honoured(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A smaller setting refuses sooner and a larger one admits again."""
    env = open_env
    prefs = _FakePreferences({"openRateCount": 2})
    env["app"].state.user_preferences_store = prefs
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        for _ in range(2):
            data = await _post(client, env["sender"].id, env["sender_token"], from_ref="main")
            assert data["state"] == "opened"
        refused = await _post(client, env["sender"].id, env["sender_token"], from_ref="main")
        assert refused["reason"] == "open_rate"
        assert "setting: 2 per 1 minute" in refused["message"]
        prefs._settings = {"openRateCount": 10}
        admitted = await _post(client, env["sender"].id, env["sender_token"], from_ref="main")
    assert admitted["state"] == "opened"


@pytest.mark.asyncio
async def test_host_offline_refused_without_wait(open_env: dict[str, Any]) -> None:
    """An offline host without ``wait_for_host`` is refused."""
    env = open_env
    env["online"]["on"] = False
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"])
    assert data["state"] == "refused"
    assert data["reason"] == "host_offline"
    assert "wait_for_host" in data["message"]


@pytest.mark.asyncio
async def test_open_creates_top_level_owned_session(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An immediate open creates a top-level session owned by the sender's user."""
    env = open_env
    captured = _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], title="opened here")
    assert data["state"] == "opened"
    assert data["project"] == "Target"
    assert data["host_id"] == HOST_ID
    assert data["host"] == HOST_NAME
    assert data["agent"] == "test-agent"
    assert data["workspace"] == "/repo"
    assert data["first_message"] is None
    sid = data["session_id"]
    conv = env["conversations"].get_conversation(sid)
    assert conv is not None and conv.parent_conversation_id is None
    assert conv.host_id == HOST_ID
    assert conv.workspace == "/repo"
    grants, _ = env["permissions"].list_for_session(sid, limit=100)
    assert any(g.user_id == ALICE and g.level >= LEVEL_OWNER for g in grants)
    assert captured["kwargs"]["calling_path_label"] == "sys_session_open"
    assert captured["kwargs"]["user_id"] == ALICE


@pytest.mark.asyncio
async def test_from_ref_creates_a_branch_worktree(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``from_ref`` rides ``SessionGitOptions`` with the generated branch."""
    env = open_env
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    captured = _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], from_ref="release/1.2")
    assert data["state"] == "opened"
    git = captured["body"].git
    assert git is not None
    assert git.branch_name == f"open-{data['session_id'][:8]}"
    assert git.base_branch == "release/1.2"
    assert git.existing_worktree is False


@pytest.mark.asyncio
async def test_create_failure_surfaces_branch_exists(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing branch is reported as ``failed/branch_exists``."""
    env = open_env
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    _patch_create(env, monkeypatch, error=Exception("branch already exists"))
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], from_ref="main")
    assert data["state"] == "failed"
    assert data["reason"] == "branch_exists"


@pytest.mark.asyncio
async def test_first_message_is_a_peer_send(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first message uses the peer contract, not a system envelope."""
    env = open_env
    _patch_create(env, monkeypatch)
    calls: list[dict[str, Any]] = []

    async def send(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"disposition": "delivered", "reason": None}

    monkeypatch.setattr(env["peer"], "send", send)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], message="please review")
    assert data["first_message"] == {"disposition": "delivered", "reason": None}
    assert len(calls) == 1
    call = calls[0]
    assert call["receiver_id"] == data["session_id"]
    assert call["text"] == "please review"
    assert call["correlation_id"] == data["session_id"]
    assert call["system"] is False
    assert call["require_init_success"] is True
    assert call["acting_user_id"] == ALICE


@pytest.mark.asyncio
async def test_waiting_open_fires_on_host_trigger(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiting open fires when the host connects and posts one line."""
    env = open_env
    env["online"]["on"] = False
    lines: list[str] = []

    async def notify_line(_sender_id: str, line: str) -> None:
        lines.append(line)

    monkeypatch.setattr(env["app"].state.peer_sweeper, "notify_line", notify_line)
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        waiting = await _post(client, env["sender"].id, env["sender_token"], wait_for_host=True)
    assert waiting["state"] == "waiting_for_host"
    sid = waiting["session_id"]
    env["online"]["on"] = True
    env["app"].state.pending_session_opens.trigger(HOST_ID)
    assert await _wait_for(lambda: len(lines) == 1)
    assert lines == [f"[System: session {sid} opened on host {HOST_NAME}]"]


@pytest.mark.asyncio
async def test_pending_from_ref_fires_despite_an_occupied_root(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiting ``from_ref`` open opens although a session holds the root."""
    env = open_env
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    holder = env["conversations"].create_conversation(
        agent_id=env["agent_id"],
        title="holder",
        host_id=HOST_ID,
        workspace="/repo",
        runner_id=token_bound_runner_id(secrets.token_hex(16)),
    )
    env["permissions"].grant(ALICE, holder.id, LEVEL_OWNER)
    env["online"]["on"] = False
    lines: list[str] = []

    async def notify_line(_sender_id: str, line: str) -> None:
        lines.append(line)

    monkeypatch.setattr(env["app"].state.peer_sweeper, "notify_line", notify_line)
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        waiting = await _post(
            client, env["sender"].id, env["sender_token"], from_ref="main", wait_for_host=True
        )
    assert waiting["state"] == "waiting_for_host"
    sid = waiting["session_id"]
    env["online"]["on"] = True
    env["app"].state.pending_session_opens.trigger(HOST_ID)
    assert await _wait_for(lambda: len(lines) == 1)
    assert lines == [f"[System: session {sid} opened on host {HOST_NAME}]"]


@pytest.mark.asyncio
async def test_pending_without_from_ref_fails_when_the_root_fills(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiting no-ref open fails ``directory_in_use`` on fire re-check."""
    env = open_env
    env["online"]["on"] = False
    lines: list[str] = []

    async def notify_line(_sender_id: str, line: str) -> None:
        lines.append(line)

    monkeypatch.setattr(env["app"].state.peer_sweeper, "notify_line", notify_line)
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        waiting = await _post(client, env["sender"].id, env["sender_token"], wait_for_host=True)
    sid = waiting["session_id"]
    holder = env["conversations"].create_conversation(
        agent_id=env["agent_id"],
        title="holder",
        host_id=HOST_ID,
        workspace="/repo",
        runner_id=token_bound_runner_id(secrets.token_hex(16)),
    )
    env["permissions"].grant(ALICE, holder.id, LEVEL_OWNER)
    env["online"]["on"] = True
    env["app"].state.pending_session_opens.trigger(HOST_ID)
    assert await _wait_for(lambda: len(lines) == 1)
    assert lines == [
        f"[System: session {sid} could not open on host {HOST_NAME}: directory_in_use]"
    ]


@pytest.mark.asyncio
async def test_pending_from_ref_does_not_block_an_immediate_root_open(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiting ``from_ref`` entry leaves the root free for a no-ref open."""
    env = open_env
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    env["online"]["on"] = False
    async with await _client(env) as client:
        waiting = await _post(
            client, env["sender"].id, env["sender_token"], from_ref="main", wait_for_host=True
        )
        assert waiting["state"] == "waiting_for_host"
        env["online"]["on"] = True
        _patch_create(env, monkeypatch)
        data = await _post(client, env["sender"].id, env["sender_token"])
    assert data["state"] == "opened"
    assert data["session_id"] != waiting["session_id"]


@pytest.mark.asyncio
async def test_fire_revalidates_session_agent_ownership(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pending open refuses when its agent becomes another owner's."""
    env = open_env
    env["online"]["on"] = False
    lines: list[str] = []

    async def notify_line(_sender_id: str, line: str) -> None:
        lines.append(line)

    monkeypatch.setattr(env["app"].state.peer_sweeper, "notify_line", notify_line)
    async with await _client(env) as client:
        waiting = await _post(client, env["sender"].id, env["sender_token"], wait_for_host=True)
    sid = waiting["session_id"]

    # The agent was a template at registration; by fire time it is a
    # session-scoped agent owned by Bob (Alice has only READ sharing).
    shared_agent_id = generate_agent_id()
    bob_conv = env["conversations"].create_conversation(
        agent_id=env["agent_id"], title="bob", runner_id=token_bound_runner_id("bob")
    )
    env["permissions"].grant(BOB, bob_conv.id, LEVEL_OWNER)
    env["permissions"].grant(ALICE, bob_conv.id, LEVEL_READ)
    shared_agent = Agent(
        id=shared_agent_id,
        created_at=1,
        name="shared-agent",
        bundle_location="test:///shared",
        session_id=bob_conv.id,
    )
    real_get = env["agents"].get
    monkeypatch.setattr(
        env["agents"],
        "get",
        lambda aid: shared_agent if aid == shared_agent_id else real_get(aid),
    )
    entry = env["app"].state.pending_session_opens._entries[sid]
    entry.agent_id = shared_agent_id

    env["online"]["on"] = True
    env["app"].state.pending_session_opens.trigger(HOST_ID)
    assert await _wait_for(lambda: len(lines) == 1)
    assert lines == [
        f"[System: session {sid} could not open on host {HOST_NAME}: agent_not_found]"
    ]


@pytest.mark.asyncio
async def test_waiting_open_expires_with_a_line(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unconnected host expires the entry and posts the expired line."""
    env = open_env
    env["app"].state.user_preferences_store = _FakePreferences({"undeliveredTtlSeconds": 1})
    env["online"]["on"] = False
    lines: list[str] = []

    async def notify_line(_sender_id: str, line: str) -> None:
        lines.append(line)

    monkeypatch.setattr(env["app"].state.peer_sweeper, "notify_line", notify_line)
    async with await _client(env) as client:
        waiting = await _post(client, env["sender"].id, env["sender_token"], wait_for_host=True)
    sid = waiting["session_id"]
    assert await _wait_for(lambda: len(lines) == 1, timeout=4.0)
    assert lines == [
        f"[System: session {sid} on host {HOST_NAME} expired: the host stayed offline]"
    ]


@pytest.mark.asyncio
async def test_fire_and_expire_race_runs_once(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fire and expiry claim the entry by pop: exactly one outcome line."""
    env = open_env
    env["online"]["on"] = False
    lines: list[str] = []

    async def notify_line(_sender_id: str, line: str) -> None:
        lines.append(line)

    monkeypatch.setattr(env["app"].state.peer_sweeper, "notify_line", notify_line)
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        waiting = await _post(client, env["sender"].id, env["sender_token"], wait_for_host=True)
    sid = waiting["session_id"]
    pending = env["app"].state.pending_session_opens
    env["online"]["on"] = True
    pending.trigger(HOST_ID)
    pending._expire(sid)
    await asyncio.sleep(0.2)
    assert len(lines) == 1


@pytest.mark.asyncio
async def test_fire_revalidates_the_host(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fire while the host is still offline reports the failed re-check."""
    env = open_env
    env["online"]["on"] = False
    lines: list[str] = []

    async def notify_line(_sender_id: str, line: str) -> None:
        lines.append(line)

    monkeypatch.setattr(env["app"].state.peer_sweeper, "notify_line", notify_line)
    async with await _client(env) as client:
        waiting = await _post(client, env["sender"].id, env["sender_token"], wait_for_host=True)
    sid = waiting["session_id"]
    env["app"].state.pending_session_opens.trigger(HOST_ID)
    assert await _wait_for(lambda: len(lines) == 1)
    assert lines == [f"[System: session {sid} could not open on host {HOST_NAME}: host_offline]"]
