"""Retained quota readings are independent of editable payment baselines."""

from omnigent.db.db_models import workspace_scope
from omnigent.stores.usage_quota_closings import UsageQuotaClosingStore
from omnigent.usage.contract import UsageWindow


def test_closest_real_observation_and_poll_freshness(db_uri):
    store = UsageQuotaClosingStore(db_uri)
    reset = 1794000000
    window = UsageWindow(id="weekly", resets_at=reset, used_value=40, limit_value=100, unit="USD")
    store.record(
        "owner", "account", [window], observed_at=reset - 120, source="reader", polling_seconds=60
    )
    store.record(
        "owner",
        "account",
        [window.model_copy(update={"used_value": 70})],
        observed_at=reset - 60,
        source="reader",
        polling_seconds=60,
    )
    store.record(
        "owner", "account", [window], observed_at=reset - 100, source="reader", polling_seconds=60
    )
    store.record(
        "owner", "account", [window], observed_at=reset + 1, source="reader", polling_seconds=60
    )
    rows = store.read("owner", reset - 1000, reset + 10, as_of=reset + 10)
    assert len(rows) == 1
    assert rows[0]["observed_at"] == reset - 60
    assert rows[0]["evidence"]["remaining_value"] == "30"
    assert rows[0]["remaining"]["state"] == "range"
    assert rows[0]["remaining"]["high"] == "30"
    assert store.read("other", 0, reset + 10, as_of=reset + 10) == []
    with workspace_scope(2):
        assert store.read("owner", 0, reset + 10, as_of=reset + 10) == []


def test_missing_poll_and_stale_remain_unknown(db_uri):
    store = UsageQuotaClosingStore(db_uri)
    for account, poll, lag in [("missing", None, 10), ("stale", 60, 181)]:
        store.record(
            "owner",
            account,
            [UsageWindow(id="window", percent_used=20, resets_at=1794000000)],
            observed_at=1794000000 - lag,
            source=None,
            polling_seconds=poll,
        )
    assert all(
        row["remaining"]["state"] == "unknown"
        for row in store.read("owner", 0, 1794000001, as_of=1794000001)
    )
