"""Permission leases bind a reviewed call to one execution and revision."""

import asyncio
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

import omnigent.permission_tiers
import omnigent.runner.permission_tiers
from omnigent.permission_tiers import PermissionTier
from omnigent.permission_tiers.host import HostPermissionState, write_host_permission_state
from omnigent.runner.mcp_execution_registry import McpExecutionRegistry
from omnigent.runner.mcp_routes import register_mcp_routes
from omnigent.runner.permission_tiers import PermissionTierRuntime
from omnigent.runner.session_init_protocol import (
    RunnerSessionInitEnvelope,
    RunnerSessionInitSnapshot,
)
from omnigent.server.permission_tiers import ConfirmedPermissionState


def _runtime(tmp_path: Path, tier: str) -> PermissionTierRuntime:
    runtime = PermissionTierRuntime()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = HostPermissionState(
        session_id="session-a",
        host_id="host-a",
        workspace=str(workspace),
        tier=tier,  # type: ignore[arg-type]
        revision=3,
        status="ready",
    )
    path = tmp_path / "bridge" / "permission_state.json"
    write_host_permission_state(path, state)
    runtime._states[state.session_id] = state
    runtime._paths[state.session_id] = path
    runtime._harnesses[state.session_id] = "antigravity-native"
    return runtime


def test_remote_tool_name_cannot_impersonate_local_write(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "auto")
    binding, decision = runtime.inspect(
        "session-a",
        call_id="call-a",
        name="sys_os_write",
        arguments={"path": str(tmp_path / "data.txt"), "content": "x"},
        expected_revision=3,
        local_execution=False,
    )
    assert decision.action.value == "DENY"
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
    )


def test_approval_requires_inspection_and_cannot_replay(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "ask")
    arguments = {"path": str(tmp_path / "data.txt"), "content": "x"}
    assert not runtime.authorize(
        "session-a",
        call_id="call-a",
        name="sys_os_write",
        arguments_digest="0" * 64,
        expected_revision=3,
    )
    binding, decision = runtime.inspect(
        "session-a",
        call_id="call-a",
        name="sys_os_write",
        arguments=arguments,
        expected_revision=3,
        local_execution=False,
    )
    assert decision.action.value == "ASK"
    assert runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
    )
    assert not runtime.idle("session-a")
    assert runtime.finish("session-a", call_id="call-a", revision=3)
    assert runtime.idle("session-a")
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
    )


def test_authorize_rechecks_path_after_inspection(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "auto")
    target = tmp_path / "workspace" / "target.txt"
    binding, decision = runtime.inspect(
        "session-a",
        call_id="call-path",
        name="sys_os_write",
        arguments={"path": str(target), "content": "x"},
        expected_revision=3,
        local_execution=True,
    )
    assert decision.action.value == "ALLOW"
    target.symlink_to("/etc/passwd")
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
    )


def test_installed_policy_and_guard_files_are_control_paths(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path, "ask")
    package_root = Path(omnigent.permission_tiers.__file__).resolve().parent.parent
    targets = (
        package_root / "permission_tiers" / "ares_rules.json",
        Path(omnigent.runner.permission_tiers.__file__).resolve(),
        package_root / "harnesses" / "antigravity_native" / "guard.py",
        package_root / "harnesses" / "antigravity_native" / "bridge.py",
    )
    for index, target in enumerate(targets):
        assert target.is_file()
        _binding, decision = runtime.inspect(
            "session-a",
            call_id=f"control-{index}",
            name="sys_os_write",
            arguments={"path": str(target), "content": "tamper"},
            expected_revision=3,
        )
        assert decision.action.value == "DENY"
        assert decision.rule_id == "control-path"


