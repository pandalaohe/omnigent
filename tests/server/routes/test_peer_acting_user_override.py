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

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock

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
from tests.server.helpers import build_agent_bundle

AGENT_ID = generate_agent_id()
ALICE = "alice@example.com"


@pytest.fixture()
async def env(
    runtime_init: None, db_uri: str, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, Any]]:
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_store.create(agent_id=AGENT_ID, name="test-agent", bundle_location="agents/test/bundle")
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
    artifact_store.put("agents/test/bundle", build_agent_bundle("test-agent"))
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
    from omnigent.server.routes import sessions

    async with httpx.AsyncClient(
        base_url="http://runner",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"queued": True})),
    ) as runner:
        monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=runner))
        monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", AsyncMock())
        yield {
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


async def test_stopped_receiver_is_rejected_without_uncertain_retry(env, db_uri):
    from omnigent.server.session_choice import CHOICE_LABEL
    from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
    from tests.server.test_agent_quota import usage_report
    from tests.server.test_session_choice import document

    app = env["app"]
    prefs = SqlAlchemyUserPreferencesStore(db_uri)
    app.state.user_preferences_store = prefs
    app.state.agent_quota_service.preferences = prefs
    value = document()
    value["limits"] = {"a": {"reserve": {"mode": "total", "window": "weekly", "stopPct": 5}}}
    prefs.replace_session_choice(ALICE, value, prefs.session_choice(ALICE)["revision"])
    now = now_epoch()
    app.state.agent_quota_service.report_account(
        ALICE,
        "a",
        usage_report(
            observed_at=now,
            windows=[
                {
                    "id": "weekly",
                    "kind": "weekly",
                    "percent_used": 96,
                    "window_minutes": 10080,
                    "resets_at": now + 86400,
                }
            ],
        ),
        now=now,
    )
    env["conv_store"].set_labels(env["receiver"].id, {CHOICE_LABEL: '{"pool":"a"}'})
    sweeper = app.state.peer_sweeper
    sweeper._app = app
    record = env["peer_store"].create(
        SessionPeerMessage(
            id="b" * 32,
            sender_session_id=env["sender"].id,
            receiver_session_id=env["receiver"].id,
            ref="ref-stopped",
            text="must not run",
            state="pending",
            created_at=now,
            expires_at=now + 3600,
        )
    )
    await sweeper._tick()
    updated = env["peer_store"].get(record.id)
    assert updated.state == "failed"
    assert "Stopped:" in updated.reason


async def test_claim_route_marks_the_copy_and_its_mirror_settles_it_at_once(env):
    """The claim route marks a peer copy pasted; its committed envelope settles it at once."""
    from omnigent.runtime import pending_inputs
    from omnigent.server.routes.sessions.routes_peer import format_peer_envelope

    peer_store: SqlAlchemyPeerMessageStore = env["peer_store"]
    receiver = env["receiver"]
    receiver_token = "receiver-tok"
    assert env["conv_store"].set_runner_id(receiver.id, token_bound_runner_id(receiver_token))
    now = now_epoch()
    legacy, native = (
        peer_store.create(
            SessionPeerMessage(
                id=record_id,
                sender_session_id=env["sender"].id,
                receiver_session_id=receiver.id,
                ref=f"ref-{record_id[0]}",
                text="mirrored body",
                state="delivering",
                reason=reason,
                created_at=now,
                updated_at=now,
                expires_at=now + 3600,
            )
        )
        for record_id, reason in (("c" * 32, None), ("d" * 32, "native_attempt:" + "e" * 32))
    )
    envelopes = {
        record.id: format_peer_envelope(
            sender_session_id=record.sender_session_id,
            sender_title="sender",
            sender_agent_name=None,
            sender_project_id=None,
            ref=record.ref,
            peer_id=record.id,
            text=record.text,
        )
        for record in (legacy, native)
    }
    pending_id = pending_inputs.record(
        receiver.id,
        [{"type": "input_text", "text": envelopes[legacy.id]}],
        peer_message_id=legacy.id,
    )
    pending_inputs.enable_runner_lease(receiver.id, pending_id)
    attempt_id = pending_inputs.delivery_attributes_for(receiver.id, pending_id)[
        "delivery_attempt_id"
    ]
    transport = httpx.ASGITransport(app=env["app"])
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            claim = await client.post(
                f"/v1/sessions/{receiver.id}/pending-inputs/{pending_id}/claim",
                params={"attempt_id": attempt_id, "claim_id": "paste"},
                headers={RUNNER_TUNNEL_TOKEN_HEADER: receiver_token},
            )
            assert claim.json() == {"claimed": True}, claim.text
            # Claimed copies are never resent; the record carries that durably.
            assert peer_store.get(legacy.id).reason == "native_claimed"
            for record in (legacy, native):
                resp = await client.post(
                    f"/v1/sessions/{receiver.id}/events",
                    json={
                        "type": "external_conversation_item",
                        "data": {
                            "item_type": "message",
                            "item_data": {
                                "role": "user",
                                "content": [{"type": "input_text", "text": envelopes[record.id]}],
                            },
                            "response_id": f"resp-{record.id}",
                        },
                    },
                    headers={"X-Forwarded-Email": ALICE},
                )
                assert resp.status_code < 300, resp.text
    finally:
        for leftover in pending_inputs.pending_ids(receiver.id):
            pending_inputs.resolve(receiver.id, leftover)
    # No sweeper tick and no grace: the committed envelope is the receipt.
    assert peer_store.get(legacy.id).state == "delivered"
    # A native attempt keeps its own response-id receipt rule.
    assert peer_store.get(native.id).state == "delivering"


@pytest.mark.parametrize("record_case", ["mark-write-fails", "already-reverted"])
async def test_claim_route_grants_a_peer_paste_only_after_its_durable_mark(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, record_case: str
) -> None:
    """A peer copy whose claim mark is not stored never gets the paste grant."""
    from omnigent.runtime import pending_inputs

    peer_store: SqlAlchemyPeerMessageStore = env["peer_store"]
    receiver = env["receiver"]
    receiver_token = "receiver-tok"
    assert env["conv_store"].set_runner_id(receiver.id, token_bound_runner_id(receiver_token))
    now = now_epoch()
    record = peer_store.create(
        SessionPeerMessage(
            id="f" * 32,
            sender_session_id=env["sender"].id,
            receiver_session_id=receiver.id,
            ref="ref-claim",
            text="claimed body",
            state="pending" if record_case == "already-reverted" else "delivering",
            created_at=now,
            updated_at=now,
            expires_at=now + 3600,
        )
    )
    pending_id = pending_inputs.record(
        receiver.id, [{"type": "input_text", "text": "claimed body"}], peer_message_id=record.id
    )
    pending_inputs.enable_runner_lease(receiver.id, pending_id)
    attempt_id = pending_inputs.delivery_attributes_for(receiver.id, pending_id)[
        "delivery_attempt_id"
    ]
    store_down = {"value": record_case == "mark-write-fails"}
    original_transition = SqlAlchemyPeerMessageStore.transition

    def _transition(self: SqlAlchemyPeerMessageStore, *args: Any, **kwargs: Any) -> bool:
        if store_down["value"]:
            raise RuntimeError("peer store unavailable")
        return original_transition(self, *args, **kwargs)

    monkeypatch.setattr(SqlAlchemyPeerMessageStore, "transition", _transition)
    transport = httpx.ASGITransport(app=env["app"])
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:

            async def _claim() -> httpx.Response:
                return await client.post(
                    f"/v1/sessions/{receiver.id}/pending-inputs/{pending_id}/claim",
                    params={"attempt_id": attempt_id, "claim_id": "paste"},
                    headers={RUNNER_TUNNEL_TOKEN_HEADER: receiver_token},
                )

            first = await _claim()
            if record_case == "already-reverted":
                # The sweeper will resend this record; the runner must not paste it too.
                assert first.json() == {"claimed": False}, first.text
                assert not pending_inputs.runner_input_is_claimed(receiver.id, pending_id)
                stored = peer_store.get(record.id)
                assert stored is not None
                assert (stored.state, stored.reason) == ("pending", None)
                return
            # The runner retries a 5xx with the same claim id until its deadline.
            assert first.status_code == 503, first.text
            assert peer_store.get(record.id).reason is None  # type: ignore[union-attr]
            store_down["value"] = False
            retry = await _claim()
            assert retry.json() == {"claimed": True}, retry.text
            assert peer_store.get(record.id).reason == "native_claimed"  # type: ignore[union-attr]
    finally:
        for leftover in pending_inputs.pending_ids(receiver.id):
            pending_inputs.resolve(receiver.id, leftover)
