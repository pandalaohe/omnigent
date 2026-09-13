"""Round-2 regression tests for defects introduced by the round-1 fixes.

Each test FAILS on the pre-round-2 source and passes after the fix:

1. Revision fence: a ``reset_host_under_lease(policy_revision=8)`` cancels
   pending idle intents at revision 8 and older, but never the revision-9
   intents a newer replica just created.
2. Deleted host: orphaned conversations (``host_id`` NULLed by the delete)
   are reset even though enumeration by the dead host id returns nothing.
3. Rotation: a conversation rebound to a different host id is not reset and
   is reported under the rotation count.
5. ``_reconnect_host_action`` is gone and the reconnect path still routes
   deleted / no-policy / policy-active correctly.

(Defect 3's lease-cancellation tests live in
``tests/server/integration/test_hosts_api.py`` beside the existing
lost-lease route test they mirror.)
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select

import omnigent.server.app as app_module
from omnigent.db.db_models import SqlCliReleaseIntent, current_workspace_id
from omnigent.entities.pagination import PagedList
from omnigent.server.cli_release_store import CliReleaseIntentStore
from omnigent.server.cli_retention import CliRetentionCoordinator


def _conv(conversation_id: str, **attrs: object) -> SimpleNamespace:
    fields: dict[str, object] = {
        "id": conversation_id,
        "runner_id": f"runner-{conversation_id}",
        "host_id": "host-a",
    }
    fields.update(attrs)
    return SimpleNamespace(**fields)


class _EmptyStore:
    """Conversation store whose host enumeration finds nothing."""

    def list_conversations(self, **kwargs: object) -> PagedList:
        return PagedList(data=[])

    def get_conversation(self, conversation_id: str) -> SimpleNamespace | None:
        raise AssertionError("not used by this test")


class _OkRouter:
    """Runner router that records reset POSTs and always succeeds."""

    def __init__(self, posts: list) -> None:
        self._posts = posts

    class _Client:
        def __init__(self, posts: list) -> None:
            self._posts = posts

        async def post(self, url: str, *, json: dict, timeout: float) -> SimpleNamespace:
            del timeout
            self._posts.append((url, json))
            return SimpleNamespace(status_code=200)

    def client_for_session_resources(
        self, session_id: str, *, conversation: SimpleNamespace
    ) -> SimpleNamespace:
        return SimpleNamespace(client=self._Client(self._posts), runner_id=conversation.runner_id)


_FENCE_HOST_ID = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
_FENCE_SESSION_IDS = {
    7: "71b2c3d4e5f60718293a4b5c6d7e8f90",
    8: "81b2c3d4e5f60718293a4b5c6d7e8f90",
    9: "91b2c3d4e5f60718293a4b5c6d7e8f90",
}


def _seed_idle_intent(intents: CliReleaseIntentStore, host_id: str, revision: int) -> None:
    intents.ensure_idle_intent(
        host_id=host_id,
        target_session_id=_FENCE_SESSION_IDS[revision],
        runner_id="runner-1",
        family="claude",
        policy_revision=revision,
        runtime_generation="boot:1",
        activity_token="tok",
        idle_threshold_seconds=60,
    )


def _intent_statuses(db_uri: str, host_id: str) -> dict[int, str]:
    engine = create_engine(db_uri)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                select(
                    SqlCliReleaseIntent.policy_revision,
                    SqlCliReleaseIntent.status,
                ).where(SqlCliReleaseIntent.host_id == host_id)
            ).all()
        return dict(rows)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_stale_reset_cancels_only_its_own_revision(db_uri: str) -> None:
    """A revision-8 reset must not cancel the revision-9 generation's cleanup."""
    intents = CliReleaseIntentStore(db_uri)
    for revision in (7, 8, 9):
        _seed_idle_intent(intents, _FENCE_HOST_ID, revision)

    coordinator = CliRetentionCoordinator(
        host_store=SimpleNamespace(),
        conversation_store=_EmptyStore(),
        runner_router=_OkRouter([]),
        intent_store=intents,
    )

    await coordinator.reset_host_under_lease(_FENCE_HOST_ID, policy_revision=8)

    assert (
        intents.count_active_idle(host_id=_FENCE_HOST_ID, family="claude", policy_revision=7) == 0
    )
    assert (
        intents.count_active_idle(host_id=_FENCE_HOST_ID, family="claude", policy_revision=8) == 0
    )
    assert (
        intents.count_active_idle(host_id=_FENCE_HOST_ID, family="claude", policy_revision=9) == 1
    )
    assert _intent_statuses(db_uri, _FENCE_HOST_ID) == {
        7: "cancelled",
        8: "cancelled",
        9: "pending",
    }


