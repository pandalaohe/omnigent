"""REST API routes for project collaboration configuration.

Covers the registered repositories and the per-host directory bindings
that supply project roots and worktree sources. Every route requires the
caller to own the project; the surface is always on (the former
``project_assignments`` flag is a deprecated no-op).
"""

from __future__ import annotations

import asyncio
import posixpath
import re
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from omnigent.db.utils import now_epoch
from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.entities import ProjectHostBinding, ProjectRepository
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostPostBindHookFrame, encode_host_frame
from omnigent.runtime.prompt import project_code_instruction
from omnigent.server.auth import AuthProvider
from omnigent.server.project_placement import project_code_locations
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.server.routes._session_create_validation import (
    _authorize_host_for_workspace,
)
from omnigent.server.routes._workspace_validation import (
    WorkspaceValidationError,
    _is_windows_absolute_path,
    validate_workspace,
)
from omnigent.stores.project_host_binding_store import ProjectHostBindingStore
from omnigent.stores.project_repository_store import ProjectRepositoryStore
from omnigent.stores.project_store import ProjectStore

if TYPE_CHECKING:
    from omnigent.server.host_registry import HostRegistry

_DEFAULT_MANIFEST_PATH = ".agents/project/manifest.json"

# The host caps one hook run at 30 s; the server waits a little longer so a
# host that is still starting the command when it replies lands inside the wait.
_POST_BIND_HOOK_TIMEOUT_S: float = 35.0

# Repository and binding names become part of
# ``refs/omnigent/assignments/<id>/input/<name>``, so they must be a single
# safe ref-path segment.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_RESERVED_NAMES = frozenset({".", ".."})

# The server keeps the latest post-bind outcome per
# (project, host, kind, name) for GET /collaboration, since server start; a
# restart clears it. Workspace-scoped so tenants sharing a pod never read
# each other's outcomes.
_SETUP_OUTCOMES: WorkspaceScopedCache[tuple[str, str, str, str | None], dict[str, Any]] = (
    WorkspaceScopedCache()
)


class RepositoryPutRequest(BaseModel):
    """Request body for ``PUT /v1/projects/{project_id}/repositories/{name}``.

    :param remote_url: The shared remote; may be empty when no git location
        is registered. A non-empty URL must carry no credentials and no
        query or fragment.
    :param default_branch: The repository's default branch name.
    :param role: ``"code"`` or ``"related"``; omitted keeps the stored role
        (``"related"`` on create).
    :param context_manifest_path: Repo-relative manifest path. Defaults to
        ``.agents/project/manifest.json`` when omitted.
    """

    model_config = ConfigDict(extra="forbid")

    remote_url: str
    default_branch: str
    role: str | None = None
    context_manifest_path: str | None = None


class BindingPutRequest(BaseModel):
    """Request body for ``PUT /v1/projects/{project_id}/hosts/{host_id}/bindings/{name}``.

    :param workspace: Absolute path on the host. Validated live via
        ``host.stat``; the canonical path the host returns is stored. An
        offline host stores the typed path after string checks.
    :param repository_name: Name of a repository registered on this project.
    :param is_primary: Deprecated and ignored (removal target 0.17.0): the
        primary binding is derived from the project's code repository.
    :param enabled: Whether the binding is eligible at claim time.
    """

    model_config = ConfigDict(extra="forbid")

    workspace: str
    repository_name: str
    is_primary: bool = False
    enabled: bool = True


def _validate_ref_name(value: str, *, kind: str) -> str:
    """Validate a repository or binding name as a safe ref-path segment.

    Beyond the charset, git forbids dot-led/trailing dots, ``..`` and
    a ``.lock`` suffix anywhere in the ref, so those are rejected here.

    :param value: The raw name from the path.
    :param kind: ``"repository"`` or ``"binding"``, for error messages.
    :returns: The validated name.
    :raises OmnigentError: ``INVALID_INPUT`` when the name is not a single
        safe segment.
    """
    if (
        not _NAME_RE.fullmatch(value)
        or value in _RESERVED_NAMES
        or value.startswith(".")
        or value.endswith((".", ".lock"))
        or ".." in value
    ):
        raise OmnigentError(
            f"invalid {kind} name {value!r}: use 1-100 letters, digits, '.', '_' or '-'",
            code=ErrorCode.INVALID_INPUT,
        )
    return value


