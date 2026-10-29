"""Permission leases bind a reviewed call to one execution and revision."""

import asyncio
import hashlib
import json
import shutil
import sys
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


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", [True, False])
async def test_first_pi_turn_waits_for_permission_bootstrap(monkeypatch, installed, caplog):
    from tests.runner.conftest import _build_app_for_spec, _runner_client
    from tests.runner.native_helpers import _harness_spec

    app, pm = await _build_app_for_spec(_harness_spec("pi"))
    harness = pm._client
    entered, release = asyncio.Event(), asyncio.Event()
    original_post = harness.post

    async def bootstrap(url, **kwargs):
        if url == "/v1/permission-tier/bootstrap":
            entered.set()
            await release.wait()
            return httpx.Response(200 if installed else 503, json={"installed": installed})
        return await original_post(url, **kwargs)

    monkeypatch.setattr(harness, "post", bootstrap)
    async with _runner_client(app, permission_harness="pi") as client:
        init = asyncio.create_task(
            client.post("/v1/sessions", json={"session_id": "cold-pi", "agent_id": "test-agent"})
        )
        await asyncio.wait_for(entered.wait(), 5)
        try:
            response = await client.post(
                "/v1/sessions/cold-pi/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "test-agent",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
            )
            assert response.status_code == 202, response.text
            turn = app.state.active_turns.get("cold-pi")
            assert turn is not None
            done, _ = await asyncio.wait([turn], timeout=0.05)
            assert not done, "first turn failed while permission bootstrap was still running"
            assert app.state.permission_tiers.state("cold-pi").status == "blocked"
        finally:
            release.set()
            initialized = await asyncio.wait_for(init, 5)
        await asyncio.wait_for(turn, 5)
    assert initialized.status_code == (201 if installed else 503)
    assert "Permission tier is unavailable: blocked" not in caplog.text
    if installed:
        assert harness.posted_bodies
        assert "turn setup failed" not in caplog.text
    else:
        assert not harness.posted_bodies
        assert "permission tier startup failed" in caplog.text


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


def _extension_runtime(
    tmp_path: Path, tier: str, scope: str = "user", name: str = "google_search"
):
    from omnigent.harnesses.pi_native.bridge import prepare_permission_installation

    runtime = _runtime(tmp_path, tier)
    runtime._harnesses["session-a"] = "pi-native"
    bridge = runtime.path("session-a").parent
    guard = bridge / "omnigent_pi_native_extension.js"
    guard.write_text("// guard\n")
    prepare_permission_installation(
        bridge, session_id="session-a", harness="pi-native", extension=guard
    )
    expected = json.loads((bridge / "permission_expected.json").read_text())
    package = tmp_path / "user-package"
    package.mkdir()
    (package / "package.json").write_text("{}")
    source = package / "index.js"
    source.write_text("// user extension\n")
    config = tmp_path / "user-config"
    config.mkdir()
    registry = {
        **{
            key: expected[key]
            for key in ("version", "installation_id", "session_id", "harness", "extension_sha256")
        },
        "tools": [
            {
                "name": name,
                "sourceInfo": {
                    "path": str(source),
                    "source": "npm:sample-extension",
                    "scope": scope,
                    "origin": "package",
                    "baseDir": str(config),
                },
                "real_path": str(source.resolve()),
                "real_base_dir": str(config.resolve()),
                "implementation_root": str(package.resolve()),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ],
    }
    (bridge / "permission_tools.json").write_text(json.dumps(registry))
    return runtime, source, registry


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("ask", "ASK"),
        ("plan", "DENY"),
        ("accept-edits", "ASK"),
        ("auto", "ALLOW"),
        ("full", "ALLOW"),
    ],
)
@pytest.mark.parametrize("name", ["google_search", "read", "bash"])
def test_user_extension_tools_follow_tier_not_builtin_name(tmp_path, tier, expected, name):
    runtime, _source, _registry = _extension_runtime(tmp_path, tier, name=name)
    _binding, decision = runtime.inspect(
        "session-a",
        call_id="extension",
        name=name,
        arguments={"query": "release notes"},
        expected_revision=3,
    )
    assert decision.action.value == expected, decision.reason


@pytest.mark.parametrize("scope", ["project", "temporary", "unknown"])
def test_extension_scope_cannot_claim_builtin_read_permissions(tmp_path, scope):
    runtime, _source, _registry = _extension_runtime(tmp_path, "auto", scope, "read")
    _binding, decision = runtime.inspect(
        "session-a",
        call_id="collision",
        name="read",
        arguments={"path": "notes.txt"},
        expected_revision=3,
    )
    assert decision.action.value == "DENY"
    assert "extension" in decision.reason.lower()


