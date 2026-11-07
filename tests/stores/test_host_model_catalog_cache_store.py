"""Tests for :class:`HostModelCatalogCacheStore`."""

from __future__ import annotations

from omnigent.db.db_models import workspace_scope
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore

_HOST = "a1b2c3d4e5f60718293a4b5c6d7e8f01"


def _store(db_uri: str) -> HostModelCatalogCacheStore:
    return HostModelCatalogCacheStore(db_uri)


def test_upsert_round_trips_and_clears_a_previous_error(db_uri: str) -> None:
    """A successful sync replaces the payload, fetch time, and error."""
    store = _store(db_uri)
    store.mark_error(_HOST, "codex", "unsupported")
    store.upsert(_HOST, "codex", [{"id": "gpt-6-sol"}], 1700000000)
    assert store.list([_HOST]) == [
        {
            "host_id": _HOST,
            "harness": "codex",
            "models": [{"id": "gpt-6-sol"}],
            "permission_options": [],
            "injects_prompt": None,
            "fetched_at": 1700000000,
            "error": None,
        }
    ]


def test_mark_error_keeps_the_last_payload(db_uri: str) -> None:
    """A failed sync keeps the previous models and fetch time."""
    store = _store(db_uri)
    store.upsert(_HOST, "codex", [{"id": "gpt-6-sol"}], 1700000000)
    store.mark_error(_HOST, "codex", "unsupported")
    (record,) = store.list([_HOST])
    assert record["models"] == [{"id": "gpt-6-sol"}]
    assert record["fetched_at"] == 1700000000
    assert record["error"] == "unsupported"


def test_permission_payload_round_trip_and_legacy_list_sync(db_uri: str) -> None:
    store = _store(db_uri)
    options = [{"value": "review", "label": "Review", "description": "Review changes"}]
    store.upsert(
        _HOST, "acp:neutral", [{"id": "a"}], 1, permission_options=options, injects_prompt=False
    )
    store.mark_error(_HOST, "acp:neutral", "offline")
    record = store.list([_HOST])[0]
    assert record["models"] == [{"id": "a"}]
    assert record["permission_options"] == options
    assert record["injects_prompt"] is False
    store.upsert(_HOST, "acp:neutral", [{"id": "b"}], 2)
    assert store.list([_HOST])[0]["permission_options"] == []
    assert store.list([_HOST])[0]["injects_prompt"] is None


def test_mark_error_creates_an_empty_row_when_none_exists(db_uri: str) -> None:
    """A first-time failure still records the pair, with no models."""
    store = _store(db_uri)
    store.mark_error(_HOST, "codex", "unsupported")
    (record,) = store.list([_HOST])
    assert record["models"] == []
    assert record["fetched_at"] is None
    assert record["error"] == "unsupported"


def test_failed_model_probe_keeps_configuration_delivery_metadata(db_uri: str) -> None:
    store = _store(db_uri)
    store.mark_error(_HOST, "acp:neutral", "probe failed", injects_prompt=False)
    record = store.list([_HOST])[0]
    assert record["models"] == []
    assert record["injects_prompt"] is False
    store.upsert(_HOST, "acp:neutral", [{"id": "previous"}], 1, injects_prompt=True)
    store.mark_error(_HOST, "acp:neutral", "probe failed again", injects_prompt=False)
    record = store.list([_HOST])[0]
    assert record["models"] == [{"id": "previous"}]
    assert record["fetched_at"] == 1
    assert record["injects_prompt"] is False


def test_list_is_workspace_scoped(db_uri: str) -> None:
    """Rows written under one workspace never leak into another."""
    store = _store(db_uri)
    store.upsert(_HOST, "codex", [{"id": "workspace-0"}], 1)
    with workspace_scope(7):
        store.upsert(_HOST, "codex", [{"id": "workspace-7"}], 2)
        assert store.list([_HOST])[0]["models"] == [{"id": "workspace-7"}]
    assert store.list([_HOST])[0]["models"] == [{"id": "workspace-0"}]
