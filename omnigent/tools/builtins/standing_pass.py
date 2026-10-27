"""Schema for a mother session's advisory standing pass."""

from typing import Any

from omnigent.tools.base import Tool


class SysStandingPassTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_standing_pass"

    @classmethod
    def description(cls) -> str:
        return (
            "Read the live standing pass before a pass-kind action. Children can only read. "
            "The mother can revoke kinds, optionally citing a user message as source. "
            "Granting kinds always requires user approval of that exact delta. "
            "The pass never overrides safety rules or other approval gates."
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
                        "op": {"type": "string", "enum": ["read", "revoke", "grant"]},
                        "kinds": {
                            "type": "array",
                            "maxItems": 32,
                            "items": {"type": "string", "minLength": 1, "maxLength": 120},
                        },
                        "source": {"type": "string", "maxLength": 256},
                    },
                    "required": ["op"],
                    "additionalProperties": False,
                },
            },
        }
