"""Recent sessions: per-user touched label, write authority, and route.

The sidebar's "Recent sessions" section orders sessions by the caller's own
last interaction, recorded as ``omnigent.touched.<user>`` (epoch-ms,
zero-padded to 13 digits) on the session's ROOT. Only human-origin requests
write it: the public ``POST /sessions/{id}/events`` route and the elicitation
resolve route. Runner-authority requests, peer sends, runner/agent items,
denied messages, interrupts, and session opens never do.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.utils import generate_agent_id
from omnigent.errors import OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._sessions.helpers import _session_status_cache
from omnigent.server.routes._sessions.orchestration import _labels_for_viewer
from omnigent.server.routes.sessions import create_sessions_router
from omnigent.spec import AgentSpec
from omnigent.spec.types import GuardrailsSpec
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store import (
    SIDE_CHAT_LABEL_KEY,
    SIDE_CHAT_SOURCE_LABEL_KEY,
    TOUCHED_LABEL_KEY,
    is_touched_label_key,
    pinned_label_key,
    touched_label_key,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

ALICE = "alice@example.com"
BOB = "bob@example.com"

_POLICY_CACHE_PATCH = "omnigent.server.routes.sessions.get_agent_cache"
_POLICY_ENGINE_PATCH = "omnigent.server.routes.sessions.build_policy_engine"


def _user_message(text: str = "hello") -> dict[str, Any]:
    return {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }


def _seed_agent(db_uri: str, name: str = "recent-agent") -> str:
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_id = generate_agent_id()
    agent_store.create(agent_id, name=name, bundle_location="test:///bundle")
    return agent_id


class _RecentRoute:
    """Bare sessions-router app with a stubbed runner and recording store."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        store: SqlAlchemyConversationStore,
        agent_id: str,
        forwarded: list[httpx.Request],
    ) -> None:
        self.client = client
        self.store = store
        self.agent_id = agent_id
        self.forwarded = forwarded


