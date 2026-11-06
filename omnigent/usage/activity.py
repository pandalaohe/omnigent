"""Pure projections of hourly usage: amounts, coverage, failures and attribution."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence, Set
from dataclasses import asdict
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnigent.stores.usage_hours_store import COUNTED_BASES, HourRow
from omnigent.usage.ranges import Span, bucket_start


class MarkSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    notice_pct: float = Field(default=5, ge=0, le=100, allow_inf_nan=False)
    serious_pct: float = Field(default=20, ge=0, le=100, allow_inf_nan=False)
    down_pct: float = Field(default=50, ge=0, le=100, allow_inf_nan=False)
    min_requests: int = Field(default=20, ge=1, strict=True)
    recent_hours: int = Field(default=2, ge=1, le=24, strict=True)

    @model_validator(mode="after")
    def ascending_levels(self) -> MarkSettings:
        if not self.notice_pct <= self.serious_pct <= self.down_pct:
            raise ValueError("mark thresholds must be in ascending order")
        return self


def attributed_rows(
    rows: Sequence[HourRow],
    accounts: Mapping[str, Mapping[str, Any]],
    meters: Mapping[tuple[str, str], set[str | None]],
    native_accounts: Sequence[str] = (),
    hostless: Set[str] = frozenset(),
) -> dict[str, list[HourRow]]:
    result: dict[str, list[HourRow]] = defaultdict(list)
    coverage = {
        (row.account_id, row.host_id, row.hour_utc)
        for row in rows
        if row.account_id in accounts
        and accounts[row.account_id].get("activity_writer") != "otlp"
        and (
            row.priced
            or any(
                value is not None
                for value in (
                    row.input_tokens,
                    row.cached_tokens,
                    row.cache_write_tokens,
                    row.output_tokens,
                    row.reasoning_tokens,
                    row.total_tokens,
                    row.requests,
                    row.failed,
                )
            )
        )
    }
    account_wide = {
        account
        for account in hostless
        if account in accounts and accounts[account].get("activity_writer") != "otlp"
    } | {account for account, host, _ in coverage if not host}
    # V1 also treated an empty activity section as account-wide replacement.
    account_wide.update(
        account
        for account, record in accounts.items()
        if record.get("has_activity")
        and record.get(
            "activity_schema_version", (record.get("report") or {}).get("schema_version", 1)
        )
        == 1
        and record.get("activity_writer") != "otlp"
    )
    linked: dict[str, str] = {}
    for account, record in sorted(accounts.items()):
        if record.get("product"):
            linked.setdefault(str(record["product"]), account)
    for row in rows:
        target = row.account_id
        if target.startswith("native:"):
            product = target.removeprefix("native:")
            matches = meters.get((product, row.model), set())
            if matches:
                meter = next(iter(matches)) if len(matches) == 1 else None
                target = (
                    meter
                    if meter
                    and (meter in accounts or meter in native_accounts or meter == product)
                    else product
                )
            else:
                target = linked.get(product, product)
            if target in account_wide or (target, row.host_id, row.hour_utc) in coverage:
                continue
        elif target not in accounts:
            # A removed source's hours never resurrect its tile.
            continue
        result[target].append(row)
    return dict(result)


def totals(rows: Sequence[HourRow]) -> dict[str, Any]:
    return {
        **_totals(rows),
        "metered": _totals([row for row in rows if row.basis in COUNTED_BASES]),
    }


def _totals(rows: Sequence[HourRow]) -> dict[str, Any]:
    cost = sum(row.cost_usd for row in rows)
    counted = [row for row in rows if row.basis in COUNTED_BASES]
    covered = [row for row in rows if row.priced and (row.total_tokens or 0) > 0]
    covered_cost = sum(row.cost_usd for row in covered)
    covered_tokens = sum(row.total_tokens or 0 for row in covered)
    failures = [row for row in rows if row.requests is not None and row.failed is not None]
    requests = sum(row.requests or 0 for row in failures)
    failed = sum(row.failed or 0 for row in failures)
    reasons: dict[str, int] = defaultdict(int)
    for row in failures:
        for reason, count in (row.reasons or {}).items():
            reasons[reason] += count
    return {
        "cost_usd": cost if any(row.priced for row in rows) else None,
        "counted_cost_usd": sum(row.cost_usd for row in counted if row.priced),
        "tokens": (
            sum(row.total_tokens or 0 for row in rows) or None
            if all(row.total_tokens is not None for row in rows)
            else None
        ),
        "unpriced": sum(not row.priced for row in rows),
        "partial_rows": sum(row.coverage == "partial" for row in rows),
        "per_million": covered_cost / covered_tokens * 1e6 if covered_tokens else None,
        "covered_cost_usd": covered_cost,
        "covered_tokens": covered_tokens,
        "cost_coverage_pct": covered_cost / cost * 100 if cost > 0 else None,
        "tokens_complete": bool(rows)
        and all((row.total_tokens or 0) > 0 for row in rows if row.priced),
        "requests": requests if failures else None,
        "failed": failed if failures else None,
        "failure_pct": failed / requests * 100 if requests else None,
        "reasons": dict(reasons),
        "last_at": max((row.updated_at for row in rows), default=None),
    }


def amount_mark(rows: Sequence[HourRow], span: Span) -> str | None:
    if any(row.basis not in COUNTED_BASES for row in rows):
        return "~"
    if any(not row.priced for row in rows):
        return "≥"
    return "≈" if rows and span.approx else None


def error_mark(
    rows: Sequence[HourRow], now: float, fresh: bool, settings: MarkSettings
) -> dict[str, Any] | None:
    if not fresh:
        return None
    hour = int(now // 3600) * 3600
    recent = hour - (settings.recent_hours - 1) * 3600
    active = [row for row in rows if int((now - 86400) // 3600) * 3600 <= row.hour_utc <= now]
    counts = totals(active)
    if (counts["requests"] or 0) < settings.min_requests or not any(
        row.hour_utc >= recent and (row.failed or 0) > 0 for row in active
    ):
        return None
    rate = counts["failure_pct"]
    if rate is None or rate < settings.notice_pct:
        return None
    level = (
        "down"
        if rate >= settings.down_pct
        else "serious"
        if rate >= settings.serious_pct
        else "notice"
    )
    return {
        "level": level,
        "requests": counts["requests"],
        "failed": counts["failed"],
        "rate": rate,
    }


def summarize(rows: Sequence[HourRow], span: Span, zone: ZoneInfo) -> dict[str, Any]:
    selected = [row for row in rows if span.counted_from <= row.hour_utc < span.until]
    buckets: dict[int, list[HourRow]] = defaultdict(list)
    for row in selected:
        buckets[bucket_start(row.hour_utc, span, zone)].append(row)
    return {
        "span": asdict(span),
        "mark": amount_mark(selected, span),
        "totals": totals(selected),
        "series": [{"at": at, **totals(group)} for at, group in sorted(buckets.items())],
    }
