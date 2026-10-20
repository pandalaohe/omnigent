"""Durable barrier between child creation and its caller's work registration."""

from __future__ import annotations

from fastapi import Request

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.session_open_rate import CREATE_ORIGIN_AGENT, CREATE_ORIGIN_HEADER

STARTUP_REGISTRATION_LABEL_KEY = "omnigent.startup.registration"
STARTUP_REGISTRATION_PENDING = "pending"
STARTUP_CREATE_PREPARATION_LABEL_KEY = "omnigent.startup.create_preparation"
STARTUP_CREATE_PREPARATION_PENDING = "pending"


def require_parent_runner(request: Request, parent: Conversation | None) -> None:
    """Require the current parent runner's binding proof, even in single-user mode."""
    token = request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)
    if (
        parent is None
        or not parent.runner_id
        or not token
        or token_bound_runner_id(token) != parent.runner_id
    ):
        raise OmnigentError(
            "Current parent runner binding required for child registration",
            code=ErrorCode.FORBIDDEN,
        )


def registration_pending_for_create(request: Request, parent: Conversation | None) -> bool:
    """Agent-origin child creates must wait for their caller's durable registration."""
    if getattr(request.app.state, "startup_admission", None) is None or parent is None:
        return False
    if request.headers.get(CREATE_ORIGIN_HEADER, "").strip().lower() != CREATE_ORIGIN_AGENT:
        return False
    require_parent_runner(request, parent)
    return True