@pytest.mark.parametrize(
    "fault", ["missing", "stale", "changed-source", "symlink-source", "symlink-registry"]
)
@pytest.mark.parametrize("tier", ["auto", "ask", "accept-edits"])
def test_extension_registry_fails_closed_and_rechecks_authorization(tmp_path, fault, tier):
    runtime, source, registry = _extension_runtime(tmp_path, tier)
    binding, decision = runtime.inspect(
        "session-a", call_id="extension", name="google_search", arguments={}, expected_revision=3
    )
    assert decision.action.value == ("ALLOW" if tier == "auto" else "ASK"), decision.reason
    path = runtime.path("session-a").parent / "permission_tools.json"
    if fault == "missing":
        path.unlink()
    elif fault == "stale":
        registry["installation_id"] = "old-installation"
        path.write_text(json.dumps(registry))
    elif fault == "changed-source":
        source.write_text("// modified since loading\n")
    elif fault == "symlink-source":
        other = tmp_path / "other.js"
        other.write_text(source.read_text())
        source.unlink()
        source.symlink_to(other)
    else:
        other = tmp_path / "registry.json"
        path.rename(other)
        path.symlink_to(other)
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
    )
    _binding, decision = runtime.inspect(
        "session-a",
        call_id="retry",
        name="google_search",
        arguments={"sourceInfo": registry["tools"][0]["sourceInfo"]},
        expected_revision=3,
    )
    assert decision.action.value == "DENY"
    assert "extension" in decision.reason.lower(), decision.reason


@pytest.mark.parametrize(
    "target", ["source", "dependency", "config", "project-pi", "project-agents"]
)
def test_extension_implementation_and_configuration_are_control_paths(tmp_path, target):
    runtime, source, registry = _extension_runtime(tmp_path, "auto")
    paths = {
        "source": source,
        "dependency": source.parent / "lib.js",
        "config": Path(registry["tools"][0]["sourceInfo"]["baseDir"]) / "settings.json",
        "project-pi": tmp_path / "workspace/.pi/extensions/injected.js",
        "project-agents": tmp_path / "workspace/.agents/extensions/injected.js",
    }
    _binding, decision = runtime.inspect(
        "session-a",
        call_id="tamper",
        name="write",
        arguments={"path": str(paths[target]), "content": "injected"},
        expected_revision=3,
    )
    assert decision.action.value == "DENY"
    assert decision.rule_id == "control-path"


@pytest.mark.parametrize("source", [None, {}, {"scope": "user", "source": "sdk"}])
def test_tool_arguments_cannot_supply_missing_extension_provenance(tmp_path, source):
    runtime, _entry, registry = _extension_runtime(tmp_path, "auto")
    registry["tools"][0]["sourceInfo"] = source
    bridge = runtime.path("session-a").parent
    (bridge / "permission_tools.json").write_text(json.dumps(registry))
    _binding, decision = runtime.inspect(
        "session-a",
        call_id="spoof",
        name="google_search",
        arguments={
            "sourceInfo": {"scope": "user", "source": "npm:trusted"},
            "tool_origin": "user-extension",
            "readOnlyHint": True,
        },
        expected_revision=3,
    )
    assert decision.action.value == "DENY"
    assert "extension source" in decision.reason


@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("ask", "ASK"),
        ("accept-edits", "ASK"),
        ("auto", "DENY"),
        ("plan", "DENY"),
        ("full", "ALLOW"),
    ],
)
def test_incomplete_initial_local_extension_source_is_untrusted_not_changed(
    tmp_path, tier, expected
):
    runtime, _entry, registry = _extension_runtime(tmp_path, tier)
    tool = registry["tools"][0]
    # Pi's explicit global settings.extensions local entries omit optional baseDir.
    tool["sourceInfo"].pop("baseDir")
    tool["sourceInfo"].update(source="local", origin="top-level")
    for key in ("real_path", "real_base_dir", "implementation_root", "source_sha256"):
        tool.pop(key)
    (runtime.path("session-a").parent / "permission_tools.json").write_text(json.dumps(registry))
    _binding, decision = runtime.inspect(
        "session-a", call_id="local", name="google_search", arguments={}, expected_revision=3
    )
    assert decision.action.value == expected, decision.reason
    if tier != "full":
        assert "changed" not in decision.reason


