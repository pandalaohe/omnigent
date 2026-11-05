"""Calendar ranges and non-overlapping UTC-hour accounting boundaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from math import ceil
from typing import Literal
from zoneinfo import ZoneInfo

from omnigent.usage.contract import UsagePeriodView

RangeKey = Literal["today", "h24", "d7", "period", "d30", "d90", "custom"]


@dataclass(frozen=True)
class Span:
    requested_from: float
    counted_from: int
    until: float
    approx: bool

    @classmethod
    def starting(cls, start: float, until: float) -> Span:
        counted = int(start // 3600) * 3600
        return cls(start, counted, until, counted != start)


def range_spans(
    key: RangeKey,
    now: float,
    zone: ZoneInfo,
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    period: UsagePeriodView | None = None,
) -> tuple[Span, Span]:
    local_now = datetime.fromtimestamp(now, zone)

    def midnight(day: date) -> float:
        return datetime.combine(day, time(), zone).timestamp()

    end = now
    if key == "today":
        start = midnight(local_now.date())
    elif key == "h24":
        start = now - 86400
    elif key in {"d7", "d30", "d90"}:
        days = {"d7": 7, "d30": 30, "d90": 90}[key]
        start = midnight(local_now.date() - timedelta(days=days - 1))
    elif key == "period":
        start = period.start if period is not None else now - 30 * 86400
    elif key == "custom":
        if (
            from_date is None
            or to_date is None
            or from_date > to_date
            or to_date > local_now.date()
        ):
            raise ValueError("Custom needs from <= to <= today")
        start = midnight(from_date)
        end = min(now, midnight(to_date + timedelta(days=1)))
    else:
        raise ValueError("Unknown usage range")
    current = Span.starting(start, end)
    if key == "today":
        previous_start = midnight(local_now.date() - timedelta(days=1))
        # Calendar arithmetic keeps the same local clock through a DST change.
        previous_end = (local_now - timedelta(days=1)).timestamp()
        previous = Span.starting(previous_start, previous_end)
    else:
        # The previous span ends at the accounting boundary, with the same
        # number of hourly starts even when the end is inside an hour.
        counted_length = ceil((end - current.counted_from) / 3600) * 3600
        previous = Span(
            current.counted_from - counted_length,
            current.counted_from - counted_length,
            current.counted_from,
            False,
        )
    return current, previous


def bucket_start(hour: int, span: Span, zone: ZoneInfo) -> int:
    length = span.until - span.requested_from
    if length <= 48 * 3600:
        return hour
    local = datetime.fromtimestamp(hour, zone)
    day = local.date()
    if length > 45 * 86400:
        day -= timedelta(days=day.weekday())
    return int(datetime.combine(day, time(), zone).timestamp())
