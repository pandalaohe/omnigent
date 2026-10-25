"""Schema for reading every visible host's current scheduling load."""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysHostLoadTool(Tool):
    """Runner-dispatched, read-only host-load tool."""

    @classmethod
    def name(cls) -> str:
        return "sys_host_load"

    @classmethod
    def description(cls) -> str:
        return (
            "Read current load and scheduling advice for every host visible to you. "
            "Returns host identity, connection state, cores, load per core, load level "
            "and reason, effective heavy-work and new-session lines, memory use, "
            "queued children, and sampling time. Spare hosts have capacity for more "
            "work; heavy_hold means wait before new heavy work; new_session_hold "
            "also means defer new child sessions. Unknown levels mean telemetry is "
            "unavailable or stale, not confirmed spare capacity. Does not change settings."
        )

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
