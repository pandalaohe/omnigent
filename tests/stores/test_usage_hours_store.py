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


@pytest.mark.parametrize(
    "dialect_name", ["postgresql+psycopg", "cockroachdb+psycopg", "mysql+pymysql", "sqlite"]
)
def test_upsert_cost_fallback_binds_as_float(dialect_name):
    import re
    from unittest.mock import Mock

    from sqlalchemy import Float, create_mock_engine

    if dialect_name.startswith("cockroachdb"):
        pytest.importorskip("sqlalchemy_cockroachdb")
    engine = create_mock_engine(
        f"{dialect_name}://", lambda *args, **kwargs: None, paramstyle="named"
    )
    session = Mock()
    session.get_bind.return_value = engine
    store = UsageHoursStore("sqlite://")
    store._upsert(
        session,
        "owner@example.test",
        "native:codex",
        HOUR,
        "m",
        "h",
        "metered",
        True,
        TokenGrowth(input_tokens=10, total_tokens=10),
        requests=None,
        failed=None,
        reasons=None,
        cost_usd=0.25,
        increment=True,
    )
    statement = session.execute.call_args.args[0]
    compiled = statement.compile(dialect=engine.dialect)
    fallback = re.search(r"coalesce\(usage_hours\.cost_usd, :(\w+)", str(compiled))
    assert fallback is not None
    parameter = fallback.group(1)
    assert isinstance(compiled.binds[parameter].type, Float)
    assert type(compiled.params[parameter]) is float


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


def test_report_correction_and_prune_share_the_cutoff_hour(db_uri, monkeypatch):
    from omnigent.stores.usage_hours_store import _ACTIVITY_MAX_AGE_S

    store = UsageHoursStore(db_uri)
    owner = "owner@example.test"
    cutoff = int(datetime(2025, 1, 1, tzinfo=UTC).timestamp())
    expired = cutoff - 3600
    now = cutoff
    monkeypatch.setattr("omnigent.stores.usage_hours_store.now_epoch", lambda: now)
    entries = [ActivityEntryV2(hour=hour, tokens=100) for hour in (expired, cutoff)]
    assert store.replace_rows(owner, "plan", entries).applied == 2

    now = cutoff + _ACTIVITY_MAX_AGE_S + 3599
    assert store.prune_hours(now) == 1
    corrections = [
        entry.model_copy(update={"tokens": 80, "correction": True}) for entry in entries
    ]
    outcome = store.replace_rows(owner, "plan", corrections)
    assert outcome.applied == 1, "cutoff-hour correction must be accepted"
    assert [(entry.hour, entry.reason) for entry in outcome.skipped] == [(expired, "out_of_range")]
    [row] = store.read(owner, expired, cutoff + 3600)
    assert (row.hour_utc, row.total_tokens) == (cutoff, 80)


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


@pytest.mark.parametrize(
    "first,second,expected",
    [
        (None, None, None),
        ("builtin", "builtin", "builtin"),
        (None, "builtin", "mixed"),
        ("import:v1", "import:v2", "mixed"),
        ("mixed", "builtin", "mixed"),
    ],
)
def test_native_price_refs_merge_without_relabelling_history(db_uri, first, second, expected):
    store = UsageHoursStore(db_uri)
    args = (
        "owner@example.test",
        "native:codex",
        HOUR,
        "model-a",
        "host-a",
        "subscription",
        True,
        1,
        0,
        0,
        1,
        2,
        0.5,
    )
    store.add_turn(*args, price_ref=first)
    store.add_turn(*args, price_ref=second)
    row = store.read("owner@example.test", HOUR, OTHER_HOUR)[0]
    assert row.price_ref == expected
    assert row.cost_usd == 1


