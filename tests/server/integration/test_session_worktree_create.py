"""
Integration tests for git worktree creation on ``POST /v1/sessions``.

Drives the JSON create path with a `git` block through the full app and
a fake host that auto-replies to the worktree control frames. Verifies
that the request's branch_name + base_branch reach the host's
``host.create_worktree`` frame, and that the created worktree path and
branch are persisted on the session. See designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.entities import ProjectHostBinding, ProjectHostEntry
from omnigent.host.frames import (
    HostCreateWorktreeFrame,
    HostHelloFrame,
    HostListWorktreesFrame,
    HostRemoveWorktreeFrame,
    HostStatFrame,
    decode_host_frame,
)
from omnigent.host.git_worktree import _resolve_worktree_path
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.host_registry import HostConnection
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_repository_store.sqlalchemy_store import (
    SqlAlchemyProjectRepositoryStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio

_HOST_ID = "2b8753b34a61b09af35a01136d40fadf"
_SOURCE_REPO = "/Users/alice/myrepo"
_ENTRY = "/Users/alice/project"
_PROJECT_ID = "aa11bb22cc33dd44ee55ff6600112233"


class _ProjectDirs:
    """In-memory project entries and bindings for the create route.

    ``entries_by_project`` scopes entries to one project; without it,
    ``entries`` applies to every project (single-project tests).
    """

    def __init__(
        self,
        *,
        entries: list[tuple[str, str]] = (),
        bindings: list[tuple[str, str]] = (),
        entries_by_project: dict[str, list[tuple[str, str]]] | None = None,
    ) -> None:
        self._entries = list(entries)
        self._bindings = list(bindings)
        self._entries_by_project = entries_by_project

    def list_entries(self, project_id: str) -> list[ProjectHostEntry]:
        """Return the project's per-host entries."""
        rows = (
            self._entries
            if self._entries_by_project is None
            else self._entries_by_project.get(project_id, [])
        )
        return [ProjectHostEntry(project_id, host_id, workspace, 1) for host_id, workspace in rows]

    def list_by_project(self, project_id: str) -> list[ProjectHostBinding]:
        """Return the project's primary bindings."""
        return [
            ProjectHostBinding(
                f"{project_id}-{host_id}",
                project_id,
                host_id,
                "primary",
                "repo",
                workspace,
                1,
                1,
                is_primary=True,
            )
            for host_id, workspace in self._bindings
        ]


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """The shared app plus a project store, so a create can resolve a project entry.

    The shared ``client`` fixture depends on this ``app``.
    """
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        comment_store=SqlAlchemyCommentStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
    )


class _FakeWebSocket:
    """Minimal WebSocket stand-in (the registry only enqueues)."""

    async def send_text(self, data: str) -> None:
        """No-op send — frames flow through the outbound queue.

        :param data: JSON-encoded frame text (ignored).
        """


@dataclass
class _HostCapture:
    """
    Frames a fake host received during one ``POST /v1/sessions`` create.

    :param create: ``host.create_worktree`` frames received.
    :param remove: ``host.remove_worktree`` frames received (a non-empty
        list proves the create-rollback path fired).
    """

    create: list[HostCreateWorktreeFrame] = field(default_factory=list)
    remove: list[HostRemoveWorktreeFrame] = field(default_factory=list)


# Factory yielded by the ``register_worktree_host`` fixture:
# register(*, create_status=, create_error=) -> _HostCapture.
RegisterHost = Callable[..., _HostCapture]


