"""Agent-facing schemas for project session hand-offs."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysSessionHandoffTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_session_handoff"

    @classmethod
    def description(cls) -> str:
        return (
            "Hand a bounded task to a top-level session in a registered project's own directory "
            "or a branch worktree, never your current directory. The receiver sees the request "
            "as coming from this session, not from the user. The result arrives later as a "
            "message. If a start call errors, call status without a handoff_id before retrying: "
            "the start may have succeeded. Poll only when your own work cannot continue."
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
                            "enum": ["start", "status", "cancel"],
                            "default": "start",
                        },
                        "project": {"type": "string", "minLength": 1},
                        "task": {"type": "string", "minLength": 1, "maxLength": 8000},
                        "constraints": {"type": "string"},
                        "expected_outcome": {"type": "string"},
                        "artifacts": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 500},
                            "maxItems": 20,
                        },
                        "branch": {"type": "string"},
                        "existing_branch": {"type": "boolean", "default": False},
                        "base_branch": {"type": "string"},
                        "agent": {"type": "string"},
                        "session": {"type": "string"},
                        "host": {"type": "string"},
                        "lifetime_minutes": {
                            "type": "integer",
                            "minimum": 5,
                            "maximum": 4320,
                            "default": 1440,
                        },
                        "allow_onward": {"type": "boolean", "default": False},
                        "handoff_id": {"type": "string", "minLength": 1},
                    },
                    "allOf": [
                        {
                            "if": {"properties": {"action": {"const": "start"}}},
                            "then": {"required": ["project", "task"]},
                        },
                        {
                            "if": {
                                "properties": {"action": {"const": "cancel"}},
                                "required": ["action"],
                            },
                            "then": {"required": ["handoff_id"]},
                        },
                    ],
                    "additionalProperties": False,
                },
            },
        }


class SysHandoffReportTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_handoff_report"

    @classmethod
    def description(cls) -> str:
        return (
            "Call once when the hand-off's task ends, with its status and "
            "what is done and not done."
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
                        "handoff_id": {"type": "string", "minLength": 1},
                        "status": {
                            "type": "string",
                            "enum": ["completed", "incomplete", "failed"],
                        },
                        "summary": {"type": "string", "maxLength": 4000},
                        "done": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 500},
                            "maxItems": 50,
                        },
                        "not_done": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 500},
                            "maxItems": 50,
                        },
                        "artifacts": {
                            "type": "array",
                            "items": {"type": "string", "maxLength": 500},
                            "maxItems": 50,
                        },
                    },
                    "required": ["handoff_id", "status", "summary"],
                    "additionalProperties": False,
                },
            },
        }