@pytest_asyncio.fixture()
async def recent_route(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[_RecentRoute]:
    """Sessions routes with a fake runner client; no runtime/workflow needed.

    The public events route, the elicitation resolve route, and the peer
    routes (feature flag on) are all registered on one real router, so the
    tests exercise the actual ``post_event`` / ``resolve_elicitation``
    handlers rather than a hand-rolled seam.
    """
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = _seed_agent(db_uri)
    peer_store = SqlAlchemyPeerMessageStore(db_uri)

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        del request
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_sessions_router(
            store,
            SqlAlchemyAgentStore(db_uri),
            feature_flags=resolve_feature_flags({"OMNIGENT_FEATURES": "session_peer_messaging"}),
            peer_message_store=peer_store,
        ),
        prefix="/v1",
    )

    forwarded: list[httpx.Request] = []

    def _capture_forward(request: httpx.Request) -> httpx.Response:
        forwarded.append(request)
        return httpx.Response(204)

    runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_capture_forward), base_url="http://runner"
    )

    async def _get_runner_client(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
        return runner

    async def _relay_ready(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sessions_module, "_get_runner_client", _get_runner_client)
    monkeypatch.setattr(sessions_module, "_ensure_runner_relay_ready", _relay_ready)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _RecentRoute(client, store, agent_id, forwarded)
    await runner.aclose()


# ── Store: filter, ordering, cursors ────────────────────────────────────────


def test_store_orders_by_touched_value_and_excludes_untouched(db_uri: str) -> None:
    """Only sessions with the key are returned, newest touch first."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = _seed_agent(db_uri, "store-order-agent")
    key = touched_label_key(ALICE)
    first = store.create_conversation(agent_id=agent_id)
    newest = store.create_conversation(agent_id=agent_id)
    middle = store.create_conversation(agent_id=agent_id)
    untouched = store.create_conversation(agent_id=agent_id)
    other_user = store.create_conversation(agent_id=agent_id)
    store.set_labels(first.id, {key: "0000000000001"})
    store.set_labels(newest.id, {key: "0000000000003"})
    store.set_labels(middle.id, {key: "0000000000002"})
    store.set_labels(other_user.id, {touched_label_key(BOB): "0000000000009"})

    page = store.list_conversations(touched_label_key=key)

    assert [conv.id for conv in page.data] == [newest.id, middle.id, first.id]
    assert page.has_more is False
    assert untouched.id not in {conv.id for conv in page.data}

    capped = store.list_conversations(touched_label_key=key, limit=2)
    assert [conv.id for conv in capped.data] == [newest.id, middle.id]
    assert capped.has_more is True


@pytest.mark.parametrize("cursor", ["after", "before"])
def test_store_touched_key_rejects_cursors(db_uri: str, cursor: str) -> None:
    """A label-value ordering has no cursor position; combining raises."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = _seed_agent(db_uri, "store-cursor-agent")
    conv = store.create_conversation(agent_id=agent_id)

    with pytest.raises(ValueError):
        store.list_conversations(
            touched_label_key=touched_label_key(ALICE),
            **{cursor: conv.id},
        )


def test_set_labels_does_not_bump_conversation_updated_at(db_uri: str) -> None:
    """The touch is label-row bookkeeping; the session's updated_at stays."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = _seed_agent(db_uri, "store-stamp-agent")
    conv = store.create_conversation(agent_id=agent_id)

    store.set_labels(conv.id, {touched_label_key(ALICE): "0000000000001"})

    reloaded = store.get_conversation(conv.id)
    assert reloaded is not None
    assert reloaded.updated_at == conv.updated_at


def test_fork_drops_touched_and_pinned_labels(db_uri: str) -> None:
    """A fork is a new session: no per-user pin or touch keys are inherited."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = _seed_agent(db_uri, "store-fork-agent")
    source = store.create_conversation(agent_id=agent_id)
    store.set_labels(
        source.id,
        {
            touched_label_key(ALICE): "0000000000001",
            pinned_label_key(ALICE): "0000000000001",
            "kept": "yes",
        },
    )

    fork = store.fork_conversation(source.id)

    assert not any(key.startswith(f"{TOUCHED_LABEL_KEY}.") for key in fork.labels)
    assert not any(key.startswith("omnigent.pinned.") for key in fork.labels)
    assert fork.labels["kept"] == "yes"


# ── Label projection ────────────────────────────────────────────────────────


def test_labels_for_viewer_strips_touched_keys() -> None:
    """No touched key — bare or suffixed — ever reaches a response."""
    viewer = _labels_for_viewer(
        {
            touched_label_key(ALICE): "0000000000001",
            TOUCHED_LABEL_KEY: "forged",
            "kept": "yes",
        },
        ALICE,
    )

    assert viewer == {"kept": "yes"}


# ── Route: bounds, ordering, archived, isolation ────────────────────────────


@pytest.mark.parametrize("limit", [0, 21])
async def test_recent_route_rejects_out_of_range_limit(
    client: httpx.AsyncClient, limit: int
) -> None:
    """limit is bounded 1..20; out-of-range values fail validation."""
    resp = await client.get(f"/v1/me/recent-sessions?limit={limit}")
    assert resp.status_code == 422


@pytest.mark.parametrize("cursor", ["after", "before"])
async def test_recent_route_rejects_cursors(client: httpx.AsyncClient, cursor: str) -> None:
    """The touched-label order has no cursor position; cursors are rejected."""
    resp = await client.get(f"/v1/me/recent-sessions?{cursor}=conv_whatever")
    assert resp.status_code == 422, resp.text


async def test_recent_route_lists_own_touches_newest_first(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """The route returns accessible, non-archived touched sessions in order."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = _seed_agent(db_uri, "route-list-agent")
    key = touched_label_key(None)
    older = store.create_conversation(agent_id=agent_id, title="older")
    newer = store.create_conversation(agent_id=agent_id, title="newer")
    archived = store.create_conversation(agent_id=agent_id, title="archived")
    untouched = store.create_conversation(agent_id=agent_id, title="untouched")
    store.set_labels(older.id, {key: "0000000000001"})
    store.set_labels(newer.id, {key: "0000000000002"})
    store.set_labels(archived.id, {key: "0000000000003"})
    store.update_conversation(archived.id, archived=True)

    resp = await client.get("/v1/me/recent-sessions?limit=20")

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert [row["id"] for row in body["data"]] == [newer.id, older.id]
    assert untouched.id not in {row["id"] for row in body["data"]}


async def test_recent_rows_match_the_session_list_rows(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """A recent row is built exactly like the same session's list row."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = _seed_agent(db_uri, "route-parity-agent")
    conv = store.create_conversation(agent_id=agent_id, title="parity")
    store.set_labels(conv.id, {touched_label_key(None): "0000000000001"})

    recent = await client.get("/v1/me/recent-sessions?limit=5")
    listed = await client.get("/v1/sessions?limit=50")

    assert recent.status_code == 200 and listed.status_code == 200
    recent_row = next(row for row in recent.json()["data"] if row["id"] == conv.id)
    list_row = next(row for row in listed.json()["data"] if row["id"] == conv.id)
    assert recent_row == list_row


async def test_recent_reconciles_orphaned_running_session_like_the_list(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    """Recent settles an orphaned running row instead of copying the list's tail."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = _seed_agent(db_uri, "route-orphan-agent")
    conv = store.create_conversation(agent_id=agent_id, title="orphan")
    assert store.set_runner_id(conv.id, f"runner_{conv.id}")
    store.set_session_live_status(conv.id, "running")
    store.set_labels(conv.id, {touched_label_key(None): "0000000000001"})
    # The suspect gate needs a status-cache miss: the "running" value came
    # from the DB mirror, not a runner this replica is relaying.
    _session_status_cache.pop(conv.id, None)

    # Recent first so it does the reconciling itself, not a read of a status
    # the list route has already settled.
    recent = await client.get("/v1/me/recent-sessions?limit=5")
    listed = await client.get("/v1/sessions?limit=50")

    assert recent.status_code == 200 and listed.status_code == 200
    recent_row = next(row for row in recent.json()["data"] if row["id"] == conv.id)
    list_row = next(row for row in listed.json()["data"] if row["id"] == conv.id)
    assert recent_row == list_row
    assert recent_row["status"] == "idle"


# ── Writes: human-origin only ───────────────────────────────────────────────


async def test_user_message_touches_root_not_child(recent_route: _RecentRoute) -> None:
    """A message to a child stamps the ROOT; an agent item afterwards stamps nothing."""
    route = recent_route
    root = route.store.create_conversation(agent_id=route.agent_id, title="root")
    child = route.store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id, title="child"
    )
    key = touched_label_key(None)

    resp = await route.client.post(
        f"/v1/sessions/{child.id}/events", json=_user_message("work on it")
    )
    assert resp.status_code == 202, resp.text

    root_after = route.store.get_conversation(root.id)
    child_after = route.store.get_conversation(child.id)
    assert root_after is not None and child_after is not None
    stamped = root_after.labels[key]
    assert stamped.isdigit() and len(stamped) == 13
    assert key not in child_after.labels

    # An agent item (external assistant message) is not an interaction: the
    # stamp must not move.
    route.store.set_labels(root.id, {key: "0000000000001"})
    resp = await route.client.post(
        f"/v1/sessions/{root.id}/events",
        json={
            "type": "external_assistant_message",
            "data": {"agent": "test-agent", "text": "done"},
        },
    )
    assert resp.status_code == 202, resp.text
    assert route.store.get_conversation(root.id).labels[key] == "0000000000001"


@pytest.mark.parametrize("hops", [1, 5])
async def test_side_chat_message_touches_source(recent_route: _RecentRoute, hops: int) -> None:
    """Side-chat messages put the source session in the caller's Recent list."""
    route = recent_route
    source = route.store.create_conversation(agent_id=route.agent_id, title="source")
    current = source
    side_chat_ids = []
    for _ in range(hops):
        side_chat = route.store.create_conversation(title="side chat")
        route.store.set_labels(
            side_chat.id,
            {SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: current.id},
        )
        side_chat_ids.append(side_chat.id)
        current = side_chat

    resp = await route.client.post(
        f"/v1/sessions/{current.id}/events", json=_user_message("work on it")
    )
    assert resp.status_code == 202, resp.text

    recent = await route.client.get("/v1/me/recent-sessions?limit=1")
    assert recent.status_code == 200, recent.text
    assert [row["id"] for row in recent.json()["data"]] == [source.id]
    key = touched_label_key(None)
    source_after = route.store.get_conversation(source.id)
    assert source_after is not None
    assert key in source_after.labels
    for side_chat_id in side_chat_ids:
        side_chat_after = route.store.get_conversation(side_chat_id)
        assert side_chat_after is not None
        assert key not in side_chat_after.labels


@pytest.mark.parametrize("source_kind", ["missing_label", "missing_session", "cycle", "too_deep"])
async def test_side_chat_without_reachable_source_does_not_touch(
    recent_route: _RecentRoute, source_kind: str
) -> None:
    """An unresolved side-chat source never falls back to stamping the side chat."""
    route = recent_route
    current = route.store.create_conversation()
    sessions = [current]
    if source_kind == "too_deep":
        for _ in range(6):
            side_chat = route.store.create_conversation()
            route.store.set_labels(
                side_chat.id,
                {SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: current.id},
            )
            sessions.append(side_chat)
            current = side_chat
    else:
        labels = {SIDE_CHAT_LABEL_KEY: "1"}
        if source_kind == "missing_session":
            labels[SIDE_CHAT_SOURCE_LABEL_KEY] = "conv_missing"
        elif source_kind == "cycle":
            labels[SIDE_CHAT_SOURCE_LABEL_KEY] = current.id
        route.store.set_labels(current.id, labels)

    resp = await route.client.post(
        f"/v1/sessions/{current.id}/events", json=_user_message("work on it")
    )
    assert resp.status_code == 202, resp.text
    key = touched_label_key(None)
    for session in sessions:
        session_after = route.store.get_conversation(session.id)
        assert session_after is not None
        assert key not in session_after.labels
    recent = await route.client.get("/v1/me/recent-sessions?limit=1")
    assert recent.status_code == 200, recent.text
    assert recent.json()["data"] == []


async def test_approval_event_touches(recent_route: _RecentRoute) -> None:
    """A successful in-band approval is an interaction."""
    route = recent_route
    conv = route.store.create_conversation(agent_id=route.agent_id)

    resp = await route.client.post(
        f"/v1/sessions/{conv.id}/events",
        json={"type": "approval", "data": {"elicitation_id": "elicit_test", "action": "accept"}},
    )

    assert resp.status_code == 202, resp.text
    assert touched_label_key(None) in route.store.get_conversation(conv.id).labels


async def test_batch_failure_keeps_an_earlier_touch(recent_route: _RecentRoute) -> None:
    """A failing later batch entry must not lose the earlier message's touch."""
    route = recent_route
    # No agent binding: the fixture has no runtime, so an agent-bound user
    # message would fail input-policy evaluation and read as denied.
    root = route.store.create_conversation(title="batch-root")
    key = touched_label_key(None)

    resp = await route.client.post(
        f"/v1/sessions/{root.id}/events",
        json=[
            _user_message("first"),
            {
                "type": "message",
                "created_by": "intruder@example.com",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "bad"}]},
            },
        ],
    )

    assert resp.status_code == 403, resp.text
    root_after = route.store.get_conversation(root.id)
    assert root_after is not None
    assert key in root_after.labels


