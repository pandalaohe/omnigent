"""Composition-root tests for boot telemetry order, lock-off override, and ZeroBus gating."""

from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest
import sqlalchemy

from omnigent import debug_logging as dl
from omnigent.db import utils as db_utils
from omnigent.runtime import telemetry
from omnigent.telemetry.environment import CI_ENV_VARS
from tests.deploy.test_databricks_app_lakebase_cold_start import (  # noqa: F401 — registers fixture
    _APP_MODULE,
    _patch_migrations,
    boot_exit_codes,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_COLLECTOR_URL = "http://collector.example.test:4317"
_ZEROBUS_ENDPOINT = "https://123456.zerobus.example.test/zerobus/v1/tables/cat.sch.logs/insert"


def _clear_telemetry_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in CI_ENV_VARS | {
        "OMNIGENT_DISABLE_TELEMETRY",
        "DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
        "OMNIGENT_ANALYTICS",
        "OMNIGENT_ENABLE_TELEMETRY",
        "OMNIGENT_TELEMETRY_ENABLED",
        "OMNIGENT_CONFIG",
        "OMNIGENT_CONFIG_HOME",
        "OMNIGENT_ADMIN_CREDENTIALS_PATH",
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_SERVICE_NAME",
        "OMNIGENT_AUTH_ENABLED",
        "OMNIGENT_AUTH_PROVIDER",
        "OMNIGENT_LOCAL_SINGLE_USER",
    }:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", _COLLECTOR_URL)
    monkeypatch.setenv(dl.CLIENT_ID_ENV_VAR, "cid")
    monkeypatch.setenv(dl.CLIENT_SECRET_ENV_VAR, "secret")
    monkeypatch.setenv(dl.WORKSPACE_URL_ENV_VAR, "https://ws.example.test")
    monkeypatch.setenv(dl.ENDPOINT_ENV_VAR, _ZEROBUS_ENDPOINT)


def _reset_otel_and_zerobus(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Mock], Mock, Mock]:
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_metrics_initialized", False)
    monkeypatch.setattr(telemetry, "_logs_initialized", False)
    monkeypatch.setattr(telemetry, "_capture_content", False)
    if hasattr(telemetry, "_server_telemetry_enabled"):
        monkeypatch.setattr(telemetry, "_server_telemetry_enabled", None)
    if hasattr(telemetry, "_server_opted_in"):
        monkeypatch.setattr(telemetry, "_server_opted_in", False)
    exporters = [Mock() for _ in range(3)]
    for signal, exporter in zip(("traces", "metrics", "logs"), exporters, strict=True):
        monkeypatch.setattr(telemetry, f"_init_otel_{signal}", exporter)
    monkeypatch.setattr(telemetry, "_instrument_httpx", Mock())
    fastapi_inst = Mock()
    monkeypatch.setattr(telemetry, "instrument_fastapi_app", fastapi_inst)
    sql_inst = Mock()
    monkeypatch.setattr(
        "opentelemetry.instrumentation.sqlalchemy.SQLAlchemyInstrumentor",
        lambda: SimpleInstrumentor(sql_inst),
    )
    monkeypatch.setattr(dl, "_active_sink", None)
    return exporters, sql_inst, fastapi_inst


class SimpleInstrumentor:
    def __init__(self, recorder: Mock) -> None:
        self._recorder = recorder

    def instrument(self, *, engine: object) -> None:
        self._recorder(engine=engine)


class _StubZerobus(dl.DebugLogHandler):
    def __init__(self, config: dl.DebugLogConfig, source: str) -> None:
        self.config = config
        super().__init__(source, lambda _batch: None)


def _cleanup_zerobus_sink(saved_root_handlers: list[logging.Handler]) -> None:
    sink = dl._active_sink
    if sink is not None:
        sink.shutdown()
        dl._active_sink = None
    for lg, saved in (
        (logging.getLogger(), saved_root_handlers),
        (dl.sse_event_logger(), []),
        (dl.audit_event_logger(), []),
    ):
        for handler in list(lg.handlers):
            if handler not in saved:
                lg.removeHandler(handler)
                if handler is not sink:
                    handler.close()


