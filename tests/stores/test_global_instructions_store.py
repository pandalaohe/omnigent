"""Tests for :class:`SqlAlchemyGlobalInstructionsStore`.

Exercises ``save``, ``current`` and ``list_revisions`` against a real
SQLite database.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.runtime.instruction_layers import compose_route
from omnigent.stores.global_instructions_store import InstructionRevisionConflict
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


@pytest.mark.parametrize(
    ("scope", "agent", "host"),
    [
        ("agent", "codex", ""),
        ("host", "", "host-a"),
        ("independent", "codex", "host-a"),
        ("mother", "", ""),
        ("child", "", ""),
    ],
)
def test_layer_history_and_blank_latest_are_isolated(store, scope, agent, host):
    shared = store.save("shared", created_by=None)
    key = {"scope": scope, "agent_key": agent, "host_id": host}
    first = store.save("scoped", created_by="admin@example.test", **key)
    cleared = store.save("", created_by=None, **key)
    assert store.current(**key) == cleared
    assert store.list_revisions(**key) == [cleared, first]
    assert store.current() == shared
    assert store.list_revisions() == [shared]
    assert {r.id for r in store.list_layers()} == {shared.id, cleared.id}
    assert first.created_us >= first.created_at * 1_000_000


def test_routes_skip_missing_keys_and_restore_stack_after_clearing_independent(store):
    store.save("shared", created_by=None)
    store.save("agent", created_by=None, scope="agent", agent_key="codex")
    store.save("host", created_by=None, scope="host", host_id="host-a")
    store.save("other host", created_by=None, scope="host", host_id="host-b")
    store.save("other agent", created_by=None, scope="agent", agent_key="claude")
    key = {"scope": "independent", "agent_key": "codex", "host_id": "host-a"}
    store.save("only this route", created_by=None, **key)
    assert compose_route(store.latest_layers("host-a", "codex")).text == "only this route"
    assert (
        compose_route(store.latest_layers("host-b", "claude")).text
        == "shared\n\nother agent\n\nother host"
    )
    store.save(" \n", created_by=None, **key)
    assert compose_route(store.latest_layers("host-a", "codex")).text == "shared\n\nagent\n\nhost"
    assert compose_route(store.latest_layers(None, "codex")).text == "shared\n\nagent"
    assert compose_route(store.latest_layers("host-a", None)).text == "shared\n\nhost"
    assert compose_route(store.latest_layers(None, None)).text == "shared"


def test_layer_reads_and_writes_are_workspace_scoped(store):
    with workspace_scope(11):
        first = store.save("workspace 11", created_by=None)
        store.save(
            "private route",
            created_by=None,
            scope="independent",
            agent_key="codex",
            host_id="host-a",
        )
    with workspace_scope(22):
        assert store.current() is None
        assert store.list_layers() == []
        assert store.latest_layers("host-a", "codex") == []
        second = store.save("workspace 22", created_by=None)
        assert store.list_revisions() == [second]
    with workspace_scope(11):
        assert store.current() == first
        assert store.list_revisions() == [first]
        assert {r.text for r in store.list_layers()} == {"workspace 11", "private route"}


@pytest.mark.parametrize(
    ("scope", "agent", "host"),
    [
        ("unknown", "", ""),
        ("shared", "codex", ""),
        ("shared", "", "host-a"),
        ("agent", "", ""),
        ("agent", "codex", "host-a"),
        ("host", "", ""),
        ("host", "codex", "host-a"),
        ("independent", "codex", ""),
        ("independent", "", "host-a"),
        ("mother", "codex", ""),
        ("child", "", "host-a"),
        ("agent", "  codex", ""),
        ("host", "", " "),
        ("agent", "a" * 65, ""),
    ],
)
def test_invalid_layer_keys_reject_every_scoped_operation(store, scope, agent, host):
    key = {"scope": scope, "agent_key": agent, "host_id": host}
    with pytest.raises(ValueError):
        store.current(**key)
    with pytest.raises(ValueError):
        store.list_revisions(**key)
    with pytest.raises(ValueError):
        store.save("invalid", created_by=None, **key)
    assert store.list_layers() == []


def test_created_us_orders_layers_even_within_same_second(store, monkeypatch):
    ticks = iter([1_000_001, 1_000_002, 1_000_003])
    monkeypatch.setattr(
        "omnigent.stores.global_instructions_store.sqlalchemy_store.now_epoch_us",
        lambda: next(ticks),
    )
    shared = store.save("shared", created_by=None)
    independent = store.save(
        "independent", created_by=None, scope="independent", agent_key="codex", host_id="host-a"
    )
    changed = store.save("new shared", created_by=None)
    assert shared.created_at == independent.created_at == changed.created_at
    assert shared.created_us < independent.created_us < changed.created_us
    assert {r.id for r in store.list_layers()} == {independent.id, changed.id}


@pytest.mark.parametrize("saved", [False, True])
def test_concurrent_guarded_saves_allow_only_one_writer(store, saved):
    first = store.save("original", created_by=None) if saved else None
    ready = Barrier(4)

    def write(index):
        ready.wait(timeout=10)
        try:
            return store.save(
                f"writer {index}",
                created_by=None,
                check_revision=True,
                expected_revision_id=first.id if first else None,
            )
        except InstructionRevisionConflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(write, range(4)))
    winners = [revision for revision in results if revision is not None]
    assert len(winners) == 1
    assert store.current() == winners[0]
    assert len(store.list_revisions()) == (2 if saved else 1)
