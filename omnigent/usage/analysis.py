"""Project bounded hourly usage onto explicit pool relations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from omnigent.stores.usage_hours_store import HourRow
from omnigent.stores.usage_pool_store import UsageAccountRelation
from omnigent.usage.activity import totals
from omnigent.usage.contract import (
    MONETARY_POOL_CHANNELS,
    QUOTA_POOL_CHANNEL,
    USAGE_POOL_CHANNEL,
    AnalysisRowView,
    AnalysisTotals,
    AnalysisUnmapped,
    PoolCatalogEntry,
)

#: Hard bound on the model-hour rows one analysis response carries.
MAX_ANALYSIS_ROWS = 5_000

#: Hard bound on the calendar span one analysis request may cover (days).
MAX_ANALYSIS_SPAN_DAYS = 370

#: Bases whose money is a list-price figure rather than a bill. A ``list_cost``
#: channel counts only these; a ``metered_cost`` channel counts the rest.
LIST_BASES = frozenset({"list", "subscription"})

#: Reasons a selected pool-destination row is not proven pool usage. Values
#: are API-stable, so a client can show why a row stayed at its parent.
BEFORE_BOUNDARY = "before_boundary"
VALUATION_MISMATCH = "valuation_mismatch"
QUOTA_CHANNEL = "quota_channel"
UNPROVEN_OVERLAP = "unproven_overlap"
MISSING_PARENT = "missing_parent"

UNMAPPED_REASONS = (
    BEFORE_BOUNDARY,
    VALUATION_MISMATCH,
    QUOTA_CHANNEL,
    UNPROVEN_OVERLAP,
    MISSING_PARENT,
)


@dataclass(frozen=True)
class AnalysedRow:
    """One hourly row with the pool decision made for it.

    ``row`` may be a money-only copy of the source row: a monetary channel is
    never allowed to contribute tokens or requests to any total.

    :param contributes_money: The parent account's money total counts this row.
    :param contributes_usage: The parent's token/request figures count it.
    :param pool_money: The named pool's money total counts it.
    :param pool_usage: The named pool's token/request figures count it.
    """

    row: HourRow
    account_id: str
    parent_account_id: str
    pool_id: str | None
    channel: str | None
    reason: str | None
    contributes_money: bool = False
    contributes_usage: bool = False
    pool_money: bool = False
    pool_usage: bool = False

    @property
    def attributed(self) -> bool:
        """Whether a proven usage channel joined this row to its pool."""
        return self.pool_usage

    def view(self) -> AnalysisRowView:
        row = self.row
        # Aggregate price selection does not remove a proven pool identity.
        return AnalysisRowView(
            account_id=self.account_id,
            parent_account_id=self.parent_account_id,
            pool_id=(
                self.pool_id if self.pool_money or self.pool_usage or self.reason is None else None
            ),
            channel=self.channel,
            hour=row.hour_utc,
            model=row.model,
            host_id=row.host_id,
            basis=row.basis,
            priced=row.priced,
            coverage=row.coverage,
            tokens=row.total_tokens,
            uncached_input_tokens=row.input_tokens,
            cache_read_input_tokens=row.cached_tokens,
            cache_creation_input_tokens=row.cache_write_tokens,
            output_tokens=row.output_tokens,
            reasoning_tokens=row.reasoning_tokens,
            requests=row.requests,
            failed=row.failed,
            cost_usd=row.cost_usd if row.priced else None,
            attributed=self.attributed,
            contributes_usage=self.contributes_usage,
            contributes_money=self.contributes_money,
            reason=self.reason,
        )


@dataclass
class PoolProjection:
    """The bounded rows of one analysis read with their attribution decisions."""

    rows: list[AnalysedRow]
    partial: bool = False

    def parent_money(self, parent: str) -> list[HourRow]:
        return [
            item.row
            for item in self.rows
            if item.parent_account_id == parent and item.contributes_money
        ]

    def parent_usage(self, parent: str) -> list[HourRow]:
        return [
            item.row
            for item in self.rows
            if item.parent_account_id == parent and item.contributes_usage
        ]

    def pool_rows(self, parent: str, pool: str, account: str, channel: str) -> list[HourRow]:
        return [
            item.row
            for item in self.rows
            if item.account_id == account
            and item.parent_account_id == parent
            and item.pool_id == pool
            and item.channel == channel
            and item.pool_money
        ]

    def pool_usage(self, parent: str, pool: str, account: str, channel: str) -> list[HourRow]:
        return [
            item.row
            for item in self.rows
            if item.account_id == account
            and item.parent_account_id == parent
            and item.pool_id == pool
            and item.channel == channel
            and item.pool_usage
        ]

    def unmapped(self) -> AnalysisUnmapped:
        reasons: dict[str, int] = {}
        for item in self.rows:
            if item.reason is None:
                continue
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
        return AnalysisUnmapped(
            count=sum(reasons.values()),
            rows=len(self.rows),
            reasons=dict(sorted(reasons.items())),
        )


def _money_only(row: HourRow) -> HourRow:
    """Strip token and request figures a money channel may not contribute."""
    return replace(
        row,
        input_tokens=None,
        cached_tokens=None,
        cache_write_tokens=None,
        output_tokens=None,
        total_tokens=None,
        reasoning_tokens=None,
        requests=None,
        failed=None,
        reasons=None,
    )


def row_worth_money(channel: str, row: HourRow) -> bool:
    """Whether one row's money belongs to a monetary channel's valuation.

    ``list_cost`` counts list-price bases only and ``metered_cost`` the rest, so
    one row never lands in both valuations.
    """
    if channel == "list_cost":
        return row.basis in LIST_BASES
    return row.basis not in LIST_BASES


def attribute_rows(
    rows: Sequence[HourRow],
    relations: Mapping[str, UsageAccountRelation],
    known_accounts: Mapping[str, Any],
) -> PoolProjection:
    """Decide, per row, which pool (if any) proves it.

    :param rows: Hourly rows already deduplicated by
        :func:`omnigent.usage.activity.attributed_rows` and restricted to the
        requested span.
    :param relations: Stored relations keyed by destination account id.
    :param known_accounts: Owner accounts the read can render a tile for; a
        relation whose parent is not among them cannot roll up.
    :returns: The per-row decisions plus the partial-overlap flag.
    """
    decided: list[AnalysedRow] = []
    own_signatures: dict[str, set[tuple[int, str, str]]] = {}

    for row in rows:
        signature = (row.hour_utc, row.model, row.host_id)
        relation = relations.get(row.account_id)
        if relation is not None and relation.parent_account_id not in known_accounts:
            usage = relation.channel == USAGE_POOL_CHANNEL
            decided.append(
                AnalysedRow(
                    row=row if usage else _money_only(row),
                    account_id=row.account_id,
                    parent_account_id=row.account_id,
                    pool_id=None,
                    channel=relation.channel,
                    reason=MISSING_PARENT,
                    contributes_money=True,
                    contributes_usage=usage,
                )
            )
            own_signatures.setdefault(row.account_id, set()).add(signature)
            continue
        if relation is None:
            # An ordinary account row: the account is not a pool destination,
            # so it is neither attributed nor "unmapped".
            decided.append(
                AnalysedRow(
                    row=row,
                    account_id=row.account_id,
                    parent_account_id=row.account_id,
                    pool_id=None,
                    channel=None,
                    reason=None,
                    contributes_money=True,
                    contributes_usage=True,
                )
            )
            own_signatures.setdefault(row.account_id, set()).add(signature)
            continue

        parent = relation.parent_account_id
        channel = relation.channel
        if channel == QUOTA_POOL_CHANNEL:
            # A quota destination proves windows, never hourly usage.
            decided.append(
                AnalysedRow(
                    row=replace(_money_only(row), priced=False, cost_usd=0),
                    account_id=row.account_id,
                    parent_account_id=parent,
                    pool_id=None,
                    channel=channel,
                    reason=QUOTA_CHANNEL,
                )
            )
            continue
        if row.hour_utc < relation.effective_from_hour:
            decided.append(
                AnalysedRow(
                    row=_money_only(row) if channel in MONETARY_POOL_CHANNELS else row,
                    account_id=row.account_id,
                    parent_account_id=parent,
                    pool_id=None,
                    channel=channel,
                    reason=BEFORE_BOUNDARY,
                    contributes_money=True,
                    contributes_usage=channel == USAGE_POOL_CHANNEL,
                )
            )
            continue
        if channel in MONETARY_POOL_CHANNELS:
            if not row_worth_money(channel, row):
                decided.append(
                    AnalysedRow(
                        row=_money_only(row),
                        account_id=row.account_id,
                        parent_account_id=parent,
                        pool_id=None,
                        channel=channel,
                        reason=VALUATION_MISMATCH,
                        contributes_money=False,
                    )
                )
                continue
            # Money only: the channel never supplies tokens or requests.
            decided.append(
                AnalysedRow(
                    row=_money_only(row),
                    account_id=row.account_id,
                    parent_account_id=parent,
                    pool_id=relation.pool_id,
                    channel=channel,
                    reason=None,
                    contributes_money=True,
                    pool_money=True,
                )
            )
            continue
        decided.append(
            AnalysedRow(
                row=row,
                account_id=row.account_id,
                parent_account_id=parent,
                pool_id=relation.pool_id,
                channel=channel,
                reason=None,
                contributes_money=True,
                contributes_usage=True,
                pool_money=True,
                pool_usage=True,
            )
        )

    # Existing attribution identities already suppress source-proven duplicates;
    # an unproven parent/child overlap is held rather than added twice.
    partial = False
    final: list[AnalysedRow] = []
    for item in decided:
        signature = (item.row.hour_utc, item.row.model, item.row.host_id)
        if (
            item.account_id != item.parent_account_id
            and item.contributes_usage
            and signature in own_signatures.get(item.parent_account_id, set())
        ):
            partial = True
            final.append(
                replace(
                    item,
                    contributes_money=False,
                    contributes_usage=False,
                    pool_money=False,
                    pool_usage=False,
                    reason=UNPROVEN_OVERLAP,
                )
            )
            continue
        final.append(item)
    # Distinct destinations can carry alternative prices of the same usage.
    # Keep each channel visible, choosing one price for the parent total.
    valuations: dict[tuple[str, str, int, str, str, str, bool], int] = {}
    usage_choices: dict[tuple[str, str, int, str, str, str, bool], int] = {}
    priority = {"billed": 4, "metered": 3, "list": 2, "subscription": 2, "estimate": 1}
    for index, item in enumerate(final):
        key = (
            item.parent_account_id,
            item.pool_id or item.account_id,
            item.row.hour_utc,
            item.row.model,
            item.row.host_id,
            # Replacement reports carry coverage metadata. Native/OTLP rows
            # without it are independent incremental basis/priced buckets.
            item.row.basis if item.row.coverage is None else "",
            item.row.priced if item.row.coverage is None else False,
        )
        if item.contributes_usage:
            old_index = usage_choices.get(key)
            if old_index is None:
                usage_choices[key] = index
            else:

                def richness(row: HourRow) -> tuple[bool, int, int]:
                    return (
                        row.coverage == "complete",
                        sum(
                            value is not None
                            for value in (
                                row.input_tokens,
                                row.cached_tokens,
                                row.cache_write_tokens,
                                row.output_tokens,
                                row.total_tokens,
                                row.requests,
                                row.failed,
                            )
                        ),
                        priority.get(row.basis, 0),
                    )

                if richness(item.row) > richness(final[old_index].row):
                    final[old_index] = replace(
                        final[old_index], contributes_usage=False, pool_usage=False
                    )
                    usage_choices[key] = index
                else:
                    final[index] = replace(final[index], contributes_usage=False, pool_usage=False)
        if not item.contributes_money or not item.row.priced:
            continue
        previous = valuations.get(key)
        if previous is None:
            valuations[key] = index
            continue
        old = final[previous]
        if priority.get(item.row.basis, 0) > priority.get(old.row.basis, 0):
            final[previous] = replace(old, contributes_money=False, pool_money=False)
            valuations[key] = index
        else:
            final[index] = replace(final[index], contributes_money=False, pool_money=False)
    return PoolProjection(rows=final, partial=partial or any(item.reason for item in final))


def _activity_key(row: HourRow) -> tuple[str, int, str, str, str, bool]:
    return (
        row.account_id,
        row.hour_utc,
        row.model,
        row.host_id,
        row.basis if row.coverage is None else "",
        row.priced if row.coverage is None else False,
    )


def _scope_totals(all_rows: Sequence[HourRow], usage_rows: Sequence[HourRow]) -> AnalysisTotals:
    """Join the chosen valuation to its measured activity, preserving unknowns."""
    money = totals(all_rows)
    usage = totals(usage_rows)
    counts = {_activity_key(row): row.total_tokens for row in usage_rows}
    covered_cost = 0.0
    covered_tokens = 0
    for row in all_rows:
        tokens = counts.get(_activity_key(row))
        if row.priced and tokens is not None and tokens > 0:
            covered_cost += row.cost_usd
            covered_tokens += tokens
    total_tokens = (
        sum(row.total_tokens for row in usage_rows if row.total_tokens is not None)
        if usage_rows and all(row.total_tokens is not None for row in usage_rows)
        else None
    )
    return AnalysisTotals(
        cost_usd=money["cost_usd"],
        counted_cost_usd=money["counted_cost_usd"],
        tokens=total_tokens,
        unpriced=money["unpriced"],
        partial_rows=money["partial_rows"],
        per_million=covered_cost / covered_tokens * 1e6 if covered_tokens else None,
        covered_cost_usd=covered_cost,
        covered_tokens=covered_tokens,
        cost_coverage_pct=(
            covered_cost / money["cost_usd"] * 100
            if money["cost_usd"] is not None and money["cost_usd"] > 0
            else None
        ),
        tokens_complete=total_tokens is not None,
        requests=usage["requests"],
        failed=usage["failed"],
        failure_pct=usage["failure_pct"],
        reasons=usage["reasons"],
        last_at=money["last_at"],
    )


def analysis_totals(all_rows: Sequence[HourRow], usage_rows: Sequence[HourRow]) -> AnalysisTotals:
    """Aggregate one compatible cohort and its metered/billed subset."""
    result = _scope_totals(all_rows, usage_rows)
    metered_rows = [row for row in all_rows if row.basis in {"metered", "billed"}]
    if metered_rows:
        metered_keys = {_activity_key(row) for row in metered_rows}
        result.metered = _scope_totals(
            metered_rows, [row for row in usage_rows if _activity_key(row) in metered_keys]
        )
    return result


def bounded_span(start: float, until: float) -> tuple[int, float, float, bool]:
    """Clamp one requested span to the analysis read budget.

    History is never read unbounded: the window is at most
    :data:`MAX_ANALYSIS_SPAN_DAYS` long and always ends at the request's
    ``until``, so the most recent usage is what a clamped read shows.

    :param start: The requested span's first second.
    :param until: The span's end (exclusive).
    :returns: ``(since_hour, until_hour, effective_from, clamped)`` where the
        hour bounds are the ``usage_hours`` range to read.
    :raises ValueError: A span that is empty or ends before it starts.
    """
    if until <= start:
        raise ValueError("usage analysis span is empty")
    budget = MAX_ANALYSIS_SPAN_DAYS * 86_400
    effective = max(start, until - budget)
    clamped = effective > start
    since = int(effective // 3600) * 3600
    return since, until, effective, clamped


def pool_catalog(
    relations: Sequence[UsageAccountRelation],
    known_accounts: Mapping[str, Any],
) -> list[PoolCatalogEntry]:
    """Group stored relations into one catalog row per observed pool.

    A pool with only a quota or monetary destination stays listed with
    ``usage_available`` false, so the selector shows it as unavailable instead
    of hiding it or dividing a parent's usage among pools.
    """
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for relation in sorted(
        relations, key=lambda item: (item.parent_account_id, item.pool_id, item.channel)
    ):
        if relation.parent_account_id not in known_accounts:
            continue
        key = (relation.parent_account_id, relation.pool_id)
        entry = grouped.setdefault(
            key,
            {
                "channels": [],
                "display_name": relation.display_name or relation.pool_id,
                "usage_available": False,
                "effective_from_hour": relation.effective_from_hour,
            },
        )
        if relation.channel not in entry["channels"]:
            entry["channels"].append(relation.channel)
        if relation.channel == USAGE_POOL_CHANNEL:
            entry["usage_available"] = True
            entry["effective_from_hour"] = min(
                entry["effective_from_hour"], relation.effective_from_hour
            )
        if relation.display_name:
            # A label edit is display only; it never changes the identity.
            entry["display_name"] = relation.display_name
    return [
        PoolCatalogEntry(
            parent_account_id=parent,
            pool_id=pool,
            display_name=entry["display_name"],
            channels=sorted(entry["channels"]),
            usage_available=entry["usage_available"],
            effective_from_hour=entry["effective_from_hour"],
        )
        for (parent, pool), entry in sorted(grouped.items())
    ]


__all__ = [
    "BEFORE_BOUNDARY",
    "LIST_BASES",
    "MAX_ANALYSIS_ROWS",
    "MAX_ANALYSIS_SPAN_DAYS",
    "MISSING_PARENT",
    "QUOTA_CHANNEL",
    "UNMAPPED_REASONS",
    "UNPROVEN_OVERLAP",
    "VALUATION_MISMATCH",
    "AnalysedRow",
    "PoolProjection",
    "analysis_totals",
    "attribute_rows",
    "bounded_span",
    "pool_catalog",
    "row_worth_money",
]
