"""Cross-host sub-agent completion forwarding (SCC06 F2b, Step B).

A member child running on another host has no parent inbox on its own runner,
so its terminal ``external_session_status`` edge must be delivered through the
PARENT's runner (addressed at the child id, where that runner rebuilds the work
entry from the server). The child's own runner still receives the edge as a
mirror for its pane / exit bookkeeping, but the delivery contract is the
parent-runner forward.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities import SessionPeerMessage
from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.errors import OmnigentError
from omnigent.runtime import session_stream
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.routes import sessions
from omnigent.server.routes.sessions.peer_child_turn import classify_child_turn
from omnigent.server.routes.sessions.routes_peer import format_peer_envelope
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore

_LEAD_HOST = "11" * 16
_MEMBER_HOST = "22" * 16


@dataclass
class _CrossHostRoute:
    client: httpx.AsyncClient
    store: SqlAlchemyConversationStore
    parent_id: str
    child_id: str
    forwarded: list[tuple[str, dict[str, Any]]]
    waited_for: list[str]
    peer_store: SqlAlchemyPeerMessageStore | None = None
    peer_on: bool = False


@pytest.fixture
async def cross_host_route(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> AsyncIterator[_CrossHostRoute]:
    """An events app with one lead + one cross-host member child conversation.

    Indirectly parametrize the fixture with ``True`` to enable the
    session-peer-messaging flag, which gates turn attribution on the edge.
    """
    peer_on = bool(getattr(request, "param", False))
    store = SqlAlchemyConversationStore(db_uri)
    peer_store = SqlAlchemyPeerMessageStore(db_uri)
    parent = store.create_conversation(
        host_id=_LEAD_HOST,
        workspace="/opt/work/omnigent/lead",
        runner_id="runner_lead",
    )
    child = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        host_id=_MEMBER_HOST,
        workspace="/opt/work/omnigent/member",
        runner_id="runner_member",
    )
    flags = FeatureFlags(frozenset({Feature.SESSION_PEER_MESSAGING}) if peer_on else frozenset())

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        sessions.create_sessions_router(
            store,
            SqlAlchemyAgentStore(db_uri),
            feature_flags=flags,
            peer_message_store=peer_store,
        ),
        prefix="/v1",
    )
    forwarded: list[tuple[str, dict[str, Any]]] = []
    waited_for: list[str] = []

    def capture_forward(request: httpx.Request) -> httpx.Response:
        forwarded.append((request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(capture_forward), base_url="http://runner"
        ) as runner,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):

        async def get_runner_client(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
            return runner

        async def wait_for_runner_client(
            session_id: str, *_args: Any, **_kwargs: Any
        ) -> httpx.AsyncClient:
            waited_for.append(session_id)
            return runner

        monkeypatch.setattr(sessions, "_get_runner_client", get_runner_client)
        monkeypatch.setattr(sessions, "_wait_for_runner_client", wait_for_runner_client)
        monkeypatch.setattr(session_stream, "publish", Mock())
        yield _CrossHostRoute(
            client,
            store,
            parent.id,
            child.id,
            forwarded,
            waited_for,
            peer_store,
            peer_on,
        )


async def _post_status(route: _CrossHostRoute, status: str) -> httpx.Response:
    return await route.client.post(
        f"/v1/sessions/{route.child_id}/events",
        json={"type": "external_session_status", "data": {"status": status, "output": "result"}},
    )


@pytest.mark.asyncio
async def test_cross_host_terminal_status_is_delivered_through_the_parent_runner(
    cross_host_route: _CrossHostRoute,
) -> None:
    """The parent runner gets the terminal edge; the child runner gets a mirror."""
    route = cross_host_route
    response = await _post_status(route, "idle")

    assert response.status_code == 202, response.text
    assert response.json() == {"queued": False}
    # Delivery contract: the parent's runner was resolved and addressed at the
    # child id, with the plain event body.
    assert route.waited_for == [route.parent_id]
    paths = [path for path, _body in route.forwarded]
    assert paths == [
        f"/v1/sessions/{route.child_id}/events",
        f"/v1/sessions/{route.child_id}/events",
    ]
    mirror_body = route.forwarded[0][1]
    delivery_body = route.forwarded[1][1]
    assert mirror_body["type"] == "external_session_status"
    assert mirror_body["data"]["cross_host"] is True
    assert "cross_host" not in delivery_body["data"]
    assert delivery_body["data"] == {"status": "idle", "output": "result"}


@pytest.mark.asyncio
async def test_non_terminal_cross_host_status_forwards_only_to_the_child_runner(
    cross_host_route: _CrossHostRoute,
) -> None:
    """A ``running`` edge is a rebrief for the child runner, not a completion."""
    route = cross_host_route
    response = await _post_status(route, "running")

    assert response.status_code == 202, response.text
    assert route.waited_for == []
    assert len(route.forwarded) == 1
    _path, body = route.forwarded[0]
    assert "cross_host" not in body["data"]


@pytest.mark.asyncio
async def test_cross_host_terminal_status_fails_when_the_parent_runner_is_gone(
    cross_host_route: _CrossHostRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The child-runner mirror alone must not ack a completion nobody owns."""
    route = cross_host_route

    async def wait_none(session_id: str, *_args: Any, **_kwargs: Any) -> None:
        route.waited_for.append(session_id)
        return

    monkeypatch.setattr(sessions, "_wait_for_runner_client", wait_none)
    response = await _post_status(route, "idle")

    assert response.status_code == 503, response.text
    assert route.waited_for == [route.parent_id]