@pytest.mark.parametrize(
    ("mode", "expected_on"),
    [
        pytest.param("legacy-vs-lock-off", False, id="legacy-opt-in-blocked-by-lock-off"),
        pytest.param("stored-opt-in-only", True, id="stored-opt-in-instruments-first-engine"),
        pytest.param("default-off", False, id="default-off"),
    ],
)
def test_docker_entrypoint_resolves_telemetry_before_first_engine_and_gates_zerobus(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_on: bool,
) -> None:
    """Docker entrypoint resolves OTel before stores and wires ZeroBus to the boot switch."""
    _clear_telemetry_env(monkeypatch, tmp_path)
    exporters, sql_inst, fastapi_inst = _reset_otel_and_zerobus(monkeypatch)
    monkeypatch.setattr(dl, "ZerobusLogHandler", _StubZerobus)
    monkeypatch.setenv("OMNIGENT_AUTH_ENABLED", "0")
    monkeypatch.setenv("OMNIGENT_LOCAL_SINGLE_USER", "1")

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if mode == "legacy-vs-lock-off":
        monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "1")
        monkeypatch.setenv("OMNIGENT_DISABLE_TELEMETRY", "1")
    elif mode == "stored-opt-in-only":
        (data_dir / "telemetry_enabled").write_text("on\n", encoding="utf-8")

    db_url = f"sqlite:///{tmp_path / 'docker-boot.db'}"
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    sys.path.insert(0, str(_REPO_ROOT))
    saved_root_handlers = list(logging.getLogger().handlers)
    try:
        entrypoint = importlib.import_module("deploy.docker.entrypoint")
        entrypoint.run_migrations(db_url)
        resolved = entrypoint._ResolvedConfig(
            cfg={},
            database_url=db_url,
            artifact_dir=artifacts_dir,
            artifact_store_uri=None,
            host="127.0.0.1",
            port=8000,
        )
        built = entrypoint.build_app(resolved)

        assert built.app.state.telemetry_enabled_at_start is expected_on
        assert telemetry.telemetry_enabled() is expected_on
        assert [exporter.called for exporter in exporters] == [expected_on] * 3
        assert sql_inst.called is expected_on
        assert fastapi_inst.called is expected_on
        from opentelemetry.trace import INVALID_SPAN

        assert isinstance(dl._active_sink, _StubZerobus) is expected_on
        with telemetry.span("server.boot.check") as active_span:
            if not expected_on:
                assert active_span is INVALID_SPAN
    finally:
        sys.path.remove(str(_REPO_ROOT))
        _cleanup_zerobus_sink(saved_root_handlers)
        engine = db_utils._engine_cache.pop(db_utils.normalize_database_url(db_url), None)
        if engine is not None:
            engine.dispose()


@pytest.mark.parametrize(
    ("mode", "expected_on"),
    [
        pytest.param("legacy-vs-lock-off", False, id="legacy-opt-in-blocked-by-lock-off"),
        pytest.param("stored-opt-in-only", True, id="stored-opt-in-instruments-first-engine"),
        pytest.param("default-off", False, id="default-off"),
    ],
)
def test_databricks_app_resolves_telemetry_before_first_engine_and_gates_zerobus(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_on: bool,
) -> None:
    """Databricks app resolves OTel before store engines and wires ZeroBus to the boot switch."""
    exit_codes = request.getfixturevalue("boot_exit_codes")
    _clear_telemetry_env(monkeypatch, tmp_path)
    exporters, sql_inst, fastapi_inst = _reset_otel_and_zerobus(monkeypatch)
    monkeypatch.setattr(dl, "ZerobusLogHandler", _StubZerobus)

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if mode == "legacy-vs-lock-off":
        monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "1")
        monkeypatch.setenv("OMNIGENT_DISABLE_TELEMETRY", "1")
    elif mode == "stored-opt-in-only":
        (data_dir / "telemetry_enabled").write_text("on\n", encoding="utf-8")

    _patch_migrations(monkeypatch, lambda _attempt: None)
    # Exercise real get_or_create_engine -> instrument_sqlalchemy_engine ordering
    # on a fake SQLAlchemy engine without a live Postgres instance.
    monkeypatch.setattr(db_utils, "_initialize_or_verify_schema", lambda _eng, _uri: None)
    monkeypatch.setattr(db_utils, "_engine_cache", {})
    monkeypatch.setattr(
        sqlalchemy, "create_engine", lambda *a, **kw: MagicMock(name="databricks-engine")
    )
    import omnigent.runtime as runtime
    import omnigent.server.auth as auth
    from omnigent.stores.artifact_store import databricks_volumes

    monkeypatch.setattr(runtime, "init", lambda **kw: None)
    monkeypatch.setattr(auth, "create_auth_provider", lambda: MagicMock())
    monkeypatch.setattr(auth, "warn_if_single_user_exposed", lambda *_a: None)
    monkeypatch.setattr(
        databricks_volumes,
        "DatabricksVolumesArtifactStore",
        MagicMock(return_value=MagicMock()),
    )
    import omnigent.server.app as server_app

    real_create_app = server_app.create_app
    seen_before_create_app: dict[str, bool] = {}

    def spy_create_app(**kwargs: object) -> object:
        seen_before_create_app["sql_instrumented"] = sql_inst.called
        seen_before_create_app["telemetry_enabled"] = telemetry.telemetry_enabled()
        return real_create_app(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(server_app, "create_app", spy_create_app)

    saved_root_handlers = list(logging.getLogger().handlers)
    try:
        mod = importlib.import_module(_APP_MODULE)
        assert exit_codes == []
        assert seen_before_create_app == {
            "sql_instrumented": expected_on,
            "telemetry_enabled": expected_on,
        }
        assert mod.app.state.telemetry_enabled_at_start is expected_on
        assert telemetry.telemetry_enabled() is expected_on
        assert [exporter.called for exporter in exporters] == [expected_on] * 3
        assert fastapi_inst.called is expected_on
        assert isinstance(dl._active_sink, _StubZerobus) is expected_on
    finally:
        _cleanup_zerobus_sink(saved_root_handlers)
        sys.modules.pop(_APP_MODULE, None)