@pytest_asyncio.fixture()
async def register_worktree_host(
    app: FastAPI,
    db_uri: str,
) -> AsyncIterator[RegisterHost]:
    """Yield a factory that registers a fake host with a replying drain.

    The drain answers ``host.stat`` (so workspace validation passes) and
    ``host.create_worktree`` (capturing each frame). Every drain started
    during the test is poisoned and awaited at teardown, so no background
    task leaks into the next test's event loop (mirrors the cleanup in
    ``test_host_worktree.py``).

    :param app: App whose ``host_registry`` to register into.
    :param db_uri: DB URI so the ``host_id`` FK target row exists.
    :returns: Async iterator yielding a ``register`` factory. Its
        kwargs: ``create_status`` (``"ok"`` returns a worktree path,
        ``"failed"`` simulates a host git failure such as a bad base
        ref) and ``create_error`` (the failure message). Returns a
        ``_HostCapture`` whose ``.create`` / ``.remove`` lists accumulate
        the create- and remove-worktree frames the host received.
        ``place_like_host=True`` makes the fake host render its reply
        through the real ``_resolve_worktree_path`` — the frame's
        ``path_template`` (or the sibling layout when it is ``None``)
        decides the returned path, exactly as a real host would.
        ``owner`` sets the host's owner (for auth-enabled tests) and
        ``app_override`` registers into that app's registry instead of the
        fixture's.
    """
    conns: list[HostConnection] = []

    def _register(
        *,
        create_status: str = "ok",
        create_error: str | None = None,
        stat_fails_for: Callable[[str], bool] | None = None,
        workspace: str | None = None,
        canonical_path: Callable[[str], str] | None = None,
        place_like_host: bool = False,
        owner: str = RESERVED_USER_LOCAL,
        app_override: FastAPI | None = None,
    ) -> _HostCapture:
        target_app = app_override if app_override is not None else app
        HostStore(db_uri).upsert_on_connect(_HOST_ID, "wt-host", owner)
        conn = target_app.state.host_registry.register(
            host_id=_HOST_ID,
            ws=_FakeWebSocket(),  # type: ignore[arg-type] — duck-typed
            hello=HostHelloFrame(version="0.1.0-test", frame_protocol_version=1, name="wt-host"),
            owner=owner,
        )
        cap = _HostCapture()

        async def _drain() -> None:
            """Answer stat + create/remove-worktree frames; capture them."""
            while True:
                frame_text = await conn.outbound_queue.get()
                if frame_text is None:
                    return
                frame = decode_host_frame(frame_text)
                if isinstance(frame, HostStatFrame):
                    fut = conn.pending_stats.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        if stat_fails_for is not None and stat_fails_for(frame.path):
                            fut.set_result(
                                {
                                    "status": "failed",
                                    "exists": False,
                                    "type": None,
                                    "canonical_path": None,
                                    "error": "stat failed",
                                }
                            )
                        else:
                            fut.set_result(
                                {
                                    "status": "ok",
                                    "exists": True,
                                    "type": "directory",
                                    "canonical_path": (
                                        canonical_path(frame.path)
                                        if canonical_path is not None
                                        else frame.path
                                    ),
                                    "error": None,
                                }
                            )
                elif isinstance(frame, HostCreateWorktreeFrame):
                    cap.create.append(frame)
                    fut = conn.pending_create_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        if create_status == "ok":
                            if place_like_host:
                                worktree_path = str(
                                    _resolve_worktree_path(
                                        frame.repo_path,
                                        frame.branch_name,
                                        path_template=frame.path_template,
                                        entry=frame.entry,
                                    )[0]
                                )
                            else:
                                dirname = frame.branch_name.replace("/", "-")
                                worktree_path = f"{_SOURCE_REPO}-worktrees/{dirname}"
                            fut.set_result(
                                {
                                    "status": "ok",
                                    "worktree_path": worktree_path,
                                    "workspace": workspace,
                                    "branch": frame.branch_name,
                                    "error": None,
                                }
                            )
                        else:
                            fut.set_result(
                                {
                                    "status": "failed",
                                    "worktree_path": None,
                                    "branch": None,
                                    "error": create_error,
                                }
                            )
                elif isinstance(frame, HostListWorktreesFrame):
                    branch = cap.create[-1].branch_name if cap.create else None
                    fut = conn.pending_list_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result(
                            {
                                "status": "ok",
                                "worktrees": [
                                    {
                                        "path": frame.repo_path,
                                        "branch": branch,
                                        "is_main": False,
                                    }
                                ],
                            }
                        )
                elif isinstance(frame, HostRemoveWorktreeFrame):
                    cap.remove.append(frame)
                    fut = conn.pending_remove_worktrees.pop(frame.request_id, None)
                    if fut is not None and not fut.done():
                        fut.set_result({"status": "ok", "error": None})

        conn._drain_task_for_test = asyncio.create_task(_drain())  # type: ignore[attr-defined]
        conns.append(conn)
        return cap

    yield _register

    # Poison each queue so the drain returns, then await/cancel it.
    for conn in conns:
        conn.outbound_queue.put_nowait(None)
        task = conn._drain_task_for_test  # type: ignore[attr-defined]
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
            await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        if not task.done():
            task.cancel()


async def _create_git_session(
    client: httpx.AsyncClient,
    agent_id: str,
    git: dict[str, Any],
    *,
    user: str | None = None,
) -> httpx.Response:
    """POST a JSON session-create with a ``git`` block.

    :param client: The test HTTP client.
    :param agent_id: Agent to bind.
    :param git: The ``git`` block, e.g.
        ``{"branch_name": "feature/x", "base_branch": "main"}``.
    :param user: Optional authenticated identity; sent as
        ``X-Forwarded-Email`` when set.
    :returns: The raw create response.
    """
    headers = {"X-Forwarded-Email": user} if user is not None else None
    return await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "host_id": _HOST_ID,
            "workspace": _SOURCE_REPO,
            "git": git,
        },
        headers=headers,
    )


_ENTRY_TEMPLATE = "{entry}/.worktrees/{repo}/{branch}"


def _store_path_template(app: FastAPI, db_uri: str) -> None:
    """Wire a preferences store carrying the ``{entry}`` worktree template.

    Auth is off in these tests, so the creating request's owner resolves
    to ``RESERVED_USER_LOCAL``.
    """
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(
        RESERVED_USER_LOCAL, "worktree_location", {"pathTemplate": _ENTRY_TEMPLATE}
    )
    app.state.user_preferences_store = store


