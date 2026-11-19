"""Selected-cohort presentation over existing bounded usage and pricing reads."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from fractions import Fraction
from typing import Any, Literal, cast

from omnigent.usage.contract import AnalysisRowView, UsageAnalysisView
from omnigent.usage.pricing import PricingBaseline
from omnigent.usage.pricing_projection import compare_periods, value_usage
from omnigent.usage.value import Value, combine, divide, endpoint, exact, scale, subtract

Dimension = Literal["account", "pool", "model", "card"]


def _evidence_key(row: AnalysisRowView) -> tuple[str, int, str]:
    return row.account_id, row.hour, row.model


class CacheSavingsFold:
    """Sum each hour-wide evidence estimate once, keeping excluded shares unvalued."""

    def __init__(self) -> None:
        self.low = Fraction(0)
        self.high = Fraction(0)
        self.bounded_keys = 0
        self.unbounded_keys = 0
        self.unknown_tokens = 0
        self.split_keys = 0
        self.split_tokens = 0
        self.split_tokens_complete = True

    def add(
        self,
        main_rows: Sequence[AnalysisRowView],
        candidates: Sequence[AnalysisRowView],
        selected: Sequence[AnalysisRowView],
    ) -> None:
        selected_counts: Counter[tuple[str, int, str]] = Counter()
        cache_reads: defaultdict[tuple[str, int, str], int] = defaultdict(int)
        missing_cache_reads = set()
        for row in selected:
            if row.contributes_usage:
                key = _evidence_key(row)
                selected_counts[key] += 1
                if row.cache_read_input_tokens is None:
                    missing_cache_reads.add(key)
                else:
                    cache_reads[key] += row.cache_read_input_tokens
        if not selected_counts:
            return
        candidate_counts = Counter(
            _evidence_key(row) for row in candidates if row.contributes_usage
        )
        estimates = {
            _evidence_key(row): row.price_estimate
            for row in main_rows
            if row.contributes_usage and row.price_estimate is not None
        }
        for key, count in selected_counts.items():
            if count < candidate_counts[key]:
                self.unknown_tokens += cache_reads[key]
                self.split_keys += 1
                self.split_tokens += cache_reads[key]
                self.split_tokens_complete &= key not in missing_cache_reads
                continue
            estimate = estimates.get(key)
            low = estimate.cache_savings_low_usd if estimate is not None else None
            high = estimate.cache_savings_high_usd if estimate is not None else None
            if low is None or high is None:
                self.unbounded_keys += 1
                self.unknown_tokens += cache_reads[key]
                continue
            self.bounded_keys += 1
            self.low += Fraction(low)
            self.high += Fraction(high)

    def result(self, *, as_of: int) -> dict[str, Any]:
        amount = Value(unit="money", currency="USD", cohort_id="cache_savings", as_of=as_of)
        if self.bounded_keys:
            amount = Value(
                unit="money",
                currency="USD",
                cohort_id="cache_savings",
                as_of=as_of,
                state="exact" if self.low == self.high else "range",
                low=endpoint(self.low),
                high=endpoint(self.high),
            )
        return {
            "amount": amount,
            "unknown_tokens": self.unknown_tokens,
            "split_keys": self.split_keys,
            "split_tokens": self.split_tokens if self.split_tokens_complete else None,
            "unbounded_keys": self.unbounded_keys,
        }


def display_values(value: Any) -> Any:
    if isinstance(value, Value):
        return {**value.model_dump(), "formatted": value.display()}
    if isinstance(value, dict):
        if {"state", "unit", "low", "cohort_id", "as_of"}.issubset(value):
            return display_values(Value.model_validate(value))
        return {key: display_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [display_values(item) for item in value]
    return value


def selected_view(
    view: UsageAnalysisView,
    baseline: PricingBaseline,
    dimension: Dimension,
    keys: list[str],
    excluded: set[str],
):
    pools = None
    models = None
    cards = None
    if dimension == "pool":
        pools = set()
        for key in keys:
            pair = json.loads(key)
            if (
                not isinstance(pair, list)
                or len(pair) != 2
                or not all(isinstance(item, str) for item in pair)
            ):
                raise ValueError("pool selection requires parent/pool pairs")
            pools.add(tuple(pair))
        parents = {parent for parent, _ in pools}
    elif dimension == "model":
        models = set(keys)
        parents = {
            row.parent_account_id
            for row in view.rows
            if row.contributes_usage and row.model in models
        }
    elif dimension == "card":
        cards = set(keys)
        parents = {card.parent for card in baseline.cards if card.id in cards}
    else:
        parents = set(keys)
    parents -= excluded
    rows = [row for row in view.rows if row.parent_account_id in parents]
    spans = {
        key: span
        for key, span in view.account_spans.items()
        if key in parents or any(row.account_id == key for row in rows)
    }
    for parent in parents:
        spans.setdefault(parent, view.span)
    scope = view.model_copy(update={"rows": rows, "account_spans": spans})
    options = {"pools": pools, "models": models, "card_ids": cards}
    projection = value_usage(scope, baseline, **options)
    selected = [
        row
        for row in rows
        if (pools is None or (row.attributed and (row.parent_account_id, row.pool_id) in pools))
        and (models is None or row.model in models)
    ]
    return scope, selected, projection, options


def missing_usage(rows, dimension: Dimension, keys: list[str], excluded: set[str]) -> bool:
    if dimension == "card":
        return False
    usage = [row for row in rows if row.contributes_usage]
    if any(row.tokens is None or row.coverage == "partial" or row.reason for row in usage):
        return True
    if dimension == "account":
        expected, actual = set(keys) - excluded, {row.parent_account_id for row in usage}
    elif dimension == "model":
        expected, actual = set(keys), {row.model for row in usage}
    else:
        expected = {
            json.dumps(json.loads(key), separators=(",", ":"))
            for key in keys
            if json.loads(key)[0] not in excluded
        }
        actual = {
            json.dumps([row.parent_account_id, row.pool_id], separators=(",", ":"))
            for row in usage
            if row.attributed
        }
    return not expected.issubset(actual)


def present_analysis(
    view: UsageAnalysisView,
    previous: UsageAnalysisView | None,
    baseline: PricingBaseline,
    *,
    dimension: Dimension,
    keys: list[str],
    excluded: set[str],
) -> dict[str, Any]:
    scope, rows, projection, options = selected_view(view, baseline, dimension, keys, excluded)
    items: list[dict[str, Any]] = []
    for key in dict.fromkeys(keys):
        _, _, item, _ = selected_view(view, baseline, dimension, [key], excluded)
        items.append({"key": key, "projection": item})
    partial = scope.partial or scope.truncated or missing_usage(rows, dimension, keys, excluded)
    if partial:
        for name in ("tokens", "api_value", "payback"):
            projection[name] = projection[name].model_copy(
                update={
                    "state": "unknown",
                    "low": None,
                    "high": None,
                    "reason": "selected_usage_not_reported",
                }
            )
    if (
        dimension in {"account", "pool"}
        and projection["paid"].low is not None
        and any(item["projection"]["paid"].low is None for item in items)
    ):
        projection["paid"] = projection["paid"].model_copy(
            update={"state": "partial", "high": None, "reason": "unknown_selected_payments"}
        )
    prior = None
    comparison: dict[str, Any] = {
        "reason": "no_previous_period_for_d90"
        if view.range == "d90"
        else "previous_usage_not_reported"
    }
    if previous is not None:
        _, previous_rows, old, _ = selected_view(previous, baseline, dimension, keys, excluded)
        prior = {
            "span": previous.span,
            "rows": previous_rows,
            "partial": previous.partial
            or previous.truncated
            or missing_usage(previous_rows, dimension, keys, excluded),
        }
        # An incomplete payment population cannot establish a period comparison.
        if (
            not partial
            and not prior["partial"]
            and all(
                value["paid"].state == "exact" and value["tokens"].state == "exact"
                for value in (projection, old)
            )
        ):
            comparison = compare_periods(projection, old)
            comparison["same_spend_difference"] = subtract(
                projection["tokens"], cast(Value, comparison["same_spend"])
            )
            now_efficiency = divide(
                projection["tokens"], projection["paid"], unit="tokens_per_money"
            )
            old_efficiency = divide(
                old["tokens"], old["paid"], unit="tokens_per_money"
            ).model_copy(update={"cohort_id": now_efficiency.cohort_id})
            ratio = divide(now_efficiency, old_efficiency, unit="ratio")
            one = exact(1, unit="ratio", cohort_id=ratio.cohort_id, as_of=ratio.as_of)
            comparison["same_spend_percent"] = scale(subtract(ratio, one), Fraction(100))
        else:
            comparison = {"reason": "missing_or_incompatible_payment_or_usage"}
    card_rows = []
    quota_values = []
    for card in projection["cards"]:
        resets = [
            reset
            for reset in card["resets"]
            if max(reset["start"], scope.span.counted_from) < min(reset["until"], scope.span.until)
        ]
        quota = (
            combine(
                [
                    reset["allowance"].model_copy(
                        update={"cohort_id": projection["paid"].cohort_id}
                    )
                    for reset in resets
                ],
                unit="USD_allowance",
            )
            if resets
            else Value(
                unit="USD_allowance",
                cohort_id=projection["paid"].cohort_id,
                as_of=int(view.generated_at),
                reason="no_allowance",
            )
        )
        quota_values.append(quota)
        paid = card["paid"].model_copy(update={"cohort_id": quota.cohort_id})
        card_rows.append(
            {
                **card,
                "allowance": quota,
                "paid_per_quota": divide(
                    paid, quota, unit="money_per_USD_allowance", currency=paid.currency
                ),
                "reset_count": len(resets),
            }
        )
    total_quota = (
        combine(quota_values, unit="USD_allowance")
        if quota_values
        else Value(
            unit="USD_allowance",
            cohort_id=projection["paid"].cohort_id,
            as_of=int(view.generated_at),
            reason="no_selected_purchases",
        )
    )
    paid = projection["paid"]
    total_per_quota = divide(
        paid, total_quota, unit="money_per_USD_allowance", currency=paid.currency
    )
    series = []
    if dimension != "model" and paid.low is not None:
        start, until = scope.span.counted_from, int(scope.span.until)
        step = 3600 if until - start <= 48 * 3600 else 86400
        times = sorted({start, until, *range((start // step + 1) * step, until, step)})
        for at in times:
            amount = Fraction(0)
            for component in projection["paid_components"]:
                number = Fraction(component["amount"].low)
                begins, ends = component["start"], component["until"]
                if begins == ends:
                    if start <= begins < at:
                        amount += number
                else:
                    amount += number * max(0, min(ends, at) - max(begins, start)) / (ends - begins)
            value = exact(
                amount,
                unit="money",
                currency=paid.currency,
                cohort_id=paid.cohort_id,
                as_of=paid.as_of,
            )
            if paid.state == "partial" and value.low is not None:
                value = value.model_copy(
                    update={"state": "partial", "high": None, "reason": paid.reason}
                )
            series.append({"at": at, "value": value})
    closings = [
        closing
        for closing in view.quota_closings
        if (
            dimension == "account"
            and closing.get("parent_account_id", closing["account_id"]) in set(keys) - excluded
        )
        or (
            dimension == "pool"
            and (closing.get("parent_account_id"), closing.get("pool_id"))
            in (options["pools"] or set())
        )
    ]
    return display_values(
        {
            "dimension": dimension,
            "partial": partial,
            "keys": keys,
            "rows": rows,
            "projection": projection,
            "items": items,
            "previous": prior,
            "comparison": comparison,
            "cards": card_rows,
            "allowance": total_quota,
            "paid_per_quota": total_per_quota,
            "paid_series": series,
            "quota_closings": closings,
        }
    )
