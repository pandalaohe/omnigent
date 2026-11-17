"""Bounded projections over attributed hour chunks, using exact monetary sums."""

from collections import defaultdict
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Any
from zoneinfo import ZoneInfo

from omnigent.stores.usage_hours_store import UsageReadLimit
from omnigent.usage.analysis_filters import local_day, local_slot
from omnigent.usage.contract import AnalysisSpan
from omnigent.usage.pricing_projection import ValueUsageFold, _PricingFold, payment_periods
from omnigent.usage.value import Value, endpoint

SCOPE_LIMIT = 2048
ACCUMULATOR_LIMIT = 50000
PAYMENT_PERIOD_LIMIT = 20000


KINDS = {
    "input": "uncached_input_tokens",
    "cache_read": "cache_read_input_tokens",
    "cache_write": "cache_creation_input_tokens",
    "output": "output_tokens",
}


class CountsFold:
    def __init__(self):
        self.sums = dict.fromkeys(("tokens", "requests", "failed"), 0)
        self.missing = set()
        self.coverage = False
        self.last_at = None
        self.rows = 0

    def add(self, rows):
        for row in rows:
            self.last_at = max(self.last_at or 0, row.updated_at or row.hour)
            self.coverage = self.coverage or bool(row.reason)
            if not row.contributes_usage:
                continue
            self.rows += 1
            self.coverage = self.coverage or row.coverage == "partial" or bool(row.reason)
            for name in self.sums:
                value = getattr(row, name)
                if value is None:
                    self.missing.add(name)
                else:
                    self.sums[name] += value

    def result(self):
        return {
            **{name: None if name in self.missing else value for name, value in self.sums.items()},
            "last_at": self.last_at,
            "coverage": "partial" if self.coverage or self.missing else "complete",
        }


class ClassesFold:
    def __init__(self):
        self.tokens = defaultdict(int)
        self.unpriced = defaultdict(int)
        self.low = defaultdict(Fraction)
        self.high = defaultdict(Fraction)
        self.unsplit = 0
        self.unsplit_low = Fraction(0)
        self.unsplit_high = Fraction(0)
        self.unsplit_unknown = False

    def add(self, rows):
        for row in rows:
            if not row.contributes_usage:
                continue
            estimate = row.price_estimate
            for kind, field in KINDS.items():
                tokens = getattr(row, field)
                if tokens is None:
                    continue
                self.tokens[kind] += tokens
                rates = (
                    [
                        Fraction(rate.per_token)
                        for rate in estimate.rates
                        if rate.token_kind
                        in (
                            {"cache_write", "cache_write_5m", "cache_write_1h"}
                            if kind == "cache_write"
                            else {"uncached_input" if kind == "input" else kind}
                        )
                    ]
                    if estimate
                    else []
                )
                if rates:
                    self.low[kind] += tokens * min(rates)
                    self.high[kind] += tokens * max(rates)
                else:
                    self.unpriced[kind] += tokens
            if any(getattr(row, field) is None for field in KINDS.values()):
                self.unsplit += max(
                    0,
                    (row.tokens or 0) - sum(getattr(row, field) or 0 for field in KINDS.values()),
                )
                if row.cost_usd is not None and all(
                    getattr(row, field) is None for field in KINDS.values()
                ):
                    self.unsplit_low += Fraction(str(row.cost_usd))
                    self.unsplit_high += Fraction(str(row.cost_usd))
                else:
                    self.unsplit_unknown = True

    def result(self):
        return {
            "items": [
                {
                    "kind": kind,
                    "tokens": self.tokens[kind],
                    "cost_low_usd": endpoint(self.low[kind]),
                    "cost_high_usd": endpoint(self.high[kind]),
                    "unpriced_tokens": self.unpriced[kind],
                }
                for kind in KINDS
            ],
            "unpriced_tokens": dict(self.unpriced),
            "unsplit_tokens": self.unsplit,
            "unsplit_cost": {
                "low": endpoint(self.unsplit_low),
                "high": None if self.unsplit_unknown else endpoint(self.unsplit_high),
            },
            "basis": "price_table_estimate",
        }


