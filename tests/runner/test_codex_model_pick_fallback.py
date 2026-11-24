"""Model picks are checked against the model list of the actual Codex launch shape."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator, Callable, Coroutine, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.config import global_config_path, load_effective_config
from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.codex_native import app_server as codex_app
from omnigent.harnesses.codex_native import bridge as codex_native_bridge
from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.models import model_catalog_store
from omnigent.runner.app import ResolvedSpec
from omnigent.runner.native import orchestration as runner_native
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    REAL_CODEX_LAUNCH_CATALOG,
    REAL_CODEX_REPROBED_LAUNCH_CATALOG,
    REAL_DATABRICKS_PROFILE_CODEX_MODELS,
)

_CODEX_PATH = "/missing-test-bin/codex"
_SESSION_ID = "c91a9d508b344ad59c65628ed80b5230"
_PROVIDER_DEFAULT = "gpt-5.6-terra"
_RETIRED_PICK = "gpt-5.4-retired"
_DISCOVER_THREAD_AND_FORWARD = runner_native._codex_discover_thread_and_forward
_RESOLVE_NATIVE_CODEX_LAUNCH = codex_app.resolve_native_codex_launch


def _unavailable_notice(pick: str, launched: str) -> dict[str, str]:
    """The in-session notice a launch posts when it replaces an unavailable pick."""
    return {
        "source": "execution",
        "code": "codex_native_model_unavailable",
        "message": f"{pick} isn't available on this host; using {launched}.",
        "level": "info",
    }


@dataclass
class _LaunchHarness:
    """A real runner launch with scripted provider configuration and processes."""

    agent_spec: AgentSpec | ResolvedSpec
    snapshot: dict[str, Any]
    server_client: httpx.AsyncClient
    app_server: SimpleNamespace
    event_client: SimpleNamespace
    registry: SessionResourceRegistry
    events: list[str]
    builds: list[dict[str, Any]]
    resets: list[dict[str, Any]]
    notices: list[dict[str, Any]]
    probe: AsyncMock
    response_status: dict[str, int]
    workspace_models: Mock
    routing: dict[str, bool]
    # The unlisted pick each fresh launch hands its thread binding (None: listed).
    unlisted_bindings: list[str | None]

    async def launch(self) -> SessionResourceView:
        """Run the production launch path, including snapshot and catalog reads."""
        return await runner_native._auto_create_codex_terminal(
            _SESSION_ID,
            self.registry,
            lambda _sid, _event: None,
            agent_spec=self.agent_spec,
            server_client=self.server_client,
        )

    def catalog_shape(self, *, ambient: bool = False) -> codex_app.NativeCodexLaunch:
        """Resolve the catalog shape without a per-session model selection."""
        spec = (
            self.agent_spec.spec if isinstance(self.agent_spec, ResolvedSpec) else self.agent_spec
        )
        return codex_app.resolve_native_codex_launch(model=None, spec=None if ambient else spec)

    def seed_catalog(
        self,
        rows: list[dict[str, Any]],
        *,
        stale: bool = False,
        ambient: bool = False,
    ) -> str:
        """Seed a real store entry, optionally older than its freshness budget."""
        fingerprint = codex_app.codex_catalog_fingerprint(
            self.catalog_shape(ambient=ambient), codex_path=_CODEX_PATH
        )
        model_catalog_store.write_catalog("codex-native", fingerprint, rows)
        if stale:
            old = time.time() - model_catalog_store.CATALOG_STALE_AFTER_S - 60
            os.utime(model_catalog_store.catalog_path("codex-native", fingerprint), (old, old))
        return fingerprint


@pytest.fixture
async def codex_launch_harness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_LaunchHarness]:
    """Keep the real catalog store and launcher isolated from all live CLIs."""
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    # Catalog fingerprints also include Codex's source config and credential files.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://runner.test")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr("omnigent.inner.codex_executor._find_codex_cli", lambda: _CODEX_PATH)
    monkeypatch.setattr(codex_app, "_find_codex_cli", lambda: _CODEX_PATH)
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.process_registry.reap_codex_native_processes_for_state_dir",
        lambda _path: None,
    )
    databrickscfg = tmp_path / "databrickscfg"
    databrickscfg.write_text("[session-provider]\nhost = https://workspace.example.com\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(databrickscfg))
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda _profile: SimpleNamespace(token="workspace-token"),
    )
    workspace_models = Mock(return_value=())
    monkeypatch.setattr(
        "omnigent.models.databricks_model_discovery.discover_databricks_codex_models",
        workspace_models,
    )

    routing = {"databricks_profile": False}

    def resolve_launch(
        *,
        model: str | None,
        spec: AgentSpec | None = None,
        terminal_launch_args: Sequence[str] = (),
    ) -> codex_app.NativeCodexLaunch:
        del terminal_launch_args
        provider = "session-provider" if spec is not None else "ambient-provider"
        if routing["databricks_profile"]:
            return codex_app.NativeCodexLaunch([], model or _PROVIDER_DEFAULT, provider)
        return codex_app.NativeCodexLaunch(
            config_overrides=[f'model_provider="{provider}"'],
            model=model or _PROVIDER_DEFAULT,
            profile=None,
        )

    monkeypatch.setattr(codex_app, "resolve_native_codex_launch", resolve_launch)
    monkeypatch.setattr(codex_app, "codex_launch_catalog", REAL_CODEX_LAUNCH_CATALOG)
    monkeypatch.setattr(
        codex_app, "codex_reprobed_launch_catalog", REAL_CODEX_REPROBED_LAUNCH_CATALOG
    )
    monkeypatch.setattr(
        codex_app, "databricks_profile_codex_models", REAL_DATABRICKS_PROFILE_CODEX_MODELS
    )
    probe = AsyncMock(return_value=[])
    monkeypatch.setattr(codex_app, "probe_codex_model_options", probe)
    events: list[str] = []
    builds: list[dict[str, Any]] = []
    resets: list[dict[str, Any]] = []
    notices: list[dict[str, Any]] = []
    snapshot: dict[str, Any] = {"model_override": _RETIRED_PICK}
    response_status = {"reset": 200, "notice": 200}
    initial_probes = set(model_catalog_store._inflight.values())

    def request(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            assert request.url.path == f"/v1/sessions/{_SESSION_ID}"
            return httpx.Response(200, json=snapshot)
        if request.method == "PATCH" and set(json.loads(request.content)) == {
            "external_session_id"
        }:
            # A fork records the Codex thread it minted.
            return httpx.Response(200, json={})
        assert request.method == "POST", "fallback must never unconditionally PATCH the pick"
        if request.url.path == f"/v1/sessions/{_SESSION_ID}/events":
            events.append("notice")
            notices.append(json.loads(request.content)["data"]["item_data"])
            return httpx.Response(response_status["notice"], json={})
        assert request.url.path == f"/v1/sessions/{_SESSION_ID}/model-override/reset"
        reset = json.loads(request.content)
        assert set(reset) == {"expected_model_override"}
        events.append("reset")
        resets.append(reset)
        if response_status["reset"] >= 400:
            return httpx.Response(response_status["reset"], json={"error": "reset unavailable"})
        applied = snapshot["model_override"] == reset["expected_model_override"]
        if applied:
            snapshot["model_override"] = None
        return httpx.Response(200, json={"reset": applied})

    app_server = SimpleNamespace(
        workspace_profile=None,
        resident_incarnation_id="test-backend-incarnation",
        exit_confirmed=False,
        process_exit_confirmed=False,
        session_id=_SESSION_ID,
        codex_path=_CODEX_PATH,
        codex_cli_version=(0, 145, 0),
        codex_home=tmp_path / "unused-home",
        env={"OPENAI_API_KEY": "test-key"},
        config_overrides=[],
        listen_url=None,
        agent_pid=None,
        start=AsyncMock(side_effect=lambda: events.append("app-server-start")),
        close=AsyncMock(),
    )

    def build_server(**kwargs: Any) -> SimpleNamespace:
        builds.append(kwargs)
        app_server.codex_home = kwargs["codex_home"]
        # The real builder writes provider definitions into the private config.
        app_server.config_overrides = [
            override
            for override in kwargs["extra_config_overrides"]
            if not override.startswith("model_providers.")
        ]
        return app_server

    async def launch_terminal(**kwargs: Any) -> SessionResourceView:
        events.append("terminal-launch")
        return SessionResourceView(
            id="terminal_codex_main",
            type="terminal",
            session_id=kwargs["session_id"],
            name="Codex",
        )

    event_client = SimpleNamespace(connect=AsyncMock(), close=AsyncMock())
    forwarder_release = asyncio.Event()
    unlisted_bindings: list[str | None] = []

    def forwarder(
        *, unlisted_model: str | None = None, **_kwargs: Any
    ) -> Coroutine[Any, Any, bool]:
        unlisted_bindings.append(unlisted_model)
        return forwarder_release.wait()

    monkeypatch.setattr(codex_app, "build_codex_native_server", build_server)
    monkeypatch.setattr(codex_app, "CodexAppServerClient", lambda **_kwargs: event_client)
    monkeypatch.setattr(runner_native, "_codex_discover_thread_and_forward", forwarder)
    registry = SessionResourceRegistry()
    registry.set_client_surface(_SESSION_ID, "desktop")
    monkeypatch.setattr(
        registry, "launch_auxiliary_terminal", AsyncMock(side_effect=launch_terminal)
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(request), base_url="http://runner.test"
    ) as server_client:
        yield _LaunchHarness(
            agent_spec=AgentSpec(
                spec_version=1,
                name="codex",
                executor=ExecutorSpec(config={"harness": "codex-native"}),
            ),
            snapshot=snapshot,
            server_client=server_client,
            app_server=app_server,
            event_client=event_client,
            registry=registry,
            events=events,
            builds=builds,
            resets=resets,
            notices=notices,
            probe=probe,
            response_status=response_status,
            workspace_models=workspace_models,
            routing=routing,
            unlisted_bindings=unlisted_bindings,
        )
    await runner_native._cancel_auto_forwarder_task(_SESSION_ID)
    runner_native._AUTO_CODEX_APP_SERVERS.pop(_SESSION_ID, None)
    pending_probes = set(model_catalog_store._inflight.values()) - initial_probes
    for task in pending_probes:
        task.cancel()
    await asyncio.gather(*pending_probes, return_exceptions=True)
    await asyncio.sleep(0)


def _set_unlisted_mode(mode: str) -> None:
    path = global_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"codex_native_unlisted_model: {mode}\n")


@pytest.fixture
def unlisted_as_requested(codex_launch_harness: _LaunchHarness) -> None:
    _set_unlisted_mode("as_requested")


@pytest.mark.asyncio
@pytest.mark.parametrize("pick", ["databricks-gpt-5-6-sol", "system.ai.gpt-5-6-sol"])
async def test_equivalent_gateway_pick_launches_without_reset(
    pick: str, codex_launch_harness: _LaunchHarness
) -> None:
    """Gateway prefixes and endpoint punctuation do not make a pick unavailable."""
    harness = codex_launch_harness
    harness.snapshot["model_override"] = pick
    harness.seed_catalog([{"id": "gpt-5.6-sol", "model": "gpt-5.6-sol", "isDefault": True}])

    await harness.launch()

    assert harness.builds[0]["model"] == pick
    assert harness.builds[0]["session_id"] == _SESSION_ID
    assert harness.app_server.env["OMNIGENT_CLIENT_SURFACE"] == "desktop"
    assert harness.resets == []
    assert harness.notices == []
    assert harness.unlisted_bindings == [None]
    harness.probe.assert_not_awaited()
    harness.workspace_models.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live_listing", "cached_listing", "kept"),
    [
        pytest.param(
            ("system.ai.gpt-5-6-sol", "system.ai.gpt-6-1-sol"), None, True, id="live-serves"
        ),
        pytest.param(
            ("system.ai.gpt-5-6-sol",), ["system.ai.gpt-6-1-sol"], False, id="live-lacks"
        ),
        pytest.param(OSError("listing failed"), ["system.ai.gpt-5-6-sol"], True, id="cache-lacks"),
        pytest.param(OSError("listing failed"), None, True, id="no-listing"),
    ],
)
async def test_profile_launch_validates_the_pick_against_its_workspace_listing(
    live_listing: tuple[str, ...] | Exception,
    cached_listing: list[str] | None,
    kept: bool,
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The live workspace listing decides; a miss in ucode's copy or no listing keeps the pick."""
    harness = codex_launch_harness
    harness.routing["databricks_profile"] = True
    pick = "gpt-6.1-sol"
    harness.snapshot["model_override"] = pick
    # Codex's own list disagrees with the workspace, so only the workspace can decide.
    harness.seed_catalog(
        [{"id": "gpt-5.6-sol", "isDefault": True}, *([] if kept else [{"id": pick}])]
    )
    if isinstance(live_listing, Exception):
        harness.workspace_models.side_effect = live_listing
    else:
        harness.workspace_models.return_value = live_listing
    cached_state = None if cached_listing is None else SimpleNamespace(codex_models=cached_listing)
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state.read_ucode_state", lambda _host: cached_state
    )

    await harness.launch()

    assert harness.builds[0]["profile"] == "session-provider"
    if kept:
        assert harness.builds[0]["model"] == pick
        assert harness.resets == []
        assert harness.notices == []
        assert harness.snapshot["model_override"] == pick
    else:
        assert harness.builds[0]["model"] == "system.ai.gpt-5-6-sol"
        assert harness.resets == [{"expected_model_override": pick}]
        assert harness.notices == [_unavailable_notice(pick, "gpt-5.6-sol")]
        assert harness.snapshot["model_override"] is None
    harness.workspace_models.assert_called_once_with(
        "https://workspace.example.com", "workspace-token"
    )
    harness.probe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("unlisted_as_requested")