def test_verified_extension_registry_is_not_remote_tool_authority(tmp_path):
    runtime, _entry, _registry = _extension_runtime(tmp_path, "auto")
    _binding, decision = runtime.inspect(
        "session-a",
        call_id="remote",
        name="google_search",
        arguments={},
        expected_revision=3,
        local_execution=False,
    )
    assert decision.action.value == "DENY"


def test_user_extension_symlink_protects_the_resolved_package(tmp_path):
    runtime, entry, registry = _extension_runtime(tmp_path, "auto")
    alias = tmp_path / "installed-link.js"
    alias.symlink_to(entry)
    registry["tools"][0]["sourceInfo"]["path"] = str(alias)
    (runtime.path("session-a").parent / "permission_tools.json").write_text(json.dumps(registry))
    _binding, decision = runtime.inspect(
        "session-a", call_id="linked", name="google_search", arguments={}, expected_revision=3
    )
    assert decision.action.value == "ALLOW"
    _binding, decision = runtime.inspect(
        "session-a",
        call_id="tamper-dependency",
        name="write",
        arguments={"path": str(entry.parent / "lib.js"), "content": "replace"},
        expected_revision=3,
    )
    assert decision.rule_id == "control-path"


def test_extension_configuration_symlink_change_invalidates_trust(tmp_path):
    runtime, _entry, registry = _extension_runtime(tmp_path, "auto")
    base = Path(registry["tools"][0]["sourceInfo"]["baseDir"])
    alias = tmp_path / "config-link"
    alias.symlink_to(base, target_is_directory=True)
    registry["tools"][0]["sourceInfo"]["baseDir"] = str(alias)
    (runtime.path("session-a").parent / "permission_tools.json").write_text(json.dumps(registry))
    binding, decision = runtime.inspect(
        "session-a", call_id="linked", name="google_search", arguments={}, expected_revision=3
    )
    assert decision.action.value == "ALLOW"
    other = tmp_path / "other-config"
    other.mkdir()
    alias.unlink()
    alias.symlink_to(other, target_is_directory=True)
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
    )


@pytest.mark.parametrize("runner_id", ["runner-a", None])
def test_cli_permission_fence_uses_authenticated_runner_binding(tmp_path, monkeypatch, runner_id):
    path = tmp_path / "permission_state.json"
    monkeypatch.setattr(PermissionTierRuntime, "state_path", staticmethod(lambda *_: path))
    envelope = RunnerSessionInitEnvelope(
        protocol_version=2,
        server_version="test",
        session_id="cli-session",
        agent_id="agent",
        snapshot=RunnerSessionInitSnapshot(
            created_at=1,
            updated_at=1,
            host_id=None,
            runner_id=runner_id,
            workspace=str(tmp_path),
            labels=ConfirmedPermissionState("auto", 1, "blocked").labels("pi"),
        ),
    )
    runtime = PermissionTierRuntime()
    if runner_id is None:
        with pytest.raises(ValueError, match="trusted host snapshot"):
            runtime.fence_start(envelope, "pi")
        assert not path.exists()
    else:
        runtime.fence_start(envelope, "pi")
        state = json.loads(path.read_text())
        assert state["host_id"] == "runner:runner-a"
        assert state["status"] == "blocked"