async def test_create_passes_branch_and_base_branch_to_host(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """The request's branch_name + base_branch reach host.create_worktree,
    and the resulting worktree path + branch are persisted on the session.

    Proves the server route threads ``git.base_branch`` through
    ``_create_session_worktree`` → ``create_worktree_on_host`` → the
    frame. If base_branch were dropped on the route, the captured
    frame's base_branch would be ``None`` and this fails.
    """
    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-create-agent")

    resp = await _create_git_session(
        client, agent["id"], {"branch_name": "feature/login", "base_branch": "main"}
    )
    assert resp.status_code == 201, resp.text

    # The host received exactly one create-worktree frame carrying both
    # the new branch and the requested base ref.
    assert len(cap.create) == 1, f"expected one create_worktree frame, got {len(cap.create)}"
    frame = cap.create[0]
    assert frame.repo_path == _SOURCE_REPO
    assert frame.branch_name == "feature/login"
    assert frame.base_branch == "main"
    # No project on this create: no entry, so the legacy location is used.
    assert frame.entry is None

    # The returned worktree path becomes the session workspace, and the
    # branch is persisted (drives sidebar display + delete cleanup).
    body = resp.json()
    assert body["git_branch"] == "feature/login"
    assert body["workspace"] == f"{_SOURCE_REPO}-worktrees/feature-login"


async def test_create_with_template_and_no_entry_uses_the_repo_as_entry(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """With the template stored but no project entry, ``{entry}`` is the source repo.

    The create has no project, so the frame carries no entry; the host
    renders ``{entry}`` from the main work tree, nesting the worktree at
    ``<source repo>/.worktrees/<repo name>/<branch>``.
    """
    cap = register_worktree_host(place_like_host=True)
    _store_path_template(app, db_uri)
    agent = await create_test_agent(client, name="wt-template-no-entry-agent")

    resp = await _create_git_session(client, agent["id"], {"branch_name": "feature/x"})
    assert resp.status_code == 201, resp.text

    assert len(cap.create) == 1, cap.create
    assert cap.create[0].entry is None
    assert cap.create[0].path_template == _ENTRY_TEMPLATE

    body = resp.json()
    assert body["workspace"] == "/Users/alice/myrepo/.worktrees/myrepo/feature-x"
    assert body["worktree"] == "/Users/alice/myrepo/.worktrees/myrepo/feature-x"


async def test_create_without_base_branch_sends_none(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """Omitting base_branch sends ``None`` to the host (branch from HEAD).

    Pairs with the test above to pin both directions: a provided base
    threads through, an omitted one stays ``None`` so the host branches
    from the source repo's current HEAD.
    """
    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-create-agent-2")

    resp = await _create_git_session(client, agent["id"], {"branch_name": "wip"})
    assert resp.status_code == 201, resp.text

    assert len(cap.create) == 1
    assert cap.create[0].branch_name == "wip"
    assert cap.create[0].base_branch is None


async def test_create_with_project_entry_sends_it_to_host(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A create filed in a project with an entry sends the entry to the host.

    The host uses it to nest the worktree under ``<entry>/.worktrees/``;
    without it the worktree would land at the legacy sibling location.
    """
    cap = register_worktree_host()
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "Entry project", None)
    app.state.project_host_binding_store = _ProjectDirs(entries=[(_HOST_ID, _ENTRY)])
    agent = await create_test_agent(client, name="wt-entry-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "project_id": _PROJECT_ID,
            "host_id": _HOST_ID,
            "workspace": _ENTRY,
            "git": {"branch_name": "feature/login", "base_branch": "main"},
        },
    )
    assert resp.status_code == 201, resp.text

    assert len(cap.create) == 1, cap.create
    frame = cap.create[0]
    assert frame.entry == _ENTRY
    # No primary binding is registered, so the entry itself is the source.
    assert frame.repo_path == _ENTRY
    assert frame.branch_name == "feature/login"


@pytest.mark.parametrize(
    ("base_branch", "expected_base"),
    [(None, "release"), ("topic/base", "topic/base")],
)
async def test_create_project_new_branch_base_falls_back_to_code_repository(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
    base_branch: str | None,
    expected_base: str,
) -> None:
    """A project new branch forks from its code repo default; an explicit base wins."""
    cap = register_worktree_host()
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "Code project", None)
    repositories = SqlAlchemyProjectRepositoryStore(db_uri)
    repositories.apply_repository(
        project_id=_PROJECT_ID,
        name="root",
        remote_url="https://git.example.test/x.git",
        default_branch="release",
        role="code",
    )
    app.state.project_repository_store = repositories
    app.state.project_host_binding_store = _ProjectDirs(entries=[(_HOST_ID, _ENTRY)])
    agent = await create_test_agent(client, name="wt-code-base-agent")

    git: dict[str, Any] = {"branch_name": "feature/x"}
    if base_branch is not None:
        git["base_branch"] = base_branch
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "project_id": _PROJECT_ID,
            "host_id": _HOST_ID,
            "workspace": _ENTRY,
            "git": git,
        },
    )
    assert resp.status_code == 201, resp.text
    assert len(cap.create) == 1, cap.create
    assert cap.create[0].base_branch == expected_base


async def test_create_with_invalid_base_branch_fails_400(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """An invalid base branch fails the create with 400 INVALID_INPUT.

    The host rejects the bad base ref (``host.create_worktree`` →
    ``status: failed``); the server maps that to INVALID_INPUT (400),
    NOT 500 — it's user-correctable input — and surfaces the host's
    reason. Worktree creation fails before ``create_conversation``, so
    no session row is created (the response carries no session id).
    """
    register_worktree_host(
        create_status="failed",
        create_error="base branch does not exist: nope-not-a-branch",
    )
    agent = await create_test_agent(client, name="wt-bad-base-agent")

    resp = await _create_git_session(
        client,
        agent["id"],
        {"branch_name": "feature/x", "base_branch": "nope-not-a-branch"},
    )

    # 400 (not 500): a bad base ref is user input, not a server fault.
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "invalid_input"
    # The host's reason is surfaced verbatim so the UI can show it.
    assert "base branch does not exist" in body["error"]["message"]


async def test_create_with_existing_worktree_persists_without_creating(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """Starting in an existing worktree persists its branch, creates nothing.

    ``git.existing_worktree`` binds the session straight to a
    pre-existing worktree directory: no create-worktree frame is sent
    to the host, and ``branch_name`` is persisted as ``git_branch`` so
    the sidebar shows it and the opt-in delete flow can offer to remove it.
    """
    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-existing-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_id": _HOST_ID,
            "workspace": _SOURCE_REPO,
            "git": {"branch_name": "feature/existing", "existing_worktree": True},
        },
    )
    assert resp.status_code == 201, resp.text

    # No worktree was created — the host received no create frame.
    assert len(cap.create) == 0, f"expected no create_worktree frame, got {len(cap.create)}"

    # The existing worktree's branch is persisted; the workspace is the
    # supplied directory verbatim (no worktree-path rewrite).
    body = resp.json()
    assert body["git_branch"] == "feature/existing"
    assert body["workspace"] == _SOURCE_REPO


async def test_create_with_invalid_existing_worktree_branch_fails_400(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """An invalid bind-mode ``branch_name`` fails the create with 400.

    The host never runs git for this path, so the server is the only
    gate on the name; a malformed branch is user-correctable input and
    maps to INVALID_INPUT (400), not 500.
    """
    register_worktree_host()
    agent = await create_test_agent(client, name="wt-existing-bad-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_id": _HOST_ID,
            "workspace": _SOURCE_REPO,
            "git": {"branch_name": "bad..branch", "existing_worktree": True},
        },
    )

    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["error"]["code"] == "invalid_input"
    # The failed create returned an error, not a session.
    assert "id" not in body


async def test_create_failure_never_removes_existing_worktree(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create_conversation failure must NOT destroy the user's worktree.

    Regression: the ``existing_worktree`` bind path sets ``git_branch``
    for a *pre-existing* worktree without Omnigent creating one. The
    create-rollback (``git worktree remove --force`` + ``git branch -D``)
    is gated on Omnigent having created a worktree, NOT on ``git_branch``
    being set — otherwise a persistence failure would force-remove the
    user's own worktree and delete their branch. Assert no remove frame
    is sent when ``create_conversation`` raises on this path.
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-no-destroy-agent")

    # Force the persistence step to fail after the bind path has already
    # set git_branch — the exact window the rollback guards. Patch the class
    # method (the store is a thin, stateless db_uri wrapper, and the route
    # uses its own instance) so the failure hits regardless of which
    # instance the router closed over.
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated create_conversation failure")

    monkeypatch.setattr(SqlAlchemyConversationStore, "create_conversation", _boom)

    # The in-process ASGI transport re-raises unhandled server errors, so
    # the simulated failure surfaces here rather than as a 500 response.
    # Either way the create failed; what matters is the side effect below.
    with pytest.raises(RuntimeError, match="simulated create_conversation failure"):
        await client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "host_id": _HOST_ID,
                "workspace": _SOURCE_REPO,
                "git": {"branch_name": "feature/existing", "existing_worktree": True},
            },
        )

    # Critically, the user's worktree is left untouched: the create-rollback
    # did NOT fire, so no remove_worktree frame reached the host.
    assert cap.remove == [], (
        f"create-rollback force-removed the user's existing worktree: {cap.remove}"
    )


async def test_create_failure_rolls_back_omnigent_created_worktree(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create_conversation failure DOES clean up an Omnigent-made worktree.

    The counterpart to the data-loss guard: when Omnigent creates the
    worktree (the ``git`` path) and persistence then fails, the orphan
    worktree it just made must be force-removed. Proves the narrowed
    rollback guard (gated on Omnigent having created a worktree) still
    fires for the case it is meant to clean up.
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-rollback-agent")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated create_conversation failure")

    monkeypatch.setattr(SqlAlchemyConversationStore, "create_conversation", _boom)

    with pytest.raises(RuntimeError, match="simulated create_conversation failure"):
        await _create_git_session(client, agent["id"], {"branch_name": "feature/orphan"})

    # Omnigent created the worktree, so the rollback force-removed it: one
    # remove frame for the worktree it just made, deleting the branch too.
    assert len(cap.create) == 1, cap.create
    assert len(cap.remove) == 1, f"expected a create-rollback remove frame, got {cap.remove}"
    assert cap.remove[0].branch == "feature/orphan"
    assert cap.remove[0].delete_branch is True


async def test_create_failure_rollback_preserves_existing_branch(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Create-rollback of an ``existing_branch`` recreate keeps the branch.

    The deleted-worktree recreate path checks out a branch that predates
    the request (it may carry unpushed commits). When persistence fails
    after the worktree was recreated, rollback still removes the
    directory but must NOT ``git branch -D`` the user's branch.
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    cap = register_worktree_host()
    agent = await create_test_agent(client, name="wt-existing-branch-rollback-agent")

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("simulated create_conversation failure")

    monkeypatch.setattr(SqlAlchemyConversationStore, "create_conversation", _boom)

    with pytest.raises(RuntimeError, match="simulated create_conversation failure"):
        await _create_git_session(
            client,
            agent["id"],
            {"branch_name": "feature/kept", "existing_branch": True},
        )

    assert len(cap.create) == 1, cap.create
    assert cap.create[0].existing_branch is True
    assert len(cap.remove) == 1, f"expected a create-rollback remove frame, got {cap.remove}"
    assert cap.remove[0].branch == "feature/kept"
    assert cap.remove[0].delete_branch is False, (
        "rollback of an existing-branch recreate must preserve the user's "
        "pre-existing branch (unpushed commits would be lost)"
    )


async def test_create_rolls_back_worktree_on_canonicalize_failure(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """F-B3: a host.stat failure resolving the just-created worktree's
    canonical path must not leak it.

    The worktree is created (status ok), but the follow-up ``host.stat``
    that canonicalises its path fails — before the conversation row is
    ever created. Without the fix, the created worktree would never be
    rolled back (the failure propagated past ``create_conversation``'s
    orphan-cleanup, which never runs).
    """
    created_path = f"{_SOURCE_REPO}-worktrees/feature-orphan"
    cap = register_worktree_host(stat_fails_for=lambda path: path == created_path)
    agent = await create_test_agent(client, name="wt-canonicalize-rollback-agent")

    resp = await _create_git_session(client, agent["id"], {"branch_name": "feature/orphan"})

    assert resp.status_code == 400, resp.text
    assert len(cap.create) == 1, cap.create
    assert len(cap.remove) == 1, f"expected a create-rollback remove frame, got {cap.remove}"
    assert cap.remove[0].worktree_path == created_path
    assert cap.remove[0].branch == "feature/orphan"
    assert cap.remove[0].delete_branch is True


async def test_create_preserves_selected_subdirectory(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """Persist the relocated subdirectory returned by the host as the session workspace."""
    workspace = f"{_SOURCE_REPO}-worktrees/worktree-1234abcd/packages/app"
    cap = register_worktree_host(workspace=workspace)
    agent = await create_test_agent(client, name="subdirectory-agent")
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_id": _HOST_ID,
            "workspace": f"{_SOURCE_REPO}/packages/app",
            "git": {"branch_name": "worktree-1234abcd"},
        },
    )
    assert response.status_code == 201, response.text
    assert cap.create[0].repo_path == f"{_SOURCE_REPO}/packages/app"
    assert response.json()["workspace"] == workspace
    detail = await client.get(f"/v1/sessions/{response.json()['id']}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["workspace"] == workspace
    from omnigent.server.routes._host_worktree import (
        WORKTREE_ROOT_LABEL_KEY,
        worktree_root_fingerprint,
    )

    assert detail.json()["labels"][WORKTREE_ROOT_LABEL_KEY] == worktree_root_fingerprint(
        f"{_SOURCE_REPO}-worktrees/worktree-1234abcd"
    )


async def test_create_records_canonical_root_so_delete_finds_worktree(
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
) -> None:
    """A raw host path that canonicalises elsewhere still cleans up on delete.

    The recorded worktree root is the canonical path while the host returned
    the raw one, so the cleanup label must fingerprint the canonical root:
    fingerprinting the raw path makes ``recorded_worktree_root`` miss it and
    delete silently leaks the worktree.
    """
    from omnigent.server.routes._host_worktree import (
        WORKTREE_ROOT_LABEL_KEY,
        worktree_root_fingerprint,
    )

    raw = f"{_SOURCE_REPO}-worktrees/feature-canonical"

    def _canonicalize(path: str) -> str:
        return f"/opt/work/canonical{path}" if path == raw else path

    cap = register_worktree_host(canonical_path=_canonicalize)
    agent = await create_test_agent(client, name="canonical-root-agent")
    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "host_id": _HOST_ID,
            "workspace": _SOURCE_REPO,
            "git": {"branch_name": "feature/canonical"},
        },
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    canonical = _canonicalize(raw)
    assert response.json()["workspace"] == canonical

    detail = await client.get(f"/v1/sessions/{session_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["labels"][WORKTREE_ROOT_LABEL_KEY] == worktree_root_fingerprint(canonical)

    deleted = await client.delete(f"/v1/sessions/{session_id}?delete_branch=true")
    assert deleted.status_code == 200, deleted.text
    assert len(cap.remove) == 1, "delete cleanup must find the canonical worktree root"
    assert cap.remove[0].worktree_path == canonical


async def test_create_rejects_forged_worktree_identity(
    client: httpx.AsyncClient,
) -> None:
    """Clients cannot redirect the server-owned cleanup identity."""
    from omnigent.server.routes._host_worktree import WORKTREE_ROOT_LABEL_KEY

    agent = await create_test_agent(client, name="forged-root-agent")
    response = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "labels": {WORKTREE_ROOT_LABEL_KEY: "forged"}},
    )
    assert response.status_code == 400, response.text


async def _create_project_parent_session(
    client: httpx.AsyncClient,
    agent_id: str,
    *,
    project_id: str = _PROJECT_ID,
    workspace: str = _ENTRY,
) -> str:
    """Create a plain (no-git) parent session filed in the given project.

    :param client: The test HTTP client.
    :param agent_id: Agent to bind.
    :param project_id: Project to file the parent under.
    :param workspace: The parent's (and its project's) directory.
    :returns: The new session's id, for use as ``parent_session_id``.
    """
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "project_id": project_id,
            "host_id": _HOST_ID,
            "workspace": workspace,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_child_inherits_parent_project_entry_for_worktree_placement(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A child create that names no project places its worktree under the
    parent project's entry.

    The child sends ``parent_session_id`` + ``git`` but no ``project_id``;
    with the ``{entry}`` template stored, its worktree must still land at
    ``<entry>/.worktrees/<repo>/<branch>`` (the parent project's entry on
    the host), not the sibling fallback. Placement only: the child's own
    launch directory stays its worktree, so the response workspace and
    worktree are both the created path.
    """
    cap = register_worktree_host(place_like_host=True)
    _store_path_template(app, db_uri)
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "Entry project", None)
    app.state.project_host_binding_store = _ProjectDirs(entries=[(_HOST_ID, _ENTRY)])
    agent = await create_test_agent(client, name="wt-child-entry-agent")
    parent_id = await _create_project_parent_session(client, agent["id"])

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent_id,
            "host_id": _HOST_ID,
            "workspace": _ENTRY,
            "git": {"branch_name": "feature/x"},
        },
    )
    assert resp.status_code == 201, resp.text

    # The host received the inherited entry; with no primary binding the
    # entry itself is the source repo.
    assert len(cap.create) == 1, cap.create
    frame = cap.create[0]
    assert frame.entry == _ENTRY
    assert frame.repo_path == _ENTRY

    # The child launches in its own worktree, not at the entry.
    body = resp.json()
    assert body["workspace"] == "/Users/alice/project/.worktrees/project/feature-x"
    assert body["worktree"] == "/Users/alice/project/.worktrees/project/feature-x"


