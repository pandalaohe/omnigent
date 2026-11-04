"""Retention uses the real store and the application's existing delete body."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert, update

from omnigent.db.db_models import (
    SqlConversation,
    SqlDetachedCard,
    current_workspace_id,
    workspace_scope,
)
from omnigent.db.session_startup import SqlSessionStartup
from omnigent.db.utils import get_or_create_engine
from omnigent.server.session_retention import SessionRetention
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store import (
    ARCHIVE_REMOVED_WORKTREE_LABEL_KEY,
    ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

_DAY = 86400
_NOW = int(time.time())
_OLD = _NOW - 91 * _DAY


@pytest.fixture
def env(
    app: FastAPI, db_uri: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> SimpleNamespace:
    config = {"session_retention_days": 90}
    monkeypatch.setattr("omnigent.server.server_config.load_server_config", lambda: config)
    monkeypatch.setattr("omnigent.server.session_retention.DELETE_DELAY_S", 0)
    store = SqlAlchemyConversationStore(db_uri)
    job = SessionRetention(
        store,
        app.state.delete_session_authorized,
        session_tree_idle=app.state.session_tree_idle,
        data_dir=tmp_path,
    )
    return SimpleNamespace(
        app=app,
        store=store,
        files=SqlAlchemyFileStore(db_uri),
        artifacts=LocalArtifactStore(str(tmp_path / "artifacts")),
        job=job,
        config=config,
        engine=get_or_create_engine(db_uri),
        tmp_path=tmp_path,
    )


def _age(env: SimpleNamespace, session_id: str, **values: object) -> None:
    with env.engine.begin() as connection:
        connection.execute(
            update(SqlConversation)
            .where(
                SqlConversation.workspace_id == current_workspace_id(),
                SqlConversation.id == session_id,
            )
            .values(**values)
        )


def _tree(env: SimpleNamespace, *, archived_child: bool = True) -> list[str]:
    root = env.store.create_conversation(title="old root")
    child = env.store.create_conversation(parent_conversation_id=root.id)
    grandchild = env.store.create_conversation(parent_conversation_id=child.id)
    for conv in (root, child, grandchild):
        archived = conv.id == root.id or archived_child
        _age(
            env,
            conv.id,
            archived=archived,
            archived_at=_OLD if archived else None,
            updated_at=_OLD,
        )
    return [root.id, child.id, grandchild.id]


def _attachment(env: SimpleNamespace, session_id: str, blob_key: str | None = None) -> str:
    row = env.files.create(
        session_id=session_id,
        filename="proof.txt",
        bytes=7,
        content_type="text/plain",
        blob_key=blob_key,
    )
    env.artifacts.put(row.blob_key or row.id, b"payload")
    return row.blob_key or row.id


@pytest.mark.parametrize("archived_child", [True, False])
async def test_deletes_children_first_and_keeps_shared_blob(
    env: SimpleNamespace, client: httpx.AsyncClient, archived_child: bool
) -> None:
    root, child, grandchild = _tree(env, archived_child=archived_child)
    outside = env.store.create_conversation()
    shared = _attachment(env, child)
    _attachment(env, outside.id, shared)
    unique = _attachment(env, grandchild)
    delete = AsyncMock(wraps=env.app.state.delete_session_authorized)
    env.job._delete_session = delete

    summary = await env.job.sweep_once(now=_NOW)

    assert summary.sessions_deleted == 3 and summary.errors == 0
    assert [call.args[0] for call in delete.call_args_list] == [grandchild, child, root]
    assert not env.artifacts.exists(unique)
    assert env.artifacts.exists(shared)
    assert env.store.get_conversation(outside.id) is not None
    assert env.store.get_conversations([root, child, grandchild]) == {}


@pytest.mark.parametrize(
    "reason", ["young_archive", "active", "unarchived_root", "locked", "hold"]
)
async def test_whole_tree_excluded(env: SimpleNamespace, reason: str) -> None:
    root, child, grandchild = _tree(env, archived_child=False)
    blob = _attachment(env, child)
    if reason == "young_archive":
        _age(env, child, archived=True, archived_at=_NOW - 10 * _DAY)
    elif reason == "active":
        _age(env, child, updated_at=_NOW - 10 * _DAY)
    elif reason == "unarchived_root":
        _age(env, root, archived=False, archived_at=None)
    elif reason == "locked":
        _age(env, child, archive_locked=True)
    else:
        env.store.set_labels(child, {ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY: "held"})
        _age(env, child, updated_at=_OLD)

    summary = await env.job.sweep_once(now=_NOW)

    assert summary.trees_seen == summary.sessions_deleted == summary.errors == 0
    assert len(env.store.get_conversations([root, child, grandchild])) == 3
    assert env.artifacts.exists(blob)


@pytest.mark.parametrize("status", ["running", "waiting", "failed", "background"])
async def test_not_idle_keeps_tree(
    env: SimpleNamespace, status: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.server.routes.sessions.routes_events import _session_background_task_count_cache

    root, child, _ = _tree(env, archived_child=False)
    if status == "background":
        monkeypatch.setitem(_session_background_task_count_cache, child, 1)
    else:
        env.store.set_session_live_status(child, status)
    _age(env, child, updated_at=_OLD)

    summary = await env.job.sweep_once(now=_NOW)

    assert summary.skipped == 1 and summary.sessions_deleted == 0
    assert env.store.get_conversation(root) is not None


@pytest.mark.parametrize("change", ["unarchive", "rearchive", "hold"])
async def test_rechecks_after_selection(env: SimpleNamespace, change: str) -> None:
    root, child, _ = _tree(env)
    original = env.store.list_session_tree_bottom_up

    def change_after_selection(root_id: str) -> list[str]:
        ids = original(root_id)
        if change == "unarchive":
            _age(env, root, archived=False, archived_at=None)
        elif change == "rearchive":
            _age(env, root, archived_at=_OLD + 1)
        else:
            env.store.set_labels(child, {ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY: "held"})
        return ids

    env.store.list_session_tree_bottom_up = change_after_selection
    summary = await env.job.sweep_once(now=_NOW)
    assert summary.sessions_deleted == 0 and summary.skipped == 1


async def test_hold_refusal_preserves_files_and_blobs(
    env: SimpleNamespace, client: httpx.AsyncClient
) -> None:
    root, child, _ = _tree(env)
    blob = _attachment(env, root)
    env.store.set_labels(child, {ARCHIVE_WORKTREE_ADMISSION_FENCE_LABEL_KEY: "held"})

    response = await client.delete(f"/v1/sessions/{root}")

    assert response.status_code == 409
    assert env.artifacts.exists(blob)
    assert env.files.list(session_id=root).data
    assert env.store.get_conversation(root) is not None
    env.store.set_labels(child, {ARCHIVE_REMOVED_WORKTREE_LABEL_KEY: "confirmed"})
    assert not env.store.has_unresolved_worktree_hold(root)


@pytest.mark.parametrize(
    "config,mode",
    [
        ({}, "off"),
        ({"session_retention_days": 0}, "off"),
        ({"session_retention_days": 90, "session_retention_dry_run": True}, "dry-run"),
        ({"session_retention_days": 90, "session_retention_dry_run": "false"}, "dry-run"),
        ({"session_retention_days": 90, "session_retention_dry_run": None}, "dry-run"),
    ],
)
async def test_off_and_dry_run(env: SimpleNamespace, config: dict, mode: str) -> None:
    root, _, _ = _tree(env)
    env.config.clear()
    env.config.update(config)
    summary = await env.job.sweep_once(now=_NOW)
    assert summary.mode == mode and summary.sessions_deleted == 0
    assert env.store.get_conversation(root) is not None
    saved = json.loads((env.tmp_path / "system-status" / "session-retention.json").read_text())
    assert saved["mode"] == mode


async def test_cap_and_next_sweep(
    env: SimpleNamespace, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, child, grandchild = _tree(env)
    monkeypatch.setattr("omnigent.server.session_retention.SESSION_CAP", 2)
    first = await env.job.sweep_once(now=_NOW)
    assert first.capped and first.sessions_deleted == 2
    assert env.store.get_conversations([child, grandchild]) == {}
    second = await env.job.sweep_once(now=_NOW)
    assert second.sessions_deleted == 1 and not second.capped
    assert env.store.get_conversation(root) is None


async def test_failure_and_resumed_child_leave_rest_for_later(
    env: SimpleNamespace, client: httpx.AsyncClient
) -> None:
    root, child, grandchild = _tree(env)
    other, _, _ = _tree(env)
    original = env.app.state.delete_session_authorized

    async def delete(session_id: str) -> object:
        if session_id == grandchild:
            raise RuntimeError("injected delete failure")
        return await original(session_id)

    env.job._delete_session = delete
    first = await env.job.sweep_once(now=_NOW)
    assert first.errors == 1 and first.sessions_deleted == 3
    assert env.store.get_conversation(other) is None

    async def resume(session_id: str) -> object:
        result = await original(session_id)
        if session_id == grandchild:
            _age(env, child, updated_at=_NOW)
        return result

    env.job._delete_session = resume
    second = await env.job.sweep_once(now=_NOW)
    assert second.sessions_deleted == 1 and second.skipped == 1
    assert env.store.get_conversation(root) is not None


async def test_paging_and_workspace_scope(
    env: SimpleNamespace, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("omnigent.server.session_retention._PAGE_SIZE", 2)
    for workspace in (0, 7):
        with workspace_scope(workspace):
            for _ in range(3):
                _tree(env)
    env.config["session_retention_dry_run"] = True
    dry = await env.job.sweep_once(now=_NOW)
    assert dry.trees_seen == 6 and dry.sessions_deleted == 0 and dry.errors == 0
    env.config["session_retention_dry_run"] = False
    done = await env.job.sweep_once(now=_NOW)
    assert done.sessions_deleted == 18 and done.errors == 0


async def test_job_lifecycle_is_cancellable(env: SimpleNamespace) -> None:
    await env.job.start()
    task = env.job._task
    await env.job.start()
    assert env.job._task is task
    await env.job.shutdown()
    assert task is not None and task.cancelled()


async def test_boot_after_delete_with_leftover_rows(
    env: SimpleNamespace, client: httpx.AsyncClient
) -> None:
    root, _, _ = _tree(env)
    with env.engine.begin() as connection:
        connection.execute(
            insert(SqlDetachedCard).values(
                elicitation_id="expired-question",
                session_id=root,
                kind="question",
                state="pending",
                mirror=False,
                params="{}",
                payload="{}",
                created_at=_OLD,
                expires_at=_NOW + _DAY,
            )
        )
        connection.execute(
            insert(SqlSessionStartup).values(
                id="a" * 32,
                owner_user_id="owner",
                host_id="b" * 32,
                session_id=root,
                operation_key="old-start",
                runtime_generation=1,
                family_id=root,
                sequence=1,
                state="released",
                created_at=_OLD,
                updated_at=_OLD,
            )
        )
    assert (await env.job.sweep_once(now=_NOW)).sessions_deleted == 3
    assert env.store.list_detached_cards()[0].session_id == root
    async with env.app.router.lifespan_context(env.app):
        assert (await client.get("/health")).status_code == 200
        assert env.app.state.session_retention._task is not None
    assert env.app.state.session_retention._task is None
