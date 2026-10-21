"""Behavior examples for the pure quota pace calculation."""

import pytest

from omnigent.quota_pace import (
    QuotaTiers,
    Window,
    accept_snapshot,
    agent_quota,
    claude_windows,
    codex_windows,
    quota_line,
    rate_limit_quota,
)

NOW = 2_000_000.0


def window(
    label: str, used: float, duration: float, left: float, *, captured: float = NOW
) -> Window:
    return Window(label, used, duration, NOW + left, captured)


@pytest.mark.parametrize(
    ("candidate", "expected", "state"),
    [
        (window("7d", 60, 7 * 86400, 3 * 86400), 93.3333333333, "ok"),
        (window("5h", 85, 5 * 3600, 3 * 3600), 25, "tight"),
        (window("monthly", 50, 30 * 86400, 10 * 86400), 150, "ok"),
    ],
)
def test_worked_paces(candidate: Window, expected: float, state: str) -> None:
    quota = agent_quota([candidate], QuotaTiers(), NOW)
    assert quota.state == state
    assert quota.pace_pct == pytest.approx(expected)
    assert quota.deciding_window == candidate.label


@pytest.mark.parametrize(
    ("used", "state"),
    [(50, "ok"), (50.1, "tight"), (80, "tight"), (80.1, "low")],
)
def test_threshold_edges(used: float, state: str) -> None:
    assert agent_quota([window("5h", used, 100, 100)], QuotaTiers(), NOW).state == state


def test_floor_wins_even_when_another_window_has_lower_pace() -> None:
    quota = agent_quota(
        [window("5h", 96, 100, 1), window("7d", 70, 100, 100)],
        QuotaTiers(floor_pct=5),
        NOW,
    )
    assert quota.state == "low"
    assert quota.pace_pct == 400
    assert quota.deciding_window == "5h"
    assert len(quota.windows) == 2


def test_exhausted_wins_and_keeps_all_window_evidence() -> None:
    quota = agent_quota(
        [window("5h", 40, 100, 100), window("7d", 100, 100, 100)],
        QuotaTiers(),
        NOW,
    )
    assert quota.state == "exhausted"
    assert quota.pace_pct == 0
    assert quota.deciding_window == "7d"
    assert [item.state for item in quota.windows] == ["ok", "exhausted"]


@pytest.mark.parametrize("captured", [NOW - 3600, NOW + 300])
def test_snapshot_boundary_is_inclusive(captured: float) -> None:
    assert accept_snapshot(captured, NOW)


@pytest.mark.parametrize("captured", [NOW - 3600.01, NOW + 300.01, True, float("nan"), None])
def test_stale_future_and_invalid_capture_is_rejected(captured: object) -> None:
    assert not accept_snapshot(captured, NOW)


@pytest.mark.parametrize(
    "candidate",
    [
        window("old", 10, 100, 100, captured=NOW - 3601),
        window("future", 10, 100, 100, captured=NOW + 301),
        window("reset-passed", 10, 100, 0),
        Window("missing-reset", 10, 100, None, NOW),
        window("boolean-used", True, 100, 100),
        window("infinite-used", float("inf"), 100, 100),
        window("bad-duration", 10, True, 100),
    ],
)
def test_invalid_window_is_unknown(candidate: Window) -> None:
    assert agent_quota([candidate], QuotaTiers(), NOW).state == "unknown"


def test_unknown_selected_period_retains_other_evidence() -> None:
    candidates = [window("5h", 100, 100, 100), window("7d", 10, 100, 100)]
    selected = agent_quota(candidates, QuotaTiers(), NOW, deciding_period="7d")
    assert selected.state == "ok"
    assert selected.deciding_window == "7d"
    assert len(selected.windows) == 2
    absent = agent_quota(candidates, QuotaTiers(), NOW, deciding_period="monthly")
    assert absent.state == "unknown"
    assert absent.deciding_window is None
    assert len(absent.windows) == 2


def test_codex_adapter_keeps_bucket_identity_and_period_selection() -> None:
    snapshot = {
        "captured_at": NOW,
        "limits": [
            {
                "limit_id": "alpha",
                "windows": [
                    {
                        "kind": "primary",
                        "used_percent": 85,
                        "window_duration_mins": 300,
                        "resets_at": NOW + 10800,
                    },
                    {
                        "kind": "secondary",
                        "used_percent": 10,
                        "window_duration_mins": 10080,
                        "resets_at": NOW + 86400,
                    },
                ],
            },
            {
                "limit_id": "beta",
                "windows": [
                    {
                        "kind": "primary",
                        "used_percent": 100,
                        "window_duration_mins": 300,
                        "resets_at": NOW + 10800,
                    }
                ],
            },
        ],
    }
    windows = codex_windows(snapshot, NOW)
    assert [item.label for item in windows] == ["5h (alpha)", "7d (alpha)", "5h (beta)"]
    selected = agent_quota(windows, QuotaTiers(), NOW, deciding_period="7d")
    assert selected.state == "ok"
    assert selected.deciding_window == "7d (alpha)"
    assert len(selected.windows) == 3
    assert codex_windows({**snapshot, "captured_at": NOW - 3601}, NOW) == []


def test_claude_adapter_alias_and_monthly_label() -> None:
    snapshot = {
        "provider": "claude",
        "captured_at": NOW,
        "windows": [
            {
                "label": "w",
                "duration_mins": 10080,
                "used_percent": 60,
                "resets_at": NOW + 3 * 86400,
            },
            {
                "label": "Monthly",
                "duration_mins": 43200,
                "used_percent": 50,
                "resets_at": NOW + 10 * 86400,
            },
        ],
    }
    windows = claude_windows(snapshot, NOW)
    assert [item.label for item in windows] == ["7d", "Monthly"]
    monthly = agent_quota(windows, QuotaTiers(), NOW, deciding_period="Monthly")
    assert monthly.pace_pct == 150
    assert claude_windows({**snapshot, "captured_at": NOW + 301}, NOW) == []


def test_quota_line_uses_current_tiers_and_reports_unknown_and_exhausted() -> None:
    tiers = QuotaTiers(tight_below_pct=70, low_below_pct=30, floor_pct=5)
    quotas = {
        "Codex": agent_quota([window("5h", 100, 100, 100)], tiers, NOW),
        "Claude": agent_quota([], tiers, NOW),
    }
    line = quota_line(quotas, tiers, NOW)
    assert line is not None
    assert "Codex: exhausted (5h)" in line
    assert "Claude: unknown" in line
    assert "tight <70%, low <30%, floor <5% remaining" in line
    assert quota_line({}, tiers, NOW) is None


@pytest.mark.parametrize(
    ("used", "reset", "state"),
    [(100, NOW + 60, "exhausted"), (100, NOW, "unknown"), (80, NOW + 60, "unknown")],
)
def test_api_buckets_do_not_invent_subscription_pace(used, reset, state) -> None:
    quota = rate_limit_quota(
        {
            "captured_at": NOW,
            "windows": [{"label": "tokens", "used_percent": used, "resets_at": reset}],
        },
        now=NOW,
    )
    assert quota.state == state
    assert quota.pace_pct == (0 if state == "exhausted" else None)
    assert quota.windows == ()


@pytest.mark.parametrize(
    ("tight", "low", "floor"),
    [
        (True, 20, None),
        (50, float("nan"), None),
        (20, 50, None),
        (50, 20, float("inf")),
    ],
)
def test_invalid_tiers_are_rejected(tight: float, low: float, floor: float | None) -> None:
    with pytest.raises(ValueError):
        QuotaTiers(tight, low, floor)