async def test_resolve_elicitation_route_touches(recent_route: _RecentRoute) -> None:
    """Answering via the URL resolve endpoint is an interaction too."""
    route = recent_route
    conv = route.store.create_conversation(agent_id=route.agent_id)

    resp = await route.client.post(
        f"/v1/sessions/{conv.id}/elicitations/elicit_test/resolve",
        json={"action": "accept"},
    )

    assert resp.status_code == 202, resp.text
    assert touched_label_key(None) in route.store.get_conversation(conv.id).labels


async def test_denied_message_does_not_touch(recent_route: _RecentRoute) -> None:
    """A policy-denied user message is not an interaction."""
    route = recent_route
    conv = route.store.create_conversation(agent_id=route.agent_id)
    spec = AgentSpec(spec_version=1, name="recent-agent", guardrails=GuardrailsSpec(policies=[]))

    async def _deny(_ctx: Any) -> Any:
        from omnigent.policies.types import PolicyAction, PolicyResult

        return PolicyResult(action=PolicyAction.DENY, reason="no")

    with (
        patch(_POLICY_CACHE_PATCH) as mock_cache,
        patch(_POLICY_ENGINE_PATCH) as mock_build,
    ):
        mock_cache.return_value.load.return_value.spec = spec
        mock_engine = mock_build.return_value
        mock_engine.evaluate = _deny
        mock_engine.apply_label_writes = lambda _writes: None
        resp = await route.client.post(
            f"/v1/sessions/{conv.id}/events", json=_user_message("deny me")
        )

    assert resp.status_code == 202, resp.text
    assert resp.json().get("denied") is True
    assert touched_label_key(None) not in route.store.get_conversation(conv.id).labels


