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


def test_server_config_override_locks_off_even_with_env_opt_in(tmp_path: Path) -> None:
    from omnigent.server.telemetry_settings import TelemetrySettings

    env = {ENABLE_TELEMETRY_ENV_VAR: "1", "OMNIGENT_CONFIG_HOME": str(tmp_path)}
    with patch.dict("os.environ", env, clear=True):
        assert TelemetrySettings({"telemetry": False}).status().model_dump() == {
            "enabled": False,
            "locked": True,
            "source": "config.yaml",
        }


def test_revoked_server_consent_blocks_config_and_buffered_outbound_requests() -> None:
    from omnigent.telemetry.events import SessionStoppedEvent

    client = telemetry.TelemetryClient(enabled=lambda: False)
    record = telemetry._build_record(SessionStoppedEvent("installation", "session", None))

    with patch("urllib.request.urlopen") as urlopen:
        client._load_config()
        client._send([record], "https://collector.example.test/events")
        urlopen.assert_not_called()


def test_consent_revoked_before_transport_call_blocks_queued_get_and_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-checking before urlopen stops a GET or POST whose worker passed its outer gate."""
    import urllib.request

    from omnigent.telemetry.events import SessionStoppedEvent

    allowed = True
    client = telemetry.TelemetryClient(enabled=lambda: allowed)
    record = telemetry._build_record(SessionStoppedEvent("installation", "session", None))

    def flip_off_during_config_url() -> str:
        nonlocal allowed
        allowed = False
        client.revoke()
        return "https://config.example.test/0.0.0.json"

    real_request = urllib.request.Request

    def flip_off_during_post_request(*args: object, **kwargs: object) -> urllib.request.Request:
        nonlocal allowed
        allowed = False
        post_client.revoke()
        return real_request(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(telemetry, "_config_url", flip_off_during_config_url)
    with patch("urllib.request.urlopen") as urlopen:
        client._load_config()
        urlopen.assert_not_called()
        assert client.config_skipped is True

    allowed = True
    post_client = telemetry.TelemetryClient(enabled=lambda: allowed)
    monkeypatch.setattr(urllib.request, "Request", flip_off_during_post_request)
    with patch("urllib.request.urlopen") as urlopen:
        post_client._send([record], "https://collector.example.test/events")
        urlopen.assert_not_called()


def test_telemetry_setting_isolates_instances_by_data_dir_and_keeps_docker_anchor(
    tmp_path: Path,
) -> None:
    from omnigent.server.sharing_settings import (
        read_telemetry_enabled_override,
        write_telemetry_enabled_override,
    )
    from omnigent.server.telemetry_settings import TelemetrySettings

    shared_home = tmp_path / "home"
    data_a = tmp_path / "instance-a"
    data_b = tmp_path / "instance-b"
    docker_data = tmp_path / "docker-data"
    shared_home.mkdir()

    env_a = {"HOME": str(shared_home), "OMNIGENT_DATA_DIR": str(data_a)}
    env_b = {"HOME": str(shared_home), "OMNIGENT_DATA_DIR": str(data_b)}
    env_docker = {
        "HOME": str(shared_home),
        "OMNIGENT_DATA_DIR": str(data_a),
        "OMNIGENT_ADMIN_CREDENTIALS_PATH": str(docker_data / "admin-credentials"),
    }

    with patch.dict("os.environ", env_a, clear=True):
        write_telemetry_enabled_override(True)
        assert read_telemetry_enabled_override() is True
        assert TelemetrySettings({}).status().model_dump() == {
            "enabled": True,
            "locked": False,
            "source": "setting",
        }

    assert (data_a / "telemetry_enabled").read_text(encoding="utf-8") == "on\n"
    assert not (shared_home / ".omnigent" / "telemetry_enabled").exists()

    with patch.dict("os.environ", env_b, clear=True):
        assert read_telemetry_enabled_override() is None
        assert TelemetrySettings({}).status().model_dump() == {
            "enabled": False,
            "locked": False,
            "source": "default",
        }
        write_telemetry_enabled_override(False)
        assert read_telemetry_enabled_override() is False

    with patch.dict("os.environ", env_a, clear=True):
        assert read_telemetry_enabled_override() is True

    with patch.dict("os.environ", env_docker, clear=True):
        assert read_telemetry_enabled_override() is None
        write_telemetry_enabled_override(True)
        assert read_telemetry_enabled_override() is True
    assert (docker_data / "telemetry_enabled").read_text(encoding="utf-8") == "on\n"


@pytest.mark.parametrize(
    "payload", [b"\xff\xfe", b"maybe\n", b""], ids=["bad-utf8", "bogus", "empty"]
)
def test_corrupt_telemetry_setting_falls_back_to_off(tmp_path: Path, payload: bytes) -> None:
    from omnigent.server.telemetry_settings import TelemetrySettings

    (tmp_path / "telemetry_enabled").write_bytes(payload)
    env = {"HOME": str(tmp_path), "OMNIGENT_DATA_DIR": str(tmp_path)}
    with patch.dict("os.environ", env, clear=True):
        assert TelemetrySettings({}).status().model_dump() == {
            "enabled": False,
            "locked": False,
            "source": "default",
        }


def test_direct_disable_env_wins_over_enable_env(tmp_path: Path) -> None:
    from omnigent.server.telemetry_settings import TelemetrySettings

    env = {
        "HOME": str(tmp_path),
        "OMNIGENT_DATA_DIR": str(tmp_path),
        "OMNIGENT_ENABLE_TELEMETRY": "1",
        "OMNIGENT_DISABLE_TELEMETRY": "1",
    }
    with patch.dict("os.environ", env, clear=True):
        assert TelemetrySettings({}).status().model_dump() == {
            "enabled": False,
            "locked": True,
            "source": "OMNIGENT_DISABLE_TELEMETRY",
        }