def _orphan_coordinator(
    posts: list,
    current: SimpleNamespace | None,
    *,
    host_rows: dict[str, SimpleNamespace | None] | None = None,
) -> CliRetentionCoordinator:
    """Coordinator whose host enumeration is empty but whose re-read hits."""

    class _HostStore:
        def get_host(self, host_id: str) -> SimpleNamespace | None:
            assert host_rows is not None
            return host_rows.get(host_id)

    class _ConversationStore:
        def list_conversations(self, **kwargs: object) -> PagedList:
            # The deleted host id owns nothing anymore: the defect's silent
            # skip reads zero rows here and reports success.
            assert kwargs.get("host_id") == "host-gone"
            return PagedList(data=[])

        def get_conversation(self, conversation_id: str) -> SimpleNamespace | None:
            assert conversation_id == "sess-1"
            return current

    coordinator = CliRetentionCoordinator(
        host_store=_HostStore(),
        conversation_store=_ConversationStore(),
        runner_router=_OkRouter(posts),
    )
    coordinator._last_results[(current_workspace_id(), "host-gone")] = {
        "configured": True,
        "policy_revision": 7,
    }
    return coordinator


@pytest.mark.asyncio
async def test_release_after_delete_resets_orphaned_conversations() -> None:
    """NULLed conversations are reset even though host enumeration is empty."""
    posts: list = []
    orphan = _conv("sess-1", host_id=None)
    coordinator = _orphan_coordinator(posts, orphan)
    stale_view = _conv("sess-1", host_id="host-gone")

    result = await coordinator.release_host_after_delete("host-gone", conversations=[stale_view])

    assert result["status"] == "reset_after_delete"
    assert result["reset_count"] == 1
    assert result["rotated_count"] == 0
    assert posts == [
        (
            "/v1/sessions/sess-1/cli-retention/reset",
            {"host_id": "host-gone", "policy_revision": 7},
        )
    ]


@pytest.mark.asyncio
async def test_release_after_delete_skips_rotated_conversations() -> None:
    """A conversation rebound to a policy-live host is owned by that host's policy."""
    posts: list = []
    rebound = _conv("sess-1", host_id="host-new")
    coordinator = _orphan_coordinator(
        posts,
        rebound,
        host_rows={"host-new": SimpleNamespace(cli_retention_policy=SimpleNamespace(version=1))},
    )
    stale_view = _conv("sess-1", host_id="host-gone")

    result = await coordinator.release_host_after_delete("host-gone", conversations=[stale_view])

    assert result["status"] == "reset_after_delete"
    assert result["reset_count"] == 0
    assert result["rotated_count"] == 1
    assert posts == []


@pytest.mark.asyncio
async def test_release_after_delete_resets_vanished_conversations_from_snapshot() -> None:
    """A conversation gone from the store is still reset from the snapshot.

    Round 3: the row's absence does not prove its runtime is gone (a session
    delete still removes the row when runner-side cleanup failed), so the
    snapshot's id/runner_id drives the reset instead of a silent drop.
    """
    posts: list = []
    coordinator = _orphan_coordinator(posts, None, host_rows={})
    stale_view = _conv("sess-1", host_id="host-gone")

    result = await coordinator.release_host_after_delete("host-gone", conversations=[stale_view])

    assert result["status"] == "reset_after_delete"
    assert result["reset_count"] == 1
    assert result["rotated_count"] == 0
    assert result["vanished_count"] == 1
    assert result["vanished"] == ["sess-1"]
    assert posts == [
        (
            "/v1/sessions/sess-1/cli-retention/reset",
            {"host_id": "host-gone", "policy_revision": 7},
        )
    ]


def test_reconnect_classifier_is_removed_but_routing_survives() -> None:
    """The dead classifier is gone; the reconnect path still branches three ways."""
    assert not hasattr(app_module, "_reconnect_host_action")
    assert not hasattr(app_module, "ReconnectHostAction")

    source = inspect.getsource(app_module.create_app)
    assert "_reconnect_host_action" not in source
    # Deleted rows take the release-after-delete path, live rows without a
    # policy reset, and policy-active rows trigger reconciliation.
    assert "host is None" in source
    assert "release_host_after_delete" in source
    assert "cli_retention_policy is None" in source
    assert ".reset_host(" in source
    assert ".trigger(" in source