def _validate_remote_url(remote_url: str, *, name: str) -> str:
    """Validate a repository remote: empty or credential-free.

    ``http(s)`` remotes must carry no userinfo at all (a bare
    access token in the username position is still a credential);
    any other scheme, or an scp-like ``user@host:path`` remote,
    allows a username but never a password. A URL form must also carry no
    query or fragment — those are where typed tokens usually hide.

    :param remote_url: The raw remote from the request.
    :param name: The repository name, for error messages; never echoes the
        URL itself.
    :returns: The trimmed remote, ``""`` when none was given.
    :raises OmnigentError: ``INVALID_INPUT`` when the remote carries
        credentials or a query / fragment (which must never be stored).
    """
    trimmed = (remote_url or "").strip()
    if not trimmed:
        return ""
    if "://" in trimmed:
        try:
            parts = urlsplit(trimmed)
            username, password = parts.username, parts.password
        except ValueError:
            raise OmnigentError(
                f"repository {name!r} has an invalid remote_url",
                code=ErrorCode.INVALID_INPUT,
            ) from None
        if parts.scheme.lower() in ("http", "https"):
            if not parts.hostname:
                raise OmnigentError(
                    f"repository {name!r} has an invalid remote_url",
                    code=ErrorCode.INVALID_INPUT,
                )
            forbidden = username is not None
        else:
            forbidden = bool(password)
        if forbidden:
            raise OmnigentError(
                f"repository {name!r} remote_url must not contain credentials",
                code=ErrorCode.INVALID_INPUT,
            )
        if parts.query or parts.fragment:
            raise OmnigentError(
                f"repository {name!r} remote_url must not contain a query or fragment",
                code=ErrorCode.INVALID_INPUT,
            )
    elif trimmed.lower().startswith(("http:", "https:")):
        raise OmnigentError(
            f"repository {name!r} has an invalid remote_url",
            code=ErrorCode.INVALID_INPUT,
        )
    elif "@" in trimmed:
        # Text before the first "@" is userinfo only when it has neither "/" nor "[".
        # Such userinfo is credentials only when it contains ":".
        userinfo, _, _ = trimmed.partition("@")
        if "/" not in userinfo and "[" not in userinfo and ":" in userinfo:
            raise OmnigentError(
                f"repository {name!r} remote_url must not contain credentials",
                code=ErrorCode.INVALID_INPUT,
            )
    return trimmed


def _validate_default_branch(default_branch: str) -> str:
    """Validate a default branch name (non-empty, fits the column).

    :param default_branch: The raw branch from the request.
    :returns: The trimmed branch.
    :raises OmnigentError: ``INVALID_INPUT`` when empty or over 255 chars.
    """
    trimmed = (default_branch or "").strip()
    if not trimmed:
        raise OmnigentError(
            "default_branch must not be empty",
            code=ErrorCode.INVALID_INPUT,
        )
    if len(trimmed) > 255:
        raise OmnigentError(
            "default_branch must be at most 255 characters",
            code=ErrorCode.INVALID_INPUT,
        )
    return trimmed


def _validate_manifest_path(context_manifest_path: str | None) -> str:
    """Validate the manifest path as repo-relative without escapes.

    Backslashes and drive-letter prefixes are rejected too, so a
    Windows-style absolute or escaping path cannot pass as relative.

    :param context_manifest_path: The raw path from the request, or ``None``
        for the default.
    :returns: The validated path.
    :raises OmnigentError: ``INVALID_INPUT`` on an absolute, escaping or
        empty-segment path.
    """
    if context_manifest_path is None:
        return _DEFAULT_MANIFEST_PATH
    trimmed = context_manifest_path.strip()
    if not trimmed:
        raise OmnigentError(
            "context_manifest_path must not be empty",
            code=ErrorCode.INVALID_INPUT,
        )
    if trimmed.startswith("/"):
        raise OmnigentError(
            f"context_manifest_path {trimmed!r} must be repo-relative, not absolute",
            code=ErrorCode.INVALID_INPUT,
        )
    if "\\" in trimmed or re.match(r"^[A-Za-z]:", trimmed):
        raise OmnigentError(
            f"context_manifest_path {trimmed!r} must be repo-relative, not absolute",
            code=ErrorCode.INVALID_INPUT,
        )
    segments = trimmed.split("/")
    if ".." in segments:
        raise OmnigentError(
            f"context_manifest_path {trimmed!r} must not contain a '..' segment",
            code=ErrorCode.INVALID_INPUT,
        )
    if any(segment in ("", ".") for segment in segments):
        raise OmnigentError(
            f"context_manifest_path {trimmed!r} must not contain an empty or '.' segment",
            code=ErrorCode.INVALID_INPUT,
        )
    return trimmed


