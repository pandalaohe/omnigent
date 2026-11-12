"""Private attestor transport uses the actual relay and existing principal registry."""

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from concurrent.futures import Future
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import _wait_for_pi_bootstrap_frame, start_tool_relay
from omnigent.harnesses.pi_cli import pi_cli_argv
from omnigent.permission_tiers import NativeCallBinding
from omnigent.permission_tiers import _native_digest as native_digest
from omnigent.runner.pi_bootstrap import process_identity
from omnigent.runner.pi_scope_relay import PiScopeRelay
from omnigent.runner.session_init_protocol import (
    RunnerSessionInitEnvelope,
    RunnerSessionInitSnapshot,
)
from omnigent.server.permission_tiers import ConfirmedPermissionState
from tests._helpers.polling import poll_until_async
from tests.runner.test_permission_tier_runtime import (
    _extension_runtime,
    _scope_call,
    _write_scope_registry,
)


def test_scoped_node_waits_for_policy_and_rpc():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for scoped transport proof")
    module = Path(__file__).resolve().parents[2] / "omnigent/resources/pi_native/scoped_client.cjs"
    script = r"""
const assert=require('node:assert/strict'), http=require('node:http');
const crypto=require('node:crypto');
const {connectAttestor}=require(process.argv[1]);
const scope={scope_id:'scope',installation_id:'install',
  runner_epoch:'runner',launch_epoch:'epoch'};
const server=http.createServer((req,res)=>{
  if(req.url==='/pi/bootstrap/watch') {
    res.writeHead(200);
    res.write(JSON.stringify({incarnation:'broker',launch_epoch:'epoch',
      live:true,revoked_scopes:[]})+'\n');return;
  }
  let body='';req.on('data',chunk=>body+=chunk);req.on('end',()=>{
    const input=JSON.parse(body);
    const reply=value=>res.end(JSON.stringify(value));
    if(req.url==='/policies/evaluate') {
      const id=input.event.data.call_id;
      setTimeout(()=>reply(id==='deny'
        ? {result:'POLICY_ACTION_DENY',reason:'Policy blocked this call'} : {
        result:'POLICY_ACTION_ALLOW',permission_tier_binding:{
          call_id:'pi:'+crypto.createHash('sha256').update('scope\0'+id).digest('hex'),
          revision:3,arguments_digest:'a'.repeat(64),pi_origin_receipt:'pi_receipt_'+'a'.repeat(32)
        }
      }),2200);
    } else if(req.url==='/pi/rpc') setTimeout(()=>reply({result:'tool completed'}),2200);
    else if(req.url==='/permission-tiers/control') reply({executed:true});
    else reply({});
  });
});
server.listen(0,'127.0.0.1',async()=>{
  let connection;
  try {
    connection=await connectAttestor({endpoint:`http://127.0.0.1:${server.address().port}`,
      credential:'test',brokerIncarnation:'broker',launchEpoch:'epoch'});
    const client=connection.client({scope,credential:'test'},'handle',
      ()=>({status:'ready',revision:3}),async()=>({}));
    const event=id=>({toolCallId:id,toolName:'read',input:{}});
    const [denied,allowed]=await Promise.all([
      client.evaluate(event('deny'),null,{}),client.evaluate(event('allow'),null,{})]);
    assert.deepEqual(denied,{block:true,reason:'Policy blocked this call'});
    assert.deepEqual(allowed,{block:false});
    const result=await client.execute(event('allow'),null,()=>client.bridge(event('allow'),0));
    assert.deepEqual(result,{result:'tool completed'});
    console.log('slow policy and rpc passed');
  } catch(error) {console.error(error.message);process.exitCode=1;}
  finally {if(connection)connection.close();server.close();}
});
"""
    result = subprocess.run(
        [node, "-e", script, str(module)], capture_output=True, text=True, timeout=12
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "slow policy and rpc passed"


@pytest.fixture
async def attestor_relay(tmp_path, monkeypatch):
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
    monkeypatch.setenv("OMNIGENT_BRIDGE_BIND_HOST", "127.0.0.1")
    runtime, source, registry = _extension_runtime(tmp_path, "auto")
    side = PiScopeRelay(runtime, "session-a")
    relay = start_tool_relay(
        bridge_dir=runtime.path("session-a").parent,
        tools=[],
        tool_executor=lambda *_: None,
        loop=asyncio.get_running_loop(),
        session_id="session-a",
        pi_scope_relay=side,
    )
    try:
        yield runtime, side.bootstrap, relay, source, registry
    finally:
        relay.close()


async def bind_ready(runtime, broker, relay, pid=None):
    pid = os.getpid() if pid is None else pid
    identity = process_identity(pid)
    assert identity is not None
    actor = broker.register_process(pid, expected_identity=identity, root=True)
    async with httpx.AsyncClient(base_url=relay._advertised_url, trust_env=False) as client:
        headers = {"Authorization": "Bearer " + actor.token}
        body = {
            "protocol": 1,
            "sequence": 1,
            "op": "bind",
            "loader": "root-loader",
            "handle": "root-manager",
        }
        opened = await client.post("/pi/bootstrap", headers=headers, json=body)
        assert opened.status_code == 200
        assert opened.json()["ready"] is False
        admission = actor.installations["root-manager"]
        _write_scope_registry(
            admission,
            [{"name": "read", "sourceInfo": {"source": "builtin", "path": "builtin:read"}}],
        )
        registry = json.loads((admission.registry_dir / "permission_tools.json").read_text())
        published = await client.post(
            "/pi/bootstrap", headers=headers, json={**body, "sequence": 2, "registry": registry}
        )
        assert published.status_code == 200
        assert published.json()["ready"] is True
    return actor, admission


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fault", ["none", "other-path", "missing-loaded", "changed-loaded", "changed-file"]
)
async def test_scoped_registry_accepts_only_the_configured_bridge_file(
    attestor_relay, tmp_path, fault
):
    from omnigent.permission_tiers.pi_tools import read_pi_tool_registry

    runtime, broker, _relay, _source, _registry = attestor_relay
    parent_expected = json.loads(
        (runtime.path("session-a").parent / "permission_expected.json").read_text()
    )
    configured = Path(parent_expected["extension_path"])
    source = configured
    if fault == "other-path":
        source = tmp_path / "other-bridge.js"
        source.write_bytes(configured.read_bytes())
    actor = broker.register_process(
        os.getpid(), expected_identity=process_identity(os.getpid()), root=True
    )
    broker.handle(
        actor, {"protocol": 1, "sequence": 1, "op": "bind", "loader": "root", "handle": "root"}
    )
    admission = actor.installations["root"]
    _write_scope_registry(
        admission,
        [
            {
                "name": "fixture__echo",
                "sourceInfo": {
                    "path": str(source),
                    "source": "cli",
                    "scope": "temporary",
                    "origin": "top-level",
                },
                "real_path": str(source),
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            }
        ],
    )
    path = admission.registry_dir / "permission_tools.json"
    published = json.loads(path.read_text())
    if fault == "missing-loaded":
        published["loaded_extensions"] = [
            row for row in published["loaded_extensions"] if row["path"] != str(source)
        ]
    elif fault == "changed-loaded":
        published["loaded_extensions"][-1]["source_sha256"] = "0" * 64
    elif fault == "changed-file":
        configured.write_text("// changed bridge\n")
    path.write_text(json.dumps(published))
    record = runtime.pi_scopes.record(admission.principal)
    registry = read_pi_tool_registry(
        admission.registry_dir, runtime.state("session-a"), expected_binding=record.expected
    )
    if fault == "none":
        assert not registry.reason and not registry.invalid_tools, registry
        assert registry.bridge_tools == frozenset({"fixture__echo"})
        assert runtime.acknowledge_pi_scope("session-a", admission.credential)
    else:
        assert registry.reason or registry.invalid_tools
        assert not runtime.acknowledge_pi_scope("session-a", admission.credential)


