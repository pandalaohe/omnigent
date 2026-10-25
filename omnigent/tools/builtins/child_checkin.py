"""Schema and argument validation for the calling session's check-ins."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from omnigent.tools.base import Tool


def validate_child_checkin_arguments(arguments: Mapping[str, object]) -> str:
    """Reject malformed actions before either tier classification or dispatch."""
    action = arguments.get("action")
    fields = {
        "read": set(),
        "set": {"min_gap_minutes", "coverage", "instructions", "reset", "reset_instructions"},
        "follow_up_add": {"text", "after_minutes"},
        "follow_up_remove": {"id"},
    }
    if not isinstance(action, str) or action not in fields:
        raise ValueError("action must be read, set, follow_up_add or follow_up_remove")
    if set(arguments) - {"action"} - fields[action]:
        raise ValueError(f"Unexpected arguments for {action}")
    for key, minimum, maximum in (
        ("min_gap_minutes", 0, 1440),
        ("after_minutes", 15, 10080),
    ):
        number = arguments.get(key)
        if key in arguments and not (type(number) is int and minimum <= number <= maximum):
            raise ValueError(f"{key} must be an integer between {minimum} and {maximum}")
    for key in ("reset", "reset_instructions"):
        if key in arguments and type(arguments[key]) is not bool:
            raise ValueError(f"{key} must be a boolean")
    if "coverage" in arguments and arguments["coverage"] not in ("changed", "summary"):
        raise ValueError("coverage must be changed or summary")
    for key, maximum, allow_empty in (("instructions", 1000, True), ("text", 200, False)):
        if key not in arguments:
            continue
        value = arguments[key]
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a string")
        value = value.replace("\0", "")
        if (
            len(value) > maximum
            or (not value and not allow_empty)
            or (value and not value.strip())
        ):
            raise ValueError(f"{key} must contain text within {maximum} characters")
    if action == "follow_up_add" and not {"text", "after_minutes"} <= arguments.keys():
        raise ValueError("follow_up_add requires text and after_minutes")
    if action == "follow_up_remove":
        fid = arguments.get("id")
        if not isinstance(fid, str) or re.fullmatch(r"[0-9a-f]{8}", fid) is None:
            raise ValueError("id must be 8 lowercase hexadecimal digits")
    if arguments.get("reset") and {"min_gap_minutes", "coverage"} & arguments.keys():
        raise ValueError("reset cannot be combined with policy values")
    if arguments.get("reset_instructions") and "instructions" in arguments:
        raise ValueError("reset_instructions cannot be combined with instructions")
    return action


class SysChildCheckinTool(Tool):
    """Runner-dispatched check-in controls, bound to the calling session."""

    @classmethod
    def name(cls) -> str:
        return "sys_child_checkin"

    @classmethod
    def description(cls) -> str:
        return (
            "Read or set this session's fallback child check-ins, or add/remove a durable "
            "follow-up. Completion and approval notices are separate. Follow-ups arrive in "
            "the next eligible check-in; read reports when delivery is off or uncertain. "
            "set overrides the check-in gap, coverage or handling instructions for this "
            "session. reset clears its policy override; reset_instructions clears its "
            "instruction override. Empty instructions send no instruction block."
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
                        "action": {
                            "type": "string",
                            "enum": ["read", "set", "follow_up_add", "follow_up_remove"],
                        },
                        "min_gap_minutes": {"type": "integer", "minimum": 0, "maximum": 1440},
                        "coverage": {"type": "string", "enum": ["changed", "summary"]},
                        "instructions": {"type": "string", "maxLength": 1000},
                        "reset": {"type": "boolean"},
                        "reset_instructions": {"type": "boolean"},
                        "text": {"type": "string", "minLength": 1, "maxLength": 200},
                        "after_minutes": {"type": "integer", "minimum": 15, "maximum": 10080},
                        "id": {"type": "string", "pattern": "^[0-9a-f]{8}$"},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
        }
