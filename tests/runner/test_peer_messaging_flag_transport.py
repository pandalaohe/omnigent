"""Flag transport for session peer messaging, runner side.

A session initialized with ``peer_messaging_enabled`` true serves
``sys_session_send`` in by-id mode on its turn path even without a
spawn grant; a session initialized without it (or with false) does
not. The flag lives in a per-session dict seeded from the init
snapshot — never the TTL'd envelope cache — so a re-init flips the
surface on the next turn.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

_SEND = "sys_session_send"


def _build_turn_app() -> tuple[FastAPI, _ScriptedHarnessClient]:
    """Runner app whose harness completes one empty turn per request."""
    sse_frames = [
        _sse({"type": "response.created", "response": {"id": "resp_1"}}),
        _sse({"type": "response.output_text.delta", "delta": "hi"}),
        _sse({"type": "response.completed", "response": {"id": "resp_1"}}),
    ]
    harness_client = _ScriptedHarnessClient(sse_frames)

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return AgentSpec(spec_version=1, name="t")

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness_client),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    return app, harness_client


def _init_body(session_id: str, agent_id: str, *, flag: bool | None) -> dict[str, Any]:
    """Session-init POST body; ``flag=None`` selects the legacy id-only body."""
    body: dict[str, Any] = {"session_id": session_id, "agent_id": agent_id}
    if flag is None:
        return body
    body["session_init"] = {
        "protocol_version": 2,
        "server_version": "0.6.0.dev0",
        "session_id": session_id,
        "agent_id": agent_id,
        "sub_agent_name": None,
        "snapshot": {
            "created_at": 1234,
            "updated_at": 1234,
            "workspace": None,
            "labels": {},
            "peer_messaging_enabled": flag,
        },
    }
    return body


def _harness_tool_names(harness_client: _ScriptedHarnessClient) -> set[str]:
    """Tool names the last turn offered its harness."""
    assert harness_client.posted_bodies, "the turn never reached the harness"
    names = set()
    for entry in harness_client.posted_bodies[-1].get("tools", []):
        if not isinstance(entry, dict):
            continue
        function = entry.get("function")
        if isinstance(function, dict) and isinstance(function.get("name"), str):
            names.add(function["name"])
        elif isinstance(entry.get("name"), str):
            names.add(entry["name"])
    return names


async def _init_and_turn(
    client: httpx.AsyncClient,
    harness_client: _ScriptedHarnessClient,
    session_id: str,
    agent_id: str,
    *,
    flag: bool | None,
) -> set[str]:
    """Init the session, run one turn, return the harness's tool names."""
    import asyncio as _asyncio

    init = await client.post("/v1/sessions", json=_init_body(session_id, agent_id, flag=flag))
    assert init.status_code == 201, init.text
    before = len(harness_client.posted_bodies)
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "role": "user",
            "model": "test-agent",
            "content": [{"type": "input_text", "text": "hello"}],
            "harness": "openai-agents",
        },
    )
    assert resp.status_code in (200, 202), resp.text
    async for _ in resp.aiter_text():
        pass
    for _ in range(100):
        if len(harness_client.posted_bodies) > before:
            break
        await _asyncio.sleep(0.05)
    return _harness_tool_names(harness_client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "expected"),
    [
        pytest.param(True, True, id="init-true"),
        pytest.param(False, False, id="init-false"),
        pytest.param(None, False, id="legacy-init"),
    ],
)
async def test_turn_path_send_follows_session_flag(flag: bool | None, expected: bool) -> None:
    """Only a session initialized with the flag offers ``sys_session_send``."""
    app, harness_client = _build_turn_app()
    session_id = f"a1b2c3d4e5f60718293a4b5c6d7e8{uuid.uuid4().hex[:2]}"
    agent_id = f"880b5afda28ad55ff74cbeb9b5fc67fb{uuid.uuid4().hex[:2]}"
    async with _runner_client(app) as client:
        names = await _init_and_turn(client, harness_client, session_id, agent_id, flag=flag)
    assert (_SEND in names) is expected


@pytest.mark.asyncio
async def test_reinit_flips_the_turn_surface() -> None:
    """Re-initializing the same session flips the send tool on the next turn."""
    app, harness_client = _build_turn_app()
    session_id = f"b1b2c3d4e5f60718293a4b5c6d7e8{uuid.uuid4().hex[:2]}"
    agent_id = f"990b5afda28ad55ff74cbeb9b5fc67fb{uuid.uuid4().hex[:2]}"
    async with _runner_client(app) as client:
        names = await _init_and_turn(client, harness_client, session_id, agent_id, flag=False)
        assert _SEND not in names
        names = await _init_and_turn(client, harness_client, session_id, agent_id, flag=True)
        assert _SEND in names


@pytest.mark.asyncio
async def test_reinit_legacy_clears_the_turn_surface() -> None:
    """An envelope-free re-init states no flag, so the next turn drops send."""
    app, harness_client = _build_turn_app()
    session_id = f"c1b2c3d4e5f60718293a4b5c6d7e8{uuid.uuid4().hex[:2]}"
    agent_id = f"aa0b5afda28ad55ff74cbeb9b5fc67fb{uuid.uuid4().hex[:2]}"
    async with _runner_client(app) as client:
        names = await _init_and_turn(client, harness_client, session_id, agent_id, flag=True)
        assert _SEND in names
        names = await _init_and_turn(client, harness_client, session_id, agent_id, flag=None)
        assert _SEND not in names
