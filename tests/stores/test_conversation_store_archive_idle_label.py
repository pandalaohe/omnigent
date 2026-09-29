"""Archive idle-deferral label is written atomically with the archive transition.

The label names the archive revision whose teardown waits for the session
tree to go idle. It must be committed in the same transaction as the
transition so recovery can never observe an archive without its deferral,
and a later transition (unarchive, or an archive without the flag) must
void it.
"""

from __future__ import annotations

from omnigent.stores.conversation_store import ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def test_archive_transition_writes_the_deferral_label_in_the_same_call(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``archive_stop_when_idle`` stamps the new revision atomically."""
    conv = conversation_store.create_conversation()

    archived = conversation_store.update_conversation(
        conv.id,
        archived=True,
        close_cli_on_archive=True,
        archive_stop_when_idle=True,
    )

    assert archived is not None
    assert archived.archive_revision == 1
    assert archived.labels[ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY] == "1"
    stored = conversation_store.get_conversation(conv.id)
    assert stored is not None
    assert stored.labels[ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY] == "1"


def test_unarchive_deletes_the_deferral_label(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The unarchive transition voids and removes the label."""
    conv = conversation_store.create_conversation()
    conversation_store.update_conversation(
        conv.id,
        archived=True,
        close_cli_on_archive=True,
        archive_stop_when_idle=True,
    )

    unarchived = conversation_store.update_conversation(conv.id, archived=False)

    assert unarchived is not None
    assert unarchived.archive_revision == 2
    assert ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY not in unarchived.labels
    stored = conversation_store.get_conversation(conv.id)
    assert stored is not None
    assert ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY not in stored.labels


def test_archive_without_the_flag_removes_a_stale_label(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A plain re-archive drops a label left by an earlier revision."""
    conv = conversation_store.create_conversation()
    conversation_store.update_conversation(
        conv.id,
        archived=True,
        close_cli_on_archive=True,
        archive_stop_when_idle=True,
    )
    conversation_store.update_conversation(conv.id, archived=False)
    conversation_store.set_labels(conv.id, {ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY: "1"})

    archived = conversation_store.update_conversation(
        conv.id,
        archived=True,
        close_cli_on_archive=True,
    )

    assert archived is not None
    assert archived.archive_revision == 3
    assert ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY not in archived.labels


def test_same_value_archive_leaves_the_label_untouched(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """No transition means no label write, even with the flag set."""
    conv = conversation_store.create_conversation()
    conversation_store.update_conversation(
        conv.id,
        archived=True,
        close_cli_on_archive=True,
        archive_stop_when_idle=True,
    )

    same_value = conversation_store.update_conversation(
        conv.id,
        archived=True,
        close_cli_on_archive=True,
        archive_stop_when_idle=False,
    )

    assert same_value is not None
    assert same_value.archive_revision == 1
    assert same_value.labels[ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY] == "1"


def test_flag_without_a_close_request_writes_no_label(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Without ``close_cli_on_archive`` there is no teardown to defer."""
    conv = conversation_store.create_conversation()

    archived = conversation_store.update_conversation(
        conv.id,
        archived=True,
        archive_stop_when_idle=True,
    )

    assert archived is not None
    assert archived.archive_close_requested_revision is None
    assert ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY not in archived.labels
