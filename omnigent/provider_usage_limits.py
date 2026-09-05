"""Small, provider-neutral usage-limit snapshots for session/UI transport."""

from __future__ import annotations

import json
import math
from typing import Any

_MAX_WINDOWS = 8
_MAX_TEXT = 64


def validate_provider_usage_limits_snapshot(snapshot: object) -> dict[str, Any] | None:
    """Validate and sanitize one harness-reported allowance snapshot."""
    if snapshot is None:
        return None
    if not isinstance(snapshot, dict):
        raise ValueError("provider usage limits must be an object or null")
    provider = snapshot.get("provider")
    captured_at = snapshot.get("captured_at")
    windows = snapshot.get("windows")
    if not isinstance(provider, str) or not provider.strip() or len(provider) > _MAX_TEXT:
        raise ValueError("invalid provider usage-limit provider")
    if not isinstance(captured_at, int) or isinstance(captured_at, bool) or captured_at <= 0:
        raise ValueError("invalid provider usage-limit capture time")
    if not isinstance(windows, list) or len(windows) > _MAX_WINDOWS:
        raise ValueError("invalid provider usage-limit windows")

    normalized_windows: list[dict[str, Any]] = []
    for candidate in windows:
        if not isinstance(candidate, dict):
            raise ValueError("invalid provider usage-limit window")
        label = candidate.get("label")
        aria_label = candidate.get("aria_label")
        used_percent = candidate.get("used_percent")
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label) > 16
            or not isinstance(aria_label, str)
            or not aria_label.strip()
            or len(aria_label) > _MAX_TEXT
            or isinstance(used_percent, bool)
            or not isinstance(used_percent, (int, float))
            or used_percent < 0
            or used_percent > 100
            or not math.isfinite(used_percent)
        ):
            raise ValueError("invalid provider usage-limit window values")
        normalized: dict[str, Any] = {
            "label": label.strip(),
            "aria_label": aria_label.strip(),
            "used_percent": float(used_percent),
        }
        duration_mins = candidate.get("duration_mins")
        if duration_mins is not None:
            if (
                not isinstance(duration_mins, int)
                or isinstance(duration_mins, bool)
                or duration_mins <= 0
            ):
                raise ValueError("invalid provider usage-limit duration")
            normalized["duration_mins"] = duration_mins
        resets_at = candidate.get("resets_at")
        if resets_at is not None:
            if not isinstance(resets_at, int) or isinstance(resets_at, bool) or resets_at <= 0:
                raise ValueError("invalid provider usage-limit reset time")
            normalized["resets_at"] = resets_at
        normalized_windows.append(normalized)

    result: dict[str, Any] = {
        "provider": provider.strip(),
        "captured_at": captured_at,
        "windows": normalized_windows,
    }
    scope = snapshot.get("scope")
    if scope is not None:
        if not isinstance(scope, str) or not scope.strip() or len(scope) > _MAX_TEXT:
            raise ValueError("invalid provider usage-limit scope")
        result["scope"] = scope.strip()
    return result


def parse_provider_usage_limits_snapshot_json(
    value: str,
    *,
    repair_clipped_label: bool = False,
) -> dict[str, Any] | None:
    """Parse a stored snapshot, optionally repairing the known label truncation.

    Legacy snapshots were written into a 256-character label. A normal Claude
    two-window payload can be exactly 257 characters, leaving only its final
    closing brace clipped. Repair is deliberately limited to that observed
    shape and still passes through the normal snapshot validator.
    """
    candidates = [value]
    if repair_clipped_label and len(value) == 256:
        candidates.append(f"{value}}}")
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            return validate_provider_usage_limits_snapshot(parsed)
        except (json.JSONDecodeError, ValueError):
            continue
    return None


__all__ = [
    "parse_provider_usage_limits_snapshot_json",
    "validate_provider_usage_limits_snapshot",
]
