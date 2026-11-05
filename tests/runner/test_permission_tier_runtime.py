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
from omnigent.permission_tiers import (
    NativeInputSnapshot,
    NativeSupport,
    NativeSupportStatus,
    PermissionTier,
)
from omnigent.permission_tiers.host import HostPermissionState, write_host_permission_state
from omnigent.runner.mcp_execution_registry import McpExecutionRegistry
from omnigent.runner.mcp_routes import register_mcp_routes
from omnigent.runner.permission_tiers import PermissionTierRuntime
from omnigent.runner.session_init_protocol import (
    RunnerSessionInitEnvelope,
    RunnerSessionInitSnapshot,
)
from omnigent.server.permission_tiers import (
    PERMISSION_CAPABILITY_LABEL_KEY,
    PERMISSION_CAPABILITY_VALUE,
    ConfirmedPermissionState,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("installed", [True, False])
@pytest.mark.parametrize("phase", ["bootstrap", "before-fence"])
async def test_first_pi_turn_waits_for_permission_bootstrap(monkeypatch, installed, phase, caplog):
    from omnigent.runner import create_runner_app
    from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
    from tests.runner.helpers import NullServerClient
    from tests.runner.native_helpers import _harness_spec

    entered, release = asyncio.Event(), asyncio.Event()
    spec = _harness_spec("pi")
    resolved = False

    async def resolve(*_args):
        nonlocal resolved
        if not resolved and phase == "before-fence":
            resolved = True
            entered.set()
            await release.wait()
        return spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm, spec_resolver=resolve, server_client=NullServerClient()
    )
    harness = pm._client
    original_post = harness.post

    async def bootstrap(url, **kwargs):
        if url == "/v1/permission-tier/bootstrap":
            if phase == "bootstrap":
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
            state = app.state.permission_tiers.state("cold-pi")
            assert state is None if phase == "before-fence" else state.status == "blocked"
            assert not harness.posted_bodies
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


@pytest.mark.asyncio
async def test_first_pi_native_turn_waits_for_late_permission_ack(monkeypatch, caplog):
    from tests.runner.conftest import _build_app_for_spec, _runner_client
    from tests.runner.native_helpers import _harness_spec

    app, pm = await _build_app_for_spec(_harness_spec("pi-native"))
    harness = pm._client
    entered = asyncio.Event()
    async with _runner_client(app, permission_harness="pi-native") as client:
        loop = asyncio.get_running_loop()
        acknowledge_at = loop.time() + 40.2

        def acknowledge_late(*_args):
            entered.set()
            return loop.time() >= acknowledge_at

        monkeypatch.setattr(
            "omnigent.harnesses.pi_native.bridge.verify_pi_permission_installation",
            acknowledge_late,
        )
        init = asyncio.create_task(
            client.post(
                "/v1/sessions", json={"session_id": "cold-pi-native", "agent_id": "test-agent"}
            )
        )
        await asyncio.wait_for(entered.wait(), 5)
        response = await client.post(
            "/v1/sessions/cold-pi-native/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "test-agent",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        )
        assert response.status_code == 202, response.text
        turn = app.state.active_turns.get("cold-pi-native")
        assert turn is not None
        done, _ = await asyncio.wait([turn], timeout=0.05)
        assert not done, "first turn failed while the extension acknowledgement was late"
        initialized = await asyncio.wait_for(init, 60)
        await asyncio.wait_for(turn, 60)
    assert initialized.status_code == 201, initialized.text
    assert "hello" in json.dumps(harness.posted_bodies)
    assert "turn setup failed" not in caplog.text


def _runtime(
    tmp_path: Path, tier: str, runtime: PermissionTierRuntime | None = None
) -> PermissionTierRuntime:
    runtime = runtime or PermissionTierRuntime()
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
    tmp_path: Path, tier: str, scope: str = "user", name: str = "google_search", runtime=None
):
    from omnigent.harnesses.pi_native.bridge import prepare_permission_installation

    runtime = _runtime(tmp_path, tier, runtime)
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


def _write_scope_registry(admission, tools):
    from omnigent.util.atomic_json import atomic_write_json

    expected = json.loads((admission.registry_dir / "permission_expected.json").read_text())
    loaded = [
        {
            "path": expected["extension_path"],
            "resolved_path": expected["extension_path"],
            "source_sha256": expected["extension_sha256"],
        }
    ]
    loaded.extend(
        {
            "path": tool["sourceInfo"]["path"],
            "resolved_path": tool["real_path"],
            "source_sha256": tool["source_sha256"],
        }
        for tool in tools
        if "real_path" in tool
    )
    atomic_write_json(
        admission.registry_dir / "permission_tools.json",
        {
            **expected,
            "tools": tools,
            "loaded_extensions": loaded,
        },
    )