def test_pi_rpc_tempdir_is_protected_and_missing_record_denies(tmp_path: Path) -> None:
    from omnigent.harnesses.pi_native.bridge import PERMISSION_EXPECTED_FILE

    runtime = _runtime(tmp_path, "ask")
    runtime._harnesses["session-a"] = "pi"
    generated = tmp_path / "omnigent_pi_active"
    generated.mkdir()
    extension = generated / "omnigent_tools.js"
    extension.write_text("// generated test extension\n", encoding="utf-8")
    expected_path = runtime.path("session-a").parent / PERMISSION_EXPECTED_FILE
    expected_path.write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": "session-a",
                "harness": "pi",
                "extension_path": str(extension),
                "extension_sha256": hashlib.sha256(extension.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    binding, decision = runtime.inspect(
        "session-a",
        call_id="pi-control",
        name="sys_os_write",
        arguments={"path": str(generated / "models.json"), "content": "tamper"},
        expected_revision=3,
    )
    assert decision.action.value == "DENY"
    assert decision.rule_id == "control-path"
    expected_path.unlink()
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
    )
    with pytest.raises(OSError):
        runtime.inspect(
            "session-a",
            call_id="pi-missing",
            name="sys_os_write",
            arguments={"path": str(generated / "models.json"), "content": "tamper"},
            expected_revision=3,
        )


