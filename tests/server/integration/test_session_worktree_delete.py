"""
Integration tests for opt-in git worktree cleanup on session delete.

Drives ``DELETE /v1/sessions/{id}`` through the full app with a fake
host registered in ``app.state.host_registry``. Verifies the
``?delete_branch`` flag gates whether a ``host.remove_worktree`` frame
is sent, and that the stored worktree path + branch (not request input)
are used. See designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.connect import HostProcess
from omnigent.host.frames import (
    CAP_WORKTREE_SAFE_ARCHIVE,
    HostCreateWorktreeFrame,
    HostHelloFrame,
    HostListWorktreesFrame,
    HostRemoveWorktreeFrame,
    decode_host_frame,
)
from omnigent.host.git_worktree import (
    CreatedWorktree,
    WorktreeError,
    WorktreeInfo,
    create_worktree,
    list_worktrees,
    remove_worktree,
)
from omnigent.host.identity import HostIdentity
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.routes._host_worktree import (
    WORKTREE_ROOT_LABEL_KEY,
    WorktreeHostRefusalError,
    WorktreeHostUnavailableError,
    refresh_worktree_admission_fence,
    worktree_root_fingerprint,
)
from omnigent.server.routes._sessions.helpers import _remove_session_worktree_best_effort
from omnigent.stores.conversation_store import (
    ARCHIVE_REMOVED_WORKTREE_LABEL_KEY,
    ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY,
    worktree_admission_fingerprint,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_host_binding_store.sqlalchemy_store import (
    SqlAlchemyProjectHostBindingStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.host.test_git_worktree import _branch_exists, _git

pytestmark = pytest.mark.asyncio

_HOST_ID = "a65b7d8e4613a95946c9134383308ac7"
_OTHER_HOST_ID = "b65b7d8e4613a95946c9134383308ac7"


async def test_safe_cleanup_blocks_admission_during_and_after_removal(db_uri: str) -> None:
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    first = SqlAlchemyConversationStore(db_uri)
    second = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    archived = first.create_conversation(
        host_id=_HOST_ID, workspace=root, git_branch="feature/topic"
    )
    first.update_conversation(archived.id, archived=True)
    registry = SimpleNamespace(get=lambda _host_id: SimpleNamespace())
    removal_entered = asyncio.Event()
    release_removal = asyncio.Event()

    async def remove_on_host(**_kwargs):
        removal_entered.set()
        await release_removal.wait()

    with (
        patch(
            "omnigent.server.routes._host_worktree.list_worktrees_on_host",
            AsyncMock(return_value=[{"path": root, "branch": "feature/topic", "is_main": False}]),
        ),
        patch("omnigent.server.routes._host_worktree.remove_worktree_on_host", remove_on_host),
    ):
        cleanup = asyncio.create_task(
            _remove_session_worktree_best_effort(
                host_id=_HOST_ID,
                worktree_path=root,
                branch="feature/topic",
                delete_branch=False,
                host_registry=registry,
                reason="session-archive",
                conversation_store=first,
                exclude_conversation_id=archived.id,
                safe_only=True,
            )
        )
        try:
            await asyncio.wait_for(removal_entered.wait(), 3)
            with pytest.raises(OmnigentError, match="lease is busy"):
                await asyncio.to_thread(
                    second.create_conversation, host_id=_HOST_ID, workspace=f"{root}/packages/app"
                )
            with pytest.raises(OmnigentError, match="lease is busy"):
                await first.delete_conversation(archived.id)
        finally:
            release_removal.set()
        assert await asyncio.wait_for(cleanup, 3) is True
    row = first.get_conversation(archived.id)
    assert row is not None
    assert ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY in row.labels
    with pytest.raises(OmnigentError, match="Worktree was removed"):
        second.create_conversation(host_id=_HOST_ID, workspace=f"{root}/packages/app")


async def test_unarchive_after_safe_removal_does_not_admit_old_worktree(db_uri: str) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    store = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    archived = store.create_conversation(
        host_id=_HOST_ID, workspace=root, git_branch="feature/topic"
    )
    store.update_conversation(archived.id, archived=True)
    registry = SimpleNamespace(get=lambda _host_id: SimpleNamespace())
    with (
        patch(
            "omnigent.server.routes._host_worktree.list_worktrees_on_host",
            AsyncMock(return_value=[{"path": root, "branch": "feature/topic", "is_main": False}]),
        ),
        patch("omnigent.server.routes._host_worktree.remove_worktree_on_host", AsyncMock()),
    ):
        assert await _remove_session_worktree_best_effort(
            host_id=_HOST_ID,
            worktree_path=root,
            branch="feature/topic",
            delete_branch=False,
            host_registry=registry,
            reason="session-archive",
            conversation_store=store,
            exclude_conversation_id=archived.id,
            safe_only=True,
        )
    undone = store.update_conversation(archived.id, archived=False)
    assert undone is not None and undone.archived is False
    with pytest.raises(OmnigentError, match="unresolved"):
        store.set_runner_id(archived.id, "0123456789abcdef0123456789abcdef")
    with pytest.raises(OmnigentError, match="unresolved"):
        store.replace_runner_id(archived.id, "0123456789abcdef0123456789abcdef")
    with pytest.raises(OmnigentError, match="unresolved"):
        store.set_worktree(archived.id, f"{root}/packages/app")
    with pytest.raises(OmnigentError, match="unresolved"):
        store.set_runner_id(
            archived.id,
            "0123456789abcdef0123456789abcdef",
            admission_host_id=_HOST_ID,
            admission_workspace="/opt/work/sample-app/new-topic",
        )
    store.set_labels(
        archived.id,
        {ARCHIVE_REMOVED_WORKTREE_LABEL_KEY: str(archived.archive_revision)},
    )
    assert store.set_runner_id(
        archived.id,
        "0123456789abcdef0123456789abcdef",
        admission_host_id=_HOST_ID,
        admission_workspace="/opt/work/sample-app/new-topic",
    )
    assert await store.delete_conversation(archived.id)


async def test_parent_delete_keeps_descendant_with_unresolved_worktree_fence(
    db_uri: str,
) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    root = "/opt/work/sample-app/child-topic"
    child = store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=parent.id,
        host_id=_HOST_ID,
        workspace=root,
        git_branch="feature/child-topic",
    )
    archived_child = store.update_conversation(child.id, archived=True)
    assert archived_child is not None
    store.set_labels(
        child.id,
        {
            ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY: worktree_admission_fingerprint(
                _HOST_ID, root
            )
        },
    )
    with pytest.raises(OmnigentError, match="Worktree removal is unresolved"):
        await store.delete_conversation(parent.id)
    assert store.get_conversation(parent.id) is not None
    assert store.get_conversation(child.id) is not None
    store.set_labels(
        child.id,
        {ARCHIVE_REMOVED_WORKTREE_LABEL_KEY: str(archived_child.archive_revision)},
    )
    assert await store.delete_conversation(parent.id)
    assert store.get_conversation(child.id) is None


async def test_project_entry_rebind_uses_cleanup_lease_and_fence(db_uri: str) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    project_id = "0123456789abcdef0123456789abcdef"
    SqlAlchemyProjectStore(db_uri).create(project_id, "Sample", "alice@example.com")
    bindings = SqlAlchemyProjectHostBindingStore(db_uri)
    store = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    bindings.put_entry(project_id, _HOST_ID, "/opt/work/sample-app/other-entry")
    archived = store.create_conversation(
        host_id=_HOST_ID, workspace=root, git_branch="feature/topic"
    )
    store.update_conversation(archived.id, archived=True)
    registry = SimpleNamespace(get=lambda _host_id: SimpleNamespace())
    remove_entered = asyncio.Event()
    release_remove = asyncio.Event()

    async def blocked_remove(**_kwargs: object) -> None:
        remove_entered.set()
        await release_remove.wait()

    with (
        patch(
            "omnigent.server.routes._host_worktree.list_worktrees_on_host",
            AsyncMock(return_value=[{"path": root, "branch": "feature/topic", "is_main": False}]),
        ),
        patch("omnigent.server.routes._host_worktree.remove_worktree_on_host", blocked_remove),
    ):
        cleanup = asyncio.create_task(
            _remove_session_worktree_best_effort(
                host_id=_HOST_ID,
                worktree_path=root,
                branch="feature/topic",
                delete_branch=False,
                host_registry=registry,
                reason="session-archive",
                conversation_store=store,
                exclude_conversation_id=archived.id,
                safe_only=True,
                project_host_binding_store=bindings,
            )
        )
        try:
            await asyncio.wait_for(remove_entered.wait(), 3)
            with pytest.raises(OmnigentError, match="lease is busy"):
                await asyncio.to_thread(
                    bindings.put_entry, project_id, _HOST_ID, f"{root}/subproject"
                )
        finally:
            release_remove.set()
        assert await asyncio.wait_for(cleanup, 3)
    with pytest.raises(OmnigentError, match="Worktree was removed"):
        bindings.put_entry(project_id, _HOST_ID, f"{root}/subproject")
    assert bindings.entry_at_or_under(_HOST_ID, root) is False
    assert bindings.list_entries(project_id)[0].workspace == "/opt/work/sample-app/other-entry"


async def test_project_entry_fence_reads_split_conversation_database(
    db_uri: str, tmp_path: Path
) -> None:
    conv_uri = f"sqlite:///{tmp_path / 'conversations.db'}"
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    project_id = "0123456789abcdef0123456789abcdef"
    SqlAlchemyProjectStore(db_uri).create(project_id, "Sample", "alice@example.com")
    store = SqlAlchemyConversationStore(db_uri, conv_uri)
    root = "/opt/work/sample-app/topic"
    archived = store.create_conversation(host_id=_HOST_ID, workspace=root)
    store.set_labels(
        archived.id,
        {
            ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY: worktree_admission_fingerprint(
                _HOST_ID, root
            )
        },
    )
    bindings = SqlAlchemyProjectHostBindingStore(db_uri, conv_uri)
    with pytest.raises(OmnigentError, match="Worktree was removed"):
        bindings.put_entry(project_id, _HOST_ID, f"{root}/subproject")


async def test_safe_cleanup_waits_for_committed_admission(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    first = SqlAlchemyConversationStore(db_uri)
    second = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    archived = first.create_conversation(
        host_id=_HOST_ID, workspace=root, git_branch="feature/topic"
    )
    first.update_conversation(archived.id, archived=True)
    admission_entered = threading.Event()
    release_admission = threading.Event()
    real_check = second._worktree_admission_fenced

    def blocked_check(host_id: str, path: str) -> bool:
        admission_entered.set()
        if not release_admission.wait(3):
            raise AssertionError("admission did not resume")
        return real_check(host_id, path)

    monkeypatch.setattr(second, "_worktree_admission_fenced", blocked_check)
    admission = asyncio.create_task(
        asyncio.to_thread(
            second.create_conversation, host_id=_HOST_ID, workspace=f"{root}/packages/app"
        )
    )
    assert await asyncio.to_thread(admission_entered.wait, 3)
    registry = SimpleNamespace(get=lambda _host_id: SimpleNamespace())
    list_host = AsyncMock(
        return_value=[{"path": root, "branch": "feature/topic", "is_main": False}]
    )
    remove_host = AsyncMock()
    with (
        patch("omnigent.server.routes._host_worktree.list_worktrees_on_host", list_host),
        patch("omnigent.server.routes._host_worktree.remove_worktree_on_host", remove_host),
    ):
        cleanup = asyncio.create_task(
            _remove_session_worktree_best_effort(
                host_id=_HOST_ID,
                worktree_path=root,
                branch="feature/topic",
                delete_branch=False,
                host_registry=registry,
                reason="session-archive",
                conversation_store=first,
                exclude_conversation_id=archived.id,
                safe_only=True,
            )
        )
        release_admission.set()
        admitted = await asyncio.wait_for(admission, 3)
        assert admitted.id
        assert await asyncio.wait_for(cleanup, 3) is False
    list_host.assert_not_awaited()
    remove_host.assert_not_awaited()


async def test_rebind_to_other_host_waits_for_source_host_fence_installation(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "old-host", RESERVED_USER_LOCAL)
    HostStore(db_uri).upsert_on_connect(_OTHER_HOST_ID, "new-host", RESERVED_USER_LOCAL)
    cleanup_store = SqlAlchemyConversationStore(db_uri)
    binding_store = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    archived = cleanup_store.create_conversation(
        host_id=_HOST_ID, workspace=root, git_branch="feature/topic"
    )
    cleanup_store.update_conversation(archived.id, archived=True)
    sharing_entered = threading.Event()
    release_sharing = threading.Event()

    def blocked_sharing(**_kwargs: object) -> bool:
        sharing_entered.set()
        if not release_sharing.wait(3):
            raise AssertionError("cleanup sharing check was not released")
        return False

    monkeypatch.setattr(cleanup_store, "has_other_live_session_in_workspace", blocked_sharing)
    registry = SimpleNamespace(get=lambda _host_id: SimpleNamespace())
    with patch(
        "omnigent.server.routes._host_worktree.list_worktrees_on_host",
        AsyncMock(return_value=[]),
    ):
        cleanup = asyncio.create_task(
            _remove_session_worktree_best_effort(
                host_id=_HOST_ID,
                worktree_path=root,
                branch="feature/topic",
                delete_branch=False,
                host_registry=registry,
                reason="session-archive",
                conversation_store=cleanup_store,
                exclude_conversation_id=archived.id,
                safe_only=True,
            )
        )
        try:
            assert await asyncio.to_thread(sharing_entered.wait, 3)
            current = binding_store.get_conversation(archived.id)
            assert current is not None
            assert ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY not in current.labels
            with pytest.raises(OmnigentError, match="lease is busy"):
                await asyncio.to_thread(
                    binding_store.set_host_id,
                    archived.id,
                    _OTHER_HOST_ID,
                    workspace="/opt/work/other-app/topic",
                )
        finally:
            release_sharing.set()
        assert await asyncio.wait_for(cleanup, 3) is False
    current = binding_store.get_conversation(archived.id)
    assert current is not None and current.host_id == _HOST_ID


async def test_safe_cleanup_waits_for_undo_and_rechecks_archived_revision(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    cleanup_store = SqlAlchemyConversationStore(db_uri)
    undo_store = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    archived = cleanup_store.create_conversation(
        host_id=_HOST_ID, workspace=root, git_branch="feature/topic"
    )
    cleanup_store.update_conversation(archived.id, archived=True)
    admission_entered = threading.Event()
    release_admission = threading.Event()
    host_store = undo_store._host_binding_store
    real_acquire = host_store.acquire_worktree_admission

    def blocked_acquire(host_id: str, **kwargs: object) -> str | None:
        token = real_acquire(host_id, **kwargs)
        admission_entered.set()
        if not release_admission.wait(3):
            raise AssertionError("Undo did not resume")
        return token

    monkeypatch.setattr(host_store, "acquire_worktree_admission", blocked_acquire)
    undo = asyncio.create_task(
        asyncio.to_thread(undo_store.update_conversation, archived.id, archived=False)
    )
    assert await asyncio.to_thread(admission_entered.wait, 3)
    registry = SimpleNamespace(get=lambda _host_id: SimpleNamespace())
    list_host = AsyncMock()
    remove_host = AsyncMock()
    with (
        patch("omnigent.server.routes._host_worktree.list_worktrees_on_host", list_host),
        patch("omnigent.server.routes._host_worktree.remove_worktree_on_host", remove_host),
    ):
        cleanup = asyncio.create_task(
            _remove_session_worktree_best_effort(
                host_id=_HOST_ID,
                worktree_path=root,
                branch="feature/topic",
                delete_branch=False,
                host_registry=registry,
                reason="session-archive",
                conversation_store=cleanup_store,
                exclude_conversation_id=archived.id,
                safe_only=True,
            )
        )
        release_admission.set()
        assert (await asyncio.wait_for(undo, 3)).archived is False
        assert await asyncio.wait_for(cleanup, 3) is False
    list_host.assert_not_awaited()
    remove_host.assert_not_awaited()


async def test_safe_cleanup_preserves_archived_peer_without_completed_cli_release(
    db_uri: str,
) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    store = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    old = store.create_conversation(host_id=_HOST_ID, workspace=root, git_branch="feature/topic")
    peer = store.create_conversation(host_id=_HOST_ID, workspace=root)
    store.update_conversation(old.id, archived=True)
    store.update_conversation(peer.id, archived=True, close_cli_on_archive=False)
    registry = SimpleNamespace(get=lambda _host_id: SimpleNamespace())
    list_host = AsyncMock()
    with patch("omnigent.server.routes._host_worktree.list_worktrees_on_host", list_host):
        removed = await _remove_session_worktree_best_effort(
            host_id=_HOST_ID,
            worktree_path=root,
            branch="feature/topic",
            delete_branch=False,
            host_registry=registry,
            reason="session-archive",
            conversation_store=store,
            exclude_conversation_id=old.id,
            safe_only=True,
        )
    assert removed is False
    list_host.assert_not_awaited()


async def test_fresh_host_validation_allows_recreated_retained_branch(db_uri: str) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    store = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    archived = store.create_conversation(
        host_id=_HOST_ID, workspace=root, git_branch="feature/topic"
    )
    store.update_conversation(archived.id, archived=True)
    store.set_labels(
        archived.id,
        {
            ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY: worktree_admission_fingerprint(
                _HOST_ID, root
            )
        },
    )
    registry = SimpleNamespace()
    conn = SimpleNamespace()
    with patch(
        "omnigent.server.routes._host_worktree.list_worktrees_on_host",
        AsyncMock(
            return_value=[
                {"path": root, "branch": "feature/topic", "is_main": False, "detached": False}
            ]
        ),
    ):
        assert (
            await refresh_worktree_admission_fence(
                host_registry=registry,
                host_conn=conn,
                conversation_store=store,
                host_id=_HOST_ID,
                workspace=f"{root}/packages/app",
                branch="feature/topic",
            )
            is True
        )
    recreated = store.create_conversation(
        host_id=_HOST_ID, workspace=f"{root}/packages/app", git_branch="feature/topic"
    )
    assert recreated.id
    row = store.get_conversation(archived.id)
    assert row is not None
    assert ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY not in row.labels


async def test_cancelled_refresh_releases_lease_acquired_in_thread(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    store = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    archived = store.create_conversation(
        host_id=_HOST_ID, workspace=root, git_branch="feature/topic"
    )
    store.update_conversation(archived.id, archived=True)
    store.set_labels(
        archived.id,
        {
            ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY: worktree_admission_fingerprint(
                _HOST_ID, root
            )
        },
    )
    host_store = store._host_binding_store
    real_acquire = host_store.acquire_worktree_admission
    acquired = threading.Event()
    release_acquire = threading.Event()

    def delayed_acquire(host_id: str, **kwargs: object) -> str | None:
        token = real_acquire(host_id, **kwargs)
        acquired.set()
        if not release_acquire.wait(3):
            raise AssertionError("refresh acquisition was not released")
        return token

    monkeypatch.setattr(host_store, "acquire_worktree_admission", delayed_acquire)
    listed = asyncio.Event()

    async def list_host(**_kwargs: object) -> list[dict[str, object]]:
        listed.set()
        return [{"path": root, "branch": "feature/topic", "is_main": False, "detached": False}]

    with patch("omnigent.server.routes._host_worktree.list_worktrees_on_host", list_host):
        refresh = asyncio.create_task(
            refresh_worktree_admission_fence(
                host_registry=SimpleNamespace(),
                host_conn=SimpleNamespace(),
                conversation_store=store,
                host_id=_HOST_ID,
                workspace=root,
                branch="feature/topic",
            )
        )
        try:
            assert await asyncio.to_thread(acquired.wait, 3)
            refresh.cancel()
            with pytest.raises(asyncio.CancelledError):
                await refresh
        finally:
            release_acquire.set()
        await asyncio.wait_for(listed.wait(), 3)
    token = None
    for _ in range(100):
        try:
            token = real_acquire(_HOST_ID, required=True)
            break
        except OmnigentError as exc:
            assert exc.code == ErrorCode.CONFLICT
            await asyncio.sleep(0.01)
    assert token is not None
    assert host_store.release_cli_retention(_HOST_ID, token)


async def test_timed_out_remove_cannot_validate_old_tree_before_host_worker_settles(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A later host listing waits for the old remove, even after RPC timeout."""
    from omnigent.server.routes import _host_worktree as proxy

    HostStore(db_uri).upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    store = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    archived = store.create_conversation(
        host_id=_HOST_ID, workspace=root, git_branch="feature/topic"
    )
    store.update_conversation(archived.id, archived=True)
    host = HostProcess(
        identity=HostIdentity(host_id=_HOST_ID, name="worktree-host"),
        server_url="http://localhost:8000",
    )
    old_tree_exists = True
    remove_started = threading.Event()
    release_remove = threading.Event()
    list_executions = 0

    def delayed_remove(**_kwargs: object) -> None:
        nonlocal old_tree_exists
        remove_started.set()
        if not release_remove.wait(3):
            raise AssertionError("old removal was not released")
        old_tree_exists = False

    def listed(**_kwargs: object) -> list[WorktreeInfo]:
        nonlocal list_executions
        list_executions += 1
        return (
            [WorktreeInfo(path=root, branch="feature/topic", is_main=False, detached=False)]
            if old_tree_exists
            else []
        )

    def recreated(**_kwargs: object) -> CreatedWorktree:
        nonlocal old_tree_exists
        old_tree_exists = True
        return CreatedWorktree(worktree_path=root, branch="feature/topic", workspace=root)

    class RpcRegistry:
        def __init__(self) -> None:
            self.conn = SimpleNamespace(
                host_id=_HOST_ID,
                hello=SimpleNamespace(capabilities=[CAP_WORKTREE_SAFE_ARCHIVE]),
                pending_remove_worktrees={},
                pending_list_worktrees={},
            )
            self.tasks: set[asyncio.Task[None]] = set()
            self.remove_handler: asyncio.Task[None] | None = None
            self.second_list_sent = asyncio.Event()
            self.list_requests = 0

        def get(self, _host_id: str) -> SimpleNamespace:
            return self.conn

        def send_text(self, _conn: SimpleNamespace, raw: str) -> None:
            frame = decode_host_frame(raw)
            if isinstance(frame, HostListWorktreesFrame):
                self.list_requests += 1
                if self.list_requests == 2:
                    self.second_list_sent.set()

            async def dispatch() -> None:
                if isinstance(frame, HostListWorktreesFrame):
                    result = await host._handle_list_worktrees(frame)
                    pending = self.conn.pending_list_worktrees
                elif isinstance(frame, HostRemoveWorktreeFrame):
                    result = await host._handle_remove_worktree(frame)
                    pending = self.conn.pending_remove_worktrees
                else:
                    raise AssertionError(type(frame))
                future = pending.get(frame.request_id)
                if future is not None and not future.done():
                    future.set_result(asdict(result))

            task = asyncio.create_task(dispatch())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            if isinstance(frame, HostRemoveWorktreeFrame):
                self.remove_handler = task

    registry = RpcRegistry()
    monkeypatch.setattr("omnigent.host.connect.remove_worktree", delayed_remove)
    monkeypatch.setattr("omnigent.host.connect.list_worktrees", listed)
    monkeypatch.setattr("omnigent.host.connect.create_worktree", recreated)
    monkeypatch.setattr(proxy, "_WORKTREE_TIMEOUT_S", 0.05)
    try:
        cleanup = asyncio.create_task(
            _remove_session_worktree_best_effort(
                host_id=_HOST_ID,
                worktree_path=root,
                branch="feature/topic",
                delete_branch=False,
                host_registry=registry,
                reason="session-archive",
                conversation_store=store,
                exclude_conversation_id=archived.id,
                safe_only=True,
            )
        )
        assert await asyncio.to_thread(remove_started.wait, 3)
        assert await asyncio.wait_for(cleanup, 3) is False
        with pytest.raises(OmnigentError, match="Worktree removal is unresolved"):
            await store.delete_conversation(archived.id)
        assert store.get_conversation(archived.id) is not None
        store.update_conversation(archived.id, archived=False)
        HostStore(db_uri).upsert_on_connect(_OTHER_HOST_ID, "other-host", RESERVED_USER_LOCAL)
        with pytest.raises(OmnigentError, match="unresolved"):
            store.set_host_id(
                archived.id,
                _OTHER_HOST_ID,
                workspace="/opt/work/other-app/topic",
                git_branch="feature/other",
            )
        with pytest.raises(OmnigentError, match="unresolved"):
            store.set_host_id(
                archived.id,
                _HOST_ID,
                workspace="/opt/work/sample-app/different-topic",
                git_branch="feature/different",
            )
        with pytest.raises(OmnigentError, match="unresolved"):
            store.set_worktree(archived.id, "/opt/work/sample-app/different-topic")
        with pytest.raises(OmnigentError, match="unresolved"):
            store.set_runner_id(
                archived.id,
                "0123456789abcdef0123456789abcdef",
                admission_host_id=_OTHER_HOST_ID,
                admission_workspace="/opt/work/other-app/topic",
            )
        rearchived = store.update_conversation(archived.id, archived=True)
        assert rearchived is not None and rearchived.host_id == _HOST_ID
        assert rearchived.labels[ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY] == (
            worktree_admission_fingerprint(_HOST_ID, root)
        )
        assert registry.remove_handler is not None
        registry.remove_handler.cancel()
        with pytest.raises(asyncio.CancelledError):
            await registry.remove_handler
        with pytest.raises(WorktreeHostUnavailableError):
            await refresh_worktree_admission_fence(
                host_registry=registry,
                host_conn=registry.conn,
                conversation_store=store,
                host_id=_HOST_ID,
                workspace=root,
                branch="feature/topic",
            )
        assert registry.second_list_sent.is_set()
        assert list_executions == 1  # only cleanup's pre-remove listing ran
        with pytest.raises(OmnigentError, match="Worktree was removed"):
            store.create_conversation(host_id=_HOST_ID, workspace=f"{root}/packages/app")
    finally:
        release_remove.set()
    for _ in range(100):
        if not host._host_subprocess_tasks and not registry.tasks:
            break
        await asyncio.sleep(0.01)
    assert old_tree_exists is False
    assert not host._host_subprocess_tasks
    monkeypatch.setattr(proxy, "_WORKTREE_TIMEOUT_S", 1.0)
    created = await host._handle_create_worktree(
        HostCreateWorktreeFrame(
            request_id="recreate",
            repo_path=root,
            branch_name="feature/topic",
            base_branch=None,
            existing_branch=True,
        )
    )
    assert created.status == "ok"
    assert await refresh_worktree_admission_fence(
        host_registry=registry,
        host_conn=registry.conn,
        conversation_store=store,
        host_id=_HOST_ID,
        workspace=root,
        branch="feature/topic",
    )
    admitted = store.create_conversation(host_id=_HOST_ID, workspace=f"{root}/packages/app")
    assert admitted.id
    assert await store.delete_conversation(archived.id)


