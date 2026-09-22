"""Flag transport for session peer messaging, runner side.

A session initialized with ``peer_messaging_enabled`` true serves
``sys_session_send`` in by-id mode on its turn path even without a
spawn grant; a session initialized without it (or with false) does
not. The flag lives in a per-session dict seeded from the init
snapshot — never the TTL'd envelope cache — so an envelope re-init
flips the surface on the next turn; an envelope-free (legacy) re-init
carries no flag snapshot and keeps the session's last known value
instead (a WS reconnect must not downgrade a real grant). The native
comment relay (``tool_relay.json``, consumed by codex-native /
claude-native) follows the same rule and rebuilds in place whenever
the stored flag changes, even when the relay was started before the
session's own init landed.
"""

from __future__ import annotations

import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.entities.session_resources import terminal_resource_view
from omnigent.harnesses.claude_native.bridge import (
    bridge_dir_for_bridge_id,
    prepare_bridge_dir,
)
from omnigent.inner.datamodel import TerminalEnvSpec
from omnigent.runner import create_runner_app
from omnigent.runner.app import get_session_peer_messaging_enabled
from omnigent.spec.types import AgentSpec
from omnigent.terminals import TerminalListEntry
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient, make_test_terminal_instance

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
async def test_reinit_legacy_keeps_the_turn_surface() -> None:
    """An envelope-free re-init carries no flag; a known True grant survives."""
    app, harness_client = _build_turn_app()
    session_id = f"c1b2c3d4e5f60718293a4b5c6d7e8{uuid.uuid4().hex[:2]}"
    agent_id = f"aa0b5afda28ad55ff74cbeb9b5fc67fb{uuid.uuid4().hex[:2]}"
    async with _runner_client(app) as client:
        names = await _init_and_turn(client, harness_client, session_id, agent_id, flag=True)
        assert _SEND in names
        names = await _init_and_turn(client, harness_client, session_id, agent_id, flag=None)
        assert _SEND in names


class _RelayStubRegistry:
    """Non-spawning resource registry: a terminal launch only starts the relay."""

    terminal_registry = None

    def __init__(self, tmp_path: Path) -> None:
        self._tmp_path = tmp_path

    def set_terminal_activity_publisher(self, publisher: object) -> None:
        del publisher

    def set_session_status_publisher(self, publisher: object) -> None:
        del publisher

    def set_terminal_exit_publisher(self, publisher: object) -> None:
        del publisher

    def compute_default_env_root(self, session_id: str, agent_spec: Any) -> str:
        del session_id, agent_spec
        return str(self._tmp_path)

    async def launch_required_terminal(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        parent_os_env: object | None = None,
        resource_role: str | None = None,
    ) -> Any:
        del spec, cwd_override, sandbox_override, parent_os_env, resource_role
        return terminal_resource_view(
            session_id,
            TerminalListEntry(
                terminal_name=terminal_name,
                session_key=session_key,
                instance=make_test_terminal_instance(terminal_name, session_key, self._tmp_path),
            ),
        )

    async def launch_auxiliary_terminal(
        self,
        session_id: str,
        terminal_name: str,
        session_key: str,
        spec: TerminalEnvSpec,
        cwd_override: str | None = None,
        sandbox_override: str | None = None,
        parent_os_env: object | None = None,
        resource_role: str | None = None,
    ) -> Any:
        return await self.launch_required_terminal(
            session_id,
            terminal_name,
            session_key,
            spec,
            cwd_override=cwd_override,
            sandbox_override=sandbox_override,
            parent_os_env=parent_os_env,
            resource_role=resource_role,
        )

    async def cleanup_session(self, session_id: str) -> None:
        del session_id

    def session_activity_epoch(self, session_id: str) -> int:
        """Mirror ``ResourceRegistry``'s default for a session with no observed turns."""
        del session_id
        return 0

    def session_turn_is_active(self, session_id: str) -> bool:
        """Mirror ``ResourceRegistry``'s default for a session with no observed turns."""
        del session_id
        return False


def _relay_tool_names(bridge_dir: Path) -> set[str]:
    """Tool names the session's comment relay currently advertises."""
    info = json.loads((bridge_dir / "tool_relay.json").read_text())
    return {entry["name"] for entry in info["tools"]}


