"""Real runner, harness processes and Pi tools across permission/lifetime changes."""

import contextlib
import json
from unittest.mock import patch

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.harnesses.pi_native import bridge as pi_bridge
from omnigent.runner import create_runner_app
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from omnigent.server.permission_gate import authorize_permission_call, inspect_permission_call
from omnigent.server.permission_tiers import (
    PERMISSION_CAPABILITY_LABEL_KEY,
    PERMISSION_CAPABILITY_VALUE,
    PERMISSION_RUNTIME_ID_LABEL_KEY,
    ConfirmedPermissionState,
)
from omnigent.server.routes import sessions
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests._helpers.polling import poll_until_async
from tests.e2e.conftest import (
    _mock_llm_server_process,
    configure_mock_llm,
    get_mock_requests,
)
from tests.runner.helpers import NullServerClient, permission_session_init_payload
from tests.runtime.harnesses.test_process_manager import short_tmp_parent as short_tmp_parent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["full-live", "full-reinitialize", "harness-replacement", "executor-replacement"]
)
async def test_real_pi_tool_survives_tier_and_harness_changes(
    tmp_path, monkeypatch, short_tmp_parent, change
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_dir = home / ".pi/agent"
    agent_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(home / ".omnigent"))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("PI_OFFLINE", "1")
    monkeypatch.setattr(pi_bridge, "_BRIDGE_ROOT", home / ".omnigent/pi-native")
    log_dir = tmp_path / "llm"
    log_dir.mkdir()
    with contextlib.contextmanager(_mock_llm_server_process)(log_dir) as model_url:
        (agent_dir / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "fixture": {
                            "baseUrl": model_url + "/v1",
                            "api": "openai-completions",
                            "apiKey": "nonsecret-fixture",
                            "models": [{"id": "local", "contextWindow": 64000, "maxTokens": 2048}],
                        }
                    }
                }
            )
        )
        manager = HarnessProcessManager(tmp_parent=short_tmp_parent)
        await manager.start(sweep_orphans=False)
        spec = AgentSpec(
            spec_version=1,
            name="pi-lifecycle",
            # Keep Pi's built-in read enabled without loading any skills.
            skills_filter=[],
            executor=ExecutorSpec(
                model="fixture/local", config={"harness": "pi", "context_files": False}
            ),
        )
        conv = Conversation(
            id="pi-lifecycle",
            created_at=1,
            updated_at=1,
            root_conversation_id="pi-lifecycle",
            agent_id="fixture",
            host_id="test-host",
            workspace=str(workspace),
            harness_override="pi",
        )

        class Server(NullServerClient):
            runtime_id = ""
            tier = "auto"
            revision = 1

            def publish(self, tier, revision):
                self.tier, self.revision = tier, revision
                conv.labels = {
                    **ConfirmedPermissionState(tier, revision, "ready").labels("pi"),
                    PERMISSION_CAPABILITY_LABEL_KEY: PERMISSION_CAPABILITY_VALUE,
                    PERMISSION_RUNTIME_ID_LABEL_KEY: self.runtime_id,
                }

            def get_conversation(self, session_id):
                return conv if session_id == conv.id else None

            async def get(self, url, **kwargs):
                if url == "/v1/sessions/pi-lifecycle":
                    return httpx.Response(
                        200,
                        json={
                            "id": conv.id,
                            "agent_id": conv.agent_id,
                            "created_at": 1,
                            "updated_at": 1,
                            "host_id": conv.host_id,
                            "workspace": conv.workspace,
                            "labels": conv.labels,
                            "harness_override": "pi",
                        },
                    )
                if url.endswith("/items"):
                    return httpx.Response(200, json={"data": [], "has_more": False})
                return await super().get(url, **kwargs)

            async def post(self, url, **kwargs):
                body = kwargs.get("json", {})
                if url.endswith("/policies/evaluate"):
                    data = body["event"]["data"]
                    inspected = await inspect_permission_call(
                        conv,
                        harness="pi",
                        name=data["name"],
                        arguments=data["arguments"],
                        call_id=data["call_id"],
                        runner_router=None,
                        local_execution=True,
                        origin_receipt=body.get("pi_origin_receipt"),
                    )
                    verdict = await authorize_permission_call(
                        inspected,
                        request=None,
                        harness="pi",
                        arguments=data["arguments"],
                        conversation_store=self,
                        runner_router=None,
                    )
                    return httpx.Response(
                        200,
                        json={
                            "result": "POLICY_ACTION_ALLOW"
                            if verdict.action.value == "ALLOW"
                            else "POLICY_ACTION_DENY",
                            "reason": verdict.reason,
                            "permission_tier_binding": inspected.execution_binding(),
                        },
                    )
                return httpx.Response(200, json={})

        server = Server()

        async def resolver(*_args):
            return spec

        app = create_runner_app(
            process_manager=manager, server_client=server, spec_resolver=resolver
        )
        payload = permission_session_init_payload(
            {"session_id": conv.id, "agent_id": "fixture"},
            harness="pi",
            snapshot={"workspace": str(workspace)},
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner", timeout=60
        ) as client:

            async def runner_client(*_args, **_kwargs):
                return client

            async def initialize(tier="auto", revision=1):
                body = json.loads(json.dumps(payload))
                body["session_init"]["snapshot"]["labels"] = ConfirmedPermissionState(
                    tier, revision, "blocked"
                ).labels("pi")
                response = await client.post("/v1/sessions", json=body)
                assert response.status_code == 201, response.text
                server.runtime_id = response.json()["permission_tier_runtime_id"]
                server.publish(tier, revision)

            async def read_marker(stage):
                marker = "REAL_PI_READ_" + stage
                (workspace / "marker.txt").write_text(marker)
                configure_mock_llm(
                    model_url,
                    [
                        {
                            "tool_calls": [
                                {
                                    "call_id": "read-" + stage,
                                    "name": "read",
                                    "arguments": json.dumps({"path": "marker.txt"}),
                                }
                            ]
                        },
                        {"text": "done"},
                    ],
                    key="local",
                )
                response = await client.post(
                    "/v1/sessions/pi-lifecycle/events?stream=true",
                    json={
                        "type": "message",
                        "role": "user",
                        "agent_id": "fixture",
                        "model": "fixture",
                        "content": [{"type": "input_text", "text": "Read marker.txt " + stage}],
                    },
                )
                assert response.status_code == 200, response.text
                requests = get_mock_requests(model_url, key="local")
                assert any(
                    marker in json.dumps(message.get("content"))
                    for request in requests
                    for message in request.get("messages", [])
                    if message.get("role") == "tool"
                ), response.text

            with patch.object(sessions, "_get_runner_client", runner_client):
                try:
                    await initialize()
                    await read_marker("before")
                    first_pid = manager._entries[conv.id].process.pid
                    if change == "executor-replacement":
                        first_client = manager._entries[conv.id].client
                        previous_scope = app.state.permission_tiers.pi_scopes.parent_scope(conv.id)
                        configure_mock_llm(
                            model_url,
                            [{"error": "fixture request refused", "status_code": 400}],
                            key="local",
                        )
                        failed = await client.post(
                            "/v1/sessions/pi-lifecycle/events?stream=true",
                            json={
                                "type": "message",
                                "role": "user",
                                "agent_id": "fixture",
                                "model": "fixture",
                                "content": "Trigger the fixture error",
                            },
                        )
                        assert failed.status_code == 200, failed.text
                        assert "event: response.failed" in failed.text, failed.text
                        await poll_until_async(lambda: previous_scope.status == "revoked")
                        await read_marker("after-executor-replacement")
                        assert manager._entries[conv.id].process.pid == first_pid
                        assert manager._entries[conv.id].client is first_client
                        current_scope = app.state.permission_tiers.pi_scopes.parent_scope(conv.id)
                        assert (
                            current_scope.admission.principal.launch_epoch
                            != previous_scope.admission.principal.launch_epoch
                        )
                    if change.startswith("full"):
                        prepared = await client.post(
                            "/v1/sessions/pi-lifecycle/events",
                            json={
                                "type": "permission_tier_prepare",
                                "permission_tier": "full",
                                "transaction_id": "full-transition",
                                "expected_revision": 1,
                            },
                        )
                        assert prepared.status_code == 200, prepared.text
                        server.publish("full", 2)
                        committed = await client.post(
                            "/v1/sessions/pi-lifecycle/events",
                            json={
                                "type": "permission_tier_commit",
                                "permission_tier": "full",
                                "transaction_id": "full-transition",
                                "expected_revision": 1,
                                "revision": 2,
                            },
                        )
                        assert committed.status_code == 200, committed.text
                        if change == "full-reinitialize":
                            await initialize("full", 2)
                        await read_marker("after-full")
                        await initialize("full", 2)
                        await read_marker("after-reinitialize")
                    await manager.release(conv.id)
                    assert app.state.permission_tiers.state(conv.id).status == "ready"
                    await read_marker("after-harness-replacement")
                    assert manager._entries[conv.id].process.pid != first_pid
                finally:
                    await client.delete("/v1/sessions/pi-lifecycle")
                    await manager.shutdown()