async def test_runner_authority_post_does_not_touch(recent_route: _RecentRoute) -> None:
    """A runner-tunnel-bound POST is runner origin, even with a user message."""
    route = recent_route
    token = "runner-token-secret"
    conv = route.store.create_conversation(
        agent_id=route.agent_id, runner_id=token_bound_runner_id(token)
    )

    resp = await route.client.post(
        f"/v1/sessions/{conv.id}/events",
        headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
        json=_user_message("runner originated"),
    )

    assert resp.status_code == 202, resp.text
    assert touched_label_key(None) not in route.store.get_conversation(conv.id).labels


async def test_interrupt_does_not_touch(recent_route: _RecentRoute) -> None:
    """Cancelling a turn is not an interaction."""
    route = recent_route
    conv = route.store.create_conversation(agent_id=route.agent_id)

    resp = await route.client.post(f"/v1/sessions/{conv.id}/events", json={"type": "interrupt"})

    assert resp.status_code == 202, resp.text
    assert touched_label_key(None) not in route.store.get_conversation(conv.id).labels


async def test_peer_send_does_not_touch(recent_route: _RecentRoute) -> None:
    """A peer delivery builds a user message but reaches _post_event_impl."""
    route = recent_route
    token = "sender-token-secret"
    sender = route.store.create_conversation(
        agent_id=route.agent_id, runner_id=token_bound_runner_id(token)
    )
    receiver = route.store.create_conversation(agent_id=route.agent_id)

    resp = await route.client.post(
        f"/v1/sessions/{receiver.id}/peer-messages",
        headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
        json={"sender_session_id": sender.id, "text": "peer hello"},
    )

    assert resp.status_code == 200, resp.text
    assert touched_label_key(None) not in route.store.get_conversation(receiver.id).labels
    assert touched_label_key(None) not in route.store.get_conversation(sender.id).labels


