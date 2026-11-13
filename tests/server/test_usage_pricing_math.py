"""User pricing arithmetic: exact allocations and conservative absence/intervals."""

from datetime import datetime
from decimal import Decimal
from fractions import Fraction

import pytest
from pydantic import ValidationError

from omnigent.usage.contract import AnalysisRowView, AnalysisSpan, UsageAnalysisView
from omnigent.usage.pricing import (
    PricingBaseline,
    PricingDefaults,
    PurchasedCard,
    default_cost_class,
)
from omnigent.usage.pricing_projection import allocation, compare_periods, value_usage
from omnigent.usage.value import Value, combine, divide, exact, share, subtract


def card(**edit):
    return PurchasedCard.model_validate(
        {
            "id": "card-a",
            "name": "Purchased card",
            "retail_amount": "100",
            "purchase_currency": "HKD",
            "allowance_usd": "100",
            "period_kind": "week",
            "starts_on": "2026-11-02",
            "target_kind": "account",
            "target_id": "account-a",
            "rates_usd_per_million": {
                "read": "2",
                "write": "4",
                "cache_read": "0.2",
                "cache_write": "2.5",
            },
            **edit,
        }
    )


def baseline(purchase=None, **defaults):
    return PricingBaseline(
        defaults=PricingDefaults(fx_to_display={"HKD": "0.128205128205128205"}, **defaults),
        cards=[purchase or card()],
    )


def view(purchase=None, **row_edit):
    purchase = purchase or card()
    b0, b1 = purchase.billing_span()
    start, until = int(b0.timestamp()), int(b1.timestamp())
    span = AnalysisSpan(requested_from=start, counted_from=start, until=until, approx=False)
    fields = {
        "account_id": "account-a",
        "parent_account_id": "account-a",
        "hour": start,
        "model": "model-a",
        "basis": "metered",
        "coverage": "complete",
        "contributes_usage": True,
        "tokens": 37_000_000,
        "uncached_input_tokens": 10_000_000,
        "output_tokens": 5_000_000,
        "cache_read_input_tokens": 20_000_000,
        "cache_creation_input_tokens": 2_000_000,
    }
    fields.update(row_edit)
    return UsageAnalysisView(
        generated_at=until,
        range="custom",
        tz="UTC",
        span=span,
        account_spans={"account-a": span},
        rows=[AnalysisRowView(**fields)],
    )


def test_worked_example_discount_fx_and_quota_independence():
    for discount, original, paid, payback, unit in [
        (None, "80", "10.26", "4.78", "0.28"),
        ("0.6", "60", "7.69", "6.37", "0.21"),
    ]:
        purchase = card(discount_override=discount)
        result = value_usage(view(), baseline(purchase))
        assert result["api_value"].low == "49"
        assert result["tokens"].low == "37000000"
        assert result["cards"][0]["original_paid"].low == original
        assert result["display"]["paid"]["low"] == paid
        assert result["display"]["payback"]["low"] == payback
        assert result["display"]["unit_cost"]["low"] == unit
        assert result["remaining_quota"].state == "unknown"


@pytest.mark.parametrize(
    "field,value",
    [
        ("retail_amount", 1.2),
        ("retail_amount", "1e3"),
        ("retail_amount", "NaN"),
        ("retail_amount", "-1"),
        ("retail_amount", "1,000"),
        ("allowance_usd", "0"),
        ("discount_override", "1.01"),
        ("retail_amount", "0.1234567890123456789"),
        ("retail_amount", "123456789012345678901"),
    ],
)
def test_invalid_inputs(field, value):
    payload = card().model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        PurchasedCard.model_validate(payload)


def test_blank_zero_and_defaults():
    assert card(discount_override="").discount_override is None
    assert card(discount_override="0").discount_override == "0"
    assert (
        PurchasedCard.model_validate(
            {**card().model_dump(), "purchase_currency": "CNY"}
        ).purchase_currency
        == "CNY"
    )
    result = value_usage(view(), baseline(card(discount_override="0")))
    assert result["paid"].low == "0"
    assert result["payback"].state == "unknown"


