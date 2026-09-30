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

    def __init__(self, *, archived: bool = False, rotate_label: str | None = "0") -> None:
        self.archived = archived
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
                "harness": "claude-native",
            },
        )

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        del url, kwargs
        return httpx.Response(200, json={})

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        del url, kwargs
        return httpx.Response(200, json={})


def _build_app(server_client: _RotationServerClient) -> Any:
    """Build a runner app wired to a claude-native spec and the test server."""
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    return create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
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
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del timeout_s, auto_confirm, confirm_hint
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
