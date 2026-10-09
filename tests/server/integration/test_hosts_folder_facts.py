"""
Integration tests for ``GET /v1/hosts/{id}/folder-facts``.

Wires up a real host tunnel + REST router pair, drives a fake host
that auto-replies to ``host.folder_facts`` frames, and exercises the
endpoint's contract end-to-end. Backs the project settings Code tab's
live folder facts.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from starlette.responses import JSONResponse

from omnigent.errors import OmnigentError
from omnigent.host.frames import (
    HostFolderFactsFrame,
    HostFolderFactsResultFrame,
    HostHelloFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.server.routes.hosts import create_hosts_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore
from tests.server.helpers import websocket_scope as _websocket_scope

# Same liveness-race flake mitigation as test_hosts_worktrees: the
# mock-WS host can be deregistered under parallel CI load, yielding a
# spurious 409. Tests are sub-second; retry masks the race.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.flaky(reruns=2, reruns_delay=1),
]

_HOST_ID = "7f6bda8f5e302e51cee65f7094f3d49e"
_HOST_NAME = "ff-test-laptop"


def _hello_text(*, project_code: bool) -> str:
    """Encode a hello frame for tests.

    :param project_code: Whether the fake host advertises the folder-facts
        capability.
    :returns: JSON-encoded hello frame.
    """
    return encode_host_frame(
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=_HOST_NAME,
            project_code=project_code,
        )
    )


@pytest.fixture()
def ff_app(
    db_uri: str,
) -> tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore]:
    """
    App with host tunnel + REST routes for folder-facts tests.

    :param db_uri: SQLite URI fixture.
    :returns: (app, registry, host_store, conv_store).
    """
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()
    app.include_router(create_host_tunnel_router(registry, host_store), prefix="/v1")
    app.include_router(
        create_hosts_router(registry, host_store, conv_store),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(
        request: Request,
        exc: OmnigentError,
    ) -> JSONResponse:
        """Convert application errors to structured JSON responses."""
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    return app, registry, host_store, conv_store


@pytest.fixture()
async def ff_setup(
    ff_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
    request: pytest.FixtureRequest,
) -> AsyncIterator[
    tuple[FastAPI, HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]]
]:
    """
    Connect a mock host and auto-reply to folder-facts frames.

    Tests register fake replies in ``replies`` (path → reply dict) before
    calling the REST endpoint; an unregistered path answers a missing-path
    facts result. Parametrize the fixture indirectly to connect a host
    without the ``project_code`` capability.

    :param ff_app: The fixture above.
    :param request: Pytest fixture request (indirect parametrization).
    :returns: Async iterator yielding the wired-up state.
    """
    project_code = getattr(request, "param", True)
    app, registry, _hs, _cs = ff_app
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input(
        {"type": "websocket.receive", "text": _hello_text(project_code=project_code)}
    )
    while registry.get(_HOST_ID) is None:
        await asyncio.sleep(0.01)

    replies: dict[str, dict[str, Any]] = {}
    stop_drain = asyncio.Event()

    async def _drain() -> None:
        """Drain outbound WS frames and feed back the configured reply."""
        while not stop_drain.is_set():
            try:
                output = await comm.receive_output(timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if output.get("type") != "websocket.send":
                continue
            text = output.get("text")
            if not isinstance(text, str):
                continue
            frame = decode_host_frame(text)
            if not isinstance(frame, HostFolderFactsFrame):
                continue
            reply = replies.get(frame.path)
            if reply is None:
                reply_frame = HostFolderFactsResultFrame(
                    request_id=frame.request_id,
                    status="ok",
                    exists=False,
                    error="path does not exist",
                )
            else:
                reply_frame = HostFolderFactsResultFrame(
                    request_id=frame.request_id,
                    **reply,
                )
            await comm.send_input(
                {"type": "websocket.receive", "text": encode_host_frame(reply_frame)}
            )

    drain_task = asyncio.create_task(_drain())
    try:
        yield app, registry, comm, replies
    finally:
        stop_drain.set()
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()
        # Send an explicit disconnect so the tunnel endpoint's finally-block
        # calls host_store.set_offline() and registry.deregister() before
        # this fixture returns. Without this, those calls happen whenever the
        # comm is GC'd — potentially during the next test's setup window.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await comm.send_input({"type": "websocket.disconnect", "code": 1000})


async def test_folder_facts_returns_facts(
    ff_setup: tuple[FastAPI, HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]],
) -> None:
    """The endpoint returns the host's facts as ``{"object": "folder_facts", ...}``."""
    app, _reg, _comm, replies = ff_setup
    replies["/opt/work/omnigent/fork/myrepo"] = {
        "status": "ok",
        "exists": True,
        "is_dir": True,
        "is_repo": True,
        "toplevel": "/opt/work/omnigent/fork/myrepo",
        "branch": "main",
        "head": "b" * 40,
        "detached": False,
        "dirty": True,
        "remotes": [{"name": "origin", "url": "https://git.example.test/x.git"}],
        "setup_command_configured": True,
        "error": None,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/v1/hosts/{_HOST_ID}/folder-facts",
            params={"path": "/opt/work/omnigent/fork/myrepo"},
        )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "object": "folder_facts",
        "exists": True,
        "is_dir": True,
        "is_repo": True,
        "toplevel": "/opt/work/omnigent/fork/myrepo",
        "branch": "main",
        "head": "b" * 40,
        "detached": False,
        "dirty": True,
        "remotes": [{"name": "origin", "url": "https://git.example.test/x.git"}],
        "setup_command_configured": True,
        "error": None,
    }


