"""Read-time purchase allocation and valuation of the actual selected cohort."""

from __future__ import annotations

from fractions import Fraction
from typing import Any

from omnigent.usage.analysis import LIST_BASES
from omnigent.usage.contract import AnalysisRowView, UsageAnalysisView
from omnigent.usage.price_resolution import row_value
from omnigent.usage.pricing import Currency, PricingBaseline, PurchasedCard
from omnigent.usage.value import Coverage, Value, combine, divide, exact, multiply, subtract


def allocation(paid: Fraction, billing: tuple[int, int], selected: tuple[int, int]) -> Fraction:
    start, until = billing
    return paid * max(0, min(until, selected[1]) - max(start, selected[0])) / (until - start)


def _matches(card: PurchasedCard, row: AnalysisRowView) -> bool:
    return row.parent_account_id == card.parent and (
        card.target_kind == "account" or row.pool_id == card.target_id
    )


def _value_usage(
    view: UsageAnalysisView,
    baseline: PricingBaseline,
    *,
    pool: str | None = None,
    model: str | None = None,
    card_id: str | None = None,
    pools: set[tuple[str, str]] | None = None,
    models: set[str] | None = None,
    card_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Never split a shared purchase by model or infer pool activity from payment."""
    pools = {(parent, pool) for parent in view.account_spans} if pool is not None else pools
    models = {model} if model is not None else models
    card_ids = {card_id} if card_id is not None else card_ids
    start, until = int(view.span.counted_from), int(view.span.until)
    defaults = baseline.defaults
    as_of = int(view.generated_at)
    currency = defaults.display_currency
    cohort = (
        f"{start}:{until}:{','.join(sorted(view.account_spans))}:"
        f"{sorted(pools) if pools is not None else None}:"
        f"{sorted(models) if models is not None else None}:"
        f"{sorted(card_ids) if card_ids is not None else None}"
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
        and (
            pools is None
            or (card.target_kind == "pool" and (card.parent, card.target_id) in pools)
        )
        and (card_ids is None or card.id in card_ids)
    ]
    periods = [
        (card, index, billing)
        for card in all_cards
        for index, billing in baseline.card_periods(
            card,
            (view.account_spans.get(card.parent) or view.span).counted_from,
            (view.account_spans.get(card.parent) or view.span).until,
            as_of,
        )
    ]
    actual_rows = [row for row in view.rows if row.contributes_usage]
    rows = actual_rows
    if card_ids is not None:
        rows = [
            row
            for row in rows
            if any(
                _matches(card, row) and b <= row.hour < min(e, baseline.replacement_boundary(card))
                for card, _, (b, e) in periods
            )
        ]
    rows = [
        row
        for row in rows
        if (pools is None or (row.attributed and (row.parent_account_id, row.pool_id) in pools))
        and (models is None or row.model in models)
    ]
    cards = []
    paid_components = []
    known_paid_rows: list[AnalysisRowView] = []
    converted_paid = Fraction(0)
    ratio_paid = Fraction(0)
    paid_missing = False
    api_high: Fraction | None = Fraction(0)
    known_api_high: Fraction | None = Fraction(0)
    api_total = Fraction(0)
    price_sources = []
    pricing_reasons = []
    known_api = Fraction(0)
    known_priced_units = 0
    known_api_partial = False
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
    actual_by_parent: dict[str, list[AnalysisRowView]] = {}
    for row in actual_rows:
        actual_by_parent.setdefault(row.parent_account_id, []).append(row)
    # Reported list valuations also apply to cards whose rates are inherited.
    # Billed/metered amounts supply paid, never an alternative API numerator.
    source_money = [
        row
        for row in view.rows
        if row.reason is None
        and (
            row.channel is None
            or (row.pool_id is not None and row.channel in {"usage", "list_cost", "metered_cost"})
        )
        and (row.channel != "list_cost" or row.basis in LIST_BASES)
        and (row.channel != "metered_cost" or row.basis not in LIST_BASES)
        and row.priced
        and row.cost_usd is not None
        and (pools is None or (row.parent_account_id, row.pool_id) in pools)
        and (models is None or row.model in models)
    ]

    def activity_key(row: AnalysisRowView):
        return row.parent_account_id, row.pool_id, row.hour, row.model, row.host_id

    money_by_activity: dict[tuple, list[AnalysisRowView]] = {}
    for row in source_money:
        money_by_activity.setdefault(activity_key(row), []).append(row)

    def recorded_list(row: AnalysisRowView) -> AnalysisRowView | None:
        if row.account_id.startswith("native:"):
            return row if row.priced and row.cost_usd is not None else None
        return max(
            (
                item
                for item in money_by_activity.get(activity_key(row), [])
                if item.basis in LIST_BASES and not item.account_id.startswith("native:")
            ),
            key=lambda item: item.channel == "list_cost",
            default=None,
        )

    for card, period_index, billing in periods:
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
        if models is not None:
            converted = unknown("shared_payment_cannot_be_split_by_model")
        if converted.low is None:
            paid_missing = True
        else:
            converted_paid += paid * Fraction(defaults.fx_to_display[card.purchase_currency])
            known_paid += 1
            paid_components.append(
                {
                    "start": billing[0],
                    "until": billing[1],
                    "amount": money(paid_full, card.purchase_currency),
                }
            )
        reset_results = []
        for reset in card.reset_spans(period_index):
            reset_paid = allocation(
                paid_full, billing, (max(selected[0], reset[0]), min(selected[1], reset[1]))
            )
            reset_money = money(reset_paid, card.purchase_currency)
            allowance = (
                exact(Fraction(card.allowance_usd), unit="USD_allowance", **metadata)
                if card.allowance_usd is not None
                else unknown("no_allowance", "USD_allowance", None)
            )
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
            if _matches(card, row)
            and billing[0] <= row.hour < min(billing[1], baseline.replacement_boundary(card))
        ]
        if converted.low is not None:
            known_paid_cohort_units += sum(row.tokens or 0 for row in card_rows)
            ratio_units_complete = ratio_units_complete and all(
                row.tokens is not None for row in card_rows
            )
            if card_rows:
                ratio_items.add(f"{card.id}:{billing[0]}")
                ratio_paid += paid * Fraction(defaults.fx_to_display[card.purchase_currency])
        target_rows = [
            row
            for row in actual_by_parent.get(card.parent, [])
            if _matches(card, row)
            and billing[0] <= row.hour < min(billing[1], baseline.replacement_boundary(card))
        ]
        for row in card_rows:
            card_covered.add(id(row))
            valued = row_value(
                row,
                baseline,
                card=card,
                card_models={item.model for item in target_rows},
                recorded=recorded_list(row),
            )
            if valued.low is None or row.hour < selected[0] or row.hour + 3600 > selected[1]:
                if valued.reason and valued.reason not in pricing_reasons:
                    pricing_reasons.append(valued.reason)
                api_partial = True
                continue
            amount = Fraction(valued.low)
            api_total += amount
            api_high = (
                None
                if api_high is None or valued.high is None
                else api_high + Fraction(valued.high)
            )
            for source in valued.price_sources:
                if source not in price_sources:
                    price_sources.append(source)
            priced_units += valued.coverage.priced_units
            if converted.low is not None:
                known_api += amount
                known_priced_units += valued.coverage.priced_units
                known_api_partial = known_api_partial or valued.state == "partial"
                known_api_high = (
                    None
                    if known_api_high is None or valued.high is None
                    else known_api_high + Fraction(valued.high)
                )
                known_paid_rows.append(row)
            if valued.state == "partial" or row.reason:
                api_partial = True
        cards.append(
            {
                "id": card.id,
                "period_id": f"{card.id}:{billing[0]}",
                "repeats": card.repeats,
                "next_renewal": baseline.next_renewal(card, as_of),
                "name": card.name,
                "parent_account_id": card.parent,
                "pool_id": card.target_id if card.target_kind == "pool" else None,
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

    legacy_paid: dict[str, Fraction] = {}
    legacy_api: dict[str, Fraction] = {}
    legacy_precision = False
    for row in rows:
        if id(row) in card_covered:
            continue
        matches = money_by_activity.get(activity_key(row), [])
        listed = recorded_list(row)
        billed = max(
            (
                item
                for item in matches
                if item.basis in {"billed", "metered"}
                and (not row.account_id.startswith("native:") or item is row)
            ),
            key=lambda item: item.basis == "billed",
            default=None,
        )
        if billed is not None and billed.cost_usd is not None:
            legacy_precision = True
            legacy_paid[row.parent_account_id] = legacy_paid.get(
                row.parent_account_id, Fraction(0)
            ) + Fraction(str(billed.cost_usd))
            if defaults.fx_to_display.get("USD") is not None:
                paid_components.append(
                    {
                        "start": row.hour,
                        "until": row.hour,
                        "amount": money(Fraction(str(billed.cost_usd)), "USD"),
                    }
                )
            known_paid_cohort_units += row.tokens or 0
            ratio_units_complete = ratio_units_complete and row.tokens is not None
        else:
            missing_paid_accounts.add(row.parent_account_id)
        valued = row_value(row, baseline, recorded=listed)
        if valued.low is not None and row.tokens is not None:
            legacy_precision = legacy_precision or listed is not None
            amount = Fraction(valued.low)
            api_total += amount
            api_high = (
                None
                if api_high is None or valued.high is None
                else api_high + Fraction(valued.high)
            )
            for source in valued.price_sources:
                if source not in price_sources:
                    price_sources.append(source)
            priced_units += valued.coverage.priced_units
            if valued.state == "partial":
                api_partial = True
            if billed is not None:
                known_priced_units += valued.coverage.priced_units
                known_api_partial = known_api_partial or valued.state == "partial"
                known_api_high = (
                    None
                    if known_api_high is None or valued.high is None
                    else known_api_high + Fraction(valued.high)
                )
                legacy_api[row.parent_account_id] = (
                    legacy_api.get(row.parent_account_id, Fraction(0)) + amount
                )
                known_paid_rows.append(row)
        else:
            if valued.reason and valued.reason not in pricing_reasons:
                pricing_reasons.append(valued.reason)
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
        else unknown(
            pricing_reasons[0]
            if len(pricing_reasons) == 1
            else "insufficient_priced_unit_coverage"
        )
    )
    if api.low is not None and api_high is not None and api_high != api_total:
        upper = money(api_high, "USD", coverage)
        api = api.model_copy(update={"state": "range", "high": upper.high})
    api = api.model_copy(update={"price_sources": price_sources})
    if api.low is not None and (api_partial or priced_units < usage_units):
        api = api.model_copy(
            update={"state": "partial", "high": None, "reason": "incomplete_usage_or_pricing"}
        )
    if card_ids is not None:
        api = unknown("per_card_usage_not_attributable")
    paid_value = (
        money(converted_paid, currency, coverage)
        if items and not paid_missing
        else unknown("missing_fx_or_payment_baseline")
    )
    if models is not None:
        paid_value = unknown("shared_payment_cannot_be_split_by_model")
    elif missing_paid_accounts and paid_value.low is not None:
        paid_value = paid_value.model_copy(
            update={"state": "partial", "high": None, "reason": "unknown_paid_items"}
        )
    known_units = known_priced_units
    known_api_value = (
        money(known_api, "USD", coverage)
        if known_units
        and ratio_units_complete
        and known_paid_cohort_units > 0
        and known_units * 2 >= known_paid_cohort_units
        else unknown("no_compatible_known_paid_cohort")
    )
    if known_api_value.low is not None:
        if known_api_high is not None and known_api_high != known_api:
            upper = money(known_api_high, "USD", coverage)
            known_api_value = known_api_value.model_copy(
                update={"state": "range", "high": upper.high}
            )
        if (
            known_api_partial
            or known_units < known_paid_cohort_units
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
    known_api_value = known_api_value.model_copy(
        update={**ratio_metadata, "price_sources": price_sources}
    )
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
    if card_ids is not None:
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
        paid_components=paid_components,
        coverage=coverage,
        pricing_reasons=pricing_reasons,
        **values,
        display={
            name: value.display(money=name not in {"payback", "tokens"})
            for name, value in values.items()
        },
        remaining_quota=unknown("requires_independent_quota_evidence", "percent", None),
    )


def value_usage(
    view: UsageAnalysisView,
    baseline: PricingBaseline,
    *,
    pool: str | None = None,
    model: str | None = None,
    card_id: str | None = None,
    pools: set[tuple[str, str]] | None = None,
    models: set[str] | None = None,
    card_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Keep fee/payback cohorts separate from metered spend and free usage."""
    pools = {(parent, pool) for parent in view.account_spans} if pool is not None else pools
    models = {model} if model is not None else models
    card_ids = {card_id} if card_id is not None else card_ids
    options = {"pools": pools, "models": models, "card_ids": card_ids}
    classes = {**view.account_cost_classes, **baseline.account_classes}
    if not classes:
        return _value_usage(view, baseline, **options)

    def selected(accounts: set[str]) -> UsageAnalysisView:
        return view.model_copy(
            update={
                "account_spans": {
                    key: span for key, span in view.account_spans.items() if key in accounts
                },
                "rows": [row for row in view.rows if row.parent_account_id in accounts],
            }
        )

    accounts = set(view.account_spans)
    if pools is not None:
        accounts &= {parent for parent, _ in pools}
    if card_ids is not None:
        accounts &= {card.parent for card in baseline.cards if card.id in card_ids}
    fee_accounts = {key for key in accounts if classes.get(key) in {"subscription", "prepaid"}}
    result = _value_usage(selected(fee_accounts), baseline, **options)
    money_by_account = {}
    spends: list[Value] = []
    for account in accounts:
        kind = classes.get(account)
        account_view = selected({account})
        projection = _value_usage(account_view, baseline, **options)
        metadata = {
            "cohort_id": result["paid"].cohort_id,
            "as_of": int(view.generated_at),
            "unit": "money",
            "currency": baseline.defaults.display_currency,
        }
        spend = Value(reason="class_unset" if kind is None else "not_applicable", **metadata)
        estimated = False
        if kind == "metered":
            # One bill per activity identity; a monetary sidecar takes precedence.
            reported = {}
            for row in account_view.rows:
                if (
                    row.basis not in {"billed", "metered"}
                    or not row.priced
                    or row.cost_usd is None
                    or row.reason
                ):
                    continue
                if row.channel not in {None, "usage", "metered_cost"}:
                    continue
                if (
                    (pools is not None and (row.parent_account_id, row.pool_id) not in pools)
                    or (models is not None and row.model not in models)
                    or card_ids is not None
                ):
                    continue
                key = row.hour, row.model, row.host_id, row.pool_id
                old = reported.get(key)
                if old is None or (row.basis == "billed", row.channel == "metered_cost") > (
                    old.basis == "billed",
                    old.channel == "metered_cost",
                ):
                    reported[key] = row
            if reported:
                rate = baseline.defaults.fx_to_display.get("USD")
                if rate is not None:
                    spend = exact(
                        sum(
                            (Fraction(str(row.cost_usd)) for row in reported.values()), Fraction(0)
                        )
                        * Fraction(rate),
                        **metadata,
                    )
                    usage_keys = {
                        (row.hour, row.model, row.host_id, row.pool_id)
                        for row in account_view.rows
                        if row.contributes_usage
                        and (pool is None or row.pool_id == pool)
                        and (model is None or row.model == model)
                        and (models is None or row.model in models)
                        and (pools is None or (row.parent_account_id, row.pool_id) in pools)
                    }
                    if (
                        view.partial
                        or any(row.coverage == "partial" for row in reported.values())
                        or usage_keys - reported.keys()
                    ):
                        spend = spend.model_copy(
                            update={
                                "state": "partial",
                                "high": None,
                                "reason": "incomplete_billed_coverage",
                            }
                        )
                else:
                    spend = Value(reason="missing_fx:USD", **metadata)
            else:
                spend = projection["api_value"].model_copy(
                    update={"cohort_id": metadata["cohort_id"]}
                )
                estimated = True
        if kind == "metered":
            spends.append(spend)
        money_by_account[account] = dict(
            cost_class=kind,
            show_money=kind != "free",
            show_payback=kind in {"subscription", "prepaid"},
            estimated=estimated,
            spend=spend,
            **{
                name: projection[name]
                if kind in {"subscription", "prepaid"}
                else Value(
                    reason="class_unset" if kind is None else "not_applicable",
                    **{**metadata, "unit": "ratio" if name == "payback" else "money"},
                )
                for name in ("api_value", "paid", "payback")
            },
        )
    result["account_money"] = money_by_account
    result["show_money"] = any(classes.get(key) != "free" for key in accounts)
    result["show_payback"] = bool(fee_accounts)
    result["spend_estimated"] = any(
        item["cost_class"] == "metered" and item["estimated"] for item in money_by_account.values()
    )
    result["spend"] = (
        combine(spends, unit="money", currency=baseline.defaults.display_currency)
        if spends
        else Value(
            unit="money",
            currency=baseline.defaults.display_currency,
            cohort_id=result["paid"].cohort_id,
            as_of=int(view.generated_at),
            reason="no_metered_accounts",
        )
    )
    return result


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
