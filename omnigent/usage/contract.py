"""Versioned usage reports and the read-side view contract."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, StrictFloat, StrictInt, model_validator

_Id = Annotated[
    str,
    Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$"),
]
_Number = Annotated[StrictFloat | StrictInt, Field(allow_inf_nan=False)]


class UsageWindow(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: _Id
    kind: Literal["session", "weekly", "weekly_model", "monthly", "daily", "term", "other"] = (
        "other"
    )
    label: str | None = Field(default=None, max_length=32)
    group: str | None = Field(default=None, max_length=32)
    percent_used: Annotated[_Number, Field(ge=0, le=100)] | None = None
    window_minutes: Annotated[StrictInt, Field(gt=0, le=2_628_000)] | None = None
    resets_at: Annotated[_Number, Field(gt=0)] | None = None
    used_value: Annotated[_Number, Field(ge=0)] | None = None
    limit_value: Annotated[_Number, Field(ge=0)] | None = None
    unit: str | None = Field(default=None, max_length=24)
    note: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def has_measurement(self) -> UsageWindow:
        if self.percent_used is None and self.used_value is None:
            raise ValueError("a window needs percent_used or used_value")
        if self.limit_value is not None and self.used_value is None:
            raise ValueError("limit_value needs used_value")
        return self


class ActivityHour(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hour: Annotated[StrictInt, Field(gt=0)]
    model: str | None = Field(default=None, max_length=128)
    requests: Annotated[StrictInt, Field(ge=0)] | None = None
    failed: Annotated[StrictInt, Field(ge=0)] | None = None
    reasons: dict[str, int] | None = None
    tokens: Annotated[StrictInt, Field(ge=0)] | None = None
    cost_usd: Annotated[_Number, Field(ge=0)] | None = None
    cost_basis: Literal["metered", "estimate"] = "metered"

    @model_validator(mode="after")
    def bounded_reasons(self) -> ActivityHour:
        if self.reasons is not None:
            if len(self.reasons) > 16 or any(len(name) > 64 for name in self.reasons):
                raise ValueError("reasons allows at most 16 names of at most 64 characters")
            if any(type(count) is not int or count < 0 for count in self.reasons.values()):
                raise ValueError("reason counts must be non-negative integers")
        return self


#: Channels a pool relation may name. ``usage`` rows are attributable tokens,
#: requests and cost; ``metered_cost``/``list_cost`` rows contribute money only;
#: ``quota`` names a destination that reports windows rather than hourly usage.
PoolChannel = Literal["usage", "metered_cost", "list_cost", "quota"]
POOL_CHANNELS: frozenset[str] = frozenset({"usage", "metered_cost", "list_cost", "quota"})
USAGE_POOL_CHANNEL = "usage"
MONETARY_POOL_CHANNELS: frozenset[str] = frozenset({"metered_cost", "list_cost"})
QUOTA_POOL_CHANNEL = "quota"

#: A pool identity is the exporter's namespace plus digest, never a label.
_PoolId = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"),
]


class PoolRelationClaim(BaseModel):
    """Optional report-v2 metadata naming the pool one destination reports.

    The exporter owns ``pool_id`` (a namespace plus digest). Renaming or
    reordering a pool's labels does not change this value, so a stored relation
    stays valid. The claim names no new hourly key: a pool-specific destination
    keeps reporting its attributable rows under the existing activity
    ``account_id``.
    """

    model_config = ConfigDict(extra="ignore")

    parent_account_id: _Id
    pool_id: _PoolId
    channel: PoolChannel
    effective_from_hour: Annotated[StrictInt, Field(ge=0)]

    @model_validator(mode="after")
    def utc_hour_boundary(self) -> PoolRelationClaim:
        if self.effective_from_hour % 3600:
            raise ValueError("effective_from_hour must be a UTC hour boundary")
        return self


class ReportModel(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    input_per_million: Annotated[_Number, Field(ge=0)] | None = None
    output_per_million: Annotated[_Number, Field(ge=0)] | None = None
    input_cost_per_token: Annotated[_Number, Field(ge=0)] | None = None
    output_cost_per_token: Annotated[_Number, Field(ge=0)] | None = None
    cache_read_input_token_cost: Annotated[_Number, Field(ge=0)] | None = None
    cache_creation_input_token_cost: Annotated[_Number, Field(ge=0)] | None = None
    output_cost_per_image: Annotated[_Number, Field(ge=0)] | None = None
    input_cost_per_request: Annotated[_Number, Field(ge=0)] | None = None


class UsageBalance(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: _Id
    label: str | None = Field(default=None, max_length=32)
    group: str | None = Field(default=None, max_length=32)
    remaining: _Number
    unit: str = Field(min_length=1, max_length=24)
    usd_per_unit: Annotated[_Number, Field(gt=0)] | None = None
    note: str | None = Field(default=None, max_length=64)
    expires_at: Annotated[_Number, Field(gt=0)] | None = None


class ActivityEntryV2(BaseModel):
    model_config = ConfigDict(extra="ignore")

    hour: Annotated[StrictInt, Field(gt=0)]
    model: str = Field(default="", max_length=128)
    host: str = Field(default="", max_length=64)
    uncached_input_tokens: Annotated[StrictInt, Field(ge=0)] | None = None
    cache_read_input_tokens: Annotated[StrictInt, Field(ge=0)] | None = None
    cache_creation_input_tokens: Annotated[StrictInt, Field(ge=0)] | None = None
    output_tokens: Annotated[StrictInt, Field(ge=0)] | None = None
    reasoning_tokens: Annotated[StrictInt, Field(ge=0)] | None = None
    tokens: Annotated[StrictInt, Field(ge=0)] | None = None
    requests: Annotated[StrictInt, Field(ge=0)] | None = None
    failed: Annotated[StrictInt, Field(ge=0)] | None = None
    reasons: dict[str, int] | None = None
    cost_usd: Annotated[_Number, Field(ge=0)] | None = None
    cost_basis: Literal["billed", "metered", "list", "estimate"] = "metered"
    coverage: Literal["complete", "partial"] = "complete"
    removed: bool = Field(default=False, strict=True)
    correction: bool = Field(default=False, strict=True)

    @model_validator(mode="before")
    @classmethod
    def removal_names_only(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("removed") is True:
            return {
                key: item
                for key, item in value.items()
                if key in {"hour", "model", "host", "cost_basis", "removed"}
            }
        return value

    @model_validator(mode="after")
    def validate_entry(self) -> ActivityEntryV2:
        ActivityHour(hour=self.hour, reasons=self.reasons)
        if self.cost_basis == "estimate":
            self.cost_basis = "list"
        figures = (
            self.uncached_input_tokens,
            self.cache_read_input_tokens,
            self.cache_creation_input_tokens,
            self.output_tokens,
            self.reasoning_tokens,
            self.tokens,
            self.requests,
            self.failed,
            self.cost_usd,
        )
        if (
            not self.removed
            and self.coverage != "partial"
            and not any(value is not None for value in figures)
            and not self.reasons
        ):
            raise ValueError("an activity entry without figures requires coverage: partial")
        return self

    def total(self) -> int | None:
        buckets = (
            self.uncached_input_tokens,
            self.cache_read_input_tokens,
            self.cache_creation_input_tokens,
            self.output_tokens,
        )
        if all(value is not None for value in buckets):
            return sum(value for value in buckets if value is not None)
        return self.tokens


class EntryOutcome(BaseModel):
    hour: int
    host: str
    model: str
    cost_basis: str
    reason: Literal["lower_reread", "partial_over_complete", "out_of_range"]


class WriteOutcome(BaseModel):
    applied: int = Field(default=0, ge=0)
    held: list[EntryOutcome] = Field(default_factory=list)
    skipped: list[EntryOutcome] = Field(default_factory=list)
    older_sections: list[Literal["windows", "balances"]] = Field(default_factory=list)


class _ReportFields(BaseModel):
    model_config = ConfigDict(extra="ignore")

    observed_at: Annotated[_Number, Field(gt=0)]
    status: Literal["available", "unavailable"] = "available"
    fresh_until: Annotated[_Number, Field(gt=0)] | None = None
    polling_seconds: Annotated[StrictInt, Field(gt=0, le=86400)] | None = None
    pinned_window: _Id | None = None
    display_name: str | None = Field(default=None, max_length=64)
    plan: str | None = Field(default=None, max_length=64)
    band: str | None = Field(default=None, max_length=32)
    details_url: HttpUrl | None = None
    renews_at: Annotated[_Number, Field(gt=0)] | None = None
    product: str | None = None
    host_id: str | None = Field(default=None, max_length=128)
    models: Annotated[list[ReportModel] | None, Field(max_length=128)] = None

    def stored(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class UsageReport(_ReportFields):
    schema_version: Literal[1] = 1
    windows: Annotated[list[UsageWindow], Field(max_length=16)]
    activity: Annotated[list[ActivityHour] | None, Field(max_length=500)] = None

    @model_validator(mode="after")
    def window_ids_and_pin(self) -> UsageReport:
        ids = [window.id for window in self.windows]
        if len(set(ids)) != len(ids):
            raise ValueError("window ids must be unique")
        if self.pinned_window is not None and self.pinned_window not in ids:
            raise ValueError("pinned_window must name a reported window")
        return self


class UsageReportV2(_ReportFields):
    schema_version: Literal[2]
    windows: Annotated[list[UsageWindow], Field(max_length=16)] = Field(default_factory=list)
    balances: Annotated[list[UsageBalance], Field(max_length=16)] = Field(default_factory=list)
    activity: Annotated[list[ActivityEntryV2] | None, Field(max_length=500)] = None
    source_type: Literal["subscription", "metered", "relay", "manual", "other"] | None = None
    source: str | None = Field(default=None, max_length=64)
    # Optional: an older reporter omits it, which never erases a stored relation.
    pool_relation: PoolRelationClaim | None = None

    @model_validator(mode="after")
    def validate_sections(self) -> UsageReportV2:
        for section in (self.windows, self.balances):
            ids = [item.id for item in section]
            if len(ids) != len(set(ids)):
                raise ValueError("section ids must be unique")
        if "windows" in self.model_fields_set and self.pinned_window is not None:
            if self.pinned_window not in {item.id for item in self.windows}:
                raise ValueError("pinned_window must name a reported window")
        return self


def parse_usage_report(payload: Any) -> UsageReport | UsageReportV2:
    if isinstance(payload, dict) and payload.get("schema_version") == 2:
        return UsageReportV2.model_validate(payload)
    return UsageReport.model_validate(payload)


class UsageWindowView(BaseModel):
    id: str
    kind: str
    label: str | None = None
    group: str | None = None
    percent_used: float | None = None
    remaining_pct: float | None = None
    window_minutes: int | None = None
    resets_at: float | None = None
    time_left_s: float | None = None
    pace_pct: float | None = None
    state: str
    ahead_pts: float | None = None
    used_value: float | None = None
    limit_value: float | None = None
    unit: str | None = None
    note: str | None = None
    past_reset: bool = False


class UsageHostView(BaseModel):
    host_id: str
    name: str
    read_at: float | None = None
    windows: list[UsageWindowView] = Field(default_factory=list)


class UsagePeriodView(BaseModel):
    start: float
    days: float
    moved_from: float | None = None


class UsageAccountView(BaseModel):
    id: str
    kind: Literal["native", "account"]
    display_name: str
    plan: str | None = None
    band: str | None = None
    product: str | None = None
    details_url: str | None = None
    renews_at: float | None = None
    state: str
    reason: str | None = None
    deciding_window: str | None = None
    pace_pct: float | None = None
    read_at: float | None = None
    age_s: float | None = None
    freshness: Literal["fresh", "old", "none"]
    mark: dict[str, Any] | None = None
    period: UsagePeriodView | None = None
    windows: list[UsageWindowView] = Field(default_factory=list)
    hosts: list[UsageHostView] = Field(default_factory=list)
    models: list[ReportModel] = Field(default_factory=list)
    balances: list[UsageBalance] = Field(default_factory=list)
    balance_read_at: float | None = None
    balance_freshness: Literal["fresh", "old", "none"] = "none"
    source_type: str | None = None
    source: str | None = None
    source_at: float | None = None


class UsageOverview(BaseModel):
    schema_version: Literal[1] = 1
    generated_at: float
    tiers: dict[str, float | None]
    accounts: list[UsageAccountView]
    agents: dict[str, dict[str, Any]]


class AnalysisSpan(BaseModel):
    """The bounded window one analysis read covers."""

    requested_from: float
    counted_from: int
    until: float
    approx: bool


class AnalysisTotals(BaseModel):
    """The nullable figures one grouped analysis value reports.

    A figure that a source did not report stays ``None``; a reported zero is a
    known zero and survives as ``0``.
    """

    cost_usd: float | None = None
    counted_cost_usd: float = 0.0
    tokens: int | None = None
    unpriced: int = 0
    partial_rows: int = 0
    per_million: float | None = None
    covered_cost_usd: float = 0.0
    covered_tokens: int = 0
    cost_coverage_pct: float | None = None
    tokens_complete: bool = False
    requests: int | None = None
    failed: int | None = None
    failure_pct: float | None = None
    reasons: dict[str, int] = Field(default_factory=dict)
    last_at: int | None = None
    metered: AnalysisTotals | None = None


class PoolCatalogEntry(BaseModel):
    """One observed pool, listed even while its usage stays unavailable.

    ``usage_available`` states whether a proven ``usage`` channel exists for the
    pool; a quota-only or monetary-only pool keeps its catalog row with
    ``usage_available`` false while its value/tokens read as unknown.
    """

    parent_account_id: str
    pool_id: str
    display_name: str
    channels: list[str] = Field(default_factory=list)
    usage_available: bool = False
    effective_from_hour: int | None = None


class AnalysisRowView(BaseModel):
    """One model-hour row of an analysis window, with its pool attribution."""

    account_id: str
    #: The parent tile this row rolls into (itself when it has no relation).
    parent_account_id: str
    pool_id: str | None = None
    #: ``None`` when the account has no stored relation, so no channel is proven.
    channel: str | None = None
    hour: int
    model: str = ""
    host_id: str = ""
    basis: str
    priced: bool = False
    coverage: str | None = None
    tokens: int | None = None
    uncached_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_tokens: int | None = None
    requests: int | None = None
    failed: int | None = None
    cost_usd: float | None = None
    attributed: bool = False
    contributes_usage: bool = False
    contributes_money: bool = False
    reason: str | None = None


class AnalysisAccountTotal(BaseModel):
    account_id: str
    totals: AnalysisTotals


class AnalysisParentTotal(BaseModel):
    parent_account_id: str
    totals: AnalysisTotals


class PoolTotalView(BaseModel):
    """A pool's own actual rows, kept per destination and channel."""

    parent_account_id: str
    pool_id: str
    account_id: str
    channel: str
    totals: AnalysisTotals