@pytest.mark.parametrize(
    ("path", "accepted"),
    [("builtin:tool-search", True), ("builtin:other-search", False)],
)
def test_scoped_registry_accepts_only_observed_tool_search_alias(tmp_path, path, accepted):
    runtime, _source, _registry = _extension_runtime(tmp_path, "auto")
    admission = runtime.begin_pi_scope_launch("session-a", native_guard=True)
    _write_scope_registry(
        admission,
        [{"name": "tool_search", "sourceInfo": {"source": "builtin", "path": path}}],
    )
    assert runtime.acknowledge_pi_scope("session-a", admission.credential) is accepted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "publication", ["pending", "ready", "changed-registry", "revoked", "state-write-retry"]
)
async def test_scoped_startup_keeps_root_blocked_until_complete_registration(
    attestor_relay, monkeypatch, publication
):
    runtime, broker, _relay, _source, _registry = attestor_relay
    path = runtime.path("session-a")
    monkeypatch.setattr(runtime, "state_path", lambda *_: path)
    envelope = RunnerSessionInitEnvelope(
        protocol_version=2,
        server_version="test",
        session_id="session-a",
        agent_id="agent",
        snapshot=RunnerSessionInitSnapshot(
            created_at=1,
            updated_at=1,
            host_id="host-a",
            workspace=runtime.state("session-a").workspace,
            labels=ConfirmedPermissionState("auto", 3, "blocked").labels("pi-native"),
        ),
    )
    runtime.fence_start(envelope, "pi-native")
    expected = json.loads((path.parent / "permission_expected.json").read_text())
    (path.parent / "permission_installation.json").write_text(
        json.dumps(
            {
                **expected,
                "tier": "auto",
                "revision": 3,
                "tools_verified": True,
                "active_tools": ["read"],
            }
        )
    )
    identity = process_identity(os.getpid())
    actor = broker.register_process(os.getpid(), expected_identity=identity, root=True)
    bind = {"protocol": 1, "op": "bind", "loader": "root-loader", "handle": "root"}
    assert broker.handle(actor, {**bind, "sequence": 1})["ready"] is False
    admission = actor.installations["root"]
    if publication != "pending":
        _write_scope_registry(
            admission,
            [{"name": "read", "sourceInfo": {"source": "builtin", "path": "builtin:read"}}],
        )
        registry_path = admission.registry_dir / "permission_tools.json"
        registry = json.loads(registry_path.read_text())
        assert broker.handle(actor, {**bind, "sequence": 2, "registry": registry})["ready"] is True
        if publication == "changed-registry":
            registry_path.write_text(json.dumps({**registry, "tools": []}))
        elif publication == "revoked":
            runtime.revoke_pi_scope(admission.principal)
        elif publication == "state-write-retry":
            from omnigent.runner import permission_tiers as permission_module

            write = permission_module.write_host_permission_state

            def fail_write(*_args):
                raise OSError("fixture state write failed")

            monkeypatch.setattr(permission_module, "write_host_permission_state", fail_write)
            with pytest.raises(OSError, match="fixture state write failed"):
                runtime.install(envelope, "pi-native")
            assert runtime.pi_scopes.preparing("session-a")
            monkeypatch.setattr(permission_module, "write_host_permission_state", write)
            registry_path.write_text(json.dumps({**registry, "tools": []}))
    assert runtime.state("session-a").status == "blocked"
    with pytest.raises(ValueError):
        runtime.pi_origin_receipt(
            "session-a",
            admission.credential,
            call_id="early",
            name="read",
            arguments={"path": "notes.txt"},
            local_execution=True,
        )
    with pytest.raises(ValueError):
        runtime.inspect(
            "session-a",
            call_id="early",
            name="read",
            arguments={"path": "notes.txt"},
            expected_revision=3,
        )
    if publication == "ready":
        assert runtime.install(envelope, "pi-native").status == "ready"
        assert runtime.pi_scopes.authenticate("session-a", admission.credential).status == "ready"
        with pytest.raises(ValueError):
            runtime.inspect(
                "session-a",
                call_id="legacy",
                name="read",
                arguments={"path": "notes.txt"},
                expected_revision=3,
            )
    else:
        with pytest.raises(ValueError, match="Pi scope"):
            runtime.install(envelope, "pi-native")
        assert runtime.state("session-a").status == "blocked"


@pytest.mark.asyncio
async def test_root_launcher_control_rejects_scope_authority_and_stale_generations(attestor_relay):
    from omnigent.harnesses.pi_native.bridge import prepare_permission_installation

    runtime, broker, relay, _source, _registry = attestor_relay
    runtime._harnesses["session-a"] = "pi"
    bridge = runtime.path("session-a").parent
    prepare_permission_installation(
        bridge,
        session_id="session-a",
        harness="pi",
        extension=bridge / "omnigent_pi_native_extension.js",
    )
    descriptor = broker.launcher_descriptor()
    actor, admission = await bind_ready(runtime, broker, relay)
    decoder = relay._httpd.RequestHandlerClass._read_json_body
    decoded = []

    def decode(handler):
        decoded.append(True)
        return decoder(handler)

    relay._httpd.RequestHandlerClass._read_json_body = decode
    path = "/pi/bootstrap/launcher"
    headers = {"Authorization": "Bearer " + descriptor["process_capability"]}
    body = {
        "protocol": 1,
        "op": "reserve",
        "launch_id": "a" * 32,
    }
    async with httpx.AsyncClient(base_url=relay._advertised_url, trust_env=False) as client:
        for token in ["invalid", admission.credential, actor.token, relay._relay_info["token"]]:
            response = await client.post(
                path, content=b"not JSON", headers={"Authorization": "Bearer " + token}
            )
            assert response.status_code == 403
        assert decoded == []
        first = await client.post(path, headers=headers, json=body)
        repeated = await client.post(path, headers=headers, json=body)
        assert first.status_code == repeated.status_code == 200
        assert first.json() == repeated.json()
        assert set(first.json()) == {"handoff", "launch_id"}
        identity = process_identity(os.getpid())
        publish = {**body, "op": "publish", "pid": identity[0], "started_at": identity[1]}
        for _ in range(2):
            response = await client.post(path, headers=headers, json=publish)
            assert response.status_code == 200
            assert response.json()["published"] is True
        response = await client.post(path, headers=headers, json={**body, "op": "confirm"})
        assert response.status_code == 200
        assert response.json()["ready"] is False
        other = {**body, "launch_id": "b" * 32}
        assert (await client.post(path, headers=headers, json=other)).status_code == 403
        assert (
            await client.post(path, headers=headers, json={**body, "op": "revoke"})
        ).status_code == 200
        reserved = await client.post(path, headers=headers, json=other)
        assert reserved.status_code == 200
        assert (
            await client.post(path, headers=headers, json={**body, "op": "revoke"})
        ).status_code == 403
        assert (await client.post(path, headers=headers, json=other)).json() == reserved.json()
        broker.launcher_descriptor()
        before = len(decoded)
        assert (await client.post(path, headers=headers, content=b"not JSON")).status_code == 403
        assert len(decoded) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "windows",
    [
        pytest.param(
            False,
            marks=pytest.mark.skipif(os.name == "nt", reason="POSIX no-follow flags required"),
        ),
        True,
    ],
)
@pytest.mark.parametrize(
    "changed_field", [None, "st_ctime_ns", "st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns"]
)
async def test_root_launcher_checks_registration_identity(
    attestor_relay, monkeypatch, windows, changed_field
):
    from omnigent.permission_tiers import pi_tools

    _runtime, broker, relay, _source, _registry = attestor_relay
    descriptor = broker.launcher_descriptor()
    monkeypatch.setattr(pi_tools, "IS_WINDOWS", windows)

    def fstat(fd):
        info = os.fstat(fd)
        fields = {
            name: getattr(info, name)
            for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        }
        if changed_field is not None:
            fields[changed_field] += 1
        return SimpleNamespace(**fields)

    monkeypatch.setattr(pi_tools, "os", SimpleNamespace(**{**vars(os), "fstat": fstat}))
    identity = process_identity(os.getpid())
    assert identity is not None
    body = {"protocol": 1, "op": "reserve", "launch_id": "a" * 32}
    headers = {"Authorization": "Bearer " + descriptor["process_capability"]}
    async with httpx.AsyncClient(base_url=relay._advertised_url, trust_env=False) as client:
        reserved = await client.post("/pi/bootstrap/launcher", headers=headers, json=body)
        assert reserved.status_code == 200
        response = await client.post(
            "/pi/bootstrap/launcher",
            headers=headers,
            json={**body, "op": "publish", "pid": identity[0], "started_at": identity[1]},
        )
    accepted = changed_field is None or (windows and changed_field == "st_ctime_ns")
    assert response.status_code == (200 if accepted else 403)
    if accepted:
        assert response.json()["published"] is True


