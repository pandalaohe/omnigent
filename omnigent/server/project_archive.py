"""Resumable project archive using the ordinary session archive lifecycle."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import Request

from omnigent.entities import Conversation, Project, ScheduledTask
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.server.routes.sessions.routes_core import apply_session_archive_transition
from omnigent.server.schemas import UpdateSessionRequest
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.stores.scheduled_task_store import ScheduledTaskStore

_BATCH_LABEL = "omnigent.project_archive"


def _tree_order(sessions: list[Conversation], *, leaves_first: bool) -> list[Conversation]:
    by_id = {session.id: session for session in sessions}

    def depth(session: Conversation) -> int:
        seen = {session.id}
        parent = session.parent_conversation_id
        while parent in by_id and parent not in seen:
            seen.add(parent)
            parent = by_id[parent].parent_conversation_id
        return len(seen)

    return sorted(sessions, key=depth, reverse=leaves_first)


class ProjectArchiveService:
    def __init__(
        self,
        projects: ProjectStore,
        conversations: ConversationStore,
        permissions: PermissionStore | None,
        tasks: ScheduledTaskStore | None,
        runner_router: RunnerRouter | None,
    ) -> None:
        self.projects = projects
        self.conversations = conversations
        self.permissions = permissions
        self.tasks = tasks
        self.runner_router = runner_router

    async def plan_project_archive(
        self, project: Project
    ) -> tuple[list[Conversation], list[ScheduledTask]]:
        sessions = await asyncio.to_thread(
            self.conversations.list_project_archive_sessions,
            project.id,
            project.name,
            project.user_id,
        )
        tasks = (
            await asyncio.to_thread(
                self.tasks.list_for_project_archive,
                project.id,
                owner_user_id=project.user_id,
            )
            if self.tasks is not None
            else []
        )
        return sessions, tasks

    async def summary(self, project: Project) -> dict[str, int]:
        batch = project.archive_state.get("batch")
        if not batch:
            return {"sessions": 0, "tasks": 0}
        sessions = await asyncio.to_thread(
            self.conversations.list_project_archive_sessions,
            project.id,
            project.name,
            project.user_id,
            batch=batch,
        )
        tasks = (
            await asyncio.to_thread(
                self.tasks.list_for_project_archive,
                project.id,
                owner_user_id=project.user_id,
                batch=batch,
            )
            if self.tasks is not None
            else []
        )
        return {"sessions": len(sessions), "tasks": len(tasks)}

    async def preview(self, request: Request, project: Project) -> dict[str, Any]:
        sessions, tasks = await self.plan_project_archive(project)
        scheduler = getattr(request.app.state, "scheduled_task_scheduler", None)
        host_store = getattr(request.app.state, "host_store", None)
        hosts: dict[str, bool] = {}
        for session in sessions:
            if session.host_id and session.host_id not in hosts and host_store is not None:
                host = await asyncio.to_thread(host_store.get_host, session.host_id)
                policy = host.cli_retention_policy if host is not None else None
                hosts[session.host_id] = policy is not None and not policy.close_on_archive
        statuses = (
            await asyncio.to_thread(
                self.tasks.list_latest_run_status_for_tasks, [task.id for task in tasks]
            )
            if self.tasks is not None
            else {}
        )
        hub = getattr(request.app.state, "system_status", None)
        health_project = (
            hub.get_settings().get("health_check", {}).get("project_id") if hub else None
        )
        return {
            "sessions": [
                {
                    "id": session.id,
                    "title": session.title or session.id,
                    "status": session.live_status or "idle",
                    "project_id": session.project_id,
                    "belongs_to_project": session.project_id == project.id
                    or (
                        session.project_id is None
                        and session.labels.get("omni_project") == project.name
                    ),
                    "parent_session_id": session.parent_conversation_id,
                    "parent_id": session.parent_conversation_id,
                    "via_parent_outside_project": session.project_id not in (None, project.id),
                    "host_keeps_cli": hosts.get(session.host_id or "", False),
                }
                for session in sessions
            ],
            "scheduled_tasks": [
                {
                    "id": task.id,
                    "name": task.name,
                    "state": task.state,
                    "next_run_at": scheduler.next_run_at(task.id) if scheduler else None,
                    "last_run_status": statuses.get(task.id),
                }
                for task in tasks
            ],
            "health_check_uses_project": health_project == project.id,
        }

    async def transition(
        self, request: Request, project: Project, kind: Literal["archive", "unarchive"]
    ) -> dict[str, Any]:
        op = await asyncio.to_thread(
            self.projects.begin_archive_op, project.id, user_id=project.user_id, kind=kind
        )
        scheduler = getattr(request.app.state, "scheduled_task_scheduler", None)
        done_sessions: list[str] = []
        done_tasks: list[dict[str, Any]] = []
        skipped_sessions: list[dict[str, str]] = []
        skipped_tasks: list[dict[str, str]] = []
        failed: dict[str, dict[str, str]] = {}
        lost_claim = False

        async def fence() -> bool:
            return await asyncio.to_thread(
                self.projects.fence_archive_op, project.id, op.id, user_id=project.user_id
            )

        def sync_current_task(task_id: str) -> bool:
            assert self.tasks is not None

            def reconcile(current: ScheduledTask | None) -> None:
                if scheduler is None:
                    if current is not None and current.state == "active":
                        raise RuntimeError("Scheduler unavailable; retry to restore scheduling")
                    return
                if current is not None and current.state == "active":
                    scheduler.update(current)
                else:
                    scheduler.remove(task_id)

            # Timers belong to this event loop. Keep the operation and task
            # locked through their synchronous reconciliation so no takeover
            # can apply newer state between the read and the scheduler effect.
            return self.tasks.reconcile_project_archive_task(task_id, project.id, op.id, reconcile)

        for _round in range(3 if kind == "archive" else 1):
            if kind == "archive":
                sessions, tasks = await self.plan_project_archive(project)
                # Reconcile removals after an interruption following the pause commit.
                if self.tasks is not None:
                    marked = await asyncio.to_thread(
                        self.tasks.list_for_project_archive,
                        project.id,
                        owner_user_id=project.user_id,
                        batch=op.batch,
                    )
                    tasks = list({task.id: task for task in [*tasks, *marked]}.values())
            else:
                sessions = await asyncio.to_thread(
                    self.conversations.list_project_archive_sessions,
                    project.id,
                    project.name,
                    project.user_id,
                    batch=op.batch,
                )
                tasks = (
                    await asyncio.to_thread(
                        self.tasks.list_for_project_archive,
                        project.id,
                        owner_user_id=project.user_id,
                        batch=op.batch,
                    )
                    if self.tasks is not None
                    else []
                )
            for session in _tree_order(sessions, leaves_first=kind == "archive"):
                if not await fence():
                    lost_claim = True
                    break
                try:
                    revision = session.archive_revision
                    batch_label = f"{_BATCH_LABEL}.{op.batch}"
                    remove_batch_labels = [batch_label]
                    canonical_tag = session.labels.get(_BATCH_LABEL, "")
                    if canonical_tag.startswith(f"{op.batch}:"):
                        remove_batch_labels.append(_BATCH_LABEL)
                    if kind == "unarchive":
                        tagged_revision = session.labels.get(batch_label)
                        if tagged_revision is None:
                            _, _, tagged_revision = canonical_tag.rpartition(":")
                        reason = ""
                        if not session.archived or str(revision) != tagged_revision:
                            reason = "Session was handled separately"
                        elif session.project_id:
                            current_project = await asyncio.to_thread(
                                self.projects.get, session.project_id, user_id=project.user_id
                            )
                            if (
                                current_project is not None
                                and current_project.archived_at is not None
                            ):
                                reason = "Current project is archived"
                        if reason:
                            cleared = await asyncio.to_thread(
                                self.conversations.clear_project_archive_labels,
                                session.id,
                                project.id,
                                op.id,
                                op.batch,
                            )
                            if not cleared:
                                lost_claim = True
                                break
                            skipped_sessions.append({"id": session.id, "reason": reason})
                            continue
                    result = await apply_session_archive_transition(
                        request,
                        session,
                        UpdateSessionRequest(
                            archived=kind == "archive",
                            stop_when_idle=True,
                            keep_worktree=kind == "archive",
                        ),
                        self.conversations,
                        self.permissions,
                        self.runner_router,
                        expect_archived=kind != "archive",
                        expect_revision=revision,
                        project_archive_batch=op.batch if kind == "archive" else None,
                        archive_project_id=project.id,
                        archive_op_id=op.id,
                        remove_labels=remove_batch_labels if kind == "unarchive" else None,
                    )
                    if result.archive_outcome == "applied":
                        done_sessions.append(session.id)
                        failed.pop(session.id, None)
                    else:
                        skipped_sessions.append(
                            {"id": session.id, "reason": result.archive_outcome or "conflict"}
                        )
                except Exception as exc:  # noqa: BLE001 - retain per-member failures for retry
                    failed[session.id] = {
                        "id": session.id,
                        "kind": "session",
                        "error": exc.message
                        if isinstance(exc, OmnigentError)
                        else "Session transition failed; retry the operation",
                    }
            if lost_claim:
                break
            for task in tasks:
                if not await fence():
                    lost_claim = True
                    break
                assert self.tasks is not None
                try:
                    if kind == "archive":
                        changed = await asyncio.to_thread(
                            self.tasks.pause_for_project_archive,
                            task.id,
                            project.id,
                            op.batch,
                            op_id=op.id,
                        )
                    else:
                        changed = await asyncio.to_thread(
                            self.tasks.restore_from_project_archive,
                            task.id,
                            project.id,
                            op.batch,
                            op_id=op.id,
                        )
                    if not await fence():
                        lost_claim = True
                        break
                    if changed is not None:
                        if not sync_current_task(task.id):
                            lost_claim = True
                            break
                    if kind == "unarchive":
                        await asyncio.to_thread(
                            self.tasks.clear_project_archive_marker,
                            task.id,
                            op.batch,
                            project_id=project.id,
                            op_id=op.id,
                        )
                    if changed is not None:
                        done_tasks.append(
                            {
                                "id": task.id,
                                "name": task.name,
                                "next_run_at": scheduler.next_run_at(task.id)
                                if scheduler
                                else None,
                            }
                        )
                        failed.pop(task.id, None)
                    else:
                        skipped_tasks.append(
                            {"id": task.id, "reason": "Task was handled separately"}
                        )
                except Exception as exc:  # noqa: BLE001 - retain per-member failures for retry
                    failed[task.id] = {
                        "id": task.id,
                        "kind": "task",
                        "error": exc.message
                        if isinstance(exc, OmnigentError)
                        else "Task scheduling failed; retry the operation",
                    }
            if lost_claim or kind == "unarchive":
                break
            remaining_sessions, remaining_tasks = await self.plan_project_archive(project)
            if not remaining_sessions and not remaining_tasks:
                break

        if kind == "archive":
            remaining_sessions, remaining_tasks = await self.plan_project_archive(project)
            complete = (
                not remaining_sessions and not remaining_tasks and not failed and not lost_claim
            )
        else:
            fresh = await asyncio.to_thread(self.projects.get, project.id, user_id=project.user_id)
            assert fresh is not None
            summary = await self.summary(fresh)
            complete = not any(summary.values()) and not failed and not lost_claim
        if not lost_claim:
            finished = await asyncio.to_thread(
                self.projects.finish_archive_op,
                project.id,
                op.id,
                user_id=project.user_id,
                clear_batch=kind == "unarchive" and complete,
            )
            if not finished:
                complete = False
        fresh = await asyncio.to_thread(self.projects.get, project.id, user_id=project.user_id)
        if fresh is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return {
            "status": "complete" if complete else "incomplete",
            "project": fresh,
            "batch": op.batch,
            "sessions": {"done": list(dict.fromkeys(done_sessions)), "skipped": skipped_sessions},
            "tasks": {
                "done": list({task["id"]: task for task in done_tasks}.values()),
                "skipped": skipped_tasks,
            },
            "failed": list(failed.values()),
        }
