"""Schema-only instruction tools; the runner proxies the settings API."""

from typing import Any

from omnigent.tools.base import Tool

_LAYERS = (
    "shared applies to all routes; agent to one coding-agent product; host to one host; "
    "independent (cell) replaces shared/agent/host for a host + agent pair; "
    "mother applies to top-level sessions that can spawn; child to child sessions. "
)
_KEYS: dict[str, Any] = {
    "scope": {
        "type": "string",
        "enum": ["shared", "agent", "host", "independent", "mother", "child"],
    },
    "agent_key": {"type": "string", "description": "Required for agent and independent scopes."},
    "host_id": {"type": "string", "description": "Required for host and independent scopes."},
}


class SysInstructionLayersReadTool(Tool):
    """Read settings layers, one route preview, or revision history."""

    @classmethod
    def name(cls) -> str:
        return "sys_instruction_layers_read"

    @classmethod
    def description(cls) -> str:
        return (
            "Read Global Instructions: mode=layers lists saved text and revision ids (an absent "
            "layer has no revision); mode=preview returns composed text and source sections for "
            "host_id + agent_key, or session_id; mode=revisions returns one layer's history. "
            + _LAYERS
            + "Changes apply to sessions started or relaunched after saving; active terminals "
            "keep their startup instructions. Revision history requires deployment admin access."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "enum": ["layers", "preview", "revisions"]},
                        **_KEYS,
                        "session_id": {"type": "string"},
                        "child": {
                            "type": "boolean",
                            "description": "Preview a child session (default false).",
                        },
                        "spawn": {
                            "type": "boolean",
                            "description": "Preview spawn capability; default from the agent.",
                        },
                        "peer": {
                            "type": "boolean",
                            "description": "Preview peer messaging (default true).",
                        },
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                    "required": ["mode"],
                    "additionalProperties": False,
                },
            },
        }


class SysInstructionLayerUpdateTool(Tool):
    """Replace exactly one layer with an optimistic revision guard."""

    @classmethod
    def name(cls) -> str:
        return "sys_instruction_layer_update"

    @classmethod
    def description(cls) -> str:
        return (
            "Replace one Global Instructions layer (empty text disables it). "
            + _LAYERS
            + "Read first with sys_instruction_layers_read; pass its revision_id as "
            "expected_revision_id, or null for an unsaved layer. Stale revisions are refused. "
            "Requires deployment admin access and user tool approval by default. Admins can "
            "manage the Global Instructions update approval policy in existing policy settings. "
            "Returns the new revision and newly over-budget route warnings. Changes apply to "
            "sessions started or relaunched after saving; active terminals keep their "
            "startup instructions."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        **_KEYS,
                        "text": {"type": "string"},
                        "expected_revision_id": {"type": ["string", "null"]},
                    },
                    "required": ["scope", "text", "expected_revision_id"],
                    "additionalProperties": False,
                },
            },
        }