@pytest.mark.parametrize("windows", [False, True])
def test_registration_rejects_ctime_change_during_read(tmp_path, monkeypatch, windows):
    from omnigent.permission_tiers import pi_tools

    path = tmp_path / "registration.json"
    path.write_bytes(b"{}")
    monkeypatch.setattr(pi_tools, "IS_WINDOWS", windows)
    reads = 0

    def fstat(fd):
        nonlocal reads
        info = os.fstat(fd)
        reads += 1
        fields = {
            name: getattr(info, name)
            for name in ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        }
        fields["st_ctime_ns"] = path.stat().st_ctime_ns + (reads if windows else reads - 1)
        return SimpleNamespace(**fields)

    monkeypatch.setattr(pi_tools, "os", SimpleNamespace(**{**vars(os), "fstat": fstat}))
    with pytest.raises(ValueError, match="Registration changed during read"):
        pi_tools._read_file(path)


@pytest.mark.asyncio
async def test_real_rpc_launcher_rebinds_fresh_process_without_unlocking_startup(
    attestor_relay, monkeypatch
):
    from omnigent.inner.pi_executor import PiExecutor

    node, pi = shutil.which("node"), shutil.which("pi")
    if not node or not pi:
        pytest.skip("installed Pi SDK and Node required")
    sdk = next(
        (
            parent
            for parent in Path(pi_cli_argv(pi)[-1]).resolve().parents
            if (parent / "package.json").is_file() and (parent / "dist/index.js").is_file()
        ),
        None,
    )
    if sdk is None or json.loads((sdk / "package.json").read_text()).get("version") not in (
        "1.3.0",
        "1.5.0",
    ):
        pytest.skip("a reviewed Pi SDK (1.3.0 or 1.5.0) is required")
    runtime, broker, _relay, _source, _registry = attestor_relay
    permission = runtime.state("session-a")
    path = runtime.path("session-a")
    bridge = path.parent
    monkeypatch.setattr(runtime, "state_path", lambda *_: path)
    envelope = RunnerSessionInitEnvelope(
        protocol_version=2,
        server_version="test",
        session_id="session-a",
        agent_id="agent",
        snapshot=RunnerSessionInitSnapshot(
            created_at=1,
            updated_at=1,
            host_id="host-a",
            workspace=permission.workspace,
            labels=ConfirmedPermissionState("auto", 3, "blocked").labels("pi"),
        ),
    )
    runtime.fence_start(envelope, "pi")
    monkeypatch.setattr("omnigent.inner.pi_executor.bridge_dir_for_session_id", lambda _: bridge)
    monkeypatch.setattr("omnigent.inner.pi_executor.prepare_bridge_dir", lambda _: bridge)
    executor = PiExecutor(
        cwd=permission.workspace, pi_path=pi, context_files=False, permission_controlled=True
    )
    try:
        await executor.configure_scoped_bootstrap("session-a", broker.launcher_descriptor())
        assert await executor.bootstrap_permission_tier("session-a")
        first = executor._session_states["session-a"].rpc
        first_scope = runtime.pi_scopes.parent_scope("session-a").admission
        assert runtime.state("session-a").status == "blocked"
        assert runtime.pi_scopes.record(first_scope.principal).status == "ready"
        with pytest.raises(ValueError):
            runtime.pi_origin_receipt(
                "session-a",
                first_scope.credential,
                call_id="early",
                name="read",
                arguments={"path": "notes.txt"},
                local_execution=True,
            )
        assert first.process is not None
        old_identity = process_identity(first.process.pid)
        assert runtime.install(envelope, "pi").status == "ready"
        replacement = await executor._ensure_rpc("session-a", "Fresh root instructions", None, [])
        assert replacement is not first
        assert first.process is None
        assert replacement.process is not None
        assert process_identity(replacement.process.pid) != old_identity
        current = runtime.pi_scopes.parent_scope("session-a").admission
        assert current.principal.launch_epoch != first_scope.principal.launch_epoch
        assert current.credential != first_scope.credential
        response = await replacement.request(
            {"type": "get_state", "id": "ready"}, expected="get_state"
        )
        assert response is not None
        with pytest.raises(ValueError):
            runtime.pi_scopes.authenticate("session-a", first_scope.credential)
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_runner_initializes_and_rebinds_real_scoped_pi_harness(tmp_path, monkeypatch):
    from omnigent.inner import pi_harness
    from omnigent.inner.pi_executor import PiExecutor
    from tests.runner.conftest import _build_app_for_spec
    from tests.runner.helpers import permission_session_init_payload
    from tests.runner.native_helpers import _harness_spec

    pi = shutil.which("pi")
    if pi is None:
        pytest.skip("the pinned Pi SDK is required")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app, manager = await _build_app_for_spec(_harness_spec("pi"))
    executor = PiExecutor(
        cwd=str(workspace), pi_path=pi, context_files=False, permission_controlled=True
    )
    monkeypatch.setattr(pi_harness, "_build_pi_executor", lambda: executor)
    harness_app = pi_harness.create_app()
    harness_app.state.conversation_id = "production-pi"
    harness_app.state.harness_auth_token = "nonsecret-fixture"
    payload = permission_session_init_payload(
        {"session_id": "production-pi", "agent_id": "test-agent"},
        harness="pi",
        snapshot={"workspace": str(workspace)},
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness_app),
        base_url="http://harness",
        headers={"Authorization": "Bearer nonsecret-fixture"},
    ) as harness_client:
        manager._client = harness_client
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            try:
                response = await client.post("/v1/sessions", json=payload)
                assert response.status_code == 201, response.text
                runtime = app.state.permission_tiers
                first = runtime.pi_scopes.parent_scope("production-pi")
                assert first.status == "ready"
                assert runtime.state("production-pi").status == "ready"
                assert "process_capability" not in response.text
                assert "pi_attestor_" not in response.text
                rpc = executor._session_states["production-pi"].rpc
                assert rpc is not None and rpc.process is not None
                first_identity = process_identity(rpc.process.pid)
                response = await client.post("/v1/sessions", json=payload)
                assert response.status_code == 201, response.text
                current = runtime.pi_scopes.parent_scope("production-pi")
                assert current.status == "ready"
                assert (
                    current.admission.principal.launch_epoch
                    != first.admission.principal.launch_epoch
                )
                assert first.status == "revoked"
                replacement = executor._session_states["production-pi"].rpc
                assert replacement is not None and replacement.process is not None
                assert process_identity(replacement.process.pid) != first_identity
                assert (
                    await replacement.request(
                        {"type": "get_state", "id": "ready"}, expected="get_state"
                    )
                    is not None
                )
            finally:
                await client.delete("/v1/sessions/production-pi")
                await executor.close()


def _pi_turn_double(reply: str, order: list[str]):
    from omnigent.inner.pi_executor import _PiRpcSession

    class Writer:
        def __init__(self):
            self.data = []

        def write(self, data):
            self.data.append(data)

        async def drain(self):
            pass

        def close(self):
            pass

        async def wait_closed(self):
            pass

    class Process:
        def __init__(self):
            self.stdin = Writer()
            self.returncode = None

        async def wait(self):
            return 0

    rpc = _PiRpcSession()
    rpc.process = Process()
    for event in (
        {"type": "response", "success": True},
        {
            "type": "message_update",
            "assistantMessageEvent": {"type": "text_delta", "delta": reply},
        },
        {"type": "agent_end", "messages": []},
    ):
        rpc._line_queue.put_nowait(json.dumps(event))
    prompts = []
    started, release = asyncio.Event(), asyncio.Event()
    original_send = rpc.send_command

    async def send(command):
        if command.get("type") == "prompt":
            prompts.append(command)
            order.append("prompt")
            started.set()
            await release.wait()
        await original_send(command)

    rpc.send_command = send
    return rpc, prompts, started, release


