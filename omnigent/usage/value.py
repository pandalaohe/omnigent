"""Shared bounded monetary values for baseline and source-price projections."""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from typing import Literal

from pydantic import BaseModel, Field, model_validator


def endpoint(value: Fraction | Decimal | int) -> str:
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        number = (
            Decimal(value.numerator) / Decimal(value.denominator)
            if isinstance(value, Fraction)
            else Decimal(value)
        )
        text = format(number, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text


class Coverage(BaseModel):
    priced_units: int = 0
    usage_units: int | None = None
    known_paid: int = 0
    total_items: int = 0


class Value(BaseModel):
    state: Literal["exact", "range", "partial", "unknown"] = "unknown"
    low: str | None = None
    high: str | None = None
    unit: str
    currency: str | None = None
    cohort_id: str
    coverage: Coverage = Field(default_factory=Coverage)
    as_of: int
    reason: str | None = None

    @model_validator(mode="after")
    def valid_bounds(self) -> Value:
        if self.state == "unknown":
            if self.low is not None or self.high is not None:
                raise ValueError("unknown values have no endpoints")
            return self
        if any(
            value is not None and re.fullmatch(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value) is None
            for value in (self.low, self.high)
        ):
            raise ValueError("endpoints must be base-10 decimal strings")
        if self.low is None or not Decimal(self.low).is_finite():
            raise ValueError("a known value needs a finite lower endpoint")
        if self.high is None:
            if self.state != "partial":
                raise ValueError("only partial values may have no finite upper bound")
        elif not Decimal(self.high).is_finite() or Decimal(self.high) < Decimal(self.low):
            raise ValueError("invalid upper endpoint")
        if self.state == "exact" and self.low != self.high:
            raise ValueError("exact endpoints must match")
        return self

    def display(self, *, money: bool = True) -> dict[str, str | None]:
        def rounded(value: str | None, rounding: str) -> str | None:
            if value is None:
                return None
            with localcontext() as context:
                context.prec = max(60, len(value) + 4)
                result = format(Decimal(value).quantize(Decimal("0.01"), rounding=rounding), ".2f")
                return result if money else result.rstrip("0").rstrip(".")

        exact = self.state == "exact"
        return {
            "low": rounded(self.low, ROUND_HALF_EVEN if exact else ROUND_FLOOR),
            "high": rounded(self.high, ROUND_HALF_EVEN if exact else ROUND_CEILING),
        }


def exact(value: Fraction | Decimal | int, **metadata) -> Value:
    text = endpoint(value)
    return Value(state="exact", low=text, high=text, **metadata)


def divide(
    numerator: Value, denominator: Value, *, unit: str, currency: str | None = None
) -> Value:
    metadata = {
        "unit": unit,
        "currency": currency,
        "cohort_id": numerator.cohort_id,
        "coverage": numerator.coverage,
        "as_of": min(numerator.as_of, denominator.as_of),
    }
    if (
        numerator.cohort_id != denominator.cohort_id
        or (
            numerator.currency is not None
            and denominator.currency is not None
            and numerator.currency != denominator.currency
        )
        or numerator.low is None
        or denominator.low is None
        or Decimal(denominator.low) <= 0
    ):
        return Value(
            **metadata, reason="missing_or_nonpositive_denominator_or_incompatible_cohort"
        )
    partial = numerator.state == "partial" or denominator.state == "partial"
    low = (
        Fraction(0)
        if denominator.high is None
        else Fraction(numerator.low) / Fraction(denominator.high)
    )
    high = None if numerator.high is None else Fraction(numerator.high) / Fraction(denominator.low)
    if numerator.high is not None and denominator.high is not None:
        bounds = [
            Fraction(a) / Fraction(p)
            for a in (numerator.low, numerator.high)
            for p in (denominator.low, denominator.high)
        ]
        low, high = min(bounds), max(bounds)
    state = "partial" if partial else "exact" if high == low else "range"
    return Value(
        state=state, low=endpoint(low), high=None if high is None else endpoint(high), **metadata
    )


def combine(values: list[Value], *, unit: str, currency: str | None = None) -> Value:
    """Add compatible bounds without substituting a known subtotal for Unknown."""
    if not values:
        raise ValueError("combine requires a cohort")
    first = values[0]
    metadata = {
        "unit": unit,
        "currency": currency,
        "cohort_id": first.cohort_id,
        "coverage": first.coverage,
        "as_of": min(item.as_of for item in values),
    }
    if any(
        item.low is None
        or item.cohort_id != first.cohort_id
        or item.currency != first.currency
        or item.unit != first.unit
        for item in values
    ):
        return Value(**metadata, reason="missing_or_incompatible_operand")
    low = sum((Fraction(item.low) for item in values if item.low is not None), Fraction(0))
    high = (
        None
        if any(item.high is None for item in values)
        else sum((Fraction(item.high) for item in values if item.high is not None), Fraction(0))
    )
    return Value(
        state="partial"
        if any(item.state == "partial" for item in values)
        else "exact"
        if low == high
        else "range",
        low=endpoint(low),
        high=None if high is None else endpoint(high),
        **metadata,
    )


def scale(
    value: Value, factor: Fraction, *, unit: str | None = None, currency: str | None = None
) -> Value:
    if factor < 0:
        raise ValueError("positive scaling only")
    if value.low is None:
        return value
    return value.model_copy(
        update={
            "low": endpoint(Fraction(value.low) * factor),
            "high": None if value.high is None else endpoint(Fraction(value.high) * factor),
            "unit": unit or value.unit,
            "currency": value.currency if currency is None else currency,
        }
    )


def subtract(left: Value, right: Value) -> Value:
    metadata = left.model_dump(exclude={"state", "low", "high", "reason"})
    if (
        left.cohort_id != right.cohort_id
        or left.currency != right.currency
        or left.unit != right.unit
        or left.low is None
        or right.low is None
        or right.high is None
    ):
        return Value(**metadata, reason="missing_or_incompatible_operand")
    low = Fraction(left.low) - Fraction(right.high)
    high = None if left.high is None else Fraction(left.high) - Fraction(right.low)
    return Value(
        state="partial"
        if "partial" in {left.state, right.state}
        else "exact"
        if low == high
        else "range",
        low=endpoint(low),
        high=None if high is None else endpoint(high),
        **metadata,
    )


def share(item: Value, total: Value) -> Value:
    value = divide(item, total, unit="share")
    if value.low is None:
        return value
    return value.model_copy(
        update={
            "low": endpoint(max(Fraction(0), Fraction(value.low))),
            "high": None
            if value.high is None
            else endpoint(min(Fraction(1), Fraction(value.high))),
        }
    )


def multiply(left: Value, right: Value, *, unit: str, currency: str | None = None) -> Value:
    metadata = {
        "unit": unit,
        "currency": currency,
        "cohort_id": left.cohort_id,
        "coverage": left.coverage,
        "as_of": min(left.as_of, right.as_of),
    }
    if (
        left.low is None
        or right.low is None
        or left.cohort_id != right.cohort_id
        or min(Fraction(left.low), Fraction(right.low)) < 0
    ):
        return Value(**metadata, reason="missing_or_incompatible_operand")
    low = Fraction(left.low) * Fraction(right.low)
    high = (
        None
        if left.high is None or right.high is None
        else Fraction(left.high) * Fraction(right.high)
    )
    return Value(
        state="partial"
        if "partial" in {left.state, right.state}
        else "exact"
        if low == high
        else "range",
        low=endpoint(low),
        high=None if high is None else endpoint(high),
        **metadata,
    )
