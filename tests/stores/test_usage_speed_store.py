"""Speed replay guards, aggregation and lifecycle on each database backend."""

from dataclasses import replace

from omnigent.db.db_models import workspace_scope
from omnigent.db.utils import now_epoch
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.usage_evidence import UsageEvidenceService
from omnigent.stores.usage_evidence_store import UsageEvidenceStore
from omnigent.stores.usage_hours_store import UsageHoursStore
from omnigent.usage.speed import SpeedPoint

OWNER = "owner@example.test"


def point(**changes):
    end = now_epoch() // 3600 * 3600
    return replace(
        SpeedPoint(
            "a",
            "model-a",
            "ttft",
            end - 3600,
            end,
            "s",
            [0.1, 1.0],
            [1, 2, 0],
            3,
            "complete",
            "valid samples",
        ),
        **changes,
    )


def test_speed_replay_guard_correction_and_scope(db_uri):
    store = UsageEvidenceStore(db_uri)
    p = point()
    assert store.upsert_speed(OWNER, [p, p]) == 0
    assert store.upsert_speed(OWNER, [replace(p, count=2, bucket_counts=[1, 1, 0])]) == 1
    assert store.upsert_speed(OWNER, [replace(p, coverage="partial")]) == 1
    rows = store.read_speed(OWNER, p.start_utc, p.end_utc)
    assert len(rows) == 1 and rows[0]["count"] == 3
    assert store.read_speed("other@example.test", p.start_utc, p.end_utc) == []
    with workspace_scope(9):
        assert store.read_speed(OWNER, p.start_utc, p.end_utc) == []
        store.upsert_speed(OWNER, [p])
    assert (
        store.upsert_speed(OWNER, [replace(p, count=1, bucket_counts=[0, 1, 0], correction=True)])
        == 0
    )
    assert store.read_speed(OWNER, p.start_utc, p.end_utc)[0]["count"] == 1


def test_speed_aggregate_bounds_requests_and_account_filter(db_uri):
    store = UsageEvidenceStore(db_uri)
    p = point()
    store.upsert_speed(
        OWNER,
        [
            p,
            replace(p, metric="tpot"),
            replace(
                p, metric="requests", unit="{request}", bounds=None, bucket_counts=None, count=4
            ),
        ],
    )
    service = UsageEvidenceService(store)
    [row] = service.read_speed(OWNER)["rows"]
    assert row["requests"] == 4
    assert row["ttft"]["count"] == row["tpot"]["count"] == 3
    store.upsert_speed(
        OWNER,
        [
            replace(p, account_id="b", coverage="partial"),
            replace(p, start_utc=p.start_utc - 3600, end_utc=p.end_utc - 3600, bounds=[0.2, 1.0]),
        ],
    )
    [row] = service.read_speed(OWNER)["rows"]
    assert (row["hours"], row["complete_hours"], row["partial_hours"]) == (1, 1, 0)
    # Account a's earlier interval selects a different schema, excluding both later intervals.
    assert row["ttft"]["explicit_bounds"] == [0.2, 1.0]
    assert len(row["excluded_hours"]) == 2
    assert row["requests"] is None
    [a] = service.read_speed(OWNER, account="a", model="model-a")["rows"]
    assert a["ttft"]["count"] == 3
    assert service.read_speed(OWNER, model="absent")["rows"] == []
    store.upsert_speed(
        OWNER,
        [replace(p, start_utc=p.start_utc - 3600, end_utc=p.end_utc - 3600, correction=True)],
    )
    [row] = service.read_speed(OWNER)["rows"]
    assert row["ttft"]["bucket_counts"] == [3, 6, 0]
    assert row["tpot"]["count"] == 3
    assert row["hours"] == 3
    assert (row["complete_hours"], row["partial_hours"]) == (2, 1)
    assert row["requests"] is None
    assert len(service.read_speed(OWNER, by="account")["rows"]) == 2


def test_speed_removal_and_retention(db_uri):
    store = UsageEvidenceStore(db_uri)
    p = point()
    store.upsert_speed(OWNER, [p, replace(p, account_id="b")])
    UsageHoursStore(db_uri).delete_account(OWNER, "a")
    assert store.read_speed(OWNER, p.start_utc, p.end_utc)[0]["account_id"] == "b"
    assert store.prune({"usage_speed_points": p.end_utc + 1}) == {"usage_speed_points": 1}
    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("alice", "fixture-hash")
    store.upsert_speed("alice", [p])
    assert accounts.delete_user("alice")
    assert store.read_speed("alice", p.start_utc, p.end_utc) == []