def test_missing_fx_invalidates_whole_total_and_preserves_original():
    state = PricingBaseline(cards=[card()])
    result = value_usage(view(), state)
    assert result["cards"][0]["original_paid"].low == "80"
    assert (
        result["paid"].state == result["payback"].state == result["unit_cost"].state == "unknown"
    )
    state.defaults = PricingDefaults(display_currency="HKD")
    result = value_usage(view(), state)
    assert result["paid"].low == "80"
    assert result["api_value"].state == result["payback"].state == "unknown"


def test_reset_allocation_and_dst():
    purchase = card(
        period_kind="month_weekly_reset",
        starts_on="2026-11-01",
        retail_amount="30",
        discount_override="1",
        purchase_currency="USD",
    )
    result = value_usage(view(purchase), PricingBaseline(cards=[purchase]))
    resets = result["cards"][0]["resets"]
    assert [item["paid"].low for item in resets] == ["7", "7", "7", "7", "2"]
    assert sum(Fraction(item["paid"].low) for item in resets) == 30
    assert resets[-1]["paid_per_quota"].low == "0.02"
    dst = card(timezone="America/New_York", starts_on="2026-03-07", period_kind="week")
    start, end = dst.billing_span()
    seconds = int(end.timestamp() - start.timestamp())
    assert seconds == 7 * 86400 - 3600
    billing = (int(start.timestamp()), int(end.timestamp()))
    cut = billing[0] + 86400
    assert (
        allocation(Fraction(80), billing, (billing[0], cut))
        + allocation(Fraction(80), billing, (cut, billing[1]))
        == 80
    )
    month_end = card(starts_on="2026-01-31", period_kind="month")
    assert month_end.billing_span()[1].date().isoformat() == "2026-02-28"


def test_incompatible_models_partitions_and_card_selection():
    sample = view()
    sample.rows.append(sample.rows[0].model_copy(update={"model": "model-b"}))
    assert value_usage(sample, baseline())["api_value"].state == "unknown"
    assert value_usage(sample, baseline())["api_value"].reason == "card rates name no model"
    assert (
        value_usage(view(cache_creation_input_tokens=None), baseline())["api_value"].state
        == "partial"
    )
    result = value_usage(view(), baseline(), card_id="card-a")
    assert result["paid"].low is not None
    assert result["tokens"].state == result["api_value"].state == "unknown"
    assert value_usage(view(), baseline(), model="model-a")["paid"].state == "unknown"