def test_closed_hour_fill_records_its_actual_price_ref(db_uri):
    store = UsageHoursStore(db_uri)
    store.add_turn(
        "owner@example.test",
        "native:codex",
        HOUR,
        "model-a",
        "host-a",
        "subscription",
        False,
        10,
        0,
        0,
        0,
        10,
        0,
    )
    assert (
        store.fill_unpriced(
            lambda model: ModelPricing(0.1, 0.2, price_ref="import:v1"), OTHER_HOUR
        )
        == 1
    )
    row = store.read("owner@example.test", HOUR, OTHER_HOUR)[0]
    assert row.price_ref == "import:v1" and row.priced


def test_agent_parts_replace_replay_remove_and_invalid_sum(db_uri):
    from omnigent.stores.usage_agent_parts import split_row
    from omnigent.usage.contract import AgentPart

    store = UsageHoursStore(db_uri)
    owner = "owner@example.test"
    entry = ActivityEntryV2(
        hour=HOUR,
        model="m",
        tokens=100,
        requests=10,
        agents=[
            AgentPart(agent="claude", tokens=30, requests=3),
            AgentPart(agent="codex", tokens=50, requests=5),
        ],
    )
    for _ in range(2):
        assert store.replace_rows(owner, "plan", [entry]).applied == 1
        [rows] = list(store.read_chunks(owner, HOUR, OTHER_HOUR))
        [row] = rows
        assert {part.agent: part.total_tokens for part in split_row(row, row.agent_parts)} == {
            "claude": 30,
            "codex": 50,
            "": 20,
        }
        assert len(row.agent_parts) == 2
    unsplit = entry.model_copy(update={"agents": None})
    store.replace_rows(owner, "plan", [unsplit])
    assert next(iter(store.read_chunks(owner, HOUR, OTHER_HOUR)))[0].agent_parts == ()
    invalid = entry.model_copy(update={"agents": [AgentPart(agent="codex", tokens=101)]})
    outcome = store.replace_rows(owner, "plan", [invalid])
    assert outcome.warnings[0].reason == "agents_over_total"
    row = next(iter(store.read_chunks(owner, HOUR, OTHER_HOUR)))[0]
    assert row.total_tokens == 100 and not row.agent_parts
    store.replace_rows(owner, "plan", [entry])
    store.replace_rows(owner, "plan", [ActivityEntryV2(hour=HOUR, model="m", removed=True)])
    assert list(store.read_chunks(owner, HOUR, OTHER_HOUR)) == []
    store.replace_rows(owner, "plan", [entry])
    store.delete_account(owner, "plan")
    from sqlalchemy import select

    from omnigent.db.db_models import SqlUsageHourAgent

    with store._read_session()("test_deleted_parts") as session:
        assert list(session.scalars(select(SqlUsageHourAgent))) == []


def test_otlp_agent_parts_increment_in_same_key(db_uri):
    from omnigent.stores.usage_hours_store import UsageBuckets

    store = UsageHoursStore(db_uri)
    for agent in ("claude", "codex", "codex"):
        store.add_activity(
            "owner@example.test",
            "otlp",
            HOUR,
            "m",
            "h",
            "list",
            True,
            UsageBuckets(total_tokens=10),
            1,
            0,
            0.25,
            agent=agent,
        )
    [row] = next(iter(store.read_chunks("owner@example.test", HOUR, OTHER_HOUR)))
    assert row.total_tokens == 30 and row.cost_usd == 0.75
    assert {part["agent"]: part["total_tokens"] for part in row.agent_parts} == {
        "claude": 10,
        "codex": 20,
    }