async def test_second_archive_cannot_replace_unresolved_fence_for_another_root(
    db_uri: str,
) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "old-host", RESERVED_USER_LOCAL)
    HostStore(db_uri).upsert_on_connect(_OTHER_HOST_ID, "new-host", RESERVED_USER_LOCAL)
    store = SqlAlchemyConversationStore(db_uri)
    old_root = "/opt/work/old-app/topic"
    new_root = "/opt/work/new-app/topic"
    archived = store.create_conversation(
        host_id=_OTHER_HOST_ID, workspace=new_root, git_branch="feature/topic"
    )
    store.update_conversation(archived.id, archived=True)
    old_fence = worktree_admission_fingerprint(_HOST_ID, old_root)
    store.set_labels(archived.id, {ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY: old_fence})
    store.update_conversation(archived.id, archived=False)
    store.update_conversation(archived.id, archived=True)
    registry = SimpleNamespace(get=lambda _host_id: SimpleNamespace())
    remove_host = AsyncMock()
    with (
        patch(
            "omnigent.server.routes._host_worktree.list_worktrees_on_host",
            AsyncMock(
                return_value=[{"path": new_root, "branch": "feature/topic", "is_main": False}]
            ),
        ),
        patch("omnigent.server.routes._host_worktree.remove_worktree_on_host", remove_host),
    ):
        removed = await _remove_session_worktree_best_effort(
            host_id=_OTHER_HOST_ID,
            worktree_path=new_root,
            branch="feature/topic",
            delete_branch=False,
            host_registry=registry,
            reason="session-archive",
            conversation_store=store,
            exclude_conversation_id=archived.id,
            safe_only=True,
        )
    assert removed is False
    remove_host.assert_not_awaited()
    current = store.get_conversation(archived.id)
    assert current is not None
    assert current.labels[ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY] == old_fence
    with pytest.raises(OmnigentError, match="Worktree was removed"):
        store.create_conversation(host_id=_HOST_ID, workspace=f"{old_root}/packages/app")