def _repository_to_response(repository: ProjectRepository) -> dict[str, Any]:
    """Convert a repository entity to a response dict.

    :param repository: The entity to convert.
    :returns: Dict with the repository fields.
    """
    return {
        "id": repository.id,
        "project_id": repository.project_id,
        "name": repository.name,
        "role": repository.role,
        "remote_url": repository.remote_url,
        "default_branch": repository.default_branch,
        "context_manifest_path": repository.context_manifest_path,
        "revision": repository.revision,
        "created_at": repository.created_at,
        "updated_at": repository.updated_at,
    }


def _binding_to_response(binding: ProjectHostBinding) -> dict[str, Any]:
    """Convert a binding entity to a response dict.

    :param binding: The entity to convert.
    :returns: Dict with the binding fields.
    """
    return {
        "id": binding.id,
        "project_id": binding.project_id,
        "host_id": binding.host_id,
        "name": binding.name,
        "is_primary": binding.is_primary,
        "repository_id": binding.repository_id,
        "workspace": binding.workspace,
        "enabled": binding.enabled,
        "revision": binding.revision,
        "path_verified_at": binding.path_verified_at,
        "created_at": binding.created_at,
        "updated_at": binding.updated_at,
    }


def _collaboration_problems(
    repositories: list[ProjectRepository],
    bindings: list[ProjectHostBinding],
) -> list[dict[str, Any]]:
    """Compute machine-readable config problems for a project.

    Flags a binding whose ``repository_id`` names no registered repository.
    A host without a primary is not a problem: it just has no code
    repository (or no enabled folder for it), which the web reads from roles.

    :param repositories: The project's registered repositories.
    :param bindings: The project's host bindings.
    :returns: Problem dicts with a stable ``code`` plus offending ids.
    """
    problems: list[dict[str, Any]] = []
    known_ids = {repository.id for repository in repositories}
    for binding in bindings:
        if binding.repository_id not in known_ids:
            problems.append(
                {
                    "code": "dangling_repository",
                    "binding_id": binding.id,
                    "host_id": binding.host_id,
                    "repository_id": binding.repository_id,
                }
            )
    return problems


def _is_managed_worktree_path(path: str) -> bool:
    """Whether *path* lies inside an Omnigent-managed worktree area.

    Assignment release and session cleanup remove those directories, so a
    project or repository folder there could later be matched by a removal
    that never intended it. Components compare case-insensitively: Windows
    paths are, and the removal that owns those directories resolves
    case-insensitively too.

    :param path: Absolute path to inspect.
    :returns: ``True`` for a ``.worktrees`` component or an
        ``.omnigent/worktrees`` pair, on either path separator, in any case.
    """
    components = [component.lower() for component in path.replace("\\", "/").split("/")]
    if ".worktrees" in components:
        return True
    return any(
        components[index] == ".omnigent" and components[index + 1] == "worktrees"
        for index in range(len(components) - 1)
    )


def _offline_workspace_path(workspace: str, *, kind: str) -> str:
    """Validate a path stored without a host round trip.

    The host cannot canonicalise while offline, so only what the server can
    check locally applies: the path must be absolute, must not name a
    managed worktree area, and keeps no trailing separators.

    :param workspace: The typed path.
    :param kind: ``"project"`` or ``"repository"``, for the error message.
    :returns: The trimmed path.
    :raises OmnigentError: ``INVALID_INPUT`` for a relative path or one
        inside a managed worktree area.
    """
    typed = workspace.strip()
    if not (typed.startswith("/") or _is_windows_absolute_path(typed)):
        raise OmnigentError(
            "workspace must be an absolute path starting with /",
            code=ErrorCode.INVALID_INPUT,
        )
    trimmed = typed.rstrip("/\\")
    # A filesystem root would trim to nothing (or a bare drive); keep it.
    if not trimmed or (len(trimmed) == 2 and trimmed[1] == ":"):
        trimmed = typed
    # Normalise a copy so dot and empty segments cannot hide a worktree
    # folder; the stored value stays exactly as typed. Backslashes are
    # separators to normpath too, so a mixed-separator UNC path collapses.
    normalised = posixpath.normpath(trimmed.replace("\\", "/"))
    if _is_managed_worktree_path(normalised):
        raise OmnigentError(
            f"a {kind} directory cannot be inside a worktree folder "
            "(.worktrees or .omnigent/worktrees)",
            code=ErrorCode.INVALID_INPUT,
        )
    return trimmed