def _publish_scope(runtime, admission, tools):
    _write_scope_registry(admission, tools)
    ready = runtime.acknowledge_pi_scope(admission.principal.root_session_id, admission.credential)
    assert ready
    record = runtime.pi_scopes.record(admission.principal)
    if record.native_classifier is None:
        expected = record.expected
        guard = Path(expected["extension_path"])
        snapshot = NativeInputSnapshot(
            str(guard), str(guard.resolve()), expected["extension_sha256"]
        )
        runtime.install_pi_native_classifier(
            admission.principal,
            lambda _call_id, binding, _arguments: NativeSupport(
                binding, NativeSupportStatus.ATTESTED, inputs=(snapshot,)
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("vendor", ["scoped", "legacy-ack", "failed", "full", "no-descriptor"])
async def test_runner_pi_bootstrap_requires_private_configuration_and_registration(
    tmp_path, monkeypatch, vendor
):
    from omnigent.harnesses.pi_native.bridge import prepare_permission_installation
    from omnigent.runner.pi_bootstrap import PiBootstrapBroker
    from tests.runner.conftest import _build_app_for_spec
    from tests.runner.helpers import permission_session_init_payload
    from tests.runner.native_helpers import _harness_spec

    app, manager = await _build_app_for_spec(_harness_spec("pi"))
    runtime = app.state.permission_tiers
    monkeypatch.setattr(runtime, "_verify_installation", lambda *_: None)
    if vendor == "no-descriptor":

        def unavailable(_self):
            raise ValueError("fixture launcher unavailable")

        monkeypatch.setattr(PiBootstrapBroker, "launcher_descriptor", unavailable)
    original_post = manager._client.post
    phases = []
    launcher_fields = []

    async def bootstrap(url, **kwargs):
        if url != "/v1/permission-tier/bootstrap":
            return await original_post(url, **kwargs)
        body = kwargs["json"]
        phases.append(body.get("phase"))
        launcher_fields.append(set(body.get("launcher", {})))
        assert runtime.state("cold-pi").status == "blocked"
        if vendor == "scoped" and body.get("phase") == "configure":
            bridge = runtime.path("cold-pi").parent
            source = bridge / "fixture-bridge.js"
            source.write_text("// vendor bridge fixture\n")
            prepare_permission_installation(
                bridge, session_id="cold-pi", harness="pi", extension=source
            )
            admission = runtime.begin_pi_scope_launch("cold-pi", native_guard=True)
            _publish_scope(
                runtime,
                admission,
                [{"name": "read", "sourceInfo": {"source": "builtin", "path": "builtin:read"}}],
            )
        return httpx.Response(200, json={"installed": vendor != "failed"})

    monkeypatch.setattr(manager._client, "post", bootstrap)
    payload = permission_session_init_payload(
        {"session_id": "cold-pi", "agent_id": "test-agent"},
        harness="pi",
        snapshot={
            "workspace": str(tmp_path),
            "labels": ConfirmedPermissionState(
                "full" if vendor == "full" else "auto", 1, "blocked"
            ).labels("pi"),
        },
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        try:
            response = await client.post("/v1/sessions", json=payload)
            success = vendor in ("scoped", "full")
            assert response.status_code == (201 if success else 503)
            assert runtime.state("cold-pi").status == ("ready" if success else "blocked")
            if vendor == "full":
                assert phases == [None]
                assert launcher_fields == [set()]
            elif vendor == "no-descriptor":
                assert phases == []
            else:
                assert phases == ["configure"]
                assert launcher_fields == [
                    {"protocol", "session_id", "endpoint", "process_capability"}
                ]
                assert runtime.pi_scope_protocol_active("cold-pi") is success
        finally:
            await client.delete("/v1/sessions/cold-pi")


@pytest.mark.parametrize(
    "name", ["read", "write", "bash", "sys_read_inbox", "sys_os_read", "subagent"]
)
def test_pi_scope_without_native_bootstrap_fact_denies_every_call(tmp_path, name):
    runtime, _parent, _child, _source, _registry = _scoped_runtime(tmp_path)
    child = runtime.open_pi_child_scope("session-a")
    guard = runtime.pi_scopes.record(child.principal).expected["extension_path"]
    _write_scope_registry(child, [{"name": name, "sourceInfo": {"path": guard}}])
    assert runtime.acknowledge_pi_scope("session-a", child.credential)
    arguments = {
        "path": "notes.txt",
        "content": "x",
        "command": "cat notes.txt",
        "agent": "reader",
        "task": "read",
        "async": False,
        "native_support": {"status": "attested_native"},
        "pi_delegation": True,
    }
    receipt, binding, result = _scope_call(runtime, child, name=name, arguments=arguments)
    assert result.action.value == "DENY"
    assert result.rule_id == "native-proof"
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
        origin_receipt=receipt.token,
    )


@pytest.mark.parametrize("fault", ["agent", "absent-settings"])
def test_native_semantics_and_input_snapshot_refresh_at_authorize_and_execute(tmp_path, fault):
    runtime, _parent, _child, source, registry = _scoped_runtime(tmp_path, "ask")
    manifest = source.parent / "package.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "pi-subagents",
                "version": "0.80.0",
                "exports": {
                    "./preflight": "./index.js",
                    "./required-child-extensions": "./index.js",
                    "./native-child-lifecycle": "./index.js",
                },
            }
        )
    )
    child = runtime.open_pi_child_scope("session-a")
    tool = {**registry["tools"][0], "name": "subagent"}
    _write_scope_registry(child, [tool])
    assert runtime.acknowledge_pi_scope("session-a", child.credential)
    agent = tmp_path / "workspace/.pi/agents/reader.md"
    agent.parent.mkdir(parents=True)
    agent.write_text("read-only fixture")
    snapshot = NativeInputSnapshot(
        str(agent), str(agent.resolve()), hashlib.sha256(agent.read_bytes()).hexdigest()
    )
    settings = agent.parents[1] / "settings.json"
    runtime.install_pi_native_classifier(
        child.principal,
        lambda _call_id, binding, _args: NativeSupport(
            binding,
            NativeSupportStatus.ATTESTED,
            "delegate",
            inputs=(snapshot, NativeInputSnapshot(str(settings), None, None)),
            semantics_digest=binding.arguments_digest,
        ),
    )
    arguments = {"agent": "reader", "task": "read", "async": False}
    receipt, binding, decision = _scope_call(runtime, child, name="subagent", arguments=arguments)
    assert decision.action.value == "ALLOW"
    changed = agent if fault == "agent" else settings
    changed.write_text("changed fixture")
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name="subagent",
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
        origin_receipt=receipt.token,
    )
    if fault == "agent":
        agent.write_text("read-only fixture")
    else:
        settings.unlink()
    receipt, binding, _ = _scope_call(
        runtime, child, call_id="fresh", name="subagent", arguments=arguments
    )
    assert runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name="subagent",
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
        origin_receipt=receipt.token,
    )
    changed.write_text("changed before backend")
    assert not runtime.verify_execute(
        "session-a",
        call_id=binding.call_id,
        name="subagent",
        arguments=arguments,
        arguments_digest=binding.arguments_digest,
        revision=3,
        operation_id="native-launch",
        step="initial",
        origin_receipt=receipt.token,
    )


def test_pi_native_semantic_and_bootstrap_modules_are_control_paths(tmp_path):
    runtime, _source, _registry = _extension_runtime(tmp_path, "auto")
    root = Path(omnigent.permission_tiers.__file__).resolve().parent.parent
    for index, filename in enumerate(
        ("native_support.cjs", "omnigent_pi_child_extension.js", "omnigent_pi_bootstrap.mjs")
    ):
        target = root / "resources/pi_native" / filename
        assert target.is_file()
        _binding, decision = runtime.inspect(
            "session-a",
            call_id=f"native-control-{index}",
            name="sys_os_write",
            arguments={"path": str(target), "content": "changed"},
            expected_revision=3,
        )
        assert decision.action.value == "DENY"
        assert decision.rule_id == "control-path"


def _scoped_runtime(tmp_path, tier="auto", runtime=None):
    runtime, source, registry = _extension_runtime(tmp_path, tier, runtime=runtime)
    parent = runtime.begin_pi_scope_launch("session-a")
    _publish_scope(runtime, parent, registry["tools"])
    child = runtime.open_pi_child_scope("session-a")
    _publish_scope(
        runtime,
        child,
        [
            {
                "name": "read",
                "sourceInfo": {"source": "builtin", "path": "builtin:read"},
            }
        ],
    )
    return runtime, parent, child, source, registry