@pytest.mark.parametrize(
    ("name", "local_execution", "expected"),
    [
        ("sys_read_inbox", True, "ALLOW"),
        ("sys_read_inbox", False, "DENY"),
        ("unknown_control", True, "DENY"),
    ],
)
def test_framework_inbox_requires_local_execution(
    tmp_path: Path, name: str, local_execution: bool, expected: str
) -> None:
    runtime = _runtime(tmp_path, "auto")
    _binding, decision = runtime.inspect(
        "session-a",
        call_id="inbox",
        name=name,
        arguments={},
        expected_revision=3,
        local_execution=local_execution,
    )
    assert decision.action.value == expected


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
    tmp_path: Path,
) -> None:
    from omnigent.spec import load

    runtime = _runtime(tmp_path, "full")
    runtime._harnesses["session-a"] = "pi"
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "config.yaml").write_bytes(
        (
            Path(__file__).parents[2] / "omnigent" / "resources" / "examples" / "pi-rpc.yaml"
        ).read_bytes()
    )
    spec = load(bundle)

    async def none(_session_id: str) -> None:
        return None

    async def cwd(_session_id: str) -> Path:
        return tmp_path / "workspace"

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
            _session_spec_cache={"session-a": spec},
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

        async def call(name: str, arguments: dict[str, object]) -> dict:
            response = await client.post(
                "/v1/sessions/session-a/mcp/execute",
                json={"method": "tools/call", "params": {"name": name, "arguments": arguments}},
            )
            return response.json()

        written = await call("sys_os_write", {"path": "note.txt", "content": "before"})
        assert "error" not in json.loads(written["result"]["output"])
        edited = await call(
            "sys_os_edit", {"path": "note.txt", "oldText": "before", "newText": "after"}
        )
        assert "error" not in json.loads(edited["result"]["output"])
        assert (tmp_path / "workspace" / "note.txt").read_text() == "after"
        search = (
            "dir /s /b note.txt"
            if sys.platform == "win32" and not (shutil.which("bash") or shutil.which("sh"))
            else "find . -name note.txt"
        )
        found = await call("sys_os_shell", {"command": search})
        shell = json.loads(found["result"]["output"])
        assert shell["exit_code"] == 0
        assert "note.txt" in shell["stdout"]
        runtime.force_block("session-a", "transaction-a")
        for name, arguments in (
            ("sys_os_write", {"path": "blocked.txt", "content": "denied"}),
            ("sys_os_edit", {"path": "note.txt", "oldText": "after", "newText": "denied"}),
            ("sys_os_shell", {"command": "echo denied > blocked.txt"}),
        ):
            denied = await call(name, arguments)
            assert denied["error"]["message"] == "Permission tier unavailable"
        assert not (tmp_path / "workspace" / "blocked.txt").exists()
        assert (tmp_path / "workspace" / "note.txt").read_text() == "after"


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


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["pi", "pi-native", "antigravity-native"])
@pytest.mark.parametrize(
    "workspace_source", ["session", "runner", "session-worktree", "worktree", "missing"]
)
async def test_runner_rejects_missing_trusted_init_and_accepts_current_server_input(
    monkeypatch, tmp_path, harness, workspace_source
):
    from omnigent.permission_tiers.host import read_host_permission_state
    from omnigent.runner import create_runner_app
    from omnigent.runner.native import orchestration
    from tests.runner.conftest import (
        _FakeProcessManager,
        _runner_client,
        _ScriptedHarnessClient,
        _spec_resolver_returning,
    )
    from tests.runner.helpers import NullServerClient
    from tests.runner.native_helpers import _harness_spec

    runner_workspace = tmp_path / "runner-workspace"
    expected_workspace = tmp_path / workspace_source
    if workspace_source == "runner":
        expected_workspace = runner_workspace
    launches = []

    async def launch(session_id, *_args, **_kwargs):
        state_path = app.state.permission_tiers.state_path(session_id, harness, {})
        state = read_host_permission_state(state_path)
        assert state.workspace == str(expected_workspace)
        assert state.status == "blocked"
        launches.append(session_id)

    monkeypatch.setattr(orchestration, "_auto_create_pi_terminal", launch)
    monkeypatch.setattr(orchestration, "_auto_create_antigravity_terminal", launch)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,
        spec_resolver=await _spec_resolver_returning(_harness_spec(harness)),
        server_client=NullServerClient(),
        runner_workspace=runner_workspace if workspace_source != "missing" else None,
    )
    body = {"session_id": "trusted-startup", "agent_id": "test-agent"}
    async with _runner_client(app) as client:
        refused = await client.post("/v1/sessions", json=body)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"] == "permission_tier_unavailable"
    assert app.state.permission_tiers.state(body["session_id"]) is None
    snapshot = {
        "workspace": str(expected_workspace) if workspace_source.startswith("session") else None,
        "worktree": str(expected_workspace) if "worktree" in workspace_source else None,
        "host_id": None,
        "runner_id": "runner-parent",
        "parent_session_id": "parent-session",
    }
    async with _runner_client(
        app, permission_harness=harness, permission_snapshot=snapshot
    ) as client:
        created = await client.post("/v1/sessions", json=body)
    if workspace_source in ("missing", "worktree"):
        assert created.status_code == 409, created.text
        assert not pm.get_client_calls
        assert not launches
        assert app.state.permission_tiers.state(body["session_id"]) is None
        return
    assert created.status_code == 201, created.text
    state = app.state.permission_tiers.state(body["session_id"])
    assert (state.tier, state.revision, state.status) == ("auto", 1, "ready")
    assert state.workspace == str(expected_workspace)
    assert state.host_id == "runner:runner-parent"
    assert launches == ([] if harness == "pi" else [body["session_id"]])
    if harness == "pi":
        assert pm.get_client_calls[-1][2]["HARNESS_PI_CWD"] == str(expected_workspace)
    state_path = app.state.permission_tiers.state_path(body["session_id"], harness, {})
    assert state_path.is_relative_to(tmp_path)
    assert state_path.is_file()