class ScopeFold:
    def __init__(
        self,
        view,
        baseline,
        card_models,
        *,
        parents=None,
        pools=None,
        card_ids=None,
        valuation_cache=None,
        payment_context=None,
        payment=True,
    ):
        view = view.model_copy(update={"rows": []})
        if parents is not None:
            view = view.model_copy(
                update={
                    "account_spans": {
                        key: span for key, span in view.account_spans.items() if key in parents
                    }
                }
            )
        self.view = view
        options = {
            "pools": pools,
            "card_ids": card_ids,
            "card_models": card_models,
            "include_components": False,
            "valuation_cache": valuation_cache,
            "payment_context": payment_context,
        }
        self.pricing = (
            ValueUsageFold(view, baseline, include_accounts=False, **options) if payment else None
        )
        self.api = (
            _PricingFold(view, baseline, include_payment=False, **options)
            if self.pricing is None or self.pricing.classes
            else None
        )
        self.card_ids = card_ids
        self.counts = CountsFold()

    def add(self, rows, *, partial=False):
        partial = partial or any(row.reason for row in rows)
        if partial:
            self.counts.coverage = True
            if self.pricing is not None:
                self.pricing.mark_partial()
            if self.api is not None:
                self.api.view.partial = True
        self.counts.add(rows)
        if self.pricing is not None:
            self.pricing.add(rows)
        if self.api is not None:
            self.api.add(rows)

    def result(self, *, payment=True):
        if self.pricing is not None:
            priced = self.pricing.result()
        else:
            assert self.api is not None
            priced = self.api.result()
        api = self.api.result() if self.api is not None and self.pricing is not None else priced
        result = {
            **self.counts.result(),
            "api_value": api["api_value"],
            "paid": priced["paid"],
            "payback": priced["payback"],
        }
        if self.card_ids is not None:
            result.update(tokens=None, requests=None, failed=None, coverage="partial")
        if not payment:
            for name in ("paid", "payback"):
                result[name] = Value(
                    unit="ratio" if name == "payback" else "money",
                    currency=priced[name].currency,
                    cohort_id=priced[name].cohort_id,
                    as_of=priced[name].as_of,
                    reason="shared_payment_cannot_be_split_by_filter",
                )
        return result


