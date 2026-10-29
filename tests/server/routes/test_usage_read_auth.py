"""Usage-reader credentials are owner-backed and reach only two usage GETs."""

from __future__ import annotations

import secrets

import pytest
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


@pytest.fixture
def config_token(monkeypatch: pytest.MonkeyPatch) -> str:
    token = "fake_usage_" + secrets.token_urlsafe(32)
    monkeypatch.setenv("OMNIGENT_USAGE_READ_TOKEN", f"owner:{token}")
    return token


def test_config_reader_binds_live_authority_and_never_caches(config_token: str) -> None:
    from omnigent.db.account_authority import account_authority_scope, account_generation

    provider = _provider()
    generation = "generation-one"
    provider.set_usage_read_account_lookup(lambda _: generation)
    with account_authority_scope(None, None):
        for path in ("/v1/usage-events", "/v1/usage-cutover/receipts"):
            request = _request(config_token, path)
            assert provider.get_user_id(request) == "owner"
            assert request.state.usage_read_grant_id == "config:usage-read"
            assert account_generation("owner") == generation
            generation = "generation-two"
        provider.set_usage_read_account_lookup(lambda _: None)
        assert provider.get_user_id(_request(config_token)) is None
        assert account_generation("owner") is None


def test_config_reader_rejects_other_authority(config_token: str) -> None:
    provider = _provider()
    assert provider.get_user_id(_request(config_token)) is None  # lookup not wired
    provider.set_usage_read_account_lookup(lambda _: "generation")
    for path, method in [
        ("/v1/usage-events", "POST"),
        ("/v1/usage-events/child", "GET"),
        ("/v1/sessions", "GET"),
        ("/auth/users", "GET"),
        ("/v1/usage-cutover/holds/host/claude-sdk", "GET"),
        ("/v1/usage-cutover/receipts", "POST"),
        ("/health", "GET"),
        ("/api/version", "GET"),
        ("/oauth/revoke", "POST"),
    ]:
        assert provider.get_user_id(_request(config_token, path, method)) is None
    assert provider.get_user_id(_request("fake_wrong_" + secrets.token_urlsafe(32))) is None
    cookie = _request("")
    cookie.scope["headers"] = [(b"cookie", f"__Host-ap_session={config_token}".encode())]
    assert provider.get_user_id(cookie) is None


@pytest.mark.parametrize("setting", [None, ""])
def test_config_reader_off(monkeypatch: pytest.MonkeyPatch, setting: str | None) -> None:
    if setting is None:
        monkeypatch.delenv("OMNIGENT_USAGE_READ_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_USAGE_READ_TOKEN", setting)
    provider = _provider()
    assert provider.usage_read_token_user is None
    assert provider.get_user_id(_request("fake_usage_" + secrets.token_urlsafe(32))) is None


@pytest.mark.parametrize("malformed", ["missing", "account", "short", "jwt", "colon"])
def test_config_reader_bad_config_never_echoed(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, malformed: str
) -> None:
    token = "fake_usage_" + secrets.token_urlsafe(32)
    account = "private-owner"
    value = {
        "missing": token,
        "account": f"PrivateOwner:{token}",
        "short": f"{account}:" + "x" * 31,
        "jwt": f"{account}:{token}.jwt",
        "colon": f"{account}:{token}:extra",
    }[malformed]
    monkeypatch.setenv("OMNIGENT_USAGE_READ_TOKEN", value)
    with pytest.raises(ValueError) as error:
        _provider()
    assert str(error.value) == "Invalid OMNIGENT_USAGE_READ_TOKEN"
    for part in value.split(":"):
        assert part not in str(error.value) + caplog.text


@pytest.mark.parametrize("source", ["header", "oidc"])
def test_config_reader_ignored_outside_accounts(
    config_token: str, caplog: pytest.LogCaptureFixture, source: str
) -> None:
    provider = UnifiedAuthProvider(source=source, local_single_user=False)
    assert provider.usage_read_token_user is None
    assert provider.get_user_id(_request(config_token)) is None
    assert "OMNIGENT_USAGE_READ_TOKEN" in caplog.text
    assert "ignored" in caplog.text
    assert config_token not in caplog.text


def test_config_reader_logs_only_validated_account(
    config_token: str, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level("INFO", logger="omnigent.server.auth"):
        provider = _provider()
        assert provider.usage_read_token_user == "owner"
        provider.get_user_id(_request(config_token))
        provider.get_user_id(_request(config_token, "/v1/sessions"))
    assert "usage read token enabled for owner" in caplog.text
    assert config_token not in caplog.text
