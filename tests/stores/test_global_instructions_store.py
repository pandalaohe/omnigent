"""Tests for :class:`SqlAlchemyGlobalInstructionsStore`.

Exercises ``save``, ``current`` and ``list_revisions`` against a real
SQLite database.
"""

from __future__ import annotations

import time

import pytest

from omnigent.stores.global_instructions_store.sqlalchemy_store import (
    SqlAlchemyGlobalInstructionsStore,
)


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyGlobalInstructionsStore:
    """A fresh store backed by the per-test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyGlobalInstructionsStore`.
    """
    return SqlAlchemyGlobalInstructionsStore(db_uri)


def test_current_none_on_blank_store(store: SqlAlchemyGlobalInstructionsStore) -> None:
    """Nothing saved: ``current`` returns ``None``."""
    assert store.current() is None


def test_save_then_current_returns_revision(
    store: SqlAlchemyGlobalInstructionsStore,
) -> None:
    """``save`` round-trips text, author and timestamp into ``current``."""
    saved = store.save("prefer rg", created_by="admin@example.com")
    current = store.current()
    assert current is not None
    assert current == saved
    assert current.text == "prefer rg"
    assert current.created_at > 0
    assert current.created_by == "admin@example.com"


def test_back_to_back_saves_keep_newest_live(
    store: SqlAlchemyGlobalInstructionsStore,
) -> None:
    """Two saves in the same instant leave the second live, stamped no later than now."""
    store.save("first", created_by=None)
    saved = store.save("second", created_by=None)

    current = store.current()
    assert current is not None
    assert current.text == "second"
    assert saved.created_at <= int(time.time())


def test_list_revisions_newest_first_with_limit(
    store: SqlAlchemyGlobalInstructionsStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Revisions list newest first and honor ``limit``.

    The clock is pinned to distinct microsecond ticks so the order is the
    assertion rather than a same-µs race.
    """
    ticks = iter([100_000_000, 200_000_000, 300_000_000])
    monkeypatch.setattr(
        "omnigent.stores.global_instructions_store.sqlalchemy_store.now_epoch_us",
        lambda: next(ticks),
    )
    for text in ("first", "second", "third"):
        store.save(text, created_by=None)

    assert [r.text for r in store.list_revisions()] == ["third", "second", "first"]
    assert [r.text for r in store.list_revisions(limit=2)] == ["third", "second"]
