"""Schema for answering a server-offered, single-use child approval."""

from typing import Any

from omnigent.tools.base import Tool


class SysApprovalAnswerTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_approval_answer"

    @classmethod
    def description(cls) -> str:
        return (
            "Answer an approval offered to this parent session. Approve or decline only "
            "what you are already authorized to decide; give the authority in reason. "
            "Approvals reserved for the user cannot be approved or declined here. "
            "Otherwise pass_to_user with note: one plain sentence in the user's language "
            "explaining what the child wants and why the user must decide, and recommend: "
            "approve or decline. An answer is single-use, logged and attributed to this "
            "session. A passed card cannot be taken back."
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
                        "session_id": {"type": "string", "minLength": 1},
                        "elicitation_id": {"type": "string", "minLength": 1},
                        "decision": {
                            "type": "string",
                            "enum": ["approve", "decline", "pass_to_user"],
                        },
                        "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                        "note": {"type": "string", "minLength": 1, "maxLength": 500},
                        "recommend": {"type": "string", "enum": ["approve", "decline"]},
                    },
                    "required": ["session_id", "elicitation_id", "decision", "reason"],
                    "additionalProperties": False,
                },
            },
        }
