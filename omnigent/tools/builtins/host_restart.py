"""Schema for owner-approved supervised host restart."""

from typing import Any

from omnigent.tools.base import Tool


class SysHostRestartTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_host_restart"

    @classmethod
    def description(cls) -> str:
        return (
            "Preview or restart an online host you own, by exact name or id. "
            "dry_run defaults true and returns a harmless preview valid for 120 seconds. "
            "A real restart always requires the application's approval card; it previews "
            "automatically when preview_id is omitted. The card lists affected sessions. "
            "Restart ends every runner and terminal on that host, including this session "
            "when it runs there. The result includes a request_id for restart status, "
            "and a message wakes this session after reconnect or failure. "
            "Hosts without a supervisor are refused; offline hosts cannot be started."
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
                        "host": {"type": "string", "minLength": 1},
                        "dry_run": {"type": "boolean", "default": True},
                        "force": {"type": "boolean", "default": False},
                        "preview_id": {"type": "string"},
                    },
                    "required": ["host"],
                    "additionalProperties": False,
                },
            },
        }
