"""Human approval boundary shared by host restart and terminal access cards."""

from urllib.parse import urlsplit

from fastapi import Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.auth import AuthProvider, UnifiedAuthProvider
from omnigent.server.ws_origin import origin_allowed, parse_allowed_origins


def require_owner_browser_approval(
    request: Request, auth_provider: AuthProvider | None, owner: str | None
) -> None:
    """Require the owner's browser cookie and a configured, trusted HTTP origin.

    Proxy/header and unauthenticated runtimes have no browser-session proof and
    fail closed. Legacy cookies require a fresh browser login for these cards.
    """
    origin = request.headers.get("origin")
    try:
        parts = urlsplit(origin or "")
        _ = parts.port  # Validate a malformed or out-of-range port.
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
    if (
        not valid_origin
        or request.headers.get("authorization") is not None
        or request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) is not None
        or not isinstance(auth_provider, UnifiedAuthProvider)
        or owner is None
        or auth_provider.browser_session_user_id(request) != owner
        or not origin_allowed(
            origin,
            local_mode=False,
            extra_allowed=parse_allowed_origins()
            | frozenset({auth_provider.browser_session_origin or ""}),
        )
    ):
        raise OmnigentError(
            "Sign in again to the host owner's account in the web app to answer this card. "
            "A trusted browser origin is required.",
            code=ErrorCode.FORBIDDEN,
        )