def _scope_call(runtime, admission, *, call_id="same-call", name="read", arguments=None):
    arguments = arguments if arguments is not None else {"path": "notes.txt"}
    receipt = runtime.pi_origin_receipt(
        "session-a",
        admission.credential,
        call_id=call_id,
        name=name,
        arguments=arguments,
        local_execution=True,
    )
    binding, decision = runtime.inspect(
        "session-a",
        call_id=receipt.binding.call_id,
        name=name,
        arguments=arguments,
        expected_revision=3,
        origin_receipt=receipt.token,
    )
    return receipt, binding, decision


@pytest.mark.parametrize("tier", ["plan", "ask", "accept-edits", "auto", "full"])
def test_scoped_inline_tool_is_denied_without_disabling_registered_read(tmp_path, tier):
    runtime, _parent, _child, _source, _registry = _scoped_runtime(tmp_path, tier)
    child = runtime.open_pi_child_scope("session-a")
    _publish_scope(
        runtime,
        child,
        [
            {"name": "read", "sourceInfo": {"source": "builtin", "path": "builtin:read"}},
            {
                "name": "contact_supervisor",
                "sourceInfo": {
                    "path": "<inline:pi-subagents:prompt-runtime>",
                    "source": "inline",
                    "scope": "temporary",
                    "origin": "top-level",
                },
            },
        ],
    )
    _receipt, _binding, read = _scope_call(runtime, child, call_id="read")
    assert read.action.value == "ALLOW", read.reason
    _receipt, _binding, inline = _scope_call(
        runtime, child, call_id="inline", name="contact_supervisor", arguments={}
    )
    assert inline.action.value == ("ALLOW" if tier == "full" else "DENY"), inline.reason
    if tier != "full":
        assert inline.reason == "Pi inline tool source is not attested"
    assert runtime.pi_scopes.record(child.principal).status == "ready"


def test_pi_native_attestation_keeps_identical_calls_and_refreshes_distinct(tmp_path):
    runtime, _parent, _child, _source, _registry = _scoped_runtime(tmp_path)
    child = runtime.open_pi_child_scope("session-a")
    tools = [{"name": "read", "sourceInfo": {"source": "builtin", "path": "builtin:read"}}]
    _write_scope_registry(child, tools)
    assert runtime.acknowledge_pi_scope("session-a", child.credential)
    record = runtime.pi_scopes.record(child.principal)
    expected = record.expected
    guard = Path(expected["extension_path"])
    snapshot = NativeInputSnapshot(str(guard), str(guard.resolve()), expected["extension_sha256"])
    seen = []
    allowed = {"pi:" + hashlib.sha256((child.principal.scope_id + "\0A").encode()).hexdigest()}

    def classify(canonical_call_id, binding, _arguments):
        seen.append(canonical_call_id)
        return NativeSupport(
            binding,
            NativeSupportStatus.ATTESTED
            if canonical_call_id in allowed
            else NativeSupportStatus.FAILED,
            inputs=(snapshot,),
        )

    runtime.install_pi_native_classifier(child.principal, classify)
    a, binding_a, decision_a = _scope_call(runtime, child, call_id="A")
    b, binding_b, decision_b = _scope_call(runtime, child, call_id="B")
    assert decision_a.action.value == "ALLOW"
    assert decision_b.action.value == "DENY"
    assert binding_a.call_id != binding_b.call_id
    assert not runtime.authorize(
        "session-a",
        call_id=binding_b.call_id,
        name="read",
        arguments_digest=binding_b.arguments_digest,
        expected_revision=3,
        origin_receipt=b.token,
    )
    assert runtime.authorize(
        "session-a",
        call_id=binding_a.call_id,
        name="read",
        arguments_digest=binding_a.arguments_digest,
        expected_revision=3,
        origin_receipt=a.token,
    )
    assert seen.count(binding_a.call_id) >= 3
    assert seen.count(binding_b.call_id) >= 2


@pytest.mark.parametrize("untrusted", [None, "", "forged", {}, {"origin": "parent"}])
def test_pi_v2_local_call_requires_runner_receipt(tmp_path, untrusted):
    runtime, _parent, _child, _source, _registry = _scoped_runtime(tmp_path)
    with pytest.raises(ValueError):
        runtime.inspect(
            "session-a",
            call_id="same-call",
            name="read",
            arguments={"path": "notes.txt", "is_parent": True, "scope_id": "root"},
            expected_revision=3,
            origin_receipt=untrusted,
        )


@pytest.mark.parametrize("arguments", [{"path": "notes.txt"}, {"path": "笔记.txt"}])
def test_pi_scope_owns_registry_and_namespaces_duplicate_call_ids(tmp_path, arguments):
    runtime, parent, child, _source, _registry = _scoped_runtime(tmp_path)
    _p_receipt, parent_binding, parent_decision = _scope_call(
        runtime,
        parent,
        name="google_search",
        arguments={},
    )
    receipt, binding, decision = _scope_call(runtime, child, arguments=arguments)
    assert parent_decision.action.value == "ALLOW"
    assert decision.action.value == "ALLOW"
    assert parent_binding.call_id != binding.call_id
    assert not runtime.authorize(
        "session-a",
        call_id=parent_binding.call_id,
        name=parent_binding.name,
        arguments_digest=parent_binding.arguments_digest,
        expected_revision=3,
        origin_receipt=receipt.token,
    )


@pytest.mark.parametrize("fault", ["source", "registry", "generation", "expectation"])
def test_pi_child_registry_refreshes_at_authorize(tmp_path, fault):
    runtime, _parent, _child, source, registry = _scoped_runtime(tmp_path)
    child = runtime.open_pi_child_scope("session-a")
    _publish_scope(runtime, child, registry["tools"])
    receipt, binding, decision = _scope_call(
        runtime,
        child,
        name="google_search",
        arguments={},
    )
    assert decision.action.value == "ALLOW"
    registry_path = child.registry_dir / "permission_tools.json"
    original = {
        path: path.read_bytes()
        for path in (source, registry_path, child.registry_dir / "permission_expected.json")
    }
    if fault == "source":
        source.write_text("// changed after inspection\n")
    elif fault == "registry":
        registry_path.unlink()
    else:
        path = (
            registry_path
            if fault == "generation"
            else child.registry_dir / "permission_expected.json"
        )
        payload = json.loads(path.read_text())
        payload["registry_generation"] = "another-generation"
        path.write_text(json.dumps(payload))
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
        origin_receipt=receipt.token,
    )
    for path, contents in original.items():
        path.write_bytes(contents)
    with pytest.raises(ValueError):
        runtime.acknowledge_pi_scope("session-a", child.credential)
    with pytest.raises(ValueError):
        _scope_call(runtime, child, call_id="after-restore")


