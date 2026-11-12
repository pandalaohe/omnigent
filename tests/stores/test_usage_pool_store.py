"""Immutable owner-scoped pool identities, including concurrent graph edits."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.stores.usage_pool_store import (
    UsagePoolConflictError,
    UsagePoolRelationError,
    UsagePoolStore,
)
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore
from omnigent.usage.contract import PoolRelationClaim

OWNER = "owner@example.test"
HOUR = 1794304800


def claim(parent="parent", pool="source:pool-a", channel="usage", hour=HOUR):
    return PoolRelationClaim(
        parent_account_id=parent, pool_id=pool, channel=channel, effective_from_hour=hour
    )


def parent_snapshot(uri, account="parent", owner=OWNER):
    UsageSnapshotStore(uri).write(
        owner, "usage.account." + account, {"schema_version": 1, "report": {"windows": []}}
    )


def test_identity_survives_rename_and_rejects_reassignment(db_uri):
    parent_snapshot(db_uri)
    store = UsagePoolStore(db_uri)
    first = store.write(OWNER, "pool-a", claim(), display_name="First")
    renamed = store.write(OWNER, "pool-a", claim(), display_name="Renamed")
    assert renamed.identity == first.identity
    assert renamed.display_name == "Renamed"
    assert len(store.read(OWNER)) == 1
    for edit in [claim(pool="other"), claim(channel="quota"), claim(hour=HOUR + 3600)]:
        with pytest.raises(UsagePoolConflictError):
            store.write(OWNER, "pool-a", edit)
    with pytest.raises(UsagePoolConflictError):
        store.write(OWNER, "duplicate", claim())
    assert store.read("other@example.test") == []
    with workspace_scope(41):
        assert store.read(OWNER) == []
        with pytest.raises(UsagePoolRelationError):
            store.write(OWNER, "pool-a", claim())


def test_parent_must_belong_to_same_owner_and_cycles_are_rejected(db_uri):
    parent_snapshot(db_uri)
    store = UsagePoolStore(db_uri)
    with pytest.raises(UsagePoolRelationError):
        store.write("other@example.test", "pool-a", claim())
    store.write(OWNER, "pool-a", claim())
    with pytest.raises(UsagePoolRelationError):
        store.write(OWNER, "parent", claim(parent="pool-a", pool="cycle"))
    assert len(store.read(OWNER)) == 1


def test_concurrent_empty_graph_cannot_create_cycle(db_uri):
    for account in ("a", "b"):
        parent_snapshot(db_uri, account)
    barrier = Barrier(2)

    def write(account, parent):
        store = UsagePoolStore(db_uri)
        barrier.wait(timeout=15)
        try:
            store.write(OWNER, account, claim(parent=parent, pool=account))
            return "accepted"
        except UsagePoolRelationError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(write, "a", "b")
        b = executor.submit(write, "b", "a")
        assert sorted([a.result(timeout=40), b.result(timeout=40)]) == ["accepted", "rejected"]
    assert len(UsagePoolStore(db_uri).read(OWNER)) == 1