@pytest.fixture
async def same_host_route(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_CrossHostRoute]:
    """An events app with a lead and a child on the SAME host (unchanged path)."""
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(
        host_id=_LEAD_HOST,
        workspace="/lead/ws",
        runner_id="runner_lead",
    )
    child = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        host_id=_LEAD_HOST,
        workspace="/lead/ws/child",
        runner_id="runner_lead",
    )

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        sessions.create_sessions_router(store, SqlAlchemyAgentStore(db_uri)), prefix="/v1"
    )
    forwarded: list[tuple[str, dict[str, Any]]] = []
    waited_for: list[str] = []

    def capture_forward(request: httpx.Request) -> httpx.Response:
        forwarded.append((request.url.path, json.loads(request.content)))
        return httpx.Response(204)

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(capture_forward), base_url="http://runner"
        ) as runner,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):

        async def get_runner_client(*_args: Any, **_kwargs: Any) -> httpx.AsyncClient:
            return runner

        async def wait_for_runner_client(
            session_id: str, *_args: Any, **_kwargs: Any
        ) -> httpx.AsyncClient:
            waited_for.append(session_id)
            return runner

        monkeypatch.setattr(sessions, "_get_runner_client", get_runner_client)
        monkeypatch.setattr(sessions, "_wait_for_runner_client", wait_for_runner_client)
        monkeypatch.setattr(session_stream, "publish", Mock())
        yield _CrossHostRoute(client, store, parent.id, child.id, forwarded, waited_for)


@pytest.mark.asyncio
async def test_same_host_child_keeps_the_single_child_runner_forward(
    same_host_route: _CrossHostRoute,
) -> None:
    """A co-located child's terminal edge is unchanged: one child-runner forward."""
    route = same_host_route
    response = await _post_status(route, "idle")

    assert response.status_code == 202, response.text
    assert route.waited_for == []
    assert len(route.forwarded) == 1
    _path, body = route.forwarded[0]
    assert "cross_host" not in body["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_host", "expects_forward"),
    [
        (_LEAD_HOST, False),
        (_MEMBER_HOST, True),
    ],
    ids=["same-effective-host", "different-host"],
)
async def test_effective_host_decides_cross_host_for_hostless_row_parent(
    cross_host_route: _CrossHostRoute,
    child_host: str,
    expects_forward: bool,
) -> None:
    """A placed child of a hostless-row parent compares effective hosts.

    The parent row carries no ``host_id`` of its own (a mirrored/native child
    row), but the root is host-bound. A child that names that same host shares
    the parent's runner and must not trigger a second-runner forward; a child
    on another host must.
    """
    route = cross_host_route
    root = route.store.create_conversation(
        host_id=_LEAD_HOST,
        workspace="/lead/ws",
        runner_id="runner_lead",
    )
    hostless_parent = route.store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=root.id,
        runner_id="runner_lead",
    )
    child = route.store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=hostless_parent.id,
        host_id=child_host,
        workspace="/placed/ws",
        runner_id="runner_placed",
    )

    response = await route.client.post(
        f"/v1/sessions/{child.id}/events",
        json={"type": "external_session_status", "data": {"status": "idle", "output": "result"}},
    )

    assert response.status_code == 202, response.text
    if expects_forward:
        assert route.waited_for == [hostless_parent.id]
        assert len(route.forwarded) == 2
    else:
        assert route.waited_for == []
        assert len(route.forwarded) == 1
        assert "cross_host" not in route.forwarded[0][1]["data"]


