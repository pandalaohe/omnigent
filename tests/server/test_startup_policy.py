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
