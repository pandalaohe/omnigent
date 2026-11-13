"""Schema for a parent session's advisory standing approval."""

from typing import Any

from omnigent.tools.base import Tool


class SysStandingApprovalTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_standing_approval"

    @classmethod
    def description(cls) -> str:
        return (
            "Use sys_standing_approval_read for a read without native approval. "
            "Read the live standing approval before an approval-kind action. "
            "Children can only read. "
            "The parent can revoke kinds, optionally citing a user message as source. "
            "Granting kinds always requires user approval of that exact delta. "
            "Standing approval never overrides safety rules. With answer-child-approvals, "
            "the server may offer single-use child approval gates to the parent through "
            "sys_approval_answer; other approval gates still apply."
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


class SysStandingApprovalReadTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_standing_approval_read"

    @classmethod
    def description(cls) -> str:
        return "Read live standing approval. This read-only tool cannot grant or revoke approval."

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        }