@pytest.mark.parametrize("check", ["acknowledge", "authorize"])
def test_pi_ready_registry_replacement_revokes_same_generation(tmp_path, check):
    runtime, _parent, child, _source, registry = _scoped_runtime(tmp_path, "ask")
    receipt, binding, decision = _scope_call(runtime, child)
    assert decision.action.value == "ALLOW"
    changed = {**registry["tools"][0], "name": "read"}
    _write_scope_registry(child, [changed])
    if check == "acknowledge":
        assert not runtime.acknowledge_pi_scope("session-a", child.credential)
    else:
        assert not runtime.authorize(
            "session-a",
            call_id=binding.call_id,
            name=binding.name,
            arguments_digest=binding.arguments_digest,
            expected_revision=3,
            origin_receipt=receipt.token,
        )
    with pytest.raises(ValueError):
        _scope_call(runtime, child, call_id="after-replacement")


@pytest.mark.parametrize("refreshed", ["ASK", "changed-ALLOW"])
def test_pi_authorize_refuses_a_different_inspected_verdict(tmp_path, monkeypatch, refreshed):
    from omnigent.permission_tiers import TierDecision, Verdict

    runtime, _parent, child, _source, _registry = _scoped_runtime(tmp_path, "ask")
    receipt, binding, decision = _scope_call(runtime, child)
    assert decision.action.value == "ALLOW"
    changed = (
        TierDecision(Verdict.ASK, "changed tool classification")
        if refreshed == "ASK"
        else TierDecision(Verdict.ALLOW, "different policy", "different-rule")
    )
    monkeypatch.setattr(runtime, "_evaluate", lambda *_args: changed)
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
        origin_receipt=receipt.token,
    )
    assert not runtime.verify_execute(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments={"path": "notes.txt"},
        arguments_digest=binding.arguments_digest,
        revision=3,
        operation_id="changed-verdict",
        step="initial",
        origin_receipt=receipt.token,
    )


@pytest.mark.parametrize("tier", ["ask", "accept-edits"])
@pytest.mark.parametrize(
    "fault",
    [
        "missing-source",
        "invalid-source",
        "missing-provenance",
        "invalid-provenance",
        "project-provenance",
    ],
)
def test_pi_malformed_v2_registration_is_not_approvable(tmp_path, tier, fault):
    runtime, _parent, _child, _source, registry = _scoped_runtime(tmp_path, tier)
    child = runtime.open_pi_child_scope("session-a")
    _write_scope_registry(child, registry["tools"])
    path = child.registry_dir / "permission_tools.json"
    payload = json.loads(path.read_text())
    row = payload["tools"][0]
    if fault == "missing-source":
        row.pop("sourceInfo")
    elif fault == "invalid-source":
        row["sourceInfo"] = "broken"
    elif fault == "missing-provenance":
        row.pop("real_path")
    elif fault == "invalid-provenance":
        row["source_sha256"] = 7
    else:
        row["sourceInfo"]["scope"] = "project"
        row.pop("implementation_root")
    path.write_text(json.dumps(payload))
    assert not runtime.acknowledge_pi_scope("session-a", child.credential)
    with pytest.raises(ValueError):
        _scope_call(runtime, child, name="google_search", arguments={})


@pytest.mark.parametrize(
    "fault", ["registry-symlink", "guard-symlink", "entry-retarget", "registry-replaced-fd"]
)
def test_pi_registration_file_races_refuse_readiness(tmp_path, monkeypatch, fault):
    import os

    runtime, _parent, _child, source, registry = _scoped_runtime(tmp_path)
    child = runtime.open_pi_child_scope("session-a")
    _write_scope_registry(child, registry["tools"])
    path = child.registry_dir / "permission_tools.json"
    target = (
        path
        if fault != "guard-symlink"
        else runtime.path("session-a").parent / "omnigent_pi_native_extension.js"
    )
    changed = False
    if fault == "entry-retarget":
        alias = source.with_name("loaded-alias.js")
        alias.symlink_to(source)
        other = source.with_name("other-entry.js")
        other.write_bytes(source.read_bytes())
        payload = json.loads(path.read_text())
        payload["loaded_extensions"][1]["resolved_path"] = str(alias)
        path.write_text(json.dumps(payload))
        resolve = Path.resolve

        def retarget(candidate, *args, **kwargs):
            nonlocal changed
            resolved = resolve(candidate, *args, **kwargs)
            if candidate == alias and not changed:
                changed = True
                alias.unlink()
                alias.symlink_to(other)
            return resolved

        monkeypatch.setattr(Path, "resolve", retarget)
    else:
        other = target.with_name("replacement-" + target.name)
        other.write_bytes(target.read_bytes())
        if fault == "registry-replaced-fd":
            open_file = os.open

            def replace_after_open(candidate, *args, **kwargs):
                nonlocal changed
                fd = open_file(candidate, *args, **kwargs)
                if Path(candidate) == target and not changed:
                    changed = True
                    os.replace(other, target)
                return fd

            monkeypatch.setattr(os, "open", replace_after_open)
        else:
            lstat = Path.lstat

            def replace_after_stat(candidate, *args, **kwargs):
                nonlocal changed
                info = lstat(candidate, *args, **kwargs)
                if candidate == target and not changed:
                    changed = True
                    target.unlink()
                    target.symlink_to(other)
                return info

            monkeypatch.setattr(Path, "lstat", replace_after_stat)
    assert not runtime.acknowledge_pi_scope("session-a", child.credential)
    assert changed, "race must occur at the actual file-validation boundary"


def test_pi_scope_credential_lookup_verifies_secret_in_constant_time(tmp_path, monkeypatch):
    import hmac

    runtime, _parent, child, _source, _registry = _scoped_runtime(tmp_path)
    compare = hmac.compare_digest
    comparisons = []

    def compare_secret(candidate, expected):
        comparisons.append((len(candidate), len(expected)))
        return compare(candidate, expected)

    monkeypatch.setattr(hmac, "compare_digest", compare_secret)
    assert runtime.pi_scopes.authenticate("session-a", child.credential).admission == child
    assert comparisons
    comparisons.clear()
    # An indexed candidate still has to prove the associated secret.
    candidate = "not-a-scope-credential"
    runtime.pi_scopes._credentials[hashlib.sha256(candidate.encode()).hexdigest()] = (
        child.principal.scope_id
    )
    with pytest.raises(ValueError):
        runtime.pi_scopes.authenticate("session-a", candidate)
    assert comparisons


def test_pi_expected_file_does_not_short_circuit_incomplete_publication(tmp_path):
    runtime, _parent, _child, _source, _registry = _scoped_runtime(tmp_path)
    pending = runtime.open_pi_child_scope("session-a")
    assert not runtime.acknowledge_pi_scope("session-a", pending.credential)
    with pytest.raises(ValueError):
        runtime.pi_origin_receipt(
            "session-a",
            pending.credential,
            call_id="call",
            name="read",
            arguments={},
            local_execution=True,
        )
    _publish_scope(
        runtime,
        pending,
        [
            {
                "name": "read",
                "sourceInfo": {"source": "builtin", "path": "builtin:read"},
            }
        ],
    )
    assert _scope_call(runtime, pending)[2].action.value == "ALLOW"


