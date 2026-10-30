"""Usage telemetry starts only after an explicit opt-in; the opt-outs still win."""

from __future__ import annotations

from unittest.mock import patch

import pytest

import omnigent.telemetry.client as telemetry


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
