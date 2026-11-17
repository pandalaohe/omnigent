"""Evidence replay, isolation, deletion and retention on the selected backend."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, update

from omnigent.db.db_models import SqlUsageHour, SqlUsageSourceStatus, workspace_scope
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


@pytest.mark.parametrize("confirmation_age_days", [0, 31], ids=["recently_confirmed", "expired"])
def test_daily_prune_covers_workspaces_and_last_confirmation(db_uri, confirmation_age_days):
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
            with get_or_create_engine(db_uri).begin() as connection:
                connection.execute(
                    update(SqlUsageSourceStatus)
                    .where(
                        SqlUsageSourceStatus.workspace_id == workspace,
                        SqlUsageSourceStatus.owner_id == OWNER,
                        SqlUsageSourceStatus.source == "old",
                    )
                    .values(updated_at=now_epoch() - confirmation_age_days * 86400)
                )
    service.prune()
    for workspace in (0, 9):
        with workspace_scope(workspace):
            assert store.read_events(OWNER)[2] == 0
            expected = ["new", "old"] if confirmation_age_days == 0 else ["new"]
            assert [row["source"] for row in store.read_sources(OWNER)] == expected


def test_episode_pairing_across_pages_and_out_of_order_arrival(db_uri):
    store = UsageEvidenceStore(db_uri)
    service = UsageEvidenceService(store)
    now = now_epoch()

    def change(id, at, severity, **values):
        return event(id).model_copy(
            update={
                "time": datetime.fromtimestamp(at, UTC).isoformat(),
                "severity": severity,
                "type": "connection",
                **values,
            }
        )

    recovery = change("recover", now, "recover")
    store.insert_events(OWNER, [recovery])
    assert service.read_events(OWNER)["events"][0]["episode"] is None
    fault = change("fault", now - 60, "error")
    others = [change(str(i), now - i - 1, "info") for i in range(5)]
    store.insert_events(OWNER, [fault, *others])
    page1 = service.read_events(OWNER, limit=6)
    assert len(page1["events"]) == 5
    assert "recover" not in {row["id"] for row in page1["events"]}
    page2 = service.read_events(OWNER, before=page1["next_before"], limit=6)
    assert page2["events"][0]["episode"] == {
        "recovered_at": now,
        "recover_event_id": "recover",
    }
    assert page2["events"][0]["time"] == now - 60
    store.insert_events(
        OWNER,
        [
            change("other-type", now - 61, "error", type="tier_day"),
            change("other-account", now - 61, "error", subject="different"),
            change("other-source", now - 61, "error", source="/different"),
        ],
    )
    rows = {row["id"]: row for row in service.read_events(OWNER)["events"]}
    assert all(
        rows[key]["episode"] is None for key in ("other-type", "other-account", "other-source")
    )
    filtered = service.read_events(OWNER, kind="disconnect", limit=2)
    assert all(row["type"] == "connection" for row in filtered["events"])
    assert filtered["next_before"] is not None


@pytest.mark.parametrize("fault_type", ["connection", "Connection"])
def test_episode_types_preserve_case_and_matching_type_pairs_across_pages(db_uri, fault_type):
    store = UsageEvidenceStore(db_uri)
    service = UsageEvidenceService(store)
    now = now_epoch()
    fault = event("fault").model_copy(
        update={
            "type": fault_type,
            "severity": "error",
            "time": datetime.fromtimestamp(now - 60, UTC).isoformat(),
        }
    )
    other = event("other-case-recovery").model_copy(
        update={
            "type": "Connection" if fault_type == "connection" else "connection",
            "severity": "recover",
            "time": datetime.fromtimestamp(now - 30, UTC).isoformat(),
        }
    )
    store.insert_events(OWNER, [fault, other])
    rows = {row["id"]: row for row in service.read_events(OWNER)["events"]}
    assert set(rows) == {"fault", "other-case-recovery"}
    assert rows["fault"]["episode"] is None

    matching = event("matching-recovery").model_copy(
        update={
            "type": fault_type,
            "severity": "recover",
            "time": datetime.fromtimestamp(now, UTC).isoformat(),
        }
    )
    store.insert_events(OWNER, [matching])
    page1 = service.read_events(OWNER, limit=2)
    assert [row["id"] for row in page1["events"]] == ["other-case-recovery"]
    page2 = service.read_events(OWNER, before=page1["next_before"], limit=2)
    assert page2["events"][0]["id"] == "fault"
    assert page2["events"][0]["episode"] == {
        "recovered_at": now,
        "recover_event_id": "matching-recovery",
    }


def test_report_log_import_replays_same_identity(db_uri):
    from omnigent.usage.source_events import report_log_event

    snapshots = UsageSnapshotStore(db_uri)
    entry = {"kind": "read_failed", "at": float(now_epoch()), "section": "windows"}
    snapshots.write(
        OWNER,
        "usage.account.a",
        {
            "schema_version": 1,
            "account_id": "a",
            "report_log": [entry],
        },
    )
    service = UsageEvidenceService(UsageEvidenceStore(db_uri))
    service.import_report_logs()
    service.import_report_logs()
    assert service.read_events(OWNER)["stored"] == 1
    row = service.read_events(OWNER)["events"][0]
    assert row["id"] == report_log_event("a", entry).id
    assert row["severity"] == "error" and row["type"] == "read_failed"
    assert row["data"] == entry


def test_quota_tier_changes_are_durable_and_edge_triggered(db_uri):
    from omnigent.quota_pace import QuotaTiers
    from omnigent.usage.contract import UsageAccountView, UsageWindowView

    store = UsageEvidenceStore(db_uri)
    account = UsageAccountView(
        id="a", kind="account", display_name="A", state="ok", freshness="fresh"
    )
    now = now_epoch()
    for remaining in (60, 40, 40, 10, 10, 60, 60):
        account.windows = [
            UsageWindowView(
                id="day", label="Day", kind="rolling", state="ok", remaining_pct=remaining
            )
        ]
        store.record_quota_tiers(OWNER, [account], QuotaTiers(), now)
        now += 1
    rows = UsageEvidenceService(store).read_events(OWNER)["events"]
    assert [row["severity"] for row in rows] == ["error", "warn"]
    assert rows[0]["episode"] is not None
    assert store.read_events(OWNER)[2] == 3
    # A new store instance sees the committed normal state, so reload adds nothing.
    UsageEvidenceStore(db_uri).record_quota_tiers(OWNER, [account], QuotaTiers(), now)
    assert store.read_events(OWNER)[2] == 3


def test_host_registry_disconnect_reconnect_archives_one_episode(db_uri):
    from omnigent.host.frames import HostHelloFrame
    from omnigent.server.host_registry import HostRegistry

    service = UsageEvidenceService(UsageEvidenceStore(db_uri))
    registry = HostRegistry()
    registry.connection_observer = service.host_connection
    hello = HostHelloFrame(name="Workstation", version="0.1.0", frame_protocol_version=1)
    first = registry.register("a" * 32, None, hello, OWNER)
    assert registry.deregister(first.host_id, conn=first)
    second = registry.register("a" * 32, None, hello, OWNER)
    assert not registry.deregister(first.host_id, conn=first)
    # A live socket replacement is not another connectivity transition.
    registry.register("a" * 32, None, hello, OWNER)
    page = service.read_events(OWNER, kind="disconnect")
    assert page["stored"] == 3
    errors = [row for row in page["events"] if row["severity"] == "error"]
    assert len(errors) == 1 and errors[0]["episode"] is not None
    assert second.host_id == first.host_id
