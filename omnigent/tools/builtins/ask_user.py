"""Schema-only ``ask_user_async`` builtin tool class.

The class is the **tool surface only** — ``name()``, ``description()`` and
``get_schema()`` — so the tool is advertised to the LLM. Execution lives in
the runner dispatch layer (``omnigent/runner/tool_dispatch.py``): the runner
POSTs to the server's ``async-questions`` route, which needs the runner's
``server_client`` that ``ToolContext`` does not carry. A call that reaches
``Tool.invoke`` here means the tool was misrouted to the server-side path —
the base class raises ``NotImplementedError`` loudly in that case.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class AskUserAsyncTool(Tool):
    """Ask the user questions without blocking the session (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"ask_user_async"``."""
        return "ask_user_async"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Ask the user one or more questions on a card. This tool returns "
            "immediately and does not block the session: the user's answers "
            "arrive later as a new user message. Use it when the session must "
            "keep working while the user decides, or when the user is away. "
            "Put any context the user needs (report paths, links, which "
            "session or file the question is about) in ``context``. Each "
            "question may offer selectable ``options``; without options it is "
            "answered as free text."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict.
        """
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "questions": {
                            "type": "array",
                            "minItems": 1,
                            "description": "The questions to ask, in order.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "question": {
                                        "type": "string",
                                        "description": "The question text.",
                                    },
                                    "header": {
                                        "type": "string",
                                        "description": ("Short label shown above the question."),
                                    },
                                    "options": {
                                        "type": "array",
                                        "description": (
                                            "Selectable answers. Omit or leave "
                                            "empty for a free-text question."
                                        ),
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "label": {
                                                    "type": "string",
                                                    "description": "The option label.",
                                                },
                                                "description": {
                                                    "type": "string",
                                                    "description": (
                                                        "Optional explanation of the option."
                                                    ),
                                                },
                                            },
                                            "required": ["label"],
                                            "additionalProperties": False,
                                        },
                                    },
                                    "multiSelect": {
                                        "type": "boolean",
                                        "description": (
                                            "When true the user may select more than one option."
                                        ),
                                    },
                                },
                                "required": ["question"],
                                "additionalProperties": False,
                            },
                        },
                        "context": {
                            "type": "string",
                            "description": (
                                "Optional free markdown shown above the "
                                "questions, e.g. links or paths the user needs."
                            ),
                        },
                    },
                    "required": ["questions"],
                    "additionalProperties": False,
                },
            },
        }
