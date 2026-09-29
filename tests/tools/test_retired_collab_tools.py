"""Retirement sweep: no retired collaboration tool is ever advertised.

Hand-off (S1) and assignment (S2) tools are gone from the codebase; this
sweep pins that no ``ToolManager`` surface and no native relay schema
resurrects a name under any flag combination.
"""

from __future__ import annotations

import pytest

from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas
from omnigent.spec.types import AgentSpec
from omnigent.tools.manager import ToolManager

_RETIRED_EXACT = ("sys_session_handoff", "sys_handoff_report")
_RETIRED_PREFIX = "sys_assignment_"


def _assert_clean(names: set[str]) -> None:
    for retired in _RETIRED_EXACT:
        assert retired not in names
    stale = sorted(name for name in names if name.startswith(_RETIRED_PREFIX))
    assert not stale, stale


def _manager_names(spec: AgentSpec, **kwargs: object) -> set[str]:
    manager = ToolManager(spec, **kwargs)  # type: ignore[arg-type]
    try:
        return set(manager.get_tool_names())
    finally:
        manager.shutdown()


@pytest.mark.parametrize("spawn", [False, True])
@pytest.mark.parametrize("peer_messaging_enabled", [False, True])
@pytest.mark.parametrize("session_open_enabled", [False, True])
def test_no_retired_tool_in_any_surface(
    spawn: bool, peer_messaging_enabled: bool, session_open_enabled: bool
) -> None:
    """Every flag combination and spec shape advertises no retired tool."""
    kwargs = {
        "peer_messaging_enabled": peer_messaging_enabled,
        "session_open_enabled": session_open_enabled,
    }
    spec = AgentSpec(spec_version=1, spawn=spawn)
    _assert_clean(_manager_names(spec, **kwargs))
    _assert_clean({s["name"] for s in build_native_relay_tool_schemas(spec, **kwargs)})
    _assert_clean({s["name"] for s in build_native_relay_tool_schemas(None, **kwargs)})
