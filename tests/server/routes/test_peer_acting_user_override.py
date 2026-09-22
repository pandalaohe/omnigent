"""``_post_event_impl``'s ``acting_user_id`` override — real end-to-end.

The peer sweeper posts through a synthetic request that carries no auth
headers at all. Delivery can only succeed there because ``acting_user_id``
replaces the ``_get_user_id(request, auth_provider)`` call inside
``_post_event_impl`` — with a permission store configured, a headerless
request with no override would 401. No fakes on the delivery path: this
drives the real ``_true_state`` / ``_deliver`` / ``_post_event_impl``
closures ``register_peer_routes`` builds, through the app's real
``peer_sweeper``. The inline send (default path, real ``X-Forwarded-Email``
auth, no override) is exercised in the same test to show it is unchanged.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.db.utils import generate_agent_id, now_epoch
from omnigent.entities import SessionPeerMessage
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_OWNER, UnifiedAuthProvider
from omnigent.server.feature_flags import resolve_feature_flags
from omnigent.server.peer_sweeper import PeerSweeper
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.peer_message_store.sqlalchemy_store import SqlAlchemyPeerMessageStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

AGENT_ID = generate_agent_id()
ALICE = "alice@example.com"


@pytest.fixture()
def env(runtime_init: None, db_uri: str, tmp_path: Any) -> dict[str, Any]:
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_store.create(agent_id=AGENT_ID, name="test-agent", bundle_location="test:///bundle")
    conv_store = SqlAlchemyConversationStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(ALICE)
    sender_token = "sender-tok"
    sender_runner = token_bound_runner_id(sender_token)
    sender = conv_store.create_conversation(
        title="sender", agent_id=AGENT_ID, runner_id=sender_runner
    )
    receiver = conv_store.create_conversation(title="receiver", agent_id=AGENT_ID)
    perm_store.grant(ALICE, sender.id, LEVEL_OWNER)
    perm_store.grant(ALICE, receiver.id, LEVEL_OWNER)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        host_store=HostStore(db_uri),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
        feature_flags=resolve_feature_flags({"OMNIGENT_FEATURES": "session_peer_messaging"}),
        peer_message_store=SqlAlchemyPeerMessageStore(db_uri),
    )
    return {
        "app": app,
        "sender": sender,
        "receiver": receiver,
        "sender_token": sender_token,
        "conv_store": conv_store,
        "peer_store": SqlAlchemyPeerMessageStore(db_uri),
    }


async def test_sweeper_delivery_succeeds_without_auth_headers_via_override(
    env: dict[str, Any],
) -> None:
    """No headers on the synthetic request; only ``acting_user_id`` gets past auth."""
    sweeper = env["app"].state.peer_sweeper
    assert isinstance(sweeper, PeerSweeper)
    sweeper._app = env["app"]

    peer_store: SqlAlchemyPeerMessageStore = env["peer_store"]
    now = now_epoch()
    record = peer_store.create(
        SessionPeerMessage(
            id="a" * 32,
            sender_session_id=env["sender"].id,
            receiver_session_id=env["receiver"].id,
            ref="ref-override",
            text="hello via sweeper",
            state="pending",
            created_at=now,
            expires_at=now + 3600,
        )
    )

    await sweeper._tick()

    updated = peer_store.get(record.id)
    assert updated is not None
    # Without the override this is exactly the shape a headerless request
    # produces: ``_require_access_and_level`` raises UNAUTHORIZED (a
    # permission store is configured and ``_get_user_id`` has no header to
    # read), ``_deliver`` catches it and maps the non-RUNNER_UNAVAILABLE
    # code to "not_ready" -> ``failed``. Reaching "delivered" here is only
    # possible because ``acting_user_id`` replaced that lookup.
    assert updated.state == "delivered", updated.reason


async def test_inline_send_default_path_unchanged(env: dict[str, Any]) -> None:
    """The ordinary HTTP send (real auth header, no override) still delivers."""
    transport = httpx.ASGITransport(app=env["app"])
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            f"/v1/sessions/{env['receiver'].id}/peer-messages",
            json={"sender_session_id": env["sender"].id, "text": "hi inline"},
            headers={
                "X-Forwarded-Email": ALICE,
                RUNNER_TUNNEL_TOKEN_HEADER: env["sender_token"],
            },
        )
    assert resp.status_code == 200, resp.text
    assert resp.json()["disposition"] == "delivered"