async def test_folder_facts_missing_path_is_a_fact(
    ff_setup: tuple[FastAPI, HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]],
) -> None:
    """A missing path is a 200 with ``exists`` false, not an error."""
    app, _reg, _comm, _replies = ff_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        # No reply registered → the drain answers the missing-path facts.
        resp = await client.get(
            f"/v1/hosts/{_HOST_ID}/folder-facts",
            params={"path": "/opt/work/missing"},
        )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["object"] == "folder_facts"
    assert payload["exists"] is False
    assert payload["is_repo"] is False


async def test_folder_facts_offline_host_409(
    ff_setup: tuple[FastAPI, HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]],
) -> None:
    """A host with no live tunnel on this replica maps to 409."""
    app, registry, _comm, _replies = ff_setup
    registry.deregister(_HOST_ID)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/v1/hosts/{_HOST_ID}/folder-facts",
            params={"path": "/opt/work/omnigent/fork/myrepo"},
        )
    assert resp.status_code == 409, resp.text


@pytest.mark.parametrize("ff_setup", [False], indirect=True)
async def test_folder_facts_without_capability_501(
    ff_setup: tuple[FastAPI, HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]],
) -> None:
    """A host build without ``project_code`` answers 501 unsupported."""
    app, _reg, _comm, _replies = ff_setup
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/v1/hosts/{_HOST_ID}/folder-facts",
            params={"path": "/opt/work/omnigent/fork/myrepo"},
        )
    assert resp.status_code == 501, resp.text


async def test_folder_facts_unknown_host_404(
    ff_app: tuple[FastAPI, HostRegistry, HostStore, SqlAlchemyConversationStore],
) -> None:
    """An unknown host id yields 404 (existence is gated before the offline check)."""
    app, _reg, _hs, _cs = ff_app
    unknown_id = "1e498b8cd21815434fca9278770ff1d1"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/v1/hosts/{unknown_id}/folder-facts",
            params={"path": "/opt/work/omnigent/fork/myrepo"},
        )
    assert resp.status_code == 404, resp.text


async def test_folder_facts_non_owner_403(db_uri: str) -> None:
    """A host owned by another user answers 403 before any tunnel lookup."""
    from omnigent.server.auth import AuthProvider

    class _Stub(AuthProvider):
        """Stub auth provider reading the caller from X-Test-User."""

        def get_user_id(self, request: Any) -> str | None:
            """Return the caller id the test set on the request.

            :param request: FastAPI request.
            :returns: Header value or ``None``.
            """
            return request.headers.get("X-Test-User")

    auth = _Stub()
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_host_tunnel_router(registry, host_store, auth_provider=auth),
        prefix="/v1",
    )
    app.include_router(
        create_hosts_router(registry, host_store, conv_store, auth_provider=auth),
        prefix="/v1",
    )
    host_id = "f54bb9272002938a3a934bfcb6bb228a"
    host_store.upsert_on_connect(host_id=host_id, name="alice-laptop", user_id="alice@example.com")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(
            f"/v1/hosts/{host_id}/folder-facts",
            params={"path": "/opt/work/omnigent/fork/myrepo"},
            headers={"X-Test-User": "bob@example.com"},
        )
    assert resp.status_code == 403, resp.text
