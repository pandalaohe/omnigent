"""Personal keys preserve tenant, account and one-time-secret boundaries."""

import hashlib
import re

import pytest
from sqlalchemy import select

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import SqlUserApiKey
from omnigent.errors import OmnigentError
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.api_key_store import ApiKeyNameTaken, UserApiKeyStore
from omnigent.server.auth import api_key_scope_allows
from tests.server.routes.test_usage_read_auth import _provider, _request


@pytest.fixture
def keys(db_uri):
    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("owner", "synthetic-hash")
    accounts.create_user_with_password("other", "synthetic-hash")
    with account_authority_scope(None, None):
        yield UserApiKeyStore(db_uri), accounts


def test_key_lifecycle_and_account_lifetime(keys):
    store, accounts = keys
    key, value = store.create("owner", name="feed", scopes=("*",), expires_at=None, now=100)
    assert re.fullmatch(r"omk_[A-Za-z0-9_-]{43}", value)
    digest = hashlib.sha256(value.encode()).hexdigest()
    assert key.prefix == value[:12]
    assert store.list_for_user("other") == []
    with store._session("inspect_digest") as session:
        assert session.scalar(select(SqlUserApiKey.secret_sha256)) == digest
    provider = _provider()
    provider.set_api_key_store(store.resolve, store.touch)
    provider.set_api_key_usage_check(lambda user: store.has_usage_key(user, 101))
    assert provider.has_usage_api_key("owner")
    request = _request(value)
    assert provider.get_user_id(request) == "owner"
    assert request.state.usage_read_grant_id == "config:usage-read"
    for path, method in [
        ("/v1/sessions", "GET"),
        ("/v1/me/api-keys", "GET"),
        ("/v1/me/api-keys", "POST"),
    ]:
        assert provider.get_user_id(_request(value, path, method)) is None
    assert provider.get_user_id(_request(value, "/v1/me/tasks/sources/feed", "PUT")) == "owner"
    assert store.update("other", key.id, name="stolen") is None
    assert store.regenerate("other", key.id, expires_at=None, now=102) is None
    assert not store.delete("other", key.id)
    store.update("owner", key.id, scopes=("usage",), expires_at=200)
    assert store.resolve(digest, 199) is not None
    assert store.resolve(digest, 200) is None
    assert not store.has_usage_key("owner", 200)
    with account_authority_scope(None, None):
        accounts.update_password("owner", "new-synthetic-hash", revoke_sessions=True)
    assert store.resolve(digest, 199)[1] == accounts.get_user("owner").account_generation
    from omnigent.db.account_authority import clear_account_authority

    clear_account_authority()
    new_key, new_value = store.regenerate("owner", key.id, expires_at=None, now=201)
    assert new_key.last_used_at is None and new_key.regenerated_at == 201
    assert new_key.prefix != key.prefix
    assert store.resolve(digest, 202) is None
    assert store.resolve(hashlib.sha256(new_value.encode()).hexdigest(), 202)
    assert store.delete("owner", key.id)
    assert store.list_for_user("owner") == []


def test_throttle_digest_and_name_conflicts(keys):
    store, _ = keys
    key, value = store.create("owner", name="feed", scopes=("usage",), expires_at=None, now=1)
    digest = hashlib.sha256(value.encode()).hexdigest()
    with pytest.raises(ApiKeyNameTaken):
        store.create("owner", name="feed", scopes=("tasks",), expires_at=None, now=1)
    store.create("other", name="feed", scopes=("tasks",), expires_at=None, now=1)
    store.touch(key.id, digest, 100)
    store.touch(key.id, digest, 101)
    assert store.list_for_user("owner")[0].last_used_at == 100
    store.touch(key.id, "0" * 64, 900)
    assert store.list_for_user("owner")[0].last_used_at == 100
    store.touch(key.id, digest, 701)
    assert store.list_for_user("owner")[0].last_used_at == 701


def test_account_deletion_removes_keys_and_refuses_new_writes(keys):
    store, accounts = keys
    key, value = store.create("owner", name="feed", scopes=("*",), expires_at=None, now=1)
    assert accounts.delete_user("owner") is not False
    assert not store.list_for_user("owner")
    with pytest.raises(OmnigentError):
        store.create("owner", name="late", scopes=("*",), expires_at=None, now=2)
    accounts.create_user_with_password("owner", "replacement-synthetic-hash")
    assert store.resolve(hashlib.sha256(value.encode()).hexdigest(), 3) is None
    assert store.update("owner", key.id, name="late") is None