@pytest.mark.parametrize("authorized", [False, True])
def test_pi_close_rejects_late_approval_and_unused_lease_without_affecting_sibling(
    tmp_path, authorized
):
    runtime, parent, child, _source, _registry = _scoped_runtime(tmp_path)
    receipt, binding, _ = _scope_call(runtime, child)
    if authorized:
        assert runtime.authorize(
            "session-a",
            call_id=binding.call_id,
            name=binding.name,
            arguments_digest=binding.arguments_digest,
            expected_revision=3,
            origin_receipt=receipt.token,
        )
    runtime.revoke_pi_scope(child.principal)
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
        origin_receipt=receipt.token,
    )
    assert not runtime.verify_execute(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments={"path": "notes.txt"},
        arguments_digest=binding.arguments_digest,
        revision=3,
        operation_id="operation",
        step="initial",
        origin_receipt=receipt.token,
    )
    assert runtime.idle("session-a")
    with pytest.raises(ValueError):
        _scope_call(runtime, child, call_id="new-call")
    assert (
        _scope_call(runtime, parent, name="google_search", arguments={})[2].action.value == "ALLOW"
    )


def test_pi_revoked_scope_can_only_finish_its_own_already_executing_call(tmp_path):
    runtime, parent, child, _source, _registry = _scoped_runtime(tmp_path)
    receipt, binding, _ = _scope_call(runtime, child)
    assert runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
        origin_receipt=receipt.token,
    )
    assert runtime.verify_execute(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments={"path": "notes.txt"},
        arguments_digest=binding.arguments_digest,
        revision=3,
        operation_id="operation",
        step="initial",
        origin_receipt=receipt.token,
    )
    other, _, _ = _scope_call(runtime, parent, name="google_search", arguments={})
    runtime.revoke_pi_scope(child.principal)
    assert not runtime.finish(
        "session-a", call_id=binding.call_id, revision=3, origin_receipt=other.token
    )
    assert runtime.finish(
        "session-a", call_id=binding.call_id, revision=3, origin_receipt=receipt.token
    )
    assert runtime.finish(
        "session-a", call_id=binding.call_id, revision=3, origin_receipt=receipt.token
    )
    assert runtime.idle("session-a")


def test_pi_sdk_resolved_identity_is_used_instead_of_a_guessed_display_path(tmp_path):
    runtime, _parent, _child, source, registry = _scoped_runtime(tmp_path)
    child = runtime.open_pi_child_scope("session-a")
    registry["tools"][0]["sourceInfo"]["path"] = str(source.parent / "index\u00a0alias.js")
    _publish_scope(runtime, child, registry["tools"])
    assert (
        _scope_call(runtime, child, name="google_search", arguments={})[2].action.value == "ALLOW"
    )


def test_pi_root_rebind_invalidates_previous_credentials_and_receipts(tmp_path):
    runtime, _parent, child, _source, _registry = _scoped_runtime(tmp_path)
    receipt, binding, _ = _scope_call(runtime, child)
    runtime.begin_pi_scope_launch("session-a")
    with pytest.raises(ValueError):
        runtime.inspect(
            "session-a",
            call_id=binding.call_id,
            name=binding.name,
            arguments={"path": "notes.txt"},
            expected_revision=3,
            origin_receipt=receipt.token,
        )
    with pytest.raises(ValueError):
        _scope_call(runtime, child)


def test_pi_scope_cannot_invoke_unregistered_builtin_or_switch_execution_class(tmp_path):
    runtime, _parent, child, _source, _registry = _scoped_runtime(tmp_path)
    _, _, denied = _scope_call(
        runtime, child, name="write", arguments={"path": "new.txt", "content": "x"}
    )
    assert denied.action.value == "DENY"
    receipt = runtime.pi_origin_receipt(
        "session-a",
        child.credential,
        call_id="classified",
        name="read",
        arguments={"path": "notes.txt"},
        local_execution=False,
    )
    with pytest.raises(ValueError):
        runtime.inspect(
            "session-a",
            call_id=receipt.binding.call_id,
            name="read",
            arguments={"path": "notes.txt"},
            expected_revision=3,
            local_execution=True,
            origin_receipt=receipt.token,
        )


def test_pi_full_to_restricted_does_not_restore_unscoped_parent_fallback(tmp_path):
    runtime, _parent, child, _source, _registry = _scoped_runtime(tmp_path, "full")
    assert (
        runtime.inspect(
            "session-a",
            call_id="legacy",
            name="read",
            arguments={},
            expected_revision=3,
        )[1].action.value
        == "ALLOW"
    )
    runtime.prepare("session-a", expected_revision=3, transaction_id="switch")
    runtime.settle("session-a", transaction_id="switch", tier="auto", revision=4)
    with pytest.raises(ValueError):
        runtime.pi_origin_receipt(
            "session-a",
            child.credential,
            call_id="old-full-scope",
            name="read",
            arguments={"path": "notes.txt"},
            local_execution=True,
        )
    with pytest.raises(ValueError):
        runtime.open_pi_child_scope("session-a")
    with pytest.raises(ValueError):
        runtime.inspect(
            "session-a", call_id="legacy", name="read", arguments={}, expected_revision=4
        )
    fresh = runtime.begin_pi_scope_launch("session-a")
    _publish_scope(
        runtime,
        fresh,
        [{"name": "read", "sourceInfo": {"source": "builtin", "path": "builtin:read"}}],
    )
    receipt = runtime.pi_origin_receipt(
        "session-a",
        fresh.credential,
        call_id="reinstalled",
        name="read",
        arguments={"path": "notes.txt"},
        local_execution=True,
    )
    _, decision = runtime.inspect(
        "session-a",
        call_id=receipt.binding.call_id,
        name="read",
        arguments={"path": "notes.txt"},
        expected_revision=4,
        origin_receipt=receipt.token,
    )
    assert decision.action.value == "ALLOW"