class AnalysisUnmapped(BaseModel):
    """Rows held at the parent with a reason instead of being divided."""

    count: int = 0
    rows: int = 0
    reasons: dict[str, int] = Field(default_factory=dict)


class UsageAnalysisView(BaseModel):
    schema_version: Literal[1] = 1
    generated_at: float
    range: str
    tz: str
    span: AnalysisSpan
    account_spans: dict[str, AnalysisSpan] = Field(default_factory=dict)
    pool_catalog: list[PoolCatalogEntry] = Field(default_factory=list)
    rows: list[AnalysisRowView] = Field(default_factory=list)
    accounts: list[AnalysisAccountTotal] = Field(default_factory=list)
    parents: list[AnalysisParentTotal] = Field(default_factory=list)
    pools: list[PoolTotalView] = Field(default_factory=list)
    unmapped: AnalysisUnmapped = Field(default_factory=AnalysisUnmapped)
    truncated: bool = False
    partial: bool = False
    row_limit: int = 0
    pricing: dict[str, Any] | None = None
    quota_closings: list[dict[str, Any]] = Field(default_factory=list)


AnalysisTotals.model_rebuild()


@dataclass(frozen=True)
class UsagePeriod:
    start: float
    days: float
    window_id: str | None
    rolling: bool = False


