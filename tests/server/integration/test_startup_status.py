"""Read-only startup projections and owner-only exact-attempt cancellation."""

from uuid import uuid4

import pytest

from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.session_startup_store import SessionStartupStore
from tests.server.helpers import create_test_agent


@pytest.mark.asyncio
async def test_snapshot_and_cancellation_keep_session_authority_and_attempt_fence(
    auth_app, auth_client, db_uri
):
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    startups = SessionStartupStore(db_uri)
    auth_app.state.startup_store = startups
    owner = {"X-Forwarded-Email": "owner@example.test"}
    agent = await create_test_agent(auth_client, user="owner@example.test")
    created = await auth_client.post("/v1/sessions", json={"agent_id": agent["id"]}, headers=owner)
    assert created.status_code == 201, created.text
    session = conversations.get_conversation(created.json()["id"])
    host = uuid4().hex
    conversations.set_host_id(session.id, host, "/opt/work/project")
    for user, level in (("owner@example.test", 4), ("reader@example.test", 1)):
        permissions.ensure_user(user)
        permissions.grant(user, session.id, level)
    startups.configure(host, host_resident_limit=7, deployment_resident_limit=20)
    queued = startups.enqueue(
        owner_user_id="owner@example.test",
        host_id=host,
        session_id=session.id,
        operation_key="create",
        input_id=None,
    )
    reader = {"X-Forwarded-Email": "reader@example.test"}
    response = await auth_client.get(f"/v1/sessions/{session.id}", headers=reader)
    assert response.status_code == 200, response.text
    assert response.json()["startup"]["state"] == "queued"
    assert not response.json()["startup"]["can_cancel"]
    url = f"/v1/sessions/{session.id}/startup/cancel"
    body = {"startup_id": queued.id, "attempt_id": None}
    assert (await auth_client.post(url, json=body, headers=reader)).status_code == 403
    attempt = startups.reserve(host)
    assert (await auth_client.post(url, json=body, headers=owner)).status_code == 409
    assert startups.get(queued.id).state == "starting"
    body["attempt_id"] = attempt.attempt_id
    response = await auth_client.post(url, json=body, headers=owner)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "cancelling"
    assert not startups.claim(queued.id, attempt.attempt_id, runtime_id="late-runtime")
