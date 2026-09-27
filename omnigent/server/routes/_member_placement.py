"""Cross-host member placement: a session's branch worktree on another host (SCC06 F2b).

A joint-agent member saved on another host runs there, in the checkout of the
same repository and branch the lead session is working in. The repository is
resolved on the member's host through the lead session's project (the same
``project_placement`` resolution SCC01 S2 uses) and the branch is the lead
session's recorded ``git_branch``; the worktree checking that branch out on the
target host is the member's working directory.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.project_placement import (
    bindings_apply,
    load_bindings,
    load_entries,
    root_on_host,
)
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._host_worktree import (
    WorktreeHostUnavailableError,
    WorktreeProxyError,
)
from omnigent.server.routes.sessions.routes_handoff import list_worktrees_and_match_branch

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MemberWorktree:
    """The repository + branch worktree a remote member runs in.

    :param workspace: Absolute worktree path on the target host, e.g.
        ``"/Users/alice/myrepo/.worktrees/feature-login"``. Stored as the
        child session's workspace.
    :param repository: The repository the worktree belongs to on that host.
    :param branch: The branch the worktree has checked out.
    """

    workspace: str
    repository: str
    branch: str


async def resolve_member_worktree_on_host(
    *,
    conversation: Any,
    host_id: str,
    user_id: str | None,
    project_store: Any,
    binding_store: Any,
    host_registry: Any,
    host_store: Any,
    feature_flags: Any,
) -> MemberWorktree:
    """
    Resolve *conversation*'s branch worktree on *host_id* for member dispatch.

    The lead session must be filed under a project that has a directory on the
    target host, and its recorded branch must be checked out in one of that
    host directory's worktrees — the member runs where the lead works.

    The target host is authorized first, with the same rule ``GET
    /v1/hosts/{id}`` uses (owner check, skipped when auth is disabled): a
    caller-supplied project must not turn this route into a probe of another
    owner's host.

    :param conversation: The lead session row (needs ``project_id``,
        ``git_branch``, ``workspace``).
    :param host_id: The member's target host.
    :param user_id: Caller used for the host and owner-scoped project reads, or
        ``None`` when auth is disabled.
    :param project_store: Project store, or ``None`` (a server without one
        cannot map a repository to another host).
    :param binding_store: Per-host binding / entry store, or ``None``.
    :param host_registry: Live host tunnel registry, or ``None``.
    :param host_store: Persistent host registrations, used to authorize the
        target host before any host request.
    :param feature_flags: Feature flags driving the placement gates.
    :returns: The resolved :class:`MemberWorktree`.
    :raises HTTPException: 404 if the host is unknown; 403 if it is owned by a
        different user.
    :raises OmnigentError: 400 naming the missing fact (no project, no
        directory on the host, no recorded branch, no matching worktree) or
        409 when the host cannot be reached for the worktree listing.
    """
    if host_store is None:
        raise OmnigentError(
            "this server cannot verify the target host's owner",
            code=ErrorCode.INTERNAL_ERROR,
        )
    # Ownership runs FIRST: before the project read, the registry lookup, and
    # the worktree listing (which contacts the host). Cross-user host probe.
    await asyncio.to_thread(
        resolve_host_owner,
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
    )
    if project_store is None:
        raise OmnigentError(
            "this server cannot resolve a project's repository on another host",
            code=ErrorCode.INTERNAL_ERROR,
        )
    project_id = getattr(conversation, "project_id", None)
    if not project_id:
        raise OmnigentError(
            f"session {conversation.id!r} has no project; a member on host "
            f"{host_id!r} needs one to map its repository there",
            code=ErrorCode.INVALID_INPUT,
        )
    project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
    if project is None:
        raise OmnigentError(f"project {project_id!r} was not found", code=ErrorCode.INVALID_INPUT)
    branch = getattr(conversation, "git_branch", None)
    if not isinstance(branch, str) or not branch:
        raise OmnigentError(
            f"session {conversation.id!r} has no recorded git branch; the member "
            f"on host {host_id!r} needs the branch the lead works in",
            code=ErrorCode.INVALID_INPUT,
        )
    bindings = await load_bindings(binding_store, project.id)
    entries = await load_entries(binding_store, project.id)
    gates_on = bindings_apply(project, feature_flags)
    root = root_on_host(project, bindings, host_id, gates_on=gates_on, entries=entries)
    if root is None:
        raise OmnigentError(
            f"project {project.name!r} has no directory on host {host_id!r}; add "
            "one in the project settings",
            code=ErrorCode.INVALID_INPUT,
        )
    repository = root.checkout if root.source == "entry" and root.checkout else root.workspace
    host_conn = host_registry.get(host_id) if host_registry is not None else None
    if host_conn is None:
        raise OmnigentError(
            f"host {host_id!r} is not connected; reconnect it and retry",
            code=ErrorCode.CONFLICT,
        )
    try:
        _worktrees, matched = await list_worktrees_and_match_branch(
            host_registry=host_registry,
            host_conn=host_conn,
            repo_path=repository,
            branch=branch,
        )
    except WorktreeHostUnavailableError as exc:
        raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
    except WorktreeProxyError as exc:
        raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
    if matched is None:
        raise OmnigentError(
            f"branch {branch!r} is not checked out in a worktree of "
            f"{repository!r} on host {host_id!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return MemberWorktree(workspace=matched, repository=repository, branch=branch)
