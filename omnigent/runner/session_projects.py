"""List projects and update the owner's existing sessions via the session APIs."""

import asyncio
import json
from typing import Any

import httpx

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.session_requests import normalize_session_id
from omnigent.server.auth import LEVEL_OWNER
from omnigent.stores.conversation_store import PROJECT_LABEL_KEY
from omnigent.tools.builtins.session_projects import (
    MAX_LISTED_SESSIONS,
    MAX_PROJECT_NAME_LENGTH,
)

_DEFAULT_LISTED_SESSIONS = 50
_SNAPSHOT_PARAMS = {
    "include_items": "false",
    "include_liveness": "false",
    "include_live_status": "false",
    "include_usage": "false",
}
_CONCURRENT_NAME_READS = 8
_UPDATE_PREFLIGHT_TIMEOUT_S = 120.0
# A combined update can perform two 310s native Codex forwards with older runners.
# Leave room for the session lock and response work after those forwards.
_SETTINGS_PATCH_TIMEOUT_S = 720.0


async def resolve_project_id(
    client: httpx.AsyncClient, name: str, *, headers: dict[str, str] | None = None
) -> tuple[str, bool]:
    """
    Return the id of the caller's project named *name* and whether it was created.

    Matches the web picker: an exact name match wins, otherwise the project is
    created, and a create that loses a race to the same name (409) reuses the
    winner's project.

    :param client: HTTP client pointed at the Omnigent server.
    :param name: The project name, e.g. ``"nightly triage"``.
    :param headers: Extra headers for the create request, e.g. the orchestration source.
    :returns: ``(project_id, created)``.
    :raises ValueError: If the name is blank or the project can't be resolved.
    """
    trimmed = name.strip()
    if not trimmed:
        raise ValueError("project_name must not be empty")

    async def find() -> str | None:
        resp = await client.get("/v1/projects", timeout=30.0)
        if resp.status_code >= 400:
            raise ValueError(f"could not list projects: {resp.text[:200]}")
        projects = resp.json().get("data", [])
        return next((p["id"] for p in projects if p.get("name") == trimmed), None)

    existing = await find()
    if existing is not None:
        return existing, False
    created = await client.post(
        "/v1/projects", json={"name": trimmed}, headers=headers, timeout=30.0
    )
    if created.status_code < 400:
        return str(created.json()["id"]), True
    racer = await find() if created.status_code == 409 else None
    if racer is not None:
        return racer, False
    raise ValueError(f"could not create project {trimmed!r}: {created.text[:200]}")


