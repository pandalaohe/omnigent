"""
Integration tests for the assignment host proxy (``omnigent.server.assignment_host``).

Wires up a real host tunnel, drives a fake host that auto-replies to
``host.assignment_prepare`` / ``host.assignment_release`` frames, and
exercises the proxy contract end-to-end: ok results, failed results, the
timeout path, and the ``assignments`` hello capability gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI

from omnigent.host.frames import (
    HostAssignmentPrepareFrame,
    HostAssignmentPrepareRepository,
    HostAssignmentPrepareResultFrame,
    HostAssignmentReleaseFrame,
    HostAssignmentReleaseRepository,
    HostAssignmentReleaseResultFrame,
    HostHelloFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server import assignment_host
from omnigent.server.assignment_host import (
    AssignmentHostUnavailableError,
    host_supports_assignments,
    prepare_assignment_on_host,
    release_assignment_on_host,
)
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.stores.host_store import HostStore

# Same liveness-race flake mitigation as test_hosts_worktrees: the
# mock-WS host can be deregistered under parallel CI load, yielding a
# spurious 409. Tests are sub-second; retry masks the race.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.flaky(reruns=2, reruns_delay=1),
]

_HOST_ID = "8b186e636e3549acb1ea2ee5a552f2af"
_HOST_NAME = "asg-test-laptop"


def _websocket_scope(path: str) -> dict[str, object]:
    """Build a minimal ASGI WebSocket scope.

    :param path: WebSocket path, e.g. ``"/v1/hosts/X/tunnel"``.
    :returns: ASGI scope dict.
    """
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


def _hello_text() -> str:
    """Encode a hello frame advertising the assignments capability."""
    return encode_host_frame(
        HostHelloFrame(
            version="0.1.0-test",
            frame_protocol_version=1,
            name=_HOST_NAME,
            assignments=True,
        )
    )


def _prepare_frame() -> HostAssignmentPrepareFrame:
    """Build a prepare request with one repository entry."""
    return HostAssignmentPrepareFrame(
        request_id="req_ap_1",
        assignment_id="asg_1",
        repositories=[
            HostAssignmentPrepareRepository(
                repository_name="root",
                source_directory="/Users/alice/myrepo",
                remote_url="git@github.com:acme/myrepo.git",
                input_ref="refs/omnigent/assignments/asg_1/input/root",
                input_commit="a" * 40,
                context_manifest_path=".agents/project/manifest.json",
                manifest_digest="sha256:" + "0" * 64,
            )
        ],
    )


def _release_frame() -> HostAssignmentReleaseFrame:
    """Build a release request with one repository entry."""
    return HostAssignmentReleaseFrame(
        request_id="req_ar_1",
        assignment_id="asg_1",
        repositories=[
            HostAssignmentReleaseRepository(
                repository_name="root", source_directory="/Users/alice/myrepo"
            )
        ],
    )


@pytest.fixture()
def asg_app(db_uri: str) -> tuple[FastAPI, HostRegistry, HostStore]:
    """
    App with the host tunnel route for assignment-proxy tests.

    :param db_uri: SQLite URI fixture.
    :returns: (app, registry, host_store).
    """
    registry = HostRegistry()
    host_store = HostStore(db_uri)
    app = FastAPI()
    app.include_router(create_host_tunnel_router(registry, host_store), prefix="/v1")
    return app, registry, host_store


@pytest.fixture()
async def asg_setup(
    asg_app: tuple[FastAPI, HostRegistry, HostStore],
) -> AsyncIterator[tuple[HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]]]:
    """
    Connect a mock host and auto-reply to assignment frames.

    Tests register fake replies in ``replies`` (``"prepare"`` /
    ``"release"`` → reply dict) before calling the proxy. No reply
    registered means the host stays silent (the timeout path).

    :param asg_app: The fixture above.
    :returns: Async iterator yielding the wired-up state.
    """
    app, registry, _hs = asg_app
    path = f"/v1/hosts/{_HOST_ID}/tunnel"
    comm = ApplicationCommunicator(app, _websocket_scope(path))
    await comm.send_input({"type": "websocket.connect"})
    accepted = await comm.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"
    await comm.send_input({"type": "websocket.receive", "text": _hello_text()})
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
            if isinstance(frame, HostAssignmentPrepareFrame):
                reply = replies.get("prepare")
                if reply is None:
                    continue
                reply_frame = HostAssignmentPrepareResultFrame(
                    request_id=frame.request_id,
                    status=reply.get("status", "ok"),
                    directories=reply.get("directories", {}),
                    error_code=reply.get("error_code"),
                    error=reply.get("error"),
                    repository_name=reply.get("repository_name"),
                )
            elif isinstance(frame, HostAssignmentReleaseFrame):
                reply = replies.get("release")
                if reply is None:
                    continue
                reply_frame = HostAssignmentReleaseResultFrame(
                    request_id=frame.request_id,
                    status=reply.get("status", "ok"),
                    removed=reply.get("removed", []),
                    failures=reply.get("failures", {}),
                )
            else:
                continue
            await comm.send_input(
                {"type": "websocket.receive", "text": encode_host_frame(reply_frame)}
            )

    drain_task = asyncio.create_task(_drain())
    try:
        yield registry, comm, replies
    finally:
        stop_drain.set()
        try:
            await asyncio.wait_for(drain_task, timeout=1.0)
        except asyncio.TimeoutError:
            drain_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await comm.send_input({"type": "websocket.disconnect", "code": 1000})


def _conn(registry: HostRegistry) -> HostConnection:
    """Return the connected fake host's connection."""
    conn = registry.get(_HOST_ID)
    assert conn is not None
    return conn


