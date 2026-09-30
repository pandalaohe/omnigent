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
from omnigent.entities import Agent, ProjectHostBinding
from omnigent.errors import OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server import session_open_rate
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.routes._host_worktree import WorktreeProxyError
from omnigent.server.routes._sessions.helpers import SessionLiveness
from omnigent.server.routes.sessions import routes_open
from omnigent.server.routes.sessions.routes_open import register_open_routes
from omnigent.server.routes.sessions.routes_peer import register_peer_routes
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY
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


def _patch_worktrees(
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> list[str]:
    """Stub the host worktree listing and record the requested repo paths."""
    seen: list[str] = []

    async def _list(*, host_registry: Any, host_conn: Any, repo_path: str) -> Any:
        seen.append(repo_path)
        if error is not None:
            raise error
        return rows if rows is not None else []

    monkeypatch.setattr(routes_open, "list_worktrees_on_host", _list)
    return seen


def _holder(env: dict[str, Any], title: str, **overrides: Any) -> Any:
    """Create one live top-level session in the project root."""
    conv = env["conversations"].create_conversation(
        agent_id=env["agent_id"],
        title=title,
        host_id=HOST_ID,
        workspace="/repo",
        runner_id=token_bound_runner_id(secrets.token_hex(16)),
        **overrides,
    )
    env["permissions"].grant(ALICE, conv.id, LEVEL_OWNER)
    return conv


@pytest.mark.asyncio
async def test_occupied_root_opens_and_lists_shared_with(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared root opens; ``shared_with`` lists the live holder only."""
    env = open_env
    live = _holder(env, "live-holder")
    archived = _holder(env, "archived-holder")
    env["conversations"].update_conversation(archived.id, archived=True)
    closed = _holder(env, "closed-holder")
    env["conversations"].set_labels(closed.id, {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE})
    side = _holder(env, "side-chat")
    env["conversations"].set_labels(side.id, {SIDE_CHAT_LABEL_KEY: "true"})
    captured = _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"])
    assert data["state"] == "opened"
    assert data["shared_with"] == [{"id": live.id, "name": "live-holder"}]
    assert "shared_with_total" not in data
    assert captured["body"].git is None


@pytest.mark.asyncio
async def test_shared_with_caps_at_ten_with_total(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """More than ten holders cap ``shared_with`` and report the full count."""
    env = open_env
    holders = [_holder(env, f"holder-{index}") for index in range(12)]
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"])
    assert data["state"] == "opened"
    assert len(data["shared_with"]) == 10
    assert data["shared_with_total"] == len(holders)
    assert data["session_id"] not in {item["id"] for item in data["shared_with"]}


@pytest.mark.asyncio
async def test_two_concurrent_root_opens_both_succeed(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two root opens in flight at once both create their sessions."""
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
        second = await asyncio.wait_for(
            _post(client, env["sender"].id, env["sender_token"]), timeout=5
        )
        gate.set()
        response = await first
    assert response.status_code == 200
    assert response.json()["state"] == "opened"
    assert second["state"] == "opened"
    assert second["session_id"] != response.json()["session_id"]


@pytest.mark.asyncio
async def test_shared_with_omitted_for_a_new_branch_worktree(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A from_ref/branch open cuts a fresh worktree, so nothing is shared."""
    env = open_env
    _holder(env, "root-holder")
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], from_ref="main")
    assert data["state"] == "opened"
    assert data["shared_with"] == []
    assert "shared_with_total" not in data


@pytest.mark.asyncio
@pytest.mark.parametrize("from_ref", ["", "   "])
async def test_blank_from_ref_opens_plain(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, from_ref: str
) -> None:
    """A blank ``from_ref`` is no ref: a plain root open with no worktree."""
    env = open_env
    captured = _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], from_ref=from_ref)
    assert data["state"] == "opened"
    assert captured["body"].git is None
    assert captured["body"].workspace == "/repo"


@pytest.mark.asyncio
async def test_failed_create_does_not_block_a_later_open(
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
    """A create that fails after persisting leaves the row owned."""
    env = open_env
    captured = _patch_create(env, monkeypatch, error=Exception("boom"), persist_before_error=True)
    async with await _client(env) as client:
        failed = await _post(client, env["sender"].id, env["sender_token"])
        assert failed["state"] == "failed"
        assert failed["reason"] == "create_failed"
        sid = captured["kwargs"]["conversation_id"]
        assert env["conversations"].get_conversation(sid) is not None
        assert env["permissions"].get(ALICE, sid) is not None


@pytest.mark.asyncio
async def test_pending_fire_into_an_occupied_root_opens(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A waiting root open fires even though a session took the directory."""
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
    _holder(env, "holder")
    env["online"]["on"] = True
    env["app"].state.pending_session_opens.trigger(HOST_ID)
    assert await _wait_for(lambda: len(lines) == 1)
    assert lines == [f"[System: session {sid} opened on host {HOST_NAME}]"]


@pytest.mark.asyncio
async def test_rate_limits_the_eleventh_open(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The open past the window's count is refused with the setting text."""
    env = open_env
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        for _ in range(10):
            data = await _post(client, env["sender"].id, env["sender_token"], from_ref="main")
            assert data["state"] == "opened"
        refused = await _post(client, env["sender"].id, env["sender_token"], from_ref="main")
    assert refused["state"] == "refused"
    assert refused["reason"] == "open_rate"
    assert refused["message"] == (
        "Opening sessions too fast (setting: 10 per 1 minute; Settings > Session collaboration)"
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
async def test_workspace_joins_a_listed_worktree(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace matching a listed worktree binds with its branch."""
    env = open_env
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    captured = _patch_create(env, monkeypatch)
    seen = _patch_worktrees(
        monkeypatch,
        rows=[
            {
                "path": "/repo/.worktrees/task",
                "branch": "task/fix",
                "is_main": False,
                "detached": False,
            },
            {"path": "/repo", "branch": "main", "is_main": True, "detached": False},
        ],
    )
    async with await _client(env) as client:
        data = await _post(
            client, env["sender"].id, env["sender_token"], workspace="/repo/.worktrees/task"
        )
    assert data["state"] == "opened"
    assert seen == ["/repo"]
    body = captured["body"]
    assert body.workspace == "/repo/.worktrees/task"
    assert body.git is not None
    assert body.git.branch_name == "task/fix"
    assert body.git.existing_worktree is True
    assert data["shared_with"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "rows",
    [
        [{"path": "/repo/.worktrees/other", "branch": "other", "is_main": False}],
        [{"path": "/repo/sub", "branch": "", "is_main": False, "detached": True}],
    ],
    ids=["no-matching-path", "detached-head"],
)
async def test_workspace_not_a_worktree_places_plainly(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, rows: list[dict[str, Any]]
) -> None:
    """A subdirectory or detached worktree gets no git options."""
    env = open_env
    captured = _patch_create(env, monkeypatch)
    _patch_worktrees(monkeypatch, rows=rows)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], workspace="/repo/sub")
    assert data["state"] == "opened"
    assert captured["body"].workspace == "/repo/sub"
    assert captured["body"].git is None


@pytest.mark.asyncio
async def test_workspace_listing_failure_places_plainly(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host listing failure falls back to plain placement, never a refusal."""
    env = open_env
    captured = _patch_create(env, monkeypatch)
    _patch_worktrees(monkeypatch, error=WorktreeProxyError("listing failed"))
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], workspace="/repo/sub")
    assert data["state"] == "opened"
    assert captured["body"].workspace == "/repo/sub"
    assert captured["body"].git is None


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", ["/elsewhere", "relative/dir"])
async def test_workspace_outside_the_project_refused(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, workspace: str
) -> None:
    """A workspace outside (or not inside) the root is a needs_input problem."""
    env = open_env
    captured = _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], workspace=workspace)
    assert data["state"] == "needs_input"
    assert data["reason"] == "workspace_outside_project"
    assert "body" not in captured, "a placement refusal must not create a session"


@pytest.mark.asyncio
async def test_workspace_dotdot_traversal_refused(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workspace that only escapes the root after lexical normalisation is refused."""
    env = open_env
    captured = _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(
            client, env["sender"].id, env["sender_token"], workspace="/repo/../outside"
        )
    assert data["state"] == "needs_input"
    assert data["reason"] == "workspace_outside_project"
    assert "body" not in captured, "a placement refusal must not create a session"


@pytest.mark.asyncio
async def test_workspace_dotdot_segments_normalised(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-root workspace reaches create and worktree matching without ``..``."""
    env = open_env
    captured = _patch_create(env, monkeypatch)
    seen = _patch_worktrees(
        monkeypatch,
        rows=[
            {"path": "/repo/task", "branch": "task/fix", "is_main": False, "detached": False},
        ],
    )
    async with await _client(env) as client:
        data = await _post(
            client, env["sender"].id, env["sender_token"], workspace="/repo/sub/../task"
        )
    assert data["state"] == "opened"
    assert seen == ["/repo"]
    assert captured["body"].workspace == "/repo/task"
    assert captured["body"].git is not None
    assert captured["body"].git.branch_name == "task/fix"


@pytest.mark.asyncio
@pytest.mark.parametrize("companion", [{"branch": "task/fix"}, {"from_ref": "main"}])
async def test_workspace_with_a_branch_refused(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, companion: dict[str, Any]
) -> None:
    """workspace and branch/from_ref are exclusive directions."""
    env = open_env
    captured = _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(
            client, env["sender"].id, env["sender_token"], workspace="/repo/sub", **companion
        )
    assert data["state"] == "needs_input"
    assert data["reason"] == "workspace_with_branch"
    assert "workspace joins an existing directory" in data["message"]
    assert "body" not in captured


@pytest.mark.asyncio
async def test_branch_alone_cuts_a_new_branch_worktree(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """branch alone rides SessionGitOptions with no base."""
    env = open_env
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    captured = _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], branch="task/fix")
    assert data["state"] == "opened"
    git = captured["body"].git
    assert git is not None
    assert git.branch_name == "task/fix"
    assert git.base_branch is None
    assert git.existing_worktree is False


@pytest.mark.asyncio
async def test_branch_with_from_ref_uses_it_as_the_base(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """branch + from_ref branches the new branch from that ref."""
    env = open_env
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    captured = _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(
            client,
            env["sender"].id,
            env["sender_token"],
            branch="task/fix",
            from_ref="release/1.2",
        )
    assert data["state"] == "opened"
    git = captured["body"].git
    assert git is not None
    assert git.branch_name == "task/fix"
    assert git.base_branch == "release/1.2"


@pytest.mark.asyncio
async def test_branch_on_a_binding_only_root_needs_an_entry(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A binding-supplied root cannot cut a worktree without an entry."""
    env = open_env
    binding = ProjectHostBinding(
        "b1", env["project"].id, HOST_ID, "primary", "repo", "/bound", 1, 1, is_primary=True
    )

    async def _bindings(*_args: Any, **_kwargs: Any) -> list[Any]:
        return [binding]

    monkeypatch.setattr(routes_open, "load_bindings", _bindings)
    _patch_create(env, monkeypatch)
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], branch="task/fix")
    assert data["state"] == "needs_input"
    assert data["reason"] == "no_entry_for_worktree"


@pytest.mark.asyncio
async def test_branch_exists_reason_and_join_hint(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An existing branch is reported with the workspace join hint."""
    env = open_env
    env["bindings_store"].put_entry(env["project"].id, HOST_ID, "/repo")
    _patch_create(env, monkeypatch, error=Exception("branch already exists"))
    async with await _client(env) as client:
        data = await _post(client, env["sender"].id, env["sender_token"], branch="task/fix")
    assert data["state"] == "failed"
    assert data["reason"] == "branch_exists"
    assert "pass workspace=" in data["message"]


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


@pytest.mark.asyncio
async def test_master_switch_off_refuses_open(
    open_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the owner's collaboration switch off the route refuses; on admits."""
    env = open_env
    _patch_create(env, monkeypatch)
    env["app"].state.user_preferences_store = _FakePreferences({"enabled": False})
    async with await _client(env) as client:
        refused = await _post(client, env["sender"].id, env["sender_token"])
        assert refused["state"] == "refused"
        assert refused["reason"] == "collab_disabled"
        env["app"].state.user_preferences_store = _FakePreferences({"enabled": True})
        opened = await _post(client, env["sender"].id, env["sender_token"])
    assert opened["state"] == "opened"