# ── Write authority: client-supplied touched keys ───────────────────────────


async def test_client_cannot_seed_touched_labels(client: httpx.AsyncClient, db_uri: str) -> None:
    """Create and PATCH refuse every client-supplied touched key."""
    agent_id = _seed_agent(db_uri, "guard-agent")
    store = SqlAlchemyConversationStore(db_uri)

    for key in (
        TOUCHED_LABEL_KEY,
        f"{TOUCHED_LABEL_KEY}.{BOB}",
        f"OMNIGENT.Touched.{BOB}",
        f"omni\ufeffgent.touched.{BOB}",
        f"omnigent.t\u00f8uched.{BOB}",
    ):
        created = await client.post(
            "/v1/sessions", json={"agent_id": agent_id, "labels": {key: "0000000000001"}}
        )
        assert created.status_code == 400, created.text
        assert created.json()["error"]["code"] == "invalid_input"

    conv = store.create_conversation(agent_id=agent_id)
    patched = await client.patch(
        f"/v1/sessions/{conv.id}",
        json={"labels": {f"{TOUCHED_LABEL_KEY}.{BOB}": "0000000000001"}},
    )
    assert patched.status_code == 400, patched.text
    reloaded = store.get_conversation(conv.id)
    assert reloaded is not None
    assert not any(is_touched_label_key(key) for key in reloaded.labels)