@pytest.mark.parametrize("harness", ["antigravity-native", "codex-native"])
def test_trusted_harness_rebind_retires_pi_authority_without_blocking_other_harness(
    tmp_path, monkeypatch, harness
):
    from omnigent.runner.pi_scope_relay import PiScopeRelay

    runtime, _parent, child, _source, _registry = _scoped_runtime(tmp_path)
    labels = (
        ConfirmedPermissionState("auto", 3, "blocked").labels(harness)
        if harness == "antigravity-native"
        else {}
    )
    envelope = RunnerSessionInitEnvelope(
        protocol_version=2,
        server_version="test",
        session_id="session-a",
        agent_id="agent",
        snapshot=RunnerSessionInitSnapshot(
            created_at=1,
            updated_at=1,
            host_id="host-a",
            workspace=str(tmp_path / "workspace"),
            labels=labels,
        ),
    )
    monkeypatch.setattr(
        PermissionTierRuntime, "state_path", staticmethod(lambda *_: runtime.path("session-a"))
    )
    monkeypatch.setattr(
        PermissionTierRuntime, "_verify_installation", staticmethod(lambda *_: None)
    )
    runtime.fence_start(envelope, harness)
    runtime.install(envelope, harness)
    assert not runtime.pi_scope_protocol_active("session-a")
    assert PiScopeRelay(runtime, "session-a").legacy_allowed()
    with pytest.raises(ValueError):
        _scope_call(runtime, child)
    if harness == "antigravity-native":
        _, decision = runtime.inspect(
            "session-a",
            call_id="other-harness-read",
            name="read",
            arguments={"path": "notes.txt"},
            expected_revision=3,
        )
        assert decision.action.value == "ALLOW"


@pytest.mark.asyncio
async def test_scoped_native_http_policy_runs_through_server_and_runner(tmp_path, monkeypatch):
    import hmac
    from types import SimpleNamespace

    from fastapi import APIRouter

    from omnigent.entities import Conversation
    from omnigent.harnesses.claude_native import bridge as relay_bridge
    from omnigent.harnesses.pi_native import bridge as pi_bridge
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.runner.pi_scope_relay import PiScopeRelay
    from omnigent.server.auth import LEVEL_READ
    from omnigent.server.routes import sessions
    from omnigent.server.routes._auth_helpers import SessionAccess
    from omnigent.server.routes.sessions import routes_hooks
    from tests.runner.conftest import _build_app_for_spec, _runner_client
    from tests.runner.native_helpers import _harness_spec

    runner, _pm = await _build_app_for_spec(_harness_spec("pi-native"))
    runtime, _source, registry = _extension_runtime(
        tmp_path, "auto", runtime=runner.state.permission_tiers
    )
    parent = runtime.begin_pi_scope_launch("session-a")
    parent_read = {**registry["tools"][0], "name": "read"}
    parent_read["sourceInfo"] = {**parent_read["sourceInfo"], "scope": "project"}
    _publish_scope(runtime, parent, [parent_read])
    child = runtime.open_pi_child_scope("session-a")
    _publish_scope(
        runtime,
        child,
        [{"name": "read", "sourceInfo": {"source": "builtin", "path": "builtin:read"}}],
    )
    conv = Conversation(
        id="session-a",
        created_at=1,
        updated_at=1,
        root_conversation_id="session-a",
        host_id="host-a",
        runner_id=token_bound_runner_id("test-binding"),
        workspace=str(tmp_path / "workspace"),
        harness_override="pi-native",
        labels={
            **ConfirmedPermissionState("auto", 3, "ready").labels("pi-native"),
            PERMISSION_CAPABILITY_LABEL_KEY: PERMISSION_CAPABILITY_VALUE,
        },
    )
    store = SimpleNamespace(get_conversation=lambda _id: conv)

    async def access(*_args, **_kwargs):
        return SessionAccess(level=LEVEL_READ, conversation=conv)

    monkeypatch.setattr(routes_hooks, "_require_access_and_level", access)
    router = APIRouter()
    routes_hooks.register_hooks_routes(router, conversation_store=store, agent_store=None)
    server = FastAPI()
    server.include_router(router, prefix="/v1")
    monkeypatch.setattr(pi_bridge, "_BRIDGE_ROOT", tmp_path)

    async def no_tool(_name, _arguments):
        raise AssertionError("child must not reach unrestricted relay tool dispatch")

    async with _runner_client(runner) as runner_client:

        async def get_runner(*_args, **_kwargs):
            return runner_client

        monkeypatch.setattr(sessions, "_get_runner_client", get_runner)

        async def control(body):
            response = await runner_client.post("/v1/sessions/session-a/events", json=body)
            return response.json()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=server),
            base_url="http://server",
            headers={RUNNER_TUNNEL_TOKEN_HEADER: "test-binding"},
        ) as policy_client:
            relay = relay_bridge.start_tool_relay(
                bridge_dir=runtime.path("session-a").parent,
                tools=[],
                tool_executor=no_tool,
                loop=asyncio.get_running_loop(),
                session_id="session-a",
                policy_client=policy_client,
                permission_tier_control=control,
                pi_scope_relay=PiScopeRelay(runtime, "session-a"),
            )
            info = json.loads((runtime.path("session-a").parent / "tool_relay.json").read_text())
            comparisons = []
            compare = hmac.compare_digest

            def compare_secret(candidate, expected):
                comparisons.append((len(candidate), len(expected)))
                return compare(candidate, expected)

            monkeypatch.setattr(hmac, "compare_digest", compare_secret)
            try:
                async with httpx.AsyncClient(base_url=info["url"], trust_env=False) as client:
                    bad = await client.post(
                        "/policies/evaluate",
                        content=b"not JSON",
                        headers={"Authorization": "Bearer invalid"},
                    )
                    assert bad.status_code == 401
                    legacy = await client.post(
                        "/policies/evaluate",
                        content=b"not JSON",
                        headers={"Authorization": "Bearer " + info["token"]},
                    )
                    assert legacy.status_code == 401
                    assert any(
                        expected == len("Bearer " + info["token"])
                        for _candidate, expected in comparisons
                    )
                    child_headers = {"Authorization": "Bearer " + child.credential}
                    event = {
                        "event": {
                            "type": "PHASE_TOOL_CALL",
                            "target": "read",
                            "data": {
                                "name": "read",
                                "arguments": {"path": "notes.txt"},
                                "call_id": "same-call",
                            },
                        },
                        "pi_origin_receipt": "forged",
                        "is_parent": True,
                        "scope_id": parent.principal.scope_id,
                    }
                    response = await client.post(
                        "/policies/evaluate", json=event, headers=child_headers
                    )
                    assert response.status_code == 200
                    body = response.json()
                    assert body["result"] == "POLICY_ACTION_UNSPECIFIED"
                    binding = body.get("permission_tier_binding")
                    assert isinstance(binding, dict)
                    receipt = runtime.pi_scopes.resolve(
                        "session-a", binding.get("pi_origin_receipt")
                    )
                    assert receipt.principal == child.principal
                    parent_response = await client.post(
                        "/policies/evaluate",
                        json=event,
                        headers={"Authorization": "Bearer " + parent.credential},
                    )
                    assert parent_response.json()["result"] == "POLICY_ACTION_DENY"
                    bypass = await client.post(
                        "/permission-tiers/control",
                        headers=child_headers,
                        json={"type": "permission_tier_authorize"},
                    )
                    assert bypass.status_code == 403
                    forbidden = await client.post(
                        "/tool",
                        headers=child_headers,
                        json={"name": "sys_session_send", "arguments": {}},
                    )
                    assert forbidden.status_code == 401
                    execute = {
                        "type": "permission_tier_execute",
                        "call_id": binding["call_id"],
                        "name": "read",
                        "arguments": {"path": "notes.txt"},
                        "expected_revision": 3,
                        "arguments_digest": binding["arguments_digest"],
                        "operation_id": "actual-read",
                        "step": "initial",
                        "pi_origin_receipt": receipt.token,
                    }
                    started = await client.post(
                        "/permission-tiers/control", headers=child_headers, json=execute
                    )
                    assert started.json().get("executed") is True
                    runtime.revoke_pi_scope(child.principal)
                    late = await client.post(
                        "/permission-tiers/control",
                        headers=child_headers,
                        json={**execute, "step": "late"},
                    )
                    assert late.status_code == 403
                    finish = {
                        "type": "permission_tier_finish",
                        "call_id": binding["call_id"],
                        "expected_revision": 3,
                        "pi_origin_receipt": receipt.token,
                    }
                    wrong = await client.post(
                        "/permission-tiers/control",
                        headers={"Authorization": "Bearer " + parent.credential},
                        json=finish,
                    )
                    assert wrong.status_code == 403
                    settled = await client.post(
                        "/permission-tiers/control", headers=child_headers, json=finish
                    )
                    assert settled.json().get("finished") is True
                    assert runtime.idle("session-a")
            finally:
                relay.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True])