async def test_profile_fallback_prefers_the_agents_listed_model(
    codex_launch_harness: _LaunchHarness,
) -> None:
    """A profile fallback prefers the agent's listed model over the workspace default."""
    harness = codex_launch_harness
    harness.routing["databricks_profile"] = True
    assert isinstance(harness.agent_spec, AgentSpec)
    harness.agent_spec.executor.config["model"] = "gpt-5.5"
    harness.workspace_models.return_value = ("system.ai.gpt-5-6-sol", "system.ai.gpt-5-5")

    await harness.launch()

    assert harness.builds[0]["model"] == "gpt-5.5"
    assert harness.resets == [{"expected_model_override": _RETIRED_PICK}]
    assert harness.notices == [_unavailable_notice(_RETIRED_PICK, "gpt-5.5")]


@pytest.mark.asyncio
async def test_codex_policy_hook_token_refresh_keeps_runner_responsive(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = codex_launch_harness
    harness.snapshot["model_override"] = None
    loop = asyncio.get_running_loop()
    loop_progressed: list[bool] = []

    def token_factory() -> str:
        released = threading.Event()
        loop.call_soon_threadsafe(released.set)
        loop_progressed.append(released.wait(timeout=1.0))
        return "test-hook-token"

    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: token_factory)
    await harness.launch()

    assert harness.builds[0]["ap_auth_headers"]["Authorization"] == "Bearer test-hook-token"
    assert loop_progressed == [True], "Codex hook token refresh blocked the runner event loop"


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_model", [None, "gpt-5.6-astra", _RETIRED_PICK])
async def test_unavailable_pick_resets_only_after_fallback_terminal_launch(
    agent_model: str | None, codex_launch_harness: _LaunchHarness
) -> None:
    """Without opt-in a live miss retires the pick after the replacement terminal starts."""
    harness = codex_launch_harness
    assert isinstance(harness.agent_spec, AgentSpec)
    if agent_model is not None:
        harness.agent_spec.executor.config["model"] = agent_model
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}, {"id": "gpt-5.6-astra"}])

    await harness.launch()

    launched = agent_model if agent_model == "gpt-5.6-astra" else _PROVIDER_DEFAULT
    assert harness.builds[0]["model"] == launched
    assert harness.resets == [{"expected_model_override": _RETIRED_PICK}]
    assert harness.snapshot["model_override"] is None
    assert harness.unlisted_bindings == [None]
    assert harness.notices == [_unavailable_notice(_RETIRED_PICK, launched)]
    assert harness.events[-3:] == ["terminal-launch", "reset", "notice"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("config_text", "reason"),
    [
        ("", None),
        ("codex_native_unlisted_model: fallback", None),
        ("codex_native_unlisted_model: yes", "invalid setting value"),
        ("codex_native_unlisted_model: null", "invalid setting value"),
        ("codex_native_unlisted_model: [", "cannot parse user config as YAML"),
        ("[as_requested]", "user config must be a YAML mapping"),
        ("[]", "user config must be a YAML mapping"),
        ("null", "user config must be a YAML mapping"),
        (None, "cannot read user config"),
    ],
)
async def test_codex_terminal_unlisted_setting_uses_fallback(
    config_text: str | None,
    reason: str | None,
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    _no_configured_codex_app_server_command: Callable[[], tuple[str, ...]],
) -> None:
    """Invalid values and errors reading the setting at terminal launch log the fallback."""
    harness = codex_launch_harness
    path = global_config_path()
    path.parent.mkdir()
    if config_text is None:
        path.mkdir()  # The config path exists but cannot be read as a file.
    else:
        path.write_text(config_text)
    monkeypatch.setattr(codex_app, "resolve_native_codex_launch", _RESOLVE_NATIVE_CODEX_LAUNCH)
    monkeypatch.setattr(
        codex_app, "_configured_app_server_command", _no_configured_codex_app_server_command
    )
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])

    await harness.launch()

    assert harness.builds[0]["model"] == _PROVIDER_DEFAULT
    assert harness.resets == [{"expected_model_override": _RETIRED_PICK}]
    assert harness.unlisted_bindings == [None]
    assert harness.notices == [_unavailable_notice(_RETIRED_PICK, _PROVIDER_DEFAULT)]
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "omnigent.config"
        and "codex_native_unlisted_model" in record.getMessage()
    ]
    if reason is None:
        assert warnings == []
    else:
        (warning,) = warnings
        assert reason in warning
        assert "as_requested or fallback" in warning


