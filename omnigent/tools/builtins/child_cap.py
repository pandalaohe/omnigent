"""Schema for reading or setting the current session's child cap."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysChildCapTool(Tool):
    """Runner-dispatched child-cap tool."""

    @classmethod
    def name(cls) -> str:
        return "sys_child_cap"

    @classmethod
    def description(cls) -> str:
        return (
            "Read the current session's child-session limit and open-child count. "
            "Supply cap as a positive integer or decimal string to set a positive "
            "limit, or 'none' to explicitly disable "
            "the limit for this session. Omit cap to read without changing it."
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
                        "cap": {
                            "oneOf": [
                                {"type": "integer", "minimum": 1},
                                {"type": "string", "pattern": r"^[1-9][0-9]*(?![\s\S])"},
                                {"type": "string", "enum": ["none"]},
                            ],
                            "description": "New limit; omit to read the effective cap.",
                        }
                    },
                    "additionalProperties": False,
                },
            },
        }
