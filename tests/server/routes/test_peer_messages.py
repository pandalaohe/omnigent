"""Route tests for session peer messaging (SCC01-S1 T2).

Builds the full app with the ``session_peer_messaging`` flag on (or off),
two runner-bound sessions, and a fake ``post_event_impl`` that records
inline-delivery calls and returns scripted shapes. Covers every
disposition row, the four order-sensitive precedences, closed detection,
native failure mapping, ``wait_seconds``, guard concurrency, thread cap,
reply detection, and the GET / action routes.
"""

from __future__ import annotations

import asyncio
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from omnigent.db.utils import generate_agent_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes.sessions import routes_peer as peer_module
from omnigent.server.routes.sessions.routes_peer import (
    PEER_HOLD_LIFETIME,
    PEER_PAIR_LIMIT,
    PEER_QUEUE_LIFETIME,
    PEER_THREAD_LIMIT,
    PeerSendRequest,
    format_peer_envelope,
    register_peer_routes,
)
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

AGENT_ID = generate_agent_id()
ALICE = "alice@example.com"
BOB = "bob@example.com"


def _build_peer_app(
    *,
    conversation_store: SqlAlchemyConversationStore,
    permission_store: SqlAlchemyPermissionStore | None,
    agent_store: SqlAlchemyAgentStore | None,
    peer_message_store: SqlAlchemyPeerMessageStore,
    post_event_impl: Any,
    liveness_lookup: Any,
    feature_flags: Any,
    runner_tunnel_tokens: frozenset[str] | None = None,
) -> FastAPI:
    """A minimal app hosting only the peer routes, wired via ``register_peer_routes``.

    F1 removed the module-level test seams (``_POST_EVENT_IMPL_OVERRIDE`` /
    ``_LIVENESS_OVERRIDE``); fakes are injected through
    ``register_peer_routes``'s own parameters instead, on a small app built
    directly rather than through ``create_app`` (which hardwires the real
    liveness/delivery wiring that these tests need to fake).
    """
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return await request_validation_exception_handler(request, exc)

    router = APIRouter()
    register_peer_routes(
        router,
        post_event_impl=post_event_impl,
        conversation_store=conversation_store,
        permission_store=permission_store,
        auth_provider=UnifiedAuthProvider(source="header"),
        liveness_lookup=liveness_lookup,
        runner_tunnel_tokens=runner_tunnel_tokens,
        feature_flags=feature_flags,
        peer_message_store=peer_message_store,
        runner_router=None,
        agent_store=agent_store,
        app_state=app.state,
    )
    app.include_router(router, prefix="/v1")
    return app