# ── Per-user isolation and response hygiene ─────────────────────────────────


def _build_multi_user_app(
    db_uri: str,
    tmp_path: Path,
    permission_store: SqlAlchemyPermissionStore,
) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=permission_store,
        auth_provider=UnifiedAuthProvider(source="header"),
    )


class _MultiUser:
    def __init__(
        self,
        client: httpx.AsyncClient,
        shared_id: str,
        alice_ids: list[str],
        bob_ids: list[str],
    ) -> None:
        self.client = client
        self.shared_id = shared_id
        self.alice_ids = alice_ids
        self.bob_ids = bob_ids


@pytest_asyncio.fixture()
async def multi_user(db_uri: str, tmp_path: Path) -> AsyncIterator[_MultiUser]:
    """Two users with their own sessions plus one shared, each touched per-user."""
    store = SqlAlchemyConversationStore(db_uri)
    agent_id = _seed_agent(db_uri, "multi-agent")
    permission_store = SqlAlchemyPermissionStore(db_uri)
    for user in (ALICE, BOB):
        permission_store.ensure_user(user)
    alice_old = store.create_conversation(agent_id=agent_id, title="alice-old")
    alice_new = store.create_conversation(agent_id=agent_id, title="alice-new")
    shared = store.create_conversation(agent_id=agent_id, title="shared")
    bob_new = store.create_conversation(agent_id=agent_id, title="bob-new")
    for user, conv in (
        (ALICE, alice_old),
        (ALICE, alice_new),
        (ALICE, shared),
        (BOB, shared),
        (BOB, bob_new),
    ):
        permission_store.grant(user, conv.id, LEVEL_OWNER)
    store.set_labels(alice_old.id, {touched_label_key(ALICE): "0000000000001"})
    store.set_labels(alice_new.id, {touched_label_key(ALICE): "0000000000003"})
    store.set_labels(
        shared.id,
        {
            touched_label_key(ALICE): "0000000000002",
            touched_label_key(BOB): "0000000000001",
        },
    )
    store.set_labels(bob_new.id, {touched_label_key(BOB): "0000000000003"})

    app = _build_multi_user_app(db_uri, tmp_path, permission_store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield _MultiUser(
            client,
            shared.id,
            [alice_new.id, shared.id, alice_old.id],
            [bob_new.id, shared.id],
        )


async def test_recent_is_per_user_ordered(multi_user: _MultiUser) -> None:
    """Each caller's list follows their own touches only."""
    alice = await multi_user.client.get(
        "/v1/me/recent-sessions?limit=20", headers={"X-Forwarded-Email": ALICE}
    )
    assert alice.status_code == 200, alice.text
    assert [row["id"] for row in alice.json()["data"]] == multi_user.alice_ids

    bob = await multi_user.client.get(
        "/v1/me/recent-sessions?limit=20", headers={"X-Forwarded-Email": BOB}
    )
    assert bob.status_code == 200, bob.text
    assert [row["id"] for row in bob.json()["data"]] == multi_user.bob_ids


async def test_session_response_carries_no_touched_keys(multi_user: _MultiUser) -> None:
    """A session snapshot never exposes any user's touch times."""
    resp = await multi_user.client.get(
        f"/v1/sessions/{multi_user.shared_id}", headers={"X-Forwarded-Email": ALICE}
    )
    assert resp.status_code == 200, resp.text
    labels = resp.json()["labels"]
    assert not any(
        key == TOUCHED_LABEL_KEY or key.startswith(f"{TOUCHED_LABEL_KEY}.") for key in labels
    )