_FORGED_PEER_TURN: dict[str, Any] = {
    "parent_session_id": "forged-parent",
    "peer_id": "forged-peer",
    "result_item_id": "forged-item",
    "ref": "forged-ref",
    "sender_session_id": "forged-sender",
    "sender_title": "Forged",
    "sender_origin": "forged",
    "excerpt": "forged",
}


def _seed_peer_turn(route: _CrossHostRoute, *, output: str = "final answer") -> None:
    """Seed a child transcript of a verified foreign envelope + assistant answer."""
    assert route.peer_store is not None
    sender = route.store.create_conversation(kind="default", title="sender")
    record = route.peer_store.create(
        SessionPeerMessage(
            id=uuid4().hex,
            sender_session_id=sender.id,
            receiver_session_id=route.child_id,
            ref="ref-1",
            text="do the thing",
            state="delivered",
            created_at=1,
            expires_at=2,
        )
    )
    envelope = format_peer_envelope(
        sender_session_id=sender.id,
        sender_title="Sender",
        sender_agent_name="claude",
        sender_project_id=None,
        ref="ref-1",
        peer_id=record.id,
        text="do the thing",
    )
    route.store.append(
        route.child_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp-1",
                data=MessageData(role="user", content=[{"type": "input_text", "text": envelope}]),
            ),
            NewConversationItem(
                type="message",
                response_id="resp-1",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": output}],
                    agent="claude",
                ),
            ),
        ],
    )


def _seed_plain_user_turn(route: _CrossHostRoute, *, output: str = "final answer") -> None:
    route.store.append(
        route.child_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp-1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "just a human message"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp-1",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": output}],
                    agent="claude",
                ),
            ),
        ],
    )


async def _post_settling_edge(
    route: _CrossHostRoute, *, output: str, data: dict[str, Any] | None = None
) -> httpx.Response:
    payload: dict[str, Any] = {"status": "idle", "output": output}
    if data is not None:
        payload.update(data)
    return await route.client.post(
        f"/v1/sessions/{route.child_id}/events",
        json={"type": "external_session_status", "data": payload},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_host_route", [True], indirect=True)
async def test_peer_turn_annotates_both_cross_host_forwards(
    cross_host_route: _CrossHostRoute,
) -> None:
    """A peer-attributed settling edge carries ``data.peer_turn`` on both forwards."""
    route = cross_host_route
    _seed_peer_turn(route)
    response = await _post_settling_edge(route, output="final answer")

    assert response.status_code == 202, response.text
    assert len(route.forwarded) == 2
    for _path, body in route.forwarded:
        peer_turn = body["data"]["peer_turn"]
        assert peer_turn["sender_title"] == "Sender"
        assert peer_turn["ref"] == "ref-1"
        assert peer_turn["parent_session_id"] == route.parent_id


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_host_route", [True], indirect=True)
async def test_plain_user_turn_drops_a_forged_peer_turn(
    cross_host_route: _CrossHostRoute,
) -> None:
    """A caller-forged ``peer_turn`` never survives a classification miss."""
    route = cross_host_route
    _seed_plain_user_turn(route)
    response = await _post_settling_edge(
        route, output="final answer", data={"peer_turn": _FORGED_PEER_TURN}
    )

    assert response.status_code == 202, response.text
    assert len(route.forwarded) == 2
    for _path, body in route.forwarded:
        assert "peer_turn" not in body["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_host_route", [False], indirect=True)
async def test_peer_turn_absent_when_flag_off(
    cross_host_route: _CrossHostRoute,
) -> None:
    """The flag gates attribution: off means a forged ``peer_turn`` is dropped."""
    route = cross_host_route
    _seed_peer_turn(route)
    response = await _post_settling_edge(
        route, output="final answer", data={"peer_turn": _FORGED_PEER_TURN}
    )

    assert response.status_code == 202, response.text
    assert len(route.forwarded) == 2
    for _path, body in route.forwarded:
        assert "peer_turn" not in body["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_host_route", [True], indirect=True)
async def test_classifier_dispatch_failure_forwards_the_edge_unchanged(
    cross_host_route: _CrossHostRoute, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure before the classifier runs must not abort the terminal edge."""
    route = cross_host_route
    _seed_peer_turn(route)

    real_to_thread = asyncio.to_thread

    async def failing_to_thread(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        if func is classify_child_turn:
            raise RuntimeError("executor is shut down")
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", failing_to_thread)
    response = await _post_settling_edge(route, output="final answer")

    assert response.status_code == 202, response.text
    assert len(route.forwarded) == 2
    for _path, body in route.forwarded:
        assert "peer_turn" not in body["data"]
