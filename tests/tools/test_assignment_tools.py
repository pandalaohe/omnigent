"""Tests for the ``sys_assignment_*`` tool surface (scenario 17).

Covers the runner-side registration contract:

- ``ToolManager(..., project_assignments_enabled=True)`` exposes exactly
  the seven assignment tools on top of the default surface; the default
  exposes none of them.
- ``build_native_relay_tool_schemas`` follows the same flag, including
  the ``spec=None`` fallback.
- The seven names are members of the local-dispatch and native-relay
  tool sets, and schema shape follows the scheduled-task precedent
  (``additionalProperties: False`` with explicit ``required`` lists).
- None of the seven occupies the ``_BUILTIN_REGISTRY`` namespace, so a
  spec can never declare them past the flag gate.
"""

from __future__ import annotations

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _ASSIGNMENT_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    build_native_relay_tool_schemas,
)
from omnigent.spec.types import AgentSpec
from omnigent.tools.builtins import BUILTIN_NAMES, INSTANTIABLE_BUILTINS, get_builtin_tool
from omnigent.tools.manager import ToolManager

_SEVEN = {
    "sys_assignment_dispatch",
    "sys_assignment_get",
    "sys_assignment_list",
    "sys_assignment_send",
    "sys_assignment_read_messages",
    "sys_assignment_complete",
    "sys_assignment_cancel",
}


def _schema_names(spec: AgentSpec, **kwargs: object) -> set[str]:
    return {s["function"]["name"] for s in ToolManager(spec, **kwargs).get_tool_schemas()}


def test_flag_on_registers_exactly_the_seven() -> None:
    """The flag adds exactly the seven tools and nothing else."""
    spec = AgentSpec(spec_version=1)
    assert _schema_names(spec, project_assignments_enabled=True) >= _SEVEN
    assert _schema_names(spec, project_assignments_enabled=True) - _schema_names(spec) == _SEVEN


def test_default_registers_none() -> None:
    """Without the flag no assignment tool reaches any agent."""
    assert not (_schema_names(AgentSpec(spec_version=1)) & _SEVEN)


def test_tools_in_dispatch_and_relay_sets() -> None:
    assert _ASSIGNMENT_TOOLS == _SEVEN
    assert _SEVEN <= _ALL_LOCAL_TOOLS
    assert _SEVEN <= _NATIVE_RELAY_BUILTIN_TOOLS


def test_relay_schemas_follow_the_flag() -> None:
    """The native relay surface carries the seven only when flagged on."""
    spec = AgentSpec(spec_version=1)
    on = {
        s["name"] for s in build_native_relay_tool_schemas(spec, project_assignments_enabled=True)
    }
    assert on >= _SEVEN
    off = {s["name"] for s in build_native_relay_tool_schemas(spec)}
    assert not (off & _SEVEN)


def test_relay_fallback_follows_the_flag() -> None:
    """The ``spec=None`` fallback also gates the seven on the flag."""
    on = {
        s["name"] for s in build_native_relay_tool_schemas(None, project_assignments_enabled=True)
    }
    assert on >= _SEVEN
    off = {s["name"] for s in build_native_relay_tool_schemas(None)}
    assert not (off & _SEVEN)


def test_no_registry_entry() -> None:
    """A spec must never declare the tools past the flag gate."""
    for name in _SEVEN:
        assert name not in BUILTIN_NAMES
        assert name not in INSTANTIABLE_BUILTINS
        assert get_builtin_tool(name) is None


def test_schemas_are_closed_objects_with_required_lists() -> None:
    """Every tool schema forbids undeclared properties and names its required args."""
    from omnigent.tools.builtins.assignments import (
        SysAssignmentCancelTool,
        SysAssignmentCompleteTool,
        SysAssignmentDispatchTool,
        SysAssignmentGetTool,
        SysAssignmentListTool,
        SysAssignmentReadMessagesTool,
        SysAssignmentSendTool,
    )

    expected_required = {
        SysAssignmentDispatchTool: {"target_agent_id", "task", "repositories", "idempotency_key"},
        SysAssignmentGetTool: {"assignment_id"},
        SysAssignmentListTool: set(),
        SysAssignmentSendTool: {"assignment_id", "body", "idempotency_key"},
        SysAssignmentReadMessagesTool: {"assignment_id"},
        SysAssignmentCompleteTool: {"assignment_id", "outputs", "summary"},
        SysAssignmentCancelTool: {"assignment_id"},
    }
    for cls, required in expected_required.items():
        params = cls().get_schema()["function"]["parameters"]
        assert params["type"] == "object"
        assert params["additionalProperties"] is False
        assert set(params["required"]) == required


def test_descriptions_state_the_commit_rule() -> None:
    """Dispatch/complete tell the model only committed work is sent."""
    from omnigent.tools.builtins.assignments import (
        SysAssignmentCompleteTool,
        SysAssignmentDispatchTool,
    )

    for cls in (SysAssignmentDispatchTool, SysAssignmentCompleteTool):
        description = cls.description()
        assert "ommitted" in description
        assert "nothing is committed for you" in description
