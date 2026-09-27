"""Cross-host member placement: the lead's branch worktree on the member host.

Covers ``resolve_member_worktree_on_host`` (SCC06 F2b Step A) and its
owner-checked route ``GET /v1/sessions/{id}/member-worktree``: every missing
fact (no project, no directory on the host, no recorded branch, no matching
worktree, unreachable host) is a named 4xx so the member dispatch fails loud
instead of running somewhere else.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes import _member_placement
from omnigent.server.routes import sessions as sessions_module
from omnigent.server.routes._host_worktree import (
    WorktreeHostUnavailableError,
    WorktreeProxyError,
)
from omnigent.server.routes._member_placement import resolve_member_worktree_on_host
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_LEAD_ID = "conv_lead_cross_host"
_PROJECT_ID = "0123456789abcdef0123456789abcdef"
_HOST_B = "host_b"
_REPO = "/host-b/repo"
_WORKTREE = "/host-b/repo/.worktrees/feature-x"
_BRANCH = "feature/x"


class _FakeProjectStore:
    """A project store whose ``get`` returns one fixed project or nothing."""

    def __init__(self, project: Any | None) -> None:
        self._project = project
        self.asked: list[tuple[str, str | None]] = []

    def get(self, project_id: str, *, user_id: str | None = None) -> Any | None:
        self.asked.append((project_id, user_id))
        if self._project is not None and self._project.id == project_id:
            return self._project
        return None


class _FakeBindingStore:
    """A project host-binding store with no bindings and no entries."""

    def list_by_project(self, project_id: str) -> list[Any]:
        return []

    def list_entries(self, project_id: str) -> list[Any]:
        return []


class _FakeHostRegistry:
    """A host registry whose one host is connected (or not)."""

    def __init__(self, connected: bool = True) -> None:
        self._connected = connected

    def get(self, host_id: str) -> Any | None:
        return SimpleNamespace(host_id=host_id) if self._connected else None


def _project(*, name: str = "lead-project", host_id: str = _HOST_B, workspace: str = _REPO) -> Any:
    """Build a project row stub with one config host root."""
    return SimpleNamespace(
        id=_PROJECT_ID,
        name=name,
        config={"host_id": host_id, "workspace": workspace},
        collaboration_enabled=False,
    )


def _conversation(
    *,
    project_id: str | None = _PROJECT_ID,
    git_branch: str | None = _BRANCH,
) -> Any:
    """Build a lead conversation stub with the placement fields."""
    return SimpleNamespace(
        id=_LEAD_ID,
        project_id=project_id,
        git_branch=git_branch,
        workspace=_REPO + "/.worktrees/feature-x",
    )


async def _resolve(
    *,
    conversation: Any,
    project: Any | None,
    host_connected: bool = True,
    worktrees: list[dict[str, Any]] | None = None,
    worktree_error: Exception | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Call the resolver against fakes, with the host listing stubbed."""

    async def _fake_list(
        *, host_registry: Any, host_conn: Any, repo_path: str, branch: str
    ) -> Any:
        if worktree_error is not None:
            raise worktree_error
        rows = worktrees if worktrees is not None else [{"branch": _BRANCH, "path": _WORKTREE}]
        match = next((str(w["path"]) for w in rows if w.get("branch") == branch), None)
        return rows, match

    monkeypatch.setattr(_member_placement, "list_worktrees_and_match_branch", _fake_list)
    return await resolve_member_worktree_on_host(
        conversation=conversation,
        host_id=_HOST_B,
        user_id="alice@example.com",
        project_store=_FakeProjectStore(project),
        binding_store=_FakeBindingStore(),
        host_registry=_FakeHostRegistry(host_connected),
        feature_flags=None,
    )


