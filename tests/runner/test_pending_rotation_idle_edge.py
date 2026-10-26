"""Turn-end ``/clear`` for a session with an armed handover rotation.

``sys_session_handover(rotate=true)`` arms a pending rotation on the runner;
the session's next native idle edge consumes it and types ``/clear`` into the
session's terminal. A session archived (or rotated) since the request, or one
whose ``omnigent.rotate_requested`` label is gone, must not be cleared.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)

CONV_ID = "conv_rotation_target"


class _RotationServerClient:
    """Answers session snapshots and label reads for the rotation coroutine."""

    def __init__(
        self,
        *,
        archived: bool = False,
        rotate_label: str | None = "0",
        harness: str = "claude-native",
        succession_status: str = "done",
        succession_http_status: int = 200,
        fail_binding: bool = False,
        parent_session_id: str | None = None,
    ) -> None:
        self.archived = archived
        self.harness = harness
        self.succession_status = succession_status
        self.succession_http_status = succession_http_status
        self.fail_binding = fail_binding
        self.parent_session_id = parent_session_id
        self.created: list[dict[str, Any]] = []
        self.succession: list[dict[str, Any]] = []
        self.bound: list[dict[str, Any]] = []
        self.labels: dict[str, str] = {}
        if rotate_label is not None:
            self.labels["omnigent.rotate_requested"] = rotate_label

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        path = url.split("?")[0]
        if path.endswith("/labels"):
            return httpx.Response(200, json={"labels": self.labels})
        return httpx.Response(
            200,
            json={
                "archived": self.archived,
                "labels": self.labels,
                "harness": self.harness,
                "agent_id": "ag_rotation",
                "runner_id": "runner_rotation",
                "workspace": "/opt/work/sample",
                "parent_session_id": self.parent_session_id,
            },
        )

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        if url.endswith("/successor"):
            if not self.created:
                self.created.append({"id": "conv_pi_next"})
            return httpx.Response(
                201,
                json={"id": "conv_pi_next"},
                request=httpx.Request("POST", f"http://test{url}"),
            )
        if url.endswith("/succession"):
            self.succession.append(kwargs["json"])
            return httpx.Response(
                self.succession_http_status,
                json={"status": self.succession_status},
                request=httpx.Request("POST", f"http://test{url}"),
            )
        return httpx.Response(200, json={})

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        self.bound.append(kwargs["json"])
        return httpx.Response(
            503 if self.fail_binding else 200,
            json={},
            request=httpx.Request("PATCH", f"http://test{url}"),
        )


def _build_app(server_client: _RotationServerClient, process_manager: Any = None) -> Any:
    """Build a runner app wired to a claude-native spec and the test server."""
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": server_client.harness}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    return create_runner_app(
        process_manager=process_manager or _FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
    )


async def _seed_session(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/sessions",
        json={"session_id": CONV_ID, "agent_id": "ag_rotation"},
    )
    assert response.status_code == 201, response.text


async def _post_idle(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"/v1/sessions/{CONV_ID}/events",
        json={"type": "external_session_status", "data": {"status": "idle"}},
    )
    assert response.status_code in (200, 204), response.text


async def _wait_for_injections(injected: list[Any], *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not injected:
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for the rotation injector")
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _clean_pending_rotations() -> Iterator[None]:
    """Keep the module-level pending-rotation set per-test."""
    saved = set(runner_app._pending_rotations)
    runner_app._pending_rotations.clear()
    try:
        yield
    finally:
        runner_app._pending_rotations.clear()
        runner_app._pending_rotations.update(saved)


@pytest.mark.asyncio
async def test_idle_edge_clears_session_with_pending_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending rotation types /clear once at the native idle edge."""
    injected: list[tuple[Any, str]] = []

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        ready_timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del timeout_s, ready_timeout_s, auto_confirm, confirm_hint
        injected.append((bridge_dir, command))

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    server = _RotationServerClient()
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client)
        runner_app.record_pending_rotation(CONV_ID)
        await _post_idle(client)
        await _wait_for_injections(injected)

    assert [command for _bridge_dir, command in injected] == ["/clear"]
    assert runner_app.pop_pending_rotation(CONV_ID) is False


@pytest.mark.asyncio
async def test_idle_edge_skips_archived_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An archived session is never cleared, even with the label still set."""
    injected: list[str] = []

    def _fake_inject(bridge_dir: Any, *, command: str, **kwargs: Any) -> None:
        del bridge_dir, kwargs
        injected.append(command)

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    server = _RotationServerClient(archived=True)
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client)
        runner_app.record_pending_rotation(CONV_ID)
        await _post_idle(client)
        await asyncio.sleep(0.2)

    assert injected == []


@pytest.mark.asyncio
async def test_idle_edge_skips_session_without_rotate_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleared/voided ``omnigent.rotate_requested`` skips the injection."""
    injected: list[str] = []

    def _fake_inject(bridge_dir: Any, *, command: str, **kwargs: Any) -> None:
        del bridge_dir, kwargs
        injected.append(command)

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    server = _RotationServerClient(rotate_label=None)
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client)
        runner_app.record_pending_rotation(CONV_ID)
        await _post_idle(client)
        await asyncio.sleep(0.2)

    assert injected == []


