"""User purchase baselines; decimal strings remain independent of display rounding."""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
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
CostClass = Literal["subscription", "prepaid", "metered", "free"]


def default_cost_class(
    source_type: str | None, *, native: bool = False, bases: set[str] | None = None
) -> CostClass | None:
    if native:
        return (
            "subscription"
            if bases == {"subscription"}
            else "metered"
            if bases == {"metered"}
            else None
        )
    defaults: dict[str, CostClass] = {
        "subscription": "subscription",
        "relay": "prepaid",
        "metered": "metered",
    }
    return defaults.get(source_type or "")


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


class NullableRates(PricingModel):
    read: DecimalInput | None = None
    write: DecimalInput | None = None
    cache_read: DecimalInput | None = None
    cache_write: DecimalInput | None = None

    @field_validator("read", "write", "cache_read", "cache_write", mode="before")
    @classmethod
    def blank_inherits(cls, value):
        return None if value == "" else value


class TokenRates(NullableRates):
    model: str | None = Field(default=None, max_length=128)


class AccountPriceOverride(NullableRates):
    account_id: Annotated[Identifier, Field(max_length=64)]
    model: str = Field(min_length=1, max_length=128)


class PurchasedCard(PricingModel):
    id: Identifier
    name: str = Field(min_length=1, max_length=128)
    retail_amount: DecimalInput
    purchase_currency: Currency = "CNY"
    allowance_usd: DecimalInput | None = None
    repeats: bool = False
    ends_on: date | None = None
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
        if self.allowance_usd is None and not self.repeats:
            raise ValueError("one-off cards require allowance_usd")
        if self.allowance_usd is not None and Decimal(self.allowance_usd) <= 0:
            raise ValueError("allowance_usd must be positive")
        if self.ends_on is not None and (not self.repeats or self.ends_on <= self.starts_on):
            raise ValueError("ends_on must follow the start of a repeating card")
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

    def period_start(self, index: int) -> datetime:
        start = datetime.combine(self.starts_on, time(), ZoneInfo(self.timezone))
        if self.period_kind == "week":
            return start + timedelta(days=7 * index)
        year, month = divmod(start.year * 12 + start.month - 1 + index, 12)
        return start.replace(
            year=year, month=month + 1, day=min(start.day, calendar.monthrange(year, month + 1)[1])
        )

    def billing_span(self, index: int = 0) -> tuple[datetime, datetime]:
        return self.period_start(index), self.period_start(index + 1)

    def reset_spans(self, index: int = 0) -> list[tuple[int, int]]:
        start, end = self.billing_span(index)
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
    account_classes: dict[Identifier, CostClass | None] = Field(
        default_factory=dict, max_length=2048
    )

    def renewal_boundary(self, card: PurchasedCard) -> float:
        boundary = (
            datetime.combine(card.ends_on, time(), ZoneInfo(card.timezone)).timestamp()
            if card.ends_on
            else float("inf")
        )
        return min(boundary, self.replacement_boundary(card))

    def replacement_boundary(self, card: PurchasedCard) -> float:
        boundary = float("inf")
        for other in self.cards:
            if other.id != card.id and (other.parent, other.target_kind, other.target_id) == (
                card.parent,
                card.target_kind,
                card.target_id,
            ):
                start = other.period_start(0).timestamp()
                if start > card.period_start(0).timestamp():
                    boundary = min(boundary, start)
        return boundary

    def card_periods(
        self, card: PurchasedCard, start: float, until: float, as_of: float
    ) -> Iterator[tuple[int, tuple[int, int]]]:
        """Yield only started periods; keep the original day and complete paid span."""
        first = card.period_start(0)
        selected = datetime.fromtimestamp(max(start, first.timestamp()), UTC).astimezone(
            first.tzinfo
        )
        index = max(
            0,
            (
                (selected.date() - first.date()).days // 7
                if card.period_kind == "week"
                else (selected.year - first.year) * 12 + selected.month - first.month
            )
            - 1,
        )
        if not card.repeats:
            index = 0
        boundary = self.renewal_boundary(card)
        while True:
            begins, ends = card.billing_span(index)
            b, e = begins.timestamp(), ends.timestamp()
            if b >= until or (card.repeats and (b > as_of or b >= boundary)):
                return
            if e > start:
                yield index, (int(b), int(e))
            if not card.repeats:
                return
            index += 1

    def next_renewal(self, card: PurchasedCard, as_of: float) -> int | None:
        if not card.repeats:
            return None
        for _, (begins, ends) in self.card_periods(
            card, as_of, as_of + 370 * 86400, as_of + 370 * 86400
        ):
            candidate = begins if begins > as_of else ends
            if candidate > as_of and candidate < self.renewal_boundary(card):
                return candidate
        return None

    def past_period_count(self, card: PurchasedCard, as_of: float) -> int:
        """Count history without expanding years of subscription payments in a response."""
        first = card.period_start(0)
        if first.timestamp() > as_of:
            return 0
        if not card.repeats:
            return 1
        boundary = self.renewal_boundary(card)
        cutoff = datetime.fromtimestamp(min(as_of, boundary), UTC).astimezone(first.tzinfo)
        index = max(
            0,
            (cutoff.date() - first.date()).days // 7
            if card.period_kind == "week"
            else (cutoff.year - first.year) * 12 + cutoff.month - first.month,
        )
        while index >= 0:
            start = card.period_start(index).timestamp()
            if start <= as_of and start < boundary:
                return index + 1
            index -= 1
        return 0

    price_overrides: list[AccountPriceOverride] = Field(default_factory=list, max_length=4096)

    @model_validator(mode="after")
    def no_overlapping_cards(self) -> PricingBaseline:
        keys = [(row.account_id, row.model.lower()) for row in self.price_overrides]
        if len(set(keys)) != len(keys):
            raise ValueError("account/model price rows must be unique")
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
                if (card.parent, card.target_kind, card.target_id) == (
                    other.parent,
                    other.target_kind,
                    other.target_id,
                ):
                    if a.timestamp() < c.timestamp() and card.repeats:
                        continue
                    if c.timestamp() < a.timestamp() and other.repeats:
                        continue
                if card.repeats:
                    b = (
                        datetime.fromtimestamp(self.renewal_boundary(card), UTC)
                        if self.renewal_boundary(card) != float("inf")
                        else datetime.max.replace(tzinfo=UTC)
                    )
                if other.repeats:
                    d = (
                        datetime.fromtimestamp(self.renewal_boundary(other), UTC)
                        if self.renewal_boundary(other) != float("inf")
                        else datetime.max.replace(tzinfo=UTC)
                    )
                if max(a.timestamp(), c.timestamp()) < min(b.timestamp(), d.timestamp()):
                    raise ValueError("overlapping account/pool card coverage")
            by_parent[card.parent].append(card)
        return self