def _relay_send_description(bridge_dir: Path) -> str | None:
    """``sys_session_send``'s advertised description, or ``None`` if absent."""
    info = json.loads((bridge_dir / "tool_relay.json").read_text())
    for entry in info["tools"]:
        if entry["name"] == _SEND:
            return entry.get("description")
    return None


def _relay_app(tmp_path: Path) -> FastAPI:
    """Runner app whose only observable surface is the comment relay."""
    return create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        resource_registry=_RelayStubRegistry(tmp_path),  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_relay_built_before_init_picks_up_peer_flag_on_init(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live codex order: relay ensured (flag absent) before init lands True.

    Reproduces the acceptance defect (X5): a resource-access path (terminal
    ensure / codex pre-launch) starts the comment relay before this session's
    own init envelope has ever been seen, so the relay is first built reading
    the flag's absent-default False. Init then lands with the flag True and
    must rebuild the already-started relay in place.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge.post_tools_changed",
        lambda *args: None,
    )
    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    agent_id = f"cc0b5afda28ad55ff74cbeb9b5fc67fb{uuid.uuid4().hex[:2]}"
    prepare_bridge_dir(session_id, workspace=tmp_path)
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    app = _relay_app(tmp_path)
    try:
        async with _runner_client(app) as client:
            launched = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
            )
            assert launched.status_code == 200, launched.text
            assert _SEND not in _relay_tool_names(bridge_dir)

            init = await client.post(
                "/v1/sessions", json=_init_body(session_id, agent_id, flag=True)
            )
            assert init.status_code == 201, init.text
            assert _SEND in _relay_tool_names(bridge_dir)
            description = _relay_send_description(bridge_dir) or ""
            assert "Confined to your direct children" not in description
            assert "peer" in description.lower()
            assert get_session_peer_messaging_enabled(session_id) is True
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_relay_built_after_init_reflects_peer_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mirror case: init lands first (flag True), the relay is built after."""
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge.post_tools_changed",
        lambda *args: None,
    )
    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    agent_id = f"dd0b5afda28ad55ff74cbeb9b5fc67fb{uuid.uuid4().hex[:2]}"
    prepare_bridge_dir(session_id, workspace=tmp_path)
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    app = _relay_app(tmp_path)
    try:
        async with _runner_client(app) as client:
            init = await client.post(
                "/v1/sessions", json=_init_body(session_id, agent_id, flag=True)
            )
            assert init.status_code == 201, init.text

            launched = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
            )
            assert launched.status_code == 200, launched.text
            assert _SEND in _relay_tool_names(bridge_dir)
            description = _relay_send_description(bridge_dir) or ""
            assert "Confined to your direct children" not in description
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)


@pytest.mark.asyncio
async def test_relay_legacy_reinit_keeps_known_peer_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy re-init must not silently revert a known-True flag to False.

    Covers the other half of X5: a session whose envelope was already seen
    (flag True, relay rebuilt to match) later gets an envelope-free re-init
    (WS reconnect / resume) — the relay and the dispatch-time flag must both
    still advertise peer messaging afterward.
    """
    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge.post_tools_changed",
        lambda *args: None,
    )
    session_id = f"conv_{uuid.uuid4().hex[:12]}"
    agent_id = f"ee0b5afda28ad55ff74cbeb9b5fc67fb{uuid.uuid4().hex[:2]}"
    prepare_bridge_dir(session_id, workspace=tmp_path)
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    app = _relay_app(tmp_path)
    try:
        async with _runner_client(app) as client:
            init = await client.post(
                "/v1/sessions", json=_init_body(session_id, agent_id, flag=True)
            )
            assert init.status_code == 201, init.text
            launched = await client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "bridge_inject_dir": True},
            )
            assert launched.status_code == 200, launched.text
            assert _SEND in _relay_tool_names(bridge_dir)

            reinit = await client.post(
                "/v1/sessions", json=_init_body(session_id, agent_id, flag=None)
            )
            assert reinit.status_code == 201, reinit.text
            assert _SEND in _relay_tool_names(bridge_dir)
            assert get_session_peer_messaging_enabled(session_id) is True
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)