def test_agent_prune_preserves_cutoff_and_rejects_stale_over_total_split(db_uri):
    from sqlalchemy import update

    from omnigent.db.db_models import SqlUsageHour
    from omnigent.db.utils import run_write_transaction
    from omnigent.stores.usage_agent_parts import split_row
    from omnigent.stores.usage_hours_store import _ACTIVITY_MAX_AGE_S
    from omnigent.usage.contract import AgentPart

    store = UsageHoursStore(db_uri)
    owner = "owner@example.test"
    for hour in (HOUR, OTHER_HOUR):
        store.replace_rows(
            owner,
            "plan",
            [ActivityEntryV2(hour=hour, tokens=100, agents=[AgentPart(agent="codex", tokens=90)])],
        )

    def old_writer(session):
        session.execute(
            update(SqlUsageHour).where(SqlUsageHour.hour_utc == HOUR).values(total_tokens=50)
        )

    run_write_transaction(store._write_session(), "test_old_writer", old_writer)
    row = next(iter(store.read_chunks(owner, HOUR, OTHER_HOUR)))[0]
    assert [(part.agent, part.total_tokens) for part in split_row(row, row.agent_parts)] == [
        ("", 50)
    ]
    count = 0
    while removed := store.prune_hours(OTHER_HOUR + _ACTIVITY_MAX_AGE_S, limit=1):
        count += removed
    assert count == 2
    assert store.prune_hours(OTHER_HOUR + _ACTIVITY_MAX_AGE_S) == 0
    row = next(iter(store.read_chunks(owner, HOUR, OTHER_HOUR + 3600)))[0]
    assert row.hour_utc == OTHER_HOUR and len(row.agent_parts) == 1


def test_hour_retention_drains_bounded_batches_across_workspaces(db_uri, monkeypatch):
    from sqlalchemy import insert, select

    from omnigent.db.db_models import SqlUsageHour, SqlUsageHourAgent
    from omnigent.db.utils import run_write_transaction
    from omnigent.server.usage_evidence import UsageEvidenceService
    from omnigent.stores.usage_evidence_store import UsageEvidenceStore
    from omnigent.stores.usage_hours_store import _ACTIVITY_MAX_AGE_S

    store = UsageHoursStore(db_uri)
    evidence = UsageEvidenceService(
        UsageEvidenceStore(db_uri), retention_days=30, hours_store=store
    )
    expired = [
        {
            "workspace_id": index % 2,
            "owner_id": "owner@example.test",
            "account_id": f"plan-{index}",
            "hour_utc": HOUR,
            "updated_at": HOUR,
        }
        for index in range(2501)
    ]
    kept = [{**expired[0], "workspace_id": space, "hour_utc": OTHER_HOUR} for space in (0, 1)]

    def seed(session):
        session.execute(insert(SqlUsageHour), expired + kept)
        session.execute(
            insert(SqlUsageHourAgent), [{**row, "agent": "codex"} for row in expired + kept]
        )

    run_write_transaction(store._write_session(), "test_seed_hour_retention", seed)
    now = OTHER_HOUR + _ACTIVITY_MAX_AGE_S + 3599
    monkeypatch.setattr("omnigent.server.usage_evidence.now_epoch", lambda: now)
    prune_batch = store.prune_hours
    batches = []

    def record_batch(at):
        removed = prune_batch(at)
        batches.append(removed)
        return removed

    monkeypatch.setattr(store, "prune_hours", record_batch)
    evidence.prune()
    evidence.prune()
    assert batches == [5000, 2, 0, 0]
    with store._read_session()("test_retained_hours") as session:
        for table in (SqlUsageHourAgent, SqlUsageHour):
            assert set(session.execute(select(table.workspace_id, table.hour_utc))) == {
                (0, OTHER_HOUR),
                (1, OTHER_HOUR),
            }


