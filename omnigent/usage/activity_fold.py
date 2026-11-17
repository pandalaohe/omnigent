"""Sufficient statistics for the existing activity response."""

from collections import defaultdict
from dataclasses import asdict
from fractions import Fraction

from omnigent.stores.usage_hours_store import COUNTED_BASES
from omnigent.usage.ranges import bucket_start


class TotalsFold:
    def __init__(self, *, metered=True):
        self.cost = Fraction(0)
        self.counted = Fraction(0)
        self.covered_cost = Fraction(0)
        self.covered_tokens = 0
        self.tokens = 0
        self.known_tokens = True
        self.positive_priced_tokens = True
        self.priced = False
        self.unpriced = self.partial = self.requests = self.failed = self.failures = self.rows = 0
        self.reasons = defaultdict(int)
        self.last = None
        self.metered = TotalsFold(metered=False) if metered else None

    def add(self, rows):
        for row in rows:
            self.rows += 1
            self.cost += Fraction(str(row.cost_usd))
            self.priced = self.priced or row.priced
            self.unpriced += not row.priced
            self.partial += row.coverage == "partial"
            self.last = max(self.last or 0, row.updated_at)
            self.tokens += row.total_tokens or 0
            self.known_tokens = self.known_tokens and row.total_tokens is not None
            if row.priced:
                self.positive_priced_tokens = (
                    self.positive_priced_tokens and (row.total_tokens or 0) > 0
                )
            if row.priced and row.basis in COUNTED_BASES:
                self.counted += Fraction(str(row.cost_usd))
            if row.priced and (row.total_tokens or 0) > 0:
                self.covered_cost += Fraction(str(row.cost_usd))
                self.covered_tokens += row.total_tokens
            if row.requests is not None and row.failed is not None:
                self.failures += 1
                self.requests += row.requests
                self.failed += row.failed
                for reason, count in (row.reasons or {}).items():
                    self.reasons[reason] += count
            if self.metered is not None and row.basis in COUNTED_BASES:
                self.metered.add([row])

    def result(self):
        result = {
            "cost_usd": float(self.cost) if self.priced else None,
            "counted_cost_usd": float(self.counted),
            "tokens": (self.tokens or None) if self.known_tokens else None,
            "unpriced": self.unpriced,
            "partial_rows": self.partial,
            "per_million": float(self.covered_cost / self.covered_tokens * 1000000)
            if self.covered_tokens
            else None,
            "covered_cost_usd": float(self.covered_cost),
            "covered_tokens": self.covered_tokens,
            "cost_coverage_pct": float(self.covered_cost / self.cost * 100)
            if self.cost > 0
            else None,
            "tokens_complete": bool(self.rows) and self.positive_priced_tokens,
            "requests": self.requests if self.failures else None,
            "failed": self.failed if self.failures else None,
            "failure_pct": self.failed / self.requests * 100 if self.requests else None,
            "reasons": dict(self.reasons),
            "last_at": self.last,
        }
        if self.metered is not None:
            result["metered"] = self.metered.result()
        return result


class ActivityFold:
    def __init__(self, span, zone):
        self.span, self.zone = span, zone
        self.total = TotalsFold()
        self.series = {}
        self.non_counted = False
        self.unpriced = False

    def add(self, rows):
        selected = [
            row for row in rows if self.span.counted_from <= row.hour_utc < self.span.until
        ]
        self.total.add(selected)
        for row in selected:
            self.non_counted = self.non_counted or row.basis not in COUNTED_BASES
            self.unpriced = self.unpriced or not row.priced
            key = bucket_start(row.hour_utc, self.span, self.zone)
            if key not in self.series:
                self.series[key] = TotalsFold()
            self.series[key].add([row])

    def result(self):
        return {
            "span": asdict(self.span),
            "mark": "~"
            if self.non_counted
            else "≥"
            if self.unpriced
            else "≈"
            if self.total.rows and self.span.approx
            else None,
            "totals": self.total.result(),
            "series": [{"at": at, **value.result()} for at, value in sorted(self.series.items())],
        }
