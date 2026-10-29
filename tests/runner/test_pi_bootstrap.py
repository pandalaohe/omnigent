"""Private attestor transport uses the actual relay and existing principal registry."""

import asyncio
import hashlib
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import start_tool_relay
from omnigent.harnesses.pi_cli import pi_cli_argv
from omnigent.permission_tiers import NativeCallBinding
from omnigent.permission_tiers import _native_digest as native_digest
from omnigent.runner.pi_bootstrap import process_identity
from omnigent.runner.pi_scope_relay import PiScopeRelay
from tests.runner.test_permission_tier_runtime import (
    _extension_runtime,
    _scope_call,
    _write_scope_registry,
)


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
async def test_continuity_ends_on_launch_rebind_with_same_process_and_tier(attestor_relay):
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
    assert runtime.state("session-a").revision == revision
    with pytest.raises(ValueError):
        broker.authenticate("Bearer " + actor.token)


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
async def test_real_node_client_seals_before_local_evaluation_on_broker_loss(attestor_relay):
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
const { connectAttestor } = require(process.argv[1]);
const lines = readline.createInterface({input:process.stdin});
lines.once('line', async line => {
  try {
    const descriptor = JSON.parse(line);
    const connection = await connectAttestor(descriptor);
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
        process.stdin.write(json.dumps(descriptor).encode() + b"\n")
        await process.stdin.drain()
        assert await asyncio.wait_for(process.stdout.readline(), 5) == b"connected\n"
        runtime.pi_scopes.invalidate("session-a")
        process.stdin.write(b"evaluate\n")
        await process.stdin.drain()
        output, error = await asyncio.wait_for(process.communicate(), 5)
        assert process.returncode == 0, error.decode()
        result = json.loads(output)
        assert result["verdict"]["block"] is True
        assert result["posts"] == 0
        assert actor.token.encode() not in output + error
        assert admission.credential.encode() not in output + error
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_private_handoff_reaches_only_registered_node_and_is_consumed_once(attestor_relay):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for private handoff proof")
    _runtime, broker, _relay, _source, _registry = attestor_relay
    locator = broker.reserve_handoff()
    module = Path(__file__).resolve().parents[2] / "omnigent/resources/pi_native/scoped_client.cjs"
    script = """
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
@pytest.mark.parametrize("mode", ["normal", "loss", "replacement"])
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
    if sdk is None or json.loads((sdk / "package.json").read_text()).get("version") != "1.3.0":
        pytest.skip("the pinned Pi SDK 1.3.0 is required")
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
            b'"evaluate": 3, "execute": 3, "finish": 3'
            if mode == "normal"
            else b'"evaluate": 1, "execute": 1, "finish": 0'
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
