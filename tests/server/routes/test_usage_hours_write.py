"""Hourly usage rows, written where a turn's spend is already recorded.

Two write seams share one recorder: the relay path (per response deltas) and the
native path (cumulative reports). The native token branch prices each report's
GROWTH rather than the running total, so a price change or a mid-session model
switch never reprices earlier usage; the hourly row then carries exactly the
growth the daily rollup receives.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationMetadata,
    SqlSessionPermission,
)
from omnigent.db.utils import get_or_create_engine
from omnigent.llms.context_window import ModelPricing
from omnigent.server.routes._sessions.orchestration import (
    _accumulate_session_usage,
    _persist_native_cumulative_usage,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.usage_hours_store import (
    UsageHoursRecorder,
    UsageHoursStore,
    configure_usage_hours,
    hours_of,
)

_OWNER = "alice@example.test"
_OTHER_OWNER = "bob@example.test"
_NATIVE = "native:unknown"
_HOST = "abcdef0123456789abcdef0123456789"


@pytest.fixture()
def hours(db_uri: str):
    """A recorder over the test database, installed for the seams."""
    store = UsageHoursStore(db_uri)
    configure_usage_hours(UsageHoursRecorder(store))
    yield store
    configure_usage_hours(None)


def _session(
    db_uri: str,
    *,
    owner: str | None = _OWNER,
    host_id: str | None = None,
) -> str:
    """Create a conversation row (and its owner grant); returns its id."""
    conv_id = uuid.uuid4().hex
    engine = get_or_create_engine(db_uri)
    now = 1_800_000_000
    with Session(engine) as session, session.begin():
        session.add(
            SqlConversation(
                id=conv_id,
                root_conversation_id=conv_id,
                title="usage",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            SqlConversationMetadata(
                id=conv_id,
                kind=1,
                host_id=host_id,
                workspace="/opt/work/omnigent/fork/wt" if host_id else None,
            )
        )
        if owner is not None:
            session.add(SqlSessionPermission(user_id=owner, conversation_id=conv_id, level=4))
    return conv_id


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _this_hour() -> int:
    return hours_of(int(datetime.now(UTC).timestamp()))


def _price(input_per_million: float) -> ModelPricing:
    return ModelPricing(input_per_token=input_per_million / 1_000_000, output_per_token=0.0)


def _patch_price(monkeypatch: pytest.MonkeyPatch, price: ModelPricing | None) -> None:
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing_with_provider",
        lambda *a, **kw: price,
    )


def _usage(store: SqlAlchemyConversationStore, conv_id: str) -> dict:
    conversation = store.get_conversation(conv_id)
    assert conversation is not None
    return dict(conversation.session_usage)


def test_relay_turn_writes_one_row_per_model(
    db_uri: str, hours: UsageHoursStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_price(monkeypatch, _price(1.0))
    conv_id = _session(db_uri, host_id=_HOST)
    store = SqlAlchemyConversationStore(db_uri)
    _accumulate_session_usage(
        {
            "usage": {
                "model": "gpt-5-mini",
                "input_tokens": 1_000,
                "output_tokens": 200,
                "total_tokens": 1_200,
                "cache_read_input_tokens": 50,
            }
        },
        conv_id,
        store,
    )

    [row] = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    assert row.account_id == _NATIVE
    assert row.model == "gpt-5-mini"
    assert row.priced is True
    assert row.host_id == _HOST
    assert (row.input_tokens, row.cached_tokens, row.output_tokens, row.total_tokens) == (
        1_000,
        50,
        200,
        1_200,
    )
    # The hour's cost is exactly the growth the daily rollup received.
    assert row.cost_usd > 0
    assert row.cost_usd == pytest.approx(store.get_daily_cost(_OWNER, _today()))


def test_relay_unpriced_turn_keeps_tokens_without_cost(
    db_uri: str, hours: UsageHoursStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_price(monkeypatch, None)
    conv_id = _session(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    _accumulate_session_usage(
        {
            "usage": {
                "model": "unpriced",
                "input_tokens": 10,
                "output_tokens": 1,
                "total_tokens": 11,
            }
        },
        conv_id,
        store,
    )

    [row] = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    assert row.priced is False
    assert (row.input_tokens, row.total_tokens, row.cost_usd) == (10, 11, 0.0)
    assert store.get_daily_cost(_OWNER, _today()) == 0.0


def test_fixed_price_matches_the_cumulative_figure(
    db_uri: str, hours: UsageHoursStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_price(monkeypatch, _price(1.0))
    conv_id = _session(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    for tokens in (1_000_000, 1_500_000, 1_600_000):
        _persist_native_cumulative_usage(
            conv_id, {"cumulative_input_tokens": tokens, "model": "m"}, store
        )

    # At one price and one model the growth rule equals pricing the total.
    assert _usage(store, conv_id)["total_cost_usd"] == pytest.approx(1.6)
    [row] = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    assert (row.input_tokens, row.total_tokens) == (1_600_000, 1_600_000)
    assert row.cost_usd == pytest.approx(1.6)
    assert store.get_daily_cost(_OWNER, _today()) == pytest.approx(1.6)


def test_price_up_charges_only_the_new_growth(
    db_uri: str, hours: UsageHoursStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv_id = _session(db_uri)
    store = SqlAlchemyConversationStore(db_uri)

    _patch_price(monkeypatch, _price(1.0))
    _persist_native_cumulative_usage(
        conv_id, {"cumulative_input_tokens": 1_000_000, "model": "m"}, store
    )
    _patch_price(monkeypatch, _price(2.0))
    _persist_native_cumulative_usage(
        conv_id, {"cumulative_input_tokens": 1_100_000, "model": "m"}, store
    )

    assert _usage(store, conv_id)["total_cost_usd"] == pytest.approx(1.2)
    assert store.get_daily_cost(_OWNER, _today()) == pytest.approx(1.2)
    [row] = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    assert row.cost_usd == pytest.approx(1.2)


def test_price_down_charges_only_the_new_growth(
    db_uri: str, hours: UsageHoursStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv_id = _session(db_uri)
    store = SqlAlchemyConversationStore(db_uri)

    _patch_price(monkeypatch, _price(1.0))
    _persist_native_cumulative_usage(
        conv_id, {"cumulative_input_tokens": 1_000_000, "model": "m"}, store
    )
    _patch_price(monkeypatch, _price(0.5))
    _persist_native_cumulative_usage(
        conv_id, {"cumulative_input_tokens": 1_100_000, "model": "m"}, store
    )

    # Pricing the running total at the new rate would have added nothing until
    # the total caught up; charging the new 100K at the new price adds 0.05.
    assert _usage(store, conv_id)["total_cost_usd"] == pytest.approx(1.05)
    assert store.get_daily_cost(_OWNER, _today()) == pytest.approx(1.05)


def test_unpriced_growth_is_filled_without_repricing_the_rollup(
    db_uri: str, hours: UsageHoursStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv_id = _session(db_uri)
    store = SqlAlchemyConversationStore(db_uri)

    _patch_price(monkeypatch, None)
    _persist_native_cumulative_usage(
        conv_id, {"cumulative_input_tokens": 1_000_000, "model": "late"}, store
    )
    assert "total_cost_usd" not in _usage(store, conv_id)

    [unpriced] = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    assert (unpriced.priced, unpriced.input_tokens) == (False, 1_000_000)

    assert hours.fill_unpriced(lambda model: _price(1.0), _this_hour() + 3600) == 1
    [priced_row] = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    assert priced_row.priced is True
    assert priced_row.cost_usd == pytest.approx(1.0)
    # The fill stays in usage_hours: neither the session total nor the daily
    # rollup is repriced retroactively.
    assert "total_cost_usd" not in _usage(store, conv_id)
    assert store.get_daily_cost(_OWNER, _today()) == 0.0

    # A later report adds only its own growth to the session figure.
    _patch_price(monkeypatch, _price(1.0))
    _persist_native_cumulative_usage(
        conv_id, {"cumulative_input_tokens": 1_100_000, "model": "late"}, store
    )
    assert _usage(store, conv_id)["total_cost_usd"] == pytest.approx(0.1)
    [row] = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    assert row.cost_usd == pytest.approx(1.1)


def test_cost_only_report_still_writes_a_row(db_uri: str, hours: UsageHoursStore) -> None:
    conv_id = _session(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    _persist_native_cumulative_usage(
        conv_id, {"cumulative_cost_usd": 0.4, "model": "claude-x"}, store
    )

    [row] = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    assert row.priced is True
    assert (row.model, row.total_tokens, row.cost_usd) == ("claude-x", 0, 0.4)


def test_claude_tokens_are_counted_once_per_growth(db_uri: str, hours: UsageHoursStore) -> None:
    conv_id = _session(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    first = {
        "cumulative_cost_usd": 0.1,
        "model": "claude-x",
        "cumulative_tokens_by_model": {
            "claude-x": {
                "input_tokens": 100,
                "cached_tokens": 20,
                "cache_write_tokens": 5,
                "output_tokens": 10,
                "total_tokens": 135,
            }
        },
    }
    _persist_native_cumulative_usage(conv_id, first, store)
    # A retried post adds nothing: the baseline is stored in session_usage.
    _persist_native_cumulative_usage(conv_id, first, store)
    [row] = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    assert (row.input_tokens, row.cached_tokens, row.cache_write_tokens, row.output_tokens) == (
        100,
        20,
        5,
        10,
    )
    assert row.cost_usd == pytest.approx(0.1)

    _persist_native_cumulative_usage(
        conv_id,
        {
            "cumulative_cost_usd": 0.3,
            "model": "claude-x",
            "cumulative_tokens_by_model": {
                "claude-x": {
                    "input_tokens": 150,
                    "cached_tokens": 20,
                    "cache_write_tokens": 5,
                    "output_tokens": 15,
                    "total_tokens": 190,
                },
                "claude-y": {"input_tokens": 40, "total_tokens": 40},
            },
        },
        store,
    )
    rows = {row.model: row for row in hours.read(_OWNER, _this_hour(), _this_hour() + 3600)}
    assert (rows["claude-x"].input_tokens, rows["claude-x"].output_tokens) == (150, 15)
    assert rows["claude-x"].cost_usd == pytest.approx(0.3)
    assert (rows["claude-y"].input_tokens, rows["claude-y"].cost_usd) == (40, 0.0)

    # A /clear drops the reported totals: growth clamps at 0, and no token is
    # ever clawed back.
    _persist_native_cumulative_usage(
        conv_id,
        {"cumulative_cost_usd": 0.3, "model": "claude-x", "cumulative_tokens_by_model": {}},
        store,
    )
    rows = {row.model: row for row in hours.read(_OWNER, _this_hour(), _this_hour() + 3600)}
    assert rows["claude-x"].input_tokens == 150
    # The displayed session fields are untouched by the per-model transport.
    assert "input_tokens" not in _usage(store, conv_id)


def test_single_user_server_records_the_local_owner(
    db_uri: str, hours: UsageHoursStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.auth import RESERVED_USER_LOCAL

    monkeypatch.setattr("omnigent.server.auth.local_single_user_enabled", lambda: True)
    _patch_price(monkeypatch, _price(1.0))
    conv_id = _session(db_uri, owner=None)
    store = SqlAlchemyConversationStore(db_uri)
    _persist_native_cumulative_usage(
        conv_id, {"cumulative_input_tokens": 1_000_000, "model": "m"}, store
    )

    [row] = hours.read(RESERVED_USER_LOCAL, _this_hour(), _this_hour() + 3600)
    assert row.account_id == _NATIVE
    assert row.input_tokens == 1_000_000


def test_unattributed_usage_is_dropped_without_the_single_user_marker(
    db_uri: str, hours: UsageHoursStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.auth import RESERVED_USER_LOCAL

    monkeypatch.setattr("omnigent.server.auth.local_single_user_enabled", lambda: False)
    _patch_price(monkeypatch, _price(1.0))
    conv_id = _session(db_uri, owner=None)
    store = SqlAlchemyConversationStore(db_uri)
    _persist_native_cumulative_usage(
        conv_id, {"cumulative_input_tokens": 1_000_000, "model": "m"}, store
    )

    assert hours.read(_OWNER, 0, 4_000_000_000) == []
    assert hours.read(RESERVED_USER_LOCAL, 0, 4_000_000_000) == []


def test_store_failure_leaves_the_turn_intact(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Broken:
        def add_turn(self, *args, **kwargs):
            raise RuntimeError("store down")

    configure_usage_hours(UsageHoursRecorder(_Broken()))
    try:
        _patch_price(monkeypatch, _price(1.0))
        conv_id = _session(db_uri)
        store = SqlAlchemyConversationStore(db_uri)
        result = _persist_native_cumulative_usage(
            conv_id, {"cumulative_input_tokens": 1_000_000, "model": "m"}, store
        )
        assert result == pytest.approx(1.0)
        assert _usage(store, conv_id)["total_cost_usd"] == pytest.approx(1.0)
        assert store.get_daily_cost(_OWNER, _today()) == pytest.approx(1.0)
    finally:
        configure_usage_hours(None)


def test_owner_isolation_between_accounts(
    db_uri: str, hours: UsageHoursStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_price(monkeypatch, _price(1.0))
    alice_conv = _session(db_uri, owner=_OWNER)
    bob_conv = _session(db_uri, owner=_OTHER_OWNER)
    store = SqlAlchemyConversationStore(db_uri)
    _persist_native_cumulative_usage(
        alice_conv, {"cumulative_input_tokens": 1_000_000, "model": "m"}, store
    )
    _persist_native_cumulative_usage(
        bob_conv, {"cumulative_input_tokens": 2_000_000, "model": "m"}, store
    )

    [alice_row] = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    [bob_row] = hours.read(_OTHER_OWNER, _this_hour(), _this_hour() + 3600)
    assert alice_row.input_tokens == 1_000_000
    assert bob_row.input_tokens == 2_000_000


def _claude_tokens(count: int) -> dict:
    """One model's cumulative token totals, as a Claude host posts them."""
    return {"claude-x": {"input_tokens": count, "total_tokens": count}}


