"""Schema-only tools for owner session selection and quota reserve controls."""

from typing import Any

from omnigent.tools.base import Tool


def choice_arguments() -> dict[str, Any]:
    return {
        "work_type": {
            "type": "string",
            "description": "Work type from the session choice table; omitted uses its default.",
        },
        "user_named": {
            "type": "boolean",
            "description": "True if the user explicitly named this agent; bypasses selection.",
        },
        "choice_reason": {
            "type": "string",
            "description": "Why this named agent is required; exhaustion and max running apply.",
        },
        "exclude": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Pools to skip after a failed child; starts their ten-minute cooldown.",
        },
        "differ_from": {
            "type": "string",
            "description": "Session whose model family this new session must differ from.",
        },
    }


class _ChoiceTool(Tool):
    tool_name = ""
    tool_description = ""
    properties: dict[str, Any] = {}
    required: list[str] = []

    @classmethod
    def name(cls) -> str:
        return cls.tool_name

    @classmethod
    def description(cls) -> str:
        return cls.tool_description

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": self.required,
                    "additionalProperties": False,
                },
            },
        }


class SysSessionChoiceReadTool(_ChoiceTool):
    tool_name = "sys_session_choice_read"
    tool_description = (
        "Read your session choice document and revision: work types, per-host tiers, "
        "preset fallback chains, limits and ability order."
    )


class SysSessionChoiceUpdateTool(_ChoiceTool):
    tool_name = "sys_session_choice_update"
    tool_description = (
        "Replace the session choice document after reading it. Pass expected_revision; "
        "stale saves return 409. Null clears the document. Requires user approval by default."
    )
    properties = {
        "settings": {"type": ["object", "null"], "additionalProperties": True},
        "expected_revision": {"type": "string"},
    }
    required = ["settings", "expected_revision"]


class SysQuotaFloorTool(_ChoiceTool):
    tool_name = "sys_quota_floor"
    tool_description = (
        "Keep a tile's reserve (off=false), turn it off until the first reset (off=true), "
        "or use up until switched off (keep=true). Never bypasses exhaustion or max running."
    )
    properties = {
        "tile": {"type": "string"},
        "off": {"type": "boolean"},
        "keep": {"type": "boolean"},
    }
    required = ["tile", "off"]
