"""Declared session modes and their launch-time application."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from omnigent.codex_approval_modes import CODEX_NATIVE_PERMISSION_PRESETS
from omnigent.harness_capabilities import ModeContext, ModeOption, ModeSet, SessionModes
from omnigent.permission_tiers import NON_FULL_TIERS
from omnigent.sdk_permission_modes import (
    CLAUDE_SDK_PERMISSION_MODE_LABEL_KEY,
    CODEX_SDK_APPROVAL_MODE_LABEL_KEY,
)

SPEED_TIER_LABEL_KEY = "omnigent.speed_tier"
SPEED_TIER_REQUESTED_LABEL_KEY = "omnigent.speed_tier_requested"
SPEED_TIER_VALUES = frozenset({"standard", "fast"})
_SERVICE_TIER_ID = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_BOTH: frozenset[ModeContext] = frozenset({"default", "create"})
_CREATE: frozenset[ModeContext] = frozenset({"create"})


@dataclass(frozen=True)
class ModeApplication:
    args: tuple[str, ...] = ()
    labels: dict[str, str] = field(default_factory=dict)
    tier: str | None = None


ModeHandler = Callable[[str, str], ModeApplication]


def _claude_permission(harness: str, value: str) -> ModeApplication:
    if harness == "claude-sdk":
        return ModeApplication(labels={CLAUDE_SDK_PERMISSION_MODE_LABEL_KEY: value})
    return ModeApplication(args=("--permission-mode", value))


def _codex_native_permission(_harness: str, value: str) -> ModeApplication:
    return ModeApplication(args=tuple(CODEX_NATIVE_PERMISSION_DEFAULT_ARGS[value]))


def _codex_sdk_permission(_harness: str, value: str) -> ModeApplication:
    return ModeApplication(labels={CODEX_SDK_APPROVAL_MODE_LABEL_KEY: value})


def _agy_tier(harness: str, value: str) -> ModeApplication:
    from omnigent.permission_tiers import normalize_permission_tier
    from omnigent.server.permission_tiers import ConfirmedPermissionState

    tier = normalize_permission_tier(value, harness=harness, legacy=False)
    labels = ConfirmedPermissionState(tier, 1, "blocked").labels(harness)
    args = (
        ("--dangerously-skip-permissions",)
        if harness == "antigravity-native" and value == "full"
        else ()
    )
    return ModeApplication(args=args, labels=labels, tier=value)


def _speed(_harness: str, value: str) -> ModeApplication:
    return ModeApplication(labels={SPEED_TIER_LABEL_KEY: value})


BUILTIN_MODE_HANDLERS: dict[str, ModeHandler] = {
    "claude-permission-mode": _claude_permission,
    "codex-approval-preset": _codex_native_permission,
    "codex-sdk-approval": _codex_sdk_permission,
    "agy-tier": _agy_tier,
    "claude-fast-mode": _speed,
    "codex-service-tier": _speed,
}

CODEX_NATIVE_PERMISSION_DEFAULT_ARGS: dict[str, list[str]] = {
    "ask-for-approval": [
        "--ask-for-approval",
        "on-request",
        "--sandbox",
        "workspace-write",
        "-c",
        'approvals_reviewer="user"',
    ],
    "approve-for-me": ["--approve-for-me"],
    "full-access": ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
    "read-only": ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
}
CLAUDE_FAST_MODE_SETTINGS: dict[str, bool] = {"fast": True, "standard": False}


def _options(
    rows: tuple[tuple[str, str, str], ...], *, create_only: frozenset[str] = frozenset()
) -> tuple[ModeOption, ...]:
    return tuple(
        ModeOption(value, label, concept, _CREATE if value in create_only else _BOTH)
        for value, label, concept in rows
    )


CLAUDE_PERMISSION_OPTIONS = _options(
    (
        ("default", "Manual", "manual"),
        ("auto", "Auto", "automatic"),
        ("acceptEdits", "Accept edits", "edit-automatic"),
        ("plan", "Plan", "read-only"),
        ("dontAsk", "Don't ask", "deny"),
        ("bypassPermissions", "Bypass permissions", "full-access"),
    ),
    create_only=frozenset({"default"}),
)
CODEX_PERMISSION_OPTIONS = _options(
    tuple(
        (
            preset.value,
            preset.label,
            {"full-access": "full-access", "read-only": "read-only"}.get(
                preset.value, "automatic"
            ),
        )
        for preset in CODEX_NATIVE_PERMISSION_PRESETS
    )
)
_TIER_LABELS = {
    "ask": "每次都问",
    "plan": "只规划",
    "accept-edits": "自动改文件",
    "auto": "自动（只拦可怕的事）",
    "full": "全放开",
}
_TIER_CONCEPTS = {
    "ask": "manual",
    "plan": "read-only",
    "accept-edits": "edit-automatic",
    "auto": "automatic",
    "full": "full-access",
}
TIER_OPTIONS = _options(
    tuple((tier, _TIER_LABELS[tier], _TIER_CONCEPTS[tier]) for tier in (*NON_FULL_TIERS, "full")),
    create_only=frozenset({"full"}),
)
SPEED_OPTIONS = _options((("standard", "Standard", "default"), ("fast", "Fast", "automatic")))

BUILTIN_SESSION_MODES: dict[str, SessionModes] = {
    "claude-native": SessionModes(
        ModeSet("claude-permission-mode", CLAUDE_PERMISSION_OPTIONS),
        ModeSet("claude-fast-mode", SPEED_OPTIONS),
    ),
    "claude-sdk": SessionModes(
        ModeSet("claude-permission-mode", CLAUDE_PERMISSION_OPTIONS),
        ModeSet("claude-fast-mode", SPEED_OPTIONS),
    ),
    "codex-native": SessionModes(
        ModeSet("codex-approval-preset", CODEX_PERMISSION_OPTIONS),
        ModeSet("codex-service-tier", "per-model"),
    ),
    "codex": SessionModes(
        ModeSet("codex-sdk-approval", CODEX_PERMISSION_OPTIONS),
        ModeSet("codex-service-tier", "per-model"),
    ),
    "pi-native": SessionModes(ModeSet("agy-tier", TIER_OPTIONS)),
    "pi": SessionModes(ModeSet("agy-tier", TIER_OPTIONS)),
    "antigravity-native": SessionModes(ModeSet("agy-tier", TIER_OPTIONS)),
}

# Compatibility for callers that inspect the built-in vocabulary directly.
# Runtime validation below always reads the merged contribution registry.
PERMISSION_DEFAULT_VALUES: dict[str, frozenset[str]] = {
    harness: frozenset(
        option.value for option in modes.permission.options if "default" in option.contexts
    )
    for harness, modes in BUILTIN_SESSION_MODES.items()
    if modes.permission is not None and modes.permission.options != "per-model"
}


def mode_set(harness: str | None, field: str) -> ModeSet | None:
    from omnigent.harness_plugins import capability_for_harness

    capability = capability_for_harness(harness)
    if capability is None or capability.session_modes is None:
        return None
    return getattr(capability.session_modes, field, None)


def mode_values(harness: str | None, field: str, context: ModeContext) -> frozenset[str]:
    if context not in {"default", "create"}:
        raise ValueError(f"unknown mode context: {context!r}")
    declared = mode_set(harness, field)
    if declared is None or declared.options == "per-model":
        return frozenset()
    return frozenset(option.value for option in declared.options if context in option.contexts)


def valid_mode(value: object, harness: str | None, field: str, context: ModeContext) -> bool:
    if context not in {"default", "create"}:
        raise ValueError(f"unknown mode context: {context!r}")
    if not isinstance(value, str):
        return False
    value = _normalized_mode(value, harness, field)
    declared = mode_set(harness, field)
    if declared is None:
        return False
    if declared.options == "per-model":
        return field == "speed" and bool(_SERVICE_TIER_ID.fullmatch(value))
    return value in mode_values(harness, field, context)


def _normalized_mode(value: str, harness: str | None, field: str) -> str:
    # Older Codex SDK tasks stored the restrictive approval preset as "default".
    return (
        "ask-for-approval"
        if (harness, field, value) == ("codex", "permission", "default")
        else value
    )


def valid_speed_tier(value: object, harness: str | None) -> bool:
    return valid_mode(value, harness, "speed", "default")


def codex_service_tier(value: str) -> str:
    if not valid_speed_tier(value, "codex"):
        raise ValueError("invalid Codex service tier")
    return {"standard": "default", "fast": "priority"}.get(value, value)


def apply_mode(
    harness: str | None, field: str, value: str, *, context: ModeContext = "create"
) -> ModeApplication:
    """Validate a declared value, then dispatch only its registered handler."""
    if not valid_mode(value, harness, field, context):
        raise ValueError(f"unsupported {field} mode {value!r} for {harness!r}")
    declared = mode_set(harness, field)
    assert declared is not None and harness is not None
    from omnigent.harness_plugins import mode_handlers

    return mode_handlers()[declared.handler](harness, _normalized_mode(value, harness, field))
