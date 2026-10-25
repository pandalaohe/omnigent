"""Host load classification and the local, fail-open load-file contract."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

Level = Literal["spare", "normal", "heavy_hold", "new_session_hold", "unknown"]
LEVELS = {"spare", "normal", "heavy_hold", "new_session_hold", "unknown"}
MAX_READING_AGE_S = 180


@dataclass(frozen=True)
class LoadLines:
    heavy: float
    new_session: float
    mem_pct: float

    def __post_init__(self) -> None:
        if not (
            all(
                not isinstance(v, bool) and math.isfinite(v)
                for v in (self.heavy, self.new_session, self.mem_pct)
            )
            and 0 < self.heavy < self.new_session <= 64
            and 0 < self.mem_pct <= 100
        ):
            raise ValueError(
                "Load lines require 0 < heavy < new_session <= 64 and 0 < mem_pct <= 100"
            )


FACTORY_LINES = LoadLines(heavy=4.0, new_session=5.0, mem_pct=90.0)


def load_equiv(
    load1: float | None,
    cpu_minute_pct: float | None,
    cpu_count: int | None,
    *,
    windows: bool,
) -> float | None:
    """Add Windows' running share to its ready-thread queue estimate."""
    if load1 is None or not math.isfinite(load1) or load1 < 0:
        return None
    if not windows:
        return load1
    if (
        cpu_minute_pct is None
        or not math.isfinite(cpu_minute_pct)
        or not 0 <= cpu_minute_pct <= 100
        or cpu_count is None
        or cpu_count <= 0
    ):
        return None
    return load1 + cpu_minute_pct / 100 * cpu_count


@dataclass
class _Flag:
    active: bool = False
    run: int = 0
    reason: str | None = None

    def update(self, condition: bool, reason: str) -> None:
        self.run = self.run + 1 if condition != self.active else 0
        if self.run >= 3:
            self.active = condition
            self.reason = reason if condition else None
            self.run = 0


class LevelState:
    """Three independent flags over completed wall-clock minute buckets."""

    def __init__(self) -> None:
        self.reading_at: float | None = None
        self._buckets: dict[int, tuple[float, float, int]] = {}
        self._next_bucket: int | None = None
        self._flags = [_Flag(), _Flag(), _Flag()]

    def add_sample(self, at: float, load_per_core: float | None, mem_pct: float) -> None:
        if (
            load_per_core is None
            or not math.isfinite(load_per_core)
            or load_per_core < 0
            or not math.isfinite(mem_pct)
            or not 0 <= mem_pct <= 100
        ):
            return
        bucket = int(at // 60)
        if self.reading_at is not None and at - self.reading_at > MAX_READING_AGE_S:
            self._flags = [_Flag(), _Flag(), _Flag()]
            self._buckets.clear()
            self._next_bucket = bucket
        if self._next_bucket is not None and bucket < self._next_bucket:
            return
        load, memory, count = self._buckets.get(bucket, (0.0, 0.0, 0))
        self._buckets[bucket] = (load + load_per_core, memory + mem_pct, count + 1)
        if self._next_bucket is None:
            self._next_bucket = bucket
        self.reading_at = at

    def step(self, now: float, lines: LoadLines) -> tuple[Level, str | None]:
        current = int(now // 60)
        for bucket in sorted(key for key in self._buckets if key < current):
            if bucket != self._next_bucket:
                for flag in self._flags:
                    flag.run = 0
            load, memory, count = self._buckets.pop(bucket)
            load /= count
            memory /= count
            new_session, heavy, spare = self._flags
            new_session.update(load >= lines.new_session, "load")
            heavy.update(
                load >= lines.heavy or memory >= lines.mem_pct,
                "load" if load >= lines.heavy else "memory",
            )
            spare.update(load < 1 and memory < lines.mem_pct, "load")
            self._next_bucket = bucket + 1
        if self._next_bucket is not None and self._next_bucket < current:
            for flag in self._flags:
                flag.run = 0
            self._next_bucket = current
        if self.reading_at is None or now - self.reading_at > MAX_READING_AGE_S:
            self._flags = [_Flag(), _Flag(), _Flag()]
            self._buckets.clear()
            self._next_bucket = current
            return "unknown", None
        levels: tuple[Level, ...] = ("new_session_hold", "heavy_hold", "spare")
        for level, flag in zip(levels, self._flags, strict=True):
            if flag.active:
                return level, None if level == "spare" else flag.reason
        return "normal", None


def utc_iso(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).isoformat(timespec="seconds")


def read_local_load(path: Path, *, now: float) -> tuple[dict[str, Any], bool]:
    """Read schema 1; unreadable, invalid or stale data always becomes unknown."""
    fallback: dict[str, Any] = {
        "schema": 1,
        "host_id": "",
        "sampled_at": utc_iso(now),
        "reading_at": None,
        "cpu_count": None,
        "load1": None,
        "load_equiv": None,
        "estimate": False,
        "load_per_core": None,
        "mem_pct": None,
        "level": "unknown",
        "reason": None,
        "lines": {"heavy": FACTORY_LINES.heavy, "new_session": FACTORY_LINES.new_session},
        "lines_synced_at": None,
        "path": str(path),
    }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not fallback.keys() <= payload.keys():
            return fallback, False
        if (
            payload["schema"] != 1
            or isinstance(payload["schema"], bool)
            or not isinstance(payload["host_id"], str)
            or payload["level"] not in LEVELS
            or payload["reason"] not in (None, "load", "memory")
            or not isinstance(payload["estimate"], bool)
        ):
            return fallback, False
        for key in ("load1", "load_equiv", "load_per_core", "mem_pct", "cpu_count"):
            value = payload[key]
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                return fallback, False
        cores = payload["cpu_count"]
        if payload["mem_pct"] is not None and payload["mem_pct"] > 100:
            return fallback, False
        if cores is not None and (not isinstance(cores, int) or cores <= 0):
            return fallback, False
        lines = payload["lines"]
        if not isinstance(lines, dict):
            return fallback, False
        LoadLines(lines["heavy"], lines["new_session"], FACTORY_LINES.mem_pct)
        for key in ("sampled_at", "reading_at", "lines_synced_at"):
            if payload[key] is None and key != "sampled_at":
                continue
            stamp = datetime.fromisoformat(payload[key])
            if stamp.utcoffset() is None:
                return fallback, False
        reading = payload["reading_at"]
        age = now - datetime.fromisoformat(reading).timestamp() if reading else math.inf
        fresh = payload["level"] != "unknown" and 0 <= age <= MAX_READING_AGE_S
        payload["path"] = str(path)
        if not fresh:
            payload.update(level="unknown", reason=None)
        return payload, fresh
    except (OSError, ValueError, TypeError, KeyError, OverflowError):
        return fallback, False