def window_start(window: UsageWindow, now: float) -> tuple[float, float] | None:
    if window.resets_at is None or window.window_minutes is None:
        return None
    length = window.window_minutes * 60.0
    reset = float(window.resets_at)
    return (reset if reset <= now else reset - length, length)


def period_of(windows: list[UsageWindow], now: float) -> UsagePeriod:
    usable = [window for window in windows if window_start(window, now) is not None]
    weekly = [window for window in usable if window.kind == "weekly"]
    if not weekly:
        weekly = [window for window in usable if window.kind == "weekly_model"]
    if not weekly:
        weekly = [window for window in usable if window.window_minutes == 10_080]
    if weekly:
        window = min(weekly, key=lambda item: float(item.resets_at or 0))
        start, length = window_start(window, now) or (now - 7 * 86400, 7 * 86400.0)
        return UsagePeriod(start, length / 86400, window.id)
    monthly = [
        window
        for window in usable
        if window.kind == "monthly"
        or (window.window_minutes is not None and 40_320 <= window.window_minutes <= 44_640)
    ]
    if monthly:
        window = min(monthly, key=lambda item: float(item.resets_at or 0))
        start, length = window_start(window, now) or (now - 30 * 86400, 30 * 86400.0)
        return UsagePeriod(start, length / 86400, window.id)
    return UsagePeriod(now - 30 * 86400, 30.0, None, rolling=True)


def next_mark(previous: Mapping[str, Any] | None, start: float, length: float) -> dict[str, Any]:
    if previous is None or previous.get("start") is None:
        return {"start": start, "moved_from": None}
    old_start = float(previous["start"])
    if abs(start - old_start) <= 3600:
        return dict(previous)
    if start >= old_start + length - 3600:
        return {"start": start, "moved_from": None}
    return {"start": start, "moved_from": old_start}
