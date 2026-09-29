"""Cross-host sub-agent completion forwarding (SCC06 F2b, Step B).

A member child running on another host has no parent inbox on its own runner,
so its terminal ``external_session_status`` edge must be delivered through the
PARENT's runner (addressed at the child id, where that runner rebuilds the work
entry from the server). The child's own runner still receives the edge as a
mirror for its pane / exit bookkeeping, but the delivery contract is the
parent-runner forward.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.runtime import session_stream
from omnigent.server.routes import sessions
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

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


@pytest.fixture
async def cross_host_route(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_CrossHostRoute]:
    """An events app with one lead + one cross-host member child conversation."""
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(
        host_id=_LEAD_HOST,
        workspace="/lead/ws",
        runner_id="runner_lead",
    )
    child = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        host_id=_MEMBER_HOST,
        workspace="/member/ws",
        runner_id="runner_member",
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
