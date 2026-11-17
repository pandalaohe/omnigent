"""Safe refusal categories shared by terminal transports and their browser UI."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Coroutine, Mapping
from typing import Any

from fastapi import HTTPException, Request, Response, WebSocket
from fastapi.routing import APIRoute
from starlette.websockets import WebSocketState

from omnigent.errors import ErrorCode, OmnigentError

_logger = logging.getLogger(__name__)
_CATEGORIES = frozenset(
    {
        "origin_untrusted",
        "runner_credential",
        "browser_session_missing",
        "browser_account_mismatch",
        "not_owner",
        "admin_not_allowed",
        "access_denied",
    }
)


class TerminalAccessDenied(HTTPException):
    def __init__(self, category: str, detail: str, *, headers: Mapping[str, str] | None = None):
        self.category = category
        super().__init__(403, detail, headers=headers)


def terminal_refusal_category(exc: BaseException, default: str = "access_denied") -> str:
    """Inspect only known discriminators, never forward arbitrary exception text."""
    if isinstance(exc, TerminalAccessDenied) and exc.category in _CATEGORIES:
        return exc.category
    if isinstance(exc, HTTPException):
        if (exc.headers or {}).get("X-Omnigent-Auth-Recovery") == "browser-session-required":
            return "browser_session_missing"
        if exc.status_code == 401:
            return "browser_session_missing"
    if exc.__cause__ is not None:
        return terminal_refusal_category(exc.__cause__, default)
    if isinstance(exc, OmnigentError) and exc.code == ErrorCode.UNAUTHORIZED:
        return "browser_session_missing"
    return default


def log_terminal_refusal(category: str) -> None:
    _logger.info("terminal access refused category=%s", category)


def is_terminal_attach_path(path: str) -> bool:
    return (
        re.fullmatch(
            r"/v1/(?:sessions/[^/]+/resources/terminals|hosts/[^/]+/terminals)/[^/]+/attach",
            path,
        )
        is not None
    )


async def close_terminal_refusal(websocket: WebSocket, category: str) -> None:
    category = category if category in _CATEGORIES else "access_denied"
    log_terminal_refusal(category)
    # A pre-accept close becomes an opaque HTTP 403 / browser code 1006.
    # Accept only to deliver this fixed reason; no terminal is opened.
    if websocket.application_state == WebSocketState.CONNECTING:
        await websocket.accept()
    await websocket.close(code=4403, reason=category)


class TerminalRefusalRoute(APIRoute):
    """Log denied HTTP controls without logging credentials or request headers."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def handle(request: Request) -> Response:
            try:
                return await handler(request)
            except (HTTPException, OmnigentError) as exc:
                status = exc.status_code if isinstance(exc, HTTPException) else exc.http_status
                if status in (401, 403):
                    log_terminal_refusal(terminal_refusal_category(exc))
                raise

        return handle