@pytest.mark.asyncio
async def test_resolves_the_branch_worktree(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lead's branch worktree on the member host is the member workspace."""
    resolved = await _resolve(
        conversation=_conversation(), project=_project(), monkeypatch=monkeypatch
    )

    assert resolved.workspace == _WORKTREE
    assert resolved.repository == _REPO
    assert resolved.branch == _BRANCH


@pytest.mark.asyncio
async def test_no_project_is_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lead session without a project cannot map its repository to host B."""
    with pytest.raises(OmnigentError) as excinfo:
        await _resolve(
            conversation=_conversation(project_id=None),
            project=None,
            monkeypatch=monkeypatch,
        )

    assert "has no project" in excinfo.value.message
    assert _HOST_B in excinfo.value.message
    assert excinfo.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_missing_project_row_is_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A project id that no longer resolves is refused, not silently skipped."""
    with pytest.raises(OmnigentError) as excinfo:
        await _resolve(conversation=_conversation(), project=None, monkeypatch=monkeypatch)

    assert "was not found" in excinfo.value.message
    assert excinfo.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_no_recorded_branch_is_a_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A lead session with no recorded branch cannot place a remote member."""
    with pytest.raises(OmnigentError) as excinfo:
        await _resolve(
            conversation=_conversation(git_branch=None),
            project=_project(),
            monkeypatch=monkeypatch,
        )

    assert "no recorded git branch" in excinfo.value.message
    assert excinfo.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_no_project_directory_on_the_host_is_a_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The project must have a directory on the member's host."""
    with pytest.raises(OmnigentError) as excinfo:
        await _resolve(
            conversation=_conversation(),
            project=_project(host_id="host_other"),
            monkeypatch=monkeypatch,
        )

    assert "no directory on host" in excinfo.value.message
    assert excinfo.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.asyncio
async def test_disconnected_host_is_a_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconnected member host is a 409, not a worktree miss."""
    with pytest.raises(OmnigentError) as excinfo:
        await _resolve(
            conversation=_conversation(),
            project=_project(),
            host_connected=False,
            monkeypatch=monkeypatch,
        )

    assert "not connected" in excinfo.value.message
    assert excinfo.value.code == ErrorCode.CONFLICT


@pytest.mark.asyncio
async def test_branch_absent_from_host_worktrees_is_a_named_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host whose worktrees do not check out the lead's branch refuses."""
    with pytest.raises(OmnigentError) as excinfo:
        await _resolve(
            conversation=_conversation(),
            project=_project(),
            worktrees=[{"branch": "other", "path": "/host-b/repo/.worktrees/other"}],
            monkeypatch=monkeypatch,
        )

    assert "is not checked out in a worktree" in excinfo.value.message
    assert _BRANCH in excinfo.value.message


@pytest.mark.asyncio
async def test_worktree_proxy_failure_maps_to_400(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host-reported listing failure is user-correctable input."""
    with pytest.raises(OmnigentError) as excinfo:
        await _resolve(
            conversation=_conversation(),
            project=_project(),
            worktree_error=WorktreeProxyError("worktree listing failed: not a git repository"),
            monkeypatch=monkeypatch,
        )

    assert excinfo.value.code == ErrorCode.INVALID_INPUT
    assert "not a git repository" in excinfo.value.message


@pytest.mark.asyncio
async def test_worktree_host_unavailable_maps_to_409(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host that stopped answering mid-listing is a 409."""
    with pytest.raises(OmnigentError) as excinfo:
        await _resolve(
            conversation=_conversation(),
            project=_project(),
            worktree_error=WorktreeHostUnavailableError("host 'host_b' connection lost"),
            monkeypatch=monkeypatch,
        )

    assert excinfo.value.code == ErrorCode.CONFLICT


@pytest.fixture
async def member_worktree_app(db_uri: str, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A sessions app with the member-worktree route and fake placement stores."""
    store = SqlAlchemyConversationStore(db_uri)
    lead = store.create_conversation(project_id=_PROJECT_ID, git_branch=_BRANCH)

    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        sessions_module.create_sessions_router(
            store,
            SqlAlchemyAgentStore(db_uri),
            host_registry=_FakeHostRegistry(),
            project_store=_FakeProjectStore(_project()),
        ),
        prefix="/v1",
    )
    app.state.project_host_binding_store = _FakeBindingStore()

    async def _fake_list(
        *, host_registry: Any, host_conn: Any, repo_path: str, branch: str
    ) -> Any:
        rows = [{"branch": _BRANCH, "path": _WORKTREE}]
        return rows, _WORKTREE

    monkeypatch.setattr(_member_placement, "list_worktrees_and_match_branch", _fake_list)
    return app, lead.id


async def _get_member_worktree(app: Any, session_id: str, host_id: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(
            f"/v1/sessions/{session_id}/member-worktree", params={"host_id": host_id}
        )


@pytest.mark.asyncio
async def test_route_returns_the_resolved_worktree(
    member_worktree_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner-checked route serves the resolved worktree for the runner."""
    app, session_id = member_worktree_app
    response = await _get_member_worktree(app, session_id, _HOST_B)

    assert response.status_code == 200, response.text
    assert response.json() == {
        "session_id": session_id,
        "host_id": _HOST_B,
        "workspace": _WORKTREE,
        "repository": _REPO,
        "branch": _BRANCH,
    }


@pytest.mark.asyncio
async def test_route_reports_a_missing_worktree(
    member_worktree_app: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A branch absent from the host's worktrees is a 400 naming it."""
    app, session_id = member_worktree_app

    async def _no_match(*, host_registry: Any, host_conn: Any, repo_path: str, branch: str) -> Any:
        return [{"branch": "other", "path": "/host-b/repo/.worktrees/other"}], None

    monkeypatch.setattr(_member_placement, "list_worktrees_and_match_branch", _no_match)
    response = await _get_member_worktree(app, session_id, _HOST_B)

    assert response.status_code == 400, response.text
    assert "not checked out in a worktree" in response.json()["error"]["message"]


@pytest.mark.asyncio
async def test_route_unknown_session_is_404(member_worktree_app: Any) -> None:
    """An unknown session id is a 404, consistent with the other session routes."""
    app, _session_id = member_worktree_app
    response = await _get_member_worktree(app, f"conv_{'0' * 32}", _HOST_B)

    assert response.status_code == 404, response.text