@pytest.mark.asyncio
async def test_project_cannot_opt_in_to_unlisted_model_launches(
    codex_launch_harness: _LaunchHarness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = codex_launch_harness
    monkeypatch.chdir(tmp_path)
    project_config = tmp_path / ".omnigent" / "config.yaml"
    project_config.parent.mkdir()
    project_config.write_text("codex_native_unlisted_model: as_requested\n")
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])

    await harness.launch()

    assert "codex_native_unlisted_model" not in load_effective_config()
    assert harness.builds[0]["model"] == _PROVIDER_DEFAULT
    assert harness.notices == [_unavailable_notice(_RETIRED_PICK, _PROVIDER_DEFAULT)]


@pytest.mark.asyncio
@pytest.mark.usefixtures("unlisted_as_requested")
@pytest.mark.parametrize("agent_model", [None, "gpt-5.6-astra"])
# A router pin is never listed; a typo or a model the list lags looks the same.
@pytest.mark.parametrize("pick", [_RETIRED_PICK, "router-a/gpt-6.2-sol"])
async def test_unlisted_pick_launches_as_requested_with_a_notice(
    pick: str,
    agent_model: str | None,
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live list without the pick launches it unchanged, keeps it, and binds it as unlisted."""
    harness = codex_launch_harness
    assert isinstance(harness.agent_spec, AgentSpec)
    harness.snapshot["model_override"] = pick
    if agent_model is not None:
        harness.agent_spec.executor.config["model"] = agent_model
    harness.seed_catalog(
        [
            {"id": "gpt-5.6-sol", "isDefault": True},
            {"id": _PROVIDER_DEFAULT},
            {"id": "gpt-5.6-astra"},
        ]
    )

    loop = asyncio.get_running_loop()
    resolve_launch = codex_app.resolve_native_codex_launch
    loop_progressed: list[bool] = []

    def resolve_without_blocking_loop(**kwargs: Any) -> codex_app.NativeCodexLaunch:
        if kwargs.get("model") != pick:
            released = threading.Event()
            loop.call_soon_threadsafe(released.set)
            loop_progressed.append(released.wait(timeout=1.0))
        return resolve_launch(**kwargs)

    monkeypatch.setattr(codex_app, "resolve_native_codex_launch", resolve_without_blocking_loop)
    await harness.launch()

    assert loop_progressed and all(loop_progressed), "catalog routing blocked the event loop"
    assert harness.builds[0]["model"] == pick
    assert harness.resets == []
    assert harness.snapshot["model_override"] == pick
    # The server posts the notice when the started thread's binding names the pick.
    assert harness.unlisted_bindings == [pick]
    assert harness.notices == []
    harness.probe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("unlisted_as_requested")
async def test_unlisted_pick_also_pinned_by_the_spec_launches_as_requested(
    codex_launch_harness: _LaunchHarness,
) -> None:
    """`omnigent codex --model` pins one unlisted id in both places; that id launches."""
    harness = codex_launch_harness
    assert isinstance(harness.agent_spec, AgentSpec)
    harness.agent_spec.executor.config["model"] = _RETIRED_PICK
    harness.seed_catalog([{"id": "gpt-5.6-sol", "isDefault": True}, {"id": _PROVIDER_DEFAULT}])

    await harness.launch()

    assert harness.builds[0]["model"] == _RETIRED_PICK
    assert harness.resets == []
    assert harness.unlisted_bindings == [_RETIRED_PICK]


@pytest.mark.asyncio
async def test_spec_only_model_launches_unchecked(codex_launch_harness: _LaunchHarness) -> None:
    """Only a per-session pick is checked against the list; a spec's own model is not."""
    harness = codex_launch_harness
    assert isinstance(harness.agent_spec, AgentSpec)
    harness.snapshot["model_override"] = None
    harness.agent_spec.executor.config["model"] = _RETIRED_PICK
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])

    await harness.launch()

    assert harness.builds[0]["model"] == _RETIRED_PICK
    assert harness.resets == []
    assert harness.unlisted_bindings == [None]


