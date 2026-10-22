"""Schema-only tools for one project's future-session defaults."""

from typing import Any

from omnigent.tools.base import Tool


class SysProjectSettingsReadTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_project_settings_read"

    @classmethod
    def description(cls) -> str:
        return (
            "Read one project's saved and effective session defaults, their sources, revision, "
            "available host/harness/model/mode choices, and catalog freshness. Use this before "
            "setting up project defaults. Reads do not run entry templates or setup hooks. "
            "Defaults apply to future sessions; existing sessions keep their current settings."
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
                        "project_id": {"type": "string"},
                        "host_id": {"type": "string"},
                        "harness": {"type": "string"},
                    },
                    "required": ["project_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysProjectSettingsUpdateTool(Tool):
    @classmethod
    def name(cls) -> str:
        return "sys_project_settings_update"

    @classmethod
    def description(cls) -> str:
        return (
            "Set one project's future-session defaults after sys_project_settings_read. Pass its "
            "revision as expected_revision. Name leaves to change: project host_id, agent_id, "
            "model; or a host's workspace, agent_id and harness model, effort, speed, permission. "
            "Null removes only that override. A new entry may initialize its Agent from the "
            "owner's template; an online entry change runs the setup hook after saving. Hook "
            "failure is a persisted-settings warning; unchanged entries do not rerun hooks. "
            "Requires owner or admin access and user approval by default. Existing sessions "
            "are unchanged; permission defaults cannot change this call's approval."
        )

    def get_schema(self) -> dict[str, Any]:
        nullable = {"type": ["string", "null"]}
        harness_patch = {
            "type": "object",
            "properties": dict.fromkeys(("model", "effort", "speed", "permission"), nullable),
            "additionalProperties": False,
        }
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "project_id": {"type": "string"},
                        "expected_revision": {"type": "string"},
                        "project": {
                            "type": "object",
                            "properties": {
                                "host_id": nullable,
                                "agent_id": nullable,
                                "model": nullable,
                            },
                            "additionalProperties": False,
                        },
                        "host": {
                            "type": "object",
                            "properties": {
                                "host_id": {"type": "string"},
                                "workspace": nullable,
                                "agent_id": nullable,
                                "harnesses": {
                                    "type": "object",
                                    "additionalProperties": harness_patch,
                                },
                            },
                            "required": ["host_id"],
                            "additionalProperties": False,
                        },
                    },
                    "required": ["project_id", "expected_revision"],
                    "additionalProperties": False,
                },
            },
        }