async def _first_turn_before_init(tmp_path, monkeypatch, *, init_error: bool):
    from omnigent.inner import pi_harness
    from omnigent.inner.pi_executor import PiExecutor, PiPermissionBootstrapError
    from tests.runner.conftest import _build_app_for_spec, _runner_client
    from tests.runner.native_helpers import _harness_spec

    session = f"late-init-{'failure' if init_error else 'success'}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    app, manager = await _build_app_for_spec(_harness_spec("pi"))
    executor = PiExecutor(
        cwd=str(workspace), pi_path="/opt/bin/pi", context_files=False, permission_controlled=True
    )
    order = []
    rpc, prompts, prompt_started, release = _pi_turn_double("FIRST_TURN_OK", order)
    entered = asyncio.Event()

    async def external_process(*_args, **_kwargs):
        entered.set()
        return rpc

    monkeypatch.setattr(executor, "_ensure_rpc", external_process)
    if init_error:

        async def fail_bootstrap(_session_id):
            raise PiPermissionBootstrapError("missing_ack")

        monkeypatch.setattr(executor, "bootstrap_permission_tier", fail_bootstrap)
    monkeypatch.setattr(pi_harness, "_build_pi_executor", lambda: executor)
    harness_app = pi_harness.create_app()
    harness_app.state.conversation_id = session
    harness_app.state.harness_auth_token = "nonsecret-fixture"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=harness_app),
        base_url="http://harness",
        headers={"Authorization": "Bearer nonsecret-fixture"},
    ) as harness_client:
        manager._client = harness_client
        async with _runner_client(
            app, permission_harness="pi", permission_snapshot={"workspace": str(workspace)}
        ) as client:
            response = await client.post(
                f"/v1/sessions/{session}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": "test-agent",
                    "content": [{"type": "input_text", "text": "FIRST_TURN_OK"}],
                },
            )
            assert response.status_code == 202, response.text
            turn = app.state.active_turns[session]
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(prompt_started.wait(), timeout=0.2)
            try:
                initialized = await client.post(
                    "/v1/sessions", json={"session_id": session, "agent_id": "test-agent"}
                )
                if init_error:
                    assert initialized.status_code == 503, initialized.text
                else:
                    assert initialized.status_code == 201, initialized.text
                    order.append("ready")
                release.set()
                await asyncio.wait_for(turn, 2)
            finally:
                release.set()
                if not turn.done():
                    turn.cancel()
                await asyncio.gather(turn, return_exceptions=True)
            return app, entered, prompts, prompt_started, order


@pytest.mark.asyncio
async def test_late_initialization_completes_before_first_pi_turn(tmp_path, monkeypatch):
    app, entered, prompts, _prompt_started, order = await _first_turn_before_init(
        tmp_path, monkeypatch, init_error=False
    )
    assert entered.is_set()
    assert order == ["ready", "prompt"]
    assert [prompt["message"] for prompt in prompts] == ["FIRST_TURN_OK"]
    assert app.state.permission_tiers.state("late-init-success").status == "ready"


@pytest.mark.asyncio
async def test_late_initialization_failure_rejects_first_pi_turn_immediately(
    tmp_path, monkeypatch
):
    app, entered, prompts, _prompt_started, _order = await _first_turn_before_init(
        tmp_path, monkeypatch, init_error=True
    )
    assert not entered.is_set()
    assert prompts == []
    queue = app.state.session_event_queues["late-init-failure"]
    statuses = []
    while not queue.empty():
        event = queue.get_nowait()
        if event.get("type") == "session.status":
            statuses.append(event["status"])
    assert statuses[-1] == "failed"


def _init_task_registry(app):
    route = next(
        route for route in app.routes if route.path == "/v1/sessions" and "POST" in route.methods
    )
    cells = zip(
        route.endpoint.__code__.co_freevars,
        route.endpoint.__closure__,
        strict=False,
    )
    return next(cell.cell_contents for name, cell in cells if name == "_session_init_tasks")


def _superseded_init_registry(app):
    """The per-session superseded-init sets, or None when the runner keeps none."""
    route = next(
        route for route in app.routes if route.path == "/v1/sessions" and "POST" in route.methods
    )
    cells = zip(
        route.endpoint.__code__.co_freevars,
        route.endpoint.__closure__,
        strict=False,
    )
    return next(
        (cell.cell_contents for name, cell in cells if name == "_session_superseded_init_tasks"),
        None,
    )


async def _retired_init_task(outcome: str):
    if outcome == "cancelled":
        task = asyncio.create_task(asyncio.sleep(60))
        task.cancel()
    else:

        async def fail():
            raise RuntimeError("old init failed")

        task = asyncio.create_task(fail())
    await asyncio.gather(task, return_exceptions=True)
    return task


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["cancelled", "exception"])
async def test_new_init_generation_supersedes_old_failure(outcome):
    from tests.runner.conftest import _build_app_for_spec, _runner_client
    from tests.runner.native_helpers import _harness_spec

    session = f"generation-{outcome}"
    app, manager = await _build_app_for_spec(_harness_spec("codex"))
    registry = _init_task_registry(app)
    registry[(session, "old-agent", "old-subagent", "old-recovery")] = await _retired_init_task(
        outcome
    )
    async with _runner_client(app) as client:
        initialized = await client.post(
            "/v1/sessions", json={"session_id": session, "agent_id": "test-agent"}
        )
        assert initialized.status_code == 201, initialized.text
        # Initialization spawns the harness itself, so only the calls the turn
        # adds after the event show whether the turn was admitted.
        calls_before_turn = len(manager.get_client_calls)
        response = await client.post(
            f"/v1/sessions/{session}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": "test-agent",
                "content": [{"type": "input_text", "text": "NEW_GENERATION_OK"}],
            },
        )
        assert response.status_code == 202, response.text
        turn = app.state.active_turns[session]
        await asyncio.wait_for(asyncio.gather(turn, return_exceptions=True), 2)
    turn_calls = [call[0] for call in manager.get_client_calls[calls_before_turn:]]
    assert turn_calls == [session]
    statuses = [
        event["status"]
        for event in list(app.state.session_event_queues[session]._queue)
        if event.get("type") == "session.status"
    ]
    assert statuses[-1] == "idle"


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup", ["replacement", "delete"])
async def test_init_task_records_are_cleaned_on_replacement_and_delete(cleanup):
    from tests.runner.conftest import _build_app_for_spec, _runner_client
    from tests.runner.native_helpers import _harness_spec

    session = f"cleanup-{cleanup}"
    app, _manager = await _build_app_for_spec(_harness_spec("codex"))
    registry = _init_task_registry(app)
    for index, outcome in enumerate(("cancelled", "exception")):
        registry[(session, f"old-agent-{index}", None, None)] = await _retired_init_task(outcome)
    async with _runner_client(app) as client:
        if cleanup == "replacement":
            response = await client.post(
                "/v1/sessions", json={"session_id": session, "agent_id": "test-agent"}
            )
            assert response.status_code == 201, response.text
        else:
            response = await client.delete(f"/v1/sessions/{session}")
            assert response.status_code == 200, response.text
    assert not [key for key in registry if key[0] == session]


@pytest.mark.asyncio
async def test_delete_cancels_superseded_running_init():
    from tests.runner.conftest import _build_app_for_spec, _runner_client
    from tests.runner.native_helpers import _harness_spec

    session = "superseded-delete"
    app, _manager = await _build_app_for_spec(_harness_spec("codex"))
    registry = _init_task_registry(app)
    superseded = _superseded_init_registry(app)
    assert superseded is not None, "runner keeps no superseded-init registry"
    running = asyncio.create_task(asyncio.sleep(60))
    registry[(session, "old-agent", None, None)] = running
    async with _runner_client(app) as client:
        replacement = await client.post(
            "/v1/sessions", json={"session_id": session, "agent_id": "test-agent"}
        )
        assert replacement.status_code == 201, replacement.text
        # A superseded init that is still running stays reachable for teardown.
        assert running in superseded[session]
        assert not running.done()
        deleted = await client.delete(f"/v1/sessions/{session}")
        assert deleted.status_code == 200, deleted.text
    await asyncio.wait_for(asyncio.gather(running, return_exceptions=True), 2)
    assert running.cancelled()
    assert session not in superseded
    assert not [key for key in registry if key[0] == session]