def _headers(user: str | None = None, token: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if user is not None:
        headers["X-Forwarded-Email"] = user
    if token is not None:
        headers[RUNNER_TUNNEL_TOKEN_HEADER] = token
    return headers


def _runner_pair() -> tuple[str, str]:
    token = secrets.token_hex(16)
    return token, token_bound_runner_id(token)


class _FakePostEvent:
    """Recording ``post_event_impl`` with a scripted outcome."""

    def __init__(self, outcome: dict[str, Any] | None = None) -> None:
        self.outcome: dict[str, Any] = outcome or {"queued": True, "item_id": "item_1"}
        self.calls: list[dict[str, Any]] = []
        self.error: BaseException | None = None

    async def __call__(self, request: Any, session_id: str, body: Any, **_kwargs: Any) -> Any:
        self.calls.append({"session_id": session_id, "type": body.type, "data": body.data})
        if self.error is not None:
            raise self.error
        return dict(self.outcome)


@pytest.fixture()
def peer_env(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """App with flag on, two owned sessions, fake inline delivery, test client pieces."""
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_store.create(agent_id=AGENT_ID, name="test-agent", bundle_location="test:///bundle")
    conv_store = SqlAlchemyConversationStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    peer_store = SqlAlchemyPeerMessageStore(db_uri)
    for user in (ALICE, BOB):
        perm_store.ensure_user(user)
    sender_token, sender_runner = _runner_pair()
    receiver_token, receiver_runner = _runner_pair()
    sender = conv_store.create_conversation(
        title="sender", agent_id=AGENT_ID, runner_id=sender_runner
    )
    receiver = conv_store.create_conversation(
        title="receiver", agent_id=AGENT_ID, runner_id=receiver_runner
    )
    perm_store.grant(ALICE, sender.id, LEVEL_OWNER)
    perm_store.grant(ALICE, receiver.id, LEVEL_OWNER)
    fake = _FakePostEvent()
    offline_ids: set[str] = set()
    host_online_ids: set[str] = set()
    peer_module._PEER_ADMISSION._pair_sends.clear()
    peer_module._PEER_ADMISSION._sender_sends.clear()
    peer_module._PEER_ADMISSION._pair_texts.clear()

    async def _fake_post_event_impl(
        request: Any, session_id: str, body: Any, **kwargs: Any
    ) -> Any:
        del kwargs
        return await fake(request, session_id, body)

    async def _fake_runner_client(
        session_id: str, runner_router: Any = None, **kwargs: Any
    ) -> None:
        del session_id, runner_router, kwargs
        return

    def _scripted_liveness(ids: list[str]) -> dict[str, Any]:
        from omnigent.server.routes.sessions import SessionLiveness

        return {
            sid: SessionLiveness(
                runner_online=sid not in offline_ids,
                host_online=True if sid in host_online_ids else None,
            )
            for sid in ids
        }

    monkeypatch.setattr(sessions_module, "_get_runner_client", _fake_runner_client)
    monkeypatch.setattr(peer_module, "_get_runner_client", _fake_runner_client)
    app = _build_peer_app(
        conversation_store=conv_store,
        permission_store=perm_store,
        agent_store=agent_store,
        peer_message_store=peer_store,
        post_event_impl=_fake_post_event_impl,
        liveness_lookup=_scripted_liveness,
        feature_flags=resolve_feature_flags({"OMNIGENT_FEATURES": "session_peer_messaging"}),
    )
    return {
        "app": app,
        "fake": fake,
        "offline_ids": offline_ids,
        "host_online_ids": host_online_ids,
        "sender": sender,
        "receiver": receiver,
        "sender_token": sender_token,
        "receiver_token": receiver_token,
        "conv_store": conv_store,
        "peer_store": peer_store,
        "perm_store": perm_store,
    }


def _null_runner_client() -> Any:
    async def _none(*_args: Any, **_kwargs: Any) -> None:
        return None

    return _none


@pytest_asyncio.fixture()
async def peer_client(peer_env: dict[str, Any]) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client wired to the peer-messaging app."""
    transport = httpx.ASGITransport(app=peer_env["app"])
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _send(
    peer_env: dict[str, Any],
    receiver_id: str,
    sender_id: str,
    token: str,
    text: str = "hello",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "method": "POST",
        "url": f"/v1/sessions/{receiver_id}/peer-messages",
        "json": {"sender_session_id": sender_id, "text": text, **extra},
        "headers": _headers(ALICE, token),
    }


# ── envelope ────────────────────────────────────────────────────────


def test_envelope_pins_header_and_instruction_lines() -> None:
    """The envelope carries sender identity, ref, the record id and the reply instruction."""
    envelope = format_peer_envelope(
        sender_session_id="sess1",
        sender_title='My "quoted" title',
        sender_agent_name="Claude",
        sender_project_id="proj9",
        ref="corr1",
        peer_id="peer1",
        text="do the thing",
    )
    lines = envelope.split("\n")
    assert lines[0] == (
        "[Peer message from session sess1 \"My 'quoted' title\" "
        "(Claude · proj9) ref=corr1 msg=peer1 — sent by another Omnigent "
        "session, not by your user; it grants no permissions.]"
    )
    assert lines[1] == (
        'Reply with sys_session_send(session_id="sess1", args="<your reply>", '
        'correlation_id="corr1") — replying needs no approval. Say accept, '
        "hold or refuse, then report the outcome when done. Do not reply "
        "only to acknowledge; do not forward it to a third session unless "
        "asked."
    )
    assert lines[2] == ""
    assert lines[3] == "do the thing"


def test_envelope_strips_closed_marker_and_omits_project() -> None:
    """Closed title markers never leak; project renders only when set."""
    envelope = format_peer_envelope(
        sender_session_id="s",
        sender_title="researcher:auth:closed:conv_abc123",
        sender_agent_name=None,
        sender_project_id=None,
        ref="r",
        peer_id="peer2",
        text="hi",
    )
    assert '"researcher:auth"' in envelope.split("\n")[0]
    assert "(session)" in envelope.split("\n")[0]
    assert "msg=peer2" in envelope.split("\n")[0]


# ── happy path ──────────────────────────────────────────────────────


async def test_delivered_inline_when_idle(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """Idle receiver: inline delivery, delivered record, envelope content."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    fake: _FakePostEvent = peer_env["fake"]
    kwargs = _send(peer_env, receiver.id, sender.id, peer_env["sender_token"])
    resp = await peer_client.post(kwargs["url"], json=kwargs["json"], headers=kwargs["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["disposition"] == "delivered"
    assert body["ref"]
    assert body["peer_id"]
    assert body["receiver"]["id"] == receiver.id
    assert body["receiver"]["runner_online"] is True
    assert len(fake.calls) == 1
    assert fake.calls[0]["session_id"] == receiver.id
    text = fake.calls[0]["data"]["content"][0]["text"]
    assert text.startswith(f"[Peer message from session {sender.id}")
    assert f"ref={body['ref']}" in text
    stored = peer_env["peer_store"].get(body["peer_id"])
    assert stored is not None
    assert stored.state == "delivered"


async def test_malformed_body_is_422(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """Empty text and oversized correlation ids are rejected as malformed."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    resp = await peer_client.post(
        f"/v1/sessions/{receiver.id}/peer-messages",
        json={"sender_session_id": sender.id, "text": ""},
        headers=_headers(ALICE, peer_env["sender_token"]),
    )
    assert resp.status_code == 422
    resp = await peer_client.post(
        f"/v1/sessions/{receiver.id}/peer-messages",
        json={"sender_session_id": sender.id, "text": "hi", "correlation_id": "x" * 65},
        headers=_headers(ALICE, peer_env["sender_token"]),
    )
    assert resp.status_code == 422


async def test_unknown_receiver_is_404(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """An unknown receiver id 404s."""
    sender = peer_env["sender"]
    resp = await peer_client.post(
        "/v1/sessions/0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f0f/peer-messages",
        json={"sender_session_id": sender.id, "text": "hi"},
        headers=_headers(ALICE, peer_env["sender_token"]),
    )
    assert resp.status_code == 404


async def test_token_bound_to_other_runner_is_401(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """A tunnel token for another runner is rejected as unauthenticated."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    resp = await peer_client.post(
        f"/v1/sessions/{receiver.id}/peer-messages",
        json={"sender_session_id": sender.id, "text": "hi"},
        headers=_headers(ALICE, peer_env["receiver_token"]),
    )
    assert resp.status_code == 401


async def test_unknown_sender_is_401(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """An unknown sender session id 401s (no session to bind the token to)."""
    receiver = peer_env["receiver"]
    token, _ = _runner_pair()
    resp = await peer_client.post(
        f"/v1/sessions/{receiver.id}/peer-messages",
        json={
            "sender_session_id": "1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e",
            "text": "hi",
        },
        headers=_headers(ALICE, token),
    )
    assert resp.status_code == 401


# ── order-sensitive precedences ─────────────────────────────────────


async def _flag_off_client(db_uri: str) -> httpx.AsyncClient:
    fake = _FakePostEvent()

    async def _fake_post_event_impl(
        request: Any, session_id: str, body: Any, **kwargs: Any
    ) -> Any:
        del kwargs
        return await fake(request, session_id, body)

    app = _build_peer_app(
        conversation_store=SqlAlchemyConversationStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        agent_store=SqlAlchemyAgentStore(db_uri),
        peer_message_store=SqlAlchemyPeerMessageStore(db_uri),
        post_event_impl=_fake_post_event_impl,
        liveness_lookup=lambda ids: {},
        feature_flags=resolve_feature_flags({}),
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_flag_off_before_auth(peer_env: dict[str, Any], db_uri: str) -> None:
    """Flag off answers refused(feature_disabled) even with a bad token."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    async with await _flag_off_client(db_uri) as c:
        resp = await c.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": "hi"},
            headers={"X-Forwarded-Email": ALICE},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["disposition"] == "refused"
    assert resp.json()["reason"] == "feature_disabled"


async def test_not_same_owner_before_is_subagent(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """Ownership refusal wins over the sub-agent check."""
    sender = peer_env["sender"]
    conv_store: SqlAlchemyConversationStore = peer_env["conv_store"]
    perm_store: SqlAlchemyPermissionStore = peer_env["perm_store"]
    parent = conv_store.create_conversation(
        title="other-parent", agent_id=AGENT_ID, runner_id="rx"
    )
    perm_store.grant(BOB, parent.id, LEVEL_OWNER)
    child = conv_store.create_conversation(
        title="child", agent_id=AGENT_ID, parent_conversation_id=parent.id
    )
    resp = await peer_client.post(
        f"/v1/sessions/{child.id}/peer-messages",
        json={"sender_session_id": sender.id, "text": "hi"},
        headers=_headers(ALICE, peer_env["sender_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reason"] == "not_same_owner"
    # F7: ownership isn't established yet, so the response carries no
    # receiver summary (title/agent/status would leak to an unrelated sender).
    assert "receiver" not in resp.json()


async def test_is_subagent_same_owner(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """A same-owner sub-agent receiver is refused(is_subagent)."""
    sender = peer_env["sender"]
    conv_store: SqlAlchemyConversationStore = peer_env["conv_store"]
    parent = conv_store.create_conversation(title="p2", agent_id=AGENT_ID)
    child = conv_store.create_conversation(
        title="child2", agent_id=AGENT_ID, parent_conversation_id=parent.id
    )
    peer_env["perm_store"].grant(ALICE, parent.id, LEVEL_OWNER)
    resp = await peer_client.post(
        f"/v1/sessions/{child.id}/peer-messages",
        json={"sender_session_id": sender.id, "text": "hi"},
        headers=_headers(ALICE, peer_env["sender_token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["reason"] == "is_subagent"


async def test_duplicate_before_closed(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """The duplicate guard fires before the closed check."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    token = peer_env["sender_token"]
    first = await peer_client.post(
        f"/v1/sessions/{receiver.id}/peer-messages",
        json={"sender_session_id": sender.id, "text": "same text here"},
        headers=_headers(ALICE, token),
    )
    assert first.json()["disposition"] == "delivered", first.text
    peer_env["conv_store"].set_labels(receiver.id, {"omnigent.closed": "true"})
    try:
        admitted_text = f"close-then-{uuid.uuid4().hex}"
        admitted = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": admitted_text},
            headers=_headers(ALICE, token),
        )
        assert admitted.json()["disposition"] == "failed", admitted.text
        assert admitted.json()["reason"] == "closed"
        second = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": "same text here"},
            headers=_headers(ALICE, token),
        )
        assert second.status_code == 200, second.text
        assert second.json()["disposition"] == "dropped"
        assert second.json()["reason"] == "duplicate"
    finally:
        peer_env["conv_store"].set_labels(receiver.id, {"omnigent.closed": "false"})


async def test_hold_before_offline(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """A hold-policy receiver stores held even when its runner is offline."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "hold"})
    try:
        resp = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"hold-me-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["disposition"] == "held"
        stored = peer_env["peer_store"].get(resp.json()["peer_id"])
        assert stored is not None
        assert stored.state == "held"
        assert stored.expires_at - stored.created_at == PEER_HOLD_LIFETIME
    finally:
        peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "accept"})


async def test_closed_before_hold(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """F5: closed wins over the hold policy — never stores a held record."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    peer_env["conv_store"].set_labels(
        receiver.id, {"peer_inbound": "hold", "omnigent.closed": "true"}
    )
    try:
        resp = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"closed-hold-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["disposition"] == "failed"
        assert resp.json()["reason"] == "closed"
        assert peer_env["fake"].calls == []
    finally:
        peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "accept"})


# ── dispositions ────────────────────────────────────────────────────


async def test_receiver_refuses_policy(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """peer_inbound=refuse answers refused(receiver_refuses), nothing stored."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    fake: _FakePostEvent = peer_env["fake"]
    peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "refuse"})
    try:
        resp = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"n-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        assert resp.json()["disposition"] == "refused"
        assert resp.json()["reason"] == "receiver_refuses"
        assert resp.json()["peer_id"] is None
        assert fake.calls == []
    finally:
        peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "accept"})


@pytest.mark.parametrize(
    "close",
    [
        {"labels": {"omnigent.closed": "true"}},
        {"title": "work:closed:conv_deadbeef"},
        {"archived": True},
    ],
    ids=["label", "title-marker", "archived_at"],
)
async def test_closed_variants_fail(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any], close: dict[str, Any]
) -> None:
    """Closed by label, title marker, or archived_at all fail(closed)."""
    sender = peer_env["sender"]
    conv_store: SqlAlchemyConversationStore = peer_env["conv_store"]
    conv = conv_store.create_conversation(
        title=close.get("title", f"closed-{uuid.uuid4().hex[:6]}"),
        agent_id=AGENT_ID,
        runner_id="rc",
    )
    peer_env["perm_store"].grant(ALICE, conv.id, LEVEL_OWNER)
    if "labels" in close:
        conv_store.set_labels(conv.id, close["labels"])
    if close.get("archived"):
        conv_store.update_conversation(conv.id, archived=True)
    try:
        resp = await peer_client.post(
            f"/v1/sessions/{conv.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"c-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["disposition"] == "failed"
        assert resp.json()["reason"] == "closed"
        assert peer_env["fake"].calls == []
    finally:
        if "labels" in close:
            conv_store.set_labels(conv.id, {"omnigent.closed": "false"})
        if close.get("archived"):
            conv_store.update_conversation(conv.id, archived=False)


async def test_runner_unavailable_maps_offline(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """OmnigentError(RUNNER_UNAVAILABLE) from delivery maps to failed(offline)."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    fake: _FakePostEvent = peer_env["fake"]
    fake.error = OmnigentError("gone", code=ErrorCode.RUNNER_UNAVAILABLE)
    try:
        resp = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"e-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        assert resp.json()["disposition"] == "failed"
        assert resp.json()["reason"] == "offline"
        stored = peer_env["peer_store"].get(resp.json()["peer_id"])
        assert stored is not None and stored.state == "failed"
    finally:
        fake.error = None


async def test_other_omnigent_error_maps_not_ready(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """Any other delivery error maps to failed(not_ready)."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    fake: _FakePostEvent = peer_env["fake"]
    fake.error = OmnigentError("boom", code=ErrorCode.INTERNAL_ERROR)
    try:
        resp = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"e2-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        assert resp.json()["disposition"] == "failed"
        assert resp.json()["reason"] == "not_ready"
    finally:
        fake.error = None


async def test_native_item_id_without_pending_id_is_not_ready(
    peer_client: httpx.AsyncClient,
    peer_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native persisted-failure shape maps to failed(not_ready)."""
    sender = peer_env["sender"]
    conv_store: SqlAlchemyConversationStore = peer_env["conv_store"]
    native = conv_store.create_conversation(title="native-recv", agent_id=AGENT_ID, runner_id="rn")
    peer_env["perm_store"].grant(ALICE, native.id, LEVEL_OWNER)
    monkeypatch.setattr(peer_module, "_is_native_terminal_session", lambda _conv: True)
    peer_env["fake"].outcome = {"queued": True, "item_id": "item_fail"}
    try:
        resp = await peer_client.post(
            f"/v1/sessions/{native.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"n-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        assert resp.json()["disposition"] == "failed", resp.text
        assert resp.json()["reason"] == "not_ready"
    finally:
        peer_env["fake"].outcome = {"queued": True, "item_id": "item_1"}


@pytest.mark.parametrize(
    "harness",
    ["sdk", "native"],
    ids=["sdk-receiver", "native-receiver"],
)
async def test_wait_seconds_offline_receiver(
    peer_client: httpx.AsyncClient,
    peer_env: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    """Offline receiver: wait 0 fails, wait 30 stores pending."""
    sender = peer_env["sender"]
    conv_store: SqlAlchemyConversationStore = peer_env["conv_store"]
    target = conv_store.create_conversation(
        title=f"off-{harness}", agent_id=AGENT_ID, runner_id="roff"
    )
    peer_env["perm_store"].grant(ALICE, target.id, LEVEL_OWNER)
    peer_env["offline_ids"].add(target.id)
    if harness == "native":
        monkeypatch.setattr(peer_module, "_is_native_terminal_session", lambda _conv: True)
    else:
        monkeypatch.setattr(peer_module, "_is_native_terminal_session", lambda _conv: False)
    monkeypatch.setattr(
        peer_module,
        "_get_runner_client",
        _null_runner_client(),
    )
    text0 = f"w0-{uuid.uuid4().hex}"
    resp = await peer_client.post(
        f"/v1/sessions/{target.id}/peer-messages",
        json={"sender_session_id": sender.id, "text": text0},
        headers=_headers(ALICE, peer_env["sender_token"]),
    )
    assert resp.json()["disposition"] == "failed", resp.text
    assert resp.json()["reason"] == "offline"
    text30 = f"w30-{uuid.uuid4().hex}"
    resp = await peer_client.post(
        f"/v1/sessions/{target.id}/peer-messages",
        json={"sender_session_id": sender.id, "text": text30, "wait_seconds": 30},
        headers=_headers(ALICE, peer_env["sender_token"]),
    )
    assert resp.json()["disposition"] == "pending", resp.text
    assert resp.json()["reason"] == "offline"
    stored = peer_env["peer_store"].get(resp.json()["peer_id"])
    assert stored is not None and stored.state == "pending"
    assert stored.expires_at - stored.created_at == 30


async def test_busy_receiver_queues_without_delivery(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """A mid-turn receiver gets a queued record and no inline delivery."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    fake: _FakePostEvent = peer_env["fake"]
    sessions_module._session_status_cache[receiver.id] = "running"
    try:
        resp = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"b-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        assert resp.json()["disposition"] == "queued", resp.text
        assert fake.calls == []
        stored = peer_env["peer_store"].get(resp.json()["peer_id"])
        assert stored is not None and stored.state == "queued"
        assert stored.expires_at - stored.created_at == PEER_QUEUE_LIFETIME
    finally:
        sessions_module._session_status_cache.pop(receiver.id, None)


async def test_relaunchable_receiver_delivers_inline(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """R3-B: a dead runner on a live host is relaunchable, not offline.

    ``runner_online=False`` with ``host_online=True`` skips the native
    readiness probe and, idle, delivers inline — same as an online receiver.
    """
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    fake: _FakePostEvent = peer_env["fake"]
    peer_env["offline_ids"].add(receiver.id)
    peer_env["host_online_ids"].add(receiver.id)
    try:
        resp = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"relaunch-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        assert resp.json()["disposition"] == "delivered", resp.text
        assert len(fake.calls) == 1
    finally:
        peer_env["offline_ids"].discard(receiver.id)
        peer_env["host_online_ids"].discard(receiver.id)


# ── guards ──────────────────────────────────────────────────────────


async def test_concurrent_identical_sends_admit_once(
    peer_env: dict[str, Any],
) -> None:
    """Two identical concurrent sends: one admitted, one dropped(duplicate).

    The sends run as bare coroutines on one loop (not ASGI requests, which
    ``asyncio.to_thread`` store calls serialize per thread), so admission
    under the sender lock is truly concurrent.
    """
    from fastapi import Request

    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    text = f"dup-{uuid.uuid4().hex}"
    scope_base: dict[str, Any] = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 50000),
        "headers": [
            (b"x-forwarded-email", ALICE.encode()),
            (
                RUNNER_TUNNEL_TOKEN_HEADER.lower().encode(),
                peer_env["sender_token"].encode(),
            ),
        ],
    }

    async def _one() -> dict[str, Any]:
        scope = dict(scope_base, path=f"/sessions/{receiver.id}/peer-messages")
        scope["app"] = peer_env["app"]
        request = Request(scope)
        routes = [
            r
            for r in peer_env["app"].routes
            if getattr(r, "path", "") == "/v1/sessions/{receiver_id}/peer-messages"
        ]
        assert routes, "peer send route not mounted"
        body = PeerSendRequest(sender_session_id=sender.id, text=text)
        return await routes[0].endpoint(request, receiver.id, body)

    first, second = await asyncio.gather(_one(), _one())
    dispositions = sorted([first["disposition"], second["disposition"]])
    assert dispositions == ["delivered", "dropped"], [first, second]
    dropped = first if first["disposition"] == "dropped" else second
    assert dropped["reason"] == "duplicate"


async def test_seven_distinct_concurrent_sends_admit_six(
    peer_env: dict[str, Any],
) -> None:
    """Seven distinct concurrent sends within 60 s admit exactly six."""
    from fastapi import Request

    sender = peer_env["sender"]
    conv_store: SqlAlchemyConversationStore = peer_env["conv_store"]
    target = conv_store.create_conversation(
        title=f"burst-{uuid.uuid4().hex[:6]}", agent_id=AGENT_ID, runner_id="rb"
    )
    peer_env["perm_store"].grant(ALICE, target.id, LEVEL_OWNER)
    routes = [
        r
        for r in peer_env["app"].routes
        if getattr(r, "path", "") == "/v1/sessions/{receiver_id}/peer-messages"
    ]
    assert routes, "peer send route not mounted"

    async def _one(i: int) -> dict[str, Any]:
        scope: dict[str, Any] = {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "server": ("test", 80),
            "client": ("test", 50000),
            "path": f"/sessions/{target.id}/peer-messages",
            "headers": [
                (b"x-forwarded-email", ALICE.encode()),
                (
                    RUNNER_TUNNEL_TOKEN_HEADER.lower().encode(),
                    peer_env["sender_token"].encode(),
                ),
            ],
        }
        scope["app"] = peer_env["app"]
        request = Request(scope)
        body = PeerSendRequest(sender_session_id=sender.id, text=f"burst-{i}-{uuid.uuid4().hex}")
        return await routes[0].endpoint(request, target.id, body)

    results = await asyncio.gather(*[_one(i) for i in range(PEER_PAIR_LIMIT + 1)])
    admitted = [r for r in results if r["disposition"] != "refused"]
    refused = [r for r in results if r["disposition"] == "refused"]
    assert len(admitted) == PEER_PAIR_LIMIT, results
    assert len(refused) == 1
    assert refused[0]["reason"] == "burst"


async def test_thread_cap(peer_client: httpx.AsyncClient, peer_env: dict[str, Any]) -> None:
    """The 21st record on one correlation id is refused(thread_limit).

    Uses an offline receiver (pending records keep their reservations, so
    only the duplicate text hash is consumed per send) with a fresh sender
    per attempt, staying under both the pair and sender budgets.
    """
    receiver = peer_env["receiver"]
    conv_store: SqlAlchemyConversationStore = peer_env["conv_store"]
    peer_env["offline_ids"].add(receiver.id)
    ref = f"thread-{uuid.uuid4().hex}"
    try:
        for i in range(PEER_THREAD_LIMIT):
            sender_token, _ = _runner_pair()
            sender_runner = token_bound_runner_id(sender_token)
            thread_sender = conv_store.create_conversation(
                title=f"thread-{i}-{uuid.uuid4().hex[:6]}",
                agent_id=AGENT_ID,
                runner_id=sender_runner,
            )
            peer_env["perm_store"].grant(ALICE, thread_sender.id, LEVEL_OWNER)
            marker = uuid.uuid5(uuid.NAMESPACE_DNS, f"{ref}-{i}").hex
            resp = await peer_client.post(
                f"/v1/sessions/{receiver.id}/peer-messages",
                json={
                    "sender_session_id": thread_sender.id,
                    "text": f"thread message {marker}",
                    "correlation_id": ref,
                    "wait_seconds": 600,
                },
                headers=_headers(ALICE, sender_token),
            )
            assert resp.json()["disposition"] == "pending", resp.text
        sender_token, _ = _runner_pair()
        sender_runner = token_bound_runner_id(sender_token)
        last_sender = conv_store.create_conversation(
            title=f"thread-last-{uuid.uuid4().hex[:6]}",
            agent_id=AGENT_ID,
            runner_id=sender_runner,
        )
        peer_env["perm_store"].grant(ALICE, last_sender.id, LEVEL_OWNER)
        resp = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={
                "sender_session_id": last_sender.id,
                "text": f"thread message {uuid.uuid4().hex}",
                "correlation_id": ref,
                "wait_seconds": 600,
            },
            headers=_headers(ALICE, sender_token),
        )
        assert resp.json()["disposition"] == "refused"
        assert resp.json()["reason"] == "thread_limit"
    finally:
        peer_env["offline_ids"].discard(receiver.id)


# ── reply detection ─────────────────────────────────────────────────


async def test_reply_detection_with_correlation_id(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """A reverse send with correlation_id=<ref> marks the original replied."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    first = await peer_client.post(
        f"/v1/sessions/{receiver.id}/peer-messages",
        json={
            "sender_session_id": sender.id,
            "text": f"q-{uuid.uuid4().hex}",
            "correlation_id": f"q-{uuid.uuid4().hex}",
        },
        headers=_headers(ALICE, peer_env["sender_token"]),
    )
    assert first.json()["disposition"] == "delivered", first.text
    original_id = first.json()["peer_id"]
    original_ref = first.json()["ref"]
    reply = await peer_client.post(
        f"/v1/sessions/{sender.id}/peer-messages",
        json={
            "sender_session_id": receiver.id,
            "text": f"a-{uuid.uuid4().hex}",
            "correlation_id": original_ref,
        },
        headers=_headers(ALICE, peer_env["receiver_token"]),
    )
    assert reply.json()["disposition"] == "delivered", reply.text
    assert reply.json().get("reply_to") == original_id
    updated = peer_env["peer_store"].get(original_id)
    assert updated is not None
    assert updated.reply_peer_id == reply.json()["peer_id"]
    assert updated.replied_at is not None


async def test_reply_detection_without_correlation_id(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """A correlation-less reverse send marks the newest unreplied record."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    first = await peer_client.post(
        f"/v1/sessions/{receiver.id}/peer-messages",
        json={"sender_session_id": sender.id, "text": f"q2-{uuid.uuid4().hex}"},
        headers=_headers(ALICE, peer_env["sender_token"]),
    )
    assert first.json()["disposition"] == "delivered", first.text
    reply = await peer_client.post(
        f"/v1/sessions/{sender.id}/peer-messages",
        json={"sender_session_id": receiver.id, "text": f"a2-{uuid.uuid4().hex}"},
        headers=_headers(ALICE, peer_env["receiver_token"]),
    )
    assert reply.json().get("reply_to") == first.json()["peer_id"], reply.text


# ── GET + action ────────────────────────────────────────────────────


async def test_get_list_action_404_when_flag_off(db_uri: str) -> None:
    """F8: GET record, GET list and POST action all 404 when the flag is off."""
    async with await _flag_off_client(db_uri) as c:
        got = await c.get(
            "/v1/peer-messages/deadbeefdeadbeefdeadbeefdeadbeef",
            headers=_headers(ALICE, None),
        )
        assert got.status_code == 404, got.text
        listed = await c.get(
            "/v1/sessions/deadbeefdeadbeefdeadbeefdeadbeef/peer-messages",
            headers=_headers(ALICE, None),
        )
        assert listed.status_code == 404, listed.text
        acted = await c.post(
            "/v1/sessions/deadbeefdeadbeefdeadbeefdeadbeef/peer-messages/"
            "deadbeefdeadbeefdeadbeefdeadbeef/action",
            json={"action": "release"},
            headers=_headers(ALICE, None),
        )
        assert acted.status_code == 404, acted.text


async def test_get_record_by_sender_runner_and_user(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """GET succeeds for the sender's runner token and for a READ user."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    sent = await peer_client.post(
        f"/v1/sessions/{receiver.id}/peer-messages",
        json={"sender_session_id": sender.id, "text": f"g-{uuid.uuid4().hex}"},
        headers=_headers(ALICE, peer_env["sender_token"]),
    )
    peer_id = sent.json()["peer_id"]
    got = await peer_client.get(
        f"/v1/peer-messages/{peer_id}",
        headers=_headers(ALICE, peer_env["sender_token"]),
    )
    assert got.status_code == 200, got.text
    assert got.json()["peer_id"] == peer_id
    assert got.json()["state"] == "delivered"
    got_user = await peer_client.get(f"/v1/peer-messages/{peer_id}", headers=_headers(ALICE, None))
    assert got_user.status_code == 200, got_user.text
    stranger = await peer_client.get(f"/v1/peer-messages/{peer_id}", headers=_headers(BOB, None))
    assert stranger.status_code == 404, stranger.text


async def test_list_receiver_records(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """GET /sessions/{id}/peer-messages lists the receiver's held records."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "hold"})
    try:
        sent = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"h-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        assert sent.json()["disposition"] == "held"
        listed = await peer_client.get(
            f"/v1/sessions/{receiver.id}/peer-messages?state=held",
            headers=_headers(ALICE, None),
        )
        assert listed.status_code == 200, listed.text
        assert sent.json()["peer_id"] in [r["peer_id"] for r in listed.json()["data"]]
    finally:
        peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "accept"})


async def test_action_release_and_refuse_with_409(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """Release moves held→pending; refuse of a released record 409s."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "hold"})
    try:
        sent = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"a-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        peer_id = sent.json()["peer_id"]
        released = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages/{peer_id}/action",
            json={"action": "release"},
            headers=_headers(ALICE, None),
        )
        assert released.status_code == 200, released.text
        assert released.json()["state"] == "pending"
        again = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages/{peer_id}/action",
            json={"action": "release"},
            headers=_headers(ALICE, None),
        )
        assert again.status_code == 409
        refused = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages/{peer_id}/action",
            json={"action": "refuse"},
            headers=_headers(ALICE, None),
        )
        assert refused.status_code == 200, refused.text
        assert refused.json()["state"] == "refused_by_user"
        stale = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages/{peer_id}/action",
            json={"action": "refuse"},
            headers=_headers(ALICE, None),
        )
        assert stale.status_code == 409
    finally:
        peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "accept"})


async def test_action_denied_without_edit(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """A READ-only user cannot release or refuse held records."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "hold"})
    try:
        sent = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"d-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        peer_env["perm_store"].grant(BOB, receiver.id, LEVEL_READ)
        denied = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages/{sent.json()['peer_id']}/action",
            json={"action": "release"},
            headers=_headers(BOB, None),
        )
        assert denied.status_code in (403, 404), denied.text
    finally:
        peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "accept"})


async def test_action_denied_for_unrelated_user(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """A user with no grant at all cannot act on held records."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "hold"})
    try:
        sent = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages",
            json={"sender_session_id": sender.id, "text": f"d2-{uuid.uuid4().hex}"},
            headers=_headers(ALICE, peer_env["sender_token"]),
        )
        denied = await peer_client.post(
            f"/v1/sessions/{receiver.id}/peer-messages/{sent.json()['peer_id']}/action",
            json={"action": "refuse"},
            headers=_headers(BOB, None),
        )
        assert denied.status_code in (403, 404), denied.text
    finally:
        peer_env["conv_store"].set_labels(receiver.id, {"peer_inbound": "accept"})


async def test_send_denied_for_unrelated_user(
    peer_client: httpx.AsyncClient, peer_env: dict[str, Any]
) -> None:
    """A user without EDIT on the receiver cannot drive the send."""
    sender = peer_env["sender"]
    receiver = peer_env["receiver"]
    resp = await peer_client.post(
        f"/v1/sessions/{receiver.id}/peer-messages",
        json={"sender_session_id": sender.id, "text": f"u-{uuid.uuid4().hex}"},
        headers=_headers(BOB, peer_env["sender_token"]),
    )
    assert resp.status_code in (403, 404), resp.text
