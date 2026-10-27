import json
from types import SimpleNamespace

import httpx
import pytest

from omnigent import startup_observation as observation
from omnigent.runner.tool_dispatch import _session_startup_observation_v1_via_rest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,value",
    [
        ("permission_tier_state", "blocked"),
        ("permission_tier_state", None),
        ("permission_tier_state", False),
        ("permission_tier_revision", 4),
        ("permission_tier_revision", None),
        ("permission_tier_revision", "__missing__"),
        ("permission_tiers_capability", False),
        ("permission_tiers_capability", None),
        ("permission_tiers_capability", "__missing__"),
    ],
)
async def test_real_confirmation_timeout_preserves_each_comparand(monkeypatch, field, value):
    snapshot = {
        "permission_tier_state": "ready",
        "permission_tier_revision": 3,
        "permission_tiers_capability": True,
    }
    if value == "__missing__":
        del snapshot[field]
    else:
        snapshot[field] = value
    times = iter([0.0, 20.0])
    monkeypatch.setattr(observation, "_now", lambda: next(times))

    async def handler(request):
        assert request.method == "GET"
        assert request.headers["X-Omnigent-Permission-Tiers"] == "1"
        return httpx.Response(200, json={**snapshot, "labels": {"do_not_copy": "sentinel"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        with pytest.raises(RuntimeError) as failure:
            await observation.wait_for_permission_confirmation(
                client, "/v1/sessions/s", 3, {"session_id": "s"}
            )
    detail = str(failure.value)
    assert "sentinel" not in detail and "labels" not in detail
    packet = json.loads(detail.split("; ", 1)[1])
    assert packet["comparands"]["expected_runner_revision"] == 3
    assert (
        packet["comparands"][
            {
                "permission_tier_state": "ready_matches",
                "permission_tier_revision": "revision_matches",
                "permission_tiers_capability": "capability_matches",
            }[field]
        ]
        is False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 426, 503])
async def test_real_confirmation_non200_reports_unknown_comparands(monkeypatch, status):
    times = iter([0.0, 20.0])
    monkeypatch.setattr(observation, "_now", lambda: next(times))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="not a snapshot")
        ),
        base_url="http://test",
    ) as client:
        with pytest.raises(RuntimeError) as failure:
            await observation.wait_for_permission_confirmation(client, "/v1/sessions/s", 3, {})
    packet = json.loads(str(failure.value).split("; ", 1)[1])
    assert packet["http_status"] == status
    assert packet["comparands"]["ready_matches"] is None