async def test_child_inherits_parent_project_entry_without_template_uses_sibling(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Without a stored template the inherited entry no longer places the worktree.

    The child sends ``parent_session_id`` + ``git`` but no ``project_id``,
    so its frame still carries the parent project's entry on the host —
    but with no ``worktree_location`` preference the host falls back to
    the upstream sibling layout, not ``<entry>/.worktrees/``.
    """
    cap = register_worktree_host(place_like_host=True)
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "Entry project", None)
    app.state.project_host_binding_store = _ProjectDirs(entries=[(_HOST_ID, _ENTRY)])
    agent = await create_test_agent(client, name="wt-child-entry-no-template-agent")
    parent_id = await _create_project_parent_session(client, agent["id"])

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent_id,
            "host_id": _HOST_ID,
            "workspace": _ENTRY,
            "git": {"branch_name": "feature/x"},
        },
    )
    assert resp.status_code == 201, resp.text

    assert len(cap.create) == 1, cap.create
    frame = cap.create[0]
    assert frame.entry == _ENTRY
    assert frame.path_template is None

    # The entry is still sent, but the worktree lands beside the repo root
    # (the entry here, since the project has no primary binding), not
    # under the entry.
    body = resp.json()
    sibling = "/Users/alice/project-worktrees/feature-x"
    assert body["workspace"] == sibling
    assert body["worktree"] == sibling


async def test_child_naming_its_project_launches_in_its_worktree(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A child that names its project launches in the worktree it cut.

    This is ``sys_session_create`` with ``worktree`` and ``project_id``: the
    project's entry fills the child's workspace and, with the ``{entry}``
    template stored, places the worktree, and the child runs in that
    worktree, not at the entry.
    """
    cap = register_worktree_host(place_like_host=True)
    _store_path_template(app, db_uri)
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "Entry project", None)
    app.state.project_host_binding_store = _ProjectDirs(entries=[(_HOST_ID, _ENTRY)])
    agent = await create_test_agent(client, name="wt-child-named-project-agent")
    parent_id = await _create_project_parent_session(client, agent["id"])

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent_id,
            "project_id": _PROJECT_ID,
            "host_id": _HOST_ID,
            "git": {"branch_name": "feature/x"},
        },
    )
    assert resp.status_code == 201, resp.text

    assert len(cap.create) == 1, cap.create
    assert cap.create[0].entry == _ENTRY
    body = resp.json()
    assert body["workspace"] == "/Users/alice/project/.worktrees/project/feature-x"
    assert body["worktree"] == "/Users/alice/project/.worktrees/project/feature-x"


