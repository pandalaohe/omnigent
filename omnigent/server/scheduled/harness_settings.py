"""Harness-specific permission overrides for saved automations."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness, is_native_harness
from omnigent.native.permission_modes import PERMISSION_LAUNCH_ARGS
from omnigent.session_default_modes import mode_set, valid_mode

_logger = logging.getLogger(__name__)


UNATTENDED_PERMISSION_MODE_MESSAGE = "Unsupported permission_mode: {mode!r}"


def unattended_permission_mode_allowed(mode: str | None) -> bool:
    """Whether the mode belongs to a supported native or SDK vocabulary."""
    try:
        validate_scheduled_permission_mode(mode)
    except OmnigentError:
        return False
    return True


def validate_scheduled_permission_mode(mode: str | None) -> str | None:
    """Validate the vocabulary before resolving the bound agent's harness."""
    if mode is None:
        return None
    from omnigent.server.routes._session_create_validation import validate_session_permission_mode

    if any(mode in modes for modes in PERMISSION_LAUNCH_ARGS.values()):
        return mode
    try:
        validate_session_permission_mode(mode)
    except OmnigentError:
        raise OmnigentError(
            f"Unsupported permission_mode: {mode!r}", code=ErrorCode.INVALID_INPUT
        ) from None
    return mode


async def _scheduled_agent_executor(agent: Any, agent_cache: Any) -> tuple[str | None, Any]:
    """Load the saved agent's executor; ``(None, None)`` when it cannot be loaded."""
    if agent is None or agent_cache is None or getattr(agent, "bundle_location", None) is None:
        return None, None
    try:
        loaded = await asyncio.to_thread(agent_cache.load, agent.id, agent.bundle_location)
        executor = getattr(loaded.spec, "executor", None)
        raw = (executor.config.get("harness") or executor.type) if executor else None
        return canonicalize_harness(raw) or raw, executor
    except Exception:
        _logger.exception("Could not resolve harness for scheduled agent %s", agent.id)
        return None, None


async def scheduled_agent_harness(agent: Any, agent_cache: Any) -> str | None:
    """Resolve the saved agent; return None when its harness cannot be determined."""
    harness, _ = await _scheduled_agent_executor(agent, agent_cache)
    return harness


async def scheduled_agent_default_permission_mode(agent: Any, agent_cache: Any) -> str | None:
    """The mode a run with no task override launches with, or ``None`` for the harness default.

    Native harnesses start in their CLI's own default. SDK harnesses take
    ``executor.config.permission_mode`` from the spec; without one they run in
    ``auto``. ``None`` also when the agent cannot be loaded.
    """
    harness, executor = await _scheduled_agent_executor(agent, agent_cache)
    if harness is None or executor is None or is_native_harness(harness):
        return None
    configured = executor.config.get("permission_mode")
    return None if configured is None else str(configured)


async def validate_scheduled_agent_default_permission(*, agent: Any, agent_cache: Any) -> None:
    """Reject an agent whose own default names an unknown permission mode."""
    mode = await scheduled_agent_default_permission_mode(agent, agent_cache)
    if not unattended_permission_mode_allowed(mode):
        raise OmnigentError(
            UNATTENDED_PERMISSION_MODE_MESSAGE.format(mode=mode)
            + " (set by the agent; set permission_mode in the agent spec)",
            code=ErrorCode.INVALID_INPUT,
        )


async def validate_scheduled_permission_support(
    *, permission_mode: str | None, agent: Any, agent_cache: Any
) -> None:
    """Reject modes belonging to a different harness or an unknown agent default."""
    if permission_mode is None:
        await validate_scheduled_agent_default_permission(agent=agent, agent_cache=agent_cache)
        return
    harness = await scheduled_agent_harness(agent, agent_cache)
    if harness is None:
        raise OmnigentError(
            "Cannot resolve the agent's harness to validate permission_mode; try again later",
            code=ErrorCode.INVALID_INPUT,
        )
    validate_scheduled_permission_harness_support(permission_mode=permission_mode, harness=harness)


def validate_scheduled_permission_harness_support(
    *, permission_mode: str | None, harness: str | None
) -> None:
    """Validate a resolved stored or library agent using its declared modes."""
    if permission_mode is None:
        return
    native_fallback = (
        mode_set(harness, "permission") is None
        or (harness == "codex-native" and permission_mode in {"default", "bypass"})
    ) and permission_mode in PERMISSION_LAUNCH_ARGS.get(harness or "", {})
    if not valid_mode(permission_mode, harness, "permission", "create") and not native_fallback:
        raise OmnigentError(
            f"permission_mode {permission_mode!r} is not supported for {harness!r}",
            code=ErrorCode.INVALID_INPUT,
        )