@pytest.mark.asyncio
@pytest.mark.parametrize("as_requested", [False, True])
@pytest.mark.parametrize("refresh", ["hit", "miss", "empty", "error"])
async def test_stale_miss_awaits_reprobe_before_judging_the_pick(
    refresh: str, as_requested: bool, codex_launch_harness: _LaunchHarness
) -> None:
    """Only a live miss after the stale-list reload can apply the host's launch policy."""
    harness = codex_launch_harness
    if as_requested:
        _set_unlisted_mode("as_requested")
    pick = "databricks-gpt-5-6-sol"
    harness.snapshot["model_override"] = pick
    stale_rows = [{"id": "gpt-5.4-yesterday", "isDefault": True}]
    fingerprint = harness.seed_catalog(stale_rows, stale=True)
    fresh_rows = [{"id": _PROVIDER_DEFAULT, "isDefault": True}]
    if refresh == "hit":
        fresh_rows.append({"id": "gpt-5.6-sol"})
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def reprobe(**kwargs: Any) -> list[dict[str, Any]]:
        assert kwargs == {"codex_path": _CODEX_PATH, "launch": harness.catalog_shape()}
        probe_started.set()
        await release_probe.wait()
        if refresh == "error":
            raise RuntimeError("catalog temporarily unavailable")
        return [] if refresh == "empty" else fresh_rows

    harness.probe.side_effect = reprobe
    launch = asyncio.create_task(harness.launch())
    try:
        await asyncio.wait_for(probe_started.wait(), timeout=2.0)
        assert not launch.done(), "the launch must join the stale catalog's pending re-probe"
        assert harness.builds == []
        assert harness.resets == []
    finally:
        release_probe.set()
        await launch

    falls_back = refresh == "miss" and not as_requested
    assert harness.builds[0]["model"] == (_PROVIDER_DEFAULT if falls_back else pick)
    assert harness.resets == ([{"expected_model_override": pick}] if falls_back else [])
    assert harness.unlisted_bindings == ([pick] if refresh == "miss" and as_requested else [None])
    harness.probe.assert_awaited_once()
    assert model_catalog_store.read_catalog("codex-native", fingerprint) == (
        fresh_rows if refresh in {"hit", "miss"} else stale_rows
    )