async def test_child_with_explicit_null_project_inherits_no_entry(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An explicit ``"project_id": null`` opts the child out of inheriting
    the parent project's entry.

    Field presence, not value, controls defaulting: the null stays explicit,
    so the create resolves no parent project and the host frame carries no
    entry (legacy sibling placement), exactly as before the inheritance fix.
    """
    cap = register_worktree_host()
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "Entry project", None)
    app.state.project_host_binding_store = _ProjectDirs(entries=[(_HOST_ID, _ENTRY)])
    agent = await create_test_agent(client, name="wt-child-null-project-agent")
    parent_id = await _create_project_parent_session(client, agent["id"])

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent_id,
            "project_id": None,
            "host_id": _HOST_ID,
            "workspace": _ENTRY,
            "git": {"branch_name": "feature/x"},
        },
    )
    assert resp.status_code == 201, resp.text

    assert len(cap.create) == 1, cap.create
    assert cap.create[0].entry is None


async def test_explicit_project_create_launches_in_its_worktree(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A create that names its project launches in the worktree it cut.

    This is the body ``sys_session_open`` sends for ``branch``: the entry
    and the stored ``{entry}`` template reach the host together, and the
    session's workspace is the created worktree, not the entry.
    """
    cap = register_worktree_host(place_like_host=True)
    _store_path_template(app, db_uri)
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "Entry project", None)
    app.state.project_host_binding_store = _ProjectDirs(entries=[(_HOST_ID, _ENTRY)])
    agent = await create_test_agent(client, name="wt-explicit-entry-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "project_id": _PROJECT_ID,
            "host_id": _HOST_ID,
            "workspace": _ENTRY,
            "git": {"branch_name": "feature/x"},
        },
    )
    assert resp.status_code == 201, resp.text

    assert len(cap.create) == 1, cap.create
    assert cap.create[0].entry == _ENTRY
    assert cap.create[0].path_template == _ENTRY_TEMPLATE

    body = resp.json()
    assert body["workspace"] == "/Users/alice/project/.worktrees/project/feature-x"
    assert body["worktree"] == "/Users/alice/project/.worktrees/project/feature-x"


