"""Tools for archiving and unarchiving sessions of the calling user."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysSessionArchiveTool(Tool):
    """Schema-only tool that archives a session the caller's user owns."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_session_archive"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Archive a session owned by your user (any host), exactly like the web "
            "Archive action: it leaves the default session list and, per its host's "
            "'stop runner on archive' setting, its runner is stopped after an 8-second "
            "undo window — a session with a parent or sub-sessions first finishes its "
            "current turn (its whole tree, at most 1 hour), and any other session's "
            "running turn is interrupted. Omit session_id to "
            "archive the session you are running in; its runner then stops only after "
            "your current turn ends (normally), so finish your reply in this turn. "
            "Archiving also archives its active descendants. Each archived session's "
            "worktree follows your account's archive deletion tier and safety checks; "
            "uncommitted source changes and branches are kept. Undo restores only "
            "the descendants archived by this action, using sys_session_unarchive "
            "or the web Archive list. "
            "Returns access_denied for a session your user does not own."
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
                        "session_id": {
                            "type": "string",
                            "description": (
                                "Session to archive, e.g. from sys_session_list. "
                                "Omit to archive your own session."
                            ),
                        }
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }


class SysSessionUnarchiveTool(Tool):
    """Schema-only tool that unarchives a session the caller's user owns."""

    @classmethod
    def name(cls) -> str:
        """Return the tool name."""
        return "sys_session_unarchive"

    @classmethod
    def description(cls) -> str:
        """Return the LLM-facing description."""
        return (
            "Unarchive a session owned by your user, exactly like the web Unarchive "
            "action: it returns to the default session list and a pending archive "
            "teardown is cancelled. It does not send anything; the next message to "
            "the session restarts it as usual. Find archived sessions with "
            "sys_session_list archived='only'. May be refused by the session restriction setting. "
            "If refused as a sub-agent, report the need to your parent in your final output. "
            "If all agent sessions are restricted, open a new session with a handover instead."
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
                        "session_id": {
                            "type": "string",
                            "description": "Archived session to restore.",
                        }
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
            },
        }
