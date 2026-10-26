"""Shared runner ownership for server stops and host launch frames."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes._sessions.helpers import retained_runner_ids, runner_is_exclusive
from omnigent.stores.conversation_store import ConversationStore


@pytest.mark.parametrize(
    ("other_runner", "archived", "expected"),
    [
        ("shared", False, False),
        ("shared", True, True),
        ("other", False, True),
    ],
)
def test_runner_is_exclusive(other_runner: str, archived: bool, expected: bool) -> None:
    rows = [
        Conversation(
            id="self", root_conversation_id="self", created_at=0, updated_at=0, runner_id="shared"
        ),
        Conversation(
            id="child",
            root_conversation_id="self",
            created_at=0,
            updated_at=0,
            runner_id=other_runner,
            archived=archived,
        ),
    ]
    assert runner_is_exclusive("shared", "self", rows) is expected


def test_retained_runners_include_hostless_children_across_all_pages() -> None:
    root = Conversation(
        id="self",
        root_conversation_id="self",
        created_at=0,
        updated_at=0,
        runner_id="new",
        host_id="host-a",
    )
    child = Conversation(
        id="child",
        root_conversation_id=root.id,
        created_at=0,
        updated_at=0,
        runner_id="shared",
        parent_conversation_id=root.id,
    )
    remote = Conversation(
        id="remote",
        root_conversation_id="remote",
        created_at=0,
        updated_at=0,
        runner_id="remote",
        host_id="host-b",
    )
    archived = Conversation(
        id="archived",
        root_conversation_id="archived",
        created_at=0,
        updated_at=0,
        runner_id="finished",
        host_id="host-a",
        archived=True,
    )
    store = Mock(spec=ConversationStore)
    store.list_conversations.side_effect = [
        SimpleNamespace(data=[root], has_more=True),
        SimpleNamespace(data=[child, remote, archived], has_more=False),
    ]
    rows = {row.id: row for row in [root, child, remote, archived]}
    store.get_conversation.side_effect = rows.get
    assert retained_runner_ids(store, "host-a", root.id) == ["shared"]
    store.get_conversation.assert_not_called()
    assert store.list_conversations.call_args_list[0].kwargs["kind"] is None
    assert store.list_conversations.call_args_list[1].kwargs["after"] == root.id


@pytest.mark.parametrize("failure", [RuntimeError("read failed"), asyncio.CancelledError()])
@pytest.mark.asyncio
async def test_relaunch_retention_failure_precedes_binding_and_pending_state(
    failure: BaseException,
) -> None:
    from omnigent.server.routes._sessions.helpers import _launch_runner_on_host_locked

    store = Mock(spec=ConversationStore)
    store.list_conversations.side_effect = failure
    registry = SimpleNamespace(admit_launch=AsyncMock())
    connection = SimpleNamespace(host_id="host-a", pending_launches={})
    conv = Conversation(
        id="self",
        root_conversation_id="self",
        created_at=0,
        updated_at=0,
        runner_id="old",
        host_id="host-a",
        workspace="/opt/work/app",
    )
    with pytest.raises(type(failure)):
        await _launch_runner_on_host_locked(conv, store, registry, connection)
    store.replace_runner_id.assert_not_called()
    registry.admit_launch.assert_not_awaited()
    assert connection.pending_launches == {}


def test_retained_runners_read_missing_ancestors_once() -> None:
    children = [
        Conversation(
            id=f"child-{i}",
            root_conversation_id="missing-root",
            created_at=0,
            updated_at=0,
            runner_id="shared",
            parent_conversation_id="missing-parent",
        )
        for i in range(1000)
    ]
    parent = Conversation(
        id="missing-parent",
        root_conversation_id="missing-root",
        created_at=0,
        updated_at=0,
        parent_conversation_id="missing-root",
    )
    root = Conversation(
        id="missing-root",
        root_conversation_id="missing-root",
        created_at=0,
        updated_at=0,
        host_id="host-a",
    )
    store = Mock(spec=ConversationStore)
    store.list_conversations.return_value = SimpleNamespace(data=children, has_more=False)
    store.get_conversation.side_effect = {parent.id: parent, root.id: root}.get
    assert retained_runner_ids(store, "host-a", "self") == ["shared"]
    assert [call.args[0] for call in store.get_conversation.call_args_list] == [parent.id, root.id]


def test_retained_runners_walk_real_store_pages(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.auth import RESERVED_USER_LOCAL
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.host_store import HostStore

    host_id = "1" * 32
    HostStore(db_uri).upsert_on_connect(host_id, "test-host", RESERVED_USER_LOCAL)
    store = SqlAlchemyConversationStore(db_uri)
    root = store.create_conversation(
        title="root", host_id=host_id, workspace="/opt/work/app", runner_id="2" * 32
    )
    store.create_conversation(parent_conversation_id=root.id, runner_id="3" * 32, title="child")
    page = store.list_conversations
    reads = []

    def small_page(**kwargs):
        reads.append(kwargs.get("after"))
        return page(**{**kwargs, "limit": 1})

    monkeypatch.setattr(store, "list_conversations", small_page)
    assert retained_runner_ids(store, host_id, root.id) == ["3" * 32]
    assert len(reads) == 2 and reads[1] is not None