def test_dense_hour_limit_never_returns_a_complete_partial_hour(db_uri):
    from sqlalchemy import insert

    from omnigent.db.db_models import SqlUsageHour
    from omnigent.db.utils import run_write_transaction
    from omnigent.stores.usage_hours_store import UsageReadLimit

    store = UsageHoursStore(db_uri)
    values = [
        {
            "workspace_id": 0,
            "owner_id": "owner@example.test",
            "account_id": "plan",
            "hour_utc": HOUR,
            "model": f"model-{index}",
            "host_id": "h",
            "basis": "list",
            "priced": 0,
            "total_tokens": 1,
            "cost_usd": 0,
            "updated_at": HOUR,
        }
        for index in range(20001)
    ]

    def write(session):
        for offset in range(0, len(values), 500):
            session.execute(insert(SqlUsageHour), values[offset : offset + 500])

    run_write_transaction(store._write_session(), "test_dense_hour", write)
    with pytest.raises(UsageReadLimit, match="hour_over_limit"):
        list(store.read_chunks("owner@example.test", HOUR, OTHER_HOUR))


@pytest.mark.parametrize("increments", [5, 50])
def test_agent_increment_rounding_never_discards_integer_attribution(db_uri, increments):
    from fractions import Fraction

    from omnigent.stores.usage_agent_parts import split_row
    from omnigent.stores.usage_hours_store import UsageBuckets

    store = UsageHoursStore(db_uri)
    for _ in range(increments):
        for agent in ("claude", "codex"):
            store.add_activity(
                "owner@example.test",
                "otlp",
                HOUR,
                "m",
                "h",
                "list",
                True,
                UsageBuckets(total_tokens=10),
                1,
                0,
                0.1,
                agent=agent,
            )
    [row] = next(store.read_chunks("owner@example.test", HOUR, OTHER_HOUR))
    parts = split_row(row, row.agent_parts)
    assert {part.agent: part.total_tokens for part in parts} == {
        "claude": increments * 10,
        "codex": increments * 10,
    }
    assert sum(Fraction(str(part.cost_usd)) for part in parts) == Fraction(str(row.cost_usd))


@pytest.mark.parametrize("overflow", ["cost_usd", "tokens", "requests", "uncached_input_tokens"])
def test_agent_overflow_preserves_only_proven_splits(db_uri, overflow):
    from omnigent.stores.usage_agent_parts import split_row
    from omnigent.usage.contract import AgentPart

    store = UsageHoursStore(db_uri)
    figures = {"tokens": 100, "requests": 10, "uncached_input_tokens": 100, "cost_usd": 1.0}
    child = {**figures, overflow: figures[overflow] + 1}
    outcome = store.replace_rows(
        "owner@example.test",
        "plan",
        [ActivityEntryV2(hour=HOUR, **figures, agents=[AgentPart(agent="codex", **child)])],
    )
    [row] = next(store.read_chunks("owner@example.test", HOUR, OTHER_HOUR))
    [part] = split_row(row, row.agent_parts)
    assert part.total_tokens == 100 and part.requests == 10
    if overflow == "cost_usd":
        assert not outcome.warnings
        assert part.agent == "codex" and part.cost_usd is None and part.agent_money_unknown
    else:
        assert outcome.warnings[0].reason == "agents_over_total"
        assert part.agent == "" and part.cost_usd == 1.0 and not part.agent_money_unknown


def test_agent_parts_owner_and_workspace_isolation(db_uri):
    from omnigent.db.db_models import workspace_scope
    from omnigent.usage.contract import AgentPart

    store = UsageHoursStore(db_uri)
    for workspace in (0, 7):
        with workspace_scope(workspace):
            for owner, agent in (("a@example.test", "claude"), ("b@example.test", "codex")):
                store.replace_rows(
                    owner,
                    "plan",
                    [
                        ActivityEntryV2(
                            hour=HOUR,
                            tokens=workspace + 10,
                            agents=[AgentPart(agent=agent, tokens=workspace + 10)],
                        )
                    ],
                )
    for workspace in (0, 7):
        with workspace_scope(workspace):
            for owner, agent in (("a@example.test", "claude"), ("b@example.test", "codex")):
                [row] = next(store.read_chunks(owner, HOUR, OTHER_HOUR))
                assert row.total_tokens == workspace + 10
                assert [part["agent"] for part in row.agent_parts] == [agent]
