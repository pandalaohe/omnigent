"""Open a plain top-level session on any host, project and agent."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysSessionOpenTool(Tool):
    """
    Open a plain top-level Omnigent session on another host or project.

    Unlike ``sys_session_create`` (a child in the caller's own project and
    working tree), this opens a **top-level** session in a registered
    project of the caller's user, on any host that project has a directory
    on, with any agent visible to that user. There is no tracking,
    report-back, cancellation or deadline: the session is an ordinary
    conversation the user can watch and drive.

    An optional ``message`` arrives as a peer message from the calling
    session; the new session's replies come back as peer messages, and
    ``sys_session_send`` drives it further.

    ``from_ref`` opens the session in a new branch worktree created from
    that ref on the target host. Commit AND push first — the target host
    must be able to resolve or fetch the ref. A session may not open into
    a directory another session already uses without ``from_ref``.

    ``wait_for_host`` waits for an offline host (up to the
    undelivered-message lifetime setting) and returns a system line when
    the session opens, fails or expires.

    Runner-dispatched: proxies ``POST /v1/sessions/{caller}/open``.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_session_open"``."""
        return "sys_session_open"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Open a plain top-level session on any host, agent and registered "
            "project of your user. No tracking, report-back, cancellation or "
            "deadline; it is an ordinary conversation. An optional message "
            "arrives as a peer message from you, and replies come back as peer "
            "messages (answer with sys_session_send; watch it with the session "
            "tools). from_ref opens it in a new branch worktree created from "
            "that ref on the target host — commit AND push first, because the "
            "target host must be able to resolve or fetch the ref. Opening "
            "into a directory another session already uses needs from_ref. "
            "wait_for_host (default false) waits for an offline host (up to "
            "the undelivered-message lifetime setting) and you get one system "
            "line when it opens, fails or expires. Top-level sessions only. "
            "Every open counts toward the open-rate setting."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a ``"function"``
            sub-dict. ``project`` / ``host`` / ``agent`` are required;
            the rest are optional.
        """
        return {
            "type": "function",
            "function": {
                "name": SysSessionOpenTool.name(),
                "description": SysSessionOpenTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project": {
                            "type": "string",
                            "description": (
                                "Registered project to open the session in, "
                                "by id or name, e.g. 'omnigent'."
                            ),
                        },
                        "host": {
                            "type": "string",
                            "description": (
                                "Host to open the session on, by id or name. "
                                "The project must have a directory on it."
                            ),
                        },
                        "agent": {
                            "type": "string",
                            "description": (
                                "Agent to launch, by name, public name (e.g. "
                                "'claude-native' / 'codex-native') or id."
                            ),
                        },
                        "model": {
                            "type": "string",
                            "description": (
                                "Optional provider-configured model id for "
                                "the new session; omit to use the agent's "
                                "default."
                            ),
                        },
                        "reasoning_effort": {
                            "type": "string",
                            "enum": [
                                "none",
                                "minimal",
                                "low",
                                "medium",
                                "high",
                                "xhigh",
                                "max",
                                "ultra",
                            ],
                            "description": (
                                "Optional reasoning level for the new "
                                "session, e.g. 'high'; omit to use the "
                                "agent's default."
                            ),
                        },
                        "message": {
                            "type": "string",
                            "maxLength": 16000,
                            "description": (
                                "Optional first message, delivered as a peer "
                                "message from you; replies come back as peer "
                                "messages. Omit to open an idle session."
                            ),
                        },
                        "from_ref": {
                            "type": "string",
                            "description": (
                                "Optional branch, tag or commit to create a "
                                "new branch worktree from on the target host. "
                                "Commit AND push it first; the target host "
                                "must resolve or fetch it."
                            ),
                        },
                        "wait_for_host": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "When true, an offline host is waited for "
                                "(up to the undelivered-message lifetime "
                                "setting) instead of refused; you get one "
                                "system line when it opens, fails or expires."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "maxLength": 200,
                            "description": (
                                "Optional human-readable label for the new "
                                "session, e.g. 'auth refactor'."
                            ),
                        },
                    },
                    "required": ["project", "host", "agent"],
                    "additionalProperties": False,
                },
            },
        }
