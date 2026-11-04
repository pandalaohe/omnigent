"""Opt-in: publish this host's Claude plan windows read without a session."""

from __future__ import annotations

import os
import re

from omnigent.host.usage_limits import AccountUsage

CLAUDE_USAGE_ENV_VAR = "OMNIGENT_HOST_CLAUDE_USAGE"
_FIXED_WINDOW_IDS = {"session": "5h", "weekly": "w", "monthly": "m"}


def claude_usage_enabled() -> bool:
    """Return whether the host owner turned the Claude usage read on (default off)."""
    return os.environ.get(CLAUDE_USAGE_ENV_VAR, "").strip().lower() in {"1", "true", "yes", "on"}


def claude_usage_report(usage: AccountUsage) -> dict[str, object]:
    """Map one reading to a version-1 usage report; window ids match the statusLine path."""
    windows: list[dict[str, object]] = []
    used_ids: set[str] = set()
    for index, window in enumerate(usage.windows):
        window_id = _FIXED_WINDOW_IDS.get(window.kind) or re.sub(
            r"[^A-Za-z0-9_-]", "-", window.label
        ).strip("-")
        window_id = window_id[:28] or "window"
        if window_id in used_ids:
            window_id = f"{window_id}-{index}"
        used_ids.add(window_id)
        entry: dict[str, object] = {
            "id": window_id,
            "kind": window.kind,
            "label": window.label[:32],
            "percent_used": window.percent_used,
        }
        if window.window_minutes is not None:
            entry["window_minutes"] = window.window_minutes
        if window.resets_at is not None:
            entry["resets_at"] = window.resets_at.timestamp()
        windows.append(entry)
    assert usage.observed_at is not None
    return {
        "schema_version": 1,
        "observed_at": usage.observed_at.timestamp(),
        "status": "available",
        "windows": windows,
        "display_name": "Claude",
    }
