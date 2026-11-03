"""Real SDK/process/relay consumers with scripted policy approval and remote results."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
import httpx

from omnigent.harnesses.claude_native import bridge
from omnigent.runner.pi_bootstrap import PiBootstrapBroker, process_identity
from omnigent.runner.pi_scope_relay import PiScopeRelay
from tests.runner.test_permission_tier_runtime import _extension_runtime


async def main():
    mode = sys.argv[3] if len(sys.argv) > 3 else "normal"
    with tempfile.TemporaryDirectory(prefix="scoped-sdk-", dir=Path.cwd() / "tmp") as directory:
        root = Path(directory)
        runtime, *_ = _extension_runtime(root, "ask")
        (root / "workspace/marker.txt").write_text("REAL_SCOPED_READ\npi_receipt_" + "m" * 43)
        os.environ["OMNIGENT_BRIDGE_BIND_HOST"] = "127.0.0.1"
        bridge._TRUSTED_PARENT = root
        bridge._BRIDGE_ROOT = root
        from omnigent.harnesses.pi_native.bridge import prepare_permission_installation
        from omnigent.inner.pi_executor import _generate_extension_js, _ToolServer

        tool_server = _ToolServer()
        tool_server._permission_bridge_dir = runtime.path("session-a").parent
        tool_server.scoped_required = True
        await tool_server.start()
        extension_dir = root / "omnigent_pi_fixture"
        extension_dir.mkdir()
        extension = extension_dir / "omnigent_tools.js"
        extension.write_text(
            _generate_extension_js(
                tool_server.port,
                [
                    {"name": name, "parameters": {"type": "object", "properties": {}}}
                    for name in ["sys_fixture", "sys_exception"]
                ],
                "nonsecret-legacy-fixture",
                scoped_bootstrap=True,
            )
        )
        runtime._harnesses["session-a"] = "pi"
        prepare_permission_installation(
            runtime.path("session-a").parent,
            session_id="session-a",
            harness="pi",
            extension=extension,
        )
        side = PiScopeRelay(runtime, "session-a")
        counts = {"evaluate": 0, "execute": 0, "finish": 0}

        class Policy:
            async def post(self, url, json):
                data = json["event"]["data"]
                receipt = json["pi_origin_receipt"]
                counts["evaluate"] += 1
                bound, verdict = runtime.inspect(
                    "session-a",
                    call_id=data["call_id"],
                    name=data["name"],
                    arguments=data["arguments"],
                    expected_revision=3,
                    origin_receipt=receipt,
                    local_execution=runtime.pi_scopes.resolve(
                        "session-a", receipt
                    ).local_execution,
                )
                allowed = verdict.action.value != "DENY" and runtime.authorize(
                    "session-a",
                    call_id=bound.call_id,
                    name=bound.name,
                    arguments_digest=bound.arguments_digest,
                    expected_revision=3,
                    origin_receipt=receipt,
                )
                return httpx.Response(
                    200,
                    json={
                        "result": "POLICY_ACTION_ALLOW" if allowed else "POLICY_ACTION_DENY",
                        "reason": verdict.reason,
                        "permission_tier_binding": {
                            "call_id": bound.call_id,
                            "revision": 3,
                            "arguments_digest": bound.arguments_digest,
                            "pi_origin_receipt": receipt,
                        },
                    },
                )

        async def control(payload):
            if payload["type"] == "permission_tier_execute":
                counts["execute"] += 1
                executed = runtime.verify_execute(
                    "session-a",
                    call_id=payload["call_id"],
                    revision=payload["expected_revision"],
                    arguments_digest=payload["arguments_digest"],
                    name=payload["name"],
                    arguments=payload["arguments"],
                    operation_id=payload["operation_id"],
                    step=payload["step"],
                    origin_receipt=payload["pi_origin_receipt"],
                )
                if mode != "normal":
                    if mode == "loss":
                        runtime.pi_scopes.invalidate("session-a")
                    else:
                        side.bootstrap.close()
                        side.bootstrap = PiBootstrapBroker(runtime, "session-a")
                        side.bootstrap.attach_listener(relay._httpd, relay._advertised_url)
                    await asyncio.sleep(0.4)  # custom-lint: disable=sleep-then-assert -- hold ACK
                    assert runtime.state("session-a").revision == 3
                    assert process_identity(process.pid) == identity
                return {"executed": executed}
            counts["finish"] += 1
            return {
                "finished": runtime.finish(
                    "session-a",
                    call_id=payload["call_id"],
                    revision=payload["expected_revision"],
                    origin_receipt=payload["pi_origin_receipt"],
                )
            }

        async def remote(name, arguments):
            if name == "sys_exception":
                raise RuntimeError("pi_origin_receipt=fixture-private-rpc-marker")
            return {
                "text": "REMOTE_SCOPED_RESULT pi_receipt_" + "m" * 43,
                "permission_tier_binding": {"pi_origin_receipt": "pi_receipt_" + "m" * 43},
            }

        relay = bridge.start_tool_relay(
            bridge_dir=runtime.path("session-a").parent,
            tools=[{"name": name} for name in ["sys_fixture", "sys_exception"]],
            tool_executor=remote,
            loop=asyncio.get_running_loop(),
            policy_client=Policy(),
            session_id="session-a",
            permission_tier_control=control,
            pi_scope_relay=side,
        )
        locator = side.bootstrap.reserve_handoff()
        resource = Path.cwd() / "omnigent/resources/pi_native/scoped_bootstrap.mjs"
        env = {name: os.environ[name] for name in ["PATH"]}
        env.update(
            HOME=str(root),
            TMPDIR=str(root),
            PI_CODING_AGENT_DIR=str(root / "agent"),
            PI_OFFLINE="1",
            PI_TELEMETRY="0",
            NO_COLOR="1",
        )
        process = await asyncio.create_subprocess_exec(
            sys.argv[1],
            str(Path(__file__).with_name("pi_scoped_sdk_probe.mjs")),
            sys.argv[2],
            str(locator),
            str(root),
            str(resource),
            str(extension),
            mode,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            identity = process_identity(process.pid)
            assert identity
            actor = side.bootstrap.register_process(
                process.pid, expected_identity=identity, root=True
            )
            side.bootstrap.publish_handoff(actor, locator)
            output, error = await asyncio.wait_for(process.communicate(), 20)
            print(output.decode())
            print(error.decode(), file=sys.stderr)
            assert process.returncode == 0, process.returncode
            expected = (
                {"evaluate": 0, "execute": 0, "finish": 0}
                if mode == "missing-bootstrap"
                else (
                    {"evaluate": 3, "execute": 3, "finish": 3}
                    if mode == "normal"
                    else {"evaluate": 1, "execute": 1, "finish": 0}
                )
            )
            assert counts == expected, counts
            assert actor.token.encode() not in output + error
            print(json.dumps(counts))
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
            relay.close()
            side.bootstrap.close()
            await tool_server.stop()


if __name__ == "__main__":
    asyncio.run(main())
