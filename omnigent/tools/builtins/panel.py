"""Schema-only ``open_in_panel`` builtin tool class.

The class is the **tool surface only** — ``name()``, ``description()`` and
``get_schema()`` — so the tool is advertised to the LLM. Execution lives in
the runner dispatch layer (``omnigent/runner/tool_dispatch.py``): the runner
POSTs to the server's artifact-open route, which needs the runner's
``server_client`` that ``ToolContext`` does not carry. A call that reaches
``Tool.invoke`` here means the tool was misrouted to the server-side path —
the base class raises ``NotImplementedError`` loudly in that case.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class OpenInPanelTool(Tool):
    """Open a workspace file in the user's web preview panel (schema only)."""

    @classmethod
    def name(cls) -> str:
        """:returns: ``"open_in_panel"``."""
        return "open_in_panel"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Open a file in the user's web preview panel. Use a path relative "
            "to the session workspace, or an absolute host path beginning with '/'."
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
                        "path": {
                            "type": "string",
                            "description": (
                                "Workspace-relative file path, or an absolute "
                                "host path beginning with '/'."
                            ),
                        },
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            },
        }
