"""Human approval boundary shared by host restart and terminal access cards."""

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.auth import AuthProvider, UnifiedAuthProvider
from omnigent.server.ws_origin import origin_allowed, parse_allowed_origins


def browser_action_user(
    request: Request, auth_provider: AuthProvider | None, *, mutating: bool
) -> str:
    """Accept only a browser-purpose cookie; writes also prove trusted origin."""
    if (
        request.headers.get("authorization") is not None
        or request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) is not None
        or not isinstance(auth_provider, UnifiedAuthProvider)
    ):
        raise HTTPException(401, "Sign in through the web app")
    user = auth_provider.browser_session_user_id(request)
    if user is None:
        raise HTTPException(401, "Sign in through the web app")
    if not mutating:
        return user
    origin = request.headers.get("origin")
    try:
        parts = urlsplit(origin or "")
        _ = parts.port
        valid_origin = (
            parts.scheme in ("http", "https")
            and bool(parts.netloc)
            and parts.path == ""
            and not parts.query
            and not parts.fragment
            and parts.username is None
            and parts.password is None
        )
    except ValueError:
        valid_origin = False
    allowed = parse_allowed_origins() | frozenset({auth_provider.browser_session_origin or ""})
    if not valid_origin or not origin_allowed(origin, local_mode=False, extra_allowed=allowed):
        raise HTTPException(403, "A trusted browser origin is required")
    return user


def require_owner_browser_approval(
    request: Request, auth_provider: AuthProvider | None, owner: str | None
) -> None:
    """Require the owner's browser cookie and a configured, trusted HTTP origin."""
    try:
        user = browser_action_user(request, auth_provider, mutating=True)
        if owner is not None and user == owner:
            return
    except HTTPException:
        pass
    raise OmnigentError(
        "Sign in again to the host owner's account in the web app to answer this card. "
        "A trusted browser origin is required.",
        code=ErrorCode.FORBIDDEN,
    )