@pytest.mark.asyncio
async def test_exact_deadline_matching_response_keeps_existing_admission_order(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(observation, "_now", lambda: clock[0])
    snapshot = {
        "permission_tier_state": "ready",
        "permission_tier_revision": 3,
        "permission_tiers_capability": True,
    }

    async def handler(request):
        clock[0] = 20.0
        return httpx.Response(200, json=snapshot)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        assert (
            await observation.wait_for_permission_confirmation(client, "/v1/sessions/s", 3, {})
            is None
        )


@pytest.mark.asyncio
async def test_polling_helper_preserves_offline_exception():
    async def handler(request):
        raise httpx.ConnectError("fixture offline")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        with pytest.raises(httpx.ConnectError):
            await observation.wait_for_permission_confirmation(client, "/v1/sessions/s", 3, {})


@pytest.mark.asyncio
async def test_new_adapter_legacy_route_table_receives_options_only():
    """Real adapter + ASGI path/method routing; not a full historical-server acceptance test."""
    from fastapi import FastAPI

    app = FastAPI()
    counts = {"ordinary_get": 0, "cache_refresh": 0}
    requests = []

    @app.get("/v1/sessions/{session_id}")
    async def ordinary_detail(session_id: str):
        counts["ordinary_get"] += 1
        counts["cache_refresh"] += 1
        return {"id": session_id}

    @app.middleware("http")
    async def record(request, call_next):
        requests.append((request.method, request.url.path))
        return await call_next(request)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://server"
    ) as client:
        packet = json.loads(await _session_startup_observation_v1_via_rest({}, "s", client))
    assert packet["startup_observation"]["reason"] == "server_support_unknown"
    assert counts == {"ordinary_get": 0, "cache_refresh": 0}
    assert requests == [("OPTIONS", "/v1/sessions/s/startup-observation/v1")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["missing_header", "wrong_version", "invalid_json", "redirect", "old_404"]
)
async def test_unknown_server_support_never_sends_get(kind):
    calls = []

    async def handler(request):
        calls.append(request)
        assert request.method == "OPTIONS"
        assert not request.url.query and "include_preview" not in str(request.url)
        if kind == "old_404":
            return httpx.Response(404)
        if kind == "redirect":
            return httpx.Response(307, headers={"Location": "/v1/sessions/s"})
        headers = {} if kind == "missing_header" else {"X-Omnigent-Startup-Observation": "1"}
        if kind == "invalid_json":
            return httpx.Response(200, headers=headers, text="ordinary HTML")
        return httpx.Response(
            200,
            headers=headers,
            json={
                "protocol": "permission_startup_observation",
                "version": 0 if kind == "wrong_version" else 1,
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server", follow_redirects=True
    ) as client:
        packet = json.loads(await _session_startup_observation_v1_via_rest({}, "s", client))
    assert len(calls) == 1 and packet["startup_observation"]["admission"] == "not_evaluated"
    assert packet["startup_observation"]["reason"] == "server_support_unknown"


@pytest.mark.asyncio
async def test_supported_registered_server_v1_negotiates_before_get():
    calls = []

    async def handler(request):
        calls.append((request.method, request.url.path))
        assert request.url.path == "/v1/sessions/s/startup-observation/v1"
        assert not request.url.query
        assert request.headers["X-Omnigent-Permission-Tiers"] == "1"
        if request.method == "OPTIONS":
            return httpx.Response(
                200,
                headers={"X-Omnigent-Startup-Observation": "1"},
                json={"protocol": "permission_startup_observation", "version": 1},
            )
        assert request.method == "GET" and len(calls) == 2
        return httpx.Response(
            200,
            json={
                "id": "s",
                "startup_observation": {
                    "schema_version": 1,
                    "status": "unknown",
                    "admission": "not_evaluated",
                },
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        packet = json.loads(await _session_startup_observation_v1_via_rest({}, "s", client))
    assert [x[0] for x in calls] == ["OPTIONS", "GET"]
    assert packet["startup_observation"]["status"] == "unknown"


def _projection_case(monkeypatch, scenario, default_harness):
    from omnigent.server import startup_observation as server_observation
    from omnigent.server.permission_tiers import (
        PERMISSION_CAPABILITY_LABEL_KEY,
        PERMISSION_CAPABILITY_VALUE,
        PERMISSION_RUNTIME_ID_LABEL_KEY,
        ConfirmedPermissionState,
    )
    from omnigent.startup_observation import OBSERVATION_CAPABILITY

    labels = ConfirmedPermissionState("ask", 3, "ready").labels("pi")
    labels.update(
        {
            PERMISSION_CAPABILITY_LABEL_KEY: PERMISSION_CAPABILITY_VALUE,
            PERMISSION_RUNTIME_ID_LABEL_KEY: "process-a",
        }
    )
    conv = SimpleNamespace(
        id="s",
        agent_id="agent-a",
        runner_id="runner-a",
        host_id="host-a",
        workspace="/opt/work/project",
        worktree=None,
        model_override=None,
        harness_override=None if default_harness else "pi",
        reasoning_effort="high",
        archived=False,
        archive_revision=0,
        labels=labels,
    )
    capabilities = (
        [] if scenario in ("old_runner", "unknown_capability") else [OBSERVATION_CAPABILITY]
    )
    if scenario == "unknown_capability":
        capabilities = ["permission_startup_observation_v0"]
    connection = SimpleNamespace(generation=7, hello=SimpleNamespace(capabilities=capabilities))

    class Registry:
        def __init__(self):
            self.calls = 0
            self.current = None if scenario == "offline" else connection

        def get(self, rid):
            self.calls += 1
            if scenario in ("router_disconnect", "wrong_replica") and self.calls >= 2:
                self.current = None
            return self.current

    registry = Registry()
    store = SimpleNamespace(get_conversation=lambda sid: conv)
    monkeypatch.setattr(
        server_observation,
        "_LOADED_BUILD",
        {"commit_sha": "2" * 40, "build_time_epoch": 456, "version": "0.1.0"},
    )
    payload = {
        "schema_version": 1,
        "binding": observation.context(conv, session_id="s", agent_id="agent-a"),
        "harness": "pi",
        "expected_runner_revision": 3,
        "init_fresh": True,
        "state_context_matches": True,
        "permission_runtime_fingerprint": observation.runtime_fingerprint("process-a"),
        "loaded_build": {"commit_sha": "1" * 40, "build_time_epoch": 123, "version": "0.1.0"},
        "labels": {"secret_sentinel": "MUST_NOT_ESCAPE"},
    }
    if scenario == "stale_incarnation":
        payload["permission_runtime_fingerprint"] = "other-process"
    if scenario == "stale_init":
        payload["init_fresh"] = False
    if scenario == "missing_expected":
        payload["expected_runner_revision"] = None
    if scenario == "missing_build":
        payload["loaded_build"] = None
    return conv, connection, registry, store, payload


@pytest.mark.asyncio
@pytest.mark.parametrize("default_harness", [False, True])
@pytest.mark.parametrize(
    "scenario,reason",
    [
        ("normal", "comparands_observed"),
        ("offline", "bound_runner_not_connected_here"),
        ("old_runner", "runner_observation_not_advertised"),
        ("unknown_capability", "runner_observation_not_advertised"),
        ("timeout", "runner_observation_timeout"),
        ("unsupported_payload", "runner_observation_unsupported"),
        ("router_disconnect", "bound_runner_route_unavailable"),
        ("wrong_replica", "bound_runner_route_unavailable"),
        ("stale_binding", "binding_or_connection_changed"),
        ("stale_connection", "binding_or_connection_changed"),
        ("stale_incarnation", "runner_context_incomplete_or_stale"),
        ("stale_init", "runner_context_incomplete_or_stale"),
        ("missing_expected", "runner_context_incomplete_or_stale"),
        ("missing_build", "loaded_build_unknown"),
    ],
)
async def test_real_router_projection_boundaries(monkeypatch, scenario, reason, default_harness):
    from omnigent.runner.routing import RunnerRouter
    from omnigent.server.startup_observation import read_startup_observation

    conv, _connection, registry, store, payload = _projection_case(
        monkeypatch, scenario, default_harness
    )
    contacts = {"diagnostic": 0, "ordinary": 0, "cache_refresh": 0}

    async def handler(request):
        if request.url.path == "/v1/sessions/s":
            contacts["ordinary"] += 1
            contacts["cache_refresh"] += 1
            raise AssertionError("ordinary fallback forbidden")
        contacts["diagnostic"] += 1
        assert (
            request.method == "GET" and request.url.path == "/v1/sessions/s/startup-observation/v1"
        )
        assert not request.url.query and request.extensions["runner_tunnel_generation"] == 7
        if scenario == "timeout":
            raise httpx.ReadTimeout("fixture timeout")
        if scenario == "stale_binding":
            conv.runner_id = "runner-b"
        if scenario == "stale_connection":
            registry.current = SimpleNamespace(generation=8)
        return httpx.Response(
            200,
            json={"id": "s"}
            if scenario == "unsupported_payload"
            else {"startup_observation": payload},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        # Actual routing implementation runs; only its eventual HTTP transport is controlled.
        router = RunnerRouter(
            registry=registry,
            conversation_store=store,
            host_registry=SimpleNamespace(get=lambda hid: None)
            if scenario == "wrong_replica"
            else None,
            host_store=SimpleNamespace(is_online=lambda hid: True)
            if scenario == "wrong_replica"
            else None,
        )
        monkeypatch.setattr(router, "_client_for_runner", lambda rid: client)
        result = await read_startup_observation(
            conv, harness=conv.harness_override, router=router, registry=registry, store=store
        )
    assert result["reason"] == reason and result["admission"] == "not_evaluated"
    assert contacts["ordinary"] == contacts["cache_refresh"] == 0
    no_contact = scenario in (
        "offline",
        "old_runner",
        "unknown_capability",
        "router_disconnect",
        "wrong_replica",
    )
    assert contacts["diagnostic"] == (0 if no_contact else 1)
    if scenario in ("router_disconnect", "wrong_replica"):
        assert registry.calls == 2
    if default_harness and scenario in (
        "offline",
        "old_runner",
        "unknown_capability",
        "timeout",
        "unsupported_payload",
    ):
        assert all(v is None for v in result["comparands"].values())
    assert result["status"] == ("observed" if scenario == "normal" else "unknown")
    assert "MUST_NOT_ESCAPE" not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["forbidden", "unauthorized", "not_found", "conflict", "internal_error"]
)
async def test_non_offline_application_errors_are_preserved(monkeypatch, code):
    from omnigent.errors import OmnigentError
    from omnigent.runner.routing import RunnerRouter
    from omnigent.server.startup_observation import read_startup_observation

    conv, _connection, registry, store, _payload = _projection_case(monkeypatch, "normal", True)
    router = RunnerRouter(registry=registry, conversation_store=store)

    def refuse(*args, **kwargs):
        raise OmnigentError("fixture refusal", code=code)

    monkeypatch.setattr(router, "client_for_session_resources", refuse)
    with pytest.raises(OmnigentError) as failure:
        await read_startup_observation(
            conv, harness=None, router=router, registry=registry, store=store
        )
    assert failure.value.code == code


@pytest.mark.asyncio
async def test_corrupt_default_harness_state_is_unknown_after_supported_read(monkeypatch):
    from omnigent.runner.routing import RunnerRouter
    from omnigent.server.permission_tiers import PERMISSION_STATE_LABEL_KEY
    from omnigent.server.startup_observation import read_startup_observation

    conv, _connection, registry, store, payload = _projection_case(monkeypatch, "normal", True)
    conv.labels[PERMISSION_STATE_LABEL_KEY] = "invalid-json"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"startup_observation": payload})
        ),
        base_url="http://runner",
    ) as client:
        router = RunnerRouter(registry=registry, conversation_store=store)
        monkeypatch.setattr(router, "_client_for_runner", lambda rid: client)
        result = await read_startup_observation(
            conv, harness=None, router=router, registry=registry, store=store
        )
    assert result["reason"] == "server_state_unknown" and result["status"] == "unknown"
    assert result["comparands"]["expected_runner_revision"] == 3
    assert result["comparands"]["server_state"] is None
    assert result["comparands"]["capability"] is None


