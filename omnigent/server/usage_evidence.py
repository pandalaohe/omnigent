"""Source evidence receipts, freshness and opaque event pagination."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import Counter
from collections.abc import Sequence
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from omnigent.db.utils import now_epoch
from omnigent.stores.usage_evidence_store import EVIDENCE_TABLES, UsageEvidenceStore
from omnigent.usage.contract import UsageAnalysisView
from omnigent.usage.evidence import EvidenceBody, Rejected, SourceEvent, SourceStatus
from omnigent.usage.prices import PriceAliases, PriceEstimate, PriceEvidence, aggregate_prices
from omnigent.usage.ranges import RangeKey, range_spans
from omnigent.usage.speed import aggregate_speed

logger = logging.getLogger(__name__)


class UsageEvidenceService:
    def __init__(self, store: UsageEvidenceStore, retention_days: int = 400) -> None:
        self.store = store
        self.retention_days = retention_days

    def attach_prices(self, owner: str, view: UsageAnalysisView, *, bounded: bool = False) -> None:
        """Join exact account/hour/model evidence without relabelling native history."""
        if bounded and not view.rows:
            return
        start = min(row.hour for row in view.rows) if bounded else int(view.span.counted_from)
        until = max(row.hour for row in view.rows) + 3600 if bounded else view.span.until
        rows, _ = self.store.read_prices(
            owner,
            start,
            until,
            keys={(row.account_id, row.hour, row.model) for row in view.rows} if bounded else None,
            include_aliases=not bounded,
        )
        prices = {(row["account_id"], row["hour_utc"], row["model"]): row for row in rows}
        cohorts = Counter(
            (row.account_id, row.hour, row.model) for row in view.rows if row.contributes_usage
        )
        for row in view.rows:
            evidence = prices.get((row.account_id, row.hour, row.model))
            if evidence is not None and evidence["state"] is not None:
                estimate = PriceEstimate.model_validate(
                    {key: evidence[key] for key in PriceEstimate.model_fields}
                )
                if cohorts[(row.account_id, row.hour, row.model)] > 1:
                    # Hour-wide evidence cannot be split across host/basis buckets.
                    estimate = estimate.model_copy(
                        update={"missing": [*estimate.missing, "ambiguous_hour_cohort"]}
                    )
                row.price_estimate = estimate

    def report_sources(self, owner: str, body: EvidenceBody) -> dict[str, Any]:
        rows = []
        skipped = []
        cutoff = now_epoch() - self.retention_days * 86400
        for index, raw in enumerate(body.sources):
            try:
                row = SourceStatus.model_validate(raw)
            except ValidationError:
                skipped.append({"section": "sources", "index": index, "reason": "invalid_value"})
                continue
            if row.observed_at is not None and row.observed_at < cutoff:
                skipped.append({"section": "sources", "index": index, "reason": "out_of_range"})
                continue
            rows.append(row)
        prices = []
        aliases = []
        for section, raws, schema, target in (
            ("prices", body.prices, PriceEvidence, prices),
            ("price_aliases", body.price_aliases, PriceAliases, aliases),
        ):
            for index, raw in enumerate(raws):
                try:
                    row = schema.model_validate(raw)
                except ValidationError:
                    skipped.append({"section": section, "index": index, "reason": "invalid_value"})
                    continue
                stamp = row.hour if isinstance(row, PriceEvidence) else row.observed_at
                if stamp is not None and (
                    stamp < cutoff
                    or (isinstance(row, PriceEvidence) and stamp + 3600 > now_epoch())
                ):
                    skipped.append({"section": section, "index": index, "reason": "out_of_range"})
                    continue
                target.append(row)
        if prices or aliases:
            self.store.upsert_prices(owner, prices, aliases)
        applied = self.store.upsert_status(owner, rows) if rows else 0
        return {
            "applied": {"prices": len(prices), "price_aliases": len(aliases), "sources": applied},
            "skipped": skipped,
        }

    def report_events(
        self, owner: str, entries: Sequence[SourceEvent | Rejected]
    ) -> dict[str, Any]:
        now = now_epoch()
        events = []
        rejected = []
        for index, entry in enumerate(entries):
            if isinstance(entry, Rejected):
                rejected.append(entry.model_dump())
                continue
            reason = None
            if entry.time_utc < now - self.retention_days * 86400:
                reason = "out_of_range"
            elif entry.time_utc > now + 300:
                reason = "future_time"
            if reason:
                rejected.append({"index": index, "reason": reason})
            else:
                events.append(entry)
        result = (
            self.store.insert_events(owner, events) if events else {"accepted": 0, "duplicates": 0}
        )
        return dict(result, rejected=rejected)

    def read_prices(
        self,
        owner: str,
        range_key: RangeKey = "h24",
        tz: str = "UTC",
        account: str | None = None,
        model: str | None = None,
        detail: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> dict[str, Any]:
        if range_key == "period":
            raise ValueError("Invalid price range")
        now = now_epoch()
        try:
            span, _ = range_spans(
                range_key, now, ZoneInfo(tz), from_date=from_date, to_date=to_date
            )
        except KeyError:
            raise ValueError("Invalid timezone") from None
        rows, aliases = self.store.read_prices(
            owner, span.counted_from, span.until, account, model
        )
        return {
            "generated_at": now,
            "range": range_key,
            "span": {
                "requested_from": span.requested_from,
                "counted_from": span.counted_from,
                "until": span.until,
                "approx": span.approx,
            },
            "rows": aggregate_prices(rows, detail == "hours"),
            "aliases": aliases,
        }

    def read_speed(
        self,
        owner: str,
        range_key: RangeKey = "h24",
        tz: str = "UTC",
        account: str | None = None,
        model: str | None = None,
        by: str = "model",
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> dict[str, Any]:
        if range_key == "period" or by not in {"model", "account"}:
            raise ValueError("Invalid speed range or grouping")
        now = now_epoch()
        try:
            span, _ = range_spans(
                range_key, now, ZoneInfo(tz), from_date=from_date, to_date=to_date
            )
        except KeyError:
            raise ValueError("Invalid timezone") from None
        points = self.store.read_speed(owner, span.counted_from, span.until, account, model)
        return {
            "generated_at": now,
            "range": range_key,
            "span": {
                "requested_from": span.requested_from,
                "counted_from": span.counted_from,
                "until": span.until,
                "approx": span.approx,
            },
            "rows": aggregate_speed(points, by),
            "last_received_at": max((p["updated_at"] for p in points), default=None),
        }

    def read_sources(self, owner: str, account: str | None = None) -> dict[str, Any]:
        now = now_epoch()
        rows = self.store.read_sources(owner, account)
        for row in rows:
            row["current"] = None if row["fresh_until"] is None else now < row["fresh_until"]
        return {"generated_at": now, "sources": rows}

    def read_events(
        self,
        owner: str,
        account: str | None = None,
        source: str | None = None,
        severity: str | None = None,
        before: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        cursor = None
        if before is not None:
            try:
                decoded = json.loads(base64.b64decode(before, altchars=b"-_", validate=True))
                if (
                    not isinstance(decoded, list)
                    or len(decoded) != 3
                    or type(decoded[0]) is not int
                    or not 0 <= decoded[0] <= 253402300799
                    or any(
                        not isinstance(key, str) or not key or len(key) > 128
                        for key in decoded[1:]
                    )
                ):
                    raise ValueError()
                cursor = (decoded[0], decoded[1], decoded[2])
            except (ValueError, TypeError, UnicodeError):
                raise ValueError("Invalid event cursor") from None
        rows, oldest, stored = self.store.read_events(
            owner, account=account, source=source, severity=severity, before=cursor, limit=limit
        )
        next_before = None
        if len(rows) > limit:
            row = rows[limit - 1]
            next_before = base64.urlsafe_b64encode(
                json.dumps([row["time"], row["source"], row["id"]], separators=(",", ":")).encode()
            ).decode()
        return {
            "generated_at": now_epoch(),
            "events": rows[:limit],
            "next_before": next_before,
            "earliest_time": oldest,
            "stored": stored,
            "history_complete": False,
        }

    def prune(self) -> None:
        cutoff = now_epoch() - self.retention_days * 86400
        self.store.prune({table.__tablename__: cutoff for table in EVIDENCE_TABLES})

    async def run_retention(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.prune)
            except Exception:
                logger.exception("Usage evidence retention failed")
            await asyncio.sleep(86400)
