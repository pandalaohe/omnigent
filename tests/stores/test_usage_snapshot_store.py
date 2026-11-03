"""Usage snapshots share preference rows without entering the settings envelope."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.usage_snapshot_store import UsageSnapshotStore


def test_snapshot_round_trip_is_owner_scoped_and_delete_is_exact(db_uri):
    store = UsageSnapshotStore(db_uri)
    first = {"schema_version": 1, "report": {"windows": []}}
    store.write("owner@example.test", "usage.account.gw", first)
    store.write("owner@example.test", "usage.account.other", {"schema_version": 1})
    store.write("other@example.test", "usage.account.gw", {"schema_version": 1})

    assert store.read("owner@example.test")["usage.account.gw"] == first
    store.delete("owner@example.test", "usage.account.gw")
    assert "usage.account.gw" not in store.read("owner@example.test")
    assert "usage.account.other" in store.read("owner@example.test")
    assert "usage.account.gw" in store.read("other@example.test")


def test_usage_rows_do_not_become_user_settings(db_uri):
    store = UsageSnapshotStore(db_uri)
    store.write("owner@example.test", "usage.account.gw", {"schema_version": 1})
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        rows = session.scalars(
            select(SqlPreference).where(
                SqlPreference.workspace_id == current_workspace_id(),
                SqlPreference.user_id == "owner@example.test",
            )
        ).all()
    assert [(row.key, row.value) for row in rows] == [("usage.account.gw", '{"schema_version":1}')]