@pytest.mark.parametrize("uncertain", [False, True])
async def test_safe_cleanup_fence_distinguishes_host_refusal_from_uncertain_result(
    db_uri: str, uncertain: bool
) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    store = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    archived = store.create_conversation(
        host_id=_HOST_ID, workspace=root, git_branch="feature/topic"
    )
    store.update_conversation(archived.id, archived=True)
    store.set_labels(archived.id, {ARCHIVE_REMOVED_WORKTREE_LABEL_KEY: "0"})
    registry = SimpleNamespace(get=lambda _host_id: SimpleNamespace())
    error = (
        WorktreeHostUnavailableError("response lost")
        if uncertain
        else WorktreeHostRefusalError("worktree is dirty")
    )
    with (
        patch(
            "omnigent.server.routes._host_worktree.list_worktrees_on_host",
            AsyncMock(return_value=[{"path": root, "branch": "feature/topic", "is_main": False}]),
        ),
        patch(
            "omnigent.server.routes._host_worktree.remove_worktree_on_host",
            AsyncMock(side_effect=error),
        ),
    ):
        removed = await _remove_session_worktree_best_effort(
            host_id=_HOST_ID,
            worktree_path=root,
            branch="feature/topic",
            delete_branch=False,
            host_registry=registry,
            reason="session-archive",
            conversation_store=store,
            exclude_conversation_id=archived.id,
            safe_only=True,
        )
    assert removed is False
    row = store.get_conversation(archived.id)
    assert row is not None
    assert (ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY in row.labels) is uncertain
    assert ARCHIVE_REMOVED_WORKTREE_LABEL_KEY not in row.labels
    if uncertain:
        with pytest.raises(OmnigentError, match="Worktree removal is unresolved"):
            await store.delete_conversation(archived.id)


