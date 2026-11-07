"""The provider-neutral usage report accepts newer fields without inventing data."""

import pytest
from pydantic import ValidationError

from omnigent.usage.contract import UsageReport, UsageReportV2, UsageWindow, next_mark, period_of


def _window(window_id="weekly", **changes):
    return {
        "id": window_id,
        "kind": "weekly",
        "percent_used": 40,
        "window_minutes": 10_080,
        "resets_at": 2_000_000,
        **changes,
    }


def _report(**changes):
    return {
        "schema_version": 1,
        "observed_at": 1_999_000,
        "status": "available",
        "windows": [_window()],
        **changes,
    }


def test_report_drops_unknown_fields_at_every_level():
    report = UsageReport.model_validate(
        _report(
            future_top_level={"ignored": True},
            windows=[_window(future_window="ignored")],
            activity=[
                {
                    "hour": 1_998_000,
                    "requests": 2,
                    "future_activity": "ignored",
                }
            ],
            models=[{"id": "model", "future_model": "ignored"}],
        )
    )

    stored = report.stored()
    assert "future_top_level" not in stored
    assert "future_window" not in stored["windows"][0]
    assert "future_activity" not in stored["activity"][0]
    assert "future_model" not in stored["models"][0]


@pytest.mark.parametrize(
    "body",
    [
        _report(windows=[_window("same"), _window("same")]),
        _report(pinned_window="missing"),
        _report(windows=[_window(percent_used=None, used_value=None)]),
        _report(windows=[_window(percent_used=None, used_value=None, limit_value=10)]),
        _report(schema_version=3),
    ],
)
def test_report_rejects_ambiguous_or_unsupported_windows(body):
    with pytest.raises(ValidationError):
        UsageReport.model_validate(body)


def test_value_window_does_not_need_a_percentage_or_duration():
    window = UsageWindow.model_validate(
        {"id": "spend", "used_value": 41, "limit_value": 100, "unit": "USD"}
    )
    assert window.percent_used is None
    assert window.window_minutes is None
    assert window.used_value == 41


def test_period_prefers_weekly_then_monthly_then_rolling():
    now = 1_900_000
    weekly = UsageWindow.model_validate(_window(resets_at=now + 3 * 86400))
    assert period_of([weekly], now).start == now - 4 * 86400
    assert period_of([weekly], now).days == 7

    monthly = UsageWindow.model_validate(
        _window("month", kind="monthly", window_minutes=43_200, resets_at=now + 10 * 86400)
    )
    assert period_of([monthly], now).start == now - 20 * 86400
    assert period_of([], now).rolling is True
    assert period_of([], now).start == now - 30 * 86400


def test_past_reset_starts_the_new_period_at_the_old_reset():
    now = 2_000_000
    window = UsageWindow.model_validate(_window(resets_at=now - 60))
    assert period_of([window], now).start == now - 60


def test_reset_mark_distinguishes_move_natural_roll_and_jitter():
    length = 7 * 86400
    first = next_mark(None, 1_000_000, length)
    assert first == {"start": 1_000_000, "moved_from": None}
    assert next_mark(first, 1_000_600, length) == first
    assert next_mark(first, 1_100_000, length) == {
        "start": 1_100_000,
        "moved_from": 1_000_000,
    }
    assert next_mark(first, 1_000_000 + length + 60, length) == {
        "start": 1_000_000 + length + 60,
        "moved_from": None,
    }


def test_v2_accepts_sections_without_windows_and_keeps_unknown_figures_absent():
    report = UsageReportV2.model_validate(
        {
            "schema_version": 2,
            "observed_at": 1_999_000,
            "activity": [{"hour": 1_998_000, "requests": 3}],
        }
    )
    assert "windows" not in report.model_fields_set
    assert report.activity[0].tokens is None


def test_v2_balance_window_and_model_prices():
    report = UsageReportV2.model_validate(
        _report(
            schema_version=2,
            source_type="subscription",
            source="cron",
            windows=[_window(kind="term", unit="quota units", note="refund held")],
            balances=[{"id": "wallet", "remaining": -2, "unit": "CNY", "usd_per_unit": 0.14}],
            models=[{"id": "m", "cache_read_input_token_cost": 0.000001}],
        )
    )
    assert report.balances[0].remaining == -2
    assert report.models[0].cache_read_input_token_cost == 0.000001
    assert report.stored()["source"] == "cron"


def test_v2_version_is_explicit_so_legacy_requests_match_one_schema():
    schema = UsageReportV2.model_json_schema()
    assert "schema_version" in schema["required"]
    with pytest.raises(ValidationError):
        UsageReportV2.model_validate({"observed_at": 1_999_000, "windows": [_window()]})


@pytest.mark.parametrize("extra", [{}, {"coverage": "complete"}])
def test_v2_figureless_entries_require_partial(extra):
    with pytest.raises(ValidationError, match="partial"):
        UsageReportV2.model_validate(
            {
                "schema_version": 2,
                "observed_at": 1_999_000,
                "activity": [{"hour": 1_998_000, **extra}],
            }
        )
    removed = UsageReportV2.model_validate(
        {
            "schema_version": 2,
            "observed_at": 1_999_000,
            "activity": [{"hour": 1_998_000, "removed": True}],
        }
    )
    assert removed.activity[0].removed is True
