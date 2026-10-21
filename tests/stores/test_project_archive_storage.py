"""Owner scope and durable provenance for project archive membership."""

from __future__ import annotations

import pytest

from omnigent.errors import ProjectArchiveBusyError
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.conversation_store import (
    PROJECT_ARCHIVE_LABEL_KEY,
    project_archive_batch_label_key,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore


def test_no_auth_members_include_ungranted_sessions_but_exclude_foreign_owner(
    conversation_store: SqlAlchemyConversationStore, db_uri: str
) -> None:
    project = SqlAlchemyProjectStore(db_uri).create("a" * 32, "Work", None)
    local = conversation_store.create_conversation(project_id=project.id)
    child = conversation_store.create_conversation(parent_conversation_id=local.id)
    foreign = conversation_store.create_conversation(project_id=project.id)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("foreign-user")
    permissions.grant("foreign-user", foreign.id, LEVEL_OWNER)

    members = conversation_store.list_project_archive_sessions(project.id, project.name, None)
    assert {member.id for member in members} == {local.id, child.id}


def test_per_batch_provenance_survives_later_archive(
    conversation_store: SqlAlchemyConversationStore, db_uri: str
) -> None:
    project = SqlAlchemyProjectStore(db_uri).create("b" * 32, "Work", None)
    session = conversation_store.create_conversation(project_id=project.id)
    first_batch = "1" * 32
    second_batch = "2" * 32

    first = conversation_store.update_conversation_with_changes(
        session.id,
        archived=True,
        expect_archived=False,
        expect_revision=0,
        project_archive_batch=first_batch,
    )
    assert first is not None and first.archive_outcome == "applied"
    assert first.conversation.labels[project_archive_batch_label_key(first_batch)] == "1"
    conversation_store.update_conversation(session.id, archived=False)
    second = conversation_store.update_conversation_with_changes(
        session.id,
        archived=True,
        expect_archived=False,
        expect_revision=2,
        project_archive_batch=second_batch,
    )
    assert second is not None and second.archive_outcome == "applied"
    assert second.conversation.labels[PROJECT_ARCHIVE_LABEL_KEY] == f"{second_batch}:3"
    assert second.conversation.labels[project_archive_batch_label_key(first_batch)] == "1"
    assert second.conversation.labels[project_archive_batch_label_key(second_batch)] == "3"
    assert [
        member.id
        for member in conversation_store.list_project_archive_sessions(
            project.id, project.name, None, batch=first_batch
        )
    ] == [session.id]
    assert [
        member.id
        for member in conversation_store.list_project_archive_sessions(
            project.id, project.name, None, batch=second_batch
        )
    ] == [session.id]

    stale = conversation_store.update_conversation_with_changes(
        session.id, archived=False, expect_archived=True, expect_revision=1
    )
    assert stale is not None and stale.archive_outcome == "conflict"
    assert conversation_store.get_conversation(session.id).archived


def test_stale_archive_operation_cannot_write_members_or_clear_labels(
    conversation_store: SqlAlchemyConversationStore,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.stores.project_store import sqlalchemy_store as project_store_module

    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create("c" * 32, "Work", None)
    archived_session = conversation_store.create_conversation(project_id=project.id)
    live_session = conversation_store.create_conversation(project_id=project.id)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    paused_task = tasks.create(
        "d" * 32,
        "Paused by archive",
        "p",
        "FREQ=DAILY",
        None,
        "e" * 32,
        "UTC",
        project_id=project.id,
    )
    live_task = tasks.create(
        "f" * 32,
        "Still active",
        "p",
        "FREQ=DAILY",
        None,
        "e" * 32,
        "UTC",
        project_id=project.id,
    )
    monkeypatch.setattr(project_store_module, "now_epoch", lambda: 1000)
    old_op = projects.begin_archive_op(project.id, user_id=None, kind="archive")
    archived = conversation_store.update_conversation_with_changes(
        archived_session.id,
        archived=True,
        expect_archived=False,
        expect_revision=0,
        project_archive_batch=old_op.batch,
        archive_project_id=project.id,
        archive_op_id=old_op.id,
    )
    assert archived is not None and archived.archive_outcome == "applied"
    assert (
        tasks.pause_for_project_archive(paused_task.id, project.id, old_op.batch, op_id=old_op.id)
        is not None
    )

    monkeypatch.setattr(project_store_module, "now_epoch", lambda: 1400)
    new_op = projects.begin_archive_op(project.id, user_id=None, kind="unarchive")
    assert new_op.id != old_op.id
    refused = conversation_store.update_conversation_with_changes(
        live_session.id,
        archived=True,
        expect_archived=False,
        expect_revision=0,
        project_archive_batch=old_op.batch,
        archive_project_id=project.id,
        archive_op_id=old_op.id,
    )
    assert refused is not None and refused.archive_outcome == "conflict"
    assert conversation_store.get_conversation(live_session.id).archived is False
    assert not conversation_store.clear_project_archive_labels(
        archived_session.id, project.id, old_op.id, old_op.batch
    )
    assert (
        project_archive_batch_label_key(old_op.batch)
        in conversation_store.get_conversation(archived_session.id).labels
    )
    with pytest.raises(ProjectArchiveBusyError):
        tasks.pause_for_project_archive(live_task.id, project.id, old_op.batch, op_id=old_op.id)
    with pytest.raises(ProjectArchiveBusyError):
        tasks.restore_from_project_archive(
            paused_task.id, project.id, old_op.batch, op_id=old_op.id
        )
    with pytest.raises(ProjectArchiveBusyError):
        tasks.clear_project_archive_marker(
            paused_task.id, old_op.batch, project_id=project.id, op_id=old_op.id
        )
    observed: list[str | None] = []
    assert not tasks.reconcile_project_archive_task(
        paused_task.id,
        project.id,
        old_op.id,
        lambda current: observed.append(current.state if current else None),
    )
    assert observed == []
    assert tasks.reconcile_project_archive_task(
        paused_task.id,
        project.id,
        new_op.id,
        lambda current: observed.append(current.state if current else None),
    )
    assert observed == ["paused"]
    tasks.restore_from_project_archive(paused_task.id, project.id, old_op.batch, op_id=new_op.id)
    assert tasks.reconcile_project_archive_task(
        paused_task.id,
        project.id,
        new_op.id,
        lambda current: observed.append(current.state if current else None),
    )
    assert observed == ["paused", "active"]
    assert tasks.get(live_task.id).state == "active"
    assert tasks.get(paused_task.id).paused_by_archive_batch == old_op.batch