@pytest.mark.asyncio
async def test_real_runner_registration_auth_and_hello_capability():
    from fastapi import FastAPI

    from omnigent.runner.app import create_runner_app
    from omnigent.runner.transports.ws_tunnel.serve import _send_hello
    from omnigent.startup_observation import (
        OBSERVATION_CAPABILITY,
        registered_observation_capabilities,
    )

    callbacks = []

    async def unexpected(request):
        callbacks.append(request)
        raise AssertionError("diagnostic must not refill ordinary cache or call server")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected), base_url="http://server"
    ) as server:
        app = create_runner_app(server_client=server, auth_token="fixture-only-token")
        assert registered_observation_capabilities(app) == [OBSERVATION_CAPABILITY]
        assert registered_observation_capabilities(FastAPI()) == []
        frames = []

        async def send(text):
            frames.append(json.loads(text))

        await _send_hello(send, "0.0.0", app=app)
        assert OBSERVATION_CAPABILITY in frames[0]["capabilities"]
        frames.clear()
        await _send_hello(send, "999.0.0", app=FastAPI())
        assert OBSERVATION_CAPABILITY not in frames[0]["capabilities"]
        before = dict(app.state.permission_tiers._states)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            path = "/v1/sessions/s/startup-observation/v1"
            assert (await client.get(path)).status_code == 401
            # Removed independent runner path has no alias or ordinary GET fallback.
            legacy = await client.get(
                "/v1/session-startup-observations/v1/s",
                headers={"Authorization": "Bearer fixture-only-token"},
            )
            assert legacy.status_code == 404
            response = await client.get(
                path, headers={"Authorization": "Bearer fixture-only-token"}
            )
            packet = response.json()["startup_observation"]
            assert packet["expected_runner_revision"] is None and packet["init_fresh"] is False
        assert app.state.permission_tiers._states == before
    assert callbacks == []


def test_dedicated_tool_registration_and_no_legacy_flag():
    from omnigent.runner.tool_dispatch import should_dispatch_locally
    from omnigent.tools.builtins.spawn import (
        SysSessionGetInfoTool,
        SysSessionGetStartupObservationV1Tool,
    )

    assert should_dispatch_locally("sys_session_get_startup_observation_v1") is True
    assert SysSessionGetStartupObservationV1Tool.name() == "sys_session_get_startup_observation_v1"
    properties = SysSessionGetInfoTool().get_schema()["function"]["parameters"]["properties"]
    assert "startup_observation_only" not in properties
    # Old adapter has no such registered builtin: that limitation is source-proven,
    # not replaced here by a fake old dispatcher or a forced internal helper call.