class AnalysisFold:
    def __init__(self, view, baseline, filters, card_models):
        self.view, self.baseline, self.filters = view, baseline, filters
        self.card_models = card_models
        self.zone = ZoneInfo(filters.tz)
        self.valuation_cache = {}
        self.scope_count = self.accumulator_count = 0
        self.payment_context = payment_periods(view, baseline, limit=PAYMENT_PERIOD_LIMIT)
        self.restricted = bool(filters.host or filters.agent or filters.model or filters.slot)
        self.total = self.scope(payment=not self.restricted)
        self.series = {}
        self.groups = {}
        self.facets = {name: {} for name in ("account", "model", "host", "agent")}
        self.heat = {
            f"{day}-{hour:02d}": CountsFold() for day in range(1, 8) for hour in range(24)
        }
        self.classes = ClassesFold()
        self.days = {}
        self.details = []
        self.rows_seen = 0
        start = datetime.fromtimestamp(view.span.counted_from, self.zone).date()
        end = datetime.fromtimestamp(
            max(view.span.counted_from, view.span.until - 1), self.zone
        ).date()
        if (end - start).days < 92:
            for offset in range((end - start).days + 1):
                self.day(start + timedelta(days=offset))

    def reserve(self):
        if self.accumulator_count >= ACCUMULATOR_LIMIT:
            raise UsageReadLimit("aggregation_over_limit")
        self.accumulator_count += 1

    def scope(
        self, *, parents=None, pools=None, card_ids=None, omit=None, span=None, payment=False
    ):
        if self.scope_count >= SCOPE_LIMIT:
            raise UsageReadLimit("aggregation_over_limit")
        self.reserve()
        self.scope_count += 1
        if self.filters.account and omit != "account":
            parents = (
                set(self.filters.account)
                if parents is None
                else parents & set(self.filters.account)
            )
        view = self.view
        if span is not None:
            view = view.model_copy(
                update={"span": span, "account_spans": dict.fromkeys(view.account_spans, span)}
            )
        return ScopeFold(
            view,
            self.baseline,
            self.card_models.get(omit, self.card_models[None]),
            parents=parents,
            pools=pools,
            card_ids=card_ids,
            valuation_cache=self.valuation_cache,
            payment_context=self.payment_context,
            payment=payment,
        )

    def day(self, day):
        key = day.isoformat()
        if key not in self.days:
            span = local_day(day, self.zone, self.view.generated_at)
            hours = {
                hour: {
                    "counts": CountsFold(),
                    "host": defaultdict(int),
                    "agent": defaultdict(int),
                    "model": defaultdict(int),
                }
                for hour in range(span.counted_from, int(span.until) + 1, 3600)
                if hour < span.until
            }
            self.days[key] = {"counts": self.scope(), "classes": ClassesFold(), "hours": hours}
        return self.days[key]

    def group_keys(self, row):
        group = self.filters.group
        if group == "pool":
            return [(row.parent_account_id, row.pool_id)] if row.pool_id else []
        if group == "card":
            return [
                card.id
                for card in self.baseline.cards
                if row.parent_account_id == card.parent
                and (card.target_kind == "account" or row.pool_id == card.target_id)
                and any(
                    start <= row.hour < min(until, self.baseline.replacement_boundary(card))
                    for _, (start, until) in self.baseline.card_periods(
                        card,
                        self.view.span.counted_from,
                        self.view.span.until,
                        int(self.view.generated_at),
                    )
                )
            ]
        return [
            row.parent_account_id
            if group == "account"
            else row.host_id
            if group == "host"
            else getattr(row, group)
        ]

    def add(self, rows, *, main_rows=None, partial=False):
        self.valuation_cache.clear()
        main_rows = rows if main_rows is None else main_rows
        partial = partial and not any(row.reason for row in main_rows)

        def source(*, omit=None, dimension=None):
            return (
                rows
                if dimension == "agent" or (self.filters.agent and omit != "agent")
                else main_rows
            )

        selected = [row for row in source() if self.filters.matches(row)]
        self.total.add(selected, partial=partial)
        self.classes.add(selected)
        buckets = defaultdict(list)
        for row in selected:
            day, _, _ = local_slot(row.hour, self.zone)
            bucket = (
                row.hour
                if self.view.span.until - self.view.span.requested_from <= 48 * 3600
                else day
            )
            buckets[bucket].append(row)
        for key, values in buckets.items():
            if key not in self.series:
                self.series[key] = self.scope()
            self.series[key].add(values, partial=partial)
        grouped = defaultdict(list)
        for row in source(omit=self.filters.group, dimension=self.filters.group):
            if self.filters.matches(row, omit=self.filters.group):
                for key in self.group_keys(row):
                    grouped[key].append(row)
        for key, values in grouped.items():
            parents = {key} if self.filters.group == "account" else None
            pools = {key} if self.filters.group == "pool" else None
            if key not in self.groups:
                self.groups[key] = {
                    "counts": self.scope(
                        parents=parents,
                        pools=pools,
                        card_ids={key} if self.filters.group == "card" else None,
                        omit=self.filters.group,
                        payment=self.filters.group in {"account", "pool", "card"}
                        and not self.restricted,
                    ),
                    "trend": {},
                }
            self.groups[key]["counts"].add(values, partial=partial)
            for row in values:
                day = local_slot(row.hour, self.zone)[0]
                bucket = (
                    row.hour
                    if self.view.span.until - self.view.span.requested_from <= 48 * 3600
                    else day
                )
                if bucket not in self.groups[key]["trend"]:
                    self.reserve()
                    self.groups[key]["trend"][bucket] = CountsFold()
                self.groups[key]["trend"][bucket].add([row])
        for dimension, facets in self.facets.items():
            values = defaultdict(list)
            for row in source(omit=dimension, dimension=dimension):
                if self.filters.matches(row, omit=dimension):
                    key = (
                        row.parent_account_id
                        if dimension == "account"
                        else row.host_id
                        if dimension == "host"
                        else getattr(row, dimension)
                    )
                    values[key].append(row)
            for key, group in values.items():
                if key not in facets:
                    facets[key] = self.scope(
                        parents={key} if dimension == "account" else None, omit=dimension
                    )
                facets[key].add(group, partial=partial)
        for row in source(omit="slot"):
            if self.filters.matches(row, omit="slot"):
                self.heat[local_slot(row.hour, self.zone)[1]].add([row])
        self.add_days(
            selected,
            agent_rows=[row for row in rows if self.filters.matches(row)],
            partial=partial,
        )
        details = [row for row in rows if self.filters.matches(row)]
        self.rows_seen += len(details)
        if "rows" in self.filters.include:
            self.details = sorted(
                [*self.details, *details],
                key=lambda row: (row.hour, row.account_id, row.model, row.host_id, row.agent),
                reverse=True,
            )[:5000]
        self.valuation_cache.clear()

    def add_days(self, selected, *, agent_rows=None, partial=False):
        daily = defaultdict(list)
        for row in selected:
            daily[local_slot(row.hour, self.zone)[0]].append(row)
        for key, values in daily.items():
            if key in self.days:
                self.days[key]["counts"].add(values, partial=partial)
                self.days[key]["classes"].add(values)
                by_hour = defaultdict(list)
                for row in values:
                    by_hour[row.hour].append(row)
                for hour, group in by_hour.items():
                    span = AnalysisSpan(
                        requested_from=hour,
                        counted_from=hour,
                        until=min(hour + 3600, self.view.span.until),
                        approx=self.view.span.approx,
                    )
                    amounts = self.scope(span=span, payment=not self.restricted)
                    amounts.add(group, partial=partial)
                    self.days[key]["hours"][hour]["amounts"] = amounts.result(
                        payment=not (
                            self.filters.host
                            or self.filters.agent
                            or self.filters.model
                            or self.filters.slot
                        )
                    )
                    self.scope_count -= 1
                    self.accumulator_count -= 1
        for row in selected:
            key = local_slot(row.hour, self.zone)[0]
            if key not in self.days:
                continue
            day = self.days[key]
            hour = day["hours"].get(row.hour)
            if hour is not None:
                hour["counts"].add([row])
                if row.contributes_usage:
                    for dimension, value in (
                        ("host", row.host_id),
                        ("model", row.model),
                    ):
                        if value not in hour[dimension]:
                            self.reserve()
                        hour[dimension][value] += row.tokens or 0
        for row in selected if agent_rows is None else agent_rows:
            key = local_slot(row.hour, self.zone)[0]
            hour = self.days.get(key, {}).get("hours", {}).get(row.hour)
            if hour is not None and row.contributes_usage:
                if row.agent not in hour["agent"]:
                    self.reserve()
                hour["agent"][row.agent] += row.tokens or 0

    def result(self) -> dict[str, Any]:
        restricted = bool(
            self.filters.host or self.filters.agent or self.filters.model or self.filters.slot
        )
        groups = []
        for key, value in self.groups.items():
            result = value["counts"].result(
                payment=self.filters.group in {"account", "pool", "card"} and not restricted
            )
            selected = (
                key in getattr(self.filters, self.filters.group, ())
                if self.filters.group in self.facets
                else False
            )
            groups.append(
                {
                    "key": list(key) if isinstance(key, tuple) else key,
                    "selected": selected,
                    **result,
                    "trend": [
                        {"at": at, **counts.result()}
                        for at, counts in sorted(value["trend"].items())
                    ],
                }
            )
        facets = {
            dimension: bounded_items(
                [{"key": key, **value.result(payment=False)} for key, value in entries.items()]
            )
            for dimension, entries in self.facets.items()
        }
        days = []
        for key, value in sorted(self.days.items(), reverse=True):
            hours = []
            for hour, state in sorted(value["hours"].items(), reverse=True):
                hours.append(
                    {
                        "start": hour,
                        "label": local_slot(hour, self.zone)[2],
                        **state["counts"].result(),
                        "totals": state.get(
                            "amounts",
                            {
                                **state["counts"].result(),
                                **{
                                    name: Value(
                                        unit="ratio" if name == "payback" else "money",
                                        currency=None
                                        if name == "payback"
                                        else self.baseline.defaults.display_currency,
                                        cohort_id=f"hour:{hour}",
                                        as_of=int(self.view.generated_at),
                                        reason="no_hour_usage",
                                    )
                                    for name in ("api_value", "paid", "payback")
                                },
                            },
                        ),
                        **{
                            f"top_{dimension}": max(
                                state[dimension],
                                key=lambda item: (state[dimension][item], item),
                                default=None,
                            )
                            for dimension in ("host", "agent", "model")
                        },
                    }
                )
            days.append(
                {
                    "day": key,
                    **value["counts"].result(payment=False),
                    "classes": value["classes"].result(),
                    "hours": hours,
                }
            )
        return {
            "span": self.view.span,
            "totals": self.total.result(payment=not restricted),
            "series": [
                {"at": at, **scope.result(payment=False)}
                for at, scope in sorted(self.series.items())
            ],
            "groups": bounded_items(groups),
            "facets": facets,
            "heat": [{"slot": key, **value.result()} for key, value in self.heat.items()],
            "days": days,
            "classes": self.classes.result(),
            "cache_savings": {"state": "unknown", "reason": "requires_savings_projection"},
            "rows": self.details if "rows" in self.filters.include else [],
            "rows_truncated": "rows" in self.filters.include and self.rows_seen > 5000,
        }