def _record_setup_outcome(
    *,
    project_id: str,
    host_id: str,
    kind: str,
    name: str | None,
    result: dict[str, Any],
) -> None:
    """Remember the latest post-bind outcome for one project host target.

    Bindings and entries are keyed apart (``kind``) so a binding literally
    named ``"entry"`` cannot overwrite the project entry's outcome.

    :param project_id: The project the hook ran for.
    :param host_id: The host that ran the command.
    :param kind: ``"binding"`` or ``"entry"``.
    :param name: Binding name, or ``None`` for a project entry.
    :param result: The D9 object (``status``/``exit_code``/``output``/
        ``error``) the hook produced.
    """
    _SETUP_OUTCOMES[(project_id, host_id, kind, name)] = {
        "host_id": host_id,
        "kind": kind,
        "target": name if kind == "binding" else None,
        "status": result.get("status"),
        "exit_code": result.get("exit_code"),
        "output": result.get("output"),
        "error": result.get("error"),
        "at": datetime.now(UTC).isoformat(),
    }


def _setup_outcomes_for_project(project_id: str) -> list[dict[str, Any]]:
    """Return the current workspace's recorded outcomes for *project_id*.

    Sorted by ``(project, host, kind, name)`` so GET output is stable.
    Reading through the workspace-scoped cache keeps another tenant's
    outcomes invisible.

    :param project_id: The project whose outcomes to return.
    """
    return [
        value
        for key, value in sorted(_SETUP_OUTCOMES.items(), key=lambda item: item[0])
        if key[0] == project_id
    ]