async def test_stale_host_admission_lease_can_be_reclaimed(db_uri: str) -> None:
    import time

    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(_HOST_ID, "lease-test", RESERVED_USER_LOCAL)
    assert host_store.claim_cli_retention(
        _HOST_ID,
        "stale",
        claimed_at=int(time.time()) - 16 * 60,
        stale_before=int(time.time()) - 17 * 60,
    )
    token = host_store.acquire_worktree_admission(_HOST_ID, required=True)
    assert token is not None and token != "stale"
    assert host_store.release_cli_retention(_HOST_ID, token)


class _FakeWebSocket:
    """Minimal WebSocket stand-in (the registry only enqueues)."""

    async def send_text(self, data: str) -> None:
        """No-op send — frames flow through the outbound queue.

        :param data: JSON-encoded frame text (ignored).
        """


async def _register_fake_host(
    app: FastAPI,
    db_uri: str,
    *,
    worktree_path: str | None = None,
    branch: str = "feature/login",
) -> list[HostRemoveWorktreeFrame]:
    """Register a fake host and start a drain that captures remove frames.

    :param app: The app whose ``host_registry`` to register into.
    :param db_uri: DB URI so the host row (FK target) can be upserted.
    :param worktree_path: Optional root returned by the host's worktree listing.
    :param branch: Branch the host's worktree listing reports.
    :returns: A list that accumulates every ``HostRemoveWorktreeFrame``
        the server sends to this host.
    """
    # Upsert the host row so the conversation's host_id FK resolves.
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host", RESERVED_USER_LOCAL)
    registry = app.state.host_registry
    conn = registry.register(
        host_id=_HOST_ID,
        ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
        hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="wt-host"),
        owner=RESERVED_USER_LOCAL,
    )
    captured: list[HostRemoveWorktreeFrame] = []

    async def _drain() -> None:
        """Capture remove-worktree frames and reply ok."""
        while True:
            frame_text = await conn.outbound_queue.get()
            if frame_text is None:
                return
            frame = decode_host_frame(frame_text)
            if isinstance(frame, HostRemoveWorktreeFrame):
                captured.append(frame)
                fut = conn.pending_remove_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result({"status": "ok", "error": None})
            elif isinstance(frame, HostListWorktreesFrame):
                fut = conn.pending_list_worktrees.pop(frame.request_id, None)
                if fut is not None and not fut.done():
                    fut.set_result(
                        {
                            "status": "ok",
                            "worktrees": [
                                {
                                    "path": worktree_path or _WORKTREE_PATH,
                                    "branch": branch,
                                    "is_main": False,
                                }
                            ],
                        }
                    )

    task = asyncio.create_task(_drain())
    # Stash so the caller can stop the drain on teardown.
    conn._drain_task_for_test = task  # type: ignore[attr-defined]
    return captured


