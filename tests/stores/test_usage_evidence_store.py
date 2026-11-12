"""Evidence replay, isolation, deletion and retention on the selected backend."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from omnigent.db.db_models import SqlUsageHour, workspace_scope
from omnigent.db.utils import get_or_create_engine, now_epoch
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.usage_evidence import UsageEvidenceService
from omnigent.stores.usage_evidence_store import UsageEvidenceStore
from omnigent.stores.usage_hours_store import UsageHoursStore
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore
from omnigent.usage.evidence import SourceEvent, SourceStatus

OWNER = "owner@example.test"


def event(id="one", **kwargs):
    return SourceEvent.model_validate(
        dict(
            specversion="1.0",
            id=id,
            source="/collector",
            type="usage.status",
            time=datetime.fromtimestamp(now_epoch(), UTC).isoformat(),
            data={"state": "ok"},
            **kwargs,
        )
    )


def test_replay_first_write_and_workspace_owner_scoping(db_uri):
    store = UsageEvidenceStore(db_uri)
    first = event()
    assert store.insert_events(OWNER, [first]) == {"accepted": 1, "duplicates": 0}
    changed = first.model_copy(update={"data": {"state": "unreachable"}})
    assert store.insert_events(OWNER, [changed, event("two")]) == {"accepted": 1, "duplicates": 1}
    service = UsageEvidenceService(store)
    assert service.read_events(OWNER)["events"][1]["data"] == {"state": "ok"}
    assert service.read_events("other@example.test")["stored"] == 0
    with workspace_scope(9):
        assert service.read_events(OWNER)["stored"] == 0
        assert store.insert_events(OWNER, [first])["accepted"] == 1
    assert service.read_events(OWNER)["stored"] == 2
    # Sidecars must never create activity or account reports.
    with get_or_create_engine(db_uri).connect() as connection:
        assert connection.execute(select(SqlUsageHour)).first() is None
    assert not any(
        key.startswith("usage.account.") for key in UsageSnapshotStore(db_uri).read(OWNER)
    )


def test_status_replacement_source_wide_and_freshness(db_uri):
    store = UsageEvidenceStore(db_uri)
    now = now_epoch()
    store.upsert_status(
        OWNER,
        [
            SourceStatus(source="reader", state="ok", fresh_until=now - 1),
            SourceStatus(account="a", source="reader", state="unknown"),
        ],
    )
    service = UsageEvidenceService(store)
    rows = service.read_sources(OWNER)["sources"]
    assert rows[0]["account"] is None and rows[0]["current"] is False
    assert service.read_sources(OWNER, "a")["sources"][0]["current"] is None
    store.upsert_status(
        OWNER, [SourceStatus(account="a", source="reader", state="ok", fresh_until=now + 100)]
    )
    assert service.read_sources(OWNER, "a")["sources"][0]["current"] is True
    with workspace_scope(9):
        assert service.read_sources(OWNER)["sources"] == []
    assert service.read_sources("other@example.test")["sources"] == []


def test_stable_pagination_ties_filters_and_bad_cursor(db_uri):
    store = UsageEvidenceStore(db_uri)
    store.insert_events(
        OWNER, [event(str(index), subject="a" if index % 2 else "b") for index in range(7)]
    )
    service = UsageEvidenceService(store)
    cursor = None
    seen = []
    while True:
        page = service.read_events(OWNER, before=cursor, limit=2)
        seen.extend(row["id"] for row in page["events"])
        assert page["stored"] == 7 and page["history_complete"] is False
        cursor = page["next_before"]
        if cursor is None:
            break
    assert seen == ["6", "5", "4", "3", "2", "1", "0"]
    assert service.read_events(OWNER, account="a")["stored"] == 3
    assert service.read_events(OWNER, severity="warn")["stored"] == 0
    for cursor in ("bad", "e30=", "W251bGwsMSwyXQ=="):
        with pytest.raises(ValueError, match="cursor"):
            service.read_events(OWNER, before=cursor)


def test_account_owner_removal_and_retention(db_uri):
    store = UsageEvidenceStore(db_uri)
    store.insert_events(OWNER, [event("wide"), event("account", subject="a")])
    store.upsert_status(
        OWNER,
        [
            SourceStatus(source="wide", state="ok"),
            SourceStatus(account="a", source="reader", state="ok"),
        ],
    )
    UsageHoursStore(db_uri).delete_account(OWNER, "a")
    assert store.read_events(OWNER)[2] == 1
    assert len(store.read_sources(OWNER)) == 1
    cutoff = now_epoch() + 1
    assert store.prune({"usage_source_events": cutoff, "usage_source_status": cutoff}) == {
        "usage_source_events": 1,
        "usage_source_status": 1,
    }
    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("alice", "fixture-hash")
    store.insert_events("alice", [event()])
    store.upsert_status("alice", [SourceStatus(source="reader", state="ok")])
    assert accounts.delete_user("alice") is True
    assert store.read_events("alice")[2] == 0
    assert store.read_sources("alice") == []


def test_concurrent_replay_has_one_winner(db_uri):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: UsageEvidenceStore(db_uri).insert_events(OWNER, [event()]), range(2)
            )
        )
    assert sum(result["accepted"] for result in results) == 1
    assert sum(result["duplicates"] for result in results) == 1


def test_identity_case_is_significant(db_uri):
    store = UsageEvidenceStore(db_uri)
    assert store.insert_events(OWNER, [event("case"), event("CASE")])["accepted"] == 2


def test_daily_prune_covers_workspaces_and_observation_fallback(db_uri):
    store = UsageEvidenceStore(db_uri)
    service = UsageEvidenceService(store, retention_days=30)
    old = now_epoch() - 31 * 86400
    for workspace in (0, 9):
        with workspace_scope(workspace):
            store.insert_events(
                OWNER,
                [
                    event("old").model_copy(
                        update={"time": datetime.fromtimestamp(old, UTC).isoformat()}
                    )
                ],
            )
            store.upsert_status(
                OWNER,
                [
                    SourceStatus(source="old", state="ok", observed_at=old),
                    SourceStatus(source="new", state="ok"),
                ],
            )
    service.prune()
    for workspace in (0, 9):
        with workspace_scope(workspace):
            assert store.read_events(OWNER)[2] == 0
            assert [row["source"] for row in store.read_sources(OWNER)] == ["new"]