@pytest.mark.asyncio
async def test_stale_catalog_hit_does_not_wait_for_background_refresh(
    codex_launch_harness: _LaunchHarness,
) -> None:
    """A still-served pick does not need to wait for an unrelated catalog refresh."""
    harness = codex_launch_harness
    harness.seed_catalog([{"id": harness.snapshot["model_override"]}], stale=True)
    release_probe = asyncio.Event()

    async def reprobe(**_kwargs: Any) -> list[dict[str, Any]]:
        await release_probe.wait()
        return [{"id": _PROVIDER_DEFAULT, "isDefault": True}]

    harness.probe.side_effect = reprobe
    try:
        await asyncio.wait_for(harness.launch(), timeout=2.0)
        assert harness.builds[0]["model"] == harness.snapshot["model_override"]
        assert harness.resets == []
    finally:
        release_probe.set()


@pytest.mark.asyncio
@pytest.mark.usefixtures("unlisted_as_requested")
@pytest.mark.parametrize("probe_fails", [False, True])
async def test_no_catalog_keeps_explicit_pick(
    probe_fails: bool, codex_launch_harness: _LaunchHarness
) -> None:
    """An absent catalog is not evidence that a requested model is unavailable."""
    harness = codex_launch_harness
    if probe_fails:
        harness.probe.side_effect = RuntimeError("catalog temporarily unavailable")

    await harness.launch()

    assert harness.builds[0]["model"] == harness.snapshot["model_override"]
    assert harness.resets == []
    harness.probe.assert_awaited_once_with(codex_path=_CODEX_PATH, launch=harness.catalog_shape())


@pytest.mark.asyncio
@pytest.mark.usefixtures("unlisted_as_requested")
@pytest.mark.parametrize("failure", ["app-server", "discovery", "terminal"])
async def test_failed_unlisted_launch_posts_no_notice(
    failure: str, codex_launch_harness: _LaunchHarness
) -> None:
    """An unlisted pick whose launch fails keeps the selection and posts no notice."""
    harness = codex_launch_harness
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])
    failing_call = {
        "app-server": harness.app_server.start,
        "discovery": harness.event_client.connect,
        "terminal": harness.registry.launch_auxiliary_terminal,
    }[failure]
    failing_call.side_effect = RuntimeError(f"{failure} launch failed")

    with pytest.raises(RuntimeError, match=f"{failure} launch failed"):
        await harness.launch()

    assert harness.builds[0]["model"] == _RETIRED_PICK
    assert harness.resets == []
    assert harness.notices == []
    assert harness.unlisted_bindings == []  # no thread binding was ever started
    if failure != "app-server":
        harness.app_server.close.assert_awaited_once()


def _bind_discovered_thread(
    monkeypatch: pytest.MonkeyPatch,
    *,
    thread_id: str | None,
    binding: Callable[[httpx.Request], httpx.Response],
) -> asyncio.Event:
    """Run the real thread discovery against a scripted thread start and binding server."""
    forwarding = asyncio.Event()

    async def wait_for_thread(*_args: object, **_kwargs: object) -> str:
        if thread_id is None:
            raise RuntimeError("event stream ended")
        return thread_id

    async def supervise_forwarder(**_kwargs: object) -> None:
        forwarding.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        runner_native, "_codex_discover_thread_and_forward", _DISCOVER_THREAD_AND_FORWARD
    )
    monkeypatch.setattr(codex_native_forwarder, "wait_for_thread_started", wait_for_thread)
    monkeypatch.setattr(codex_native_forwarder, "supervise_forwarder", supervise_forwarder)
    monkeypatch.setattr(
        "omnigent.cli_auth.open_server_client",
        lambda server_url, **_kwargs: httpx.AsyncClient(
            transport=httpx.MockTransport(binding), base_url=server_url
        ),
    )
    return forwarding


