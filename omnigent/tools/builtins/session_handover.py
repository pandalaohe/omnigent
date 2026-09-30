"""Tool for recording a handover note and requesting a session rotation."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysSessionHandoverTool(Tool):
    """Schema-only tool that records a handover for the calling session."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_session_handover"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Record a handover note for the session that continues this one. "
            "With rotate=true (default), this top-level native session is cleared at "
            "the end of the current turn and continues in a new session that receives "
            "the note and this session's live sub-agents. Sub-agent sessions cannot "
            "call it."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI-format schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "handover": {
                            "type": "string",
                            "description": "The handover note the successor session opens with.",
                            "minLength": 1,
                        },
                        "rotate": {
                            "type": "boolean",
                            "description": (
                                "Clear this session at the end of the current turn and "
                                "continue in a new session (default true)."
                            ),
                            "default": True,
                        },
                    },
                    "required": ["handover"],
                    "additionalProperties": False,
                },
            },
        }
