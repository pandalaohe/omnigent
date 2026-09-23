"""Built-in tools for cross-host project assignments (handoff).

One assignment hands a unit of work to a named ``(host, agent)``
destination. Artifacts move by git — the server stores pointers,
addressing and state, never file content. These tools let an agent
dispatch work, follow it, and report back. The runner dispatches each
to the Omnigent server's ``/v1/assignments`` REST endpoints (same
posture as the scheduled-task tools) — the runner has no in-process
store.

* ``sys_assignment_dispatch`` — hand work to a named agent on a host.
* ``sys_assignment_get`` — read one assignment.
* ``sys_assignment_list`` — list sent / received assignments.
* ``sys_assignment_send`` — append a note to an assignment.
* ``sys_assignment_read_messages`` — cursor-read an assignment's messages.
* ``sys_assignment_complete`` — report the work done (receiving side).
* ``sys_assignment_cancel`` — request cancellation.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysAssignmentGetTool(Tool):
    """Read one assignment. Runner-dispatched to ``GET /v1/assignments/{id}``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_assignment_get"``."""
        return "sys_assignment_get"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return "Read one project assignment you own: its state, task, pinned inputs and outputs."

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "assignment_id": {
                            "type": "string",
                            "description": "The assignment to read.",
                        },
                    },
                    "required": ["assignment_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysAssignmentListTool(Tool):
    """List assignments. Runner-dispatched to ``GET /v1/assignments``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_assignment_list"``."""
        return "sys_assignment_list"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List project assignments you own. Use role 'sent' for work this "
            "session dispatched, or role 'received' for work dispatched to "
            "this session."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "description": (
                                "Only assignments in this state, e.g. 'waiting' or 'running'."
                            ),
                        },
                        "role": {
                            "type": "string",
                            "description": "'sent' (this session dispatched) or 'received'.",
                        },
                        "after": {
                            "type": "string",
                            "description": "Cursor id — return assignments after this one.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum assignments in the page (at most 100).",
                        },
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
        }


class SysAssignmentSendTool(Tool):
    """Append a note. Runner-dispatched to ``POST /v1/assignments/{id}/messages``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_assignment_send"``."""
        return "sys_assignment_send"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return "Append a progress note to a project assignment, readable from either side."

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "assignment_id": {
                            "type": "string",
                            "description": "The assignment to message.",
                        },
                        "body": {
                            "type": "string",
                            "description": "The note text.",
                        },
                        "idempotency_key": {
                            "type": "string",
                            "description": ("Caller key so a retried send appends exactly once."),
                        },
                    },
                    "required": ["assignment_id", "body", "idempotency_key"],
                    "additionalProperties": False,
                },
            },
        }