_WORKTREE_PATH = "/Users/alice/myrepo-worktrees/feature-login"


def _make_worktree_conversation(
    db_uri: str,
    workspace: str = _WORKTREE_PATH,
    worktree_root: str | None = _WORKTREE_PATH,
    *,
    worktree: str | None = None,
    git_branch: str = "feature/login",
) -> str:
    """Create a session row that looks like a server-created worktree.

    :param db_uri: DB URI for the conversation store.
    :param workspace: Launch directory to record; defaults to the shared
        fixture path so two calls produce two sessions in one directory.
    :param worktree: Recorded working tree, or ``None`` for a legacy row
        whose launch directory is its working tree.
    :param git_branch: Branch recorded on the row.
    :param worktree_root: Recorded cleanup root, or None for a legacy session.
    :returns: The new conversation id.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(
        agent_id=None,
        host_id=_HOST_ID,
        workspace=workspace,
        worktree=worktree,
        git_branch=git_branch,
        labels=(
            {WORKTREE_ROOT_LABEL_KEY: worktree_root_fingerprint(worktree_root)}
            if worktree_root is not None
            else None
        ),
    )
    return conv.id


class _Entries:
    """Minimal entry guard for the delete route's ``entry_at_or_under``."""

    def __init__(self, entries: set[tuple[str, str]]) -> None:
        self._entries = entries

    def entry_at_or_under(self, host_id: str, workspace: str) -> bool:
        """Return whether any project has an entry at or inside ``(host_id, workspace)``."""
        base = workspace.rstrip("/\\")
        return any(
            entry_host == host_id
            and (entry_workspace == base or entry_workspace.startswith((base + "/", base + "\\")))
            for entry_host, entry_workspace in self._entries
        )