def bounded_items(items):
    def amount(item):
        return Fraction(item["api_value"].low or 0)

    ordered = sorted(items, key=lambda item: (-amount(item), str(item["key"])))
    if len(ordered) <= 200:
        return ordered
    remainder = ordered[200:]
    values = [item["api_value"] for item in remainder]
    known = all(value.low is not None for value in values)
    low = sum((Fraction(value.low) for value in values), Fraction(0)) if known else None
    high = (
        sum((Fraction(value.high) for value in values), Fraction(0))
        if all(value.high is not None for value in values)
        else None
    )
    state = (
        "unknown"
        if low is None
        else "partial"
        if any(value.state == "partial" for value in values)
        else "exact"
        if low == high
        else "range"
    )
    value = values[0].model_copy(
        update={
            "state": state,
            "low": endpoint(low) if low is not None else None,
            "high": endpoint(high) if high is not None and known else None,
            "reason": "unknown_remainder_value" if not known else None,
            "cohort_id": values[0].cohort_id + ":remainder",
        }
    )
    return [
        *ordered[:200],
        {
            "key": None,
            "remainder": True,
            "api_value": value,
            **{
                name: None
                if any(item[name] is None for item in remainder)
                else sum(item[name] for item in remainder)
                for name in ("tokens", "requests", "failed")
            },
        },
    ]
