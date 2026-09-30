"""Store tests for the session-succession child move and its receipt.

The move is one write transaction: it reparents the old session's live
direct children, rewrites the roots of their subtrees, writes the forward
labels and the receipt, and leaves archived children (and invalid targets)
untouched.
"""

from __future__ import annotations

import pytest

from omnigent.stores.conversation_store import (
    SUCCEEDED_BY_LABEL_KEY,
    SUCCEEDS_LABEL_KEY,
    SuccessionRefusedError,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def _child(store: SqlAlchemyConversationStore, parent_id: str, title: str) -> str:
    """Create a titled child of ``parent_id`` and return its id."""
    return store.create_conversation(parent_conversation_id=parent_id, title=title).id


def _move(
    store: SqlAlchemyConversationStore, old_id: str, new_id: str
) -> tuple[list[str], list[str]]:
    return store.reassign_live_children(old_id, new_id, "rcpt-1")


def test_move_takes_live_children_and_their_subtrees(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Live children move with their descendants; an archived child stays."""
    old = conversation_store.create_conversation(title="old")
    a = _child(conversation_store, old.id, "A")
    a1 = _child(conversation_store, a, "A1")
    b = _child(conversation_store, old.id, "B")
    c = _child(conversation_store, old.id, "C")
    conversation_store.update_conversation(c, archived=True)
    new = conversation_store.create_conversation(title="new")

    direct, moved = _move(conversation_store, old.id, new.id)

    assert set(direct) == {a, b}
    assert set(moved) == {a, a1, b}
    for session_id in (a, b):
        row = conversation_store.get_conversation(session_id)
        assert row is not None
        assert row.parent_conversation_id == new.id
        assert row.root_conversation_id == new.id
    grandchild = conversation_store.get_conversation(a1)
    assert grandchild is not None
    assert grandchild.parent_conversation_id == a
    assert grandchild.root_conversation_id == new.id
    archived = conversation_store.get_conversation(c)
    assert archived is not None
    assert archived.parent_conversation_id == old.id
    assert archived.root_conversation_id == old.id

    old_row = conversation_store.get_conversation(old.id)
    assert old_row is not None
    assert old_row.labels[SUCCEEDED_BY_LABEL_KEY] == new.id
    new_row = conversation_store.get_conversation(new.id)
    assert new_row is not None
    assert new_row.labels[SUCCEEDS_LABEL_KEY] == old.id

    receipt = conversation_store.get_succession(old.id, new.id)
    assert receipt is not None
    assert receipt.phase == "moved"
    assert set(receipt.direct_ids) == {a, b}
    assert set(receipt.moved_ids) == {a, a1, b}
    assert receipt.opening is None
    assert receipt.opening_item_id is None
    assert receipt.dropped is None
    assert receipt.questions is None
    assert receipt.error is None


def test_move_without_live_children_writes_nothing(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """An ordinary session whose children are all archived moves nothing."""
    old = conversation_store.create_conversation(title="old")
    archived = _child(conversation_store, old.id, "C")
    conversation_store.update_conversation(archived, archived=True)
    new = conversation_store.create_conversation(title="new")

    assert _move(conversation_store, old.id, new.id) == ([], [])

    assert conversation_store.get_succession(old.id, new.id) is None
    old_row = conversation_store.get_conversation(old.id)
    assert old_row is not None
    assert SUCCEEDED_BY_LABEL_KEY not in old_row.labels
    new_row = conversation_store.get_conversation(new.id)
    assert new_row is not None
    assert SUCCEEDS_LABEL_KEY not in new_row.labels
    child = conversation_store.get_conversation(archived)
    assert child is not None
    assert child.parent_conversation_id == old.id


def test_archived_child_loaded_before_the_move_stays_behind(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A child archived before the call is filtered out by the post-lock re-read."""
    old = conversation_store.create_conversation(title="old")
    live = _child(conversation_store, old.id, "A")
    archived = _child(conversation_store, old.id, "B")
    conversation_store.update_conversation(archived, archived=True)
    new = conversation_store.create_conversation(title="new")

    direct, moved = _move(conversation_store, old.id, new.id)

    assert direct == [live]
    assert moved == [live]
    stayed = conversation_store.get_conversation(archived)
    assert stayed is not None
    assert stayed.parent_conversation_id == old.id
    assert stayed.root_conversation_id == old.id


def test_title_clash_refuses_without_changing_anything(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A kept child whose title matches an existing child of new is refused."""
    old = conversation_store.create_conversation(title="old")
    child = _child(conversation_store, old.id, "clashing")
    new = conversation_store.create_conversation(title="new")
    _child(conversation_store, new.id, "clashing")

    with pytest.raises(SuccessionRefusedError) as excinfo:
        _move(conversation_store, old.id, new.id)

    assert excinfo.value.code == "title_clash"
    assert conversation_store.get_succession(old.id, new.id) is None
    unchanged = conversation_store.get_conversation(child)
    assert unchanged is not None
    assert unchanged.parent_conversation_id == old.id
    assert unchanged.root_conversation_id == old.id
    old_row = conversation_store.get_conversation(old.id)
    assert old_row is not None
    assert SUCCEEDED_BY_LABEL_KEY not in old_row.labels


def test_archived_target_refuses(conversation_store: SqlAlchemyConversationStore) -> None:
    """An archived successor cannot take children, with or without a close."""
    old = conversation_store.create_conversation(title="old")
    _child(conversation_store, old.id, "A")
    new = conversation_store.create_conversation(title="new")
    conversation_store.update_conversation(new.id, archived=True, close_cli_on_archive=False)

    with pytest.raises(SuccessionRefusedError) as excinfo:
        _move(conversation_store, old.id, new.id)

    assert excinfo.value.code == "target_archived"
    assert conversation_store.get_succession(old.id, new.id) is None


def test_child_target_refuses(conversation_store: SqlAlchemyConversationStore) -> None:
    """A successor with a parent is not top level."""
    old = conversation_store.create_conversation(title="old")
    _child(conversation_store, old.id, "A")
    root = conversation_store.create_conversation(title="root")
    child_target = _child(conversation_store, root.id, "N")

    with pytest.raises(SuccessionRefusedError) as excinfo:
        _move(conversation_store, old.id, child_target)

    assert excinfo.value.code == "not_top_level"


def test_child_old_refuses(conversation_store: SqlAlchemyConversationStore) -> None:
    """An old session with a parent is not top level."""
    root = conversation_store.create_conversation(title="root")
    old = _child(conversation_store, root.id, "old")
    _child(conversation_store, old, "A")
    new = conversation_store.create_conversation(title="new")

    with pytest.raises(SuccessionRefusedError) as excinfo:
        _move(conversation_store, old, new.id)

    assert excinfo.value.code == "not_top_level"


def test_same_session_refuses(conversation_store: SqlAlchemyConversationStore) -> None:
    """A session cannot succeed itself."""
    old = conversation_store.create_conversation(title="old")

    with pytest.raises(SuccessionRefusedError) as excinfo:
        _move(conversation_store, old.id, old.id)

    assert excinfo.value.code == "same_session"


def test_missing_target_refuses(conversation_store: SqlAlchemyConversationStore) -> None:
    """An unknown successor id is refused as not_found."""
    old = conversation_store.create_conversation(title="old")

    with pytest.raises(SuccessionRefusedError) as excinfo:
        _move(conversation_store, old.id, "0" * 32)

    assert excinfo.value.code == "not_found"


def test_update_succession_is_a_phase_compare_and_set(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Only the resumer holding the receipt's current phase advances it."""
    old = conversation_store.create_conversation(title="old")
    _child(conversation_store, old.id, "A")
    new = conversation_store.create_conversation(title="new")
    _move(conversation_store, old.id, new.id)

    dropped = [{"kind": "timer", "id": "t1", "label": "wake"}]
    assert (
        conversation_store.update_succession(
            "rcpt-1", expected_phase="moved", phase="rekeyed", dropped=dropped
        )
        is True
    )
    receipt = conversation_store.get_succession_by_id("rcpt-1")
    assert receipt is not None
    assert receipt.phase == "rekeyed"
    assert receipt.dropped == dropped

    assert (
        conversation_store.update_succession("rcpt-1", expected_phase="moved", phase="done")
        is False
    )
    receipt = conversation_store.get_succession_by_id("rcpt-1")
    assert receipt is not None
    assert receipt.phase == "rekeyed"

    opening = {"text": "Welcome", "questions": []}
    assert (
        conversation_store.update_succession(
            "rcpt-1", expected_phase="rekeyed", phase="opened", opening=opening
        )
        is True
    )
    receipt = conversation_store.get_succession_by_id("rcpt-1")
    assert receipt is not None
    assert receipt.phase == "opened"
    assert receipt.opening == opening

    assert conversation_store.update_succession("absent", expected_phase="moved") is False


def test_list_unfinished_successions_omits_done(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Finished receipts drop out of the startup resume scan."""
    first_old = conversation_store.create_conversation(title="old-1")
    _child(conversation_store, first_old.id, "A")
    first_new = conversation_store.create_conversation(title="new-1")
    conversation_store.reassign_live_children(first_old.id, first_new.id, "rcpt-a")

    second_old = conversation_store.create_conversation(title="old-2")
    _child(conversation_store, second_old.id, "B")
    second_new = conversation_store.create_conversation(title="new-2")
    conversation_store.reassign_live_children(second_old.id, second_new.id, "rcpt-b")

    assert (
        conversation_store.update_succession("rcpt-a", expected_phase="moved", phase="done")
        is True
    )

    unfinished = conversation_store.list_unfinished_successions()
    assert [receipt.id for receipt in unfinished] == ["rcpt-b"]


def test_clear_succession_link_only_removes_the_matching_pair(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Undo clears the forward pointer, but never a later succession's link."""
    old = conversation_store.create_conversation(title="old")
    _child(conversation_store, old.id, "A")
    new = conversation_store.create_conversation(title="new")
    _move(conversation_store, old.id, new.id)
    assert (
        conversation_store.update_succession("rcpt-1", expected_phase="moved", phase="done")
        is True
    )

    conversation_store.clear_succession_link(old.id, new.id)

    old_row = conversation_store.get_conversation(old.id)
    assert old_row is not None
    assert SUCCEEDED_BY_LABEL_KEY not in old_row.labels
    new_row = conversation_store.get_conversation(new.id)
    assert new_row is not None
    assert SUCCEEDS_LABEL_KEY not in new_row.labels

    conversation_store.set_labels(old.id, {SUCCEEDED_BY_LABEL_KEY: "other"})
    conversation_store.set_labels(new.id, {SUCCEEDS_LABEL_KEY: "other"})
    conversation_store.clear_succession_link(old.id, new.id)

    old_row = conversation_store.get_conversation(old.id)
    assert old_row is not None
    assert old_row.labels[SUCCEEDED_BY_LABEL_KEY] == "other"
    new_row = conversation_store.get_conversation(new.id)
    assert new_row is not None
    assert new_row.labels[SUCCEEDS_LABEL_KEY] == "other"
