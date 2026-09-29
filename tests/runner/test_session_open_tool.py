"""Runner surface and dispatch of ``sys_session_open``.

The tool is advertised only when peer messaging is on AND the session is
top-level (the server derives ``session_open_enabled`` from the init
snapshot); a child shares its parent's runner, so the flag is what hides
the tool. Dispatch proxies ``POST /v1/sessions/{caller}/open`` and passes
the server's JSON through.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runner import tool_dispatch
from omnigent.runner.tool_dispatch import (
    _execute_session_open_tool,
    _granted_tool_names,
    build_native_relay_tool_schemas,
)
from omnigent.spec.types import AgentSpec
from omnigent.tools.manager import ToolManager

_OPEN = "sys_session_open"
_CALLER = "conv_caller"


def _spec(**overrides: Any) -> AgentSpec:
    return AgentSpec(spec_version=1, **overrides)


def _manager_names(**kwargs: Any) -> set[str]:
    manager = ToolManager(_spec(), **kwargs)
    try:
        return set(manager.get_tool_names())
    finally:
        manager.shutdown()


def test_tool_manager_registers_open_only_for_top_level_peer() -> None:
    """Only peer-on + top-level registers the open tool."""
    assert _OPEN in _manager_names(peer_messaging_enabled=True, session_open_enabled=True)
    for kwargs in (
        {"peer_messaging_enabled": False, "session_open_enabled": True},
        {"peer_messaging_enabled": True, "session_open_enabled": False},
        {},
    ):
        assert _OPEN not in _manager_names(**kwargs)


def test_granted_names_follow_open_flag() -> None:
    """The dispatch-time surface excludes the tool for a child and peer off."""
    spec = _spec()
    top_level = _granted_tool_names(spec, peer_messaging_enabled=True, session_open_enabled=True)
    assert _OPEN in top_level
    child = _granted_tool_names(spec, peer_messaging_enabled=True, session_open_enabled=False)
    assert _OPEN not in child
    peer_off = _granted_tool_names(spec)
    assert _OPEN not in peer_off


def test_native_relay_schemas_follow_open_flag() -> None:
    """Relay schemas (spec and no-spec) mirror the manager's flag rule."""
    spec = _spec()
    assert _OPEN in {
        s["name"]
        for s in build_native_relay_tool_schemas(
            spec, peer_messaging_enabled=True, session_open_enabled=True
        )
    }
    assert _OPEN not in {
        s["name"]
        for s in build_native_relay_tool_schemas(
            spec, peer_messaging_enabled=True, session_open_enabled=False
        )
    }
    assert _OPEN in {
        s["name"] for s in build_native_relay_tool_schemas(None, session_open_enabled=True)
    }
    assert _OPEN not in {s["name"] for s in build_native_relay_tool_schemas(None)}


def test_ungranted_child_reason_names_the_top_level_rule() -> None:
    """A child's refusal names the top-level-only rule."""
    reason = tool_dispatch._ungranted_tool_reason(
        _OPEN,
        _spec(),
        None,
        peer_messaging_enabled=True,
        session_open_enabled=False,
    )
    assert reason == "sys_session_open is available only to top-level sessions"


async def test_execute_tool_refuses_open_for_a_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child dispatch is refused before any HTTP call."""
    monkeypatch.setattr(
        tool_dispatch,
        "_peer_messaging_enabled_for",
        lambda _conversation_id: True,
    )
    monkeypatch.setattr(
        tool_dispatch,
        "_session_open_enabled_for",
        lambda _conversation_id: False,
    )
    out = json.loads(
        await tool_dispatch.execute_tool(
            tool_name=_OPEN,
            arguments=json.dumps({"project": "p", "host": "h", "agent": "a"}),
            agent_spec=_spec(),
            conversation_id=_CALLER,
        )
    )
    assert out["error"] == "sys_session_open is available only to top-level sessions"


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")


@pytest.mark.asyncio
async def test_executor_posts_open_route_and_returns_server_json() -> None:
    """The executor POSTs the caller's open path with the args as JSON."""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"state": "opened", "session_id": "conv_new"})

    args = {
        "project": "Target",
        "host": "host-one",
        "agent": "claude-native",
        "message": "hi",
        "wait_for_host": True,
    }
    async with _client(handler) as client:
        out = await _execute_session_open_tool(args, server_client=client, conversation_id=_CALLER)
    assert json.loads(out) == {"state": "opened", "session_id": "conv_new"}
    assert seen["path"] == f"/v1/sessions/{_CALLER}/open"
    assert seen["body"] == args


@pytest.mark.asyncio
async def test_executor_returns_server_refusal_unchanged() -> None:
    """A server refusal body passes through byte-for-byte."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": "refused",
                "reason": "directory_in_use",
                "message": "busy",
                "candidates": [],
            },
        )

    async with _client(handler) as client:
        out = await _execute_session_open_tool(
            {"project": "p", "host": "h", "agent": "a"},
            server_client=client,
            conversation_id=_CALLER,
        )
    assert json.loads(out)["reason"] == "directory_in_use"


@pytest.mark.asyncio
async def test_executor_rejects_unknown_args_without_a_request() -> None:
    """Unknown arguments fail fast with ``invalid_session_open_args``."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("no HTTP call expected for invalid args")

    async with _client(handler) as client:
        out = json.loads(
            await _execute_session_open_tool(
                {"project": "p", "host": "h", "agent": "a", "lifetime_minutes": 5},
                server_client=client,
                conversation_id=_CALLER,
            )
        )
    assert out["error"] == "invalid_session_open_args"
    assert "lifetime_minutes" in out["message"]


@pytest.mark.asyncio
async def test_executor_reports_transport_errors() -> None:
    """A transport error is surfaced as ``session_open_failed``."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    async with _client(handler) as client:
        out = json.loads(
            await _execute_session_open_tool(
                {"project": "p", "host": "h", "agent": "a"},
                server_client=client,
                conversation_id=_CALLER,
            )
        )
    assert out["error"] == "session_open_failed"
    assert "boom" in out["detail"]
