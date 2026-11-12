"""Validated OTLP speed points and lossless histogram aggregation."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class SpeedPoint:
    account_id: str
    model: str
    metric: str
    start_utc: int
    end_utc: int
    unit: str
    bounds: list[float] | None
    bucket_counts: list[int] | None
    count: int
    coverage: str | None = None
    description: str | None = None
    correction: bool = False

    def values(self) -> dict[str, Any]:
        result = asdict(self)
        result.pop("correction")
        return result


def aggregate_speed(points: list[dict[str, Any]], by: str) -> list[dict[str, Any]]:
    groups: dict[tuple[str | None, str], list[dict[str, Any]]] = defaultdict(list)
    for point in points:
        groups[(point["account_id"] if by == "account" else None, point["model"])].append(point)
    rows = []
    for (account, model), entries in sorted(groups.items()):
        intervals: dict[tuple[str, int, int], list[dict[str, Any]]] = defaultdict(list)
        for point in entries:
            intervals[(point["account_id"], point["start_utc"], point["end_utc"])].append(point)
        merged: dict[str, Any] = {"ttft": None, "tpot": None}
        descriptions = {"ttft": None, "tpot": None}
        excluded = []
        included = []
        # Earliest interval chooses each metric's bucket schema deterministically.
        for (source, start, end), interval in sorted(
            intervals.items(), key=lambda item: (item[0][1], item[0][2], item[0][0])
        ):
            histograms = [p for p in interval if p["metric"] != "requests"]
            if any(
                merged[p["metric"]] is not None
                and merged[p["metric"]]["explicit_bounds"] != p["bounds"]
                for p in histograms
            ):
                excluded.append(
                    {"account": source, "start": start, "end": end, "reason": "bounds-changed"}
                )
                continue
            included.append(interval)
            for point in histograms:
                metric = point["metric"]
                if merged[metric] is None:
                    merged[metric] = {
                        "unit": point["unit"],
                        "explicit_bounds": point["bounds"],
                        "bucket_counts": list(point["bucket_counts"]),
                        "count": point["count"],
                    }
                    descriptions[metric] = point["description"]
                else:
                    hist = merged[metric]
                    hist["count"] += point["count"]
                    hist["bucket_counts"] = [
                        a + b
                        for a, b in zip(hist["bucket_counts"], point["bucket_counts"], strict=True)
                    ]
        requests = [
            next((p["count"] for p in interval if p["metric"] == "requests"), None)
            for interval in included
        ]
        complete = sum(all(p["coverage"] == "complete" for p in interval) for interval in included)
        partial = sum(any(p["coverage"] == "partial" for p in interval) for interval in included)
        rows.append(
            {
                "account": account,
                "model": model,
                "hours": len(included),
                "complete_hours": complete,
                "partial_hours": partial,
                "requests": sum(n for n in requests if n is not None)
                if requests and all(n is not None for n in requests)
                else None,
                **merged,
                "description": descriptions,
                "excluded_hours": excluded,
            }
        )
    return rows