def test_managed_auth_fail_closed_and_avoids_jwt_lookups(keys, caplog):
    store, _ = keys
    _key, value = store.create("owner", name="feed", scopes=("usage",), expires_at=None, now=1)
    provider = _provider()
    provider.set_api_key_store(store.resolve, store.touch)
    assert provider.get_user_id(_request(value, "/v1/me/tasks/sources/feed", "PUT")) is None
    assert store.list_for_user("owner")[0].last_used_at is None
    calls = []

    def broken(digest, now):
        calls.append(digest)
        raise RuntimeError(value)

    provider.set_api_key_store(broken, store.touch)
    assert provider.get_user_id(_request("invalid.jwt.token")) is None
    assert calls == []
    assert provider.get_user_id(_request(value)) is None
    assert len(calls) == 1
    assert "RuntimeError" in caplog.text and value not in caplog.text


@pytest.mark.parametrize("scopes", [("*",), ("usage", "tasks")])
def test_all_permissions_are_union_of_known_scopes(scopes):
    assert api_key_scope_allows(scopes, "GET", "/v1/usage-events")
    assert api_key_scope_allows(scopes, "PUT", "/v1/me/tasks/sources/feed")
    assert not api_key_scope_allows(scopes, "DELETE", "/v1/me/tasks/sources/feed")
    assert not api_key_scope_allows(("unknown",), "GET", "/v1/usage-events")


def test_touch_is_workspace_scoped(keys):
    from sqlalchemy import update

    from omnigent.db.db_models import workspace_scope

    store, accounts = keys
    first, value = store.create("owner", name="feed", scopes=("*",), expires_at=None, now=1)
    with workspace_scope(91):
        accounts.create_user_with_password("owner", "synthetic-hash")
        other, _ = store.create("owner", name="feed", scopes=("*",), expires_at=None, now=1)
        with store._session_immediate("fixture_shared_id") as session:
            session.execute(
                update(SqlUserApiKey)
                .where(
                    SqlUserApiKey.workspace_id == 91,
                    SqlUserApiKey.id == other.id,
                )
                .values(id=first.id, secret_sha256=hashlib.sha256(value.encode()).hexdigest())
            )
    store.touch(first.id, hashlib.sha256(value.encode()).hexdigest(), 100)
    with workspace_scope(91):
        assert store.list_for_user("owner")[0].last_used_at is None


def test_deleted_request_cannot_create_for_reregistered_account(keys):
    store, accounts = keys
    generation = accounts.get_user("owner").account_generation
    accounts.delete_user("owner")
    accounts.create_user_with_password("owner", "replacement-synthetic-hash")
    with account_authority_scope("owner", generation), pytest.raises(OmnigentError):
        store.create("owner", name="old-request", scopes=("*",), expires_at=None, now=1)


def test_legacy_to_managed_feed_cursor_keeps_cutover_receipt(keys, db_uri, monkeypatch):
    import secrets
    import uuid

    from omnigent.server.server_config import load_api_keys
    from omnigent.usage_export.cutover import UsageCutoverStore
    from omnigent.usage_export.store import UsageEventStore
    from tests.stores.test_usage_event_store import (
        _consumer_receipt,
        _drain_facts,
        _rescan,
        _sealed,
    )

    store, accounts = keys
    accounts.create_user_with_password("alice", "synthetic-hash")
    old = secrets.token_urlsafe(32)
    provider = _provider()
    provider.set_api_keys(load_api_keys({}, {"OMNIGENT_USAGE_READ_TOKEN": f"alice:{old}"}))
    provider.set_usage_read_account_lookup(lambda user: accounts.get_user(user).account_generation)
    provider.set_api_key_store(store.resolve, store.touch)
    provider.set_api_key_usage_check(lambda user: store.has_usage_key(user, 1767225600))
    _, value = store.create("alice", name="feed", scopes=("usage",), expires_at=None, now=1)
    feed = UsageEventStore(db_uri)
    host = uuid.uuid4().hex
    feed.append(
        "alice",
        _sealed("a", host_id=host, harness="claude-sdk", occurred_at="2025-12-31T23:58:59Z"),
    )
    request = _request(old)
    assert provider.get_user_id(request) == "alice"
    first = feed.page("alice", grant_id=request.state.usage_read_grant_id)
    provider.set_api_keys(())
    request = _request(value)
    assert provider.get_user_id(request) == "alice"
    second = feed.page(
        "alice", cursor=first["next_cursor"], grant_id=request.state.usage_read_grant_id
    )
    assert second["events"] == []
    cutover = UsageCutoverStore(db_uri)
    monkeypatch.setattr("omnigent.usage_export.cutover._now", lambda: 1767225600)
    cutover.create_hold("alice", host, "claude-sdk", 1)
    drained = cutover.submit_drain("alice", host, "claude-sdk", 1, _drain_facts())
    assert provider.has_usage_api_key("alice")
    result = cutover.release(
        "alice",
        host,
        "claude-sdk",
        1,
        _consumer_receipt(drained, usage_read_grant_id=request.state.usage_read_grant_id),
        "2026-01-01T00:00:00Z",
        _rescan(),
        config_reader_user="alice",
    )
    assert result["result"] == "released"
