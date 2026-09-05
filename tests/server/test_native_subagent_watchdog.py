"""Read-only probe, source fencing and lifecycle tests with real async races."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.native_subagent_snapshot import NativeSubagentSnapshot
from omnigent.server.native_subagent_watchdog import NativeSubagentWatchdog


def snapshot(generation=1, sequence=1, children=None, **kwargs):
    return NativeSubagentSnapshot(
        generation, sequence, {"c": "running"} if children is None else children, **kwargs
    )


@pytest.mark.asyncio
async def test_timeout_is_unknown_and_has_exactly_three_read_only_attempts() -> None:
    attempts = []
    changed = []

    async def verify(parent, binding):
        attempts.append((parent, binding))
        return True  # A healthy Host is NOT proof a child ended.

    watch = NativeSubagentWatchdog(
        verify=verify, changed=changed.append, heartbeat_timeout_s=0.005, retry_s=0.005
    )
    watch.heartbeat("p", "runner", snapshot())
    await asyncio.wait_for(watch._states["p"].task, 1)
    assert len(attempts) == 3
    assert watch.is_unverified("p", "c")
    assert watch._states["p"].snapshot.children == {"c": "running"}
    await watch.close()
    assert not watch._states


@pytest.mark.asyncio
async def test_new_generation_heartbeat_during_awaited_probe_cannot_be_overwritten() -> None:
    entered, resume = asyncio.Event(), asyncio.Event()

    async def verify(parent, binding):
        entered.set()
        await resume.wait()
        return False

    watch = NativeSubagentWatchdog(
        verify=verify, changed=lambda ids: None, heartbeat_timeout_s=0.01
    )
    watch.heartbeat("p", "r", snapshot())
    await asyncio.wait_for(entered.wait(), 1)
    watch.heartbeat("p", "r", snapshot(2, 1))
    resume.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not watch.is_unverified("p", "c")
    assert watch._states["p"].snapshot.generation == 2
    assert not watch.heartbeat("p", "r", snapshot(1, 99))
    assert not watch.heartbeat("p", "r", snapshot(2, 1))
    await watch.close()


@pytest.mark.asyncio
async def test_terminal_during_probe_disarms_without_stale_exhaustion() -> None:
    entered, resume = asyncio.Event(), asyncio.Event()

    async def verify(parent, binding):
        entered.set()
        await resume.wait()
        return True

    watch = NativeSubagentWatchdog(
        verify=verify, changed=lambda ids: None, heartbeat_timeout_s=0.005
    )
    watch.heartbeat("p", "r", snapshot())
    await asyncio.wait_for(entered.wait(), 1)
    watch.heartbeat("p", "r", snapshot(sequence=2, children={"c": "completed"}))
    resume.set()
    await asyncio.sleep(0)
    assert "p" not in watch._states
    await watch.close()


@pytest.mark.asyncio
async def test_omission_is_uncertain_not_a_terminal_transition() -> None:
    async def verify(parent, binding):
        return True

    watch = NativeSubagentWatchdog(verify=verify, changed=lambda ids: None)
    watch.heartbeat("p", "r", snapshot())
    watch.heartbeat("p", "r", snapshot(sequence=2, children={}))
    assert watch.is_unverified("p", "c")
    assert watch._states["p"].missing == frozenset({"c"})
    watch.heartbeat("p", "r", snapshot(sequence=3, children={"c": "running"}))
    assert not watch.is_unverified("p", "c")
    await watch.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["stop", "archive", "delete", "rebind"])
async def test_lifecycle_disarm_cancels_pending_probe_and_rejects_old_source(reason) -> None:
    entered = asyncio.Event()

    async def verify(parent, binding):
        entered.set()
        await asyncio.Event().wait()
        return True

    watch = NativeSubagentWatchdog(
        verify=verify, changed=lambda ids: None, heartbeat_timeout_s=0.005
    )
    watch.heartbeat("p", "r", snapshot())
    await asyncio.wait_for(entered.wait(), 1)
    task = watch._states["p"].task
    watch.disarm("p", retire=True)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert task.cancelled()
    assert not watch.heartbeat("p", "r", snapshot(sequence=2))
    assert not watch.is_unverified("p", "c")
    assert watch.heartbeat("p", "new-runner", snapshot())
    await watch.close()


@pytest.mark.parametrize("header_source", ["runner", "local_cli"])
def test_route_is_observation_only_and_requires_current_runner(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    header_source: str,
) -> None:
    import httpx
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    from omnigent.errors import OmnigentError
    from omnigent.native_subagent_snapshot import NativeSubagentSnapshotPublisher
    from omnigent.runner.identity import (
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
        token_bound_runner_id,
        with_runner_binding_token,
    )
    from omnigent.runner.native.orchestration import _native_forwarder_headers
    from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
    from omnigent.server.native_subagent_watchdog import _watchdogs
    from omnigent.server.routes._sessions.common import (
        _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE,
        _session_background_task_count_cache,
        _session_status_cache,
    )
    from omnigent.server.routes.sessions import create_sessions_router
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    store = SqlAlchemyConversationStore(db_uri)
    agents = SqlAlchemyAgentStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    agent_id = "087b7cb7ac30abf4debfaa578d052ec6"
    agents.create(agent_id=agent_id, name="test", bundle_location=f"{agent_id}/bundle")
    parent = store.create_conversation(title="parent", agent_id=agent_id)
    child = store.create_conversation(
        title="child", agent_id=agent_id, parent_conversation_id=parent.id
    )
    token = "native-snapshot-test-runner"
    store.replace_runner_id(parent.id, token_bound_runner_id(token))
    store.set_labels(
        parent.id, {"omnigent.wrapper": _CLAUDE_NATIVE_WRAPPER_LABEL_VALUE, "test_goal": "active"}
    )
    store.set_labels(child.id, {"omnigent.wrapper": "claude-code-native-ui-subagent"})
    permissions.ensure_user("owner@example.com")
    permissions.grant("owner@example.com", parent.id, LEVEL_OWNER)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle(request: Request, exc: OmnigentError):
        return JSONResponse(status_code=exc.http_status, content={"error": exc.message})

    app.include_router(
        create_sessions_router(
            conversation_store=store,
            agent_store=agents,
            auth_provider=UnifiedAuthProvider(source="header"),
            permission_store=permissions,
            runner_tunnel_tokens=frozenset({"other-registered-runner"}),
        ),
        prefix="/v1",
    )
    _session_status_cache[child.id] = "running"
    _session_background_task_count_cache[child.id] = 7

    def launch_headers(binding: str | None) -> dict[str, str]:
        base = {"X-Forwarded-Email": "owner@example.com"}
        if header_source == "runner":
            if binding is None:
                monkeypatch.delenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, raising=False)
            else:
                monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, binding)
            return _native_forwarder_headers(base)
        return with_runner_binding_token(base, binding)

    headers = launch_headers(token)
    payload = {
        "type": "external_native_subagent_snapshot",
        "data": {
            "generation": 1,
            "sequence": 1,
            "children": [{"session_id": child.id, "status": "completed"}],
        },
    }
    try:
        with TestClient(app) as client:

            async def publish_from_launch_headers(built_headers: dict[str, str]) -> httpx.Response:
                delivered = asyncio.Event()
                replies = []

                def deliver(request: httpx.Request) -> httpx.Response:
                    response = client.post(
                        request.url.path,
                        content=request.content,
                        headers=dict(request.headers),
                    )
                    replies.append(response)
                    delivered.set()
                    return httpx.Response(response.status_code, json=response.json())

                async with httpx.AsyncClient(
                    base_url="http://native.test",
                    headers=built_headers,
                    transport=httpx.MockTransport(deliver),
                ) as native_client:
                    async with NativeSubagentSnapshotPublisher(native_client) as publisher:
                        publisher.update(parent.id, {child.id: "running"})
                        await asyncio.wait_for(delivered.wait(), 1)
                return replies[0]

            denied = asyncio.run(
                publish_from_launch_headers(launch_headers("other-registered-runner"))
            )
            assert denied.status_code == 403
            missing = asyncio.run(publish_from_launch_headers(launch_headers(None)))
            assert missing.status_code == 403
            accepted = asyncio.run(publish_from_launch_headers(headers))
            assert accepted.status_code == 202, accepted.text
            assert any(parent.id in watch._states for watch in _watchdogs)
            import time

            payload["data"]["generation"] = time.monotonic_ns()
            terminal_inventory = client.post(
                f"/v1/sessions/{parent.id}/events",
                json=payload,
                headers=headers,
            )
            assert terminal_inventory.status_code == 202
            # A snapshot saying completed is not the authoritative status path.
            assert _session_status_cache[child.id] == "running"
            assert _session_background_task_count_cache[child.id] == 7
            assert store.get_conversation(parent.id).labels["test_goal"] == "active"
            assert (
                "omnigent.subagent.terminal_status" not in store.get_conversation(child.id).labels
            )
            payload["data"]["sequence"] = 2
            payload["data"]["children"] = [{"session_id": parent.id, "status": "running"}]
            foreign = client.post(
                f"/v1/sessions/{parent.id}/events", json=payload, headers=headers
            )
            assert foreign.status_code == 400, foreign.text
    finally:
        _session_status_cache.pop(child.id, None)
        _session_background_task_count_cache.pop(child.id, None)
