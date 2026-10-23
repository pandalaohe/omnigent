"""Create accepts host preparation before asking the host to touch a workspace."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.session_open_rate import CREATE_ORIGIN_AGENT, CREATE_ORIGIN_HEADER
from omnigent.server.startup_registration import (
    STARTUP_CREATE_PREPARATION_LABEL_KEY,
    STARTUP_CREATE_PREPARATION_PENDING,
    STARTUP_REGISTRATION_LABEL_KEY,
    STARTUP_REGISTRATION_PENDING,
)
from tests.server.helpers import create_test_agent
from tests.server.integration.test_multipart_create_host_launch import (
    _HOST_ID,
    _multipart_create,
)
from tests.server.integration.test_multipart_create_host_launch import app as app
from tests.server.integration.test_multipart_create_host_launch import conv_store as conv_store
from tests.server.integration.test_multipart_create_host_launch import (
    register_host as register_host,
)
from tests.server.test_member_snapshot import joint_bundle


@pytest.fixture(autouse=True)
def enable_admission(monkeypatch):
    # This suite tests create preparation after resident inventory reconciliation.
    monkeypatch.setattr(
        "omnigent.server.startup_admission.StartupAdmission.inventory_ready",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "omnigent.server.server_config.load_server_config",
        lambda: {
            "startup_admission": {
                "enabled": True,
                "host_resident_limit": 3,
                "deployment_resident_limit": 7,
            }
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["json", "multipart"])
async def test_create_queues_preparation_before_host_io(
    app, client, conv_store, register_host, form
):
    capture = register_host()
    workspace = "/Users/sample/project"
    if form == "multipart":
        response = await _multipart_create(client, {"host_id": _HOST_ID, "workspace": workspace})
        session_id = response.json()["session_id"]
    else:
        uploaded = await _multipart_create(client, {})
        assert uploaded.status_code == 201, uploaded.text
        response = await client.post(
            "/v1/sessions",
            json={
                "agent_id": uploaded.json()["agent_id"],
                "host_id": _HOST_ID,
                "workspace": workspace,
                "initial_items": [
                    {
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "start here"}],
                        },
                    }
                ],
            },
        )
        session_id = response.json()["id"]
    assert response.status_code == 201, response.text
    assert STARTUP_CREATE_PREPARATION_LABEL_KEY not in response.json()["labels"]
    assert capture.stats == [] and capture.launch == []
    startup = app.state.startup_store.for_session(session_id)
    assert startup is not None and startup.state == "queued"
    assert startup.preparation["workspace"] == workspace
    assert startup.preparation["git"] is None
    assert startup.preparation["session_id"] == session_id
    assert conv_store.get_conversation(session_id).runner_id is None
    if form == "json":
        [initial] = conv_store.list_pending_inputs(session_id)
        assert initial.status == "in_progress"
        assert startup.input_id == initial.id

    conn = app.state.host_registry.get(_HOST_ID)
    conn.hello.capabilities = ["startup_admission_v1", "startup_receipts:test"]
    app.state.startup_admission.resolve_harness = AsyncMock(return_value="test")
    await app.state.startup_admission.coordinator.advance(_HOST_ID)
    await app.state.startup_admission.coordinator.drain()
    assert capture.stats
    assert len(capture.launch) == 1
    assert capture.launch[0].startup.startup_id == startup.id
    if form == "json":
        [pending] = conv_store.list_pending_inputs(session_id)
        assert pending.status == "in_progress"


@pytest.mark.asyncio
async def test_enabled_create_rejects_unsupported_initial_items_before_insertion(
    app, client, conv_store, register_host
):
    capture = register_host()
    uploaded = await _multipart_create(client, {})
    assert uploaded.status_code == 201, uploaded.text
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": uploaded.json()["agent_id"],
            "host_id": _HOST_ID,
            "workspace": "/Users/sample/project",
            "initial_items": [{"type": "interrupt", "data": {}}],
        },
    )
    assert response.status_code == 400, response.text
    assert capture.stats == [] and capture.launch == []
    assert len(conv_store.list_conversations().data) == 1


@pytest.mark.asyncio
async def test_git_create_queues_worktree_without_touching_host(
    app, client, conv_store, register_host
):
    capture = register_host()
    uploaded = await _multipart_create(client, {})
    assert uploaded.status_code == 201, uploaded.text
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": uploaded.json()["agent_id"],
            "host_id": _HOST_ID,
            "workspace": "/Users/sample/project",
            "git": {"branch_name": "feature/queued", "base_branch": "main"},
        },
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    startup = app.state.startup_store.for_session(session_id)
    assert startup.preparation["git"]["branch_name"] == "feature/queued"
    assert conv_store.get_conversation(session_id).worktree is None
    assert capture.stats == [] and capture.launch == []


@pytest.mark.asyncio
async def test_agent_child_registration_requires_current_parent_runner(app, client, conv_store):
    uploaded = await _multipart_create(client, {})
    assert uploaded.status_code == 201, uploaded.text
    parent_id = uploaded.json()["session_id"]
    binding = "registration-test-parent-binding"
    assert conv_store.set_runner_id(parent_id, token_bound_runner_id(binding))
    child_body = {
        "agent_id": uploaded.json()["agent_id"],
        "parent_session_id": parent_id,
    }
    origin = {CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT}
    denied = await client.post("/v1/sessions", json=child_body, headers=origin)
    assert denied.status_code == 403
    assert len(conv_store.list_conversations().data) == 1

    authorized = {**origin, RUNNER_TUNNEL_TOKEN_HEADER: binding}
    created = await client.post("/v1/sessions", json=child_body, headers=authorized)
    assert created.status_code == 201, created.text
    child_id = created.json()["id"]
    assert created.json()["labels"][STARTUP_REGISTRATION_LABEL_KEY] == (
        STARTUP_REGISTRATION_PENDING
    )
    denied_register = await client.post(f"/v1/sessions/{child_id}/startup/registered")
    assert denied_register.status_code == 403
    assert (
        conv_store.get_conversation(child_id).labels[STARTUP_REGISTRATION_LABEL_KEY]
        == STARTUP_REGISTRATION_PENDING
    )
    registered = await client.post(
        f"/v1/sessions/{child_id}/startup/registered",
        headers={RUNNER_TUNNEL_TOKEN_HEADER: binding},
    )
    assert registered.status_code == 200, registered.text
    assert STARTUP_REGISTRATION_LABEL_KEY not in conv_store.get_conversation(child_id).labels
    assert (
        await client.post(
            f"/v1/sessions/{child_id}/startup/registered",
            headers={RUNNER_TUNNEL_TOKEN_HEADER: binding},
        )
    ).status_code == 200

    client.headers.update(authorized)
    bundled = await _multipart_create(client, {"parent_session_id": parent_id})
    assert bundled.status_code == 201, bundled.text
    bundled_id = bundled.json()["session_id"]
    assert (
        conv_store.get_conversation(bundled_id).labels[STARTUP_REGISTRATION_LABEL_KEY]
        == STARTUP_REGISTRATION_PENDING
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["json", "multipart"])
async def test_agent_child_inherits_effective_host_without_host_io(
    app, client, conv_store, register_host, form
):
    capture = register_host()
    parent = await _multipart_create(
        client, {"host_id": _HOST_ID, "workspace": "/Users/sample/project"}
    )
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["session_id"]
    binding = "child-inherited-host-binding"
    assert conv_store.set_runner_id(parent_id, token_bound_runner_id(binding))
    headers = {
        CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT,
        RUNNER_TUNNEL_TOKEN_HEADER: binding,
    }
    input_message = {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": "start child"}],
        },
    }
    if form == "json":
        child = await client.post(
            "/v1/sessions",
            json={
                "agent_id": parent.json()["agent_id"],
                "parent_session_id": parent_id,
                "initial_items": [input_message],
            },
            headers=headers,
        )
        child_id = child.json().get("id")
    else:
        client.headers.update(headers)
        child = await _multipart_create(
            client,
            {"parent_session_id": parent_id},
        )
        child_id = child.json().get("session_id")
    assert child.status_code == 201, child.text
    created = conv_store.get_conversation(child_id)
    assert created.host_id == _HOST_ID
    assert created.runner_id == token_bound_runner_id(binding)
    assert created.workspace == "/Users/sample/project"
    assert created.labels[STARTUP_REGISTRATION_LABEL_KEY] == STARTUP_REGISTRATION_PENDING
    assert child.json()["labels"][STARTUP_REGISTRATION_LABEL_KEY] == (STARTUP_REGISTRATION_PENDING)
    if form == "json":
        [pending] = conv_store.list_pending_inputs(child_id)
        assert pending.status == "in_progress"
    else:
        assert conv_store.list_pending_inputs(child_id) == []
    assert capture.stats == [] and capture.launch == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_workspace", "expected_workspace"),
    [
        pytest.param(None, "/opt/work/parent", id="inherits-parent"),
        pytest.param("/opt/work/child", "/opt/work/child", id="explicit-child-wins"),
    ],
)
async def test_direct_runner_named_child_workspace(
    client, conv_store, child_workspace, expected_workspace
):
    agent = await create_test_agent(
        client, name="direct-runner-parent", sub_agents=[{"name": "worker"}]
    )
    binding = "direct-runner-workspace-binding"
    runner_id = token_bound_runner_id(binding)
    parent = conv_store.create_conversation(
        agent_id=agent["id"], workspace="/opt/work/parent", runner_id=runner_id
    )
    body = {
        "agent_id": agent["id"],
        "parent_session_id": parent.id,
        "sub_agent_name": "worker",
        "title": "worker:workspace",
    }
    if child_workspace is not None:
        body["workspace"] = child_workspace
    response = await client.post(
        "/v1/sessions",
        json=body,
        headers={
            CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT,
            RUNNER_TUNNEL_TOKEN_HEADER: binding,
        },
    )
    assert response.status_code == 201, response.text
    created = conv_store.get_conversation(response.json()["id"])
    assert created.workspace == expected_workspace
    assert created.host_id is None
    assert created.parent_conversation_id == parent.id
    assert created.sub_agent_name == "worker"


@pytest.mark.asyncio
async def test_nested_child_inherits_host_through_hostless_parent(
    app, client, conv_store, register_host, db_uri
):
    from omnigent.server.auth import RESERVED_USER_LOCAL
    from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore

    preferences = SqlAlchemyUserPreferencesStore(db_uri)
    app.state.user_preferences_store = preferences
    preferences.patch_namespace(
        RESERVED_USER_LOCAL, "session_collab", {"sessionRestrictions": "off"}
    )
    capture = register_host()
    root = await _multipart_create(
        client, {"host_id": _HOST_ID, "workspace": "/Users/sample/project"}
    )
    assert root.status_code == 201, root.text
    root_id = root.json()["session_id"]
    binding = "nested-child-inherited-host-binding"
    assert conv_store.set_runner_id(root_id, token_bound_runner_id(binding))
    parent = conv_store.create_conversation(
        agent_id=root.json()["agent_id"],
        parent_conversation_id=root_id,
        runner_id=token_bound_runner_id(binding),
        kind="sub_agent",
        workspace="/Users/sample/project",
    )
    child = await client.post(
        "/v1/sessions",
        json={
            "agent_id": root.json()["agent_id"],
            "parent_session_id": parent.id,
        },
        headers={
            CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT,
            RUNNER_TUNNEL_TOKEN_HEADER: binding,
        },
    )
    assert child.status_code == 201, child.text
    created = conv_store.get_conversation(child.json()["id"])
    assert created.host_id == _HOST_ID
    assert created.runner_id == token_bound_runner_id(binding)
    assert capture.stats == [] and capture.launch == []


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["workspace", "git", "bundle_workspace"])
async def test_agent_child_new_placement_queues_own_runner_preparation(
    app, client, conv_store, register_host, placement
):
    capture = register_host()
    parent = await _multipart_create(
        client, {"host_id": _HOST_ID, "workspace": "/Users/sample/project"}
    )
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["session_id"]
    binding = "child-new-placement-binding"
    assert conv_store.set_runner_id(parent_id, token_bound_runner_id(binding))
    headers = {
        CREATE_ORIGIN_HEADER: CREATE_ORIGIN_AGENT,
        RUNNER_TUNNEL_TOKEN_HEADER: binding,
    }
    if placement == "bundle_workspace":
        client.headers.update(headers)
        child = await _multipart_create(
            client,
            {"parent_session_id": parent_id, "workspace": "/Users/sample/other"},
        )
        child_id = child.json().get("session_id")
    else:
        body = {
            "agent_id": parent.json()["agent_id"],
            "parent_session_id": parent_id,
        }
        if placement == "workspace":
            body["workspace"] = "/Users/sample/other"
        else:
            body["host_id"] = _HOST_ID
            body["git"] = {"branch_name": "feature/child", "base_branch": "main"}
        child = await client.post("/v1/sessions", json=body, headers=headers)
        child_id = child.json().get("id")
    assert child.status_code == 201, child.text
    created = conv_store.get_conversation(child_id)
    startup = app.state.startup_store.for_session(child_id)
    assert created.host_id == _HOST_ID
    assert created.runner_id is None
    assert created.labels[STARTUP_REGISTRATION_LABEL_KEY] == STARTUP_REGISTRATION_PENDING
    assert startup is not None and startup.state == "queued"
    assert startup.preparation["workspace"] == (
        "/Users/sample/other" if placement != "git" else "/Users/sample/project"
    )
    assert (startup.preparation["git"] is not None) == (placement == "git")
    assert capture.stats == [] and capture.launch == []


@pytest.mark.asyncio
async def test_queue_refusal_removes_unissued_create(app, client, conv_store, register_host):
    register_host()
    conn = app.state.host_registry.get(_HOST_ID)
    conn.hello.capabilities = ["startup_admission_v1", "startup_receipts:test"]
    uploaded = await _multipart_create(client, {})
    assert uploaded.status_code == 201, uploaded.text
    app.state.startup_admission.queue_preparation = AsyncMock(
        side_effect=OmnigentError("preparation refused", code=ErrorCode.CONFLICT)
    )
    refused = await client.post(
        "/v1/sessions",
        json={
            "agent_id": uploaded.json()["agent_id"],
            "host_id": _HOST_ID,
            "workspace": "/Users/sample/project",
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "start here"}],
                    },
                }
            ],
        },
    )
    assert refused.status_code == 409, refused.text
    assert len(conv_store.list_conversations().data) == 1


@pytest.mark.asyncio
async def test_first_input_refusal_removes_create_before_preparation(
    app, client, conv_store, register_host
):
    register_host()
    conn = app.state.host_registry.get(_HOST_ID)
    conn.hello.capabilities = ["startup_admission_v1", "startup_receipts:test"]
    uploaded = await _multipart_create(client, {})
    assert uploaded.status_code == 201, uploaded.text
    app.state.startup_inputs.enqueue = AsyncMock(
        side_effect=OmnigentError("input refused", code=ErrorCode.CONFLICT)
    )
    refused = await client.post(
        "/v1/sessions",
        json={
            "agent_id": uploaded.json()["agent_id"],
            "host_id": _HOST_ID,
            "workspace": "/Users/sample/project",
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "start here"}],
                    },
                }
            ],
        },
    )
    assert refused.status_code == 409, refused.text
    session_id = app.state.startup_inputs.enqueue.await_args.args[0]
    assert conv_store.get_conversation(session_id) is None
    assert app.state.startup_store.for_session(session_id) is None
    assert len(conv_store.list_conversations().data) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("form", ["json", "multipart"])
async def test_client_cannot_seed_create_preparation_label(client, conv_store, form):
    uploaded = await _multipart_create(client, {})
    assert uploaded.status_code == 201, uploaded.text
    labels = {STARTUP_CREATE_PREPARATION_LABEL_KEY: STARTUP_CREATE_PREPARATION_PENDING}
    if form == "json":
        response = await client.post(
            "/v1/sessions",
            json={"agent_id": uploaded.json()["agent_id"], "labels": labels},
        )
    else:
        response = await _multipart_create(client, {"labels": labels})
    assert response.status_code == 400, response.text
    assert len(conv_store.list_conversations().data) == 1


@pytest.mark.asyncio
async def test_create_preparation_barrier_holds_interleaved_worker(
    app, client, conv_store, register_host
):
    capture = register_host()
    conn = app.state.host_registry.get(_HOST_ID)
    conn.hello.capabilities = ["startup_admission_v1", "startup_receipts:test"]
    app.state.startup_admission.resolve_harness = AsyncMock(return_value="test")
    uploaded = await _multipart_create(client, {})
    assert uploaded.status_code == 201, uploaded.text
    entered = asyncio.Event()
    proceed = asyncio.Event()
    pending_ids = []
    original = app.state.startup_admission.queue_preparation

    async def paused_queue(host_conn, session_id, preparation):
        assert (
            conv_store.get_conversation(session_id).labels[STARTUP_CREATE_PREPARATION_LABEL_KEY]
            == STARTUP_CREATE_PREPARATION_PENDING
        )
        pending_ids.append(session_id)
        entered.set()
        await proceed.wait()
        return await original(host_conn, session_id, preparation)

    app.state.startup_admission.queue_preparation = paused_queue
    task = asyncio.create_task(
        client.post(
            "/v1/sessions",
            json={
                "agent_id": uploaded.json()["agent_id"],
                "host_id": _HOST_ID,
                "workspace": "/Users/sample/project",
                "initial_items": [
                    {
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "start here"}],
                        },
                    }
                ],
            },
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5)
        assert not await app.state.startup_inputs._recover_session(app, pending_ids[0], conn)
        with pytest.raises(OmnigentError, match="queued"):
            await app.state.startup_admission.queue_session(conn, pending_ids[0])
        await app.state.startup_admission.coordinator.advance(_HOST_ID)
        await app.state.startup_admission.coordinator.drain()
        assert app.state.startup_store.for_session(pending_ids[0]) is None
        assert capture.stats == [] and capture.launch == []
    finally:
        proceed.set()
    created = await asyncio.wait_for(task, timeout=5)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    assert (
        STARTUP_CREATE_PREPARATION_LABEL_KEY not in conv_store.get_conversation(session_id).labels
    )
    startup = app.state.startup_store.for_session(session_id)
    assert startup is not None and startup.preparation is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "fixed"])
async def test_host_native_routing_refused_before_catalog_probe(
    app, client, conv_store, register_host, monkeypatch, mode
):
    capture = register_host()
    agent = await create_test_agent(client, name="claude-native-ui")
    baseline = len(conv_store.list_conversations().data)
    from omnigent.server.routes._sessions import orchestration

    catalog_probe = AsyncMock(side_effect=AssertionError("pre-permit host catalog probe"))
    monkeypatch.setattr(orchestration, "_pre_session_model_catalog", catalog_probe)
    body = {
        "agent_id": agent["id"],
        "host_id": _HOST_ID,
        "workspace": "/Users/sample/project",
        "cost_control_mode_override": "on",
        "smart_routing_message": "route my work",
    }
    if mode == "auto":
        body["harness_override"] = "auto"
    response = await client.post("/v1/sessions", json=body)
    assert response.status_code == 400, response.text
    assert "catalog" in response.json()["error"]["message"].lower()
    catalog_probe.assert_not_awaited()
    assert len(conv_store.list_conversations().data) == baseline
    assert capture.stats == [] and capture.launch == []


@pytest.mark.asyncio
async def test_host_joint_bundle_refused_before_catalog_probe(
    app, client, conv_store, register_host, monkeypatch
):
    capture = register_host()
    from omnigent.server.routes._sessions import helpers

    catalog_probe = AsyncMock(side_effect=AssertionError("pre-permit member catalog probe"))
    monkeypatch.setattr(helpers, "_host_model_options_via_registry", catalog_probe)
    response = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"host_id": _HOST_ID, "workspace": "/Users/sample/project"})},
        files={"bundle": ("agent.tar.gz", joint_bundle(), "application/gzip")},
    )
    assert response.status_code == 400, response.text
    assert "catalog" in response.json()["error"]["message"].lower()
    catalog_probe.assert_not_awaited()
    assert len(conv_store.list_conversations().data) == 0
    assert capture.stats == [] and capture.launch == []
