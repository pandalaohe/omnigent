"""Read-time purchase allocation and valuation of the actual selected cohort."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from omnigent.usage.contract import AnalysisRowView, UsageAnalysisView
from omnigent.usage.pricing import Currency, PricingBaseline, PurchasedCard
from omnigent.usage.value import Coverage, Value, divide, exact, multiply, subtract


def allocation(paid: Fraction, billing: tuple[int, int], selected: tuple[int, int]) -> Fraction:
    start, until = billing
    return paid * max(0, min(until, selected[1]) - max(start, selected[0])) / (until - start)


def _matches(card: PurchasedCard, row: AnalysisRowView) -> bool:
    return row.parent_account_id == card.parent and (
        card.target_kind == "account" or row.pool_id == card.target_id
    )


def value_usage(
    view: UsageAnalysisView,
    baseline: PricingBaseline,
    *,
    pool: str | None = None,
    model: str | None = None,
    card_id: str | None = None,
) -> dict[str, Any]:
    """Never split a shared purchase by model or infer pool activity from payment."""
    start, until = int(view.span.counted_from), int(view.span.until)
    defaults = baseline.defaults
    as_of = int(view.generated_at)
    currency = defaults.display_currency
    cohort = (
        f"{start}:{until}:{','.join(sorted(view.account_spans))}:"
        f"{pool or ''}:{model or ''}:{card_id or ''}"
    )
    metadata = {"cohort_id": cohort, "as_of": as_of}

    def unknown(reason: str, unit: str = "money", money_currency: str | None = currency) -> Value:
        return Value(unit=unit, currency=money_currency, reason=reason, **metadata)

    def money(amount: Fraction, source: Currency, coverage: Coverage | None = None) -> Value:
        rate = defaults.fx_to_display.get(source)
        if rate is None:
            return unknown("missing_fx:" + source)
        return exact(
            amount * Fraction(rate),
            unit="money",
            currency=currency,
            coverage=coverage or Coverage(),
            **metadata,
        )

    accounts = set(view.account_spans)
    all_cards = [
        card
        for card in baseline.cards
        if (card.parent in accounts or card.target_id in accounts)
        and (pool is None or (card.target_kind == "pool" and card.target_id == pool))
        and (card_id is None or card.id == card_id)
    ]
    rows = [
        row
        for row in view.rows
        if row.contributes_usage
        and (pool is None or (row.attributed and row.pool_id == pool))
        and (model is None or row.model == model)
    ]
    cards = []
    known_paid_rows: list[AnalysisRowView] = []
    converted_paid = Fraction(0)
    ratio_paid = Fraction(0)
    paid_missing = False
    api_total = Fraction(0)
    known_api = Fraction(0)
    priced_units = 0
    usage_units = sum(row.tokens for row in rows if row.tokens is not None)
    units_complete = bool(rows) and all(row.tokens is not None for row in rows)
    api_partial = view.partial
    known_paid = 0
    items = 0
    card_covered: set[int] = set()
    missing_paid_accounts: set[str] = set()
    known_paid_cohort_units = 0
    ratio_items: set[str] = set()
    ratio_units_complete = True
    rows_by_parent: dict[str, list[AnalysisRowView]] = {}
    for row in rows:
        rows_by_parent.setdefault(row.parent_account_id, []).append(row)
    for card in all_cards:
        b0, b1 = card.billing_span()
        billing = (int(b0.timestamp()), int(b1.timestamp()))
        # Per-account billing ranges can differ for reset-based selections.
        span = (
            view.account_spans.get(card.parent)
            or view.account_spans.get(card.target_id)
            or view.span
        )
        selected = (int(span.counted_from), int(span.until))
        if max(billing[0], selected[0]) >= min(billing[1], selected[1]):
            continue
        items += 1
        discount = (
            card.discount_override
            if card.discount_override is not None
            else defaults.default_discount
        )
        paid_full = Fraction(card.retail_amount) * Fraction(discount)
        paid = allocation(paid_full, billing, selected)
        original = exact(paid, unit="money", currency=card.purchase_currency, **metadata)
        converted = money(paid, card.purchase_currency)
        if model is not None:
            converted = unknown("shared_payment_cannot_be_split_by_model")
        if converted.low is None:
            paid_missing = True
        else:
            converted_paid += paid * Fraction(defaults.fx_to_display[card.purchase_currency])
            known_paid += 1
        reset_results = []
        for reset in card.reset_spans():
            reset_paid = allocation(
                paid_full, billing, (max(selected[0], reset[0]), min(selected[1], reset[1]))
            )
            reset_money = money(reset_paid, card.purchase_currency)
            allowance = exact(Fraction(card.allowance_usd), unit="USD_allowance", **metadata)
            reset_results.append(
                {
                    "start": reset[0],
                    "until": reset[1],
                    "paid": reset_money,
                    "allowance": allowance,
                    "paid_per_quota": divide(
                        reset_money, allowance, unit="money_per_USD_allowance", currency=currency
                    ),
                }
            )
        card_rows = [
            row
            for row in rows_by_parent.get(card.parent, [])
            if _matches(card, row) and billing[0] <= row.hour < billing[1]
        ]
        if converted.low is not None:
            known_paid_cohort_units += sum(row.tokens or 0 for row in card_rows)
            ratio_units_complete = ratio_units_complete and all(
                row.tokens is not None for row in card_rows
            )
            if card_rows:
                ratio_items.add(card.id)
                ratio_paid += paid * Fraction(defaults.fx_to_display[card.purchase_currency])
        rates = card.rates_usd_per_million
        compatible = rates is not None and (
            all(row.model == rates.model for row in card_rows)
            if rates.model
            else len({row.model for row in card_rows}) <= 1
        )
        for row in card_rows:
            card_covered.add(id(row))
            counts = (
                row.uncached_input_tokens,
                row.output_tokens,
                row.cache_read_input_tokens,
                row.cache_creation_input_tokens,
            )
            # A partial hour cannot be allocated without finer source evidence.
            if (
                not compatible
                or rates is None
                or any(count is None for count in counts)
                or row.hour < selected[0]
                or row.hour + 3600 > selected[1]
            ):
                api_partial = True
                continue
            amount = (
                sum(
                    (
                        Fraction(count or 0) * Fraction(rate)
                        for count, rate in zip(
                            counts,
                            (rates.read, rates.write, rates.cache_read, rates.cache_write),
                            strict=True,
                        )
                    ),
                    Fraction(0),
                )
                / 1_000_000
            )
            api_total += amount
            priced_units += sum(count or 0 for count in counts)
            if converted.low is not None:
                known_api += amount
                known_paid_rows.append(row)
            if row.coverage == "partial" or row.reason:
                api_partial = True
        cards.append(
            {
                "id": card.id,
                "name": card.name,
                "starts_at": billing[0],
                "ends_at": billing[1],
                "effective_discount": discount,
                "original_paid": original,
                "paid": converted,
                "resets": reset_results,
                "api_value": unknown("per_card_usage_not_attributable"),
                "tokens": unknown("per_card_usage_not_attributable", "tokens", None),
            }
        )

    # A reported compatible list valuation can price usage outside a card baseline.
    # Billed/metered amounts supply paid, never an alternative API numerator.
    source_money = [
        row
        for row in view.rows
        if row.contributes_money
        and row.priced
        and row.cost_usd is not None
        and (pool is None or row.pool_id == pool)
        and (model is None or row.model == model)
    ]
    legacy_paid: dict[str, Fraction] = {}
    legacy_api: dict[str, Fraction] = {}
    legacy_precision = False

    def activity_key(row: AnalysisRowView):
        return row.parent_account_id, row.pool_id, row.hour, row.model, row.host_id

    money_by_activity: dict[tuple, list[AnalysisRowView]] = {}
    for row in source_money:
        money_by_activity.setdefault(activity_key(row), []).append(row)
    for row in rows:
        if id(row) in card_covered:
            continue
        matches = money_by_activity.get(activity_key(row), [])
        listed = next(
            (item for item in matches if item.basis == "list" and item.coverage == "complete"),
            None,
        )
        billed = next((item for item in matches if item.basis in {"billed", "metered"}), None)
        if billed is not None and billed.cost_usd is not None:
            legacy_precision = True
            legacy_paid[row.parent_account_id] = legacy_paid.get(
                row.parent_account_id, Fraction(0)
            ) + Fraction(str(billed.cost_usd))
            known_paid_cohort_units += row.tokens or 0
            ratio_units_complete = ratio_units_complete and row.tokens is not None
        else:
            missing_paid_accounts.add(row.parent_account_id)
        if listed is not None and listed.cost_usd is not None and row.tokens is not None:
            legacy_precision = True
            amount = Fraction(str(listed.cost_usd))
            api_total += amount
            priced_units += row.tokens
            if billed is not None:
                legacy_api[row.parent_account_id] = (
                    legacy_api.get(row.parent_account_id, Fraction(0)) + amount
                )
                known_paid_rows.append(row)
        else:
            api_partial = True
    for account_id, amount in legacy_paid.items():
        items += 1
        rate = defaults.fx_to_display.get("USD")
        if rate is None:
            paid_missing = True
        else:
            converted_paid += amount * Fraction(rate)
            known_paid += 1
            ratio_items.add(account_id)
            ratio_paid += amount * Fraction(rate)
            if account_id in legacy_api:
                known_api += legacy_api[account_id]
    coverage = Coverage(
        priced_units=priced_units,
        usage_units=usage_units if units_complete else None,
        known_paid=known_paid,
        total_items=items + len(missing_paid_accounts),
    )
    covered = units_complete and usage_units > 0 and priced_units * 2 >= usage_units
    api = (
        money(api_total, "USD", coverage)
        if covered
        else unknown("insufficient_priced_unit_coverage")
    )
    if api.low is not None and (api_partial or priced_units < usage_units):
        api = api.model_copy(
            update={"state": "partial", "high": None, "reason": "incomplete_usage_or_pricing"}
        )
    if card_id is not None:
        api = unknown("per_card_usage_not_attributable")
    paid_value = (
        money(converted_paid, currency, coverage)
        if items and not paid_missing
        else unknown("missing_fx_or_payment_baseline")
    )
    if model is not None:
        paid_value = unknown("shared_payment_cannot_be_split_by_model")
    elif missing_paid_accounts and paid_value.low is not None:
        paid_value = paid_value.model_copy(
            update={"state": "partial", "high": None, "reason": "unknown_paid_items"}
        )
    known_units = sum(row.tokens or 0 for row in known_paid_rows)
    known_api_value = (
        money(known_api, "USD", coverage)
        if known_units
        and ratio_units_complete
        and known_paid_cohort_units > 0
        and known_units * 2 >= known_paid_cohort_units
        else unknown("no_compatible_known_paid_cohort")
    )
    if known_api_value.low is not None:
        if (
            known_units < known_paid_cohort_units
            or view.partial
            or any(row.coverage == "partial" or row.reason for row in known_paid_rows)
        ):
            known_api_value = known_api_value.model_copy(update={"state": "partial", "high": None})
        elif coverage.known_paid < coverage.total_items:
            known_api_value = known_api_value.model_copy(
                update={"state": "partial", "reason": "known_paid_subset"}
            )
    # ANY missing FX invalidates the whole converted total and dependent ratios.
    ratio_denominator = money(ratio_paid, currency, coverage) if not paid_missing else paid_value
    ratio_metadata = {
        "cohort_id": cohort + ":known-paid:" + ",".join(sorted(ratio_items)),
        "coverage": Coverage(
            priced_units=known_units,
            usage_units=known_paid_cohort_units if ratio_units_complete else None,
            known_paid=len(ratio_items),
            total_items=coverage.total_items,
        ),
    }
    known_api_value = known_api_value.model_copy(update=ratio_metadata)
    ratio_denominator = ratio_denominator.model_copy(update=ratio_metadata)
    payback = divide(known_api_value, ratio_denominator, unit="ratio")
    tokens = (
        exact(usage_units, unit="tokens", coverage=coverage, **metadata)
        if units_complete
        else unknown("missing_tokens", "tokens", None)
    )
    million = (
        exact(
            Fraction(usage_units, 1_000_000), unit="million_tokens", coverage=coverage, **metadata
        )
        if units_complete
        else tokens
    )
    unit_cost = divide(paid_value, million, unit="money_per_million_tokens", currency=currency)
    if card_id is not None:
        tokens = unknown("per_card_usage_not_attributable", "tokens", None)
        payback = unknown("per_card_usage_not_attributable", "ratio", None)
        unit_cost = unknown("per_card_usage_not_attributable", "money_per_million_tokens")
    values = {
        "api_value": api,
        "paid": paid_value,
        "payback": payback,
        "tokens": tokens,
        "unit_cost": unit_cost,
    }
    return dict(
        basis="saved_baseline_and_reported_amounts" if legacy_precision else "user_saved_baseline",
        source_precision="legacy_decimal_bridge" if legacy_precision else "decimal_baseline",
        fx_basis="configured_source_to_display",
        revision=baseline.revision,
        cards=cards,
        coverage=coverage,
        **values,
        display={
            name: value.display(money=name not in {"payback", "tokens"})
            for name, value in values.items()
        },
        remaining_quota=unknown("requires_independent_quota_evidence", "percent", None),
    )


def compare_periods(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Value | str]:
    """Compare the same selection at two spans; do not normalize model/cache mix."""
    paid, tokens = current["paid"], current["tokens"]
    old_paid, old_tokens = previous["paid"], previous["tokens"]
    # The caller selects identical account/pool/model filters for both spans.
    old_paid = old_paid.model_copy(update={"cohort_id": paid.cohort_id})
    old_tokens = old_tokens.model_copy(update={"cohort_id": paid.cohort_id})
    same_usage = multiply(
        divide(old_paid, old_tokens, unit="money_per_token", currency=paid.currency),
        tokens,
        unit="money",
        currency=paid.currency,
    )
    same_spend = multiply(
        paid, divide(old_tokens, old_paid, unit="tokens_per_money"), unit="tokens"
    )
    return {
        "basis": "aggregate_per_token_comparison",
        "same_usage": same_usage,
        "same_usage_difference": subtract(same_usage, paid),
        "same_spend": same_spend,
        "same_spend_difference": subtract(same_spend, tokens),
    }
