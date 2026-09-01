"""Tests for the provider-neutral usage allowance boundary."""

from __future__ import annotations

import pytest

from omnigent.provider_usage_limits import validate_provider_usage_limits_snapshot


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


@pytest.mark.parametrize("used_percent", [-1, 101, True, "5"])
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