@pytest.mark.parametrize(
    "fault", ["session", "remote", "port", "empty", "long", "nonstring", "extra"]
)
def test_python_launcher_rejects_unbound_or_nonloopback_descriptor(tmp_path, fault):
    from omnigent.inner.pi_executor import PiPermissionBootstrapError, _PiScopedLauncher

    descriptor = {
        "protocol": 1,
        "session_id": "session-a",
        "endpoint": "http://127.0.0.1:1",
        "process_capability": "pi_attestor_" + "a" * 43,
    }
    key, value = {
        "session": ("session_id", "other-session"),
        "remote": ("endpoint", "http://example.invalid:80"),
        "port": ("endpoint", "http://127.0.0.1:invalid"),
        "empty": ("process_capability", ""),
        "long": ("process_capability", "a" * 250),
        "nonstring": ("process_capability", None),
        "extra": ("origin", "parent"),
    }[fault]
    descriptor[key] = value
    with pytest.raises(PiPermissionBootstrapError) as rejected:
        _PiScopedLauncher("session-a", tmp_path, descriptor)
    assert rejected.value.code == "pi_permission_bootstrap_failed"
    assert rejected.value.reason == "Pi scoped launcher descriptor unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "category", "exception", "status"),
    [
        ("connect", "request", "ConnectError", None),
        ("timeout", "request", "ReadTimeout", None),
        ("forbidden", "http_status", "ValueError", 403),
        ("decode", "decode", "JSONDecodeError", 200),
        ("shape", "shape", "ValueError", 200),
        ("launch", "launch_id", "ValueError", 200),
    ],
)
async def test_windows_launcher_failure_diagnostics_exclude_private_data(
    tmp_path, monkeypatch, caplog, fault, category, exception, status
):
    from omnigent.inner import pi_executor

    monkeypatch.setattr(pi_executor, "IS_WINDOWS", True)
    private = "private-authority-and-response"
    launch_id = "a" * 32

    def respond(request):
        if fault == "connect":
            raise httpx.ConnectError(private, request=request)
        if fault == "timeout":
            raise httpx.ReadTimeout(private, request=request)
        if fault == "forbidden":
            return httpx.Response(403, text=private)
        if fault == "decode":
            return httpx.Response(200, text=private)
        return httpx.Response(200, json=[private] if fault == "shape" else {"launch_id": private})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        pi_executor.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    launcher = pi_executor._PiScopedLauncher(
        "session-a",
        tmp_path,
        {
            "protocol": 1,
            "session_id": "session-a",
            "endpoint": "http://127.0.0.1:1",
            "process_capability": private,
        },
    )
    with pytest.raises(pi_executor.PiPermissionBootstrapError) as rejected:
        await launcher.request("reserve", launch_id)
    assert rejected.value.reason == "Pi scoped launcher transport unavailable"
    assert f"category={category}" in caplog.text
    assert f"exception={exception}" in caplog.text
    assert f"status={status}" in caplog.text
    assert "operation=reserve" in caplog.text
    assert private not in caplog.text
    assert launch_id not in caplog.text
    assert "127.0.0.1" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("windows", [False, True])
async def test_python_launcher_real_loopback_and_broker_failure_diagnostics(
    attestor_relay, tmp_path, monkeypatch, caplog, windows
):
    from omnigent.inner import pi_executor
    from omnigent.runner import pi_bootstrap

    monkeypatch.setattr(pi_executor, "IS_WINDOWS", windows)
    monkeypatch.setattr(pi_bootstrap, "IS_WINDOWS", windows)
    runtime, broker, relay, _source, _registry = attestor_relay
    descriptor = broker.launcher_descriptor()
    launcher = pi_executor._PiScopedLauncher("session-a", tmp_path, descriptor)
    launch_id = "b" * 32
    reserved = await launcher.request("reserve", launch_id)
    assert Path(reserved["handoff"]).parent == runtime.path("session-a").parent / "pi-bootstrap"
    assert descriptor["endpoint"] == relay._advertised_url
    assert relay._httpd.socket.getsockname()[0] == "127.0.0.1"
    assert relay._httpd.socket.getsockname()[1] == int(relay._advertised_url.rsplit(":", 1)[1])
    with pytest.raises(pi_executor.PiPermissionBootstrapError):
        await launcher.request("publish", launch_id, pid="private-invalid-pid", started_at=0)
    assert "stage=operation exception=ValueError" in caplog.text
    assert "check=launcher_identity_fields exception=ValueError" in caplog.text
    assert "status=403" in caplog.text
    launcher._capability = "private-invalid-credential"
    with pytest.raises(pi_executor.PiPermissionBootstrapError):
        await launcher.request("reserve", launch_id)
    assert "stage=authentication exception=ValueError" in caplog.text
    assert "check=launcher_authentication exception=ValueError" in caplog.text
    assert "private-invalid" not in caplog.text
    assert descriptor["process_capability"] not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "check", "exception"),
    [
        ("reserve-io", "handoff_directory_io", "PermissionError"),
        ("process-identity", "process_identity_or_endpoint", "ValueError"),
        ("scope-expectation", "process_scope_launch", "JSONDecodeError"),
        ("handoff-write", "handoff_atomic_write", "PermissionError"),
        ("handoff-metadata", "handoff_destination_metadata", "PermissionError"),
        ("confirm-generation", "launcher_generation", "ValueError"),
    ],
)
async def test_windows_broker_refusal_names_check_without_values(
    attestor_relay, monkeypatch, caplog, fault, check, exception
):
    from omnigent.inner import pi_executor
    from omnigent.runner import pi_bootstrap

    monkeypatch.setattr(pi_executor, "IS_WINDOWS", True)
    monkeypatch.setattr(pi_bootstrap, "IS_WINDOWS", True)
    runtime, broker, _relay, _source, _registry = attestor_relay
    bridge = runtime.path("session-a").parent
    descriptor = broker.launcher_descriptor()
    launcher = pi_executor._PiScopedLauncher("session-a", bridge, descriptor)
    mkdir, replace, exists = Path.mkdir, os.replace, Path.exists
    private = "private-fault-details"

    def deny_directory(path, *args, **kwargs):
        if path.name == "pi-bootstrap":
            raise PermissionError(private)
        return mkdir(path, *args, **kwargs)

    def deny_handoff(source, destination, *args, **kwargs):
        if Path(destination).parent.name == "pi-bootstrap":
            raise PermissionError(private)
        return replace(source, destination, *args, **kwargs)

    def deny_metadata(path):
        if path.parent.name == "pi-bootstrap":
            raise PermissionError(private)
        return exists(path)

    if fault == "reserve-io":
        monkeypatch.setattr(Path, "mkdir", deny_directory)
        operation, values = "reserve", {}
        launch_id = "c" * 32
    else:
        launch_id, _locator = await launcher.reserve()
        operation = "publish"
        identity = process_identity(os.getpid())
        values = {"pid": identity[0], "started_at": identity[1]}
        if fault == "process-identity":
            values["started_at"] = 0
        elif fault == "scope-expectation":
            (bridge / "permission_expected.json").write_text(private)
        elif fault == "handoff-write":
            monkeypatch.setattr(os, "replace", deny_handoff)
        elif fault == "handoff-metadata":
            monkeypatch.setattr(Path, "exists", deny_metadata)
        else:
            launch_id, operation, values = "d" * 32, "confirm", {}
    with pytest.raises(pi_executor.PiPermissionBootstrapError):
        await launcher.request(operation, launch_id, **values)
    assert f"check={check} exception={exception}" in caplog.text
    assert "status=403" in caplog.text
    for value in (private, str(bridge), launch_id, descriptor["process_capability"]):
        assert value not in caplog.text