@pytest.mark.asyncio
async def test_full_mcp_call_runs_only_with_ready_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner import tool_dispatch

    runtime = _runtime(tmp_path, "full")
    runtime._harnesses["session-a"] = "pi"
    calls: list[str] = []

    async def execute_tool(**kwargs: object) -> str:
        calls.append(str(kwargs["tool_name"]))
        return "ran"

    async def none(_session_id: str) -> None:
        return None

    async def cwd(_session_id: str) -> Path:
        return tmp_path / "workspace"

    monkeypatch.setattr(tool_dispatch, "execute_tool", execute_tool)
    app = FastAPI()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        register_mcp_routes(
            app,
            _publish_event=lambda *_args: None,
            _recover_undrained_subagent_results=none,
            _resolve_conversation_id=none,  # type: ignore[arg-type]
            _resolve_session_agent_spec_or_none=none,
            _resolve_session_spec_entry=none,
            _session_agent_ids={},
            _session_async_tasks={},
            _session_harness_name=lambda _session_id: "pi",
            _session_inboxes={},
            _session_runtime_cwd=cwd,
            _session_spec_cache={},
            filesystem_registry=None,
            mcp_execution_registry=McpExecutionRegistry(),
            permission_tiers=runtime,
            mcp_manager=None,
            process_manager=None,
            resource_registry=None,  # type: ignore[arg-type]
            runner_workspace=None,
            server_client=client,
            spec_resolver=None,
            terminal_registry=None,
        )
        request = {"method": "tools/call", "params": {"name": "sys_os_read", "arguments": {}}}
        allowed = await client.post("/v1/sessions/session-a/mcp/execute", json=request)
        assert allowed.json() == {"result": {"output": "ran"}}
        runtime.force_block("session-a", "transaction-a")
        denied = await client.post("/v1/sessions/session-a/mcp/execute", json=request)
        assert denied.json()["error"]["message"] == "Permission tier unavailable"
    assert calls == ["sys_os_read"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raises", [False, True])
@pytest.mark.parametrize("with_operation", [False, True])
async def test_mcp_lease_finishes_in_actual_execution_callback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raises: bool, with_operation: bool
) -> None:
    from omnigent.runner import tool_dispatch

    runtime = _runtime(tmp_path, "ask")
    arguments: dict[str, object] = {}
    binding, decision = runtime.inspect(
        "session-a",
        call_id="tool-a",
        name="sys_os_read",
        arguments=arguments,
        expected_revision=3,
        local_execution=False,
    )
    assert decision.action.value == "ASK"
    assert runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
    )
    calls = 0

    async def execute_tool(**_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        if raises:
            raise RuntimeError("tool failed")
        return "ran"

    async def none(_session_id: str) -> None:
        return None

    async def cwd(_session_id: str) -> Path:
        return tmp_path / "workspace"

    monkeypatch.setattr(tool_dispatch, "execute_tool", execute_tool)
    app = FastAPI()
    registry = McpExecutionRegistry()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        register_mcp_routes(
            app,
            _publish_event=lambda *_args: None,
            _recover_undrained_subagent_results=none,
            _resolve_conversation_id=none,  # type: ignore[arg-type]
            _resolve_session_agent_spec_or_none=none,
            _resolve_session_spec_entry=none,
            _session_agent_ids={},
            _session_async_tasks={},
            _session_harness_name=lambda _session_id: "antigravity-native",
            _session_inboxes={},
            _session_runtime_cwd=cwd,
            _session_spec_cache={},
            filesystem_registry=None,
            mcp_execution_registry=registry,
            permission_tiers=runtime,
            mcp_manager=None,
            process_manager=None,
            resource_registry=None,  # type: ignore[arg-type]
            runner_workspace=None,
            server_client=client,
            spec_resolver=None,
            terminal_registry=None,
        )
        request = {
            "method": "tools/call",
            "params": {"name": "sys_os_read", "arguments": arguments},
            "_omnigent_permission": {
                "call_id": binding.call_id,
                "revision": 3,
                "arguments_digest": binding.arguments_digest,
            },
        }
        if with_operation:
            request["_omnigent_operation"] = {"id": "operation-a", "step": "initial"}
        else:
            malformed = {**request, "_omnigent_permission": "invalid"}
            rejected = await client.post("/v1/sessions/session-a/mcp/execute", json=malformed)
            assert rejected.json()["error"]["message"] == "Permission lease rejected"
            forged = {
                **request,
                "_omnigent_permission": {
                    **request["_omnigent_permission"],
                    "arguments_digest": "0" * 64,
                },
            }
            denied = await client.post("/v1/sessions/session-a/mcp/execute", json=forged)
            assert denied.json()["error"]["message"] == "Permission lease rejected"
            assert calls == 0
            assert not runtime.idle("session-a")
        response = await client.post("/v1/sessions/session-a/mcp/execute", json=request)
        assert ("error" in response.json()) is raises
        assert runtime.idle("session-a")
        if with_operation:
            replay = await client.post("/v1/sessions/session-a/mcp/execute", json=request)
            assert replay.json() == response.json()
        assert calls == 1


@pytest.mark.asyncio
async def test_mcp_lease_survives_detached_request_until_tool_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner import tool_dispatch

    runtime = _runtime(tmp_path, "ask")
    binding, decision = runtime.inspect(
        "session-a",
        call_id="detached-a",
        name="sys_os_read",
        arguments={},
        expected_revision=3,
        local_execution=False,
    )
    assert decision.action.value == "ASK"
    assert runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
    )
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def execute_tool(**_kwargs: object) -> str:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return "ran"

    async def none(_session_id: str) -> None:
        return None

    async def cwd(_session_id: str) -> Path:
        return tmp_path / "workspace"

    monkeypatch.setattr(tool_dispatch, "execute_tool", execute_tool)
    app = FastAPI()
    registry = McpExecutionRegistry()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        register_mcp_routes(
            app,
            _publish_event=lambda *_args: None,
            _recover_undrained_subagent_results=none,
            _resolve_conversation_id=none,  # type: ignore[arg-type]
            _resolve_session_agent_spec_or_none=none,
            _resolve_session_spec_entry=none,
            _session_agent_ids={},
            _session_async_tasks={},
            _session_harness_name=lambda _session_id: "antigravity-native",
            _session_inboxes={},
            _session_runtime_cwd=cwd,
            _session_spec_cache={},
            filesystem_registry=None,
            mcp_execution_registry=registry,
            permission_tiers=runtime,
            mcp_manager=None,
            process_manager=None,
            resource_registry=None,  # type: ignore[arg-type]
            runner_workspace=None,
            server_client=client,
            spec_resolver=None,
            terminal_registry=None,
        )
        request = {
            "method": "tools/call",
            "params": {"name": "sys_os_read", "arguments": {}},
            "_omnigent_operation": {"id": "operation-detached", "step": "initial"},
            "_omnigent_permission": {
                "call_id": binding.call_id,
                "revision": 3,
                "arguments_digest": binding.arguments_digest,
            },
        }
        pending = asyncio.create_task(
            client.post("/v1/sessions/session-a/mcp/execute", json=request)
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not runtime.idle("session-a")
        release.set()
        retained = registry._entries[("session-a", "operation-detached", "initial")]
        await asyncio.wait_for(retained.task, timeout=2)
        assert runtime.idle("session-a")
        replay = await client.post("/v1/sessions/session-a/mcp/execute", json=request)
        assert replay.json() == {"result": {"output": "ran"}}
        assert calls == 1


@pytest.mark.asyncio
async def test_runner_factory_advertises_protocol_on_native_bootstrap_get(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runner.app import create_runner_app_from_env
    from omnigent.runner.native.orchestration import _fetch_native_launch_snapshot

    received: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request.headers.get("X-Omnigent-Permission-Tiers"))
        return httpx.Response(200, json={"id": "session-a"})

    original_client = httpx.AsyncClient
    clients: list[httpx.AsyncClient] = []

    def make_client(**kwargs: object) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handle)
        client = original_client(**kwargs)
        clients.append(client)
        return client

    monkeypatch.setenv("RUNNER_SERVER_URL", "http://server")
    monkeypatch.setattr(httpx, "AsyncClient", make_client)
    create_runner_app_from_env()
    assert len(clients) == 1
    try:
        snapshot = await _fetch_native_launch_snapshot(
            server_client=clients[0], session_id="session-a", runtime_label="Pi"
        )
        assert snapshot["id"] == "session-a"
        assert received == ["1"]
    finally:
        await clients[0].aclose()


