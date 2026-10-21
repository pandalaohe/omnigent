"""Pure quota pace calculations for sanitized provider snapshots."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class QuotaTiers:
    tight_below_pct: float = 50.0
    low_below_pct: float = 20.0
    floor_pct: float | None = None

    def __post_init__(self) -> None:
        tight = _number(self.tight_below_pct)
        low = _number(self.low_below_pct)
        floor = None if self.floor_pct is None else _number(self.floor_pct)
        if (
            tight is None
            or low is None
            or not 0 <= low <= tight <= 100
            or (self.floor_pct is not None and (floor is None or not 0 <= floor <= 100))
        ):
            raise ValueError("quota tiers must be finite percentages with low <= tight")


@dataclass(frozen=True)
class Window:
    label: str
    used_pct: float | None
    duration_s: float
    resets_at: float | None
    captured_at: float


@dataclass(frozen=True)
class WindowPace:
    label: str
    remaining_pct: float
    time_left_s: float
    pace_pct: float
    state: str


@dataclass(frozen=True)
class AgentQuota:
    state: str
    pace_pct: float | None
    deciding_window: str | None
    windows: tuple[WindowPace, ...]


def accept_snapshot(captured_at: object, now: object) -> bool:
    capture = _number(captured_at)
    current = _number(now)
    return (
        capture is not None and current is not None and current - 3600 <= capture <= current + 300
    )


def _period(label: str) -> str:
    # Codex labels carry a bucket ID; the period remains selectable.
    return label.partition(" (")[0]


def agent_quota(
    windows: Iterable[Window],
    tiers: QuotaTiers,
    now: float,
    deciding_period: str | None = None,
) -> AgentQuota:
    current = _number(now)
    if current is None:
        return AgentQuota("unknown", None, None, ())

    known: list[WindowPace] = []
    for window in windows:
        if not isinstance(window, Window) or not isinstance(window.label, str) or not window.label:
            continue
        used = _number(window.used_pct)
        duration = _number(window.duration_s)
        reset = _number(window.resets_at)
        if (
            not accept_snapshot(window.captured_at, current)
            or used is None
            or not 0 <= used <= 100
            or duration is None
            or duration <= 0
            or reset is None
            or reset <= current
        ):
            continue
        time_left = min(reset - current, duration)
        remaining = 100 - used
        pace = remaining * duration / time_left
        if not math.isfinite(pace):
            continue
        if used >= 100:
            state = "exhausted"
        elif tiers.floor_pct is not None and remaining < tiers.floor_pct:
            state = "low"
        elif pace >= tiers.tight_below_pct:
            state = "ok"
        elif pace >= tiers.low_below_pct:
            state = "tight"
        else:
            state = "low"
        known.append(WindowPace(window.label, remaining, time_left, pace, state))

    candidates = [
        window
        for window in known
        if deciding_period is None
        or window.label == deciding_period
        or _period(window.label) == deciding_period
    ]
    if not candidates:
        return AgentQuota("unknown", None, None, tuple(known))

    exhausted = [window for window in candidates if window.state == "exhausted"]
    floor_hits = [
        window
        for window in candidates
        if tiers.floor_pct is not None and window.remaining_pct < tiers.floor_pct
    ]
    deciding = min(exhausted or floor_hits or candidates, key=lambda window: window.pace_pct)
    return AgentQuota(deciding.state, deciding.pace_pct, deciding.label, tuple(known))


def _duration_label(minutes: float) -> str:
    if minutes % 1440 == 0:
        return f"{minutes / 1440:g}d"
    if minutes % 60 == 0:
        return f"{minutes / 60:g}h"
    return f"{minutes:g}m"


def codex_windows(snapshot: object, now: float) -> list[Window]:
    if not isinstance(snapshot, dict) or not accept_snapshot(snapshot.get("captured_at"), now):
        return []
    limits = snapshot.get("limits")
    if not isinstance(limits, list):
        return []
    result: list[Window] = []
    for bucket in limits:
        if not isinstance(bucket, dict):
            continue
        limit_id = bucket.get("limit_id")
        raw_windows = bucket.get("windows")
        if not isinstance(limit_id, str) or not limit_id or not isinstance(raw_windows, list):
            continue
        for raw in raw_windows:
            if not isinstance(raw, dict):
                continue
            duration_mins = _number(raw.get("window_duration_mins"))
            if duration_mins is None or duration_mins <= 0:
                continue
            result.append(
                Window(
                    f"{_duration_label(duration_mins)} ({limit_id})",
                    raw.get("used_percent"),
                    duration_mins * 60,
                    raw.get("resets_at"),
                    snapshot["captured_at"],
                )
            )
    return result


def claude_windows(
    snapshot: object, now: float, *, normalize_claude_labels: bool = True
) -> list[Window]:
    if not isinstance(snapshot, dict) or not accept_snapshot(snapshot.get("captured_at"), now):
        return []
    raw_windows = snapshot.get("windows")
    if not isinstance(raw_windows, list):
        return []
    result: list[Window] = []
    for raw in raw_windows:
        if not isinstance(raw, dict):
            continue
        label = raw.get("label")
        duration_mins = _number(raw.get("duration_mins"))
        if not isinstance(label, str) or not label or duration_mins is None or duration_mins <= 0:
            continue
        result.append(
            Window(
                "7d" if normalize_claude_labels and label == "w" else label,
                raw.get("used_percent"),
                duration_mins * 60,
                raw.get("resets_at"),
                snapshot["captured_at"],
            )
        )
    return result


def rate_limit_quota(snapshot: object, *, now: float) -> AgentQuota:
    """Report a depleted API bucket without inventing a subscription pace."""
    unknown = AgentQuota("unknown", None, None, ())
    if not isinstance(snapshot, dict) or not accept_snapshot(snapshot.get("captured_at"), now):
        return unknown
    windows = snapshot.get("windows")
    if not isinstance(windows, list):
        return unknown
    for window in windows:
        if not isinstance(window, dict):
            continue
        used = _number(window.get("used_percent"))
        reset = _number(window.get("resets_at"))
        label = window.get("label")
        if used == 100 and reset is not None and reset > now and isinstance(label, str) and label:
            return AgentQuota("exhausted", 0, label, ())
    return unknown


def quota_line(quotas: Mapping[str, AgentQuota], tiers: QuotaTiers, now: float) -> str | None:
    if not quotas or _number(now) is None:
        return None
    parts = []
    for product, quota in quotas.items():
        if quota.state == "unknown" or quota.pace_pct is None or quota.deciding_window is None:
            parts.append(f"{product}: unknown")
        elif quota.state == "exhausted":
            parts.append(f"{product}: exhausted ({quota.deciding_window})")
        else:
            window = next((w for w in quota.windows if w.label == quota.deciding_window), None)
            left = f", resets in {window.time_left_s / 3600:.1f}h" if window else ""
            detail = f"{quota.pace_pct:g}% pace ({quota.deciding_window}{left})"
            parts.append(f"{product}: {quota.state} {detail}")
    floor = "" if tiers.floor_pct is None else f", floor <{tiers.floor_pct:g}% remaining"
    return (
        "Quota now (pace = remaining ÷ time left vs plan average; unknown = ok): "
        + " · ".join(parts)
        + (
            f" | pace tiers: tight <{tiers.tight_below_pct:g}%, "
            f"low <{tiers.low_below_pct:g}%{floor}"
        )
    )
