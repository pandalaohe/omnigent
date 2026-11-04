"""Claude usage read with no session: opt-in gate, report mapping, host loop."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnigent.host import usage_limits
from omnigent.host.claude_usage import (
    CLAUDE_USAGE_ENV_VAR,
    claude_usage_enabled,
    claude_usage_report,
)
from omnigent.host.frames import HostClaudeUsageFrame, decode_host_frame
from omnigent.host.usage_limits import AccountUsage, UsageWindow
from omnigent.usage.contract import UsageReport
from tests.host.test_connect import _cancel, _cleanup_host, _make_host_process, _RecordingWS

FIXTURES = Path(__file__).parent / "fixtures" / "usage_limits"
CLAUDE_USAGE = json.loads((FIXTURES / "claude_get_usage.json").read_text())
OBSERVED = datetime(2026, 11, 1, 12, tzinfo=UTC)


def test_claude_usage_is_off_unless_the_host_owner_opts_in(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(CLAUDE_USAGE_ENV_VAR, raising=False)
    assert claude_usage_enabled() is False
    monkeypatch.setenv(CLAUDE_USAGE_ENV_VAR, "0")
    assert claude_usage_enabled() is False
    monkeypatch.setenv(CLAUDE_USAGE_ENV_VAR, "1")
    assert claude_usage_enabled() is True


def test_recorded_claude_reading_maps_to_a_valid_report_with_statusline_window_ids():
    usage = usage_limits.normalize_claude_usage("default", CLAUDE_USAGE, OBSERVED)
    report = UsageReport.model_validate(claude_usage_report(usage))
    assert report.status == "available"
    assert report.observed_at == OBSERVED.timestamp()
    assert [(w.id, w.kind, w.percent_used, w.window_minutes) for w in report.windows] == [
        ("5h", "session", 16.0, 300),
        ("w", "weekly", 42.0, 10080),
        ("Fable", "weekly_model", 3.0, 10080),
    ]
    assert report.windows[0].resets_at == datetime(2026, 11, 1, 15, tzinfo=UTC).timestamp()


def test_model_windows_with_the_same_label_keep_unique_ids():
    windows = [
        UsageWindow(kind="weekly_model", label="Fable", percent_used=1.0),
        UsageWindow(kind="weekly_model", label="Fable", percent_used=2.0),
    ]
    usage = AccountUsage(
        provider="claude",
        account="default",
        status="available",
        windows=windows,
        observed_at=OBSERVED,
    )
    report = UsageReport.model_validate(claude_usage_report(usage))
    assert [w.id for w in report.windows] == ["Fable", "Fable-1"]


async def test_host_publishes_an_available_reading_and_stays_silent_otherwise(
    monkeypatch: pytest.MonkeyPatch,
):
    available = usage_limits.normalize_claude_usage("default", CLAUDE_USAGE, OBSERVED)
    stale = available.model_copy(update={"status": "stale"})
    readings = iter([[stale], [available]])

    async def _read(providers: tuple[str, ...]) -> list[AccountUsage]:
        assert providers == ("claude",)
        return next(readings)

    monkeypatch.setattr("omnigent.host.connect.read_usage_limits", _read)
    monkeypatch.setattr("omnigent.host.connect.CACHE_SECONDS", 0.01)
    host, ws = _make_host_process(), _RecordingWS()
    task = asyncio.create_task(host._claude_usage_loop(ws))
    try:
        await asyncio.wait_for(ws.first_send.wait(), timeout=2.0)
    finally:
        await _cancel(task)
    assert len(ws.sent) == 1
    frame = decode_host_frame(ws.sent[0])
    assert isinstance(frame, HostClaudeUsageFrame)
    assert frame.claude_usage["windows"][0]["id"] == "5h"
    _cleanup_host(host)


def test_claude_usage_frame_rejects_a_malformed_report():
    bad = json.dumps({"kind": "host.claude_usage", "claude_usage": {"windows": "x"}})
    with pytest.raises(ValueError):
        decode_host_frame(bad)
