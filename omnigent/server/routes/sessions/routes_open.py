"""Open a plain top-level session on any host, project and agent.

``POST /v1/sessions/{sender_id}/open`` reuses the retired hand-off
route's resolver skeleton (project → host → root → agent) and the
session-create orchestration, minus every hand-off record, lease, report,
cancel, deadline and cap. The first message is an ordinary peer send from
the opener. Offline hosts can be waited for through an in-process
:class:`PendingOpens` registry that fires from the host-connect callback
or expires on a timer.
"""

from __future__ import annotations

import asyncio
import logging
import ntpath
import posixpath
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnigent.db.db_models import uuid_to_bytes
from omnigent.errors import OmnigentError
from omnigent.native.native_coding_agents import public_agent_name
from omnigent.server.auth import LEVEL_OWNER, RESERVED_USER_LOCAL
from omnigent.server.feature_flags import Feature, resolve_feature_flags
from omnigent.server.project_placement import (
    host_roots,
    load_bindings,
    load_eligible_host_ids,
    load_entries,
    root_on_host,
    same_canonical_path,
)
from omnigent.server.routes._auth_helpers import get_session_owner_id
from omnigent.server.routes._auth_helpers import get_user_id as _get_user_id
from omnigent.server.routes._host_worktree import (
    WorktreeHostUnavailableError,
    WorktreeProxyError,
    list_worktrees_on_host,
)
from omnigent.server.routes._session_create_validation import validate_session_agent
from omnigent.server.routes._sessions.helpers import _announce_session_added
from omnigent.server.routes._sessions.orchestration import _create_session_from_existing_agent
from omnigent.server.routes._workspace_validation import (
    _is_subpath_of,
    _is_windows_absolute_path,
)
from omnigent.server.routes.sessions.routes_peer import (
    PeerRoutes,
    _runner_authorized_for_sender,
    effective_owner_id,
)
from omnigent.server.schemas import ProjectSessionCreateRequest, SessionGitOptions
from omnigent.server.session_collab import COLLAB_DISABLED_MESSAGE, require_collab_enabled
from omnigent.server.session_open_rate import admit_open
from omnigent.server.user_preferences_store import read_collab_settings
from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY
from omnigent.util.session_lifecycle import is_session_closed, title_without_closed_marker

_logger = logging.getLogger(__name__)


def _normalized_path(value: str) -> str:
    """Resolve lexical ``.``/``..`` segments in an absolute path.

    Relative paths are returned unchanged; Windows absolute paths use
    ``ntpath`` so drive letters and separators normpath the host's way.

    :param value: A workspace or root path.
    :returns: The path with redundant segments removed.
    """
    if _is_windows_absolute_path(value):
        return ntpath.normpath(value)
    if value.startswith("/"):
        return posixpath.normpath(value)
    return value


class SessionOpenRequest(BaseModel):
    """Body of ``POST /sessions/{sender_id}/open``."""

    model_config = ConfigDict(extra="forbid")

    project: str = Field(min_length=1)
    host: str = Field(min_length=1)
    agent: str = Field(min_length=1)
    model: str | None = None
    reasoning_effort: str | None = None
    message: str | None = Field(default=None, max_length=16000)
    from_ref: str | None = None
    workspace: str | None = None
    branch: str | None = Field(default=None, max_length=200)
    wait_for_host: bool = False
    title: str | None = Field(default=None, max_length=200)

    @field_validator("from_ref", "workspace", "branch")
    @classmethod
    def _blank_ref_is_none(cls, value: str | None) -> str | None:
        """:returns: ``None`` for a blank or whitespace-only value."""
        if value is None:
            return None
        return value.strip() or None

    @field_validator("workspace")
    @classmethod
    def _normalize_workspace(cls, value: str | None) -> str | None:
        """:returns: the workspace with lexical ``.``/``..`` segments resolved."""
        if value is None:
            return None
        return _normalized_path(value)


