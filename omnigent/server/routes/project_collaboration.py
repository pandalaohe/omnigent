"""REST API routes for project collaboration configuration.

Covers the opt-in surface of cross-host project collaboration: the
per-project enable switch, the registered repositories, and the per-host
directory bindings.
Assignment dispatch and lifecycle live on a separate router; this module
only configures where assignments may run.

Every route requires the caller to own the project and the deployment to
opt into ``Feature.PROJECT_ASSIGNMENTS``. A disabled flag makes each
route 404, so the surface stays dark until enabled.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from omnigent.db.utils import now_epoch
from omnigent.entities import ProjectHostBinding, ProjectRepository
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostPostBindHookFrame, encode_host_frame
from omnigent.server.auth import AuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags, resolve_feature_flags
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._session_create_validation import (
    _authorize_host_for_workspace,
)
from omnigent.server.routes._workspace_validation import (
    WorkspaceValidationError,
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


class CollaborationPatchRequest(BaseModel):
    """Request body for ``PATCH /v1/projects/{project_id}/collaboration``.

    :param enabled: The new collaboration switch value.
    :param expected_revision: The ``collaboration_revision`` the caller read;
        a mismatch means another writer moved first.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    expected_revision: int


class RepositoryPutRequest(BaseModel):
    """Request body for ``PUT /v1/projects/{project_id}/repositories/{name}``.

    :param remote_url: The shared remote. Must be non-empty and carry no
        credentials.
    :param default_branch: The repository's default branch name.
    :param context_manifest_path: Repo-relative manifest path. Defaults to
        ``.agents/project/manifest.json`` when omitted.
    """

    model_config = ConfigDict(extra="forbid")

    remote_url: str
    default_branch: str
    context_manifest_path: str | None = None


class BindingPutRequest(BaseModel):
    """Request body for ``PUT /v1/projects/{project_id}/hosts/{host_id}/bindings/{name}``.

    :param workspace: Absolute path on the host. Validated live via
        ``host.stat``; the canonical path the host returns is stored.
    :param repository_name: Name of a repository registered on this project.
    :param is_primary: Whether this is the host's primary binding.
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
    """Validate a repository remote: present and credential-free.

    ``http(s)`` remotes must carry no userinfo at all (a bare
    access token in the username position is still a credential);
    any other scheme, or an scp-like ``user@host:path`` remote,
    allows a username but never a password.

    :param remote_url: The raw remote from the request.
    :param name: The repository name, for error messages.
    :returns: The trimmed remote.
    :raises OmnigentError: ``INVALID_INPUT`` when the remote is missing or
        carries userinfo credentials (which must never be stored).
    """
    trimmed = (remote_url or "").strip()
    if not trimmed:
        raise OmnigentError(
            f"repository {name!r} has no remote_url: "
            "a repository without a remote cannot join a collaboration project",
            code=ErrorCode.INVALID_INPUT,
        )
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

    Flags an enabled host (≥1 enabled binding) with no primary binding,
    and a binding whose ``repository_id`` names no registered repository.

    :param repositories: The project's registered repositories.
    :param bindings: The project's host bindings.
    :returns: Problem dicts with a stable ``code`` plus offending ids.
    """
    problems: list[dict[str, Any]] = []
    known_ids = {repository.id for repository in repositories}
    by_host: dict[str, list[ProjectHostBinding]] = {}
    for binding in bindings:
        by_host.setdefault(binding.host_id, []).append(binding)
    for host_id, host_bindings in by_host.items():
        if not any(binding.enabled for binding in host_bindings):
            continue
        if not any(binding.is_primary for binding in host_bindings):
            problems.append({"code": "missing_primary", "host_id": host_id})
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


def _post_bind_result(status: str, *, error: str | None = None) -> dict[str, Any]:
    """Build the ``post_bind`` response object for a non-frame outcome.

    :param status: One of the D9 statuses the server itself reports.
    :param error: Failure detail when the status is ``"failed"``.
    :returns: Dict with ``status``, ``exit_code``, ``output`` and ``error``.
    """
    return {"status": status, "exit_code": None, "output": None, "error": error}


