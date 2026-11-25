"""Tests for the FastAPI app lifespan hook.

Exercises the ``_lifespan`` context manager in
``omnigent.server.app`` to verify shutdown wiring for the
:class:`TerminalRegistry`. Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md``
§4.4, every live tmux session must be closed when the server's
lifespan exits.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
import pytest
from fastapi import FastAPI

from tests.budgets import budget

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("enabled", [False, True])
async def test_server_otel_exporters_use_the_boot_setting(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enabled: bool
) -> None:
    from omnigent.runtime import telemetry
    from omnigent.telemetry.environment import CI_ENV_VARS

    for name in CI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-credentials"))
    (tmp_path / "telemetry_enabled").write_text("on\n" if enabled else "off\n")
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "1")
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_capture_content", False)
    if hasattr(telemetry, "_server_telemetry_enabled"):
        monkeypatch.setattr(telemetry, "_server_telemetry_enabled", None)
    if hasattr(telemetry, "_server_opted_in"):
        monkeypatch.setattr(telemetry, "_server_opted_in", False)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "test-server")
    exporters = [Mock() for _ in range(3)]
    for signal, exporter in zip(("traces", "metrics", "logs"), exporters, strict=True):
        monkeypatch.setattr(telemetry, f"_init_otel_{signal}", exporter)
    monkeypatch.setattr(telemetry, "_instrument_httpx", Mock())
    fastapi_instrument = Mock()
    monkeypatch.setattr(telemetry, "instrument_fastapi_app", fastapi_instrument)

    request.getfixturevalue("app")

    assert telemetry.telemetry_enabled() is enabled
    assert [exporter.called for exporter in exporters] == [enabled] * 3
    assert fastapi_instrument.called is enabled


async def test_telemetry_setting_persists_across_server_restart(
    request: pytest.FixtureRequest,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnigent.runtime import telemetry
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.server.app import create_app
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore
    from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
    from omnigent.telemetry.environment import CI_ENV_VARS

    for name in CI_ENV_VARS | {
        "OMNIGENT_DISABLE_TELEMETRY",
        "DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
        "OMNIGENT_ANALYTICS",
        "OMNIGENT_ENABLE_TELEMETRY",
        "OMNIGENT_ADMIN_CREDENTIALS_PATH",
    }:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "instance"))
    monkeypatch.setattr(telemetry, "_initialized", False)
    monkeypatch.setattr(telemetry, "_capture_content", False)
    for signal in ("traces", "metrics", "logs"):
        monkeypatch.setattr(telemetry, f"_init_otel_{signal}", Mock())
    monkeypatch.setattr(telemetry, "_instrument_httpx", Mock())
    monkeypatch.setattr(telemetry, "instrument_fastapi_app", Mock())

    first_app = request.getfixturevalue("app")
    assert first_app.state.telemetry_enabled_at_start is False
    async with (
        first_app.router.lifespan_context(first_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=first_app), base_url="http://test"
        ) as client,
    ):
        updated = await client.put("/v1/system/telemetry", json={"telemetry_enabled": True})
        assert updated.status_code == 200
        assert updated.json() == {"enabled": True, "locked": False, "source": "setting"}

    assert (tmp_path / "instance" / "telemetry_enabled").read_text(encoding="utf-8") == "on\n"
    monkeypatch.setattr(telemetry, "_initialized", False)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    restarted_app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
    )
    assert restarted_app.state.telemetry_enabled_at_start is True
    assert telemetry.telemetry_enabled() is True
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=restarted_app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/info")).json()["telemetry"] == {
            "enabled": True,
            "locked": False,
            "source": "setting",
        }


async def test_usage_telemetry_switch_controls_live_fetch_and_events(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.telemetry import client as telemetry
    from omnigent.telemetry.environment import CI_ENV_VARS
    from omnigent.telemetry.events import SessionStoppedEvent

    monkeypatch.setenv("OMNIGENT_ADMIN_CREDENTIALS_PATH", str(tmp_path / "admin-credentials"))
    for name in CI_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    url = "https://collector.example.test/events"
    config_fetched = threading.Event()
    event_sent = threading.Event()
    received = []

    def fetch_config() -> telemetry.TelemetryConfig:
        config_fetched.set()
        return telemetry.TelemetryConfig(ingestion_url=url, disable_events=set())

    def send(request, *, timeout):
        received.append(request.full_url)
        event_sent.set()
        return Mock(__enter__=Mock(return_value=Mock()), __exit__=Mock(return_value=False))

    monkeypatch.setattr(telemetry, "_fetch_remote_config", fetch_config)
    monkeypatch.setattr(telemetry, "_BATCH_SIZE", 1)
    monkeypatch.setattr("urllib.request.urlopen", send)
    event = SessionStoppedEvent("server-installation", "test-session", None)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        assert (await client.get("/v1/info")).json()["telemetry"] == {
            "enabled": False,
            "locked": False,
            "source": "default",
        }
        telemetry.emit(event)
        assert not config_fetched.is_set()
        changed = await client.put("/v1/system/telemetry", json={"telemetry_enabled": True})
        assert changed.status_code == 200
        assert changed.json() == {"enabled": True, "locked": False, "source": "setting"}
        assert await asyncio.to_thread(config_fetched.wait, 2)
        telemetry.emit(event)
        assert await asyncio.to_thread(event_sent.wait, 2)
        assert received == [url]

        changed = await client.put("/v1/system/telemetry", json={"telemetry_enabled": False})
        assert changed.json()["enabled"] is False
        assert (await client.get("/v1/info")).json()["telemetry"]["enabled"] is False
        with patch.dict("os.environ", {"OMNIGENT_ENABLE_TELEMETRY": "yes"}):
            assert (await client.get("/v1/info")).json()["telemetry"] == {
                "enabled": True,
                "locked": True,
                "source": "OMNIGENT_ENABLE_TELEMETRY",
            }
            denied = await client.put("/v1/system/telemetry", json={"telemetry_enabled": True})
            assert denied.status_code == 409
            with patch.dict("os.environ", {"DO_NOT_TRACK": "1"}):
                assert (await client.get("/v1/info")).json()["telemetry"] == {
                    "enabled": False,
                    "locked": True,
                    "source": "DO_NOT_TRACK",
                }


@pytest.mark.parametrize("usage_enabled", [True, False])
async def test_lifespan_persists_quota_updates_and_deletes_with_display_off(
    app: FastAPI, usage_enabled: bool
) -> None:
    import time

    from omnigent.server.auth import RESERVED_USER_LOCAL
    from omnigent.usage.contract import UsageReport

    service = app.state.agent_quota_service
    service.usage_enabled = usage_enabled
    report = UsageReport(observed_at=time.time(), windows=[])
    service.report_account(RESERVED_USER_LOCAL, "removed", report)
    service.flush(force=True)
    assert "usage.account.removed" in service.snapshot_store.read(RESERVED_USER_LOCAL)

    async with app.router.lifespan_context(app):
        service.report_account(RESERVED_USER_LOCAL, "kept", report)
        assert service.delete_account(RESERVED_USER_LOCAL, "removed")
        flush_tasks = [
            task for task in asyncio.all_tasks() if task.get_name() == "usage-snapshot-flush"
        ]

    stored = service.snapshot_store.read(RESERVED_USER_LOCAL)
    assert "usage.account.kept" in stored
    assert "usage.account.removed" not in stored
    assert len(flush_tasks) == 1
    assert flush_tasks[0].done()


@pytest.mark.parametrize("ci", [True, False])
async def test_lifespan_usage_telemetry_uses_server_ci_environment(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ci: bool,
) -> None:
    """On an opted-in server, CI suppresses usage telemetry without a runner or host opt-out."""
    import omnigent.telemetry.client as telemetry
    from omnigent.telemetry.environment import CI_ENV_VARS
    from omnigent.telemetry.events import SessionStoppedEvent

    for name in CI_ENV_VARS | {
        "OMNIGENT_ANALYTICS",
        "DISABLE_TELEMETRY",
        "OMNIGENT_DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
    }:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("OMNIGENT_ENABLE_TELEMETRY", "true")
    if ci:
        monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(telemetry, "_CLIENT", None)
    monkeypatch.setattr(telemetry, "_IS_DISABLED_CACHE", [None])
    client_factory = Mock()
    client_factory.return_value.config_skipped = False
    monkeypatch.setattr(telemetry, "TelemetryClient", client_factory)
    installation_id = Mock(return_value="server-installation")
    monkeypatch.setattr("omnigent.telemetry.installation_id.get_installation_id", installation_id)
    event = SessionStoppedEvent("server-installation", "test-session", None)

    async with app.router.lifespan_context(app):
        telemetry.emit(event)
        if ci:
            assert telemetry.get_client() is None
            client_factory.assert_not_called()
            installation_id.assert_not_called()
        else:
            client_factory.assert_called_once()
            client_factory.return_value._ensure_started.assert_called_once_with()
            client_factory.return_value.emit.assert_called_once_with(event)


async def test_lifespan_shutdown_invokes_registry_shutdown(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifespan exit awaits ``registry.shutdown()``.

    Spies on the registry's ``shutdown`` method and asserts it
    was called exactly once during the lifespan exit. Catches
    the failure mode where the shutdown hook regresses to
    ``pass`` or skips the registry — every long-lived server
    would leak tmux subprocesses on restart.

    What breaks if this fails: deploy hosts accumulate orphan
    tmux sockets across restarts. Each restart adds another
    leaked socket directory. After enough restarts, /tmp fills
    up. We catch this here (in CI, in seconds) instead of in
    production after weeks of restarts.

    Doesn't actually launch any terminals — that requires a
    real tmux subprocess + real spec, which is overkill for
    verifying the *call*. The terminal-side cleanup behavior
    itself is covered by ``tests/terminals/test_registry.py``.
    """
    from omnigent.runtime import get_terminal_registry

    registry = get_terminal_registry()
    real_shutdown = registry.shutdown

    shutdown_calls = 0

    async def spy_shutdown() -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1
        await real_shutdown()

    monkeypatch.setattr(registry, "shutdown", spy_shutdown)

    async with app.router.lifespan_context(app):
        # Inside the lifespan: shutdown shouldn't have run yet.
        assert shutdown_calls == 0

    # After lifespan exit: shutdown was called exactly once.
    # If 0, the lifespan dropped the call (regression).
    # If >1, something is double-invoking the hook (also wrong).
    assert shutdown_calls == 1, (
        f"Expected registry.shutdown() to be called exactly once on "
        f"lifespan exit, got {shutdown_calls}. If 0, the shutdown "
        f"hook is missing — every server restart will leak any tmux "
        f"sessions registered during the previous lifetime."
    )