def _same_session(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    try:
        return normalize_session_id(left) == normalize_session_id(right)
    except ValueError:
        return left == right


async def _project_names(
    client: httpx.AsyncClient, project_ids: set[str], known: dict[str, str]
) -> dict[str, str]:
    """Add the names of *project_ids* missing from *known*, reading each owned project."""
    names = dict(known)
    gate = asyncio.Semaphore(_CONCURRENT_NAME_READS)

    async def read(project_id: str) -> None:
        async with gate:
            response = await client.get(f"/v1/projects/{project_id}", timeout=30.0)
        if not response.is_error:
            names[project_id] = str(response.json().get("name") or project_id)

    await asyncio.gather(*(read(project_id) for project_id in project_ids - names.keys()))
    return names


def _project_name(session: dict[str, Any], names_by_id: dict[str, str]) -> str | None:
    project_id = session.get("project_id")
    if isinstance(project_id, str) and project_id:
        return names_by_id.get(project_id, project_id)
    label = (session.get("labels") or {}).get(PROJECT_LABEL_KEY)
    return label if isinstance(label, str) and label else None


def _http_error(tool_name: str, response: httpx.Response) -> str:
    return json.dumps(
        {
            "error": f"{tool_name} failed: HTTP {response.status_code}",
            "detail": response.text[:1000],
        }
    )


async def execute_project_list(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    """
    List a page of the caller's projects and a page of their sessions with each one's project.

    :param args: Tool arguments: optional ``project``, ``limit``, ``after`` and ``projects_after``.
    :param client: HTTP client pointed at the Omnigent server.
    :param source_id: The calling session, marked ``this_session`` in the output.
    :returns: JSON with ``projects``, ``projects_next_cursor``, ``sessions`` and ``next_cursor``.
    """
    if client is None:
        return json.dumps({"error": "sys_project_list requires server access"})
    project = args.get("project")
    if project is not None and (
        not isinstance(project, str) or len(project) > MAX_PROJECT_NAME_LENGTH
    ):
        return json.dumps({"error": "project must be a name of at most 100 characters"})
    limit = args.get("limit", _DEFAULT_LISTED_SESSIONS)
    if isinstance(limit, bool) or not isinstance(limit, int) or not limit >= 1:
        return json.dumps({"error": f"limit must be an integer from 1 to {MAX_LISTED_SESSIONS}"})
    limit = min(limit, MAX_LISTED_SESSIONS)
    cursors = {key: args.get(key) for key in ("after", "projects_after")}
    if any(value is not None and not isinstance(value, str) for value in cursors.values()):
        return json.dumps({"error": "after and projects_after must be cursor strings"})
    session_params: dict[str, str | int] = {
        "visibility": "mine",
        "kind": "default",
        "sort_by": "updated_at",
        "limit": limit,
    }
    if project is not None:
        session_params["project"] = project.strip()
    if cursors["after"]:
        session_params["after"] = cursors["after"]
    project_params: dict[str, str | int] = {"limit": limit}
    if cursors["projects_after"]:
        project_params["after"] = cursors["projects_after"]
    try:
        projects_response = await client.get("/v1/projects", params=project_params, timeout=30.0)
        if projects_response.is_error:
            return _http_error("sys_project_list", projects_response)
        sessions_response = await client.get("/v1/sessions", params=session_params, timeout=30.0)
        if sessions_response.is_error:
            return _http_error("sys_project_list", sessions_response)
        project_page = projects_response.json()
        projects = project_page.get("data", [])
        page = sessions_response.json()
        listed = page.get("data", [])
        names_by_id = await _project_names(
            client,
            {
                s["project_id"]
                for s in listed
                if isinstance(s.get("project_id"), str) and s["project_id"]
            },
            {p["id"]: p["name"] for p in projects if p.get("id")},
        )
    except httpx.HTTPError as exc:
        return json.dumps({"error": f"sys_project_list: server unreachable: {exc}"})
    sessions = []
    for session in listed:
        family = session.get("family") or {}
        entry: dict[str, Any] = {
            "session_id": session.get("id"),
            "title": session.get("title"),
            "status": session.get("status"),
            "project": _project_name(session, names_by_id),
        }
        representative = family.get("representative_id")
        if representative and representative != session.get("id"):
            entry["moves_with"] = representative
        if _same_session(session.get("id"), source_id):
            entry["this_session"] = True
        sessions.append(entry)
    has_more = bool(page.get("has_more"))
    projects_have_more = bool(project_page.get("has_more"))
    return json.dumps(
        {
            "projects": [{"name": p["name"], "id": p.get("id")} for p in projects],
            "projects_next_cursor": project_page.get("last_id") if projects_have_more else None,
            "sessions": sessions,
            "has_more": has_more,
            "next_cursor": page.get("last_id") if has_more else None,
        }
    )


def _refusal(response: httpx.Response) -> str:
    try:
        body = response.json()
        error = body.get("error") if isinstance(body, dict) else None
        detail = error.get("message") if isinstance(error, dict) else body.get("detail")
    except (ValueError, AttributeError):
        detail = None
    if not isinstance(detail, str) or not detail:
        detail = response.text[:300]
    if response.status_code == 404:
        return f"session not found or not visible to you ({detail})"
    if response.status_code == 409 and "representative" in detail:
        return "this session shares its family's project; update the moves_with session instead"
    return f"HTTP {response.status_code}: {detail}"


async def _update_request(
    client: httpx.AsyncClient, method: str, path: str, *, wait_s: float, **kwargs: Any
) -> httpx.Response:
    """Apply a hard deadline as well as HTTPX's per-operation timeouts."""
    io_timeout_s = 60.0 if method == "PATCH" else 30.0
    try:
        async with asyncio.timeout(wait_s):
            return await client.request(
                method, path, timeout=httpx.Timeout(io_timeout_s, read=wait_s), **kwargs
            )
    except TimeoutError as exc:
        raise httpx.ReadTimeout(f"no complete response within {wait_s:g}s") from exc


async def _update_snapshot(
    client: httpx.AsyncClient, session_id: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Read the target fields needed for update validation."""
    try:
        snapshot = await _update_request(
            client,
            "GET",
            f"/v1/sessions/{session_id}",
            params=_SNAPSHOT_PARAMS,
            wait_s=_UPDATE_PREFLIGHT_TIMEOUT_S,
        )
        if snapshot.is_error:
            return None, _refusal(snapshot)
        current = snapshot.json()
        if not isinstance(current, dict):
            raise ValueError("expected a session object")
    except (httpx.HTTPError, ValueError) as exc:
        return None, f"session could not be read: {str(exc) or type(exc).__name__}"
    return current, None


def _move_refusal(current: dict[str, Any]) -> str | None:
    parent = current.get("parent_session_id")
    if parent:
        return f"sub-agent sessions follow their parent's project; update {parent} instead"
    representative = (current.get("family") or {}).get("representative_id")
    if representative and representative != current.get("id"):
        return f"this session shares its family's project; update {representative} instead"
    return None


async def _set_project(
    client: httpx.AsyncClient,
    current: dict[str, Any],
    session_id: str,
    *,
    project_id: str,
    headers: dict[str, str],
) -> tuple[str, str | None]:
    """Return ``(status, error)`` after filing the session into *project_id*."""
    labels = current.get("labels") or {}
    if (current.get("project_id") or "") == project_id and not labels.get(PROJECT_LABEL_KEY):
        return "unchanged", None
    payload = {"project_id": project_id, "labels": {PROJECT_LABEL_KEY: ""}}
    try:
        updated = await client.patch(
            f"/v1/sessions/{session_id}", json=payload, headers=headers, timeout=30.0
        )
    except httpx.HTTPError as exc:
        return "error", f"update could not be confirmed: {exc}"
    if updated.is_error:
        return "error", _refusal(updated)
    return "updated", None


async def _source_check_refusal(client: httpx.AsyncClient, *, settings: bool) -> str | None:
    """Require a positive server capability before any update or project creation."""
    probe = "sys_session_update: GET /v1/info"
    try:
        info = await _update_request(client, "GET", "/v1/info", wait_s=_UPDATE_PREFLIGHT_TIMEOUT_S)
    except httpx.HTTPError as exc:
        return f"{probe} failed: {str(exc) or type(exc).__name__}"
    if info.status_code != 200:
        return f"{probe} returned HTTP {info.status_code} while checking source sharing support"
    try:
        body = info.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return f"{probe} returned an invalid capability response"
    flags = (
        ("session_settings_source_checks", "session_organize_source_checks")
        if settings
        else ("session_move_source_checks",)
    )
    if any(body.get(flag) is True for flag in flags):
        return None
    return (
        "sys_session_update: this Omnigent server cannot check the calling "
        "session's sharing before this update; upgrade the server or use the Web controls"
    )


async def execute_session_update(
    args: dict[str, Any], client: httpx.AsyncClient | None, source_id: str | None
) -> str:
    """
    Update one of the owner's sessions through the Web settings API.

    Filing is the sidebar's ``PATCH /v1/sessions/{id}``, which only changes
    folder membership, so a running session keeps its turn. The calling session
    is sent as the orchestration source on every write, so the server refuses
    both the update and any project creation from a session shared with edit
    access. A project is created only after the session is known to be movable.

    :param args: Session ID and optional project, model and reasoning effort.
    :param client: HTTP client pointed at the Omnigent server.
    :param source_id: The calling session.
    :returns: JSON with ``session_id``, ``status``, ``project`` and ``project_created``,
        or ``session_id`` and ``error``.
    """
    session_id = args.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return json.dumps({"error": "session_id must be a session ID"})
    settings = {key: args[key] for key in ("model", "reasoning_effort") if key in args}
    if "project" not in args and not settings:
        return json.dumps(
            {
                "session_id": session_id,
                "error": "nothing to update: pass project, model or reasoning_effort",
            }
        )
    if "model" in settings and (
        not isinstance(settings["model"], str) or not settings["model"].strip()
    ):
        return json.dumps({"session_id": session_id, "error": "model must be a non-empty string"})
    effort = settings.get("reasoning_effort")
    if effort is not None and not isinstance(effort, str):
        return json.dumps(
            {"session_id": session_id, "error": "reasoning_effort must be a string or null"}
        )
    project = args.get("project", "")
    # Only an exact "" unfiles; a blank-looking name must not unfile the session.
    if (
        not isinstance(project, str)
        or len(project) > MAX_PROJECT_NAME_LENGTH
        or (project and not project.strip())
    ):
        return json.dumps(
            {
                "session_id": session_id,
                "error": 'project must be a name of at most 100 characters, or "" to unfile',
            }
        )
    try:
        canonical = normalize_session_id(session_id)
    except ValueError:
        return json.dumps({"session_id": session_id, "error": "invalid session ID"})
    if client is None or source_id is None:
        return json.dumps({"error": "sys_session_update requires an authenticated runner"})
    if (error := await _source_check_refusal(client, settings=bool(settings))) is not None:
        return json.dumps({"error": error})
    current, error = await _update_snapshot(client, canonical)
    if current is None:
        return json.dumps({"session_id": session_id, "error": error})
    level = current.get("permission_level")
    if isinstance(level, int) and level < LEVEL_OWNER:
        field = "settings" if settings else "project"
        return json.dumps(
            {"session_id": session_id, "error": f"only the session's owner can change its {field}"}
        )
    if "project" in args and (error := _move_refusal(current)) is not None:
        return json.dumps({"session_id": session_id, "error": error})
    headers = {ORCHESTRATION_SOURCE_HEADER: source_id}
    name = project.strip()
    project_id, created = "", False
    if name:
        try:
            project_id, created = await resolve_project_id(client, name, headers=headers)
        except (httpx.HTTPError, ValueError) as exc:
            return json.dumps(
                {
                    "session_id": session_id,
                    "error": f"project could not be resolved: {str(exc) or type(exc).__name__}",
                }
            )
    if settings:
        payload = {
            "model_override" if key == "model" else key: value for key, value in settings.items()
        }
        project_result = {}
        if "project" in args:
            payload.update({"project_id": project_id, "labels": {PROJECT_LABEL_KEY: ""}})
            project_result = {"project": name or None, "project_created": created}
        path = f"/v1/sessions/{canonical}"
        try:
            updated = await _update_request(
                client,
                "PATCH",
                path,
                json=payload,
                headers=headers,
                params={"include_usage": "false"},
                wait_s=_SETTINGS_PATCH_TIMEOUT_S,
            )
            if updated.is_error:
                return json.dumps({"session_id": session_id, "error": _refusal(updated)})
            applied = updated.json()
            if not isinstance(applied, dict):
                raise ValueError("expected a session object")
        except (httpx.HTTPError, ValueError) as exc:
            detail = str(exc) or type(exc).__name__
            return json.dumps(
                {
                    "session_id": session_id,
                    "error": f"update could not be confirmed: PATCH {path}: {detail}",
                }
            )
        return json.dumps(
            {
                "session_id": session_id,
                "status": "updated",
                **project_result,
                **{
                    key: applied.get("model_override" if key == "model" else key)
                    for key in settings
                },
            }
        )
    status, error = await _set_project(
        client, current, canonical, project_id=project_id, headers=headers
    )
    if error is not None:
        return json.dumps({"session_id": session_id, "error": error})
    return json.dumps(
        {
            "session_id": session_id,
            "status": status,
            "project": name or None,
            "project_created": created,
        }
    )
