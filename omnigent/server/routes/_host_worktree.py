"""
Server-side proxies for the host git-worktree tunnel frames.

Like ``_workspace_validation._ask_host_stat``: enqueue a
``host.create_worktree`` / ``host.remove_worktree`` frame, register a
future on the host connection, and await the result with a timeout. The
host (not the server) runs git. See designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from omnigent.host.frames import (
    HostCreateWorktreeFrame,
    HostFolderFactsFrame,
    HostListWorktreesFrame,
    HostRemoveWorktreeFrame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry

_logger = logging.getLogger(__name__)

# Above the host's own git timeout (120 s) so the host's specific error
# surfaces instead of a generic server-side timeout.
_WORKTREE_TIMEOUT_S: float = 150.0

# Folder facts run five short git reads with a 10 s per-command host
# timeout; the settings dialog wants them quickly and can re-read on
# Refresh, so the server waits only modestly longer than one read.
_FOLDER_FACTS_TIMEOUT_S: float = 15.0


WORKTREE_ROOT_LABEL_KEY = "omnigent.git.worktree_root_sha256"


def worktree_root_fingerprint(path: str) -> str:
    """Identify a canonical host root within the 256-character label limit.

    :param path: Canonical absolute worktree root returned by the host.
    :returns: Stable digest, normalizing Windows casing and separators.
    """
    if PureWindowsPath(path).is_absolute():
        path = path.replace("\\", "/").lower()
    return hashlib.sha256(path.rstrip("/").encode()).hexdigest()


def recorded_worktree_root(workspace: str, fingerprint: str | None) -> str | None:
    """Recover the recorded root from canonical workspace ancestors.

    :param workspace: Stored canonical session directory on the host.
    :param fingerprint: Recorded root digest, absent for legacy root sessions.
    :returns: Matching root, or None when the directory belongs to another worktree.
    """
    if fingerprint is None:
        return workspace
    path = (
        PureWindowsPath(workspace)
        if PureWindowsPath(workspace).is_absolute()
        else PurePosixPath(workspace)
    )
    return next(
        (
            str(parent)
            for parent in (path, *path.parents)
            if worktree_root_fingerprint(str(parent)) == fingerprint
        ),
        None,
    )


class WorktreeProxyError(Exception):
    """
    Raised when the host reports a worktree operation failure.

    These are typically user-correctable input problems (branch
    already exists, not a git repo, bad base ref), so the route layer
    maps this to ``INVALID_INPUT`` (400).

    :param message: Human-readable error suitable for the API
        response body, e.g.
        ``"worktree creation failed: branch already exists"``.
    """

    def __init__(self, message: str) -> None:
        """
        Initialize with the user-facing error message.

        :param message: Error string surfaced to the API caller.
        """
        super().__init__(message)
        self.message = message


class WorktreeHostUnavailableError(WorktreeProxyError):
    """
    Raised when the host can't be reached for a worktree operation.

    Connection loss or no reply within the timeout — an infrastructure
    condition, not user input. The route layer maps this to
    ``CONFLICT`` (409). Subclasses :class:`WorktreeProxyError` so
    best-effort callers that catch the base type still catch it.
    """


class WorktreeHostRefusalError(WorktreeProxyError):
    """The host replied that Git refused removal; no directory was removed."""


class FolderFactsUnsupportedError(WorktreeProxyError):
    """
    Raised when the connected host build predates ``host.folder_facts``.

    The hello carries ``project_code``; without it the host would drop the
    frame silently, so the route answers 501 "update the host" instead of
    waiting out the timeout. Subclasses :class:`WorktreeProxyError` for the
    same best-effort-caller reason as :class:`WorktreeHostUnavailableError`.
    """


@dataclass
class CreatedWorktree:
    """
    Result of a successful host worktree creation.

    :param worktree_path: Absolute path of the created worktree
        directory on the host, e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``. Used for rollback.
    :param branch: The branch checked out in the worktree, e.g.
        ``"feature/login"``.
    :param workspace: Selected directory relocated into the new worktree.
        ``None`` for results from older hosts.
    """

    worktree_path: str
    branch: str
    workspace: str | None = None


async def _await_host_worktree_result(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    pending: dict[str, asyncio.Future[dict[str, object]]],
    request_id: str,
    frame: str,
    op: str,
    timeout: float | None = None,
    timeout_hint: str = (" (it may be running an older version that does not support worktrees)"),
) -> dict[str, object]:
    """
    Send a worktree frame and await its matching result over the tunnel.

    Shared plumbing for the create/remove proxies: register a future on
    ``pending`` keyed by ``request_id``, enqueue ``frame``, await the
    reply, and clean up on every path.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection.
    :param pending: The connection's pending-future map for this op
        (``pending_create_worktrees`` or ``pending_remove_worktrees``).
    :param request_id: Correlation id already embedded in ``frame``.
    :param frame: Encoded host frame to send.
    :param op: Short label for error messages, e.g.
        ``"worktree creation"``.
    :param timeout: Seconds to await the reply; ``None`` reads
        :data:`_WORKTREE_TIMEOUT_S` at call time.
    :param timeout_hint: Suffix appending the likely cause of silence;
        the empty string omits it.
    :returns: The host's result dict (``status`` plus op-specific
        fields).
    :raises WorktreeHostUnavailableError: On connection loss or no
        reply within ``timeout``.
    """
    if timeout is None:
        timeout = _WORKTREE_TIMEOUT_S
    future: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    pending[request_id] = future
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError as exc:
            raise WorktreeHostUnavailableError(
                f"host '{host_conn.host_id}' connection lost during {op}"
            ) from exc
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise WorktreeHostUnavailableError(
                f"host '{host_conn.host_id}' did not respond to {op} within "
                f"{timeout:.0f}s{timeout_hint}"
            ) from exc
    finally:
        pending.pop(request_id, None)


async def create_worktree_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    repo_path: str,
    branch_name: str,
    base_branch: str | None,
    existing_branch: bool = False,
    entry: str | None = None,
    path_template: str | None = None,
) -> CreatedWorktree:
    """
    Send a ``host.create_worktree`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection to create the worktree on.
    :param repo_path: Absolute path inside the source repo on the
        host — the canonical picked directory, e.g.
        ``"/Users/alice/myrepo"``.
    :param branch_name: New branch to create, e.g. ``"feature/login"``.
    :param base_branch: Optional base ref, e.g. ``"main"``. ``None``
        branches from the repo's current ``HEAD``.
    :param existing_branch: When ``True``, the host checks out the
        pre-existing ``branch_name`` into a fresh worktree (the
        deleted-worktree recreate path) instead of creating a branch.
    :param entry: The session project's entry directory on the host, or
        ``None``. Only fills the template's ``{entry}`` token; on its
        own it no longer decides the location.
    :param path_template: The owner's worktree location template, e.g.
        ``"{entry}/.worktrees/{repo}/{branch}"``, or ``None`` for the
        upstream sibling layout. The host validates and renders it.
    :returns: The created worktree's path and branch.
    :raises WorktreeHostUnavailableError: If the host connection drops
        or doesn't respond within :data:`_WORKTREE_TIMEOUT_S`.
    :raises WorktreeProxyError: If the host reports a worktree failure.
    """
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(
        HostCreateWorktreeFrame(
            request_id=request_id,
            repo_path=repo_path,
            branch_name=branch_name,
            base_branch=base_branch,
            existing_branch=existing_branch,
            entry=entry,
            path_template=path_template,
        )
    )
    result = await _await_host_worktree_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_create_worktrees,
        request_id=request_id,
        frame=frame,
        op="worktree creation",
    )
    if result.get("status") != "ok":
        raise WorktreeProxyError(
            f"worktree creation failed: {result.get('error') or 'host reported no detail'}"
        )
    worktree_path = result.get("worktree_path")
    branch = result.get("branch")
    if not isinstance(worktree_path, str) or not isinstance(branch, str):
        raise WorktreeProxyError("host returned an incomplete worktree result")
    workspace = result.get("workspace")
    return CreatedWorktree(
        worktree_path=worktree_path,
        branch=branch,
        workspace=workspace if isinstance(workspace, str) else None,
    )


async def remove_worktree_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    worktree_path: str,
    branch: str | None,
    delete_branch: bool,
    safe_only: bool = False,
) -> None:
    """
    Send a ``host.remove_worktree`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection that owns the worktree.
    :param worktree_path: Absolute path of the worktree to remove on
        the host, e.g. ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Branch to delete when ``delete_branch`` is
        ``True``, e.g. ``"feature/login"``. ``None`` skips branch
        deletion.
    :param delete_branch: When ``True``, delete ``branch`` after
        removing the worktree directory.
    :raises WorktreeHostUnavailableError: If the host connection drops
        or doesn't respond within :data:`_WORKTREE_TIMEOUT_S`.
    :raises WorktreeProxyError: If the host reports a removal failure.
    """
    if safe_only:
        from omnigent.host.frames import CAP_WORKTREE_SAFE_ARCHIVE

        if CAP_WORKTREE_SAFE_ARCHIVE not in host_conn.hello.capabilities:
            raise WorktreeProxyError("host does not support safe archive removal")
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(
        HostRemoveWorktreeFrame(
            request_id=request_id,
            worktree_path=worktree_path,
            branch=branch,
            delete_branch=delete_branch,
            safe_only=safe_only,
        )
    )
    result = await _await_host_worktree_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_remove_worktrees,
        request_id=request_id,
        frame=frame,
        op="worktree removal",
    )
    if result.get("status") != "ok":
        raise WorktreeHostRefusalError(
            f"worktree removal failed: {result.get('error') or 'host reported no detail'}"
        )


async def list_worktrees_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    repo_path: str,
    for_cleanup: bool = False,
    for_status: bool = False,
) -> list[dict[str, object]]:
    """
    Send a ``host.list_worktrees`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection to list worktrees on.
    :param repo_path: Absolute path inside the source repo on the
        host — the canonical picked directory, e.g.
        ``"/Users/alice/myrepo"``.
    :param for_cleanup: Recover a canonical workspace without following replacement symlinks.
    :returns: One dict per worktree with keys ``path``, ``branch``,
        ``is_main``, ``detached``, and optional ``updated_at``
        (main first).
    :raises WorktreeHostUnavailableError: If the host connection drops
        or doesn't respond within :data:`_WORKTREE_TIMEOUT_S`.
    :raises WorktreeProxyError: If the host reports a listing failure.
    """
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(
        HostListWorktreesFrame(
            request_id=request_id,
            repo_path=repo_path,
            for_cleanup=for_cleanup,
            for_status=for_status,
        )
    )
    result = await _await_host_worktree_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_list_worktrees,
        request_id=request_id,
        frame=frame,
        op="worktree listing",
        timeout=15.0 if for_status else _WORKTREE_TIMEOUT_S,
    )
    if result.get("status") != "ok":
        raise WorktreeProxyError(
            f"worktree listing failed: {result.get('error') or 'host reported no detail'}"
        )
    worktrees = result.get("worktrees")
    if not isinstance(worktrees, list):
        raise WorktreeProxyError("host returned an incomplete worktree list")
    return worktrees


# custom-lint: disable-next=workspace-scoped-cache -- task identities keep in-flight work alive
_worktree_admission_refresh_tasks: set[asyncio.Task[bool]] = set()


async def refresh_worktree_admission_fence(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    conversation_store: Any,
    host_id: str,
    workspace: str,
    branch: str | None,
) -> bool:
    """Revalidate a recreated linked tree before clearing an old removal fence."""
    from omnigent.server.routes._workspace_validation import _is_subpath_of

    check = getattr(conversation_store, "_worktree_admission_fenced", None)
    clear = getattr(conversation_store, "clear_verified_worktree_admission_fence", None)
    host_store = getattr(conversation_store, "_host_binding_store", None)
    if not callable(check) or not callable(clear) or host_store is None:
        return False
    if not await asyncio.to_thread(check, host_id, workspace):
        return False

    async def _under_lease() -> bool:
        token = await asyncio.to_thread(
            host_store.acquire_worktree_admission,
            host_id,
            required=True,
            wait_timeout_s=15.0,
        )
        try:
            trees = await list_worktrees_on_host(
                host_registry=host_registry,
                host_conn=host_conn,
                repo_path=workspace,
                for_cleanup=True,
            )
            matches = [
                tree["path"]
                for tree in trees
                if isinstance(tree.get("path"), str)
                and _is_subpath_of(workspace, tree["path"])
                and not tree.get("is_main", True)
                and not tree.get("detached", True)
                and (branch is None or tree.get("branch") == branch)
            ]
            if not matches:
                raise WorktreeProxyError("worktree binding changed after archive cleanup")
            await asyncio.to_thread(clear, host_id, max(matches, key=len))
            return True
        finally:
            await asyncio.to_thread(host_store.release_cli_retention, host_id, token)

    task = asyncio.create_task(_under_lease())
    _worktree_admission_refresh_tasks.add(task)

    def _finish(completed: asyncio.Task[bool]) -> None:
        _worktree_admission_refresh_tasks.discard(completed)
        if not completed.cancelled():
            completed.exception()

    task.add_done_callback(_finish)
    return await asyncio.shield(task)


async def folder_facts_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    path: str,
) -> dict[str, object]:
    """
    Send a ``host.folder_facts`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection to read the folder on.
    :param path: Absolute folder path on the host, e.g.
        ``"/Users/alice/myrepo"``.
    :returns: The facts dict (``exists``, ``is_dir``, ``is_repo``,
        ``toplevel``, ``branch``, ``head``, ``detached``, ``dirty``,
        ``remotes``, ``setup_command_configured``, ``error``).
    :raises FolderFactsUnsupportedError: When the host build advertises
        no ``project_code`` capability.
    :raises WorktreeHostUnavailableError: If the host connection drops
        or doesn't respond within :data:`_FOLDER_FACTS_TIMEOUT_S`.
    :raises WorktreeProxyError: If the host reports a read failure.
    """
    if not host_conn.hello.project_code:
        raise FolderFactsUnsupportedError(
            f"host '{host_conn.host_id}' does not support folder facts — "
            "update omnigent on the host and retry"
        )
    request_id = secrets.token_hex(8)
    frame = encode_host_frame(HostFolderFactsFrame(request_id=request_id, path=path))
    result = await _await_host_worktree_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_folder_facts,
        request_id=request_id,
        frame=frame,
        op="folder facts",
        timeout=_FOLDER_FACTS_TIMEOUT_S,
        timeout_hint="",
    )
    if result.get("status") != "ok":
        raise WorktreeProxyError(
            f"folder facts read failed: {result.get('error') or 'host reported no detail'}"
        )
    facts = dict(result)
    facts.pop("status", None)
    return facts


def match_worktree_branch(
    worktrees: list[dict[str, Any]] | None,
    branch: str | None,
) -> str | None:
    """Return the path of the worktree checking out *branch*, or ``None``.

    The branch-match rule for cross-host member placement (F2b): a session's
    branch maps to the worktree on the target host that has it checked out.
    ``list_worktrees_and_match_branch`` applies it to a fresh listing.

    :param worktrees: Host worktree rows (``path`` / ``branch`` / …), or
        ``None`` when the listing was skipped or failed best-effort.
    :param branch: Branch to match, or ``None`` for no match.
    :returns: The matching worktree's path, or ``None``.
    """
    if not branch or worktrees is None:
        return None
    match = next((w for w in worktrees if w.get("branch") == branch), None)
    return str(match["path"]) if match and match.get("path") is not None else None


async def list_worktrees_and_match_branch(
    *,
    host_registry: Any,
    host_conn: Any,
    repo_path: str,
    branch: str | None,
) -> tuple[list[dict[str, object]], str | None]:
    """List a host repository's worktrees and match *branch* in one step.

    Cross-host member placement (F2b) uses the listing and the branch-match
    rule together, so both exist once.

    :param host_registry: Server host registry (frame transport).
    :param host_conn: Live host connection to list on.
    :param repo_path: Absolute repository path on the host.
    :param branch: Branch to match, or ``None`` for no match.
    :returns: ``(worktrees, matched_path)``.
    :raises WorktreeProxyError: When the host reports a listing failure.
    """
    worktrees = await list_worktrees_on_host(
        host_registry=host_registry, host_conn=host_conn, repo_path=repo_path
    )
    return worktrees, match_worktree_branch(worktrees, branch)