@pytest.mark.asyncio
async def test_canonical_server_client_advertises_protocol_on_native_bootstrap_get() -> None:
    from omnigent.cli_auth import open_server_client
    from omnigent.runner.native.orchestration import _fetch_native_launch_snapshot

    received: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        received.append(request.headers.get("X-Omnigent-Permission-Tiers"))
        return httpx.Response(200, json={"id": "session-a"})

    async with open_server_client(
        "http://127.0.0.1:6767", transport=httpx.MockTransport(handle)
    ) as client:
        snapshot = await _fetch_native_launch_snapshot(
            server_client=client, session_id="session-a", runtime_label="Pi"
        )
    assert snapshot["id"] == "session-a"
    assert received == ["1"]


def test_full_install_skips_rules_but_auto_requires_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = PermissionTierRuntime()
    state_path = tmp_path / "bridge" / "permission_state.json"
    monkeypatch.setattr(
        PermissionTierRuntime,
        "state_path",
        staticmethod(lambda _session_id, _harness, _labels: state_path),
    )
    monkeypatch.setattr(
        PermissionTierRuntime,
        "_verify_installation",
        staticmethod(lambda _path, _harness, _tier: None),
    )

    def corrupt_rules() -> None:
        raise ValueError("invalid test rules")

    monkeypatch.setattr(omnigent.runner.permission_tiers, "load_rules", corrupt_rules)

    def envelope(tier: PermissionTier) -> RunnerSessionInitEnvelope:
        return RunnerSessionInitEnvelope(
            protocol_version=2,
            server_version="test",
            session_id="session-a",
            agent_id="agent-a",
            snapshot=RunnerSessionInitSnapshot(
                created_at=1,
                updated_at=1,
                host_id="host-a",
                workspace=str(tmp_path / "workspace"),
                labels=ConfirmedPermissionState(tier, 1, "blocked").labels("antigravity-native"),
            ),
        )

    assert runtime.install(envelope("full"), "antigravity-native") is not None
    with pytest.raises(ValueError, match="invalid test rules"):
        runtime.install(envelope("auto"), "antigravity-native")
