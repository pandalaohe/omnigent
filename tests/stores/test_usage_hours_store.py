"""The hourly usage table: increments, activity replacement, fill, isolation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from omnigent.llms.context_window import ModelPricing
from omnigent.stores.usage_hours_store import (
    TokenGrowth,
    UsageHoursRecorder,
    UsageHoursStore,
    hours_of,
)
from omnigent.usage.contract import ActivityEntryV2, ActivityHour

# Two hours ago: a pushed ``activity`` hour must be in the past to be kept.
HOUR = hours_of(int(datetime.now(UTC).timestamp())) - 7200
OTHER_HOUR = HOUR + 3600


@pytest.mark.parametrize(
    "figure",
    [
        "uncached_input_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "tokens",
        "requests",
        "failed",
        "cost_usd",
    ],
)
def test_hostless_accounts_require_figures_and_follow_owner_workspace_and_removal(db_uri, figure):
    from omnigent.db.db_models import workspace_scope

    store = UsageHoursStore(db_uri)
    for owner in ("a@example.test", "b@example.test"):
        store.replace_rows(owner, "plan", [ActivityEntryV2(hour=HOUR, **{figure: 0})])
    store.replace_rows(
        "a@example.test", "marker", [ActivityEntryV2(hour=HOUR, coverage="partial")]
    )
    store.replace_rows(
        "a@example.test", "hosted", [ActivityEntryV2(hour=HOUR, host="h", tokens=1)]
    )
    assert store.hostless_accounts("a@example.test") == {"plan"}
    with workspace_scope(9):
        assert store.hostless_accounts("a@example.test") == set()
    store.replace_rows("a@example.test", "plan", [ActivityEntryV2(hour=HOUR, removed=True)])
    assert store.hostless_accounts("a@example.test") == set()
    assert store.hostless_accounts("b@example.test") == {"plan"}


def test_add_turn_accumulates_one_row_per_model_and_hour(db_uri):
    store = UsageHoursStore(db_uri)
    store.add_turn(
        "owner@example.test",
        "native:codex",
        HOUR,
        "model-a",
        "host-1",
        "metered",
        True,
        100,
        10,
        5,
        20,
        120,
        0.5,
    )
    store.add_turn(
        "owner@example.test",
        "native:codex",
        HOUR,
        "model-a",
        "host-1",
        "metered",
        True,
        1,
        2,
        3,
        4,
        5,
        0.25,
    )
    store.add_turn(
        "owner@example.test",
        "native:codex",
        HOUR,
        "model-b",
        "host-1",
        "metered",
        True,
        7,
        0,
        0,
        3,
        10,
        0.1,
    )

    rows = store.read("owner@example.test", HOUR, HOUR + 1)
    assert [(row.model, row.cost_usd) for row in rows] == [("model-a", 0.75), ("model-b", 0.1)]
    first = rows[0]
    assert (first.input_tokens, first.cached_tokens, first.cache_write_tokens) == (101, 12, 8)
    assert (first.output_tokens, first.total_tokens) == (24, 125)


def test_rows_are_owner_and_account_scoped(db_uri):
    store = UsageHoursStore(db_uri)
    for owner in ("a@example.test", "b@example.test"):
        store.add_turn(
            owner, "native:codex", HOUR, "m", "h", "metered", True, 10, 0, 0, 1, 11, 0.1
        )
    store.add_turn("a@example.test", "gw", HOUR, "m", "", "metered", True, 10, 0, 0, 1, 11, 0.2)

    assert len(store.read("a@example.test", HOUR, HOUR + 1)) == 2
    assert [row.account_id for row in store.read("a@example.test", HOUR, HOUR + 1, "gw")] == ["gw"]
    # A range is [since, until): a row at the bound is the next span's.
    assert store.read("a@example.test", HOUR + 1, HOUR + 7200) == []


def test_empty_turn_creates_no_row(db_uri):
    store = UsageHoursStore(db_uri)
    store.add_turn(
        "a@example.test", "native:codex", HOUR, "m", "h", "metered", True, 0, 0, 0, 0, 0, 0.0
    )
    assert store.read("a@example.test", HOUR, HOUR + 1) == []


def test_otlp_adds_nullable_buckets_requests_and_failures(db_uri):
    from omnigent.stores.usage_hours_store import UsageBuckets

    store = UsageHoursStore(db_uri)
    for buckets, requests, failed, cost in (
        (UsageBuckets(output_tokens=20, total_tokens=120), 1, 1, 0.25),
        (UsageBuckets(30, 60, 10, 20, 120, 5), 1, 0, 0.25),
        (UsageBuckets(), None, None, 0.5),
    ):
        store.add_activity(
            "a@example.test",
            "gateway",
            HOUR,
            "m",
            "otel:",
            "metered",
            True,
            buckets,
            requests,
            failed,
            cost,
        )
    [row] = store.read("a@example.test", HOUR, HOUR + 3600)
    assert (row.input_tokens, row.cached_tokens, row.cache_write_tokens) == (30, 60, 10)
    assert (row.output_tokens, row.total_tokens, row.reasoning_tokens) == (40, 240, 5)
    assert (row.requests, row.failed, row.cost_usd) == (2, 1, 1.0)


def test_unpriced_turn_keeps_its_token_split(db_uri):
    store = UsageHoursStore(db_uri)
    store.add_turn(
        "a@example.test",
        "native:codex",
        HOUR,
        "unpriced",
        "h",
        "metered",
        False,
        500,
        0,
        0,
        100,
        600,
        0.0,
    )
    [row] = store.read("a@example.test", HOUR, HOUR + 1)
    assert row.priced is False
    assert (row.input_tokens, row.output_tokens, row.cost_usd) == (500, 100, 0.0)


def test_replace_activity_is_last_write_wins(db_uri):
    store = UsageHoursStore(db_uri)
    store.replace_activity(
        "a@example.test",
        "gw",
        [ActivityHour(hour=HOUR, model="m", requests=10, failed=1, tokens=100, cost_usd=0.5)],
    )
    store.replace_activity(
        "a@example.test",
        "gw",
        [ActivityHour(hour=HOUR, model="m", requests=12, failed=2, tokens=140, cost_usd=0.7)],
    )

    [row] = store.read("a@example.test", HOUR, HOUR + 1)
    assert (row.requests, row.failed, row.total_tokens, row.cost_usd) == (12, 2, 140, 0.7)
    assert row.basis == "metered"
    assert row.priced is True


def test_replace_activity_keeps_distinct_models_apart(db_uri):
    store = UsageHoursStore(db_uri)
    store.replace_activity(
        "a@example.test",
        "gw",
        [
            ActivityHour(hour=HOUR, model="m1", tokens=10, cost_usd=0.1),
            ActivityHour(hour=HOUR, model="m2", tokens=20, cost_usd=0.2),
        ],
    )
    store.replace_activity(
        "a@example.test", "gw", [ActivityHour(hour=HOUR, model="m1", tokens=11, cost_usd=0.11)]
    )
    rows = {row.model: row for row in store.read("a@example.test", HOUR, HOUR + 1)}
    assert (rows["m1"].total_tokens, rows["m2"].total_tokens) == (11, 20)


def test_replace_activity_drops_rows_outside_the_report_bound(db_uri):
    store = UsageHoursStore(db_uri)
    store.replace_activity(
        "a@example.test",
        "gw",
        [
            ActivityHour(hour=HOUR - 401 * 86_400, model="old", tokens=10, cost_usd=0.1),
            ActivityHour(hour=HOUR + 86_400, model="future", tokens=10, cost_usd=0.1),
        ],
    )
    assert store.read("a@example.test", 0, HOUR + 2 * 86_400) == []


def test_replace_activity_estimate_basis_is_kept(db_uri):
    store = UsageHoursStore(db_uri)
    store.replace_activity(
        "a@example.test",
        "gw",
        [ActivityHour(hour=HOUR, model="m", tokens=10, cost_usd=0.1, cost_basis="estimate")],
    )
    [row] = store.read("a@example.test", HOUR, HOUR + 1)
    assert row.basis == "list"


def test_v1_missing_tokens_keep_zero_buckets_and_do_not_hide_known_totals(db_uri):
    from omnigent.usage.activity import totals

    store = UsageHoursStore(db_uri)
    store.replace_activity(
        "a@example.test",
        "gw",
        [
            ActivityHour(hour=HOUR, model="requests-only", requests=3),
            ActivityHour(hour=HOUR, model="known", tokens=100),
        ],
    )
    rows = store.read("a@example.test", HOUR, HOUR + 1)
    counts = {r.model: r for r in rows}
    unknown = counts["requests-only"]
    assert (
        unknown.input_tokens,
        unknown.cached_tokens,
        unknown.cache_write_tokens,
        unknown.output_tokens,
        unknown.total_tokens,
    ) == (0, 0, 0, 0, 0)
    assert counts["known"].total_tokens == 100
    assert totals(rows)["tokens"] == 100


def test_v1_lower_reread_replaces_complete_and_ignores_new_coverage_fields(db_uri):
    store = UsageHoursStore(db_uri)
    for tokens in (100, 70):
        store.replace_activity(
            "a@example.test",
            "gw",
            [
                ActivityHour.model_validate(
                    {
                        "hour": HOUR,
                        "tokens": tokens,
                        "coverage": "partial",
                    }
                )
            ],
        )
    assert store.read("a@example.test", HOUR, HOUR + 1)[0].total_tokens == 70


def test_activity_turn_does_not_erase_reported_counts(db_uri):
    store = UsageHoursStore(db_uri)
    store.replace_activity(
        "a@example.test",
        "gw",
        [ActivityHour(hour=HOUR, model="m", requests=10, failed=1, tokens=100, cost_usd=0.5)],
    )
    store.add_turn("a@example.test", "gw", HOUR, "m", "", "metered", True, 50, 0, 0, 5, 55, 0.25)
    [row] = store.read("a@example.test", HOUR, HOUR + 1)
    assert (row.requests, row.failed) == (10, 1)
    assert (row.total_tokens, row.cost_usd) == (155, 0.75)


def _pricing(input_per_million: float, output_per_million: float) -> ModelPricing:
    return ModelPricing(
        input_per_token=input_per_million / 1_000_000,
        output_per_token=output_per_million / 1_000_000,
    )


def test_fill_prices_a_closed_unpriced_row_once(db_uri):
    store = UsageHoursStore(db_uri)
    store.add_turn(
        "a@example.test",
        "native:codex",
        HOUR,
        "m",
        "h",
        "metered",
        False,
        1_000_000,
        0,
        0,
        0,
        1_000_000,
        0.0,
    )

    assert store.fill_unpriced(lambda model: None, HOUR + 3600) == 0
    assert store.fill_unpriced(lambda model: _pricing(1.0, 2.0), HOUR + 3600) == 1
    [row] = store.read("a@example.test", HOUR, HOUR + 1)
    assert row.priced is True
    assert row.cost_usd == pytest.approx(1.0)
    assert row.input_tokens == 1_000_000
    # A retry finds nothing left, and a later price does not touch it.
    assert store.fill_unpriced(lambda model: _pricing(9.0, 9.0), HOUR + 3600) == 0
    [unchanged] = store.read("a@example.test", HOUR, HOUR + 1)
    assert unchanged.cost_usd == pytest.approx(1.0)


def test_fill_merges_into_an_existing_priced_row(db_uri):
    store = UsageHoursStore(db_uri)
    store.add_turn(
        "a@example.test", "native:codex", HOUR, "m", "h", "metered", True, 10, 0, 0, 1, 11, 0.5
    )
    store.add_turn(
        "a@example.test",
        "native:codex",
        HOUR,
        "m",
        "h",
        "metered",
        False,
        1_000_000,
        0,
        0,
        0,
        1_000_000,
        0.0,
    )

    assert store.fill_unpriced(lambda model: _pricing(1.0, 2.0), HOUR + 3600) == 1
    rows = store.read("a@example.test", HOUR, HOUR + 1)
    [priced] = [row for row in rows if row.priced]
    assert priced.cost_usd == pytest.approx(1.5)
    assert priced.input_tokens == 1_000_010
    assert all(row.priced for row in rows)


def test_fill_leaves_the_current_hour_and_split_less_rows_alone(db_uri):
    store = UsageHoursStore(db_uri)
    store.add_turn(
        "a@example.test",
        "native:codex",
        HOUR,
        "m",
        "h",
        "metered",
        False,
        1_000_000,
        0,
        0,
        0,
        1_000_000,
        0.0,
    )
    store.replace_activity(
        "a@example.test",
        "gw",
        [ActivityHour(hour=HOUR, model="m", tokens=1000, cost_usd=None)],
    )

    # Nothing is closed before the row's own hour.
    assert store.fill_unpriced(lambda model: _pricing(1.0, 2.0), HOUR) == 0
    assert store.fill_unpriced(lambda model: _pricing(1.0, 2.0), OTHER_HOUR) == 1
    rows = store.read("a@example.test", HOUR, HOUR + 1)
    assert {row.account_id: row.priced for row in rows} == {"native:codex": True, "gw": False}


def test_delete_account_removes_only_that_account(db_uri):
    store = UsageHoursStore(db_uri)
    store.add_turn(
        "a@example.test", "native:codex", HOUR, "m", "h", "metered", True, 10, 0, 0, 1, 11, 0.1
    )
    store.add_turn("a@example.test", "gw", HOUR, "m", "", "metered", True, 10, 0, 0, 1, 11, 0.1)

    store.delete_account("a@example.test", "gw")
    assert [row.account_id for row in store.read("a@example.test", HOUR, HOUR + 1)] == [
        "native:codex"
    ]


def test_basis_resolution_freezes_the_host_login_kind():
    kinds = {"claude": "subscription", "codex": "api_key", "pi": "providers"}
    recorder = UsageHoursRecorder(
        UsageHoursStore("sqlite://"),
        login_kind=lambda host_id, agent_key: kinds.get(agent_key or ""),
    )
    # The lookup is keyed by the harness's agent key, and the login kind maps to
    # the accounting basis frozen on the row.
    assert recorder.basis_for("host-1", "claude-native") == "subscription"
    assert recorder.basis_for("host-1", "codex-native") == "metered"
    assert recorder.basis_for("host-1", "pi-native") == "metered"
    # No host, no harness, an unknown login and a failing probe all leave the
    # figure out of the money total rather than inflating it.
    assert recorder.basis_for("", "codex-native") == "unknown"
    assert recorder.basis_for("host-1", None) == "unknown"
    assert recorder.basis_for("host-1", "opencode-native") == "unknown"

    def _boom(host_id, agent_key):
        raise RuntimeError("registry down")

    broken = UsageHoursRecorder(UsageHoursStore("sqlite://"), login_kind=_boom)
    assert broken.basis_for("host-1", "codex-native") == "unknown"


def test_recorder_is_inert_without_a_store_and_when_disabled(db_uri):
    store = UsageHoursStore(db_uri)
    disabled = UsageHoursRecorder(store, enabled=False)
    disabled.add_growth(
        owner="a@example.test",
        account_id="native:codex",
        model="m",
        host_id="h",
        harness="codex-native",
        growth=TokenGrowth(input_tokens=10, total_tokens=10),
        cost_usd=0.1,
        priced=True,
    )
    assert store.read("a@example.test", 0, 4_000_000_000) == []