@pytest.mark.parametrize("binary_mode", [False, True])
def test_windows_scope_expectation_requires_exact_binary_source(
    tmp_path, monkeypatch, binary_mode
):
    from omnigent.harnesses.pi_native.bridge import prepare_permission_installation
    from omnigent.permission_tiers import pi_tools

    bridge = tmp_path / "bridge"
    source = tmp_path / "extension.js"
    source.write_bytes(b"// neutral fixture\r\nmodule.exports = {};\r\n")
    prepare_permission_installation(bridge, session_id="session-a", harness="pi", extension=source)
    original_open, original_read = os.open, os.read
    native_binary, simulated_binary = getattr(os, "O_BINARY", 0), 0x8000
    binary_fds = set()

    def open_crt(path, flags, *args, **kwargs):
        fd = original_open(path, (flags & ~simulated_binary) | native_binary, *args, **kwargs)
        if flags & simulated_binary:
            binary_fds.add(fd)
        else:
            binary_fds.discard(fd)
        return fd

    def read_crt(fd, size):
        data = original_read(fd, size)
        return data if fd in binary_fds else data.replace(b"\r\n", b"\n")

    monkeypatch.setattr(pi_tools, "IS_WINDOWS", True)
    monkeypatch.setattr(os, "O_BINARY", simulated_binary if binary_mode else 0, raising=False)
    monkeypatch.setattr(os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(os, "open", open_crt)
    monkeypatch.setattr(os, "read", read_crt)
    if binary_mode:
        expected = pi_tools.read_pi_tool_expectation(bridge)
        assert expected["extension_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    else:
        with pytest.raises(ValueError, match="Changed Pi enforcement source"):
            pi_tools.read_pi_tool_expectation(bridge)


@pytest.mark.asyncio
@pytest.mark.parametrize("ready", [False, True])
async def test_scoped_confirmation_uses_the_callers_remaining_deadline(
    tmp_path, monkeypatch, ready
):
    from types import SimpleNamespace

    from omnigent.inner.pi_executor import PiPermissionBootstrapError, _PiScopedLauncher

    launcher = _PiScopedLauncher(
        "session-a",
        tmp_path,
        {
            "protocol": 1,
            "session_id": "session-a",
            "endpoint": "http://127.0.0.1:1",
            "process_capability": "opaque-launcher-bearer",
        },
    )
    entered, release, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def delayed_confirm(*_args, **_kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"ready": True}

    monkeypatch.setattr(launcher, "request", delayed_confirm)
    rpc = SimpleNamespace(process=SimpleNamespace(returncode=None))
    deadline = asyncio.get_running_loop().time() + 0.05
    if ready:
        release.set()
        await launcher.confirm("a" * 32, rpc, deadline=deadline)
        assert entered.is_set()
        assert not cancelled.is_set()
    else:
        with pytest.raises(PiPermissionBootstrapError) as rejected:
            await launcher.confirm("a" * 32, rpc, deadline=deadline)
        assert rejected.value.code == "pi_permission_bootstrap_failed"
        assert rejected.value.reason == "Pi scoped process registration unavailable"
        assert entered.is_set() and cancelled.is_set()


@pytest.mark.asyncio
async def test_bootstrap_rejects_scope_and_forged_authority_before_payload_decode(attestor_relay):
    runtime, broker, relay, _source, _registry = attestor_relay
    actor, admission = await bind_ready(runtime, broker, relay)
    decoder = relay._httpd.RequestHandlerClass._read_json_body
    decoded = []

    def decode(handler):
        decoded.append(True)
        return decoder(handler)

    relay._httpd.RequestHandlerClass._read_json_body = decode
    async with httpx.AsyncClient(base_url=relay._advertised_url, trust_env=False) as client:
        for value in ["invalid", admission.credential, relay._relay_info["token"]]:
            response = await client.post(
                "/pi/bootstrap", content=b"not JSON", headers={"Authorization": "Bearer " + value}
            )
            assert response.status_code in (401, 403)
        assert decoded == []
        for value in ["invalid", relay._relay_info["token"]]:
            response = await client.post(
                "/pi/rpc", content=b"not JSON", headers={"Authorization": "Bearer " + value}
            )
            assert response.status_code == 403
        assert decoded == []
        malformed = await client.post(
            "/pi/bootstrap",
            content=b"not JSON",
            headers={"Authorization": "Bearer " + actor.token},
        )
        assert malformed.status_code == 400
        assert decoded == [True]
        unknown = await client.post(
            "/pi/bootstrap",
            headers={"Authorization": "Bearer " + actor.token},
            json={"protocol": 1, "sequence": 3, "op": "register_process", "pid": os.getpid()},
        )
        assert unknown.status_code == 403
    assert runtime.pi_scopes.record(admission.principal).status == "ready"


@pytest.mark.asyncio
async def test_runner_adapter_uses_minted_execution_kind_and_tcp_rejects_parent_token(
    attestor_relay,
):
    from omnigent.inner.pi_executor import _ToolServer

    runtime, broker, relay, _source, _registry = attestor_relay
    _actor, admission = await bind_ready(runtime, broker, relay)
    for local in [True, False]:
        receipt = runtime.pi_origin_receipt(
            "session-a",
            admission.credential,
            call_id=f"placement-{local}",
            name="read",
            arguments={"path": "notes.txt"},
            local_execution=local,
        )
        assert (
            runtime.pi_execution_kind("session-a", receipt.token, legacy_local_execution=not local)
            is local
        )
    with pytest.raises(ValueError):
        runtime.pi_execution_kind("session-a", None, legacy_local_execution=True)
    server = _ToolServer()
    server._permission_bridge_dir = runtime.path("session-a").parent
    server.scoped_required = True
    await server.start()
    try:
        for frame in [
            {
                "id": "forged",
                "kind": "pi_scoped",
                "credential": server.token,
                "payload": "not JSON",
            },
            {
                "id": "parent",
                "kind": "tool_call",
                "token": server.token,
                "tool": "read",
                "args": {},
            },
        ]:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            try:
                writer.write(json.dumps(frame).encode() + b"\n")
                await writer.drain()
                response = json.loads(await asyncio.wait_for(reader.readline(), 5))
                assert "error" in response
                assert "result" not in response
            finally:
                writer.close()
                await writer.wait_closed()
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_continuity_ends_on_launch_rebind_with_same_process_and_tier(attestor_relay, caplog):
    runtime, broker, relay, _source, _registry = attestor_relay
    actor, admission = await bind_ready(runtime, broker, relay)
    revision = runtime.state("session-a").revision
    async with httpx.AsyncClient(base_url=relay._advertised_url, trust_env=False) as client:
        async with client.stream(
            "GET", "/pi/bootstrap/watch", headers={"Authorization": "Bearer " + actor.token}
        ) as response:
            assert response.status_code == 200
            lines = response.aiter_lines()
            frame = json.loads(await anext(lines))
            assert frame["incarnation"] == broker.incarnation
            assert frame["launch_epoch"] == admission.principal.launch_epoch
            runtime.pi_scopes.invalidate("session-a")
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(lines), 3)
    assert actor.closed
    assert "Pi bootstrap watch ended: stage=continuity exception=ValueError" in caplog.text
    assert actor.token not in caplog.text
    assert admission.credential not in caplog.text
    assert runtime.state("session-a").revision == revision
    with pytest.raises(ValueError):
        broker.authenticate("Bearer " + actor.token)


@pytest.mark.asyncio
async def test_continuity_survives_runner_loop_delay_and_still_revokes(attestor_relay):
    import time

    runtime, broker, relay, _source, _registry = attestor_relay
    actor, _admission = await bind_ready(runtime, broker, relay)
    async with httpx.AsyncClient(base_url=relay._advertised_url, trust_env=False) as client:
        async with client.stream(
            "GET", "/pi/bootstrap/watch", headers={"Authorization": "Bearer " + actor.token}
        ) as response:
            assert response.status_code == 200
            lines = response.aiter_lines()
            assert json.loads(await anext(lines))["live"] is True
            # Permission inspection runs synchronously on this loop in production.
            time.sleep(2.5)
            for _ in range(5):
                frame = json.loads(await asyncio.wait_for(anext(lines), 3))
                assert frame["incarnation"] == broker.incarnation
                assert frame["live"] is True
            runtime.pi_scopes.invalidate("session-a")
            with pytest.raises(StopAsyncIteration):
                await asyncio.wait_for(anext(lines), 3)


@pytest.mark.asyncio
@pytest.mark.parametrize("validation_error", [False, True])
async def test_continuity_wait_reads_completion_after_timeout(validation_error):
    frame = {"live": True, "incarnation": "fixture"}

    class CompletingFuture(Future):
        def result(self, timeout=None):
            if not self.done():
                if validation_error:
                    self.set_exception(TimeoutError("validation failed"))
                else:
                    self.set_result(frame)
                raise TimeoutError("wait elapsed")
            return super().result(timeout)

    future = CompletingFuture()
    connection, peer = socket.socketpair()
    try:
        if validation_error:
            with pytest.raises(TimeoutError, match="validation failed"):
                _wait_for_pi_bootstrap_frame(future, asyncio.get_running_loop(), connection, None)
        else:
            assert (
                _wait_for_pi_bootstrap_frame(future, asyncio.get_running_loop(), connection, None)
                == frame
            )
    finally:
        connection.close()
        peer.close()


@pytest.mark.asyncio
async def test_continuity_wait_survives_two_minutes_of_scheduling_delay(monkeypatch):
    elapsed = 0.0
    frame = {"live": True, "incarnation": "fixture"}

    class DelayedFuture(Future):
        def result(self, timeout=None):
            nonlocal elapsed
            if timeout is not None and elapsed < 120:
                elapsed += 60
                raise TimeoutError("wait elapsed")
            self.set_result(frame)
            return super().result(timeout)

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.bridge.time",
        SimpleNamespace(monotonic=lambda: elapsed),
    )
    future = DelayedFuture()
    connection, peer = socket.socketpair()
    try:
        assert (
            _wait_for_pi_bootstrap_frame(future, asyncio.get_running_loop(), connection, None)
            == frame
        )
        assert elapsed == 120
        assert not future.cancelled()
    finally:
        connection.close()
        peer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["peer_exit", "loop_stop", "relay_close", "validation_error"])
