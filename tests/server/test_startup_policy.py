"""Admission needs explicit capacity; proposed benchmark numbers are not defaults."""

import pytest

from omnigent.server.server_config import startup_admission_policy


def test_startup_admission_is_disabled_until_explicitly_configured():
    assert startup_admission_policy({}) is None
    assert startup_admission_policy({"startup_admission": {"enabled": False}}) is None
    configured = startup_admission_policy(
        {
            "startup_admission": {
                "enabled": True,
                "host_resident_limit": 7,
                "deployment_resident_limit": 20,
                "startup_limit": 1,
            }
        }
    )
    assert configured is not None
    assert configured.host_resident_limit == 7
    assert configured.deployment_resident_limit == 20
    assert configured.startup_limit == 1


@pytest.mark.parametrize(
    "invalid",
    [
        {},
        {"host_resident_limit": True, "deployment_resident_limit": 20},
        {"host_resident_limit": 0, "deployment_resident_limit": 20},
        {"host_resident_limit": 7, "deployment_resident_limit": 20, "startup_limit": 3},
        {"host_resident_limit": 7, "deployment_resident_limit": 20, "startup_limit": 0},
    ],
)
def test_invalid_enabled_capacity_fails_closed(invalid):
    with pytest.raises(ValueError, match="startup_admission"):
        startup_admission_policy({"startup_admission": {"enabled": True, **invalid}})


def test_startup_only_policy_needs_no_resident_capacity():
    policy = startup_admission_policy(
        {"startup_admission": {"enabled": True, "mode": "startup_only"}}
    )
    assert policy is not None
    assert policy.mode == "startup_only" and policy.startup_limit == 2
    assert policy.host_resident_limit is policy.deployment_resident_limit is None
    assert policy.observation_timeout_seconds == 180 and policy.stuck_release == "manual"


@pytest.mark.parametrize(
    "settings",
    [
        {"mode": "unknown"},
        {"startup_limit": 1},
        {"startup_limit": True},
        {"stuck_release": "timeout"},
        {"observation_timeout_seconds": 0},
        {"observation_timeout_seconds": True},
        {"observation_timeout_seconds": 60},
    ],
)
def test_startup_only_rejects_unsupported_policy(settings):
    with pytest.raises(ValueError, match="startup_admission"):
        startup_admission_policy(
            {"startup_admission": {"enabled": True, "mode": "startup_only", **settings}}
        )