async def _run_post_bind_hook(
    *,
    host_registry: HostRegistry,
    host_id: str,
    binding: ProjectHostBinding,
    repository: ProjectRepository,
) -> dict[str, Any]:
    """Ask the host to run its own post-bind command for a stored binding.

    Never raises for hook outcomes: a missing tunnel, a host without the
    capability, a dropped connection and an expired wait all map to a
    status object the caller returns beside the binding.

    :param host_registry: Live host tunnels on this replica.
    :param host_id: The bound host.
    :param binding: The stored binding the hook runs for; its ``revision``
        rides the frame so the host can drop a superseded request.
    :param repository: The registered repository the binding points at.
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
            project_id=binding.project_id,
            binding_name=binding.name,
            binding_id=binding.id,
            revision=binding.revision,
            repository_name=repository.name,
            workspace=binding.workspace,
            is_primary=binding.is_primary,
            context_manifest_path=repository.context_manifest_path,
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
    feature_flags: FeatureFlags | None = None,
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
    :param feature_flags: Immutable deployment release-feature snapshot.
        When omitted, resolves ``OMNIGENT_FEATURES`` at router construction.
    :returns: A configured :class:`APIRouter`.
    """
    flags = feature_flags or resolve_feature_flags()

    def _require_flag() -> None:
        # A disabled route is indistinguishable from a non-existent one, so
        # the configuration surface stays dark until the deployment opts in.
        # Router-level, so a disabled flag 404s before body validation 422s.
        if not flags.enabled(Feature.PROJECT_ASSIGNMENTS):
            raise HTTPException(status_code=404, detail="not found")

    router = APIRouter(dependencies=[Depends(_require_flag)])

    @router.get("/projects/{project_id}/collaboration")
    async def get_collaboration(request: Request, project_id: str) -> dict[str, Any]:
        """Return the project's collaboration config plus validation status.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to inspect.
        :returns: ``{enabled, revision, repositories, bindings, problems}``.
        :raises HTTPException: 404 when the feature is disabled.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found /
            not owned by the caller.
        """
        user_id = require_user(request, auth_provider)
        project = await _require_owned_project(project_store, project_id, user_id)
        repositories = await asyncio.to_thread(repository_store.list_by_project, project_id)
        bindings = await asyncio.to_thread(binding_store.list_by_project, project_id)
        return {
            "enabled": project.collaboration_enabled,
            "revision": project.collaboration_revision,
            "repositories": [_repository_to_response(r) for r in repositories],
            "bindings": [_binding_to_response(b) for b in bindings],
            "problems": _collaboration_problems(repositories, bindings),
        }

    @router.patch("/projects/{project_id}/collaboration")
    async def set_collaboration(
        request: Request,
        project_id: str,
        body: CollaborationPatchRequest,
    ) -> dict[str, Any]:
        """Enable or disable collaboration, with optimistic-concurrency guard.

        The only route that bumps ``collaboration_revision``; repository and
        binding changes carry their own ``revision`` and leave it alone.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project to update.
        :param body: The new switch value plus the revision the caller read.
        :returns: ``{enabled, revision}`` of the updated project.
        :raises HTTPException: 404 when the feature is disabled.
        :raises OmnigentError: 401 if unauthenticated, 404 if not found /
            not owned, 409 on a stale ``expected_revision``.
        """
        user_id = require_user(request, auth_provider)
        project = await asyncio.to_thread(
            project_store.set_collaboration,
            project_id,
            user_id=user_id,
            enabled=body.enabled,
            expected_revision=body.expected_revision,
        )
        if project is None:
            raise OmnigentError("Project not found", code=ErrorCode.NOT_FOUND)
        return {
            "enabled": project.collaboration_enabled,
            "revision": project.collaboration_revision,
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
        :param body: Remote, default branch and optional manifest path.
        :returns: The inserted or updated repository.
        :raises HTTPException: 404 when the feature is disabled.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project
            is not found / not owned, 400 on a bad name, missing or
            credentialed remote, or bad manifest path.
        """
        user_id = require_user(request, auth_provider)
        await _require_owned_project(project_store, project_id, user_id)
        _validate_ref_name(name, kind="repository")
        remote_url = _validate_remote_url(body.remote_url, name=name)
        default_branch = _validate_default_branch(body.default_branch)
        manifest_path = _validate_manifest_path(body.context_manifest_path)
        repository = await asyncio.to_thread(
            repository_store.upsert,
            project_id=project_id,
            name=name,
            remote_url=remote_url,
            default_branch=default_branch,
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
        :raises HTTPException: 404 when the feature is disabled.
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
        """Validate and store a host binding, stamping ``path_verified_at``.

        The typed path is validated live on the host; the canonical path
        the host returns is what gets stored, never the typed one.

        :param request: The incoming request, used to identify the user.
        :param project_id: The project the binding belongs to.
        :param host_id: The bound host.
        :param name: Binding name; ``primary`` is the conventional value.
        :param body: Workspace, repository name and binding flags.
        :returns: The inserted or updated binding plus the ``post_bind``
            outcome object.
        :raises HTTPException: 404 when the feature is disabled.
        :raises OmnigentError: 401 if unauthenticated, 404 if the project
            is not found / not owned, 400 on a bad name, unknown
            repository or bad path, 409 when the host is offline or a
            second primary is requested for the host.
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
        canonical, _host_name = await _canonical_binding_workspace(
            user_id=user_id,
            host_id=host_id,
            workspace=body.workspace,
            host_store=host_store,
            host_registry=host_registry,
        )
        binding = await asyncio.to_thread(
            binding_store.upsert,
            project_id=project_id,
            host_id=host_id,
            name=name,
            repository_id=repository.id,
            workspace=canonical,
            is_primary=body.is_primary,
            enabled=body.enabled,
            path_verified_at=now_epoch(),
        )
        # The binding itself is the admission, so the hook runs for enabled
        # and disabled bindings alike and never refuses the stored row.
        assert host_registry is not None  # guaranteed by _canonical_binding_workspace
        post_bind = await _run_post_bind_hook(
            host_registry=host_registry,
            host_id=host_id,
            binding=binding,
            repository=repository,
        )
        return {**_binding_to_response(binding), "post_bind": post_bind}

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
        :raises HTTPException: 404 when the feature is disabled.
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
        await asyncio.to_thread(binding_store.delete, binding.id)
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
        :raises HTTPException: 404 when the feature is disabled.
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
        return {**_binding_to_response(refreshed), "post_bind": post_bind}

    return router