async def test_pending_continuity_releases_validation(ending, monkeypatch):
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever)
    entered, released, closed = threading.Event(), threading.Event(), threading.Event()
    connection, peer = socket.socketpair()
    finish = asyncio.Event()

    async def validate():
        try:
            entered.set()
            await finish.wait()
            raise ValueError("validation failed")
        finally:
            released.set()

    thread.start()
    future = asyncio.run_coroutine_threadsafe(validate(), loop)
    waiting = threading.Event()
    result = future.result

    def observe_wait(timeout=None):
        waiting.set()
        return result(timeout)

    monkeypatch.setattr(future, "result", observe_wait)
    waiter = None
    try:
        await poll_until_async(entered.is_set)
        waiter = asyncio.create_task(
            asyncio.to_thread(_wait_for_pi_bootstrap_frame, future, loop, connection, closed)
        )
        await poll_until_async(waiting.is_set)
        if ending == "peer_exit":
            peer.close()
        elif ending == "relay_close":
            closed.set()
        elif ending == "loop_stop":
            loop.call_soon_threadsafe(loop.stop)
            await asyncio.to_thread(thread.join, 2)
            assert not thread.is_alive()
        elif ending == "validation_error":
            loop.call_soon_threadsafe(finish.set)
        failure = ValueError if ending == "validation_error" else ConnectionAbortedError
        message = "validation failed" if ending == "validation_error" else "Pi bootstrap"
        with pytest.raises(failure, match=message):
            await asyncio.wait_for(waiter, 2)
        if ending == "validation_error":
            assert future.done()
            assert isinstance(future.exception(), ValueError)
        else:
            assert future.cancelled()
    finally:
        future.cancel()
        closed.set()
        if waiter is not None:
            await asyncio.gather(waiter, return_exceptions=True)
        if loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        await asyncio.to_thread(thread.join, 2)

        def drain():
            loop.run_until_complete(asyncio.sleep(0))
            loop.close()

        await asyncio.to_thread(drain)
        connection.close()
        peer.close()
    assert released.is_set(), "the validation coroutine must observe cancellation"


@pytest.mark.asyncio
async def test_reload_replaces_installation_without_revoking_sibling(attestor_relay):
    runtime, broker, relay, _source, _registry = attestor_relay
    actor, parent = await bind_ready(runtime, broker, relay)
    bind = {"protocol": 1, "op": "bind"}
    broker.handle(actor, {**bind, "sequence": 3, "loader": "child-loader", "handle": "child"})
    child = actor.installations["child"]
    _write_scope_registry(
        child, [{"name": "read", "sourceInfo": {"source": "builtin", "path": "builtin:read"}}]
    )
    assert runtime.acknowledge_pi_scope("session-a", child.credential)
    broker.handle(
        actor, {**bind, "sequence": 4, "loader": "child-loader", "handle": "replacement"}
    )
    replacement = actor.installations["replacement"]
    assert replacement.principal.registry_generation != child.principal.registry_generation
    assert replacement.credential != child.credential
    assert child.principal.scope_id in broker.frame(actor)["revoked_scopes"]
    assert runtime.pi_scopes.record(child.principal).status == "revoked"
    assert runtime.pi_scopes.record(parent.principal).status == "ready"
    assert broker.authenticate("Bearer " + actor.token) is actor


@pytest.mark.asyncio
async def test_process_identity_loss_revokes_all_hosted_scopes(attestor_relay, monkeypatch):
    runtime, broker, relay, _source, _registry = attestor_relay
    actor, admission = await bind_ready(runtime, broker, relay)
    monkeypatch.setattr("omnigent.runner.pi_bootstrap.process_identity", lambda _pid: None)
    with pytest.raises(ValueError):
        broker.authenticate("Bearer " + actor.token)
    assert runtime.pi_scopes.record(admission.principal).status == "revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize("ending", ["broker_loss", "backstop"])