async def test_pi_ask_reply_after_child_close_is_denied_by_actual_runner(
    tmp_path, monkeypatch, revoke
):
    from types import SimpleNamespace

    from omnigent.entities import Conversation
    from omnigent.server import permission_gate as gate
    from omnigent.server.routes import sessions
    from omnigent.server.routes._sessions import orchestration
    from omnigent.server.schemas import ElicitationResult
    from tests.runner.conftest import _build_app_for_spec, _runner_client
    from tests.runner.native_helpers import _harness_spec

    app, _pm = await _build_app_for_spec(_harness_spec("pi-native"))
    runtime, _parent, _child, _source, _registry = _scoped_runtime(
        tmp_path,
        "ask",
        app.state.permission_tiers,
    )
    child = runtime.open_pi_child_scope("session-a")
    _publish_scope(
        runtime,
        child,
        [{"name": "write", "sourceInfo": {"source": "builtin", "path": "builtin:write"}}],
    )
    arguments = {"path": "new.txt", "content": "approved"}
    receipt = runtime.pi_origin_receipt(
        "session-a",
        child.credential,
        call_id="waiting-write",
        name="write",
        arguments=arguments,
        local_execution=True,
    )
    conv = Conversation(
        id="session-a",
        root_conversation_id="session-a",
        created_at=1,
        updated_at=1,
        host_id="host-a",
        workspace=str(tmp_path / "workspace"),
        labels={
            **ConfirmedPermissionState("ask", 3, "ready").labels("pi-native"),
            PERMISSION_CAPABILITY_LABEL_KEY: PERMISSION_CAPABILITY_VALUE,
        },
    )
    store = SimpleNamespace(get_conversation=lambda _id: conv)
    gate._active_approvals.clear()
    gate._consumed_approvals.clear()

    async def approve(*_args, **_kwargs):
        if revoke:
            runtime.revoke_pi_scope(child.principal)
        return ElicitationResult(action="accept")

    monkeypatch.setattr(orchestration, "_publish_and_wait_for_harness_elicitation", approve)
    async with _runner_client(app) as client:

        async def get_runner(*_args, **_kwargs):
            return client

        monkeypatch.setattr(sessions, "_get_runner_client", get_runner)
        inspection = await gate.inspect_permission_call(
            conv,
            harness="pi-native",
            name="write",
            arguments=arguments,
            call_id=receipt.binding.call_id,
            runner_router=None,
            origin_receipt=receipt.token,
        )
        assert inspection.decision.action.value == "ASK", inspection.decision.reason
        decision = await gate.authorize_permission_call(
            inspection,
            request=None,
            harness="pi-native",
            arguments=arguments,
            conversation_store=store,
            runner_router=None,
        )
        assert decision.action.value == ("DENY" if revoke else "ALLOW")
        if not revoke:
            completed = await gate.finish_permission_call(
                conv,
                call_id=receipt.binding.call_id,
                revision=3,
                runner_router=None,
                origin_receipt=receipt.token,
            )
            assert completed
        assert runtime.idle("session-a")


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


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", omnigent.permission_tiers.NON_FULL_TIERS)
@pytest.mark.parametrize("background", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("scope", ["user", "project"], ids=["verified", "unverified"])
async def test_pi_subagent_denial_cannot_open_card_or_acquire_launch_lease(
    tmp_path, monkeypatch, tier, background, scope
):
    from omnigent.server import permission_gate as gate
    from omnigent.server.routes._sessions import orchestration
    from omnigent.server.schemas import ElicitationResult

    runtime, _source, _registry = _extension_runtime(tmp_path, tier, scope, "subagent")
    arguments = {"agent": "worker", "task": "Read a.py", "async": background}
    binding, decision = runtime.inspect(
        "session-a",
        call_id="delegate",
        name="subagent",
        arguments=arguments,
        expected_revision=3,
    )
    cards = []

    async def approve(*args, **kwargs):
        cards.append(kwargs)
        return ElicitationResult(action="accept")

    monkeypatch.setattr(orchestration, "_publish_and_wait_for_harness_elicitation", approve)
    for _ in range(2):
        authorized = await gate.authorize_permission_call(
            gate.InspectedPermissionCall(binding, decision),
            request=None,
            harness="pi-native",
            arguments=arguments,
            conversation_store=None,
            runner_router=None,
        )
        assert authorized.action.value == "DENY"
    assert cards == []
    assert decision.action.value == "DENY"
    assert decision.rule_id == "native-proof"
    # Even a direct attempted approval cannot mint an execution lease.
    assert not runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
    )
    assert not runtime.verify_execute(
        "session-a",
        call_id=binding.call_id,
        revision=3,
        arguments_digest=binding.arguments_digest,
        name=binding.name,
        arguments=arguments,
        operation_id="native-launch",
        step="launch",
    )


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
    ("tier", "expected"),
    [
        ("auto", "DENY"),
        ("ask", "DENY"),
        ("accept-edits", "DENY"),
        ("plan", "DENY"),
        ("full", "ALLOW"),
    ],
)
def test_legacy_pi_subagents_registry_cannot_replace_native_proof(tmp_path, tier, expected):
    runtime, source, registry = _extension_runtime(tmp_path, tier, name="subagent")
    (source.parent / "package.json").write_text(
        json.dumps({"name": "pi-subagents", "version": "0.79.0"})
    )
    registry["tools"][0]["sourceInfo"]["source"] = "npm:pi-subagents@0.79.0"
    (runtime.path("session-a").parent / "permission_tools.json").write_text(json.dumps(registry))
    binding, result = runtime.inspect(
        "session-a", call_id="legacy-subagent", name="subagent", arguments={}, expected_revision=3
    )
    assert result.action.value == expected, result.reason
    if expected != "DENY":
        assert runtime.authorize(
            "session-a",
            call_id=binding.call_id,
            name=binding.name,
            arguments_digest=binding.arguments_digest,
            expected_revision=3,
        )


