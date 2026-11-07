"""Read vendor usage responses without retaining their original payloads."""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException
from typing import Any

from pydantic import ValidationError

from omnigent.usage.contract import ActivityEntryV2, UsageReportV2


class ImportFormatError(ValueError):
    """The response does not match the selected vendor format."""


@dataclass(frozen=True)
class ImportOptions:
    units_per_usd: float = 500_000
    correction: bool = False
    observed_at: float | None = None


class _ImportReport(UsageReportV2):
    # Imports are bounded by bytes; the public report retains its 500-row cap.
    activity: list[ActivityEntryV2] | None = None


def _object(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImportFormatError("Expected a vendor JSON object")
    return value


def _list(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ImportFormatError("Expected a vendor JSON array")
    return value


def _number(value: Any, *, negative: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ImportFormatError("Expected a finite number")
    if not negative and value < 0:
        raise ImportFormatError("Expected a non-negative number")
    return value


def _tokens(row: dict[str, Any], name: str, *, required: bool = False) -> int | None:
    value = row.get(name)
    if value is None and not required:
        return None
    if type(value) is not int or value < 0:
        raise ImportFormatError("Expected a non-negative integer token or request count")
    return value


def _time(value: Any) -> float:
    if isinstance(value, str):
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is not None:
                return _number(stamp.timestamp())
        except (ValueError, OverflowError):
            pass
        raise ImportFormatError("Expected a timestamp with a UTC offset")
    return _number(value)


def _model(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > 128:
        raise ImportFormatError("Expected a model name of at most 128 characters")
    return value


def _cents(value: Any) -> float:
    if not isinstance(value, str):
        raise ImportFormatError("Expected a decimal amount in cents")
    try:
        amount = Decimal(value)
        if not amount.is_finite() or amount < 0:
            raise ImportFormatError("Expected non-negative finite cents")
        return _number(float(amount / 100))
    except (DecimalException, OverflowError):
        raise ImportFormatError("Expected a decimal amount in cents") from None


def _openai_tokens(row: dict[str, Any]) -> dict[str, Any]:
    total = _tokens(row, "input_tokens", required=True)
    output = _tokens(row, "output_tokens", required=True)
    cached = _tokens(row, "input_cached_tokens")
    creation = _tokens(row, "input_cache_write_tokens")
    uncached = _tokens(row, "input_uncached_tokens")
    if uncached is None and total is not None and cached is not None and creation is not None:
        uncached = max(0, total - cached - creation)
    return {
        "uncached_input_tokens": uncached,
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": creation,
        "output_tokens": output,
        "tokens": (total or 0) + (output or 0),
        "requests": _tokens(row, "num_model_requests", required=True),
    }


def _anthropic_tokens(row: dict[str, Any], analytics: bool) -> dict[str, Any]:
    creation = _object(row.get("cache_creation"))
    lifetimes = (
        [
            _tokens(creation, name, required=True)
            for name in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
        ]
        if "ephemeral_5m_input_tokens" in creation
        else [_tokens(creation, name, required=True) for name in ("ephemeral_5m", "ephemeral_1h")]
    )
    return {
        "uncached_input_tokens": _tokens(row, "uncached_input_tokens", required=True),
        "cache_read_input_tokens": _tokens(row, "cache_read_input_tokens", required=True),
        "cache_creation_input_tokens": sum(v for v in lifetimes if v is not None),
        "output_tokens": _tokens(row, "output_tokens", required=True),
        "requests": _tokens(row, "requests", required=True) if analytics else None,
    }


def _activity(
    payload: Any, options: ImportOptions, observed: float, *, vendor: str, costs: bool
) -> UsageReportV2:
    body = _object(payload)
    if body.get("data_refreshed_at") is not None:
        observed = min(observed, _time(body["data_refreshed_at"]))
    grain = 86400 if costs else 3600
    grain_name = "day" if costs else "hour"
    intervals: dict[int, tuple[float, bool]] = {}
    entries: dict[tuple[int, str], dict[str, Any]] = {}
    for raw_bucket in _list(body.get("data")):
        bucket = _object(raw_bucket)
        start = _time(bucket.get("start_time" if vendor == "openai" else "starting_at"))
        end = _time(bucket.get("end_time" if vendor == "openai" else "ending_at"))
        analytics = vendor == "anthropic" and "results" not in bucket
        if start % grain or not start < end <= start + grain:
            raise ImportFormatError(f"Expected a UTC-aligned {grain_name} bucket interval")
        hour = int(start)
        if hour in intervals:
            previous_end, previous_analytics = intervals[hour]
            # Analytics lists dimension rows, rather than one container per bucket.
            if not analytics or not previous_analytics or previous_end != end:
                raise ImportFormatError(f"Duplicate {grain_name} bucket interval")
        intervals[hour] = (end, analytics)
        coverage = "complete" if end - start == grain and end <= observed - 600 else "partial"
        rows = [bucket] if analytics else _list(bucket.get("results"))
        for raw_row in rows:
            row = _object(raw_row)
            group = _object(row.get("group")) if analytics else row
            if analytics:
                for dimension, value in group.items():
                    if dimension != "model" and value is not None and value != "":
                        name = ascii(dimension)[:80]
                        raise ImportFormatError(
                            f"Unsupported Analytics grouping dimension {name}; "
                            "drop that grouping and push all pages in one request"
                        )
            model = "" if vendor == "openai" and costs else _model(group.get("model"))
            if costs:
                metric = _object(row.get("cost")) if analytics else row
                if vendor == "openai":
                    amount = _object(metric.get("amount"))
                    if amount.get("currency") != "usd":
                        raise ImportFormatError("Costs require usd currency")
                    values = {"cost_usd": _number(amount.get("value"))}
                else:
                    if metric.get("currency", "USD" if analytics else None) != "USD":
                        raise ImportFormatError("Costs require USD currency")
                    values = {"cost_usd": _cents(metric.get("amount"))}
            else:
                metric = _object(row.get("usage")) if analytics else row
                values = (
                    _openai_tokens(metric)
                    if vendor == "openai"
                    else _anthropic_tokens(metric, analytics)
                )
            key = (hour, model)
            if key in entries:
                entry = entries[key]
                for name, value in values.items():
                    entry[name] = (
                        None if value is None or entry[name] is None else entry[name] + value
                    )
            else:
                entries[key] = {
                    "hour": hour,
                    "model": model,
                    "cost_basis": "billed" if costs else "metered",
                    "coverage": coverage,
                    "correction": options.correction and coverage == "complete",
                    **values,
                }
    return _ImportReport.model_validate(
        {"schema_version": 2, "observed_at": observed, "activity": list(entries.values())}
    )


def _openai_usage(payload: Any, options: ImportOptions, observed: float) -> UsageReportV2:
    return _activity(payload, options, observed, vendor="openai", costs=False)


def _openai_costs(payload: Any, options: ImportOptions, observed: float) -> UsageReportV2:
    return _activity(payload, options, observed, vendor="openai", costs=True)


def _anthropic_usage(payload: Any, options: ImportOptions, observed: float) -> UsageReportV2:
    return _activity(payload, options, observed, vendor="anthropic", costs=False)


def _anthropic_cost(payload: Any, options: ImportOptions, observed: float) -> UsageReportV2:
    return _activity(payload, options, observed, vendor="anthropic", costs=True)


_PERIODS = {"daily": 1440, "weekly": 10080, "monthly": 43200}


def _value_window(
    name: str,
    used: float,
    limit: float | None = None,
    *,
    kind: str | None = None,
    minutes: int | None = None,
    reset: Any = None,
) -> dict[str, Any]:
    window: dict[str, Any] = {
        "id": name,
        "kind": kind or "term",
        "used_value": used,
        "unit": "USD",
    }
    if limit is not None:
        window.update(
            limit_value=limit, percent_used=min(100, used / limit * 100) if limit else 100
        )
    if minutes is not None:
        window["window_minutes"] = minutes
    if reset is not None:
        window["resets_at"] = _time(reset)
    return window


def _period(value: Any) -> tuple[str, int | None]:
    if value is None:
        return "term", None
    if value not in _PERIODS:
        raise ImportFormatError("Unsupported budget reset interval")
    return value, _PERIODS[value]


def _openrouter_key(payload: Any, _options: ImportOptions, observed: float) -> UsageReportV2:
    data = _object(_object(payload).get("data"))
    windows = []
    if "limit" not in data:
        raise ImportFormatError("Expected OpenRouter key limit")
    if data["limit"] is not None:
        limit = _number(data["limit"])
        remaining = _number(data.get("limit_remaining"), negative=True)
        kind, minutes = _period(data.get("limit_reset"))
        windows.append(
            _value_window("limit", max(0, limit - remaining), limit, kind=kind, minutes=minutes)
        )
    for index, raw in enumerate(_list(data.get("budgets", []))):
        budget = _object(raw)
        kind, minutes = _period(budget.get("reset_interval"))
        windows.append(
            _value_window(
                f"budget-{index}",
                _number(budget.get("spend_usd")),
                _number(budget.get("limit_usd")),
                kind=kind,
                minutes=minutes,
                reset=budget.get("resets_at"),
            )
        )
    for name, period in (("day", "daily"), ("week", "weekly"), ("month", "monthly")):
        windows.append(
            _value_window(f"spent-{name}", _number(data.get(f"usage_{period}")), kind=period)
        )
    return UsageReportV2.model_validate(
        {"schema_version": 2, "observed_at": observed, "windows": windows}
    )


def _openrouter_credits(payload: Any, _options: ImportOptions, observed: float) -> UsageReportV2:
    data = _object(_object(payload).get("data"))
    remaining = _number(data.get("total_credits")) - _number(data.get("total_usage"))
    return UsageReportV2.model_validate(
        {
            "schema_version": 2,
            "observed_at": observed,
            "balances": [{"id": "credits", "remaining": remaining, "unit": "USD"}],
        }
    )


def _newapi_user(payload: Any, options: ImportOptions, observed: float) -> UsageReportV2:
    body = _object(payload)
    if body.get("success") is False:
        raise ImportFormatError("Relay did not return a successful user reading")
    data = _object(body.get("data"))
    remaining = _number(data.get("quota"), negative=True) / options.units_per_usd
    used = _number(data.get("used_quota")) / options.units_per_usd
    return UsageReportV2.model_validate(
        {
            "schema_version": 2,
            "observed_at": observed,
            "windows": [_value_window("used", used)],
            "balances": [{"id": "wallet", "remaining": remaining, "unit": "USD"}],
        }
    )


def _litellm_key(payload: Any, _options: ImportOptions, observed: float) -> UsageReportV2:
    data = _object(_object(payload).get("info"))
    spend = _number(data.get("spend"))
    limit = data.get("max_budget")
    if limit is None:
        window = _value_window("spend", spend)
    else:
        minutes = None
        duration = data.get("budget_duration")
        if duration is not None:
            if (
                not isinstance(duration, str)
                or (match := re.fullmatch(r"([1-9][0-9]*)(s|m|h|d)", duration)) is None
            ):
                raise ImportFormatError("Unsupported LiteLLM budget duration")
            seconds = int(match[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[match[2]]
            if seconds % 60 == 0:
                minutes = seconds // 60
        kind = {1440: "daily", 10080: "weekly", 43200: "monthly"}.get(minutes or 0, "term")
        window = _value_window(
            "budget",
            spend,
            _number(limit),
            kind=kind,
            minutes=minutes,
            reset=data.get("budget_reset_at"),
        )
    return UsageReportV2.model_validate(
        {"schema_version": 2, "observed_at": observed, "windows": [window]}
    )


READERS: Mapping[str, Callable[[Any, ImportOptions, float], UsageReportV2]] = {
    "openai-usage": _openai_usage,
    "openai-costs": _openai_costs,
    "anthropic-usage": _anthropic_usage,
    "anthropic-cost": _anthropic_cost,
    "openrouter-key": _openrouter_key,
    "openrouter-credits": _openrouter_credits,
    "newapi-user": _newapi_user,
    "litellm-key": _litellm_key,
}
ACTIVITY_FORMATS = frozenset({"openai-usage", "openai-costs", "anthropic-usage", "anthropic-cost"})


def read_import(name: str, payload: Any, options: ImportOptions, now: float) -> UsageReportV2:
    observed = now if options.observed_at is None else options.observed_at
    age_limit = 400 * 86400 if name in ACTIVITY_FORMATS else 3600
    if (
        type(observed) not in (int, float)
        or not math.isfinite(observed)
        or not now - age_limit <= observed <= now + 300
    ):
        raise ImportFormatError("observed_at is outside the allowed observation interval")
    if _number(options.units_per_usd) <= 0:
        raise ImportFormatError("units_per_usd must be positive")
    try:
        return READERS[name](payload, options, observed)
    except (ValidationError, TypeError, OverflowError):
        raise ImportFormatError("Invalid vendor fields for selected format") from None