@pytest.mark.asyncio
@pytest.mark.usefixtures("unlisted_as_requested")
@pytest.mark.parametrize("thread_starts", [False, True])
async def test_fresh_unlisted_launch_names_its_pick_only_in_a_started_threads_binding(
    thread_starts: bool,
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TUI that never starts a thread reports that failure; a started one binds with the pick."""
    harness = codex_launch_harness
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])
    bridge_dir = codex_native_bridge.bridge_dir_for_bridge_id(_SESSION_ID)
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936d"
    bindings: list[dict[str, Any]] = []
    published_at_binding: list[str | None] = []

    def record_binding(request: httpx.Request) -> httpx.Response:
        state = codex_native_bridge.read_bridge_state(bridge_dir)
        published_at_binding.append(state.thread_id if state is not None else None)
        bindings.append(json.loads(request.content))
        return httpx.Response(200, json={})

    forwarding = _bind_discovered_thread(
        monkeypatch, thread_id=thread_id if thread_starts else None, binding=record_binding
    )

    resource = await harness.launch()
    discovery = runner_native._AUTO_FORWARDER_TASKS[_SESSION_ID]
    await asyncio.wait_for(forwarding.wait() if thread_starts else discovery, timeout=5)

    assert resource.id == "terminal_codex_main"
    assert harness.events.count("terminal-launch") == 1
    assert harness.builds[0]["model"] == _RETIRED_PICK
    assert harness.resets == []
    assert harness.notices == []  # a fresh launch leaves the notice to the server
    failure = codex_native_bridge.read_bridge_startup_failure(bridge_dir)
    if thread_starts:
        assert failure is None
        assert bindings == [{"external_session_id": thread_id, "unlisted_model": _RETIRED_PICK}]
        assert published_at_binding == [thread_id]
    else:
        assert failure is not None
        assert failure.code == "codex_thread_not_started"
        assert bindings == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("unlisted_as_requested")
@pytest.mark.parametrize("server", ["current", "older"])
async def test_unlisted_thread_binding_leaves_no_task_beside_forwarding(
    server: str,
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forwarding is the launch's only task and teardown awaits nothing else; old servers bind."""
    harness = codex_launch_harness
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936d"
    bindings: list[dict[str, Any]] = []

    def record_binding(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bindings.append(body)
        if server == "older" and "unlisted_model" in body:
            # A server whose request schema forbids the unknown field.
            return httpx.Response(422, json={"detail": [{"type": "extra_forbidden"}]})
        return httpx.Response(200, json={})

    forwarding = _bind_discovered_thread(monkeypatch, thread_id=thread_id, binding=record_binding)
    tasks_before = asyncio.all_tasks()

    await harness.launch()
    discovery = runner_native._AUTO_FORWARDER_TASKS[_SESSION_ID]
    await asyncio.wait_for(forwarding.wait(), timeout=5)

    unlisted_binding = {"external_session_id": thread_id, "unlisted_model": _RETIRED_PICK}
    assert bindings == (
        [unlisted_binding]
        if server == "current"
        else [unlisted_binding, {"external_session_id": thread_id}]
    )
    assert harness.notices == []
    assert asyncio.all_tasks() - tasks_before == {discovery}
    assert await runner_native._cancel_auto_forwarder_task(_SESSION_ID)
    assert discovery.done()
    assert asyncio.all_tasks() - tasks_before == set()


@pytest.mark.asyncio
@pytest.mark.usefixtures("unlisted_as_requested")
@pytest.mark.parametrize("wrapped_spec", [False, True])
@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("spec_serves_pick", [False, True])
async def test_spec_catalog_is_not_replaced_by_ambient_provider_catalog(
    wrapped_spec: bool,
    stale: bool,
    spec_serves_pick: bool,
    codex_launch_harness: _LaunchHarness,
    tmp_path: Path,
) -> None:
    """The agent's provider controls catalog lookup, freshness, and re-probing."""
    harness = codex_launch_harness
    if wrapped_spec:
        assert isinstance(harness.agent_spec, AgentSpec)
        harness.agent_spec = ResolvedSpec(spec=harness.agent_spec, workdir=tmp_path)
    pick = harness.snapshot["model_override"]
    ambient_model = _PROVIDER_DEFAULT if spec_serves_pick else pick
    harness.seed_catalog([{"id": ambient_model, "isDefault": True}], ambient=True)
    session_rows = [{"id": _PROVIDER_DEFAULT, "isDefault": True}]
    if spec_serves_pick:
        session_rows.append({"id": pick})
    fingerprint = harness.seed_catalog(session_rows, stale=stale)
    harness.probe.return_value = session_rows

    await harness.launch()

    assert harness.builds[0]["model"] == pick
    assert 'model_provider="session-provider"' in harness.builds[0]["extra_config_overrides"]
    assert harness.resets == []
    # Only the session provider's list decides whether the pick is unlisted.
    assert harness.unlisted_bindings == ([None] if spec_serves_pick else [pick])
    if stale:
        task = model_catalog_store._inflight.get(("codex-native", fingerprint))
        if task is not None:
            await task
        harness.probe.assert_awaited_once_with(
            codex_path=_CODEX_PATH, launch=harness.catalog_shape()
        )
    else:
        harness.probe.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("unlisted_as_requested")
async def test_unlisted_pick_keeps_its_resolved_model_config(
    codex_launch_harness: _LaunchHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unlisted pick keeps its resolved launch, including its embedded model config."""
    harness = codex_launch_harness
    pick = harness.snapshot["model_override"]

    def resolve_launch(
        *,
        model: str | None,
        spec: AgentSpec | None = None,
        terminal_launch_args: Sequence[str] = (),
    ) -> codex_app.NativeCodexLaunch:
        del spec, terminal_launch_args
        model = model or _PROVIDER_DEFAULT
        return codex_app.NativeCodexLaunch(
            config_overrides=[
                'model_provider="configured-provider"',
                'model_providers.configured-provider.base_url="https://provider.test/v1"',
                f"model={json.dumps(model)}",
            ],
            model=model,
            profile=None,
        )

    monkeypatch.setattr(codex_app, "resolve_native_codex_launch", resolve_launch)
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])
    harness.workspace_models.return_value = (f"system.ai.{pick}",)

    await harness.launch()

    assert harness.builds[0]["model"] == pick
    assert f'model="{pick}"' in harness.builds[0]["extra_config_overrides"]
    assert f'model="{_PROVIDER_DEFAULT}"' not in harness.builds[0]["extra_config_overrides"]
    assert harness.resets == []
    harness.workspace_models.assert_not_called()


@pytest.mark.asyncio
async def test_conditional_reset_still_propagates_cancellation(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Best-effort metadata handling must not swallow cancellation of its caller."""
    harness = codex_launch_harness
    monkeypatch.setattr(
        harness.server_client, "post", AsyncMock(side_effect=asyncio.CancelledError)
    )

    with pytest.raises(asyncio.CancelledError):
        await runner_native._clear_session_model_override(
            _SESSION_ID, harness.server_client, expected_model_override=_RETIRED_PICK
        )


@pytest.mark.asyncio
@pytest.mark.usefixtures("unlisted_as_requested")
@pytest.mark.parametrize("stale", [False, True])
async def test_subscription_unlisted_pick_launches_as_requested(
    stale: bool,
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Codex's own login an unlisted pick launches; only a fresh miss is named unlisted."""
    harness = codex_launch_harness

    def resolve_launch(
        *,
        model: str | None,
        spec: AgentSpec | None = None,
        terminal_launch_args: Sequence[str] = (),
    ) -> codex_app.NativeCodexLaunch:
        del spec, terminal_launch_args
        return codex_app.NativeCodexLaunch(
            config_overrides=['model_provider="openai"'], model=model, profile=None
        )

    monkeypatch.setattr(codex_app, "resolve_native_codex_launch", resolve_launch)
    harness.seed_catalog([{"id": "gpt-5.6-sol", "isDefault": True}], stale=stale)
    harness.probe.side_effect = RuntimeError("catalog temporarily unavailable")

    await harness.launch()

    assert harness.builds[0]["model"] == _RETIRED_PICK
    assert harness.builds[0]["profile"] is None
    assert 'model_provider="openai"' in harness.builds[0]["extra_config_overrides"]
    assert harness.resets == []
    assert harness.unlisted_bindings == ([None] if stale else [_RETIRED_PICK])


@pytest.mark.asyncio
@pytest.mark.usefixtures("unlisted_as_requested")
async def test_resumed_unlisted_launch_runs_the_pick_without_a_notice(
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known thread got its notice when it first started; resuming it posts none."""
    harness = codex_launch_harness
    harness.snapshot["external_session_id"] = "019e96aa-0be2-7343-8d3b-6f914d60936c"
    harness.seed_catalog([{"id": _PROVIDER_DEFAULT, "isDefault": True}])
    preload = AsyncMock()
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.main._ensure_local_codex_resume_rollout", AsyncMock()
    )
    monkeypatch.setattr(codex_app, "preload_codex_thread_for_resume", preload)
    monkeypatch.setattr(runner_native, "_codex_forward_known_thread", AsyncMock())

    resource = await harness.launch()

    assert resource.id == "terminal_codex_main"
    preload.assert_awaited_once()
    assert harness.builds[0]["model"] == _RETIRED_PICK
    assert harness.snapshot["model_override"] == _RETIRED_PICK
    assert harness.resets == []
    assert harness.unlisted_bindings == []
    assert harness.notices == []


_FORK_PICK = "system.ai.gpt-6-1-sol"


def _seed_fork_source(harness: _LaunchHarness, *, source_model: str, carry_history: bool) -> None:
    """Make the launch a fork onto ``_FORK_PICK`` of a same-host source rollout."""
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    harness.snapshot["model_override"] = _FORK_PICK
    harness.snapshot["labels"] = {
        "omnigent.fork.source_id": "conv_source",
        "omnigent.fork.source_external_session_id": source_thread,
        **({"omnigent.fork.carry_history": "1"} if carry_history else {}),
    }
    harness.seed_catalog([{"id": "gpt-6.1-sol", "isDefault": True}])
    source_home = codex_native_bridge.codex_home_for_bridge_dir(
        codex_native_bridge.bridge_dir_for_bridge_id("conv_source")
    )
    rollout_dir = source_home / "sessions" / "2026" / "10" / "15"
    rollout_dir.mkdir(parents=True)
    turn = {"turn_id": "turn_1", "cwd": "/repo", "model": source_model}
    (rollout_dir / f"rollout-2026-10-15T16-28-06-{source_thread}.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": source_thread, "cwd": "/repo"}})
        + "\n"
        + json.dumps({"type": "turn_context", "payload": turn})
        + "\n",
        encoding="utf-8",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_model", "carry_history", "rebuilt"),
    [
        ("system.ai.gpt-6-astra", True, True),
        ("gpt-6.1-sol", True, False),
        # Without the item rebuild, the clone is the only way to carry the history.
        ("system.ai.gpt-6-astra", False, False),
    ],
    ids=["source-on-other-model", "source-on-fork-model", "no-history-rebuild"],
)
async def test_fork_onto_another_model_rebuilds_history_instead_of_cloning(
    source_model: str,
    carry_history: bool,
    rebuilt: bool,
    codex_launch_harness: _LaunchHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fork whose model differs from the source's last turn is rebuilt from items."""
    harness = codex_launch_harness
    _seed_fork_source(harness, source_model=source_model, carry_history=carry_history)
    rebuild = AsyncMock(
        side_effect=lambda *_args, **kwargs: kwargs["codex_home"] / "rebuilt.jsonl"
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.main._ensure_local_codex_resume_rollout", rebuild
    )
    preload = AsyncMock()
    monkeypatch.setattr(codex_app, "preload_codex_thread_for_resume", preload)
    monkeypatch.setattr(runner_native, "_codex_forward_known_thread", AsyncMock())

    await harness.launch()

    assert harness.builds[0]["model"] == _FORK_PICK
    assert rebuild.await_count == (1 if rebuilt else 0)
    clone_home = harness.builds[0]["codex_home"]
    assert bool(list(clone_home.glob("sessions/**/rollout-*.jsonl"))) is not rebuilt
    preload.assert_awaited_once()


@pytest.mark.asyncio
async def test_fork_onto_another_model_fails_when_its_history_cannot_be_rebuilt(
    codex_launch_harness: _LaunchHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declined clone leaves only the rebuild, so a fresh launch would drop the history."""
    from omnigent.harnesses.codex_native.main import _CodexResumeHistoryUnavailableError

    harness = codex_launch_harness
    _seed_fork_source(harness, source_model="system.ai.gpt-6-astra", carry_history=True)
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.main._ensure_local_codex_resume_rollout",
        AsyncMock(side_effect=_CodexResumeHistoryUnavailableError("history returned 503")),
    )

    with pytest.raises(_CodexResumeHistoryUnavailableError, match="503"):
        await harness.launch()

    harness.registry.launch_auxiliary_terminal.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_message_does_not_reintroduce_retired_prelaunch_override(
    codex_launch_harness: _LaunchHarness,
) -> None:
    """An ordinary queued message keeps the working fallback even with an old snapshot."""
    from omnigent._wrapper_labels import CODEX_NATIVE_WRAPPER_VALUE, WRAPPER_LABEL_KEY
    from omnigent.entities.conversation import Conversation
    from omnigent.runtime.harnesses._scaffold import MessageEvent
    from omnigent.server.routes._sessions.orchestration import _build_native_terminal_message_event
    from omnigent.server.schemas import SessionEventInput

    harness = codex_launch_harness
    # Only a profile launch still retires an unlisted pick.
    harness.routing["databricks_profile"] = True
    harness.workspace_models.return_value = ("system.ai.gpt-5-6-sol",)
    stale_snapshot = Conversation(
        id=_SESSION_ID,
        created_at=0,
        updated_at=0,
        root_conversation_id=_SESSION_ID,
        agent_id="test-codex-agent",
        labels={WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE},
        model_override=harness.snapshot["model_override"],
    )

    await harness.launch()
    message = _build_native_terminal_message_event(
        stale_snapshot,
        SessionEventInput(
            type="message",
            data={"role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        ),
    )
    request = MessageEvent.model_validate(message).to_create_request()

    assert harness.builds[0]["model"] == "system.ai.gpt-5-6-sol"
    assert harness.resets == [{"expected_model_override": _RETIRED_PICK}]
    assert "model_override" not in message
    assert request.model_override is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pause_at", ["listing", "terminal"])
async def test_newer_pick_survives_reset_after_slow_fallback_launch(
    pause_at: str, codex_launch_harness: _LaunchHarness
) -> None:
    """Fallback only retires its original rejected pick, never a newer user selection."""
    harness = codex_launch_harness
    # Only a profile launch still retires an unlisted pick.
    harness.routing["databricks_profile"] = True
    new_pick = "gpt-5.6-sol"
    listing = ("system.ai.gpt-5-6-sol",)
    loop = asyncio.get_running_loop()
    paused = asyncio.Event()
    release = asyncio.Event()
    listing_release = threading.Event()

    def delayed_listing(*_args: Any) -> tuple[str, ...]:
        # The workspace listing runs in a worker thread.
        loop.call_soon_threadsafe(paused.set)
        listing_release.wait(timeout=5.0)
        return listing

    original_terminal_launch = harness.registry.launch_auxiliary_terminal.side_effect

    async def delayed_terminal(**kwargs: Any) -> SessionResourceView:
        paused.set()
        await release.wait()
        return await original_terminal_launch(**kwargs)

    if pause_at == "listing":
        harness.workspace_models.side_effect = delayed_listing
    else:
        harness.workspace_models.return_value = listing
        harness.registry.launch_auxiliary_terminal.side_effect = delayed_terminal
    launch = asyncio.create_task(harness.launch())
    try:
        await asyncio.wait_for(paused.wait(), timeout=2.0)
        assert not launch.done()
        harness.snapshot["model_override"] = new_pick
    finally:
        listing_release.set()
        release.set()
        await launch

    assert harness.snapshot["model_override"] == new_pick
    assert harness.resets == [{"expected_model_override": _RETIRED_PICK}]
    assert harness.builds[0]["model"] == "system.ai.gpt-5-6-sol"