@pytest.mark.parametrize("priced,expected", [(49, "unknown"), (50, "partial")])
def test_priced_unit_threshold(priced, expected):
    sample = view(
        tokens=priced,
        uncached_input_tokens=priced,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    sample.rows.append(
        sample.rows[0].model_copy(
            update={
                "tokens": 100 - priced,
                "uncached_input_tokens": None,
                "hour": sample.rows[0].hour + 3600,
            }
        )
    )
    assert value_usage(sample, baseline())["api_value"].state == expected


def test_overlapping_parent_pool_rejected_but_disjoint_pools_allowed():
    first = card()
    second = card(
        id="other", target_kind="pool", target_id="source:pool", parent_account_id="account-a"
    )
    with pytest.raises(ValidationError, match="overlapping"):
        PricingBaseline(cards=[first, second])
    PricingBaseline(
        cards=[second, second.model_copy(update={"id": "third", "target_id": "source:other"})]
    )


def test_value_intervals_rounding_and_compatibility():
    metadata = {"unit": "money", "currency": "USD", "cohort_id": "sample", "as_of": 0}
    assert exact(Decimal("1.005"), **metadata).display()["low"] == "1.00"
    assert exact(Decimal("1.015"), **metadata).display()["low"] == "1.02"
    uncertain = Value(state="range", low="49", high="70", **metadata)
    ratio = divide(uncertain, exact(10, **metadata), unit="ratio")
    assert (ratio.low, ratio.high, ratio.state) == ("4.9", "7", "range")
    bounded = Value(state="range", low="1.005", high="1.015", **metadata)
    assert bounded.display() == {"low": "1.00", "high": "1.02"}
    partial = uncertain.model_copy(update={"state": "partial", "high": None})
    assert combine([partial, exact(10, **metadata)], unit="money", currency="USD").high is None
    assert divide(uncertain, exact(0, **metadata), unit="ratio").state == "unknown"
    assert (
        divide(uncertain, exact(10, **{**metadata, "cohort_id": "other"}), unit="ratio").state
        == "unknown"
    )
    difference = subtract(uncertain, exact(10, **metadata))
    assert (difference.low, difference.high) == ("39", "60")
    assert share(uncertain, exact(50, **metadata)).high == "1"


def test_comparison_uses_aggregate_actual_tokens_and_paid():
    current = value_usage(view(), baseline())
    prior = value_usage(view(), baseline(card(discount_override="0.4")))
    values = compare_periods(current, prior)
    assert values["same_usage"].display()["low"] == "5.13"
    assert values["same_usage_difference"].display()["low"] == "-5.13"
    assert values["same_spend"].low == "74000000"


def test_known_paid_subset_excludes_other_accounts_api_value():
    sample = view()
    second = sample.rows[0].model_copy(
        update={
            "account_id": "account-b",
            "parent_account_id": "account-b",
            "basis": "list",
            "priced": True,
            "contributes_money": True,
            "cost_usd": 100.0,
        }
    )
    sample.rows.append(second)
    sample.account_spans["account-b"] = sample.span
    result = value_usage(sample, baseline())
    assert result["api_value"].low == "149"
    assert result["source_precision"] == "legacy_decimal_bridge"
    assert result["coverage"].known_paid == 1
    assert result["coverage"].total_items == 2
    assert result["paid"].state == "partial"
    assert result["payback"].state == "partial"
    assert result["payback"].display(money=False)["low"] == "4.77"


def test_card_replaces_reported_paid_without_double_valuation():
    sample = view(contributes_money=True, priced=True, cost_usd=999.0)
    result = value_usage(sample, baseline())
    assert result["display"]["paid"]["low"] == "10.26"
    assert result["api_value"].low == "49"


def test_known_payment_with_unpriced_usage_stays_in_ratio_denominator():
    sample = view()
    sample.rows.append(
        sample.rows[0].model_copy(
            update={
                "account_id": "account-b",
                "parent_account_id": "account-b",
                "cache_creation_input_tokens": None,
            }
        )
    )
    sample.account_spans["account-b"] = sample.span
    state = baseline()
    state.cards.append(card(id="card-b", target_id="account-b"))
    result = value_usage(sample, state)
    assert result["payback"].state == "partial"
    assert result["payback"].display(money=False)["low"] == "4.53"
    assert result["payback"].coverage.usage_units == 74_000_000


@pytest.mark.parametrize("selected", ["card-a", "missing-card"])
def test_card_selection_excludes_other_accounts_and_uncovered_period_payments(selected):
    purchase = card(purchase_currency="USD")
    sample = view(purchase)
    sample.rows.extend(
        [
            sample.rows[0].model_copy(
                update={
                    "account_id": "account-b",
                    "parent_account_id": "account-b",
                    "contributes_money": True,
                    "priced": True,
                    "cost_usd": 20.0,
                }
            ),
            sample.rows[0].model_copy(
                update={
                    "hour": sample.rows[0].hour - 3600,
                    "contributes_money": True,
                    "priced": True,
                    "cost_usd": 30.0,
                }
            ),
        ]
    )
    sample.account_spans["account-b"] = sample.span
    result = value_usage(sample, baseline(purchase), card_id=selected)
    if selected == "card-a":
        assert result["paid"].state == "exact"
        assert result["paid"].low == "80"
        assert result["coverage"].total_items == 1
    else:
        assert result["paid"].state == "unknown"
        assert result["cards"] == []
    assert result["api_value"].state == result["tokens"].state == "unknown"


def test_model_selection_does_not_establish_unmapped_rate_compatibility():
    sample = view()
    sample.rows.append(sample.rows[0].model_copy(update={"model": "model-b"}))
    assert value_usage(sample, baseline(), model="model-a")["api_value"].state == "unknown"
    purchase = card()
    purchase.rates_usd_per_million.model = "model-a"
    result = value_usage(sample, baseline(purchase), model="model-a")
    assert result["api_value"].state == "exact"
    assert result["api_value"].low == "49"


def test_renewal_anchors_month_end_and_honors_exclusive_boundary():
    purchase = card(
        repeats=True,
        allowance_usd=None,
        period_kind="month",
        starts_on="2026-01-31",
        ends_on="2026-04-30",
        timezone="America/New_York",
    )
    state = PricingBaseline(cards=[purchase], account_classes={"account-a": "subscription"})
    assert [purchase.period_start(i).date().isoformat() for i in range(4)] == [
        "2026-01-31",
        "2026-02-28",
        "2026-03-31",
        "2026-04-30",
    ]
    start = purchase.period_start(0).timestamp()
    now = purchase.period_start(2).timestamp()
    periods = list(state.card_periods(purchase, start, purchase.period_start(5).timestamp(), now))
    assert len(periods) == 3
    assert periods[1][1][1] - periods[1][1][0] == 31 * 86400 - 3600
    assert state.next_renewal(purchase, now) is None
    assert (
        len(
            list(
                state.card_periods(purchase, start, purchase.period_start(5).timestamp(), now - 1)
            )
        )
        == 2
    )


def test_replacement_keeps_started_period_and_does_not_double_price_usage():
    first = card(
        repeats=True,
        allowance_usd=None,
        period_kind="month",
        starts_on="2026-01-01",
        purchase_currency="USD",
    )
    second = card(
        id="card-b",
        repeats=True,
        allowance_usd=None,
        period_kind="month",
        starts_on="2026-01-15",
        purchase_currency="USD",
        retail_amount="200",
    )
    state = PricingBaseline(cards=[first, second], account_classes={"account-a": "subscription"})
    sample = view(first)
    sample.generated_at = datetime.fromisoformat("2026-02-01T00:00:00+00:00").timestamp()
    sample.account_cost_classes = {"account-a": "subscription"}
    sample.rows.append(
        sample.rows[0].model_copy(update={"hour": second.period_start(0).timestamp()})
    )
    result = value_usage(sample, state)
    assert len(result["cards"]) == 2
    assert result["cards"][0]["ends_at"] == first.period_start(1).timestamp()
    assert result["api_value"].low == "98"
    assert result["cards"][0]["resets"][0]["allowance"].state == "unknown"
    assert state.next_renewal(first, sample.generated_at) is None
    edited = state.model_copy(update={"defaults": PricingDefaults(default_discount="0.4")})
    assert result["display"]["paid"]["low"] == "167.74"
    assert value_usage(sample, edited)["display"]["paid"]["low"] == "83.87"


@pytest.mark.parametrize(
    "source,native,bases,expected",
    [
        ("relay", False, None, "prepaid"),
        ("subscription", False, None, "subscription"),
        ("metered", False, None, "metered"),
        ("manual", False, None, None),
        ("subscription", True, {"unknown"}, None),
        (None, True, {"subscription"}, "subscription"),
        (None, True, {"metered"}, "metered"),
        (None, True, {"metered", "subscription"}, None),
    ],
)
def test_cost_class_defaults_only_known_facts(source, native, bases, expected):
    assert default_cost_class(source, native=native, bases=bases) == expected


@pytest.mark.parametrize("kind", ["free", "metered", None])
def test_non_purchase_classes_never_show_payback(kind):
    sample = view(cost_usd=12.5, priced=True, contributes_money=True)
    sample.account_cost_classes = {"account-a": kind}
    result = value_usage(sample, baseline())
    assert result["show_money"] == (kind != "free")
    assert result["show_payback"] is False
    assert result["account_money"]["account-a"]["payback"].state == "unknown"
    if kind == "metered":
        assert result["spend"].low == "12.5"
        assert not result["account_money"]["account-a"]["estimated"]


def test_cancel_preserves_started_period_pricing_and_other_account_pool_renewals():
    purchase = card(
        repeats=True,
        allowance_usd=None,
        period_kind="month",
        starts_on="2026-01-01",
        ends_on="2026-01-15",
        target_kind="pool",
        target_id="shared",
        parent_account_id="account-a",
    )
    other = card(
        id="other",
        repeats=True,
        allowance_usd=None,
        starts_on="2026-02-01",
        target_kind="pool",
        target_id="shared",
        parent_account_id="account-b",
    )
    state = PricingBaseline(cards=[purchase, other])
    assert state.replacement_boundary(purchase) == float("inf")
    assert state.past_period_count(purchase, purchase.period_start(3).timestamp()) == 1
    sample = view(
        purchase,
        hour=int(purchase.period_start(0).timestamp()) + 20 * 86400,
        pool_id="shared",
        attributed=True,
    )
    result = value_usage(sample, state)
    assert result["api_value"].low == "49"
    ongoing = purchase.model_copy(update={"ends_on": None})
    state = PricingBaseline(cards=[ongoing, other])
    assert state.past_period_count(ongoing, ongoing.period_start(3).timestamp()) == 4


def test_metered_token_estimate_retains_its_label_without_a_reported_bill():
    sample = view()
    sample.account_cost_classes = {"account-a": "metered"}
    result = value_usage(sample, baseline())
    assert result["spend"].low == "49"
    assert result["spend_estimated"] is True
    assert result["show_payback"] is False


def test_field_overrides_clear_restore_and_missing_partitions(monkeypatch):
    from omnigent.llms.context_window import ModelPricing
    from omnigent.usage.price_resolution import row_value
    from omnigent.usage.pricing import AccountPriceOverride, TokenRates

    monkeypatch.setattr(
        "omnigent.usage.price_resolution.selected_price",
        lambda model: ModelPricing(2e-6, 4e-6, 0.2e-6, 2.5e-6),
    )
    monkeypatch.setattr(
        "omnigent.usage.price_resolution.fetch_model_pricing", lambda *a, **k: None
    )
    row = view(priced=True, basis="list", cost_usd=7).rows[0]
    saved = PricingBaseline(
        price_overrides=[AccountPriceOverride(account_id="account-a", model="model-a", read="3")]
    )
    value = row_value(row, saved, recorded=row)
    assert value.low == "59"
    assert {source.layer for source in value.price_sources} == {"account", "import"}
    purchase = card(rates_usd_per_million=TokenRates(write="6", model="model-a"))
    assert row_value(row, saved, card=purchase, recorded=row).low == "69"
    saved.price_overrides[0].read = None
    assert row_value(row, saved, recorded=row).low == "7"
    assert row_value(row, saved, recorded=row).price_sources[0].version is None
    saved.price_overrides[0].read = "0"
    row.output_tokens = None
    partial = row_value(row, saved, recorded=row)
    assert partial.state == "partial" and partial.high is None
    assert partial.low == "9"
    assert TokenRates(read="").read is None


def test_override_model_matching_and_no_model_card_protection(monkeypatch):
    from omnigent.usage.price_resolution import row_value
    from omnigent.usage.pricing import AccountPriceOverride, TokenRates

    monkeypatch.setattr("omnigent.usage.price_resolution.selected_price", lambda model: None)
    monkeypatch.setattr(
        "omnigent.usage.price_resolution.fetch_model_pricing", lambda *a, **k: None
    )
    rates = {"read": "0", "write": "0", "cache_read": "0", "cache_write": "0"}
    saved = PricingBaseline(
        price_overrides=[
            AccountPriceOverride(account_id="account-a", model="vendor/model-a", **rates)
        ]
    )
    row = view().rows[0]
    assert row_value(row, saved).low == "0"
    saved.price_overrides.append(
        AccountPriceOverride(
            account_id="account-a", model="duplicate/model-a", **{**rates, "read": "0.00"}
        )
    )
    assert row_value(row, saved).low == "0"
    saved.price_overrides.append(
        AccountPriceOverride(account_id="account-a", model="other/model-a", read="1")
    )
    assert row_value(row, saved).state == "unknown"
    purchase = card(rates_usd_per_million=TokenRates(read="2"))
    assert (
        row_value(row, saved, card=purchase, card_models={"model-a", "model-b"}).reason
        == "card rates name no model"
    )


def test_recorded_gateway_range_and_native_old_ref_survive_import_changes(monkeypatch):
    from omnigent.llms.context_window import ModelPricing
    from omnigent.usage.prices import PriceEstimate

    monkeypatch.setattr(
        "omnigent.usage.price_resolution.selected_price", lambda model: ModelPricing(9, 9, 9, 9)
    )
    sample = view(priced=True, cost_usd=999, basis="list")
    sample.rows[0].price_estimate = PriceEstimate(
        low_usd="10",
        high_usd="12",
        state="range",
        priced_units=37_000_000,
        usage_units=37_000_000,
        price_source="gateway list",
        price_version="v1",
        fetched_at=123,
    )
    result = value_usage(sample, PricingBaseline())["api_value"]
    assert (result.state, result.low, result.high) == ("range", "10", "12")
    assert result.price_sources[0].version == "v1"
    sample.rows[0].price_estimate = None
    sample.rows[0].account_id = "native:codex"
    sample.rows[0].basis = "subscription"
    sample.rows[0].coverage = None
    result = value_usage(sample, PricingBaseline())["api_value"]
    assert result.low == "999"
    assert result.price_sources[0].name == "记录时估价，来源未记录"


@pytest.mark.parametrize("with_card", [False, True])
def test_native_recorded_values_stay_with_their_basis_and_priced_bucket(monkeypatch, with_card):
    monkeypatch.setattr("omnigent.usage.price_resolution.selected_price", lambda model: None)
    monkeypatch.setattr(
        "omnigent.usage.price_resolution.fetch_model_pricing", lambda *a, **k: None
    )
    sample = view(
        account_id="native:codex",
        basis="unknown",
        coverage=None,
        tokens=100,
        uncached_input_tokens=100,
        output_tokens=0,
        cache_read_input_tokens=0,
        cache_creation_input_tokens=0,
    )
    first = sample.rows[0]
    sample.rows.extend(
        [
            first.model_copy(
                update={
                    "tokens": 200,
                    "uncached_input_tokens": 200,
                    "priced": True,
                    "cost_usd": 2,
                    "price_ref": "import:v1",
                }
            ),
            first.model_copy(
                update={
                    "tokens": 300,
                    "uncached_input_tokens": 300,
                    "basis": "subscription",
                    "priced": True,
                    "cost_usd": 3,
                    "price_ref": "provider",
                }
            ),
        ]
    )
    state = baseline(card(rates_usd_per_million=None)) if with_card else PricingBaseline()
    result = value_usage(sample, state)["api_value"]
    assert (result.low, result.high, result.state) == ("5", None, "partial")
    assert (result.coverage.priced_units, result.coverage.usage_units) == (500, 600)
    assert {(source.layer, source.version) for source in result.price_sources} == {
        ("import", "v1"),
        ("provider", None),
    }


def test_blank_card_and_cleared_overrides_restore_separate_recorded_list_channel(monkeypatch):
    from omnigent.llms.context_window import ModelPricing
    from omnigent.usage.pricing import AccountPriceOverride, TokenRates

    monkeypatch.setattr(
        "omnigent.usage.price_resolution.selected_price",
        lambda model: ModelPricing(2e-6, 4e-6, 0.2e-6, 2.5e-6),
    )
    monkeypatch.setattr(
        "omnigent.usage.price_resolution.fetch_model_pricing", lambda *a, **k: None
    )
    sample = view(account_id="usage-source", pool_id="pool-a", channel="usage", attributed=True)
    sample.rows.append(
        sample.rows[0].model_copy(
            update={
                "account_id": "list-source",
                "channel": "list_cost",
                "basis": "list",
                "contributes_usage": False,
                "tokens": None,
                "cost_usd": 7,
                "priced": True,
                "price_ref": "import:historical",
            }
        )
    )
    state = baseline(card(rates_usd_per_million=None))
    assert value_usage(sample, state)["api_value"].low == "7"
    state.price_overrides = [
        AccountPriceOverride(account_id="account-a", model="model-a", read="3")
    ]
    assert value_usage(sample, state)["api_value"].low == "59"
    state.price_overrides[0].read = None
    state.cards[0].rates_usd_per_million = TokenRates(write="6", model="model-a")
    assert value_usage(sample, state)["api_value"].low == "59"
    state.cards[0].rates_usd_per_million.write = None
    result = value_usage(sample, state)["api_value"]
    assert result.low == "7"
    assert result.price_sources[0].version == "historical"
