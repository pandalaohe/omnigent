"""User pricing arithmetic: exact allocations and conservative absence/intervals."""

from decimal import Decimal
from fractions import Fraction

import pytest
from pydantic import ValidationError

from omnigent.usage.contract import AnalysisRowView, AnalysisSpan, UsageAnalysisView
from omnigent.usage.pricing import PricingBaseline, PricingDefaults, PurchasedCard
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
    assert (
        value_usage(view(cache_creation_input_tokens=None), baseline())["api_value"].state
        == "unknown"
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
    assert result["payback"].display(money=False)["low"] == "2.38"
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
