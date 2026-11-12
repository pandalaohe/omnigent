"""Price evidence validation and exact decimal aggregation."""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, localcontext
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator

from omnigent.usage.evidence import Account, Timestamp

Money = Annotated[str, Field(max_length=40, pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")]
Count = Annotated[StrictInt, Field(ge=0, le=9223372036854775807)]
TokenKind = Literal[
    "uncached_input", "output", "cache_read", "cache_write", "cache_write_5m", "cache_write_1h"
]


class PriceRate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token_kind: TokenKind
    per_token: Money


class PriceEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    low_usd: Money | None = None
    high_usd: Money | None = None
    state: Literal["exact", "range", "partial", "unknown"]
    missing: Annotated[list[Annotated[str, Field(max_length=64)]], Field(max_length=16)] = []
    priced_units: Count | None = None
    usage_units: Count | None = None
    cache_savings_low_usd: Money | None = None
    cache_savings_high_usd: Money | None = None
    rates: Annotated[list[PriceRate], Field(max_length=32)] = []
    priced_as: Annotated[list[Annotated[str, Field(max_length=128)]], Field(max_length=8)] = []
    price_source: Annotated[str, Field(max_length=256)] | None = None
    price_version: Annotated[str, Field(max_length=128)] | None = None
    fetched_at: Timestamp | None = None
    as_of: Timestamp | None = None

    @model_validator(mode="after")
    def ordered(self) -> PriceEstimate:
        for low, high in (
            (self.low_usd, self.high_usd),
            (self.cache_savings_low_usd, self.cache_savings_high_usd),
            (self.priced_units, self.usage_units),
        ):
            if low is not None and high is not None and Decimal(low) > Decimal(high):
                raise ValueError("reversed bounds")
        return self


class PricePartition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_class: Annotated[StrictInt, Field(ge=0, le=5)]
    upstream_host: Annotated[str, Field(max_length=256)]
    calls: Count
    prompt_tokens: Count
    prompt_present: bool
    completion_tokens: Count
    completion_present: bool
    cache_read_tokens: Count
    cache_read_present: bool
    cache_write_tokens: Count
    cache_write_present: bool


class PriceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: Account
    hour: Timestamp
    model: Annotated[str, Field(max_length=128)]
    estimate: PriceEstimate | None = None
    partitions: Annotated[list[PricePartition], Field(max_length=64)] | None = None

    @model_validator(mode="after")
    def closed_identity(self) -> PriceEvidence:
        if self.hour % 3600 or not self.model_fields_set.intersection({"estimate", "partitions"}):
            raise ValueError("hour or evidence part missing")
        if any(
            getattr(self, name) is None
            for name in self.model_fields_set.intersection({"estimate", "partitions"})
        ):
            raise ValueError("omit absent parts")
        return self


class PriceAliases(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account: Account
    mappings: dict[
        Annotated[str, Field(max_length=128)], Annotated[str, Field(max_length=128)] | None
    ]
    observed_at: Timestamp | None = None

    @field_validator("mappings")
    @classmethod
    def bounded(cls, value: dict) -> dict:
        if len(value) > 128:
            raise ValueError("too many aliases")
        return value


def _sum(rows: list[dict], key: str) -> str | None:
    values = [Decimal(row[key]) for row in rows if row.get(key) is not None]
    if not values:
        return None
    with localcontext() as context:
        context.prec = 80
        return format(sum(values, Decimal(0)), "f")


def aggregate_prices(rows: list[dict[str, Any]], detail: bool = False) -> list[dict[str, Any]]:
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["account_id"], row["model"]].append(row)
    result = []
    for (account, model), hours in sorted(grouped.items()):
        estimates = [row for row in hours if row["state"] is not None]
        states = Counter(row["state"] for row in estimates)
        rates = defaultdict(list)
        partitions: dict[tuple, dict] = {}
        for row in hours:
            for rate in row["rates"] or []:
                rates[rate["token_kind"]].append(Decimal(rate["per_token"]))
            for part in row["partitions"] or []:
                key = part["context_class"], part["upstream_host"]
                if key not in partitions:
                    partitions[key] = dict(part)
                else:
                    for name, value in part.items():
                        if name.endswith("_present"):
                            partitions[key][name] = partitions[key][name] and value
                        elif name not in {"context_class", "upstream_host"}:
                            partitions[key][name] += value
        sources = {
            (r["price_source"], r["price_version"], r["fetched_at"])
            for r in estimates
            if any(r[k] is not None for k in ("price_source", "price_version", "fetched_at"))
        }
        estimate = {
            "low_usd": _sum(estimates, "low_usd"),
            "high_usd": _sum(estimates, "high_usd"),
            "rows_with_low": sum(r["low_usd"] is not None for r in estimates),
            "rows_with_high": sum(r["high_usd"] is not None for r in estimates),
            "states": {state: states[state] for state in ("exact", "range", "partial", "unknown")},
            "missing": sorted({v for r in estimates for v in (r["missing"] or [])}),
            "as_of": max((r["as_of"] for r in estimates if r["as_of"] is not None), default=None),
            "price_sources": [
                {"source": source, "version": version, "fetched_at": fetched}
                for source, version, fetched in sorted(
                    sources, key=lambda v: tuple(str(x) for x in v)
                )
            ],
            "cache_savings": {
                "low_usd": _sum(estimates, "cache_savings_low_usd"),
                "high_usd": _sum(estimates, "cache_savings_high_usd"),
                "rows_with_bounds": sum(
                    r["cache_savings_low_usd"] is not None
                    and r["cache_savings_high_usd"] is not None
                    for r in estimates
                ),
            },
        }
        for key in ("priced_units", "usage_units"):
            estimate[key] = (
                sum(r[key] for r in hours) if all(r[key] is not None for r in hours) else None
            )
        entry = {
            "account": account,
            "model": model,
            "hours": len(hours),
            "estimate": estimate,
            "rates": [
                {
                    "token_kind": kind,
                    "low_per_token": format(min(values), "f"),
                    "high_per_token": format(max(values), "f"),
                }
                for kind, values in sorted(rates.items())
            ],
            "priced_as": sorted({v for r in estimates for v in (r["priced_as"] or [])}),
            "partitions": [partitions[k] for k in sorted(partitions)],
        }
        if detail:
            entry["hourly"] = [
                {
                    "hour": r["hour_utc"],
                    "estimate": {k: r[k] for k in PriceEstimate.model_fields}
                    if r["state"] is not None
                    else None,
                    "partitions": r["partitions"],
                }
                for r in sorted(hours, key=lambda r: r["hour_utc"])
            ]
        result.append(entry)
    return result
