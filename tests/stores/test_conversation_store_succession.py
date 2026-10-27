"""Store tests for the session-succession child move and its receipt.

The move is one write transaction: it reparents the old session's live
direct children, rewrites the roots of their subtrees, writes the forward
labels and the receipt, and leaves archived children (and invalid targets)
untouched.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from omnigent.child_checkin import (
    CHECKIN_LABEL_PREFIX,
    FOLLOWUP_NEXT_KEY,
    FOLLOWUP_PREFIX,
    INSTRUCTIONS_PREFIX,
    LAST_KEY,
    POLICY_KEY,
    FollowUp,
    decode_followup,
    encode_followup,
)
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


@pytest.mark.parametrize("successor_override", [False, True])
def test_move_transfers_checkin_state_and_preserves_successor_overrides(
    conversation_store: SqlAlchemyConversationStore, successor_override: bool
) -> None:
    store = conversation_store
    old = store.create_conversation()
    new = store.create_conversation()
    _child(store, old.id, "child")
    source = {
        POLICY_KEY: '{"g":0,"c":"summary"}',
        f"{INSTRUCTIONS_PREFIX}0": "source start",
        f"{INSTRUCTIONS_PREFIX}1": "source end",
        f"{FOLLOWUP_PREFIX}11111111": encode_followup(FollowUp("11111111", 10, "old work")),
        FOLLOWUP_NEXT_KEY: "stale",
        LAST_KEY: "123",
        f"{CHECKIN_LABEL_PREFIX}future": "move too",
        "other": "stay",
    }
    store.set_labels(old.id, source)
    target = {
        f"{FOLLOWUP_PREFIX}22222222": encode_followup(FollowUp("22222222", 1, "uncertain", True)),
        LAST_KEY: "456",
    }
    if successor_override:
        target |= {POLICY_KEY: '{"g":3600}', f"{INSTRUCTIONS_PREFIX}0": "target only"}
    store.set_labels(new.id, target)

    _move(store, old.id, new.id)

    predecessor = store.get_conversation(old.id)
    successor = store.get_conversation(new.id)
    assert predecessor is not None and successor is not None
    assert {key: value for key, value in predecessor.labels.items() if key in source} == {
        LAST_KEY: "123",
        "other": "stay",
    }
    expected = {
        key: value for key, value in source.items() if key.startswith(CHECKIN_LABEL_PREFIX)
    }
    if successor_override:
        del expected[f"{INSTRUCTIONS_PREFIX}1"]
    expected |= target | {FOLLOWUP_NEXT_KEY: "10"}
    assert {
        key: value
        for key, value in successor.labels.items()
        if key.startswith(CHECKIN_LABEL_PREFIX)
    } == expected


def test_move_merges_followup_collisions_and_keeps_overflow(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    store = conversation_store
    old = store.create_conversation()
    new = store.create_conversation()
    _child(store, old.id, "child")
    source = {
        f"{FOLLOWUP_PREFIX}{index:08x}": encode_followup(FollowUp("", 100 + index, f"old {index}"))
        for index in range(15)
    }
    target = {
        f"{FOLLOWUP_PREFIX}{index:08x}": encode_followup(FollowUp("", 200 + index, f"new {index}"))
        for index in range(10)
    }
    store.set_labels(old.id, source | {LAST_KEY: "123", FOLLOWUP_NEXT_KEY: "wrong"})
    store.set_labels(new.id, target | {FOLLOWUP_NEXT_KEY: "wrong"})

    _move(store, old.id, new.id)

    predecessor = store.get_conversation(old.id)
    successor = store.get_conversation(new.id)
    assert predecessor is not None and successor is not None
    merged = {
        key: value for key, value in successor.labels.items() if key.startswith(FOLLOWUP_PREFIX)
    }
    assert len(merged) == 25
    assert len({item.text for raw in merged.values() if (item := decode_followup(raw))}) == 25
    assert all(merged[key] == value for key, value in target.items())
    assert all(len(key.removeprefix(FOLLOWUP_PREFIX)) == 8 for key in merged)
    assert successor.labels[FOLLOWUP_NEXT_KEY] == "100"
    assert LAST_KEY not in successor.labels
    assert {
        key: value
        for key, value in predecessor.labels.items()
        if key.startswith(CHECKIN_LABEL_PREFIX)
    } == {LAST_KEY: "123"}


def test_empty_move_clears_stale_followup_hint_without_copying_last(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    store = conversation_store
    old = store.create_conversation()
    new = store.create_conversation()
    source = {
        f"{FOLLOWUP_PREFIX}11111111": encode_followup(FollowUp("", 1, "uncertain", True)),
        f"{FOLLOWUP_PREFIX}22222222": "malformed",
        FOLLOWUP_NEXT_KEY: "1",
        LAST_KEY: "123",
    }
    store.set_labels(old.id, source)
    store.set_labels(new.id, {FOLLOWUP_NEXT_KEY: "1"})
    assert store.reassign_live_children(old.id, new.id, "empty", allow_empty=True) == ([], [])
    successor = store.get_conversation(new.id)
    assert successor is not None
    assert FOLLOWUP_NEXT_KEY not in successor.labels
    assert LAST_KEY not in successor.labels
    assert successor.labels[f"{FOLLOWUP_PREFIX}22222222"] == "malformed"


def test_failed_receipt_rolls_back_checkin_transfer_and_children(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    store = conversation_store
    first_old = store.create_conversation()
    first_new = store.create_conversation()
    _child(store, first_old.id, "first child")
    _move(store, first_old.id, first_new.id)
    old = store.create_conversation()
    new = store.create_conversation()
    child = _child(store, old.id, "second child")
    state = {f"{FOLLOWUP_PREFIX}11111111": encode_followup(FollowUp("", 1, "work"))}
    store.set_labels(old.id, state)

    with pytest.raises(IntegrityError):
        _move(store, old.id, new.id)

    predecessor = store.get_conversation(old.id)
    successor = store.get_conversation(new.id)
    child_row = store.get_conversation(child)
    assert predecessor is not None and successor is not None and child_row is not None
    assert predecessor.labels == state
    assert successor.labels == {}
    assert child_row.parent_conversation_id == old.id
    assert store.get_succession(old.id, new.id) is None


def test_competing_successors_have_one_winner(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store = conversation_store
    old = store.create_conversation(title="old").id
    child = _child(store, old, "child")
    targets = [store.create_conversation(title=title).id for title in ("one", "two")]
    barrier = Barrier(2)

    def compete(target: str) -> str:
        barrier.wait(timeout=10)
        try:
            store.reassign_live_children(old, target, f"receipt-{target}", allow_empty=True)
            return "won"
        except SuccessionRefusedError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(compete, targets))
    assert sorted(outcomes) == ["already_succeeded", "won"]
    winner = targets[outcomes.index("won")]
    assert store.get_conversation(old).labels[SUCCEEDED_BY_LABEL_KEY] == winner
    assert store.get_conversation(child).parent_conversation_id == winner


@pytest.mark.parametrize("wrapper", ["claude-code-native-ui-subagent", "codex-native-ui-subagent"])
def test_direct_parent_owned_mirror_stays_with_old(
    conversation_store: SqlAlchemyConversationStore,
    wrapper: str,
) -> None:
    store = conversation_store
    old = store.create_conversation(title="old").id
    new = store.create_conversation(title="new").id
    child = _child(store, old, "child")
    mirror = _child(store, old, "mirror")
    nested = _child(store, child, "nested mirror")
    for session_id in (mirror, nested):
        store.set_labels(session_id, {"omnigent.wrapper": wrapper})
    direct, moved = _move(store, old, new)
    assert direct == [child]
    assert set(moved) == {child, nested}
    assert store.get_conversation(mirror).parent_conversation_id == old
    store.update_conversation(old, archived=True, close_cli_on_archive=False)
    assert store.get_conversation(mirror).archived
    assert not store.get_conversation(nested).archived