async def _canonical_binding_workspace(
    *,
    user_id: str | None,
    host_id: str,
    workspace: str,
    host_store: Any | None,
    host_registry: Any | None,
) -> tuple[str, str | None]:
    """Authorize the host and canonicalise a binding path via ``host.stat``.

    The binding is host-level, never per-agent, so no agent boundary
    applies (``spec_cwd=None``). A genuinely offline host is a 409, while
    a bad path is a 400 like the session-create mapping.

    :param user_id: Authenticated caller, or ``None`` when auth is off.
    :param host_id: Target host id.
    :param workspace: Caller-supplied absolute path on the host.
    :param host_store: Persistent host registrations; ``None`` skips the
        ownership check (minimal test wirings).
    :param host_registry: Live host tunnels on this replica.
    :returns: The canonical workspace path plus the host display name.
    :raises OmnigentError: ``CONFLICT`` when the host is offline,
        ``INVALID_INPUT`` when the path fails validation.
    """
    if host_registry is None:
        raise OmnigentError(
            "host registry is not configured on this server",
            code=ErrorCode.INTERNAL_ERROR,
        )
    host_name = await _authorize_host_for_workspace(
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
        host_registry=host_registry,
    )
    if host_registry.get(host_id) is None:
        display = host_name or host_id
        raise OmnigentError(
            f"host '{display}' is offline; reconnect the host and try again",
            code=ErrorCode.CONFLICT,
        )
    try:
        canonical = await validate_workspace(
            host_registry=host_registry,
            host_id=host_id,
            workspace=workspace,
            spec_cwd=None,
            host_name_for_errors=host_name,
        )
    except WorkspaceValidationError as exc:
        raise OmnigentError(
            exc.message,
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    return canonical, host_name


async def _resolve_stored_workspace(
    *,
    user_id: str | None,
    host_id: str,
    workspace: str,
    kind: str,
    host_store: Any | None,
    host_registry: Any | None,
) -> tuple[str, str | None, bool]:
    """Authorize the host and resolve a path for storage.

    An online host canonicalises through ``host.stat`` (``checked=True``);
    an offline host stores the typed path after local string checks
    (``checked=False``), so a save survives a disconnected host.

    :param user_id: Authenticated caller, or ``None`` when auth is off.
    :param host_id: Target host id.
    :param workspace: Caller-supplied path on the host.
    :param kind: ``"project"`` or ``"repository"``, for error messages.
    :param host_store: Persistent host registrations; ``None`` skips the
        ownership check (minimal test wirings).
    :param host_registry: Live host tunnels on this replica.
    :returns: The path to store, the host display name, and whether the
        host checked it.
    :raises OmnigentError: ``INTERNAL_ERROR`` without a host registry,
        ``INVALID_INPUT`` when the path fails validation.
    """
    if host_registry is None:
        raise OmnigentError(
            "host registry is not configured on this server",
            code=ErrorCode.INTERNAL_ERROR,
        )
    host_name = await _authorize_host_for_workspace(
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
        host_registry=host_registry,
    )
    if host_registry.get(host_id) is None:
        return _offline_workspace_path(workspace, kind=kind), host_name, False
    try:
        canonical = await validate_workspace(
            host_registry=host_registry,
            host_id=host_id,
            workspace=workspace,
            spec_cwd=None,
            host_name_for_errors=host_name,
        )
    except WorkspaceValidationError as exc:
        raise OmnigentError(
            exc.message,
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    return canonical, host_name, True


def _post_bind_result(status: str, *, error: str | None = None) -> dict[str, Any]:
    """Build the ``post_bind`` response object for a non-frame outcome.

    :param status: One of the D9 statuses the server itself reports.
    :param error: Failure detail when the status is ``"failed"``.
    :returns: Dict with ``status``, ``exit_code``, ``output`` and ``error``.
    """
    return {"status": status, "exit_code": None, "output": None, "error": error}


async def run_post_bind_request(
    *,
    host_registry: HostRegistry,
    host_id: str,
    project_id: str,
    binding_name: str,
    binding_id: str,
    revision: int,
    repository_name: str,
    workspace: str,
    is_primary: bool,
    context_manifest_path: str,
    trigger: str,
) -> dict[str, Any]:
    """Ask the host to run its own post-bind command for one stored row.

    Never raises for hook outcomes: a missing tunnel, a host without the
    capability, a dropped connection and an expired wait all map to a
    status object the caller returns beside the row.

    :param host_registry: Live host tunnels on this replica.
    :param host_id: The host the command runs on.
    :param project_id: The project the row belongs to.
    :param binding_name: Binding name, or ``""`` for a project entry.
    :param binding_id: Stored binding row id, or ``"entry:<project_id>"``
        for a project entry.
    :param revision: Stored binding revision; entries carry 0.
    :param repository_name: Registered repository name, or ``""`` for an
        entry.
    :param workspace: Canonical directory the command runs in.
    :param is_primary: Whether the row is the host's primary.
    :param context_manifest_path: Repo-relative manifest path.
    :param trigger: ``"binding"`` or ``"entry"``; the host exports it as
        ``OMNIGENT_HOOK_TRIGGER``.
    :returns: The D9 object (``status``, ``exit_code``, ``output``,
        ``error``).
    """
    conn = host_registry.get(host_id)
    if conn is None:
        return _post_bind_result("unreachable")
    if not conn.hello.post_bind_hook:
        return _post_bind_result("unsupported")
    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
    conn.pending_post_bind_hooks[request_id] = future
    frame = encode_host_frame(
        HostPostBindHookFrame(
            request_id=request_id,
            project_id=project_id,
            binding_name=binding_name,
            binding_id=binding_id,
            revision=revision,
            repository_name=repository_name,
            workspace=workspace,
            is_primary=is_primary,
            context_manifest_path=context_manifest_path,
            trigger=trigger,
        )
    )
    try:
        try:
            host_registry.send_text(conn, frame)
        except ConnectionError:
            return _post_bind_result("unreachable")
        try:
            result = await asyncio.wait_for(future, timeout=_POST_BIND_HOOK_TIMEOUT_S)
        except asyncio.TimeoutError:
            return _post_bind_result("unreachable")
    finally:
        # The tunnel's receive loop also pops on success; this is the only
        # cleanup path when the caller is cancelled mid-wait.
        conn.pending_post_bind_hooks.pop(request_id, None)
    return {
        "status": result.get("status", "failed"),
        "exit_code": result.get("exit_code"),
        "output": result.get("output"),
        "error": result.get("error"),
    }


async def _run_post_bind_hook(
    *,
    host_registry: HostRegistry,
    host_id: str,
    binding: ProjectHostBinding,
    repository: ProjectRepository,
) -> dict[str, Any]:
    """Ask the host to run its post-bind command for a stored binding.

    :param host_registry: Live host tunnels on this replica.
    :param host_id: The bound host.
    :param binding: The stored binding the hook runs for; its ``revision``
        rides the frame so the host can drop a superseded request.
    :param repository: The registered repository the binding points at.
    :returns: The D9 object (``status``, ``exit_code``, ``output``,
        ``error``).
    """
    return await run_post_bind_request(
        host_registry=host_registry,
        host_id=host_id,
        project_id=binding.project_id,
        binding_name=binding.name,
        binding_id=binding.id,
        revision=binding.revision,
        repository_name=repository.name,
        workspace=binding.workspace,
        is_primary=binding.is_primary,
        context_manifest_path=repository.context_manifest_path,
        trigger="binding",
    )


async def _require_owned_project(
    project_store: ProjectStore,
    project_id: str,
    user_id: str | None,
) -> Any:
    """Return the caller's project, or 404 when absent / not owned.

    :param project_store: The store backing project persistence.
    :param project_id: The project to fetch.
    :param user_id: The requesting owner.
    :returns: The project entity.
    :raises OmnigentError: ``NOT_FOUND`` when not found / not owned.
    """
    project = await asyncio.to_thread(project_store.get, project_id, user_id=user_id)
    if project is None:
        raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
    return project


def create_project_collaboration_router(
    project_store: ProjectStore,
    repository_store: ProjectRepositoryStore,
    binding_store: ProjectHostBindingStore,
    auth_provider: AuthProvider | None = None,
    host_store: Any | None = None,
    host_registry: Any | None = None,
) -> APIRouter:
    """Build the project-collaboration router (under ``/v1/projects``).

    :param project_store: The store backing project persistence.
    :param repository_store: The store backing registered repositories.
    :param binding_store: The store backing per-host bindings.
    :param auth_provider: Auth provider used to identify the requesting user.
        ``None`` in single-user mode (owner scope is ``None``).
    :param host_store: Persistent host registrations for binding
        authorization; ``None`` skips the ownership check.
    :param host_registry: Live host tunnels, used to validate binding
        paths on the host.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.get("/projects/{project_id}/collaboration")
    async def get_collaboration(request: Request, project_id: str) -> dict[str, Any]:
        """Return the project's collaboration config plus validation status.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to inspect.
        :returns: ``{repositories, bindings, problems, setup_outcomes}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found /
            not owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        await _require_owned_project(project_store, project_id, user_id)
        repositories = await asyncio.to_thread(repository_store.list_by_project, project_id)
        bindings = await asyncio.to_thread(binding_store.list_by_project, project_id)
        outcomes = _setup_outcomes_for_project(project_id)
        return {
            "repositories": [_repository_to_response(r) for r in repositories],
            "bindings": [_binding_to_response(b) for b in bindings],
            "problems": _collaboration_problems(repositories, bindings),
            "setup_outcomes": outcomes,
        }

    @router.put("/projects/{project_id}/repositories/{name}")
    async def put_repository(
        request: Request,
        project_id: str,
        name: str,
        body: RepositoryPutRequest,
    ) -> dict[str, Any]:
        """Register a repository or revise its registration.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to register the repository on.
        :param name: Stable repository identity, unique per project.
        :param body: Remote, default branch, role and optional manifest path.
        :returns: The inserted or updated repository.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project
            is not found / not owned, 400 on a bad name, credentialed or
            query-carrying remote, bad role, or bad manifest path.
        """
        user_id = require_user(request, auth_provider)
        await _require_owned_project(project_store, project_id, user_id)
        _validate_ref_name(name, kind="repository")
        remote_url = _validate_remote_url(body.remote_url, name=name)
        default_branch = _validate_default_branch(body.default_branch)
        manifest_path = _validate_manifest_path(body.context_manifest_path)
        repository, _changed_bindings = await asyncio.to_thread(
            repository_store.apply_repository,
            project_id=project_id,
            name=name,
            remote_url=remote_url,
            default_branch=default_branch,
            role=body.role,
            context_manifest_path=manifest_path,
        )
        return _repository_to_response(repository)

    @router.delete("/projects/{project_id}/repositories/{name}")
    async def delete_repository(request: Request, project_id: str, name: str) -> dict[str, Any]:
        """Delete a registered repository.

        Refused while any binding of the project still references it.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project the repository is registered on.
        :param name: The repository name.
        :returns: ``{"id": ..., "object": "project_repository.deleted",
            "deleted": True}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project
            or repository is not found / not owned, 409 when a binding
            still references the repository.
        """
        user_id = require_user(request, auth_provider)
        await _require_owned_project(project_store, project_id, user_id)
        _validate_ref_name(name, kind="repository")
        repository = await asyncio.to_thread(
            repository_store.get_by_name, project_id=project_id, name=name
        )
        if repository is None:
            raise OmnigentError("Repository not found", code=ErrorCode.NOT_FOUND)
        # The store recounts references under the project lock, so a
        # binding created between this lookup and the delete still blocks.
        await asyncio.to_thread(repository_store.delete, repository.id)
        return {
            "id": repository.id,
            "object": "project_repository.deleted",
            "deleted": True,
        }

    @router.put("/projects/{project_id}/hosts/{host_id}/bindings/{name}")
    async def put_binding(
        request: Request,
        project_id: str,
        host_id: str,
        name: str,
        body: BindingPutRequest,
    ) -> dict[str, Any]:
        """Validate and store a host binding, deriving the host's primary.

        The typed path is validated live on the host; the canonical path
        the host returns is what gets stored, never the typed one. An
        offline host stores the typed path unchecked (``checked: false``)
        and skips the post-bind hook.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project the binding belongs to.
        :param host_id: The bound host.
        :param name: Binding name; ``primary`` is the conventional value.
        :param body: Workspace, repository name and enabled flag. The
            deprecated ``is_primary`` input is ignored.
        :returns: The inserted or updated binding plus ``checked`` and, for
            an online host, the ``post_bind`` outcome object.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project
            is not found / not owned, 400 on a bad name, unknown
            repository or bad path.
        """
        user_id = require_user(request, auth_provider)
        await _require_owned_project(project_store, project_id, user_id)
        _validate_ref_name(name, kind="binding")
        repository = await asyncio.to_thread(
            repository_store.get_by_name,
            project_id=project_id,
            name=body.repository_name,
        )
        if repository is None:
            raise OmnigentError(
                f"unknown repository {body.repository_name!r} on project {project_id}",
                code=ErrorCode.INVALID_INPUT,
            )
        workspace, _host_name, checked = await _resolve_stored_workspace(
            user_id=user_id,
            host_id=host_id,
            workspace=body.workspace,
            kind="repository",
            host_store=host_store,
            host_registry=host_registry,
        )
        binding = await asyncio.to_thread(
            binding_store.apply_binding,
            project_id=project_id,
            host_id=host_id,
            name=name,
            repository_id=repository.id,
            workspace=workspace,
            enabled=body.enabled,
            verified=checked,
        )
        response = {**_binding_to_response(binding), "checked": checked}
        if not checked:
            return response
        # The binding itself is the admission, so the hook runs for enabled
        # and disabled bindings alike and never refuses the stored row.
        assert host_registry is not None  # guaranteed by _resolve_stored_workspace
        post_bind = await _run_post_bind_hook(
            host_registry=host_registry,
            host_id=host_id,
            binding=binding,
            repository=repository,
        )
        _record_setup_outcome(
            project_id=project_id,
            host_id=host_id,
            kind="binding",
            name=name,
            result=post_bind,
        )
        response["post_bind"] = post_bind
        return response

    @router.delete("/projects/{project_id}/hosts/{host_id}/bindings/{name}")
    async def delete_binding(
        request: Request, project_id: str, host_id: str, name: str
    ) -> dict[str, Any]:
        """Delete a host binding.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project the binding belongs to.
        :param host_id: The bound host.
        :param name: The binding name.
        :returns: ``{"id": ..., "object": "project_host_binding.deleted",
            "deleted": True}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project
            or binding is not found / not owned.
        """
        user_id = require_user(request, auth_provider)
        await _require_owned_project(project_store, project_id, user_id)
        _validate_ref_name(name, kind="binding")
        binding = await asyncio.to_thread(
            binding_store.get_by_name,
            project_id=project_id,
            host_id=host_id,
            name=name,
        )
        if binding is None:
            raise OmnigentError("Binding not found", code=ErrorCode.NOT_FOUND)
        await asyncio.to_thread(binding_store.delete_binding, project_id, host_id, name)
        return {
            "id": binding.id,
            "object": "project_host_binding.deleted",
            "deleted": True,
        }

    @router.post("/projects/{project_id}/hosts/{host_id}/bindings/{name}/verify")
    async def verify_binding(
        request: Request, project_id: str, host_id: str, name: str
    ) -> dict[str, Any]:
        """Re-run host validation for the stored binding path.

        On success refreshes ``path_verified_at`` (and the stored workspace
        when the canonical path moved, bumping ``revision`` only then). On
        failure the row is left unchanged and the validation error is
        returned.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project the binding belongs to.
        :param host_id: The bound host.
        :param name: The binding name.
        :returns: The refreshed binding plus the ``post_bind`` outcome object.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project
            or binding is not found / not owned, 409 when the host is
            offline or the binding changed during verification, 400 when
            the stored path no longer validates.
        """
        user_id = require_user(request, auth_provider)
        await _require_owned_project(project_store, project_id, user_id)
        _validate_ref_name(name, kind="binding")
        binding = await asyncio.to_thread(
            binding_store.get_by_name,
            project_id=project_id,
            host_id=host_id,
            name=name,
        )
        if binding is None:
            raise OmnigentError("Binding not found", code=ErrorCode.NOT_FOUND)
        canonical, _host_name = await _canonical_binding_workspace(
            user_id=user_id,
            host_id=host_id,
            workspace=binding.workspace,
            host_store=host_store,
            host_registry=host_registry,
        )
        refreshed = await asyncio.to_thread(
            binding_store.record_verification,
            binding.id,
            expected_revision=binding.revision,
            workspace=canonical,
            path_verified_at=now_epoch(),
        )
        if refreshed is None:
            raise OmnigentError(
                "binding changed during verification; retry",
                code=ErrorCode.CONFLICT,
            )
        assert host_registry is not None  # guaranteed by _canonical_binding_workspace
        repository = await asyncio.to_thread(repository_store.get, refreshed.repository_id)
        if repository is None:
            # Store invariants make this unreachable; keep the response shape
            # and surface the corruption instead of inventing a frame.
            post_bind = _post_bind_result(
                "failed",
                error=f"repository {refreshed.repository_id!r} is not registered",
            )
        else:
            post_bind = await _run_post_bind_hook(
                host_registry=host_registry,
                host_id=host_id,
                binding=refreshed,
                repository=repository,
            )
        _record_setup_outcome(
            project_id=project_id,
            host_id=host_id,
            kind="binding",
            name=name,
            result=post_bind,
        )
        return {
            **_binding_to_response(refreshed),
            "checked": True,
            "post_bind": post_bind,
        }

    @router.get("/projects/{project_id}/hosts/{host_id}/agent-code-note")
    async def get_agent_code_note(
        request: Request, project_id: str, host_id: str
    ) -> dict[str, Any]:
        """Preview the code-location text the agent receives on one host.

        The text is built from the same locations and formatter the runner
        init snapshot uses, so the preview cannot drift from what a new
        session on that host is told. ``delivered`` is false when the host
        is offline or its build predates the capability; the text is built
        either way.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project whose repositories are described.
        :param host_id: The host whose folders are described.
        :returns: ``{object, text, delivered, reason}``.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project
            is not found / not owned by the caller.
        :raises HTTPException: 404 if the host is unknown; 403 if it is
            owned by a different user.
        """
        user_id = require_user(request, auth_provider)
        await _require_owned_project(project_store, project_id, user_id)
        # Ownership only: a stale online row with no tunnel here is offline,
        # not a wrong-replica error — the local registry decides delivery.
        if host_store is not None:
            await asyncio.to_thread(
                resolve_host_owner,
                user_id=user_id,
                host_id=host_id,
                host_store=host_store,
            )
        repositories = await asyncio.to_thread(repository_store.list_by_project, project_id)
        bindings = await asyncio.to_thread(binding_store.list_by_project, project_id)
        text = project_code_instruction(project_code_locations(repositories, bindings, host_id))
        conn = host_registry.get(host_id) if host_registry is not None else None
        if conn is None:
            delivered, reason = False, "host_offline"
        elif not conn.hello.project_code:
            delivered, reason = False, "host_update_needed"
        else:
            delivered, reason = True, None
        return {
            "object": "agent_code_note",
            "text": text,
            "delivered": delivered,
            "reason": reason,
        }

    return router