def test_replayed_lower_report_does_not_recharge(
    db_uri: str, hours: UsageHoursStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shrunken then restored cumulative report bills 1M tokens once.

    A replayed or rebased report below the high-water mark must not reset the
    billing baseline: 1M -> 500K -> 1M at $1/M is $1.00 of usage, and the extra
    $0.50 would exhaust a real budget.
    """
    _patch_price(monkeypatch, _price(1.0))
    conv_id = _session(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    for tokens in (1_000_000, 500_000, 1_000_000):
        _persist_native_cumulative_usage(
            conv_id, {"cumulative_input_tokens": tokens, "model": "m"}, store
        )

    assert _usage(store, conv_id)["total_cost_usd"] == pytest.approx(1.0)
    assert store.get_daily_cost(_OWNER, _today()) == pytest.approx(1.0)
    [row] = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    assert row.cost_usd == pytest.approx(1.0)
    assert row.input_tokens == 1_000_000


def test_disabled_recorder_does_not_look_up_the_owner(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recording off means no attribution work at all, so nothing can fail."""
    lookups: list[str] = []
    configure_usage_hours(UsageHoursRecorder(UsageHoursStore(db_uri), enabled=False))
    try:
        _patch_price(monkeypatch, None)
        conv_id = _session(db_uri)
        store = SqlAlchemyConversationStore(db_uri)
        real = store.get_session_owner_authority

        def _spy(conversation_id, **kwargs):
            lookups.append(conversation_id)
            return real(conversation_id, **kwargs)

        monkeypatch.setattr(store, "get_session_owner_authority", _spy)
        _persist_native_cumulative_usage(
            conv_id, {"cumulative_input_tokens": 10, "model": "unpriced"}, store
        )
        assert lookups == []
        assert _usage(store, conv_id)["input_tokens"] == 10
    finally:
        configure_usage_hours(None)


def test_owner_lookup_failure_does_not_fail_the_turn(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unpriced completion survives a failing attribution lookup.

    Before the hourly rows existed, an unpriced turn needed no owner lookup at
    all; adding one must not turn a bookkeeping read into a failed turn after
    the usage was already persisted.
    """
    configure_usage_hours(UsageHoursRecorder(UsageHoursStore(db_uri)))
    try:
        _patch_price(monkeypatch, None)
        conv_id = _session(db_uri)
        store = SqlAlchemyConversationStore(db_uri)

        def _boom(*args, **kwargs):
            raise RuntimeError("owner lookup down")

        monkeypatch.setattr(store, "get_session_owner_authority", _boom)
        _persist_native_cumulative_usage(
            conv_id, {"cumulative_input_tokens": 10, "model": "unpriced"}, store
        )
        assert _usage(store, conv_id)["input_tokens"] == 10
    finally:
        configure_usage_hours(None)


def test_policy_only_tokens_are_not_filled_again(db_uri: str, hours: UsageHoursStore) -> None:
    """Tokens carried by a policy-only post are already covered by Claude's cost.

    Mid-sub-agent the forwarder posts tokens with only ``policy_cost_usd``, and
    the display cost arrives later on its own. The tokens are priced (design
    §2.7), so a later fill must not charge them a second time.
    """
    conv_id = _session(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    _persist_native_cumulative_usage(
        conv_id,
        {
            "policy_cost_usd": 0.0,
            "model": "claude-x",
            "cumulative_tokens_by_model": _claude_tokens(1_000_000),
        },
        store,
    )
    _persist_native_cumulative_usage(
        conv_id,
        {
            "cumulative_cost_usd": 1.0,
            "model": "claude-x",
            "cumulative_tokens_by_model": _claude_tokens(1_000_000),
        },
        store,
    )

    assert hours.fill_unpriced(lambda model: _price(1.0), _this_hour() + 3600) == 0
    rows = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    daily = store.get_daily_cost(_OWNER, _today())
    assert sum(row.cost_usd for row in rows) == pytest.approx(daily)
    assert daily == pytest.approx(1.0)


def test_reconstructed_transcript_rebases_the_token_baseline(
    db_uri: str, hours: UsageHoursStore
) -> None:
    """A rebuilt transcript restarts the ledger, so the next tokens still count."""
    conv_id = _session(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    _persist_native_cumulative_usage(
        conv_id,
        {
            "cumulative_cost_usd": 1.0,
            "model": "claude-x",
            "cumulative_tokens_by_model": _claude_tokens(1_000_000),
        },
        store,
    )
    # Cross-host reconstruction replays history without usage, so the ledger
    # restarts at zero and this report covers only the new work.
    _persist_native_cumulative_usage(
        conv_id,
        {
            "cumulative_cost_usd": 1.1,
            "model": "claude-x",
            "cumulative_tokens_by_model": _claude_tokens(100_000),
        },
        store,
    )

    rows = [row for row in hours.read(_OWNER, _this_hour(), _this_hour() + 3600) if row.model]
    assert sum(row.input_tokens for row in rows) == 1_100_000


def test_upgraded_session_does_not_backfill_historical_tokens(
    db_uri: str, hours: UsageHoursStore
) -> None:
    """A session the previous build already recorded adopts its baseline.

    There is no hours history to take (design §2.7: no backfill), so the first
    report after the upgrade records only its own cost growth and no tokens.
    """
    conv_id = _session(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    store.increment_session_usage(
        conv_id,
        {"input_tokens": 1_000_000, "total_tokens": 1_000_000, "total_cost_usd": 1.0},
    )
    _persist_native_cumulative_usage(
        conv_id,
        {
            "cumulative_cost_usd": 1.1,
            "model": "claude-x",
            "cumulative_tokens_by_model": _claude_tokens(1_100_000),
        },
        store,
    )

    rows = hours.read(_OWNER, _this_hour(), _this_hour() + 3600)
    assert [row.total_tokens for row in rows] == [0]
    assert sum(row.cost_usd for row in rows) == pytest.approx(0.1)

    # The growth after the adoption is recorded normally.
    _persist_native_cumulative_usage(
        conv_id,
        {
            "cumulative_cost_usd": 1.2,
            "model": "claude-x",
            "cumulative_tokens_by_model": _claude_tokens(1_200_000),
        },
        store,
    )
    token_rows = [
        row for row in hours.read(_OWNER, _this_hour(), _this_hour() + 3600) if row.total_tokens
    ]
    assert [row.input_tokens for row in token_rows] == [100_000]
