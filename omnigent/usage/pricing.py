"""User purchase baselines; decimal strings remain independent of display rounding."""

from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Literal
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    field_validator,
    model_validator,
)

Currency = Literal["CNY", "USD", "HKD"]


def decimal_input(value: str) -> str:
    if not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?", value):
        raise ValueError("use a non-negative base-10 decimal string")
    whole, _, fractional = value.partition(".")
    if len(whole) > 20 or len(fractional) > 18 or len(whole + fractional) > 38:
        raise ValueError("decimal exceeds Numeric(38,18)")
    return value


DecimalInput = Annotated[str, AfterValidator(decimal_input)]
Identifier = Annotated[
    str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9:_.-]*$")
]


class PricingModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PricingDefaults(PricingModel):
    schema_version: Literal[1] = 1
    default_discount: DecimalInput = "0.8"
    display_currency: Currency = "USD"
    fx_to_display: dict[Currency, DecimalInput] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_rates(self) -> PricingDefaults:
        if Decimal(self.default_discount) > 1:
            raise ValueError("default_discount must be between 0 and 1")
        for source, rate in self.fx_to_display.items():
            if Decimal(rate) <= 0 or (source == self.display_currency and Decimal(rate) != 1):
                raise ValueError("FX must be positive; identity rate must be 1")
        self.fx_to_display[self.display_currency] = "1"
        return self


class TokenRates(PricingModel):
    read: DecimalInput
    write: DecimalInput
    cache_read: DecimalInput
    cache_write: DecimalInput
    model: str | None = Field(default=None, max_length=128)


class PurchasedCard(PricingModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=128)
    retail_amount: DecimalInput
    purchase_currency: Currency = "CNY"
    allowance_usd: DecimalInput
    period_kind: Literal["week", "month", "month_weekly_reset"]
    starts_on: date
    timezone: str = "UTC"
    target_kind: Literal["account", "pool"]
    target_id: Identifier
    parent_account_id: Identifier | None = None
    rates_usd_per_million: TokenRates | None = None
    discount_override: DecimalInput | None = None

    @field_validator("discount_override", mode="before")
    @classmethod
    def blank_inherits(cls, value):
        return None if value == "" else value

    @model_validator(mode="after")
    def valid_card(self) -> PurchasedCard:
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("name must not be blank")
        if Decimal(self.allowance_usd) <= 0:
            raise ValueError("allowance_usd must be positive")
        if self.discount_override is not None and Decimal(self.discount_override) > 1:
            raise ValueError("discount_override must be between 0 and 1")
        try:
            ZoneInfo(self.timezone)
            self.billing_span()
        except (ValueError, KeyError, OverflowError):
            raise ValueError("invalid date or IANA timezone") from None
        if (self.target_kind == "pool") != (self.parent_account_id is not None):
            raise ValueError("pool targets require parent_account_id; account targets omit it")
        return self

    @property
    def parent(self) -> str:
        return self.parent_account_id or self.target_id

    def billing_span(self) -> tuple[datetime, datetime]:
        start = datetime.combine(self.starts_on, time(), ZoneInfo(self.timezone))
        if self.period_kind == "week":
            return start, start + timedelta(days=7)
        year = start.year + (start.month == 12)
        month = start.month % 12 + 1
        end = start.replace(
            year=year, month=month, day=min(start.day, calendar.monthrange(year, month)[1])
        )
        return start, end

    def reset_spans(self) -> list[tuple[int, int]]:
        start, end = self.billing_span()
        if self.period_kind != "month_weekly_reset":
            return [(int(start.timestamp()), int(end.timestamp()))]
        result = []
        while start < end:
            until = min(start + timedelta(days=7), end)
            result.append((int(start.timestamp()), int(until.timestamp())))
            start = until
        return result


class PricingBaseline(PricingModel):
    revision: Annotated[StrictInt, Field(ge=0)] = 0
    defaults: PricingDefaults = Field(default_factory=PricingDefaults)
    cards: list[PurchasedCard] = Field(default_factory=list, max_length=2048)

    @model_validator(mode="after")
    def no_overlapping_cards(self) -> PricingBaseline:
        if len({card.id for card in self.cards}) != len(self.cards):
            raise ValueError("card ids must be unique")
        by_parent: dict[str, list[PurchasedCard]] = {}
        for card in self.cards:
            for other in by_parent.setdefault(card.parent, []):
                if (
                    card.target_kind == other.target_kind == "pool"
                    and card.target_id != other.target_id
                ):
                    continue
                a, b = card.billing_span()
                c, d = other.billing_span()
                if max(a.timestamp(), c.timestamp()) < min(b.timestamp(), d.timestamp()):
                    raise ValueError("overlapping account/pool card coverage")
            by_parent[card.parent].append(card)
        return self
