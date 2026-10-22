"""A usage-reader credential is owner-backed and reaches one GET only."""

from __future__ import annotations

from starlette.requests import Request

from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes.device_auth import mint_delegated_token

_KEY = b"test-usage-reader-key-32-bytes-long"


def _provider() -> UnifiedAuthProvider:
    return UnifiedAuthProvider(
        source="accounts",
        accounts_config=AccountsConfig(
            cookie_secret=_KEY,
            session_ttl_hours=1,
            base_url="https://omni.example.test",
            init_admin_password=None,
            invite_ttl_seconds=3600,
            magic_ttl_seconds=300,
        ),
    )


def _request(token: str, path: str = "/v1/usage-events", method: str = "GET") -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "scheme": "https",
            "server": ("omni.example.test", 443),
            "query_string": b"",
        }
    )


def _token(grant_id: str | None = "a" * 32, scope: str = "usage:read") -> str:
    return mint_delegated_token(
        "owner",
        _KEY,
        3600,
        "accounts",
        grant_id=grant_id,
        client_id="usage-reader",
        jti="test-jti",
        scope=scope,
    )


def test_usage_scope_never_inherits_session_or_write_authority() -> None:
    provider = _provider()
    provider.set_grant_revocation_check(lambda _: False)
    token = _token()
    assert provider.get_user_id(_request(token)) == "owner"
    for path, method in [
        ("/v1/usage-events", "POST"),
        ("/v1/usage-events/child", "GET"),
        ("/v1/sessions", "GET"),
        ("/v1/runners", "POST"),
        ("/auth/users", "GET"),
        # Only the receipts page joins the one read path; every other cutover
        # route stays owner-only.
        ("/v1/usage-cutover/receipts", "POST"),
        ("/v1/usage-cutover/holds", "GET"),
        ("/v1/usage-cutover/holds/harness", "GET"),
    ]:
        assert provider.get_user_id(_request(token, path, method)) is None
    assert provider.get_user_id(_request(token)) == "owner"
    receipts = _request(token, "/v1/usage-cutover/receipts")
    assert provider.get_user_id(receipts) == "owner"
    assert receipts.state.usage_read_grant_id == "a" * 32


def test_usage_scope_requires_live_owner_grant() -> None:
    provider = _provider()
    token = _token()
    assert provider.get_user_id(_request(token)) is None
    provider.set_grant_revocation_check(lambda _: False)
    assert provider.get_user_id(_request(_token(grant_id=None))) is None
    assert provider.get_user_id(_request(token)) == "owner"
    provider.set_grant_revocation_check(lambda _: True)
    assert provider.get_user_id(_request(token)) is None
    assert provider.get_user_id(_request(_token(scope="sessions"))) is None
