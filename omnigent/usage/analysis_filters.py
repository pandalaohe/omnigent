"""The normalized repeated-value analysis contract and local-hour assignment."""

import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from typing import cast
from zoneinfo import ZoneInfo

from omnigent.usage.ranges import RangeKey, Span, range_spans

DIMENSIONS = ("account", "pool", "model", "card", "host", "agent")


@dataclass(frozen=True)
class AnalysisFilter:
    group: str
    range: str
    tz: str = "UTC"
    account: tuple[str, ...] = ()
    model: tuple[str, ...] = ()
    host: tuple[str, ...] = ()
    agent: tuple[str, ...] = ()
    day: str | None = None
    slot: str | None = None
    from_date: date | None = None
    to_date: date | None = None
    include: tuple[str, ...] = ()

    @classmethod
    def parse(cls, query, now: float):
        def one(name, default=None):
            values = query.getlist(name)
            if len(values) > 1:
                raise ValueError("Use one selection format")
            return values[0] if values else default

        if any(name in query for name in ("dimension", "selection", "exclude", "pool", "card")):
            raise ValueError("Use one selection format")
        group, key, tz = one("group"), one("range"), one("tz", "UTC")
        if group not in DIMENSIONS or key not in {"today", "d7", "d30", "d90", "custom"}:
            raise ValueError("Use one selection format")
        assert isinstance(tz, str)
        today = datetime.fromtimestamp(now, ZoneInfo(tz)).date()
        day, slot = one("day"), one("slot")
        if slot is not None and re.fullmatch(r"[1-7]-(?:[01][0-9]|2[0-3])", slot) is None:
            raise ValueError("Use one selection format")
        if day is not None:
            if (
                re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) is None
                or not today - timedelta(days=399) <= date.fromisoformat(day) <= today
                or "from" in query
                or "to" in query
            ):
                raise ValueError("Use one selection format")
        start, end = one("from"), one("to")
        if any(
            value is not None and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None
            for value in (start, end)
        ):
            raise ValueError("Use one selection format")
        if (start is not None or end is not None) and key != "custom":
            raise ValueError("Use one selection format")
        selected = {}
        for name, length in (("account", 64), ("model", 128), ("host", 64), ("agent", 32)):
            values = query.getlist(name)
            if len(values) > 64 or any(len(value) > length for value in values):
                raise ValueError("Use one selection format")
            if name == "agent" and any(
                value and re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,31}", value) is None
                for value in values
            ):
                raise ValueError("Use one selection format")
            selected[name] = tuple(sorted(set(values)))
        include = tuple(sorted(set(query.getlist("include"))))
        if set(include) - {"previous", "rows"}:
            raise ValueError("Use one selection format")
        result = cls(
            group=group,
            range=key,
            tz=tz,
            day=day,
            slot=slot,
            from_date=date.fromisoformat(start) if start else None,
            to_date=date.fromisoformat(end) if end else None,
            include=include,
            **selected,
        )
        result.spans(now)
        return result

    def echo(self):
        values = asdict(self)
        values.pop("include")
        values["from"] = values.pop("from_date")
        values["to"] = values.pop("to_date")
        return values

    def spans(self, now):
        zone = ZoneInfo(self.tz)
        if self.from_date and self.to_date and (self.to_date - self.from_date).days >= 370:
            raise ValueError("usage analysis supports at most 370 local dates")
        if self.day:
            current = local_day(date.fromisoformat(self.day), zone, now)
            previous = local_day(date.fromisoformat(self.day) - timedelta(days=1), zone, now)
            return current, previous
        current, previous = range_spans(
            cast(RangeKey, self.range), now, zone, from_date=self.from_date, to_date=self.to_date
        )
        counted = math.ceil(current.requested_from / 3600) * 3600
        current = Span(current.requested_from, counted, current.until, current.approx)
        if self.range != "today":
            length = math.ceil((current.until - counted) / 3600) * 3600
            previous = Span(counted - length, counted - length, counted, current.approx)
        else:
            previous = Span(
                previous.requested_from,
                math.ceil(previous.requested_from / 3600) * 3600,
                previous.until,
                previous.approx,
            )
        return current, previous

    def matches(self, row, *, omit=None):
        values = {
            "account": row.parent_account_id,
            "model": row.model,
            "host": row.host_id,
            "agent": row.agent,
        }
        if any(
            getattr(self, name) and values[name] not in getattr(self, name)
            for name in values
            if name != omit
        ):
            return False
        return (
            omit == "slot"
            or not self.slot
            or local_slot(row.hour, ZoneInfo(self.tz))[1] == self.slot
        )


def local_day(day: date, zone: ZoneInfo, now: float) -> Span:
    start = datetime.combine(day, time(), zone).timestamp()
    end = min(now, datetime.combine(day + timedelta(days=1), time(), zone).timestamp())
    return Span(start, math.ceil(start / 3600) * 3600, end, start % 3600 != 0)


def local_slot(hour: int, zone: ZoneInfo):
    local = datetime.fromtimestamp(hour, zone)
    return (
        local.date().isoformat(),
        f"{local.isoweekday()}-{local.hour:02d}",
        local.strftime("%H:%M %z"),
    )