class SysAssignmentReadMessagesTool(Tool):
    """Cursor-read messages. Runner-dispatched to ``GET /v1/assignments/{id}/messages``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_assignment_read_messages"``."""
        return "sys_assignment_read_messages"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return "Read a project assignment's notes and state events. Reading never consumes."

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "assignment_id": {
                            "type": "string",
                            "description": "The assignment whose messages to read.",
                        },
                        "after": {
                            "type": "string",
                            "description": "Cursor id — return messages after this one.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum messages in the page (at most 100).",
                        },
                    },
                    "required": ["assignment_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysAssignmentCancelTool(Tool):
    """Request cancellation. Runner-dispatched to ``POST /v1/assignments/{id}/cancel``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_assignment_cancel"``."""
        return "sys_assignment_cancel"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Request cancellation of a project assignment. Cancelling executing "
            "work is a request — it is reported when it actually stops."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "assignment_id": {
                            "type": "string",
                            "description": "The assignment to cancel.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "Optional human-readable reason.",
                        },
                    },
                    "required": ["assignment_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysAssignmentDispatchTool(Tool):
    """Hand work to a named agent. Runs ``POST /v1/assignments`` + git + ``/published``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_assignment_dispatch"``."""
        return "sys_assignment_dispatch"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Hand a unit of work to a named agent on a host: the receiving "
            "session auto-starts in a prepared worktree and reports back. The "
            "assignment is filed under this session's project. Only committed "
            "work is sent — commit first; nothing is committed for you."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "target_agent_id": {
                            "type": "string",
                            "description": "The agent to run on arrival, e.g. 'ag_abc123'.",
                        },
                        "task": {
                            "type": "string",
                            "description": "The natural-language instruction for the receiver.",
                        },
                        "repositories": {
                            "type": "array",
                            "description": (
                                "Pinned input repositories: each names a repository "
                                "registered on this session's project and the commit "
                                "to hand off."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "repository_name": {
                                        "type": "string",
                                        "description": "A repository registered on the project.",
                                    },
                                    "commit": {
                                        "type": "string",
                                        "description": (
                                            "The pinned commit (full hex sha). Must already "
                                            "be committed locally."
                                        ),
                                    },
                                    "artifact_paths": {
                                        "type": "array",
                                        "description": (
                                            "Repo-relative paths the receiver materializes."
                                        ),
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["repository_name", "commit"],
                                "additionalProperties": False,
                            },
                        },
                        "idempotency_key": {
                            "type": "string",
                            "description": (
                                "Caller key so a retried dispatch reuses the assignment."
                            ),
                        },
                        "execution_root": {
                            "type": "string",
                            "description": (
                                "Which dispatched repository the session workspace is "
                                "prepared from. Required with more than one repository."
                            ),
                        },
                        "host_id": {
                            "type": "string",
                            "description": (
                                "The destination host. Omit to resolve to the owner's "
                                "freshest eligible online host."
                            ),
                        },
                        "binding_name": {
                            "type": "string",
                            "description": (
                                "Omit this or pass 'primary' to use the destination host's "
                                "primary binding, whatever its name. Any other value names "
                                "a binding exactly."
                            ),
                        },
                        "model_override": {
                            "type": "string",
                            "description": "Optional per-assignment model override.",
                        },
                        "harness_override": {
                            "type": "string",
                            "description": "Optional per-assignment harness override.",
                        },
                        "start_deadline": {
                            "type": "integer",
                            "description": (
                                "Unix epoch seconds bounding the wait. Omit to wait "
                                "until cancelled."
                            ),
                        },
                    },
                    "required": ["target_agent_id", "task", "repositories", "idempotency_key"],
                    "additionalProperties": False,
                },
            },
        }


class SysAssignmentCompleteTool(Tool):
    """Report work done. Runs ``POST /v1/assignments/{id}/complete`` + git + ``/finish``."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_assignment_complete"``."""
        return "sys_assignment_complete"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Report a received assignment's work complete: commit the work in "
            "the prepared directories first, then call with each changed "
            "repository and a summary. Only committed work is sent — nothing "
            "is committed for you. Completing is the last work step: dispatch "
            "any onward assignment with sys_assignment_dispatch before it. The "
            "session is closed after this turn ends."
        )

    def get_schema(self) -> dict[str, Any]:
        """:returns: The OpenAI-format tool schema."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "assignment_id": {
                            "type": "string",
                            "description": "The assignment being completed.",
                        },
                        "outputs": {
                            "type": "array",
                            "description": "One entry per repository changed.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "repository_name": {
                                        "type": "string",
                                        "description": "A repository from the assignment inputs.",
                                    },
                                    "commit": {
                                        "type": "string",
                                        "description": (
                                            "The output commit (full hex sha). Must already "
                                            "be committed locally."
                                        ),
                                    },
                                    "artifact_paths": {
                                        "type": "array",
                                        "description": "Repo-relative paths produced.",
                                        "items": {"type": "string"},
                                    },
                                },
                                "required": ["repository_name", "commit"],
                                "additionalProperties": False,
                            },
                        },
                        "summary": {
                            "type": "string",
                            "description": "Human-readable outcome summary.",
                        },
                    },
                    "required": ["assignment_id", "outputs", "summary"],
                    "additionalProperties": False,
                },
            },
        }