async def test_prepare_ok_result_delivered(
    asg_setup: tuple[HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]],
) -> None:
    """An ok prepare result delivers the repository → directory map."""
    registry, _comm, replies = asg_setup
    replies["prepare"] = {
        "directories": {"root": "/Users/alice/myrepo/.omnigent/worktrees/asg_1/root"},
    }
    result = await prepare_assignment_on_host(
        host_registry=registry, host_conn=_conn(registry), frame=_prepare_frame()
    )
    assert result.status == "ok"
    assert result.directories == {"root": "/Users/alice/myrepo/.omnigent/worktrees/asg_1/root"}
    assert result.request_id == "req_ap_1"


async def test_prepare_failed_result_delivered(
    asg_setup: tuple[HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]],
) -> None:
    """A failed prepare result is returned (not raised) with its error code."""
    registry, _comm, replies = asg_setup
    replies["prepare"] = {
        "status": "failed",
        "error_code": "context_missing",
        "error": "required context missing: root:AGENTS.md",
        "repository_name": "root",
    }
    result = await prepare_assignment_on_host(
        host_registry=registry, host_conn=_conn(registry), frame=_prepare_frame()
    )
    assert result.status == "failed"
    assert result.error_code == "context_missing"
    assert result.error == "required context missing: root:AGENTS.md"
    assert result.repository_name == "root"


async def test_prepare_timeout_suggests_older_version(
    asg_setup: tuple[HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent host times out with a message naming the older-version cause."""
    registry, _comm, _replies = asg_setup
    monkeypatch.setattr(assignment_host, "_ASSIGNMENT_PREPARE_TIMEOUT_S", 0.05)
    conn = _conn(registry)
    with pytest.raises(AssignmentHostUnavailableError, match="older version"):
        await prepare_assignment_on_host(
            host_registry=registry, host_conn=conn, frame=_prepare_frame()
        )
    assert conn.pending_assignment_prepares == {}


async def test_release_partial_result_delivered(
    asg_setup: tuple[HostRegistry, ApplicationCommunicator, dict[str, dict[str, Any]]],
) -> None:
    """A partial release result delivers removed names and per-repo failures."""
    registry, _comm, replies = asg_setup
    replies["release"] = {
        "status": "partial",
        "removed": ["docs"],
        "failures": {"root": "worktree contains uncommitted changes"},
    }
    result = await release_assignment_on_host(
        host_registry=registry, host_conn=_conn(registry), frame=_release_frame()
    )
    assert result.status == "partial"
    assert result.removed == ["docs"]
    assert result.failures == {"root": "worktree contains uncommitted changes"}
    assert result.request_id == "req_ar_1"


def _connection_with_hello(hello: HostHelloFrame) -> HostConnection:
    """Build a connection carrying ``hello`` (capability checks read it only)."""
    return HostConnection(
        workspace_id=0,
        host_id="hello-probe",
        ws=object(),
        hello=hello,
        owner=None,
        outbound_queue=asyncio.Queue(),
        connected_at=0.0,
        last_frame_at=0.0,
    )


def test_host_supports_assignments_true_when_advertised() -> None:
    """A hello with the capability set passes the gate."""
    conn = _connection_with_hello(
        HostHelloFrame(version="v", frame_protocol_version=1, name="n", assignments=True)
    )
    assert host_supports_assignments(conn) is True


def test_host_supports_assignments_false_for_hello_without_the_key() -> None:
    """A hello from before the capability decodes False and fails the gate."""
    decoded = decode_host_frame(
        json.dumps(
            {
                "kind": "host.hello",
                "version": "0.1.0",
                "frame_protocol_version": 1,
                "name": "older-host",
            }
        )
    )
    assert isinstance(decoded, HostHelloFrame)
    assert host_supports_assignments(_connection_with_hello(decoded)) is False
