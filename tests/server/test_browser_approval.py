"""High-privilege cards require a browser-issued cookie and trusted origin."""

from dataclasses import replace

import jwt
import pytest
from starlette.requests import Request

from omnigent.errors import OmnigentError
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.browser_approval import require_owner_browser_approval
from omnigent.server.oidc import mint_session_cookie
from tests.server.integration.oidc_fixtures import make_oidc_config


@pytest.mark.parametrize("source", ["accounts", "oidc"])
@pytest.mark.parametrize(
    "case",
    [
        "browser",
        "allowlisted",
        "runner",
        "legacy",
        "bearer",
        "scope",
        "grant",
        "expired",
        "bad-signature",
        "other-owner",
        "revoked",
        "missing-origin",
        "bad-origin",
    ],
)
def test_owner_browser_approval(source, case, monkeypatch):
    secret = b"a" * 32
    config = AccountsConfig(secret, 8, "https://app.example.test/base", None, 3600, 300)
    oidc_config = replace(
        make_oidc_config(), redirect_uri="https://app.example.test/auth/callback"
    )
    provider = UnifiedAuthProvider(source, accounts_config=config, oidc_config=oidc_config)
    owner = "owner@example.test"
    token = mint_session_cookie(owner, secret, 8, source, browser_session=True)
    if case == "runner":
        token = provider.mint_runner_token(owner, 60)
    elif case == "legacy":
        token = mint_session_cookie(owner, secret, 8, source)
    elif case in ("scope", "grant", "expired", "bad-signature"):
        claims = jwt.decode(token, secret, algorithms=["HS256"])
        if case == "scope":
            claims["scope"] = "sessions"
        elif case == "grant":
            claims["grant_id"] = "test-grant"
        elif case == "expired":
            claims["exp"] = 1
        token = jwt.encode(
            claims, b"b" * 32 if case == "bad-signature" else secret, algorithm="HS256"
        )
    if case == "revoked":
        provider._account_check = lambda *_: False
    cookie_name = provider.session_cookie_name
    headers = {"cookie": f"{cookie_name}={token}", "origin": "https://app.example.test"}
    if case == "bearer":
        headers["authorization"] = f"Bearer {token}"
        del headers["cookie"]
    if case == "missing-origin":
        del headers["origin"]
    elif case in ("allowlisted", "bad-origin"):
        headers["origin"] = "https://other.example.test"
    monkeypatch.setenv(
        "OMNIGENT_WS_ALLOWED_ORIGINS",
        "https://other.example.test" if case == "allowlisted" else "",
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/resolve",
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
        }
    )
    if case in ("browser", "allowlisted"):
        require_owner_browser_approval(request, provider, owner)
    else:
        with pytest.raises(OmnigentError):
            require_owner_browser_approval(
                request, provider, "other@example.test" if case == "other-owner" else owner
            )


@pytest.mark.parametrize(
    "provider",
    [None, UnifiedAuthProvider("header"), UnifiedAuthProvider("header", local_single_user=True)],
)
def test_unproven_browser_identity_fails_closed(provider):
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/resolve",
            "headers": [
                (b"origin", b"http://localhost"),
                (b"x-forwarded-email", b"owner@example.test"),
            ],
        }
    )
    with pytest.raises(OmnigentError):
        require_owner_browser_approval(request, provider, "owner@example.test")
