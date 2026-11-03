"""Usage telemetry starts only after an explicit opt-in; the opt-outs still win."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import omnigent.telemetry.client as telemetry
from omnigent.telemetry.environment import (
    CI_ENV_VARS,
    ENABLE_TELEMETRY_ENV_VAR,
    is_disabled_by_env,
)


@pytest.mark.parametrize(
    ("env", "starts"),
    [
        pytest.param({}, False, id="default-off"),
        pytest.param({"OMNIGENT_ENABLE_TELEMETRY": "false"}, False, id="opt-in-false"),
        pytest.param({"OMNIGENT_ENABLE_TELEMETRY": "true"}, True, id="opt-in"),
        pytest.param(
            {"OMNIGENT_ENABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1"}, False, id="opt-out-wins"
        ),
    ],
)
def test_client_starts_only_after_opt_in(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], starts: bool
) -> None:
    monkeypatch.setattr(telemetry, "_CLIENT", None)
    monkeypatch.setattr(telemetry, "_IS_DISABLED_CACHE", [None])
    monkeypatch.setattr(telemetry, "_config_telemetry_disabled", lambda: False)
    with (
        patch.dict("os.environ", env, clear=True),
        patch.object(telemetry, "TelemetryClient") as client,
        patch("omnigent.telemetry.installation_id.get_installation_id"),
    ):
        telemetry.init_client()

        assert (telemetry.get_client() is not None) is starts
        assert client.called is starts


_OPT_OUTS = [
    ("OMNIGENT_ANALYTICS", "0"),
    ("DISABLE_TELEMETRY", "true"),
    ("OMNIGENT_DISABLE_TELEMETRY", "1"),
    ("DO_NOT_TRACK", "1"),
    *((name, "true") for name in sorted(CI_ENV_VARS)),
]


@pytest.mark.parametrize(("name", "value"), _OPT_OUTS)
def test_every_env_opt_out_wins_over_the_opt_in(name: str, value: str) -> None:
    opted_in = {ENABLE_TELEMETRY_ENV_VAR: "true"}

    assert is_disabled_by_env(opted_in) is False
    assert is_disabled_by_env({**opted_in, name: value}) is True


def test_config_opt_out_wins_over_the_opt_in(tmp_path: Path) -> None:
    (tmp_path / "config.yaml").write_text("telemetry: false\n", encoding="utf-8")
    env = {ENABLE_TELEMETRY_ENV_VAR: "true", "OMNIGENT_CONFIG_HOME": str(tmp_path)}

    with patch.dict("os.environ", env, clear=True):
        assert telemetry._compute_is_disabled() is True
