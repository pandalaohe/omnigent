"""Tests for the provider-neutral usage allowance boundary."""

from __future__ import annotations

import pytest

from omnigent.provider_usage_limits import (
    pi_provider_usage_limits_from_headers,
    validate_provider_usage_limits_snapshot,
)


def test_pi_reports_documented_api_limits_without_forwarding_other_headers() -> None:
    snapshot = pi_provider_usage_limits_from_headers(
        {
            "anthropic-ratelimit-requests-limit": "100",
            "anthropic-ratelimit-requests-remaining": "25",
            "anthropic-ratelimit-requests-reset": "2030-01-01T00:00:00Z",
            "anthropic-ratelimit-input-tokens-limit": "10000",
            "anthropic-ratelimit-input-tokens-remaining": "0",
            "authorization": "discarded",
            "unrelated-account-id": "discarded",
        },
        captured_at=1_900_000_000,
    )
    assert snapshot == {
        "provider": "Anthropic API",
        "scope": "API rate limits",
        "captured_at": 1_900_000_000,
        "windows": [
            {
                "label": "requests",
                "aria_label": "requests rate limit",
                "used_percent": 75.0,
                "resets_at": 1_893_456_000,
            },
            {
                "label": "input",
                "aria_label": "input tokens rate limit",
                "used_percent": 100.0,
            },
        ],
    }


@pytest.mark.parametrize("bad", [None, True, "", "nan", "Infinity", "-1", "101", "1" * 129])
def test_pi_omits_unknown_or_invalid_provider_limits(bad: object) -> None:
    assert (
        pi_provider_usage_limits_from_headers(
            {
                "anthropic-ratelimit-tokens-limit": "100",
                "anthropic-ratelimit-tokens-remaining": bad,
            },
            captured_at=1,
        )
        is None
    )


def test_provider_usage_limits_accepts_bounded_windows() -> None:
    snapshot = {
        "provider": "Claude",
        "scope": "Claude plan",
        "captured_at": 1_900_000_000,
        "windows": [
            {
                "label": "5h",
                "aria_label": "5 hour",
                "used_percent": 11.4,
                "duration_mins": 300,
            }
        ],
    }
    assert validate_provider_usage_limits_snapshot(snapshot) == snapshot


@pytest.mark.parametrize("used_percent", [-1, 101, True, "5", float("nan"), float("inf"), 10**400])
def test_provider_usage_limits_rejects_invalid_percent(used_percent: object) -> None:
    with pytest.raises(ValueError, match="window values"):
        validate_provider_usage_limits_snapshot(
            {
                "provider": "Claude",
                "captured_at": 1,
                "windows": [
                    {
                        "label": "5h",
                        "aria_label": "5 hour",
                        "used_percent": used_percent,
                    }
                ],
            }
        )