async def test_real_node_client_seals_before_local_evaluation_on_broker_loss(
    attestor_relay, handoff, ending, monkeypatch, caplog
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for private client consumer proof")
    runtime, broker, relay, _source, _registry = attestor_relay
    resource = (
        Path(__file__).resolve().parents[2] / "omnigent/resources/pi_native/scoped_client.cjs"
    )
    script = """
const readline = require('node:readline');
const http = require('node:http');
let posts = 0;
const request = http.request;
http.request = (...args) => { posts++; return request(...args); };
const { connectAttestor, connectFromHandoff } = require(process.argv[1]);
const lines = readline.createInterface({input:process.stdin});
lines.once('line', async line => {
  try {
    const descriptor = JSON.parse(line);
    const connection = descriptor.locator
      ? (await connectFromHandoff(descriptor.locator)).connection
      : await connectAttestor(descriptor);
    const client = connection.client(descriptor.binding, 'root-manager',
      () => ({status:'ready',revision:3}), async () => ({}));
    console.log('connected');
    lines.once('line', async () => {
      await new Promise(resolve => setTimeout(resolve, 400));
      const event = {toolCallId:'after-loss',toolName:'read',input:{}};
      const verdict = await client.evaluate(event, {}, {});
      console.log(JSON.stringify({verdict,posts,client}));
      connection.close(); lines.close(); process.stdin.destroy();
    });
  } catch (_) { console.log('private client failed'); process.exitCode=1; lines.close(); }
});
"""
    process = await asyncio.create_subprocess_exec(
        node,
        "-e",
        script,
        str(resource),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        actor, admission = await bind_ready(runtime, broker, relay, process.pid)
        descriptor = {
            "endpoint": broker.endpoint,
            "credential": actor.token,
            "brokerIncarnation": broker.incarnation,
            "launchEpoch": actor.launch_epoch,
            "binding": {"scope": asdict(admission.principal), "credential": admission.credential},
        }
        if handoff:
            locator = broker.reserve_handoff()
            broker.publish_handoff(actor, locator)
            descriptor["locator"] = str(locator)
        process.stdin.write(json.dumps(descriptor).encode() + b"\n")
        await process.stdin.drain()
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"connected\n"
        if ending == "broker_loss":
            runtime.pi_scopes.invalidate("session-a")
        else:
            from omnigent.harnesses.claude_native import bridge

            def expire_stalled_validation(future, loop, connection, closed):
                stalled = Future()
                clock = iter([0.0, bridge._PI_CONTINUITY_STUCK_BACKSTOP_S + 1])
                try:
                    with monkeypatch.context() as patch:
                        patch.setattr(
                            bridge,
                            "time",
                            SimpleNamespace(
                                **{**vars(bridge.time), "monotonic": lambda: next(clock)}
                            ),
                        )
                        return _wait_for_pi_bootstrap_frame(stalled, loop, connection, closed)
                finally:
                    future.cancel()
                    assert stalled.cancelled()

            monkeypatch.setattr(bridge, "_wait_for_pi_bootstrap_frame", expire_stalled_validation)
        process.stdin.write(b"evaluate\n")
        await process.stdin.drain()
        output, error = await asyncio.wait_for(process.communicate(), 5)
        if handoff:
            assert process.returncode == 1, (output, error)
            assert b"Scoped Pi authority transport lost" in error
            assert not output, "a launched Pi must stop instead of continuing without tools"
        else:
            assert process.returncode == 0, error.decode()
            result = json.loads(output)
            assert result["verdict"]["block"] is True
            assert result["posts"] == 0
        assert actor.token.encode() not in output + error
        assert admission.credential.encode() not in output + error
        if ending == "backstop":
            assert (
                "Pi bootstrap watch ended: stage=continuity exception=TimeoutError" in caplog.text
            )
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("windows", [False, True])
async def test_handoff_directory_mode_uses_host_permission_semantics(
    attestor_relay, monkeypatch, windows
):
    from types import SimpleNamespace

    from omnigent.runner import pi_bootstrap

    _runtime, broker, relay, _source, _registry = attestor_relay
    descriptor = broker.launcher_descriptor()
    monkeypatch.setattr(pi_bootstrap, "IS_WINDOWS", windows, raising=False)
    original = Path.lstat

    def windows_lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        return (
            SimpleNamespace(st_mode=info.st_mode | 0o077, st_uid=info.st_uid)
            if path.name == "pi-bootstrap"
            else info
        )

    with monkeypatch.context() as patch:
        patch.setattr(Path, "lstat", windows_lstat)
        async with httpx.AsyncClient(base_url=relay._advertised_url, trust_env=False) as client:
            response = await client.post(
                "/pi/bootstrap/launcher",
                headers={"Authorization": "Bearer " + descriptor["process_capability"]},
                json={"protocol": 1, "op": "reserve", "launch_id": "a" * 32},
            )
        assert response.status_code == (200 if windows else 403)
        if windows:
            assert Path(response.json()["handoff"]).parent.name == "pi-bootstrap"


@pytest.mark.asyncio
@pytest.mark.parametrize("windows", [False, True])
async def test_private_handoff_reaches_only_registered_node_and_is_consumed_once(
    attestor_relay, windows
):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for private handoff proof")
    _runtime, broker, _relay, _source, _registry = attestor_relay
    locator = broker.reserve_handoff()
    module = Path(__file__).resolve().parents[2] / "omnigent/resources/pi_native/scoped_client.cjs"
    script = """
const fs=require('node:fs');
if(process.argv[3]==='true') {
  Object.defineProperty(process,'platform',{value:'win32'});
  const mockFs={...fs,constants:{...fs.constants,O_NOFOLLOW:undefined}};
  for(const name of ['lstatSync','fstatSync']) {
    const original=fs[name];
    mockFs[name]=(...args)=>{const info=original(...args);info.mode|=0o077;return info;};
  }
  const Module=require('node:module'), originalLoad=Module._load;
  Module._load=function(name,...args) {
    return name==='node:fs'?mockFs:originalLoad.call(this,name,...args);
  };
}
const path=require('node:path');
require(path.join(path.dirname(process.argv[1]),'native_support.cjs')).snapshot(process.argv[1]);
require(process.argv[1]).connectFromHandoff(process.argv[2]).then(({connection}) => {
  connection.close(); console.log('consumed');
}).catch(() => {console.log('handoff refused');process.exitCode=1;});
"""
    process = await asyncio.create_subprocess_exec(
        node,
        "-e",
        script,
        str(module),
        str(locator),
        str(windows).lower(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        identity = process_identity(process.pid)
        assert identity is not None
        actor = broker.register_process(process.pid, expected_identity=identity, root=True)
        broker.publish_handoff(actor, locator)
        output, error = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, error.decode()
        assert output == b"consumed\n"
        assert actor.token.encode() not in output + error
        assert not locator.exists()
        with pytest.raises(ValueError):
            broker.publish_handoff(actor, locator)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["normal", "loss", "replacement", "missing-bootstrap", "incompatible"]
)
async def test_real_sdk_scoped_read_rpc_exception_and_jsonl_confine_receipts(mode):
    node, pi = shutil.which("node"), shutil.which("pi")
    if not node or not pi:
        pytest.skip("installed Pi SDK and Node required")
    sdk = next(
        (
            parent
            for parent in Path(pi_cli_argv(pi)[-1]).resolve().parents
            if (parent / "package.json").is_file() and (parent / "dist/index.js").is_file()
        ),
        None,
    )
    if sdk is None or json.loads((sdk / "package.json").read_text()).get("version") not in (
        "1.3.0",
        "1.5.0",
    ):
        pytest.skip("a reviewed Pi SDK (1.3.0 or 1.5.0) is required")
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).with_name("pi_scoped_sdk_consumer.py")),
        node,
        str(sdk),
        mode,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), 30)
        assert process.returncode == 0, (output + error).decode()
        assert b'"receiptExcluded":true' in output
        expected = (
            b'"evaluate": 0, "execute": 0, "finish": 0'
            if mode == "missing-bootstrap"
            else (
                b'"evaluate": 1, "execute": 1, "finish": 1'
                if mode == "incompatible"
                else (
                    b'"evaluate": 3, "execute": 3, "finish": 3'
                    if mode == "normal"
                    else b'"evaluate": 1, "execute": 1, "finish": 0'
                )
            )
        )
        assert expected in output
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_broker_proof_ack_is_call_specific_and_shared_with_actual_runtime(attestor_relay):
    runtime, broker, relay, _source, _registry = attestor_relay
    actor, admission = await bind_ready(runtime, broker, relay)
    args = {"path": "notes.txt"}
    _missing, _binding, denied = _scope_call(
        runtime, admission, call_id="before-ACK", arguments=args
    )
    assert denied.action.value == "DENY"
    principal = admission.principal
    binding = NativeCallBinding(
        principal.scope_id,
        principal.installation_id,
        principal.registry_generation,
        "read",
        native_digest(args),
        3,
    )
    expected = runtime.pi_scopes.record(principal).expected
    guard = Path(expected["extension_path"])
    fact = {
        "binding": asdict(binding),
        "status": "attested_native",
        "action": "ordinary",
        "inputs": [
            {
                "path": str(guard),
                "resolved_path": str(guard.resolve()),
                "sha256": hashlib.sha256(guard.read_bytes()).hexdigest(),
            }
        ],
        "semantics_digest": "",
    }
    async with httpx.AsyncClient(base_url=relay._advertised_url, trust_env=False) as client:
        result = await client.post(
            "/pi/bootstrap",
            headers={"Authorization": "Bearer " + actor.token},
            json={
                "protocol": 1,
                "sequence": 3,
                "op": "attest",
                "handle": "root-manager",
                "call_id": "A",
                "name": "read",
                "arguments": args,
                "revision": 3,
                "support": fact,
            },
        )
        assert result.status_code == 200
        assert result.json()["attested"] is True
    a, bound_a, allowed = _scope_call(runtime, admission, call_id="A", arguments=args)
    b, bound_b, denied = _scope_call(runtime, admission, call_id="B", arguments=args)
    assert allowed.action.value == "ALLOW"
    assert denied.action.value == "DENY"
    for receipt, binding, expected in [
        (b, bound_b, False),
        (a, bound_a, True),
        (b, bound_b, False),
    ]:
        actual = runtime.authorize(
            "session-a",
            call_id=binding.call_id,
            name="read",
            arguments_digest=binding.arguments_digest,
            expected_revision=3,
            origin_receipt=receipt.token,
        )
        assert actual is expected


@pytest.mark.asyncio
async def test_bootstrap_refuses_wildcard_listener_without_changing_legacy_relay(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)
    monkeypatch.setenv("OMNIGENT_BRIDGE_BIND_HOST", "0.0.0.0")
    runtime, _source, _registry = _extension_runtime(tmp_path, "auto")
    side = PiScopeRelay(runtime, "session-a")
    relay = start_tool_relay(
        bridge_dir=runtime.path("session-a").parent,
        tools=[],
        tool_executor=lambda *_: None,
        loop=asyncio.get_running_loop(),
        session_id="session-a",
        pi_scope_relay=side,
    )
    try:
        assert side.bootstrap.endpoint is None
        with pytest.raises(ValueError):
            identity = process_identity(os.getpid())
            assert identity is not None
            side.bootstrap.register_process(os.getpid(), expected_identity=identity, root=True)
        assert not runtime.pi_scope_protocol_active("session-a")
    finally:
        relay.close()