def _problem(
    state: str,
    reason: str,
    message: str,
    candidates: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Render a refusal / failure result.

    :param state: ``"refused"``, ``"needs_input"`` or ``"failed"``.
    :param reason: Stable machine reason, e.g. ``"host_not_found"``.
    :param message: Human text the agent can act on.
    :param candidates: Optional id/name choices, capped at 20.
    :returns: The route's result dict.
    """
    result: dict[str, Any] = {"state": state, "reason": reason, "message": message}
    if candidates is not None:
        result["candidates"] = candidates[:20]
    return result


def _store_id(value: str) -> bool:
    """Whether *value* parses as a stored 32-hex identifier."""
    try:
        uuid_to_bytes(value)
    except ValueError:
        return False
    return True


@dataclass
class _PendingOpen:
    """One open waiting for an offline host to connect."""

    sid: str
    owner: str
    create_user_id: str | None
    sender_id: str
    host_id: str
    host_name: str
    project_id: str
    root_workspace: str
    agent_id: str
    body: SessionOpenRequest
    created_at: int
    expiry_handle: asyncio.TimerHandle | None = field(default=None)


class PendingOpens:
    """In-process registry of opens waiting for an offline host.

    :param app_state: FastAPI app state, read for the peer sweeper that
        carries the opener's outcome line.
    :param open_entry: Callable that re-validates one entry and runs the
        create + first message; returns the open result or a refusal.
    """

    def __init__(
        self,
        app_state: Any,
        *,
        open_entry: Any,
    ) -> None:
        self._app_state = app_state
        self._open_entry = open_entry
        self._entries: dict[str, _PendingOpen] = {}
        # Strong refs to in-flight fire/expire tasks (a bare task can be GC'd).
        self._tasks: set[asyncio.Task[None]] = set()

    def _schedule(self, coro: Any) -> None:
        """Run one fire/expire coroutine as a tracked background task."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def register(self, entry: _PendingOpen, ttl_s: int) -> None:
        """Add one waiting open and arm its expiry timer.

        :param entry: The pending open.
        :param ttl_s: Lifetime in seconds (``undelivered_ttl_s``).
        """
        entry.expiry_handle = asyncio.get_running_loop().call_later(ttl_s, self._expire, entry.sid)
        self._entries[entry.sid] = entry

    def trigger(self, host_id: str) -> None:
        """Schedule every entry waiting on *host_id* to fire.

        Runs from the tunnel connect callback: it never awaits, it only
        schedules the fire task.

        :param host_id: Host that just came online.
        """
        for entry in list(self._entries.values()):
            if entry.host_id == host_id:
                self._schedule(self._claim_and_open(entry.sid))

    def _claim(self, sid: str) -> _PendingOpen | None:
        """Pop one entry, cancelling its timer; ``None`` when already claimed."""
        entry = self._entries.pop(sid, None)
        if entry is None:
            return None
        if entry.expiry_handle is not None:
            entry.expiry_handle.cancel()
            entry.expiry_handle = None
        return entry

    async def _claim_and_open(self, sid: str) -> None:
        """Fire path: claim the entry, open it, notify the opener."""
        entry = self._claim(sid)
        if entry is None:
            return
        try:
            result = await self._open_entry(entry)
        except Exception:
            _logger.exception("Pending session open failed", extra={"session_id": sid})
            result = {"state": "failed", "reason": "create_failed"}
        state = result.get("state")
        if state == "opened":
            line = f"[System: session {sid} opened on host {entry.host_name}]"
        else:
            reason = result.get("reason") or state or "failed"
            line = f"[System: session {sid} could not open on host {entry.host_name}: {reason}]"
        await self._notify(entry.sender_id, line)
        _logger.info(
            "Pending session open resolved",
            extra={"session_id": sid, "state": state, "host_id": entry.host_id},
        )

    def _expire(self, sid: str) -> None:
        """Timer callback: claim the entry and schedule its expiry notice."""
        entry = self._claim(sid)
        if entry is None:
            return
        self._schedule(self._expire_entry(entry))

    async def _expire_entry(self, entry: _PendingOpen) -> None:
        """Expiry path: notify the opener that the host stayed offline."""
        line = (
            f"[System: session {entry.sid} on host {entry.host_name} expired: "
            "the host stayed offline]"
        )
        await self._notify(entry.sender_id, line)
        _logger.info(
            "Pending session open expired",
            extra={"session_id": entry.sid, "host_id": entry.host_id},
        )

    async def _notify(self, sender_id: str, line: str) -> None:
        """Post one system line to the opener, best effort."""
        sweeper = getattr(self._app_state, "peer_sweeper", None)
        if sweeper is None:
            return
        try:
            await sweeper.notify_line(sender_id, line)
        except Exception:
            _logger.warning(
                "Session-open notice failed",
                exc_info=True,
                extra={"session_id": sender_id},
            )


def register_open_routes(
    router: APIRouter,
    *,
    peer: PeerRoutes,
    project_store: Any,
    conversation_store: Any,
    agent_store: Any,
    runner_router: Any,
    permission_store: Any,
    auth_provider: Any,
    runner_tunnel_tokens: frozenset[str] | None,
    feature_flags: Any,
    host_registry: Any,
    agent_cache: Any,
    file_store: Any,
    artifact_store: Any,
    background_title_coordinator: Any,
    app_state: Any,
) -> None:
    """Register ``POST /sessions/{sender_id}/open`` on the sessions router.

    :param router: The sessions router to register on.
    :param peer: Peer delivery callable from :func:`register_peer_routes`.
    :param project_store: Store for owner-scoped project lookup.
    :param conversation_store: Store for sessions.
    :param agent_store: Store for agent lookup.
    :param runner_router: Runner router, passed to the create orchestration.
    :param permission_store: Permission store, or ``None`` in single-user mode.
    :param auth_provider: Auth provider for user identity extraction.
    :param runner_tunnel_tokens: Server tunnel-token allow-list.
    :param feature_flags: Resolved feature snapshot.
    :param host_registry: Live-host registry.
    :param agent_cache: Agent cache for the create path.
    :param file_store: File metadata store for the create path.
    :param artifact_store: Artifact store for the create path.
    :param background_title_coordinator: Background title coordinator.
    :param app_state: The owning FastAPI app's ``.state``; the pending
        registry is lazily created on it.
    """
    flags = feature_flags if feature_flags is not None else resolve_feature_flags()
    if not flags.enabled(Feature.SESSION_PEER_MESSAGING):

        async def unavailable() -> None:
            raise HTTPException(status_code=404, detail="Not found")

        router.add_api_route(
            "/sessions/{sender_id}/open",
            unavailable,
            methods=["POST"],
            include_in_schema=False,
        )
        return

    host_store = getattr(app_state, "host_store", None)
    binding_store = getattr(app_state, "project_host_binding_store", None)

    async def runner_auth(request: Request, session_id: str) -> Any:
        """Require the sender to exist and its runner token to match."""
        if not _store_id(session_id):
            raise HTTPException(status_code=404, detail="Not found")
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="Not found")
        if not _runner_authorized_for_sender(request, conv, runner_tunnel_tokens):
            raise HTTPException(status_code=403, detail="Runner token does not match session")
        return conv

    async def list_sessions(**filters: Any) -> list[Any]:
        """Read every page of an owner-scoped session listing."""
        rows: list[Any] = []
        after = None
        while True:
            page = await asyncio.to_thread(
                conversation_store.list_conversations, limit=200, after=after, **filters
            )
            rows.extend(page.data)
            if not page.has_more or page.last_id is None:
                return rows
            after = page.last_id

    def _synthetic_request(session_id: str) -> Request:
        """Build the standalone request the pending fire path needs."""
        from omnigent.server.peer_sweeper import PeerSweeper

        sweeper = getattr(app_state, "peer_sweeper", None)
        app = getattr(sweeper, "_app", None) or SimpleNamespace(state=app_state)
        return PeerSweeper._synthetic_request(session_id, app)

    def _placement_problem(
        body: SessionOpenRequest,
        root: Any,
        *,
        state: str,
        project_name: str,
        host_name: str,
    ) -> dict[str, Any] | None:
        """Validate workspace / branch / from_ref against the resolved root.

        :param body: The open request.
        :param root: The resolved :class:`HostRoot`.
        :param state: ``"needs_input"`` (immediate) or ``"failed"`` (fire).
        :param project_name: Project display name for messages.
        :param host_name: Host display name for messages.
        :returns: The problem dict, or ``None`` when the placement is valid.
        """
        if body.workspace is not None:
            if body.branch is not None or body.from_ref is not None:
                return _problem(
                    state,
                    "workspace_with_branch",
                    "workspace joins an existing directory; branch / from_ref "
                    "cut a new one — pass one or the other.",
                )
            if not (
                body.workspace.startswith("/") or _is_windows_absolute_path(body.workspace)
            ) or not _is_subpath_of(body.workspace, _normalized_path(root.workspace)):
                return _problem(
                    state,
                    "workspace_outside_project",
                    f"Workspace {body.workspace!r} is not inside project "
                    f"{project_name!r}'s directory {root.workspace!r} on host "
                    f"{host_name!r}.",
                )
        if (body.from_ref or body.branch) and root.source != "entry":
            return _problem(
                state,
                "no_entry_for_worktree",
                f"Project {project_name!r} has no entry on host {host_name!r} for a "
                "branch worktree; add one in the project settings first.",
            )
        return None

    async def _bound_worktree_git(
        *, host_id: str, workspace: str, repo_path: str
    ) -> SessionGitOptions | None:
        """Bind *workspace* to its listed worktree branch, when one matches.

        A listing failure or a plain directory (subdirectory, detached HEAD,
        or the repo itself for a non-worktree entry) leaves the caller to
        place the session plainly in *workspace*.

        :param host_id: Target host.
        :param workspace: Absolute directory the session joins.
        :param repo_path: Repository whose worktrees are listed.
        :returns: Bind-mode git options, or ``None`` for plain placement.
        """
        if host_registry is None:
            return None
        host_conn = host_registry.get(host_id)
        if host_conn is None:
            return None
        try:
            worktrees = await list_worktrees_on_host(
                host_registry=host_registry,
                host_conn=host_conn,
                repo_path=repo_path,
            )
        except (WorktreeHostUnavailableError, WorktreeProxyError) as exc:
            _logger.warning(
                "Session-open worktree listing failed; placing plainly in %s: %s",
                workspace,
                exc,
                extra={"host_id": host_id},
            )
            return None
        for row in worktrees:
            branch = row.get("branch")
            path = row.get("path")
            if (
                isinstance(branch, str)
                and branch
                and isinstance(path, str)
                and same_canonical_path(path, workspace)
            ):
                return SessionGitOptions(branch_name=branch, existing_worktree=True)
        return None

    async def _open_now(
        *,
        sid: str,
        owner: str,
        create_user_id: str | None,
        sender: Any,
        project: Any,
        host: Any,
        host_id: str,
        root: Any,
        agent: Any,
        agent_name: str,
        body: SessionOpenRequest,
        request: Request,
    ) -> dict[str, Any]:
        """Create the session, grant ownership, deliver the first message."""
        # Grant before create persists its row: a create that raises after
        # writing one must leave the partial session visible and owned.
        if permission_store is not None:
            await asyncio.to_thread(permission_store.ensure_user, owner)
            await asyncio.to_thread(permission_store.grant, owner, sid, LEVEL_OWNER)
        try:
            workspace = root.workspace
            git: SessionGitOptions | None = None
            if body.workspace is not None:
                workspace = body.workspace
                git = await _bound_worktree_git(
                    host_id=host_id,
                    workspace=body.workspace,
                    repo_path=root.checkout or root.workspace,
                )
            elif body.branch is not None:
                git = SessionGitOptions(branch_name=body.branch, base_branch=body.from_ref)
            elif body.from_ref is not None:
                git = SessionGitOptions(branch_name=f"open-{sid[:8]}", base_branch=body.from_ref)
            create_body = ProjectSessionCreateRequest(
                project_id=project.id,
                host_id=host_id,
                workspace=workspace,
                agent_id=agent.id,
                git=git,
                title=body.title,
                model_override=body.model,
                reasoning_effort=body.reasoning_effort,
            )
            _, conv = await _create_session_from_existing_agent(
                conversation_store,
                agent_store,
                runner_router,
                create_body,
                request,
                agent_cache=agent_cache,
                user_id=create_user_id,
                permission_store=permission_store,
                liveness_lookup=None,
                file_store=file_store,
                artifact_store=artifact_store,
                background_title_coordinator=background_title_coordinator,
                project_store=project_store,
                conversation_id=sid,
                calling_path_label="sys_session_open",
            )
        except Exception as exc:
            detail = str(exc)
            reason = (
                "branch_exists"
                if (body.from_ref or body.branch) and "already exists" in detail.lower()
                else "create_failed"
            )
            if (
                permission_store is not None
                and (await asyncio.to_thread(conversation_store.get_conversation, sid)) is None
            ):
                # No row was persisted: drop the provisional grant so a retry
                # does not leave an orphan owner behind.
                try:
                    await asyncio.to_thread(permission_store.revoke, owner, sid)
                except Exception:
                    _logger.warning(
                        "Session-open grant cleanup failed",
                        exc_info=True,
                        extra={"session_id": sid},
                    )
            message = f"Could not open the session: {detail}"
            if reason == "branch_exists":
                message += " To join that branch's worktree, pass workspace=<its directory>."
            return _problem("failed", reason, message)
        _announce_session_added(owner, sid)
        first_message: dict[str, Any] | None = None
        if body.message:
            try:
                delivery = await peer.send(
                    sender=sender,
                    receiver_id=sid,
                    text=body.message,
                    correlation_id=sid,
                    system=False,
                    require_init_success=True,
                    request=request,
                    acting_user_id=owner,
                )
                first_message = {
                    "disposition": delivery.get("disposition"),
                    "reason": delivery.get("reason"),
                }
            except Exception as exc:
                _logger.warning(
                    "Session-open first message failed",
                    exc_info=True,
                    extra={"session_id": sid},
                )
                first_message = {"disposition": "failed", "reason": str(exc)}
        result: dict[str, Any] = {
            "state": "opened",
            "session_id": sid,
            "project": project.name,
            "host_id": host_id,
            "host": host.name,
            "agent": agent_name,
            "workspace": conv.workspace,
            "worktree": conv.worktree,
            "git_branch": conv.git_branch,
            "first_message": first_message,
            "shared_with": [],
        }
        if git is not None and not git.existing_worktree:
            # The open cut a fresh branch worktree; nothing shares it yet.
            return result
        try:
            landed_dir = conv.worktree or conv.workspace
            if landed_dir:
                others = await list_sessions(
                    owned_by=owner if permission_store else None,
                    host_id=host_id,
                    include_archived=False,
                )
                shared = [
                    other
                    for other in others
                    if other.id != sid
                    and other.parent_conversation_id is None
                    and not is_session_closed(other.labels, other.title)
                    and SIDE_CHAT_LABEL_KEY not in (other.labels or {})
                    and same_canonical_path(other.worktree or other.workspace or "", landed_dir)
                ]
                result["shared_with"] = [
                    {
                        "id": other.id,
                        "name": title_without_closed_marker(other.title) or other.id,
                    }
                    for other in shared[:10]
                ]
                if len(shared) > 10:
                    result["shared_with_total"] = len(shared)
        except Exception:
            _logger.warning(
                "Session-open shared-with lookup failed",
                exc_info=True,
                extra={"session_id": sid},
            )
        return result

    async def open_pending(entry: _PendingOpen) -> dict[str, Any]:
        """Re-validate one waiting entry and open it (pending fire path)."""
        sender = await asyncio.to_thread(conversation_store.get_conversation, entry.sender_id)
        if sender is None:
            return _problem("failed", "sender_gone", "The opening session no longer exists.")
        if not await _collab_enabled(entry.owner):
            return _problem("failed", "collab_disabled", COLLAB_DISABLED_MESSAGE)
        if host_registry is None or host_registry.get(entry.host_id) is None:
            return _problem(
                "failed",
                "host_offline",
                f"Host {entry.host_name} is still offline.",
            )
        project = (
            await asyncio.to_thread(project_store.get, entry.project_id, user_id=entry.owner)
            if project_store is not None
            else None
        )
        if project is None:
            return _problem("failed", "project_not_found", "The project is no longer available.")
        bindings = await load_bindings(binding_store, project.id)
        entries = await load_entries(binding_store, project.id)
        host = (
            await asyncio.to_thread(host_store.get_host, entry.host_id)
            if host_store is not None
            else None
        )
        if host is None:
            return _problem(
                "failed", "host_not_found", f"Host {entry.host_name} is no longer available."
            )
        root = root_on_host(project, bindings, entry.host_id, entries=entries)
        if root is None:
            return _problem(
                "failed",
                "no_root",
                f"Project {project.name!r} has no directory on host {entry.host_name!r}.",
            )
        placement = _placement_problem(
            entry.body,
            root,
            state="failed",
            project_name=project.name,
            host_name=entry.host_name,
        )
        if placement is not None:
            return placement
        agent = (
            await asyncio.to_thread(agent_store.get, entry.agent_id)
            if agent_store is not None
            else None
        )
        if agent is None:
            return _problem("failed", "agent_not_found", "The agent is no longer available.")
        try:
            agent = await validate_session_agent(
                user_id=entry.owner,
                agent_id=agent.id,
                agent_store=agent_store,
                permission_store=permission_store,
                conversation_store=conversation_store,
            )
        except OmnigentError:
            return _problem("failed", "agent_not_found", "The agent is no longer available.")
        if (
            agent.session_id is not None
            and permission_store is not None
            and await asyncio.to_thread(get_session_owner_id, agent.session_id, permission_store)
            != entry.owner
        ):
            return _problem(
                "failed",
                "agent_not_found",
                "The agent belongs to another user's session.",
            )
        return await _open_now(
            sid=entry.sid,
            owner=entry.owner,
            create_user_id=entry.create_user_id,
            sender=sender,
            project=project,
            host=host,
            host_id=entry.host_id,
            root=root,
            agent=agent,
            agent_name=public_agent_name(agent.name) or agent.name,
            body=entry.body,
            request=_synthetic_request(entry.sid),
        )

    async def _collab_enabled(owner: str) -> bool:
        """Whether *owner*'s session-collaboration master switch is on."""
        try:
            await asyncio.to_thread(
                require_collab_enabled,
                getattr(app_state, "user_preferences_store", None),
                owner,
            )
        except OmnigentError:
            return False
        return True

    entry_registry: PendingOpens
    existing_pending = getattr(app_state, "pending_session_opens", None)
    if isinstance(existing_pending, PendingOpens):
        entry_registry = existing_pending
    else:
        entry_registry = PendingOpens(app_state, open_entry=open_pending)
        if app_state is not None:
            app_state.pending_session_opens = entry_registry

    @router.post("/sessions/{sender_id}/open", include_in_schema=False, response_model=None)
    async def open_session(
        request: Request, sender_id: str, body: SessionOpenRequest
    ) -> dict[str, Any]:
        """Open a plain top-level session on a project host."""
        sender = await runner_auth(request, sender_id)
        if sender.parent_conversation_id is not None:
            return _problem(
                "refused",
                "is_subagent",
                "Only a top-level session can open another session.",
            )
        request_user_id = _get_user_id(request, auth_provider)
        owner = (
            await asyncio.to_thread(
                effective_owner_id, sender, conversation_store, permission_store
            )
            if permission_store
            else request_user_id or RESERVED_USER_LOCAL
        )
        if owner is None:
            return _problem("refused", "not_same_owner", "The sender session has no owning user.")
        # The create path authorizes against the request user without a
        # permission store; the owner sentinel is only for grants and locks.
        create_user_id = owner if permission_store else request_user_id
        if not await _collab_enabled(owner):
            return _problem("refused", "collab_disabled", COLLAB_DISABLED_MESSAGE)
        project = (
            await asyncio.to_thread(project_store.get, body.project, user_id=owner)
            if project_store and _store_id(body.project)
            else None
        )
        if project is None:
            projects = (
                await asyncio.to_thread(project_store.list, user_id=owner) if project_store else []
            )
            matches = [
                item for item in projects if item.name.casefold() == body.project.casefold()
            ]
            if len(matches) != 1:
                return _problem(
                    "needs_input",
                    "project_ambiguous" if matches else "project_not_found",
                    f"Project {body.project!r} was not found."
                    if not matches
                    else f"Project name {body.project!r} matches several projects.",
                    [{"id": item.id, "name": item.name} for item in (matches or projects)],
                )
            project = matches[0]
        bindings = await load_bindings(binding_store, project.id)
        entries = await load_entries(binding_store, project.id)
        roots = host_roots(project, bindings, entries=entries)
        eligible = await load_eligible_host_ids(
            host_store, owner, (root.host_id for root in roots)
        )
        candidates: list[dict[str, str]] = []
        for candidate_root in roots:
            if eligible is not None and candidate_root.host_id not in eligible:
                continue
            host_row = (
                await asyncio.to_thread(host_store.get_host, candidate_root.host_id)
                if host_store
                else None
            )
            candidates.append(
                {
                    "id": candidate_root.host_id,
                    "name": host_row.name if host_row else candidate_root.host_id,
                }
            )
        hosts = await asyncio.to_thread(host_store.list_hosts, owner) if host_store else []
        host = next((item for item in hosts if item.host_id == body.host), None)
        if host is None:
            named = [item for item in hosts if item.name.casefold() == body.host.casefold()]
            host = named[0] if len(named) == 1 else None
        if host is None:
            return _problem(
                "needs_input",
                "host_not_found",
                f"Host {body.host!r} was not found. Pick one of the candidates "
                "that has this project's directory.",
                candidates,
            )
        host_id = host.host_id
        root = root_on_host(project, bindings, host_id, entries=entries)
        if root is None:
            return _problem(
                "needs_input",
                "no_root",
                f"Project {project.name!r} has no directory on host {host.name!r}.",
                candidates,
            )
        placement = _placement_problem(
            body,
            root,
            state="needs_input",
            project_name=project.name,
            host_name=host.name,
        )
        if placement is not None:
            return placement
        agent = (
            await asyncio.to_thread(agent_store.get, body.agent)
            if agent_store and _store_id(body.agent)
            else None
        )
        if agent is None and agent_store:
            agent = await asyncio.to_thread(agent_store.get_by_name, body.agent)
        if agent is None and agent_store:
            matches = []
            after = None
            while True:
                page = await asyncio.to_thread(agent_store.list, limit=200, after=after)
                for item in page.data:
                    if (public_agent_name(item.name) or "").casefold() != body.agent.casefold():
                        continue
                    try:
                        visible = await validate_session_agent(
                            user_id=owner,
                            agent_id=item.id,
                            agent_store=agent_store,
                            permission_store=permission_store,
                            conversation_store=conversation_store,
                        )
                    except OmnigentError:
                        continue
                    matches.append(visible)
                if not page.has_more or page.last_id is None:
                    break
                after = page.last_id
            if len(matches) != 1:
                return _problem(
                    "needs_input",
                    "agent_not_found",
                    f"Agent {body.agent!r} was not found.",
                    [
                        {"id": item.id, "name": public_agent_name(item.name) or item.name}
                        for item in matches
                    ],
                )
            agent = matches[0]
        if agent is None:
            return _problem(
                "needs_input", "agent_not_found", f"Agent {body.agent!r} was not found."
            )
        try:
            agent = await validate_session_agent(
                user_id=owner,
                agent_id=agent.id,
                agent_store=agent_store,
                permission_store=permission_store,
                conversation_store=conversation_store,
            )
        except OmnigentError:
            return _problem(
                "needs_input", "agent_not_found", f"Agent {body.agent!r} was not found."
            )
        if (
            agent.session_id is not None
            and permission_store is not None
            and await asyncio.to_thread(get_session_owner_id, agent.session_id, permission_store)
            != owner
        ):
            return _problem(
                "needs_input",
                "agent_not_found",
                f"Agent {body.agent!r} belongs to another user's session.",
            )
        agent_name = public_agent_name(agent.name) or agent.name
        refusal_text = await asyncio.to_thread(admit_open, app_state, owner)
        if refusal_text is not None:
            return _problem("refused", "open_rate", refusal_text)
        host_online = host_registry is not None and host_registry.get(host_id) is not None
        if not host_online:
            if not body.wait_for_host:
                return _problem(
                    "refused",
                    "host_offline",
                    f"Host {host.name!r} is offline. Retry with wait_for_host=true "
                    "to open the session when it connects.",
                )
            sid = uuid.uuid4().hex
            settings = await asyncio.to_thread(
                read_collab_settings,
                getattr(app_state, "user_preferences_store", None),
                owner,
            )
            entry_registry.register(
                _PendingOpen(
                    sid=sid,
                    owner=owner,
                    create_user_id=create_user_id,
                    sender_id=sender_id,
                    host_id=host_id,
                    host_name=host.name,
                    project_id=project.id,
                    root_workspace=root.workspace,
                    agent_id=agent.id,
                    body=body,
                    created_at=int(time.time()),
                ),
                settings.undelivered_ttl_s,
            )
            return {
                "state": "waiting_for_host",
                "session_id": sid,
                "host_id": host_id,
                "host": host.name,
                "project": project.name,
                "agent": agent_name,
            }
        sid = uuid.uuid4().hex
        return await _open_now(
            sid=sid,
            owner=owner,
            create_user_id=create_user_id,
            sender=sender,
            project=project,
            host=host,
            host_id=host_id,
            root=root,
            agent=agent,
            agent_name=agent_name,
            body=body,
            request=request,
        )
