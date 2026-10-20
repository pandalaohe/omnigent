"""Child cap population and session-only label behavior."""

from __future__ import annotations

from omnigent.server.child_cap import CHILD_CAP_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def test_count_only_open_unnamed_direct_children(
    split_db_conversation_store: SqlAlchemyConversationStore,
) -> None:
    store = split_db_conversation_store
    parent = store.create_conversation()
    counted = store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)
    named = store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id, sub_agent_name="reviewer"
    )
    archived = store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)
    store.update_conversation(archived.id, archived=True)
    store.create_conversation(kind="sub_agent", parent_conversation_id=counted.id)

    assert store.count_counted_children(parent.id) == 1
    assert store.count_counted_children(counted.id) == 1
    assert store.count_counted_children(named.id) == 0


def test_fork_drops_session_child_cap(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    source = conversation_store.create_conversation()
    conversation_store.set_labels(source.id, {CHILD_CAP_LABEL_KEY: "2", "retained": "yes"})

    fork = conversation_store.fork_conversation(source.id)

    assert CHILD_CAP_LABEL_KEY not in fork.labels
    assert fork.labels["retained"] == "yes"
