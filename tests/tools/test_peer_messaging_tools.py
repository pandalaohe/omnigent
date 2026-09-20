"""Tests for the peer-messaging send registration contract.

A no-spawn spec gets ``sys_session_send`` in by-id mode only when
``peer_messaging_enabled`` is true — and never ``sys_session_close``.
With the flag off neither registers; a spawn spec gets both regardless
of the flag.
"""

from __future__ import annotations

from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas
from omnigent.spec.types import AgentSpec
from omnigent.tools.manager import ToolManager

_SEND = "sys_session_send"
_CLOSE = "sys_session_close"


def _no_spawn_spec() -> AgentSpec:
    return AgentSpec(spec_version=1)


def _spawn_spec() -> AgentSpec:
    return AgentSpec(spec_version=1, spawn=True)


def _schema_names(spec: AgentSpec, **kwargs: object) -> set[str]:
    return {s["function"]["name"] for s in ToolManager(spec, **kwargs).get_tool_schemas()}


def _send_schema(spec: AgentSpec, **kwargs: object) -> dict[str, object]:
    for schema in ToolManager(spec, **kwargs).get_tool_schemas():
        function = schema.get("function")
        if isinstance(function, dict) and function.get("name") == _SEND:
            params = function.get("parameters")
            assert isinstance(params, dict)
            return params
    raise AssertionError(f"{_SEND} not registered")


def test_no_spawn_spec_with_flag_registers_send_only() -> None:
    """The flag registers by-id send — not close — for a no-spawn spec."""
    names = _schema_names(_no_spawn_spec(), peer_messaging_enabled=True)
    assert _SEND in names
    assert _CLOSE not in names


def test_no_spawn_spec_with_flag_send_is_by_id_mode() -> None:
    """The flag-added send schema omits the named-mode agent/title params."""
    properties = _send_schema(_no_spawn_spec(), peer_messaging_enabled=True).get("properties")
    assert isinstance(properties, dict)
    assert "session_id" in properties
    assert "agent" not in properties
    assert "title" not in properties


def test_no_spawn_spec_without_flag_registers_neither() -> None:
    """Flag off: no-spawn specs get neither send nor close."""
    names = _schema_names(_no_spawn_spec())
    assert _SEND not in names
    assert _CLOSE not in names


def test_spawn_spec_registers_both_regardless_of_flag() -> None:
    """A spawn spec gets send and close with or without the flag."""
    for kwargs in ({}, {"peer_messaging_enabled": True}):
        names = _schema_names(_spawn_spec(), **kwargs)  # type: ignore[arg-type]
        assert _SEND in names
        assert _CLOSE in names


def test_relay_schemas_follow_the_flag() -> None:
    """The native relay surface carries send for no-spawn specs only when on."""
    on = {s["name"] for s in build_native_relay_tool_schemas(_no_spawn_spec(), peer_messaging_enabled=True)}
    assert _SEND in on
    assert _CLOSE not in on
    off = {s["name"] for s in build_native_relay_tool_schemas(_no_spawn_spec())}
    assert _SEND not in off
    assert _CLOSE not in off


def test_relay_fallback_follows_the_flag() -> None:
    """The ``spec=None`` fallback carries by-id send only when flagged on."""
    on = {s["name"] for s in build_native_relay_tool_schemas(None, peer_messaging_enabled=True)}
    assert _SEND in on
    assert _CLOSE not in on
    off = {s["name"] for s in build_native_relay_tool_schemas(None)}
    assert _SEND not in off
    assert _CLOSE not in off
