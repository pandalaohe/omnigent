"""Stopping a child that borrows its parent's runner leaves that runner up."""

from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.runtime import session_stream
from omnigent.server.routes import sessions
from omnigent.server.routes.sessions import routes_events
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_HOST = "1234567890abcdef1234567890abcdef"
_RUNNER = "runner-shared"


async def test_stop_child_on_parent_runner_releases_only_the_child(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)
    sibling = store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)
    for row in (parent, child, sibling):
        store.set_runner_id(row.id, _RUNNER)
        # A same-host child placed in its own workspace keeps the host binding.
        store.set_host_id(row.id, _HOST, workspace=f"/opt/work/omnigent/{row.id}")
    for row in (child, sibling):
        store.set_session_live_status(row.id, "running")
        sessions._session_status_cache[row.id] = "running"
    requests: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(204)

    host_stop = AsyncMock(return_value=True)
    monkeypatch.setattr(routes_events, "_stop_host_runner_intentionally", host_stop)
    try:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handle), base_url="http://runner"
        ) as runner_client:
            monkeypatch.setattr(
                sessions, "_get_runner_client", AsyncMock(return_value=runner_client)
            )
            response = await client.post(
                f"/v1/sessions/{child.id}/events", json={"type": "stop_session"}
            )
            assert response.status_code == 202, response.text
            host_stop.assert_not_awaited()
            assert requests == [
                ("POST", f"/v1/sessions/{child.id}/events"),
                ("DELETE", f"/v1/sessions/{child.id}"),
            ]
            saved_child = store.get_conversation(child.id)
            saved_sibling = store.get_conversation(sibling.id)
            assert saved_child is not None and saved_child.live_status == "idle"
            assert saved_sibling is not None and saved_sibling.live_status == "running"
            assert sessions._session_status_cache.get(sibling.id) == "running"
            for row in (parent, child, sibling):
                saved = store.get_conversation(row.id)
                assert saved is not None and saved.runner_id == _RUNNER

            response = await client.post(
                f"/v1/sessions/{parent.id}/events", json={"type": "stop_session"}
            )
            assert response.status_code == 202, response.text
            host_stop.assert_awaited_once()
            assert host_stop.await_args.args[:3] == (parent.id, _HOST, _RUNNER)
    finally:
        for row in (parent, child, sibling):
            sessions._session_status_cache.pop(row.id, None)
            routes_events._interrupt_fenced_sessions.discard(row.id)
            session_stream.close(row.id)