async def test_explicit_project_bind_launches_in_the_existing_worktree(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Binding an existing worktree inside the entry launches in it.

    This is the body ``sys_session_open`` sends for ``workspace`` naming a
    listed worktree: nothing is created and the session runs there.
    """
    cap = register_worktree_host()
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "Entry project", None)
    app.state.project_host_binding_store = _ProjectDirs(entries=[(_HOST_ID, _ENTRY)])
    agent = await create_test_agent(client, name="wt-explicit-bind-agent")
    existing = f"{_ENTRY}/.worktrees/project/task"

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "project_id": _PROJECT_ID,
            "host_id": _HOST_ID,
            "workspace": existing,
            "git": {"branch_name": "task/fix", "existing_worktree": True},
        },
    )
    assert resp.status_code == 201, resp.text

    assert cap.create == []
    body = resp.json()
    assert body["workspace"] == existing
    assert body["worktree"] == existing
    assert body["git_branch"] == "task/fix"


async def test_child_of_project_without_entry_uses_sibling_placement(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A parent project with no entry row on the host inherits nothing.

    The child's host frame carries no entry, so the worktree lands at the
    legacy sibling location, exactly as before the inheritance fix.
    """
    cap = register_worktree_host()
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "No-entry project", None)
    app.state.project_host_binding_store = _ProjectDirs()
    agent = await create_test_agent(client, name="wt-child-no-entry-agent")
    parent_id = await _create_project_parent_session(client, agent["id"])

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent_id,
            "host_id": _HOST_ID,
            "workspace": _ENTRY,
            "git": {"branch_name": "feature/x"},
        },
    )
    assert resp.status_code == 201, resp.text

    assert len(cap.create) == 1, cap.create
    assert cap.create[0].entry is None


