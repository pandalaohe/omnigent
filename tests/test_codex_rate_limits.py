"""Tests for the sanitized Codex rate-limit boundary."""

from __future__ import annotations

import pytest

from omnigent.codex_rate_limits import (
    normalize_codex_rate_limits_response,
    validate_codex_rate_limits_snapshot,
)


def test_normalize_keeps_only_display_windows() -> None:
    snapshot = normalize_codex_rate_limits_response(
        {
            "result": {
                "account": {"email": "must-not-cross@example.com"},
                "credits": {"balance": 123},
                "rateLimitsByLimitId": {
                    "codex": {
                        "limitName": "Codex",
                        "primary": {
                            "usedPercent": 11.4,
                            "windowDurationMins": 300,
                            "resetsAt": 2_000_000_000,
                        },
                        "secondary": {
                            "usedPercent": 6,
                            "windowDurationMins": 10_080,
                        },
                    }
                },
            }
        },
        captured_at=1_900_000_000,
    )

    assert snapshot == {
        "captured_at": 1_900_000_000,
        "limits": [
            {
                "limit_id": "codex",
                "limit_name": "Codex",
                "windows": [
                    {
                        "kind": "primary",
                        "used_percent": 11.4,
                        "window_duration_mins": 300,
                        "resets_at": 2_000_000_000,
                    },
                    {
                        "kind": "secondary",
                        "used_percent": 6.0,
                        "window_duration_mins": 10_080,
                    },
                ],
            }
        ],
    }
    assert "account" not in snapshot
    assert "credits" not in snapshot


def test_normalize_supports_legacy_single_bucket_and_omits_missing_month() -> None:
    snapshot = normalize_codex_rate_limits_response(
        {
            "result": {
                "rateLimits": {
                    "primary": {"usedPercent": 3, "windowDurationMins": 300},
                    "secondary": {"usedPercent": 8, "windowDurationMins": 10_080},
                }
            }
        },
        captured_at=1,
    )

    assert snapshot is not None
    assert snapshot["limits"][0]["limit_id"] == "codex"
    assert len(snapshot["limits"][0]["windows"]) == 2


@pytest.mark.parametrize("used_percent", [-1, 101, True, "5", float("nan"), 10**400])
def test_normalize_rejects_invalid_percentages(used_percent: object) -> None:
    assert (
        normalize_codex_rate_limits_response(
            {
                "result": {
                    "rateLimits": {
                        "primary": {
                            "usedPercent": used_percent,
                            "windowDurationMins": 300,
                        }
                    }
                }
            },
            captured_at=1,
        )
        is None
    )


def test_wire_validator_rejects_extra_or_malformed_values() -> None:
    with pytest.raises(ValueError, match="snapshot"):
        validate_codex_rate_limits_snapshot({"captured_at": 1, "limits": []})
    with pytest.raises(ValueError, match="window values"):
        validate_codex_rate_limits_snapshot(
            {
                "captured_at": 1,
                "limits": [
                    {
                        "limit_id": "codex",
                        "windows": [
                            {
                                "kind": "primary",
                                "used_percent": 500,
                                "window_duration_mins": 300,
                            }
                        ],
                    }
                ],
            }
        )


@pytest.mark.parametrize("kind", [[], {}, None, True])
def test_wire_validator_rejects_non_string_window_kind(kind: object) -> None:
    with pytest.raises(ValueError, match="window kind"):
        validate_codex_rate_limits_snapshot(
            {
                "captured_at": 1,
                "limits": [
                    {
                        "limit_id": "codex",
                        "windows": [
                            {
                                "kind": kind,
                                "used_percent": 5,
                                "window_duration_mins": 300,
                            }
                        ],
                    }
                ],
            }
        )
