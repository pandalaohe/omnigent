"""Complete-window analysis orchestration with an explicit shared scan budget."""

import time
from dataclasses import replace
from typing import Any

from omnigent.stores.usage_agent_parts import MEASURES, split_row
from omnigent.stores.usage_hours_store import UsageReadLimit
from omnigent.usage.analysis_filters import AnalysisFilter
from omnigent.usage.analysis_fold import AnalysisFold
from omnigent.usage.pricing import PricingBaseline
from omnigent.usage.pricing_projection import add_card_models

CARD_MODEL_LIMIT = 20000


class ScanBudget:
    def __init__(self, limit=400000):
        self.limit = limit
        self.scanned = 0

    def add(self, count):
        self.scanned += count
        if self.scanned > self.limit:
            raise UsageReadLimit("scan_over_limit")


def split_analysis(view, raw):
    sources = {
        (row.account_id, row.hour_utc, row.model, row.host_id, row.basis, row.priced): row
        for row in raw
    }
    result = []
    fields = {**MEASURES, "total_tokens": "tokens"}
    for row in view.rows:
        if row.agent:
            result.append(row)
            continue
        source = sources.get(
            (row.account_id, row.hour, row.model, row.host_id, row.basis, row.priced)
        )
        if source is None or not source.agent_parts:
            result.append(row)
            continue
        pieces = split_row(source, source.agent_parts)
        for piece in pieces:
            updates = {
                field: getattr(piece, name) if getattr(row, field) is not None else None
                for name, field in fields.items()
            }
            updates["agent"] = piece.agent
            updates["agent_split"] = True
            updates["agent_money_unknown"] = piece.agent_money_unknown
            updates["cost_usd"] = float(piece.cost_usd) if piece.cost_usd is not None else None
            if piece.agent_money_unknown and row.price_estimate is not None:
                updates["price_estimate"] = row.price_estimate.model_copy(
                    update={
                        "state": "unknown",
                        "low_usd": None,
                        "high_usd": None,
                        "cache_savings_low_usd": None,
                        "cache_savings_high_usd": None,
                        "missing": [*row.price_estimate.missing, "agents_money_over_total"],
                    }
                )
            elif len(pieces) > 1 and row.price_estimate is not None:
                updates["price_estimate"] = row.price_estimate.model_copy(
                    update={"missing": [*row.price_estimate.missing, "ambiguous_hour_cohort"]}
                )
            result.append(row.model_copy(update=updates))
    return result


def collect_window(
    reader, filters, baseline, owner, evidence, budget, *, only_days=False
) -> tuple[dict[str, Any], str | None]:
    contexts = {None: {}, "host": {}}
    if baseline.cards:
        for view, raw in reader.chunks(include_agents=False):
            budget.add(getattr(raw, "scanned_count", len(raw)))
            for omit in contexts:
                rows = [
                    row
                    for row in view.rows
                    if omit == "host" or not filters.host or row.host_id in filters.host
                ]
                add_card_models(
                    contexts[omit], rows, reader.metadata, baseline, limit=CARD_MODEL_LIMIT
                )
    fold = AnalysisFold(reader.metadata, baseline, filters, contexts)
    try:
        for view, raw in reader.chunks():
            budget.add(getattr(raw, "scanned_count", len(raw)))
            if evidence is not None:
                evidence.attach_prices(owner, view, bounded=True)
            rows = split_analysis(view, raw)
            if only_days:
                fold.valuation_cache.clear()
                fold.add_days(
                    [
                        row
                        for row in (rows if filters.agent else view.rows)
                        if filters.matches(row)
                    ],
                    agent_rows=[row for row in rows if filters.matches(row)],
                    partial=view.partial and not any(row.reason for row in view.rows),
                )
                fold.valuation_cache.clear()
            else:
                fold.add(rows, main_rows=view.rows, partial=view.partial)
    except UsageReadLimit as exc:
        return fold.result(), str(exc)
    return fold.result(), None


def read_analysis_v2(
    service,
    owner,
    filters: AnalysisFilter,
    *,
    evidence=None,
    now=None,
    chunk_size=2000,
    hours_only=False,
):
    now = time.time() if now is None else now
    baseline = service.pricing_store.read(owner) if service.pricing_store else PricingBaseline()
    budget = ScanBudget()
    current, previous = filters.spans(now)
    payload: dict[str, Any] = {}
    reason = None
    root_reader = service.analysis_reader(
        owner,
        range_key=filters.range,
        tz=filters.tz,
        from_date=filters.from_date,
        to_date=filters.to_date,
        now=now,
        span_override=current,
        chunk_size=chunk_size,
    )
    for prior, span in ((False, current), (True, previous)):
        if prior and "previous" not in filters.include:
            break
        reader = root_reader if not prior else root_reader.for_span(span)
        try:
            result, incomplete = collect_window(reader, filters, baseline, owner, evidence, budget)
        except UsageReadLimit as exc:
            result = AnalysisFold(
                reader.metadata,
                baseline.model_copy(update={"cards": []}),
                filters,
                {None: {}, "host": {}},
            ).result()
            incomplete = str(exc)
        reason = reason or incomplete
        if not prior:
            payload = result
        else:
            payload["previous"] = {name: result[name] for name in ("span", "totals", "series")}
        if reason:
            break
    payload["hours"] = [
        hour
        for day in payload.get("days", [])
        if day["day"] == filters.day
        for hour in day["hours"]
        if hour["last_at"] is not None
    ]
    if filters.day and filters.range != "custom" and reason is None and not hours_only:
        all_days = replace(filters, day=None, include=())
        span, _ = all_days.spans(now)
        try:
            daily, incomplete = collect_window(
                root_reader.for_span(span),
                all_days,
                baseline,
                owner,
                evidence,
                budget,
                only_days=True,
            )
            payload["days"] = daily["days"]
            reason = reason or incomplete
        except UsageReadLimit as exc:
            reason = str(exc)
    return {
        **payload,
        "generated_at": now,
        "filter": filters.echo(),
        "tz": filters.tz,
        "complete": reason is None,
        "incomplete_reason": reason,
        "approx": current.approx,
    }