async def test_delete_with_flag_sends_remove_worktree(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``?delete_branch=true`` on a worktree session sends a
    host.remove_worktree frame carrying the stored path + branch.

    If no frame is captured, the delete-flow gate or the proxy call
    is broken and the user's checkbox would silently do nothing. The
    path/branch assertions prove the server uses the *stored* values
    (not request input), which is the multi-user-safe contract.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    assert resp.status_code == 200

    # Exactly one remove frame, with the stored worktree path/branch
    # and delete_branch=True (the box was checked).
    assert len(captured) == 1, (
        f"Expected exactly one host.remove_worktree frame, got {len(captured)}. "
        "0 means the delete-flow cleanup gate didn't fire; >1 means it fired twice."
    )
    frame = captured[0]
    assert frame.worktree_path == "/Users/alice/myrepo-worktrees/feature-login"
    assert frame.branch == "feature/login"
    assert frame.delete_branch is True


async def test_delete_without_flag_sends_no_remove_worktree(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Deleting a worktree session WITHOUT the flag leaves the worktree
    alone — no host.remove_worktree frame is sent.

    If a frame is captured here, the cleanup is happening
    unconditionally and would destroy worktrees/branches the user
    never asked to remove.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}")
    assert resp.status_code == 200
    # Default is delete_branch=false → no cleanup.
    assert captured == []


@pytest.mark.parametrize(
    ("first_subdir", "second_subdir"),
    [("", ""), ("/web", ""), ("", "/web"), ("/web", "/packages/app")],
)
async def test_delete_shared_worktree_keeps_it_until_the_last_session(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    first_subdir: str,
    second_subdir: str,
) -> None:
    """
    Two sessions in one worktree: deleting the first leaves the directory,
    deleting the last removes it.

    A fork reusing the source's worktree, or two sessions attached to the
    same existing one, both run in the same cwd. If the first delete
    removed it, the survivor's runner would be left on a deleted
    directory and stop responding.
    """
    captured = await _register_fake_host(app, db_uri)
    first = _make_worktree_conversation(db_uri, _WORKTREE_PATH + first_subdir)
    second = _make_worktree_conversation(db_uri, _WORKTREE_PATH + second_subdir)
    assert first != second

    resp = await client.delete(f"/v1/sessions/{first}?delete_branch=true")
    assert resp.status_code == 200
    assert captured == [], (
        "worktree removed while another live session still runs there — "
        "that session's runner is now on a deleted directory"
    )

    resp = await client.delete(f"/v1/sessions/{second}?delete_branch=true")
    assert resp.status_code == 200
    assert len(captured) == 1, "the last session out must remove the worktree"
    assert captured[0].worktree_path == _WORKTREE_PATH
    assert captured[0].delete_branch is True


async def test_delete_removes_worktree_shared_only_with_archived_session(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    An archived session in the same worktree does not keep it alive.

    Archived sessions run nothing, so the directory can go. Counting them
    would mean a worktree shared by two forks is never cleaned up once
    either one is archived — the cleanup would silently never happen.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    archived = _make_worktree_conversation(db_uri)
    conv_store.update_conversation(archived, archived=True)
    live = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{live}?delete_branch=true")
    assert resp.status_code == 200
    assert len(captured) == 1, "only an archived session shares this path — remove it"
    assert captured[0].worktree_path == _WORKTREE_PATH


def test_has_other_live_session_in_workspace(db_uri: str) -> None:
    """The store gate itself: other live sessions count, self and archived
    sessions don't, and a different path never does."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    mine = _make_worktree_conversation(db_uri)

    # Alone in the directory.
    assert not conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )

    # A fork reusing the directory — visible from either side.
    theirs = _make_worktree_conversation(db_uri)
    for viewer in (mine, theirs):
        assert conv_store.has_other_live_session_in_workspace(
            host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=viewer
        )

    # Archiving the other one frees the directory.
    conv_store.update_conversation(theirs, archived=True)
    assert not conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )

    # A different path, and a different host, never count.
    assert not conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace="/Users/alice/elsewhere", exclude_conversation_id=mine
    )
    assert not conv_store.has_other_live_session_in_workspace(
        host_id="b" * 32, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )


def test_has_other_live_session_answers_in_use_past_the_scan_bound(db_uri: str) -> None:
    """More sharers than the scan bound answers "in use" without checking
    archived state — the safe direction, since a wrong "free" deletes a
    directory a running session is sitting in."""
    from omnigent.stores.conversation_store import sqlalchemy_store

    conv_store = SqlAlchemyConversationStore(db_uri)
    mine = _make_worktree_conversation(db_uri)
    others = [
        _make_worktree_conversation(db_uri)
        for _ in range(sqlalchemy_store._WORKSPACE_SHARER_SCAN_LIMIT + 1)
    ]
    # Archived to the last one: the bound short-circuits before archived state
    # is consulted, so the answer is still "in use".
    for cid in others:
        conv_store.update_conversation(cid, archived=True)

    assert conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )


def test_shared_worktree_check_stays_cheap(db_uri: str) -> None:
    """
    The gate's cost, which the delete path is sensitive to: one query when
    nothing else is in the directory, and a second only when it really is
    shared.

    A regression here is invisible behaviourally — the delete still returns
    the right answer, just slower on every session delete.
    """
    from sqlalchemy import event

    conv_store = SqlAlchemyConversationStore(db_uri)
    mine = _make_worktree_conversation(db_uri)

    statements: list[str] = []
    for engine in {conv_store._engine, conv_store._conv_engine}:
        event.listen(
            engine,
            "before_cursor_execute",
            # PRAGMAs are per-connection setup, not work this gate asked for.
            lambda conn, cur, stmt, params, ctx, many: (
                statements.append(stmt) if not stmt.startswith("PRAGMA") else None
            ),
        )

    assert not conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )
    assert len(statements) == 1, (
        f"expected a single query when the directory is unshared, got {len(statements)}: "
        f"{statements}. The archived filter must not open the second database "
        "on the common path."
    )

    _make_worktree_conversation(db_uri)
    statements.clear()
    assert conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )
    assert len(statements) == 2, (
        f"a shared directory should cost the candidate query plus the archived "
        f"filter, got {len(statements)}: {statements}"
    )

    # Past the bound the answer is already settled, so the archived filter is
    # skipped and its IN list can never grow with the directory.
    from omnigent.stores.conversation_store import sqlalchemy_store

    for _ in range(sqlalchemy_store._WORKSPACE_SHARER_SCAN_LIMIT):
        _make_worktree_conversation(db_uri)
    statements.clear()
    assert conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=mine
    )
    assert len(statements) == 1, (
        "past the scan bound the answer is already known, so the archived filter "
        f"must not run: {statements}"
    )


async def test_delete_non_worktree_session_ignores_flag(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``?delete_branch=true`` on a session with no worktree
    (``git_branch`` NULL) is a no-op — no remove frame.

    The gate keys off ``git_branch IS NOT NULL``; without that check a
    plain session delete would try to remove a worktree that doesn't
    exist.
    """
    captured = await _register_fake_host(app, db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    # Plain session: no host, no workspace, no git_branch.
    conv = conv_store.create_conversation(agent_id=None)

    resp = await client.delete(f"/v1/sessions/{conv.id}?delete_branch=true")
    assert resp.status_code == 200
    assert captured == []


def _upsert_host_row(db_uri: str) -> None:
    """Insert the host row without registering a live tunnel.

    :param db_uri: DB URI so the conversation's host_id FK resolves.
    """
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host", RESERVED_USER_LOCAL)


def test_conversation_worktree_round_trip(db_uri: str) -> None:
    """A created session keeps its launch directory and working tree distinct."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(
        agent_id=None,
        host_id=_HOST_ID,
        workspace="/Users/alice/entry",
        worktree=_WORKTREE_PATH,
        git_branch="feature/login",
    )
    loaded = conv_store.get_conversation(conv.id)
    assert loaded is not None
    assert loaded.workspace == "/Users/alice/entry"
    assert loaded.worktree == _WORKTREE_PATH
    assert loaded.git_branch == "feature/login"
    legacy = conv_store.create_conversation(agent_id=None, host_id=_HOST_ID, workspace="/w")
    assert conv_store.get_conversation(legacy.id).worktree is None


def test_set_host_id_worktree_and_clear_host_binding(db_uri: str) -> None:
    """``set_host_id``'s None means "leave it"; ``clear_host_binding`` nulls it."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(agent_id=None)
    bound = conv_store.set_host_id(
        conv.id, _HOST_ID, workspace="/Users/alice/entry", worktree=_WORKTREE_PATH
    )
    assert (bound.workspace, bound.worktree) == ("/Users/alice/entry", _WORKTREE_PATH)
    unchanged = conv_store.set_host_id(conv.id, _HOST_ID)
    assert (unchanged.workspace, unchanged.worktree) == ("/Users/alice/entry", _WORKTREE_PATH)
    moved = conv_store.set_host_id(conv.id, _HOST_ID, worktree="/elsewhere")
    assert moved.worktree == "/elsewhere"
    cleared = conv_store.clear_host_binding(conv.id)
    assert (
        cleared.host_id,
        cleared.workspace,
        cleared.worktree,
        cleared.git_branch,
        cleared.runner_id,
    ) == (None, None, None, None, None)


def test_set_worktree_sets_and_clears_a_hostless_row(db_uri: str) -> None:
    """``set_worktree`` is the host-less placement write: explicit ``None`` clears."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(agent_id=None)
    assert conv_store.set_worktree(conv.id, "/entry/nested/wt").worktree == "/entry/nested/wt"
    assert conv_store.get_conversation(conv.id).worktree == "/entry/nested/wt"
    assert conv_store.set_worktree(conv.id, None).worktree is None
    assert conv_store.get_conversation(conv.id).worktree is None


def test_has_other_live_session_matches_effective_worktree(db_uri: str) -> None:
    """The sharing check compares ``worktree ?? workspace``, never the entry."""
    conv_store = SqlAlchemyConversationStore(db_uri)
    entry_session = conv_store.create_conversation(
        agent_id=None,
        host_id=_HOST_ID,
        workspace="/Users/alice/entry",
        worktree=_WORKTREE_PATH,
    )
    legacy = conv_store.create_conversation(
        agent_id=None, host_id=_HOST_ID, workspace=_WORKTREE_PATH
    )
    # The entry session's launch directory is not the directory being cleaned.
    assert not conv_store.has_other_live_session_in_workspace(
        host_id=_HOST_ID,
        workspace="/Users/alice/entry",
        exclude_conversation_id=legacy.id,
    )
    # Its worktree does count, whichever row is the one being excluded.
    for exclude in (entry_session.id, legacy.id):
        assert conv_store.has_other_live_session_in_workspace(
            host_id=_HOST_ID, workspace=_WORKTREE_PATH, exclude_conversation_id=exclude
        )


class _OfflineRunnerRouter:
    """Runner router that reports every bound session's runner as offline."""

    def client_for_session_resources(self, session_id: str, **kwargs: object) -> object:
        del session_id, kwargs
        raise OmnigentError(
            "runner 'runner_token_offline' is offline",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )


def _assert_worktree_offline_conflict(resp: httpx.Response) -> None:
    """Assert the Option B 409 body for offline worktree cleanup.

    :param resp: DELETE response that should refuse the cleanup.
    """
    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == "conflict"
    assert "runner offline" in error["message"]
    assert "delete_branch=false" in error["message"]


def _assert_session_still_exists(db_uri: str, conv_id: str) -> None:
    """The conversation row must still be in the store.

    These fixture sessions have no agent binding, so ``GET /v1/sessions/{id}``
    500s on snapshot build. The store read is the existence check.

    :param db_uri: DB URI for the conversation store.
    :param conv_id: Session id that must still exist.
    """
    conv = SqlAlchemyConversationStore(db_uri).get_conversation(conv_id)
    assert conv is not None, (
        f"session {conv_id} was deleted; it must remain after a refused worktree cleanup"
    )


async def test_delete_with_flag_when_host_offline_returns_conflict(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    ``?delete_branch=true`` on a worktree session whose host is not
    connected must 409 with an actionable message — not 404, and not
    a silent skip that leaves the caller thinking the session is gone.

    The git worktree lives on the host; an offline host cannot run
    ``git worktree remove``. The session must remain so the user can
    retry with ``delete_branch=false``.
    """
    _upsert_host_row(db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    _assert_worktree_offline_conflict(resp)
    _assert_session_still_exists(db_uri, conv_id)


async def test_refused_delete_keeps_session_files(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A 409-refused delete must be non-destructive end to end: the
    session's files must survive alongside the row, so a later retry
    (runner back online, or without the flag) deletes a fully intact
    session rather than one whose files were already destroyed.
    """
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    _upsert_host_row(db_uri)
    conv_id = _make_worktree_conversation(db_uri)
    file_store = SqlAlchemyFileStore(db_uri)
    stored = file_store.create("notes.txt", 4, "text/plain", session_id=conv_id)

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    _assert_worktree_offline_conflict(resp)
    _assert_session_still_exists(db_uri, conv_id)
    assert file_store.get(stored.id, session_id=conv_id) is not None, (
        "the refused delete must not have destroyed the session's files"
    )


async def test_delete_without_flag_when_host_offline_still_deletes(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Without ``delete_branch``, an offline host must not block delete."""
    _upsert_host_row(db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{conv_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True

    get_resp = await client.get(f"/v1/sessions/{conv_id}")
    assert get_resp.status_code == 404


async def test_delete_with_flag_when_runner_offline_and_host_offline_returns_conflict(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Worktree session, runner unreachable, delete_branch set. Must not
    map to 404 (session-not-found); the session exists and
    the user owns it — cleanup cannot proceed because the host/runner
    tunnel is down.
    """
    from omnigent.runtime import _globals, set_runner_router

    _upsert_host_row(db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    prior = _globals._runner_router
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    try:
        resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    finally:
        set_runner_router(prior)

    _assert_worktree_offline_conflict(resp)
    _assert_session_still_exists(db_uri, conv_id)


async def test_delete_with_flag_when_runner_offline_but_host_online_cleans_up(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Git cleanup rides the host tunnel, not the runner. A dead runner on
    a still-connected host must still send ``host.remove_worktree`` and
    delete the session — failing this would block cleanup that can proceed.
    """
    from omnigent.runtime import _globals, set_runner_router

    captured = await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    prior = _globals._runner_router
    set_runner_router(_OfflineRunnerRouter())  # type: ignore[arg-type]
    try:
        resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    finally:
        set_runner_router(prior)

    assert resp.status_code == 200, resp.text
    assert len(captured) == 1
    assert captured[0].delete_branch is True

    get_resp = await client.get(f"/v1/sessions/{conv_id}")
    assert get_resp.status_code == 404


async def test_delete_shared_worktree_when_host_offline_still_deletes_non_last(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    An offline host must not 409 deleting a session that shares its
    worktree — that delete would not have removed the directory.
    """
    _upsert_host_row(db_uri)
    first = _make_worktree_conversation(db_uri)
    second = _make_worktree_conversation(db_uri)

    resp = await client.delete(f"/v1/sessions/{first}?delete_branch=true")
    assert resp.status_code == 200, resp.text

    # Last remaining session still needs the host to clean up.
    resp = await client.delete(f"/v1/sessions/{second}?delete_branch=true")
    _assert_worktree_offline_conflict(resp)
    _assert_session_still_exists(db_uri, second)


async def test_delete_with_flag_when_host_drops_during_remove_returns_conflict(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host that drops mid-remove is the same as offline: 409, session stays."""
    from omnigent.server.routes._host_worktree import WorktreeHostUnavailableError

    await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    async def _unavailable(**_kwargs: object) -> None:
        raise WorktreeHostUnavailableError("host connection lost during worktree removal")

    monkeypatch.setattr(
        "omnigent.server.routes._host_worktree.remove_worktree_on_host",
        _unavailable,
    )

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    _assert_worktree_offline_conflict(resp)
    _assert_session_still_exists(db_uri, conv_id)


async def test_delete_with_flag_still_succeeds_on_host_git_failure(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reachable host that reports a git error must not block the delete.

    Unavailability is the caller's problem (retry without the flag).
    A git failure after we reached the host is still best-effort.
    """
    from omnigent.server.routes._host_worktree import WorktreeProxyError

    await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri)

    async def _git_failed(**_kwargs: object) -> None:
        raise WorktreeProxyError("worktree removal failed: not a git repo")

    monkeypatch.setattr(
        "omnigent.server.routes._host_worktree.remove_worktree_on_host",
        _git_failed,
    )

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    assert resp.status_code == 200, resp.text
    get_resp = await client.get(f"/v1/sessions/{conv_id}")
    assert get_resp.status_code == 404


# ── R-CLEAN: the effective worktree, never an entry ─────────────────────


async def test_delete_removes_the_recorded_worktree_not_the_entry(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Scenario 8: an entry-started session's delete removes its worktree only."""
    worktree = "/Users/alice/entry/nested/wt"
    captured = await _register_fake_host(app, db_uri, worktree_path=worktree)
    conv_id = _make_worktree_conversation(
        db_uri,
        workspace="/Users/alice/entry",
        worktree_root=worktree,
        worktree=worktree,
        git_branch="feature/login",
    )

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    assert resp.status_code == 200, resp.text

    assert len(captured) == 1, "the session's worktree must be removed"
    assert captured[0].worktree_path == worktree, (
        "the cleanup must remove the effective worktree, never the entry"
    )
    assert captured[0].branch == "feature/login"
    assert captured[0].delete_branch is True


async def test_delete_removes_a_legacy_rows_launch_directory(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Scenario 9: with no recorded worktree a legacy row keeps cleaning up its workspace."""
    captured = await _register_fake_host(app, db_uri)
    conv_id = _make_worktree_conversation(db_uri, workspace=_WORKTREE_PATH, worktree=None)

    resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    assert resp.status_code == 200, resp.text
    assert len(captured) == 1
    assert captured[0].worktree_path == _WORKTREE_PATH


async def test_delete_of_an_entry_session_is_not_blocked_by_a_sibling_at_the_entry(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Scenario 18: sessions at the entry with distinct worktrees are not sharers."""
    captured = await _register_fake_host(
        app, db_uri, worktree_path="/Users/alice/entry/wt-a", branch="feature/a"
    )
    app.state.project_host_binding_store = _Entries({(_HOST_ID, "/Users/alice/entry")})
    first = _make_worktree_conversation(
        db_uri,
        workspace="/Users/alice/entry",
        worktree_root="/Users/alice/entry/wt-a",
        worktree="/Users/alice/entry/wt-a",
        git_branch="feature/a",
    )
    _make_worktree_conversation(
        db_uri,
        workspace="/Users/alice/entry",
        worktree_root="/Users/alice/entry/wt-b",
        worktree="/Users/alice/entry/wt-b",
        git_branch="feature/b",
    )

    resp = await client.delete(f"/v1/sessions/{first}?delete_branch=true")
    assert resp.status_code == 200, resp.text

    assert len(captured) == 1, "a co-located entry session must not count as sharing this worktree"
    assert captured[0].worktree_path == "/Users/alice/entry/wt-a"


async def test_delete_never_removes_a_project_entry(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scenario 19: a session bound to some project's entry is never cleaned up."""
    captured = await _register_fake_host(app, db_uri)
    entry = "/Users/alice/entry-repo"
    # A No Project session bound (existing_worktree) to project A's entry: the
    # row's launch directory is the entry and carries a branch, exactly the
    # shape the cleanup gate used to remove.
    app.state.project_host_binding_store = _Entries({(_HOST_ID, entry)})
    conv_id = _make_worktree_conversation(
        db_uri, workspace=entry, worktree_root=entry, worktree=None
    )

    with caplog.at_level("WARNING", logger="omnigent.server.routes.sessions"):
        resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")

    assert resp.status_code == 200, resp.text
    assert captured == [], "an entry on that host must never be removed"
    assert any("project entry" in record.message for record in caplog.records), (
        "the skipped removal must be logged"
    )


async def test_delete_keeps_a_worktree_holding_a_nested_project_entry(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Scenario 20: an entry nested inside a worktree keeps the whole tree.

    A project may register a subdirectory of a worktree as its entry; removing
    the parent would take that entry with it, so the guard must match an entry
    anywhere under the directory, not only exactly at it.
    """
    captured = await _register_fake_host(app, db_uri)
    worktree = "/Users/alice/repo-worktrees/topic"
    app.state.project_host_binding_store = _Entries({(_HOST_ID, f"{worktree}/subproject")})
    conv_id = _make_worktree_conversation(
        db_uri, workspace=worktree, worktree_root=worktree, worktree=worktree
    )

    with caplog.at_level("WARNING", logger="omnigent.server.routes.sessions"):
        resp = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")

    assert resp.status_code == 200, resp.text
    assert captured == [], (
        "a session worktree holding a project entry must not be removed with its parent"
    )
    assert any("project entry" in record.message for record in caplog.records), (
        "the skipped removal must be logged"
    )


async def test_delete_checks_recorded_root_for_sibling_project_entry(
    app: FastAPI, client: httpx.AsyncClient, db_uri: str
) -> None:
    captured = await _register_fake_host(app, db_uri)
    root = "/opt/work/sample-app/topic"
    app.state.project_host_binding_store = _Entries({(_HOST_ID, f"{root}/packages/other")})
    conv_id = _make_worktree_conversation(
        db_uri,
        workspace=f"{root}/packages/app",
        worktree=f"{root}/packages/app",
        worktree_root=root,
    )
    response = await client.delete(f"/v1/sessions/{conv_id}?delete_branch=true")
    assert response.status_code == 200, response.text
    assert captured == []


@pytest.mark.parametrize(
    "change", ["rename", "delete", "replace-with-file", "symlink", "symlink-to-repo"]
)
async def test_delete_worktree_after_workspace_disappears(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    """Resolve and remove the real Git worktree after its session directory disappears."""
    await _register_fake_host(app, db_uri)
    repo = (tmp_path / "repo").resolve()
    source = repo / "packages" / "app"
    source.mkdir(parents=True)
    (source / "README.md").write_text("tracked project")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "init")
    created = create_worktree(repo_path=str(source), branch_name="feature/login")
    session_id = _make_worktree_conversation(db_uri, created.workspace, created.worktree_path)
    root = Path(created.worktree_path)
    packages = root / "packages"
    if change == "rename":
        packages.rename(root / "renamed-packages")
    else:
        shutil.rmtree(packages)
        if change == "replace-with-file":
            packages.write_text("now a file")
        elif change.startswith("symlink"):
            target = (tmp_path / "external").resolve()
            target.mkdir()
            (target / "keep.txt").write_text("unrelated data")
            if change == "symlink-to-repo":
                _git(target, "init", "-q", "-b", "main")
            packages.symlink_to(target, target_is_directory=True)

    removed: list[str] = []

    async def list_on_host(
        *, repo_path: str, for_cleanup: bool = False, **_kwargs: object
    ) -> list[dict[str, object]]:
        return [
            asdict(tree) for tree in list_worktrees(repo_path=repo_path, for_cleanup=for_cleanup)
        ]

    async def remove_on_host(
        *, worktree_path: str, branch: str, delete_branch: bool, **_kwargs: object
    ) -> None:
        removed.append(worktree_path)
        remove_worktree(worktree_path=worktree_path, branch=branch, delete_branch=delete_branch)

    monkeypatch.setattr(
        "omnigent.server.routes._host_worktree.list_worktrees_on_host", list_on_host
    )
    monkeypatch.setattr(
        "omnigent.server.routes._host_worktree.remove_worktree_on_host", remove_on_host
    )
    response = await client.delete(f"/v1/sessions/{session_id}?delete_branch=true")
    assert response.status_code == 200, response.text
    assert removed == [created.worktree_path]
    assert not root.exists()
    assert not _branch_exists(repo, created.branch)
    if change.startswith("symlink"):
        assert (tmp_path / "external" / "keep.txt").read_text() == "unrelated data"


@pytest.mark.parametrize(
    ("root", "other", "in_use"),
    [
        ("/repo-worktrees/feature", "/repo-worktrees/feature/web", True),
        ("/repo-worktrees/feature", "/repo-worktrees/Feature/web", False),
        ("/repo-worktrees/feature", "/repo-worktrees/feature-other/web", False),
        ("/repo-worktrees/feature_%", "/repo-worktrees/feature_%/web", True),
        ("/repo-worktrees/feature_%", "/repo-worktrees/feature-abc/web", False),
        ("C:/repo-worktrees/feature", r"c:\repo-worktrees\feature\web", True),
        ("C:/Wörk/Ärger", r"C:\Wörk\Ärger\web", True),
        ("C:/Wörk/Ärger", r"c:\wörk\ärger\web", True),
        ("c:/wörk/ärger", r"C:\WÖRK\ÄRGER\web", True),
        ("C:/Wörk/Ärger", r"C:\Wörk\Ärger-other\web", False),
        ("//SÉRVER/share/Ärger", r"\\sérver\share\ärger\web", True),
    ],
)
async def test_worktree_sharing_includes_descendants(
    db_uri: str, root: str, other: str, in_use: bool
) -> None:
    """Descendant matching respects path boundaries, SQL wildcards, and Windows separators."""
    _upsert_host_row(db_uri)
    mine = _make_worktree_conversation(db_uri, root)
    sibling = _make_worktree_conversation(db_uri, other)
    store = SqlAlchemyConversationStore(db_uri)
    assert (
        store.has_other_live_session_in_workspace(
            host_id=_HOST_ID,
            workspace=root,
            exclude_conversation_id=mine,
            include_subdirectories=True,
        )
        is in_use
    )
    store.update_conversation(sibling, archived=True)
    assert not store.has_other_live_session_in_workspace(
        host_id=_HOST_ID,
        workspace=root,
        exclude_conversation_id=mine,
        include_subdirectories=True,
    )


async def test_windows_sharing_limit_preserves_worktree(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full Windows candidate batch cannot prove that later sessions do not share it."""
    from omnigent.stores.conversation_store import sqlalchemy_store

    monkeypatch.setattr(sqlalchemy_store, "_WORKSPACE_SHARER_SCAN_LIMIT", 2)
    mine = _make_worktree_conversation(db_uri, "C:/Wörk/Ärger")
    for index in range(3):
        _make_worktree_conversation(db_uri, f"C:/unrelated/{index}")
    store = SqlAlchemyConversationStore(db_uri)
    assert store.has_other_live_session_in_workspace(
        host_id=_HOST_ID,
        workspace="C:/Wörk/Ärger",
        exclude_conversation_id=mine,
        include_subdirectories=True,
    )


@pytest.mark.parametrize(
    ("root", "workspace"),
    [
        (r"\\server\share\repo-worktrees\feature", r"\\server\share\repo-worktrees\feature\web"),
        ("//server/share/repo-worktrees/feature", r"\\SERVER\share\repo-worktrees\feature\web"),
        (r"\\SERVER\share\repo-worktrees\feature", "//server/share/repo-worktrees/feature/web"),
    ],
)
async def test_delete_unc_worktree_preserves_sharers_and_cleans_up_last_session(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    root: str,
    workspace: str,
) -> None:
    """UNC subdirectories share a cleanup root even with mixed separators and casing."""
    captured = await _register_fake_host(app, db_uri, worktree_path=root)
    first = _make_worktree_conversation(db_uri, root, root)
    second = _make_worktree_conversation(db_uri, workspace, root)
    response = await client.delete(f"/v1/sessions/{first}?delete_branch=true")
    assert response.status_code == 200, response.text
    assert captured == []
    response = await client.delete(f"/v1/sessions/{second}?delete_branch=true")
    assert response.status_code == 200, response.text
    assert len(captured) == 1
    assert captured[0].worktree_path == root
    assert captured[0].delete_branch is True


@pytest.mark.parametrize("record_root", [True, False])
async def test_delete_does_not_remove_enclosing_worktree(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_root: bool,
) -> None:
    """A missing nested checkout must never transfer cleanup to its enclosing repo."""
    from omnigent.server.routes._host_worktree import WorktreeProxyError

    await _register_fake_host(app, db_uri)
    repo = (tmp_path / "outer").resolve()
    repo.mkdir()
    (repo / "keep.txt").write_text("outer data")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "outer")
    outer = create_worktree(repo_path=str(repo), branch_name="feature/login")
    inner = Path(outer.worktree_path) / "inner"
    source = inner / "web"
    source.mkdir(parents=True)
    (source / "README").write_text("inner")
    _git(inner, "init", "-q", "-b", "main")
    _git(inner, "add", ".")
    _git(inner, "commit", "-qm", "inner")
    created = create_worktree(repo_path=str(source), branch_name="feature/login")
    session_id = _make_worktree_conversation(
        db_uri, created.workspace, created.worktree_path if record_root else None
    )
    shutil.rmtree(created.worktree_path)
    removed: list[str] = []

    async def list_on_host(
        *, repo_path: str, for_cleanup: bool = False, **_kwargs: object
    ) -> list[dict[str, object]]:
        try:
            return [
                asdict(tree)
                for tree in list_worktrees(repo_path=repo_path, for_cleanup=for_cleanup)
            ]
        except WorktreeError as exc:
            raise WorktreeProxyError(str(exc)) from exc

    async def remove_on_host(*, worktree_path: str, **_kwargs: object) -> None:
        removed.append(worktree_path)

    monkeypatch.setattr(
        "omnigent.server.routes._host_worktree.list_worktrees_on_host", list_on_host
    )
    monkeypatch.setattr(
        "omnigent.server.routes._host_worktree.remove_worktree_on_host", remove_on_host
    )
    response = await client.delete(f"/v1/sessions/{session_id}?delete_branch=true")
    assert response.status_code == 200, response.text
    assert removed == []
    assert (Path(outer.worktree_path) / "keep.txt").exists()


async def test_legacy_root_session_still_cleans_up(
    app: FastAPI, client: httpx.AsyncClient, db_uri: str
) -> None:
    """Older sessions lack a fingerprint but may still remove their exact root."""
    captured = await _register_fake_host(app, db_uri)
    session_id = _make_worktree_conversation(db_uri, worktree_root=None)
    response = await client.delete(f"/v1/sessions/{session_id}?delete_branch=true")
    assert response.status_code == 200, response.text
    assert [frame.worktree_path for frame in captured] == [_WORKTREE_PATH]


async def test_cleanup_uses_one_sharing_lookup_for_recorded_root(
    app: FastAPI, client: httpx.AsyncClient, db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Root recovery and pre-dispatch both check for sessions sharing the worktree."""
    captured = await _register_fake_host(app, db_uri)
    session_id = _make_worktree_conversation(db_uri, f"{_WORKTREE_PATH}/web")
    calls: list[tuple[str, bool]] = []
    original = SqlAlchemyConversationStore.has_other_live_session_in_workspace

    def check(
        self: SqlAlchemyConversationStore,
        *,
        host_id: str,
        workspace: str,
        exclude_conversation_id: str,
        include_subdirectories: bool = False,
    ) -> bool:
        calls.append((workspace, include_subdirectories))
        return original(
            self,
            host_id=host_id,
            workspace=workspace,
            exclude_conversation_id=exclude_conversation_id,
            include_subdirectories=include_subdirectories,
        )

    monkeypatch.setattr(SqlAlchemyConversationStore, "has_other_live_session_in_workspace", check)
    response = await client.delete(f"/v1/sessions/{session_id}?delete_branch=true")
    assert response.status_code == 200, response.text
    assert calls == [(_WORKTREE_PATH, True), (_WORKTREE_PATH, True)]
    assert len(captured) == 1