async def test_child_inherited_checkout_outside_boundary_fails_400(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An inherited source checkout that fails validation refuses the create.

    The parent project's primary binding names the repository a worktree
    sources from; the caller's own workspace (the entry) never stood in for
    it, so the swapped source is validated against the agent's boundary
    first. A failure raises 400 invalid_input before any
    ``host.create_worktree`` frame — no worktree, no session row.
    """
    binding_repo = "/Users/alice/other-repo"
    cap = register_worktree_host(stat_fails_for=lambda path: path == binding_repo)
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "Entry project", None)
    app.state.project_host_binding_store = _ProjectDirs(
        entries=[(_HOST_ID, _ENTRY)],
        bindings=[(_HOST_ID, binding_repo)],
    )
    agent = await create_test_agent(client, name="wt-child-bad-checkout-agent")
    parent_id = await _create_project_parent_session(client, agent["id"])

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent_id,
            "host_id": _HOST_ID,
            "workspace": _ENTRY,
            "git": {"branch_name": "feature/x"},
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_input"
    assert cap.create == [], f"expected no create_worktree frame, got {cap.create}"


async def test_explicit_project_checkout_outside_boundary_fails_400(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A create naming its project validates the checkout it swaps to.

    This is the body ``sys_session_open`` sends for ``branch``: the caller's
    workspace is the entry, and the worktree would be cut from the
    project's primary binding. A checkout that fails validation refuses the
    create with 400 before any ``host.create_worktree`` frame.
    """
    binding_repo = "/Users/alice/other-repo"
    cap = register_worktree_host(stat_fails_for=lambda path: path == binding_repo)
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "Entry project", None)
    app.state.project_host_binding_store = _ProjectDirs(
        entries=[(_HOST_ID, _ENTRY)],
        bindings=[(_HOST_ID, binding_repo)],
    )
    agent = await create_test_agent(client, name="wt-explicit-bad-checkout-agent")

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "project_id": _PROJECT_ID,
            "host_id": _HOST_ID,
            "workspace": _ENTRY,
            "git": {"branch_name": "feature/x"},
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "invalid_input"
    assert cap.create == [], f"expected no create_worktree frame, got {cap.create}"