@pytest.mark.asyncio
async def test_idle_edge_without_pending_rotation_injects_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No armed rotation means the idle edge never reads or clears the session."""
    injected: list[str] = []

    def _fake_inject(bridge_dir: Any, *, command: str, **kwargs: Any) -> None:
        del bridge_dir, kwargs
        injected.append(command)

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    server = _RotationServerClient()
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client)
        await _post_idle(client)
        await asyncio.sleep(0.2)

    assert injected == []


@pytest.mark.asyncio
@pytest.mark.parametrize("succession_status", ["done", "pending", "noop", "retired"])
async def test_pi_rpc_turn_end_releases_only_after_succession(succession_status: str) -> None:
    server = _RotationServerClient(harness="pi", succession_status=succession_status)
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = _build_app(server, manager)
    async with _runner_client(app, permission_harness="pi") as client:
        await _seed_session(client)
        runner_app.record_pending_rotation(CONV_ID)
        app.state.on_proxy_stream_end(CONV_ID)
        deadline = asyncio.get_running_loop().time() + 2.0
        while not server.succession and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)

    assert len(server.created) == 1
    assert len(server.bound) == 1
    assert server.succession == [{"target_session_id": "conv_pi_next", "allow_empty": True}]
    assert manager.released == ([CONV_ID] if succession_status == "done" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_type", [True, False])
async def test_pi_rpc_user_clear_rotates_without_handover(explicit_type: bool) -> None:
    server = _RotationServerClient(harness="pi", rotate_label=None)
    harness_client = _ScriptedHarnessClient([])
    manager = _FakeProcessManager(harness_client)
    app = _build_app(server, manager)
    async with _runner_client(app, permission_harness="pi") as client:
        await _seed_session(client)
        body: dict[str, Any] = {"content": [{"type": "input_text", "text": "/clear"}]}
        if explicit_type:
            body["type"] = "message"
        response = await client.post(f"/v1/sessions/{CONV_ID}/events", json=body)

    assert response.status_code == 202
    assert not harness_client.posted_bodies
    assert len(server.created) == 1
    assert server.succession == [{"target_session_id": "conv_pi_next", "allow_empty": True}]
    assert manager.released == [CONV_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["binding", "succession", "pending"])
async def test_pi_rpc_retry_keeps_old_process_and_reuses_successor(failure: str) -> None:
    server = _RotationServerClient(
        harness="pi",
        rotate_label=None,
        fail_binding=failure == "binding",
        succession_http_status=409 if failure == "succession" else 200,
        succession_status="pending" if failure == "pending" else "done",
    )
    harness_client = _ScriptedHarnessClient([])
    manager = _FakeProcessManager(harness_client)
    app = _build_app(server, manager)
    message = {"type": "message", "content": [{"type": "input_text", "text": "/clear"}]}
    async with _runner_client(app, permission_harness="pi") as client:
        await _seed_session(client)
        first = await client.post(f"/v1/sessions/{CONV_ID}/events", json=message)
        assert first.status_code == 503
        assert manager.released == []
        assert not harness_client.posted_bodies
        server.fail_binding = False
        server.succession_http_status = 200
        server.succession_status = "done"
        second = await client.post(f"/v1/sessions/{CONV_ID}/events", json=message)
    assert second.status_code == 202
    assert len(server.created) == 1
    assert manager.released == [CONV_ID]
    assert all(body["target_session_id"] == "conv_pi_next" for body in server.succession)


@pytest.mark.asyncio
async def test_pi_child_clear_never_creates_a_top_level_successor() -> None:
    server = _RotationServerClient(harness="pi", rotate_label=None, parent_session_id="mother")
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = _build_app(server, manager)
    async with _runner_client(app, permission_harness="pi") as client:
        await _seed_session(client)
        response = await client.post(
            f"/v1/sessions/{CONV_ID}/events",
            json={"type": "message", "content": [{"type": "input_text", "text": "/clear"}]},
        )
    assert response.status_code == 409
    assert not server.created and not server.succession and not manager.released


@pytest.mark.asyncio
async def test_pi_retired_rotation_is_a_final_conflict() -> None:
    server = _RotationServerClient(harness="pi", succession_status="retired")
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = _build_app(server, manager)
    async with _runner_client(app, permission_harness="pi") as client:
        await _seed_session(client)
        result = await client.post(
            f"/v1/sessions/{CONV_ID}/events",
            json={"type": "message", "content": [{"type": "input_text", "text": "/clear"}]},
        )
    assert result.status_code == 409
    assert result.json()["detail"]["error"] == "succession_retired"
    assert not manager.released
