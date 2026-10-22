"""The production app owns local-host queue recovery even after create returns."""

from unittest.mock import AsyncMock

import pytest

from tests.server.integration.test_multipart_create_host_launch import (
    _HOST_ID,
    _multipart_create,
)
from tests.server.integration.test_multipart_create_host_launch import (
    app as app,
)
from tests.server.integration.test_multipart_create_host_launch import (
    conv_store as conv_store,
)
from tests.server.integration.test_multipart_create_host_launch import (
    register_host as register_host,
)


@pytest.fixture(autouse=True)
def enable_admission(monkeypatch):
    # Queue/preparation tests assume independently reconciled resident accounting.
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


def test_enabled_app_installs_admission_and_saved_input_worker(app):
    assert app.state.startup_admission is not None
    assert app.state.startup_inputs is not None
    assert app.state.host_registry.startup_admitter is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("create_form", ["multipart", "json"])
async def test_create_returns_durable_queue_on_incompatible_local_host(
    app,
    client,
    conv_store,
    register_host,
    create_form,
):
    capture = register_host()
    response = await _multipart_create(
        client,
        {"host_id": _HOST_ID, "workspace": "/Users/sample/project"}
        if create_form == "multipart"
        else {},
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["session_id"]
    if create_form == "json":
        response = await client.post(
            "/v1/sessions",
            json={
                "agent_id": response.json()["agent_id"],
                "host_id": _HOST_ID,
                "workspace": "/Users/sample/project",
            },
        )
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]
    startup = app.state.startup_store.for_session(session_id)
    assert startup is not None and startup.state == "queued"
    assert startup.input_id is None
    assert conv_store.get_conversation(session_id).runner_id is None
    assert capture.launch == []

    result = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": "work"}]},
        },
    )
    assert result.status_code == 202, result.text
    assert result.json()["queued"]
    assert not await app.state.startup_inputs._recover_session(
        app,
        session_id,
        app.state.host_registry.get(_HOST_ID),
    )
    [canonical] = conv_store.list_pending_inputs(session_id)
    assert app.state.startup_store.for_session(session_id).input_id == canonical.id
    assert canonical.status == "in_progress"
    assert capture.launch == []


@pytest.mark.asyncio
async def test_host_runner_request_is_queued_before_any_workspace_probe(
    app, client, register_host
):
    capture = register_host()
    created = await _multipart_create(client, {})
    assert created.status_code == 201, created.text
    session_id = created.json()["session_id"]
    response = await client.post(
        f"/v1/hosts/{_HOST_ID}/runners",
        json={
            "session_id": session_id,
            "workspace": "/Users/sample/project",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "queued"
    assert capture.stats == [] and capture.launch == []
    startup = app.state.startup_store.for_session(session_id)
    assert startup is not None and startup.host_id == _HOST_ID
    assert startup.preparation["workspace"] == "/Users/sample/project"
    conn = app.state.host_registry.get(_HOST_ID)
    conn.hello.capabilities = ["startup_admission_v1", "startup_receipts:test"]
    app.state.startup_admission.resolve_harness = AsyncMock(return_value="test")
    await app.state.startup_admission.coordinator.advance(_HOST_ID)
    await app.state.startup_admission.coordinator.drain()
    assert capture.stats
    assert len(capture.launch) == 1

    assert capture.launch[0].startup.startup_id == startup.id
    assert app.state.startup_store.for_session(session_id).state == "verifying"
    retried = await client.post(
        f"/v1/hosts/{_HOST_ID}/runners",
        json={
            "session_id": session_id,
            "workspace": "/Users/sample/project",
        },
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["startup_id"] == startup.id
    assert len(capture.launch) == 1


@pytest.mark.asyncio
async def test_definite_preparation_refusal_does_not_hold_runtime_capacity(
    app,
    client,
    register_host,
    monkeypatch,
):
    from omnigent.server.routes._workspace_validation import WorkspaceValidationError

    capture = register_host()
    created = await _multipart_create(client, {})
    session_id = created.json()["session_id"]
    response = await client.post(
        f"/v1/hosts/{_HOST_ID}/runners",
        json={
            "session_id": session_id,
            "workspace": "/Users/sample/project",
        },
    )
    assert response.status_code == 200, response.text
    conn = app.state.host_registry.get(_HOST_ID)
    conn.hello.capabilities = ["startup_admission_v1", "startup_receipts:test"]
    app.state.startup_admission.resolve_harness = AsyncMock(return_value="test")
    monkeypatch.setattr(
        "omnigent.server.routes._workspace_validation.validate_workspace",
        AsyncMock(side_effect=WorkspaceValidationError("outside the session boundary")),
    )
    await app.state.startup_admission.coordinator.advance(_HOST_ID)
    await app.state.startup_admission.coordinator.drain()
    current = app.state.startup_store.for_session(session_id)
    assert current.state == "blocked" and current.reason == "preparation_refused"
    assert not current.resident
    assert capture.stats == [] and capture.launch == []
    retried = await client.post(
        f"/v1/hosts/{_HOST_ID}/runners",
        json={
            "session_id": session_id,
            "workspace": "/Users/sample/corrected",
        },
    )
    assert retried.status_code == 200, retried.text
    assert retried.json()["startup_id"] != current.id