async def test_named_sub_agent_child_sends_no_entry(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A named sub-agent child is out of the inheritance fix's scope.

    Its create takes the resolver's named-sub-agent early return, so even
    with a parent filed in a project the host frame carries no entry —
    today's behaviour, unchanged.
    """
    cap = register_worktree_host()
    SqlAlchemyProjectStore(db_uri).create(_PROJECT_ID, "Entry project", None)
    app.state.project_host_binding_store = _ProjectDirs(entries=[(_HOST_ID, _ENTRY)])

    parent = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"project_id": _PROJECT_ID})},
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="wt-named-parent", sub_agents=[{"name": "worker"}]),
                "application/gzip",
            )
        },
    )
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["session_id"]
    parent_agent = await client.get(f"/v1/sessions/{parent_id}/agent")
    assert parent_agent.status_code == 200, parent_agent.text

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": parent_agent.json()["id"],
            "parent_session_id": parent_id,
            "sub_agent_name": "worker",
            "host_id": _HOST_ID,
            "workspace": _ENTRY,
            "git": {"branch_name": "feature/x"},
        },
    )
    assert resp.status_code == 201, resp.text

    assert len(cap.create) == 1, cap.create
    assert cap.create[0].entry is None


_PROJECT_B = "90817263544536271809f8e7d6c5b4a3"
_ROOT_A = "/opt/work/project-a"
_ROOT_B = "/opt/work/project-b"
_REPO_B = "/opt/work/project-b/repo"


def _calling_config(workspace: str, model: str, effort: str) -> dict[str, object]:
    """A project root on the test host whose per-host row names model / effort."""
    return {
        "host_id": _HOST_ID,
        "workspace": workspace,
        "calling_defaults": {
            _HOST_ID: {"harnesses": {"claude-sdk": {"model": model, "effort": effort}}}
        },
    }


async def test_child_worktree_from_another_projects_repo_lands_under_that_project(
    app: FastAPI,
    register_worktree_host: RegisterHost,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A child worktree cut from project B's repo lands under B's entry and takes B's defaults."""
    cap = register_worktree_host(place_like_host=True)
    _store_path_template(app, db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    projects.create(
        _PROJECT_ID, "project-a", None, config=_calling_config(_ROOT_A, "model-a", "high")
    )
    projects.create(
        _PROJECT_B, "project-b", None, config=_calling_config(_ROOT_B, "model-b", "low")
    )
    app.state.host_store = HostStore(db_uri)
    app.state.project_host_binding_store = _ProjectDirs(
        entries_by_project={_PROJECT_B: [(_HOST_ID, _ROOT_B)]}
    )
    agent = await create_test_agent(client, name="wt-cross-project-agent")
    parent_id = await _create_project_parent_session(
        client, agent["id"], project_id=_PROJECT_ID, workspace=_ROOT_A
    )

    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "parent_session_id": parent_id,
            "host_id": _HOST_ID,
            "workspace": _REPO_B,
            "git": {"branch_name": "feature/x"},
        },
    )
    assert resp.status_code == 201, resp.text

    # The child named no project, but its workspace lives in B's root: the
    # owner lookup files it under B and the frame carries B's entry, so the
    # host cuts the worktree under B — not the parent project A's entry.
    assert len(cap.create) == 1, cap.create
    frame = cap.create[0]
    assert frame.entry == _ROOT_B
    assert frame.repo_path == _REPO_B

    body = resp.json()
    assert body["project_id"] == _PROJECT_B
    assert body["model_override"] == "model-b"
    assert body["reasoning_effort"] == "low"
    assert body["workspace"] == f"{_ROOT_B}/.worktrees/repo/feature-x"
    assert body["worktree"] == f"{_ROOT_B}/.worktrees/repo/feature-x"


async def test_create_reads_the_authenticated_owners_template(
    auth_app: FastAPI,
    register_worktree_host: RegisterHost,
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """With auth on, the creating user's own template is read, not ``"local"``'s.

    Bob and the reserved local owner each hold a different template, so a
    hard-coded owner would surface in the frame: it must carry alice's.
    """
    alice = "alice@example.com"
    store = SqlAlchemyUserPreferencesStore(db_uri)
    store.patch_namespace(alice, "worktree_location", {"pathTemplate": _ENTRY_TEMPLATE})
    store.patch_namespace(
        "bob@example.com", "worktree_location", {"pathTemplate": "/data/wt/{repo}/{branch}"}
    )
    store.patch_namespace(
        RESERVED_USER_LOCAL, "worktree_location", {"pathTemplate": "/tmp/local/{repo}/{branch}"}
    )
    auth_app.state.user_preferences_store = store
    cap = register_worktree_host(app_override=auth_app, owner=alice)
    agent = await create_test_agent(auth_client, name="wt-auth-owner-agent", user=alice)

    resp = await _create_git_session(
        auth_client, agent["id"], {"branch_name": "feature/x"}, user=alice
    )

    assert resp.status_code == 201, resp.text
    assert len(cap.create) == 1, cap.create
    assert cap.create[0].path_template == _ENTRY_TEMPLATE
