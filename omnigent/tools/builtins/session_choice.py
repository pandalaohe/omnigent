"""Schema-only tools for owner session selection and quota reserve controls."""

from typing import Any

from omnigent.tools.base import Tool


def choice_arguments() -> dict[str, Any]:
    return {
        "work_type": {
            "type": "string",
            "description": "Work type from the session choice table; omitted uses its default.",
        },
        "user_named": {
            "type": "boolean",
            "description": "True if the user explicitly named this agent; bypasses selection.",
        },
        "choice_reason": {
            "type": "string",
            "description": "Why this named agent is required; exhaustion and max running apply.",
        },
        "exclude": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Pools to skip after a failed child; starts their ten-minute cooldown.",
        },
        "differ_from": {
            "type": "string",
            "description": "Session whose model family this new session must differ from.",
        },
    }


class _ChoiceTool(Tool):
    tool_name = ""
    tool_description = ""
    properties: dict[str, Any] = {}
    required: list[str] = []

    @classmethod
    def name(cls) -> str:
        return cls.tool_name

    @classmethod
    def description(cls) -> str:
        return cls.tool_description

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": self.required,
                    "additionalProperties": False,
                },
            },
        }


class SysSessionChoiceReadTool(_ChoiceTool):
    tool_name = "sys_session_choice_read"
    tool_description = (
        "Read your session choice document and revision: work types, per-host tiers, "
        "preset fallback chains (including optional per-link permissionTier), "
        "explicit single-scope and multi-member Agent/Host/Model limit rules, legacy pool limits, "
        "ability order, supported document capabilities, and active drain locks."
    )


class SysSessionChoiceUpdateTool(_ChoiceTool):
    tool_name = "sys_session_choice_update"
    tool_description = (
        "Replace the session choice document after reading it. Pass expected_revision; "
        "stale saves return 409. Null clears the document. Requires user approval by default. "
        "In schemaVersion 2, each limitRules entry accepts either single-scope "
        "{id, agent, host?, model?, maxRunning?, reserve?, reserveExempt?} or multi-member "
        "{id, members: [{agent, host?, model?}, ...], maxRunning?} (1..n exact scopes; "
        "maxRunning caps the summed running sessions across all members; every matching cap "
        "holds and the tightest decides; reserve and reserveExempt are refused on multi-member "
        "rules). Any preset or row link may also carry optional permissionTier "
        "(ask | plan | accept-edits | auto), used when sys_session_create omits permission_tier "
        "and validated like an explicit tier (absent uses the host default)."
    )
    properties = {
        "settings": {
            "type": ["object", "null"],
            "additionalProperties": True,
            "description": (
                "Complete session choice document, or null to clear. Supports single-scope or "
                "multi-member limitRules (members: 1..n {agent, host?, model?} scopes with a "
                "summed maxRunning cap) and optional link permissionTier "
                "(ask, plan, accept-edits, auto)."
            ),
        },
        "expected_revision": {"type": "string"},
        "conversion_proof": {
            "type": "object",
            "description": (
                "Optional metadata from a fresh server conversion preview; "
                "never authorizes a write."
            ),
            "properties": {
                "source_revision": {"type": "string"},
                "mapping_fingerprint": {"type": "string", "minLength": 1},
                "replaced_paths": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "array", "items": {"type": ["string", "integer"]}},
                },
                "new_rule_ids": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            },
            "required": [
                "source_revision",
                "mapping_fingerprint",
                "replaced_paths",
                "new_rule_ids",
            ],
            "additionalProperties": False,
        },
    }
    required = ["settings", "expected_revision"]


class SysQuotaFloorTool(_ChoiceTool):
    tool_name = "sys_quota_floor"
    tool_description = (
        "Keep a tile's reserve (off=false), turn it off until the first reset (off=true), "
        "or use up until switched off (keep=true). Never bypasses exhaustion or max running."
    )
    properties = {
        "tile": {"type": "string"},
        "off": {"type": "boolean"},
        "keep": {"type": "boolean"},
    }
    required = ["tile", "off"]
