"""Self-healing archive-close fence for dead leases left across unarchive."""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from omnigent.server.routes.sessions.routes_events import _archive_blocks_external_user_work
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _request() -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(host_store=None)))


def _archive_claim_unarchive(store: SqlAlchemyConversationStore, *, claimed_at: int = 100):
    conv = store.create_conversation()
    archived = store.update_conversation(conv.id, archived=True, close_cli_on_archive=True)
    assert archived is not None
    assert (
        store.claim_archive_close(
            conv.id,
            archived.archive_revision,
            "worker",
            claimed_at=claimed_at,
            stale_before=0,
        )
        == "claimed"
    )
    unarchived = store.update_conversation(conv.id, archived=False)
    assert unarchived is not None
    fresh = store.get_conversation(conv.id)
    assert fresh is not None
    assert fresh.archive_close_claimed is True
    return fresh


@pytest.mark.asyncio
async def test_stale_dead_lease_is_cleared_and_does_not_block(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = _archive_claim_unarchive(store, claimed_at=100)

    assert await _archive_blocks_external_user_work(_request(), conv, store) is False

    settled = store.get_conversation(conv.id)
    assert settled is not None
    assert settled.archive_close_claimed is False

    fresh = store.get_conversation(conv.id)
    assert fresh is not None
    assert await _archive_blocks_external_user_work(_request(), fresh, store) is False


@pytest.mark.asyncio
async def test_live_renewed_lease_still_blocks(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = _archive_claim_unarchive(store, claimed_at=100)
    now = int(time.time())
    assert store.renew_archive_close_claim(conv.id, "worker", claimed_at=now)

    fresh = store.get_conversation(conv.id)
    assert fresh is not None
    assert await _archive_blocks_external_user_work(_request(), fresh, store) is True

    held = store.get_conversation(conv.id)
    assert held is not None
    assert held.archive_close_claimed is True


@pytest.mark.asyncio
async def test_current_archived_request_still_blocks_with_token_untouched(
    db_uri: str,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    archived = store.update_conversation(conv.id, archived=True, close_cli_on_archive=True)
    assert archived is not None
    assert (
        store.claim_archive_close(
            conv.id,
            archived.archive_revision,
            "owner-a",
            claimed_at=100,
            stale_before=0,
        )
        == "claimed"
    )
    calls: list[tuple[str, int]] = []
    orig = store.clear_stale_archive_close_claim

    def _spy(conversation_id: str, *, stale_before: int) -> bool:
        calls.append((conversation_id, stale_before))
        return orig(conversation_id, stale_before=stale_before)

    store.clear_stale_archive_close_claim = _spy  # type: ignore[method-assign]
    try:
        fresh = store.get_conversation(conv.id)
        assert fresh is not None
        assert await _archive_blocks_external_user_work(_request(), fresh, store) is True
    finally:
        store.clear_stale_archive_close_claim = orig  # type: ignore[method-assign]

    assert calls == []
    held = store.get_conversation(conv.id)
    assert held is not None
    assert held.archive_close_claimed is True


@pytest.mark.asyncio
async def test_no_claim_makes_no_clear_call(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    calls: list[tuple[str, int]] = []
    orig = store.clear_stale_archive_close_claim

    def _spy(conversation_id: str, *, stale_before: int) -> bool:
        calls.append((conversation_id, stale_before))
        return orig(conversation_id, stale_before=stale_before)

    store.clear_stale_archive_close_claim = _spy  # type: ignore[method-assign]
    try:
        fresh = store.get_conversation(conv.id)
        assert fresh is not None
        assert await _archive_blocks_external_user_work(_request(), fresh, store) is False
    finally:
        store.clear_stale_archive_close_claim = orig  # type: ignore[method-assign]

    assert calls == []