async def test_lifespan_starts_periodic_metrics_otel_publisher(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The lifespan starts periodic OTEL publication for server metrics.

    If this wiring regresses, request/resource gauges stop exporting
    even though per-request duration histograms still work.
    """
    from omnigent.server import app as server_app
    from omnigent.server.performance_metrics import (
        ServerMetricsOtelPublisher,
        ServerPerformanceMetrics,
    )

    publisher_started = asyncio.Event()

    async def fake_publisher(
        metrics: ServerPerformanceMetrics,
        *,
        otel_publisher: ServerMetricsOtelPublisher,
        interval_seconds: float = 10.0,
    ) -> None:
        """
        Capture lifespan publisher arguments and wait for cancellation.

        :param metrics: Metrics tracker owned by the app lifespan.
        :param otel_publisher: OTEL publisher supplied by the app
            lifespan.
        :param interval_seconds: Publisher interval in seconds, e.g.
            ``10.0``.
        """
        assert isinstance(metrics, ServerPerformanceMetrics)
        assert isinstance(otel_publisher, ServerMetricsOtelPublisher)
        assert interval_seconds == 10.0
        publisher_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        server_app,
        "publish_server_metrics_periodically",
        fake_publisher,
    )

    threads_before = set(threading.enumerate())
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(publisher_started.wait(), timeout=1.0)
        watchdogs = [
            thread
            for thread in set(threading.enumerate()) - threads_before
            if thread.name == "omnigent-event-loop-watchdog"
        ]
        assert len(watchdogs) == 1
    watchdogs[0].join(timeout=budget(3))
    assert not watchdogs[0].is_alive()
