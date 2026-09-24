"""Transport of the server-held global instructions, runner side.

A session initialized with ``global_instructions`` records the text in a
per-session map seeded from the init snapshot — never the TTL'd envelope
cache — so an envelope re-init refreshes it and every turn still composes
it after the cache's 60 s TTL. A session that records a working tree other
than its launch directory has the worktree line prepended to that text; SDK
harnesses receive the extras as the last framework instruction of the
composed per-turn prompt.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.runtime.prompt import (
    EMBEDDED_BROWSER_PRIORITY_INSTRUCTION,
    WORKTREE_INSTRUCTION,
    session_startup_extras,
)
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient


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


def _init_body(
    session_id: str,
    agent_id: str,
    global_instructions: str,
    *,
    workspace: str | None = None,
    worktree: str | None = None,
) -> dict[str, Any]:
    """Session-init POST body carrying *global_instructions* in the snapshot."""
    return {
        "session_id": session_id,
        "agent_id": agent_id,
        "session_init": {
            "protocol_version": 2,
            "server_version": "0.6.0.dev0",
            "session_id": session_id,
            "agent_id": agent_id,
            "sub_agent_name": None,
            "snapshot": {
                "created_at": 1234,
                "updated_at": 1234,
                "workspace": workspace,
                "worktree": worktree,
                "labels": {},
                "global_instructions": global_instructions,
            },
        },
    }


async def _init_session(
    client: httpx.AsyncClient,
    session_id: str,
    agent_id: str,
    global_instructions: str,
    *,
    workspace: str | None = None,
    worktree: str | None = None,
) -> None:
    init = await client.post(
        "/v1/sessions",
        json=_init_body(
            session_id,
            agent_id,
            global_instructions,
            workspace=workspace,
            worktree=worktree,
        ),
    )
    assert init.status_code == 201, init.text


def _ids() -> tuple[str, str]:
    """A fresh session/agent id pair this test owns exclusively."""
    return (
        f"d1b2c3d4e5f60718293a4b5c6d7e8{uuid.uuid4().hex[:2]}",
        f"bb0b5afda28ad55ff74cbeb9b5fc67fb{uuid.uuid4().hex[:2]}",
    )


async def _turn_instructions(
    client: httpx.AsyncClient,
    harness_client: _ScriptedHarnessClient,
    session_id: str,
) -> str:
    """Run one background SDK turn; return the instructions it composed."""
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
        await asyncio.sleep(0.05)
    assert len(harness_client.posted_bodies) > before, "the turn never reached the harness"
    composed = harness_client.posted_bodies[-1].get("instructions")
    assert isinstance(composed, str)
    return composed


@pytest.mark.asyncio
async def test_background_turn_composes_the_global_text_last() -> None:
    """A background SDK turn ends its composed instructions with the recorded text."""
    app, harness_client = _build_turn_app()
    session_id, agent_id = _ids()
    async with _runner_client(app) as client:
        await _init_session(client, session_id, agent_id, "GLOBAL-MARKER")
        composed = await _turn_instructions(client, harness_client, session_id)

    assert EMBEDDED_BROWSER_PRIORITY_INSTRUCTION in composed
    assert composed.endswith("GLOBAL-MARKER")


@pytest.mark.asyncio
async def test_reinit_replaces_the_global_text_on_the_next_turn() -> None:
    """A re-init overwrites the recorded text: the next turn composes B, not A."""
    app, harness_client = _build_turn_app()
    session_id, agent_id = _ids()
    async with _runner_client(app) as client:
        await _init_session(client, session_id, agent_id, "FIRST-TEXT")
        first = await _turn_instructions(client, harness_client, session_id)
        assert first.endswith("FIRST-TEXT")

        await _init_session(client, session_id, agent_id, "SECOND-TEXT")
        composed = await _turn_instructions(client, harness_client, session_id)

    assert composed.endswith("SECOND-TEXT")
    assert "FIRST-TEXT" not in composed


@pytest.mark.asyncio
async def test_turn_composes_the_text_after_the_envelope_cache_expires(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The silent-failure path the map exists for: cache gone, text not.

    Composition reads the recorded text rather than the raw envelope cache,
    so a turn arriving after the cache's TTL still carries it.
    """
    monkeypatch.setattr("omnigent.runner.app._SESSION_INIT_ENVELOPE_TTL_SECONDS", 0.0)
    app, harness_client = _build_turn_app()
    session_id, agent_id = _ids()
    async with _runner_client(app) as client:
        await _init_session(client, session_id, agent_id, "SURVIVOR-TEXT")
        composed = await _turn_instructions(client, harness_client, session_id)

    assert composed.endswith("SURVIVOR-TEXT")


def _spy_on_startup_extras(
    monkeypatch: pytest.MonkeyPatch,
) -> list[str | None]:
    """Record every extras value the runner writes into its per-session map."""
    recorded: list[str | None] = []
    real_extras = session_startup_extras

    def _spy(
        global_instructions: str | None,
        *,
        workspace: str | None,
        worktree: str | None,
    ) -> str | None:
        value = real_extras(global_instructions, workspace=workspace, worktree=worktree)
        recorded.append(value)
        return value

    monkeypatch.setattr("omnigent.runner.app.session_startup_extras", _spy)
    return recorded


@pytest.mark.asyncio
async def test_worktree_line_recorded_before_the_global_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session-init envelope with distinct workspace and worktree records the
    worktree line ahead of the global text, and the next turn composes them
    after the framework instructions."""
    recorded = _spy_on_startup_extras(monkeypatch)
    app, harness_client = _build_turn_app()
    session_id, agent_id = _ids()
    async with _runner_client(app) as client:
        await _init_session(
            client,
            session_id,
            agent_id,
            "GLOBAL-MARKER",
            workspace="/entry",
            worktree="/entry/.worktrees/repo/topic",
        )
        composed = await _turn_instructions(client, harness_client, session_id)

    line = WORKTREE_INSTRUCTION.format(workspace="/entry", worktree="/entry/.worktrees/repo/topic")
    assert recorded == [f"{line}\n\nGLOBAL-MARKER"]
    assert composed.endswith(f"{line}\n\nGLOBAL-MARKER")
    assert composed.index(EMBEDDED_BROWSER_PRIORITY_INSTRUCTION) < composed.index(line)


@pytest.mark.asyncio
async def test_without_a_worktree_the_recorded_value_is_the_global_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session without a worktree records the global text exactly — the
    unchanged path — and composes no worktree line."""
    recorded = _spy_on_startup_extras(monkeypatch)
    app, harness_client = _build_turn_app()
    session_id, agent_id = _ids()
    async with _runner_client(app) as client:
        await _init_session(
            client,
            session_id,
            agent_id,
            "GLOBAL-MARKER",
            workspace="/entry",
        )
        composed = await _turn_instructions(client, harness_client, session_id)

    assert recorded == ["GLOBAL-MARKER"]
    assert composed.endswith("GLOBAL-MARKER")
    assert "You started in the project directory" not in composed