@pytest.mark.parametrize("version", ["0.79.0", "0.80.0"])
def test_scoped_pi_subagents_registry_still_requires_native_exports(tmp_path, version):
    runtime, source, registry = _extension_runtime(tmp_path, "auto", name="subagent")
    (source.parent / "package.json").write_text(
        json.dumps({"name": "pi-subagents", "version": version})
    )
    registry["tools"][0]["sourceInfo"]["source"] = "npm:pi-subagents@" + version
    parent = runtime.begin_pi_scope_launch("session-a")
    _write_scope_registry(parent, registry["tools"])
    assert not runtime.acknowledge_pi_scope("session-a", parent.credential)


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
        package_root / "runner" / "pi_scopes.py",
        package_root / "runner" / "pi_scope_relay.py",
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
@pytest.mark.parametrize("scoped", [False, True])
async def test_mcp_lease_finishes_in_actual_execution_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raises: bool,
    with_operation: bool,
    scoped: bool,
) -> None:
    from omnigent.runner import tool_dispatch

    token = None
    if scoped:
        runtime, _parent, _child, _source, _registry = _scoped_runtime(tmp_path, "ask")
        child = runtime.open_pi_child_scope("session-a")
        bridge_tools = [
            {
                "name": "sys_os_read",
                "sourceInfo": {
                    "path": str(
                        runtime.path("session-a").parent / "omnigent_pi_native_extension.js"
                    ),
                },
            }
        ]
        _publish_scope(runtime, child, bridge_tools)
        receipt = runtime.pi_origin_receipt(
            "session-a",
            child.credential,
            call_id="tool-a",
            name="sys_os_read",
            arguments={},
            local_execution=False,
        )
        token = receipt.token
        call_id = receipt.binding.call_id
    else:
        runtime = _runtime(tmp_path, "ask")
        call_id = "tool-a"
    arguments: dict[str, object] = {}
    binding, decision = runtime.inspect(
        "session-a",
        call_id=call_id,
        name="sys_os_read",
        arguments=arguments,
        expected_revision=3,
        local_execution=False,
        origin_receipt=token,
    )
    assert decision.action.value == "ASK"
    authorized = runtime.authorize(
        "session-a",
        call_id=binding.call_id,
        name=binding.name,
        arguments_digest=binding.arguments_digest,
        expected_revision=3,
        origin_receipt=token,
    )
    assert authorized
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
            _session_harness_name=lambda _session_id: (
                "pi-native" if scoped else "antigravity-native"
            ),
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
        if token is not None:
            request["_omnigent_permission"]["pi_origin_receipt"] = token
        if scoped:
            for fault in ("registry-replacement", "malformed-registration", "missing-bootstrap"):
                rejected_child = runtime.open_pi_child_scope("session-a")
                if fault == "missing-bootstrap":
                    _write_scope_registry(rejected_child, bridge_tools)
                    assert runtime.acknowledge_pi_scope("session-a", rejected_child.credential)
                else:
                    _publish_scope(runtime, rejected_child, bridge_tools)
                rejected_receipt = runtime.pi_origin_receipt(
                    "session-a",
                    rejected_child.credential,
                    call_id="rejected-call",
                    name="sys_os_read",
                    arguments=arguments,
                    local_execution=False,
                )
                rejected_binding, _ = runtime.inspect(
                    "session-a",
                    call_id=rejected_receipt.binding.call_id,
                    name="sys_os_read",
                    arguments=arguments,
                    expected_revision=3,
                    local_execution=False,
                    origin_receipt=rejected_receipt.token,
                )
                path = rejected_child.registry_dir / "permission_tools.json"
                payload = json.loads(path.read_text())
                if fault != "missing-bootstrap":
                    payload["tools"][0]["sourceInfo"] = (
                        {"path": "<inline:omnigent-permission>"}
                        if fault == "registry-replacement"
                        else "broken"
                    )
                    path.write_text(json.dumps(payload))
                allowed = runtime.authorize(
                    "session-a",
                    call_id=rejected_binding.call_id,
                    name=rejected_binding.name,
                    arguments_digest=rejected_binding.arguments_digest,
                    expected_revision=3,
                    origin_receipt=rejected_receipt.token,
                )
                rejected = await client.post(
                    "/v1/sessions/session-a/mcp/execute",
                    json={
                        **request,
                        "_omnigent_permission": {
                            "call_id": rejected_binding.call_id,
                            "revision": 3,
                            "arguments_digest": rejected_binding.arguments_digest,
                            "pi_origin_receipt": rejected_receipt.token,
                        },
                    },
                )
                assert calls == 0, "invalid registration must never reach the tool backend"
                assert not allowed
                assert "error" in rejected.json()
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
        if scoped and with_operation:
            sibling = runtime.open_pi_child_scope("session-a")
            _publish_scope(runtime, sibling, bridge_tools)
            sibling_receipt = runtime.pi_origin_receipt(
                "session-a",
                sibling.credential,
                call_id="tool-a",
                name="sys_os_read",
                arguments=arguments,
                local_execution=False,
            )
            sibling_binding, _ = runtime.inspect(
                "session-a",
                call_id=sibling_receipt.binding.call_id,
                name="sys_os_read",
                arguments=arguments,
                expected_revision=3,
                local_execution=False,
                origin_receipt=sibling_receipt.token,
            )
            sibling_allowed = runtime.authorize(
                "session-a",
                call_id=sibling_binding.call_id,
                name=sibling_binding.name,
                arguments_digest=sibling_binding.arguments_digest,
                expected_revision=3,
                origin_receipt=sibling_receipt.token,
            )
            assert sibling_allowed
            sibling_request = {
                **request,
                "_omnigent_permission": {
                    "call_id": sibling_binding.call_id,
                    "revision": 3,
                    "arguments_digest": sibling_binding.arguments_digest,
                    "pi_origin_receipt": sibling_receipt.token,
                },
            }
            second = await client.post("/v1/sessions/session-a/mcp/execute", json=sibling_request)
            assert ("error" in second.json()) is raises
            assert calls == 2, "a sibling must not receive another scope's cached result"


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
