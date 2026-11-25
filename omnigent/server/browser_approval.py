"""Human approval boundary shared by host restart and terminal access cards."""

from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.server.auth import AuthProvider, UnifiedAuthProvider, local_single_user_enabled
from omnigent.server.ws_origin import origin_allowed, parse_allowed_origins

BROWSER_APPROVAL_UNAVAILABLE_REASON = (
    "Needs a signed-in browser — available with accounts sign-in."
)


def browser_approval_supported(auth_provider: AuthProvider | None) -> bool:
    """Whether *auth_provider* can verify signed browser-session cookies."""
    if not isinstance(auth_provider, UnifiedAuthProvider):
        return False
    if auth_provider._source == "oidc":
        return auth_provider._oidc_config is not None
    if auth_provider._source == "accounts":
        return auth_provider._accounts_config is not None
    return False


def is_single_user_without_browser_approval(auth_provider: AuthProvider | None) -> bool:
    """Whether the server runs in single-user local mode without browser-session auth."""
    if browser_approval_supported(auth_provider):
        return False
    if isinstance(auth_provider, UnifiedAuthProvider) and auth_provider._local_single_user:
        return True
    return local_single_user_enabled()


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
    if mutating and (
        not valid_origin or not origin_allowed(origin, local_mode=False, extra_allowed=allowed)
    ):
        raise HTTPException(403, "A trusted browser origin is required")
    user = auth_provider.browser_session_user_id(request)
    if user is None:
        raise HTTPException(
            401,
            "Sign in through the web app",
            headers={"X-Omnigent-Auth-Recovery": "browser-session-required"},
        )
    return user


def require_owner_browser_approval(
    request: Request, auth_provider: AuthProvider | None, owner: str | None
) -> None:
    """Require the owner's browser cookie and a configured, trusted HTTP origin."""
    if is_single_user_without_browser_approval(auth_provider):
        raise OmnigentError(BROWSER_APPROVAL_UNAVAILABLE_REASON, code=ErrorCode.FORBIDDEN)
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
