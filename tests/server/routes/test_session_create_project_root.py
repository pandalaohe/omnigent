"""Project root placement in session creation and the host-roots route."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import builtin_agent_id
from omnigent.entities import ProjectHostBinding, ProjectHostEntry
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_READ, UnifiedAuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.routes._host_worktree import CreatedWorktree
from omnigent.server.routes._session_create_validation import resolve_project_session_create
from omnigent.server.schemas import ProjectSessionCreateRequest
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio
ALICE = "alice@example.com"
AGENT_ID = builtin_agent_id("generic-builtin")


@pytest.fixture()
def app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agents = SqlAlchemyAgentStore(db_uri)
    agents.create(AGENT_ID, "generic-builtin", f"{AGENT_ID}/bundle")
    artifacts.put(f"{AGENT_ID}/bundle", build_agent_bundle(name="generic-builtin"))
    projects = SqlAlchemyProjectStore(db_uri)
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    app = create_app(
        agent_store=agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        permission_store=permissions,
        project_store=projects,
        auth_provider=UnifiedAuthProvider(source="header"),
    )
    app.state.project_store = projects
    app.state.conversation_store = conversations
    app.state.permission_store = permissions
    return app


@pytest_asyncio.fixture()
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as value:
        yield value


def _headers(user: str = ALICE) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


async def _project(
    client: httpx.AsyncClient, name: str, config: dict[str, object], user: str = ALICE
) -> str:
    response = await client.post(
        "/v1/projects", json={"name": name, "config": config}, headers=_headers(user)
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


class Bindings:
    def __init__(
        self,
        bindings: list[ProjectHostBinding],
        entries: list[ProjectHostEntry] | None = None,
    ) -> None:
        self.bindings = bindings
        self.entries = entries or []

    def list_by_project(self, project_id: str) -> list[ProjectHostBinding]:
        return [binding for binding in self.bindings if binding.project_id == project_id]

    def list_entries(self, project_id: str) -> list[ProjectHostEntry]:
        return [entry for entry in self.entries if entry.project_id == project_id]


def _binding(project_id: str, host_id: str, workspace: str) -> ProjectHostBinding:
    return ProjectHostBinding(
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


def _entry(project_id: str, host_id: str, workspace: str) -> ProjectHostEntry:
    return ProjectHostEntry(project_id, host_id, workspace, 1)


async def test_config_root_rejection_explicit_workspace_and_hostless_create(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(
        client, "roots", {"agent_id": AGENT_ID, "host_id": "h1", "workspace": "/c"}
    )
    wrong = await client.post(
        "/v1/sessions", json={"project_id": project_id, "host_id": "h2"}, headers=_headers()
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["message"] == (
        "Project 'roots' has no directory on host 'h2'. Pass workspace, or set "
        "this host's directory in the project settings."
    )
    config_root = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(project_id=project_id, host_id="h1"),
        user_id=ALICE,
        project_store=app.state.project_store,
    )
    assert config_root.body.workspace == "/c"
    explicit_host_type = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(project_id=project_id, host_type="external"),
        user_id=ALICE,
        project_store=app.state.project_store,
        fill_host=True,
    )
    assert explicit_host_type.body.host_id is None
    assert explicit_host_type.body.workspace == "/c"
    explicit = await client.post(
        "/v1/sessions",
        json={"project_id": project_id, "workspace": "/elsewhere"},
        headers=_headers(),
    )
    assert explicit.status_code == 201, explicit.text
    assert explicit.json()["project_id"] == project_id
    assert explicit.json()["host_id"] is None
    assert explicit.json()["workspace"] == "/elsewhere"
    null_workspace = await client.post(
        "/v1/sessions", json={"project_id": project_id, "workspace": None}, headers=_headers()
    )
    assert null_workspace.status_code == 201, null_workspace.text
    assert null_workspace.json()["host_id"] is None
    assert null_workspace.json()["workspace"] is None


async def test_resolver_binding_and_host_choices(app: FastAPI, client: httpx.AsyncClient) -> None:
    project_id = await _project(
        client, "bound", {"agent_id": AGENT_ID, "host_id": "h1", "workspace": "/c"}
    )
    app.state.project_host_binding_store = Bindings([_binding(project_id, "h1", "/b")])
    app.state.feature_flags = FeatureFlags(frozenset({Feature.PROJECT_ASSIGNMENTS}))
    app.state.project_store.set_collaboration(
        project_id, user_id=ALICE, enabled=True, expected_revision=0
    )
    resolved = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(project_id=project_id, host_id="h1"),
        user_id=ALICE,
        project_store=app.state.project_store,
        binding_store=app.state.project_host_binding_store,
        feature_flags=app.state.feature_flags,
    )
    assert resolved.body.workspace == "/b"
    filled = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(project_id=project_id),
        user_id=ALICE,
        project_store=app.state.project_store,
        binding_store=app.state.project_host_binding_store,
        feature_flags=app.state.feature_flags,
        fill_host=True,
    )
    assert (filled.body.host_id, filled.body.workspace) == ("h1", "/b")


async def test_ambiguous_host_fill_and_switch_gate(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client, "several", {"agent_id": AGENT_ID})
    app.state.project_host_binding_store = Bindings(
        [_binding(project_id, "h1", "/one"), _binding(project_id, "h2", "/two")]
    )
    app.state.feature_flags = FeatureFlags(frozenset({Feature.PROJECT_ASSIGNMENTS}))
    no_switch = await client.post(
        "/v1/sessions", json={"project_id": project_id, "host_id": "h1"}, headers=_headers()
    )
    assert no_switch.status_code == 400
    app.state.project_store.set_collaboration(
        project_id, user_id=ALICE, enabled=True, expected_revision=0
    )
    ambiguous = await client.post(
        "/v1/sessions", json={"project_id": project_id}, headers=_headers()
    )
    assert ambiguous.status_code == 400
    assert ambiguous.json()["error"]["message"] == (
        "Project 'several' has a directory on several hosts (h1, h2). Pass host_id."
    )

    class Hosts:
        def get_host(self, host_id: str) -> object | None:
            return SimpleNamespace(host_id="h1", user_id=ALICE) if host_id == "h1" else None

    app.state.host_store = Hosts()
    root_response = await client.get(f"/v1/projects/{project_id}/host-roots", headers=_headers())
    assert root_response.json() == {
        "roots": [{"host_id": "h1", "workspace": "/one", "source": "binding", "checkout": "/one"}],
        "default_host_id": "h1",
        "default_host_reason": "single_root",
    }
    filled = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(project_id=project_id),
        user_id=ALICE,
        project_store=app.state.project_store,
        binding_store=app.state.project_host_binding_store,
        feature_flags=app.state.feature_flags,
        host_store=app.state.host_store,
        fill_host=True,
    )
    assert (filled.body.host_id, filled.body.workspace) == ("h1", "/one")


async def test_deleted_config_host_is_not_replaced_by_binding(
    app: FastAPI, client: httpx.AsyncClient, db_uri: str
) -> None:
    host_id = "2" * 32
    project_id = await _project(
        client, "deleted-host", {"agent_id": AGENT_ID, "host_id": host_id, "workspace": "/c"}
    )
    app.state.project_host_binding_store = Bindings([_binding(project_id, "3" * 32, "/b")])
    app.state.feature_flags = FeatureFlags(frozenset({Feature.PROJECT_ASSIGNMENTS}))
    app.state.project_store.set_collaboration(
        project_id, user_id=ALICE, enabled=True, expected_revision=0
    )
    app.state.host_store = HostStore(db_uri)
    response = await client.post(
        "/v1/sessions", json={"project_id": project_id}, headers=_headers()
    )
    assert response.status_code == 404
    roots = await client.get(f"/v1/projects/{project_id}/host-roots", headers=_headers())
    assert roots.json()["default_host_id"] == host_id
    assert roots.json()["roots"] == []


@pytest.mark.parametrize("use_binding", [False, True])
async def test_json_filled_host_reaches_launch(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
    use_binding: bool,
) -> None:
    from omnigent.server.routes import _host_launch
    from omnigent.server.routes._sessions import orchestration

    host_id = "1" * 32
    config: dict[str, object] = {"agent_id": AGENT_ID}
    if not use_binding:
        config.update({"host_id": host_id, "workspace": "/c"})
    project_id = await _project(client, "launch", config)
    if use_binding:
        app.state.project_host_binding_store = Bindings([_binding(project_id, host_id, "/b")])
        app.state.feature_flags = FeatureFlags(frozenset({Feature.PROJECT_ASSIGNMENTS}))
        app.state.project_store.set_collaboration(
            project_id, user_id=ALICE, enabled=True, expected_revision=0
        )
    expected_workspace = "/b" if use_binding else "/c"
    launched: list[str] = []
    validated: list[str] = []
    conn = SimpleNamespace(pending_launches={})

    class Registry:
        async def admit_launch(self, connection: object, session_id: str) -> None:
            assert connection is conn

        def send_text(self, connection: object, frame: str) -> None:
            next(iter(conn.pending_launches.values())).set_result({"status": "ok"})

    async def validate_workspace(**kwargs: object) -> str:
        validated.append(str(kwargs["host_id"]))
        return str(kwargs["workspace"])

    def resolve_launch(**kwargs: object) -> object:
        launched.append(str(kwargs["host_id"]))
        return SimpleNamespace(
            conn=conn, conv=app.state.conversation_store.get_conversation(kwargs["session_id"])
        )

    app.state.host_store = HostStore(db_uri)
    app.state.host_store.upsert_on_connect(host_id, "desktop", ALICE)
    app.state.host_registry = Registry()
    monkeypatch.setattr(orchestration, "_validate_session_workspace", validate_workspace)
    monkeypatch.setattr(_host_launch, "resolve_host_launch", resolve_launch)
    response = await client.post(
        "/v1/sessions", json={"project_id": project_id}, headers=_headers()
    )
    assert response.status_code == 201, response.text
    assert response.json()["host_id"] == host_id
    assert response.json()["workspace"] == expected_workspace
    assert response.json()["project_id"] == project_id
    assert validated == [host_id]
    assert launched == [host_id]
    other_host_id = "2" * 32
    app.state.host_store.upsert_on_connect(other_host_id, "other-desktop", ALICE)
    explicit = await client.post(
        "/v1/sessions",
        json={"project_id": project_id, "host_id": other_host_id, "workspace": "/x"},
        headers=_headers(),
    )
    assert explicit.status_code == 201, explicit.text
    assert explicit.json()["project_id"] == project_id
    assert explicit.json()["workspace"] == "/x"
    assert launched == [host_id, other_host_id]


async def test_child_inherits_owned_project_and_multipart_reply(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client, "children", {"agent_id": AGENT_ID})
    parent = await client.post("/v1/sessions", json={"project_id": project_id}, headers=_headers())
    assert parent.status_code == 201, parent.text
    parent_id = parent.json()["id"]
    child = await client.post(
        "/v1/sessions",
        json={"agent_id": AGENT_ID, "parent_session_id": parent_id},
        headers=_headers(),
    )
    assert child.status_code == 201, child.text
    assert child.json()["project_id"] == project_id
    assert child.json()["workspace"] is None
    bundle = await client.post(
        "/v1/sessions",
        data={"metadata": f'{{"parent_session_id":"{parent_id}"}}'},
        files={"bundle": ("agent.tar.gz", build_agent_bundle(name="helper"), "application/gzip")},
        headers=_headers(),
    )
    assert bundle.status_code == 201, bundle.text
    assert bundle.json()["project_id"] == project_id


async def test_child_does_not_inherit_foreign_or_unfiled_project(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    bob = "bob@example.com"
    foreign_project = await _project(client, "foreign-parent", {"agent_id": AGENT_ID}, bob)
    foreign_parent = await client.post(
        "/v1/sessions", json={"project_id": foreign_project}, headers=_headers(bob)
    )
    assert foreign_parent.status_code == 201, foreign_parent.text
    parent_id = foreign_parent.json()["id"]
    app.state.permission_store.ensure_user(ALICE)
    app.state.permission_store.grant(ALICE, parent_id, LEVEL_READ)
    child = await client.post(
        "/v1/sessions",
        json={"agent_id": AGENT_ID, "parent_session_id": parent_id},
        headers=_headers(),
    )
    assert child.status_code == 201, child.text
    assert child.json()["project_id"] is None
    unfiled = await client.post("/v1/sessions", json={"agent_id": AGENT_ID}, headers=_headers())
    assert unfiled.status_code == 201, unfiled.text
    unfiled_child = await client.post(
        "/v1/sessions",
        json={"agent_id": AGENT_ID, "parent_session_id": unfiled.json()["id"]},
        headers=_headers(),
    )
    assert unfiled_child.status_code == 201, unfiled_child.text
    assert unfiled_child.json()["project_id"] is None


async def test_multipart_project_create_keeps_host_absent(
    client: httpx.AsyncClient,
) -> None:
    project_id = await _project(client, "multipart", {"host_id": "h1", "workspace": "/c"})
    response = await client.post(
        "/v1/sessions",
        data={"metadata": f'{{"project_id":"{project_id}"}}'},
        files={"bundle": ("agent.tar.gz", build_agent_bundle(name="helper"), "application/gzip")},
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    session = await client.get(f"/v1/sessions/{response.json()['session_id']}", headers=_headers())
    assert session.status_code == 200, session.text
    assert session.json()["host_id"] is None
    assert session.json()["workspace"] == "/c"


async def test_import_project_create_keeps_host_absent(
    client: httpx.AsyncClient, db_uri: str
) -> None:
    SqlAlchemyAgentStore(db_uri).create(
        builtin_agent_id("claude-native-ui"),
        name="claude-native-ui",
        bundle_location="builtin://claude-native-ui",
    )
    project_id = await _project(client, "import", {"host_id": "h1", "workspace": "/c"})
    response = await client.post(
        "/v1/imports",
        json={
            "source": "claude",
            "external_session_id": "root-placement-import",
            "project_id": project_id,
            "items": [
                {
                    "type": "message",
                    "response_id": "claude:turn-1",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "hello"}],
                    },
                }
            ],
        },
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    session = await client.get(f"/v1/sessions/{response.json()['session_id']}", headers=_headers())
    assert session.status_code == 200, session.text
    assert session.json()["host_id"] is None
    assert session.json()["workspace"] == "/c"


async def test_host_roots_endpoint_is_owner_scoped_and_not_flag_gated(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client, "endpoint", {"host_id": "h1", "workspace": "/c"})
    app.state.project_host_binding_store = Bindings([_binding(project_id, "h2", "/b")])
    response = await client.get(f"/v1/projects/{project_id}/host-roots", headers=_headers())
    assert response.status_code == 200, response.text
    assert response.json() == {
        "roots": [{"host_id": "h1", "workspace": "/c", "source": "config", "checkout": None}],
        "default_host_id": "h1",
        "default_host_reason": "config",
    }
    foreign = await client.get(
        f"/v1/projects/{project_id}/host-roots", headers=_headers("bob@example.com")
    )
    assert foreign.status_code == 404
    assert foreign.json()["error"]["message"] == "Project not found"


async def test_sandbox_config_never_fills_a_bound_host(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    project_id = await _project(
        client, "sandbox", {"agent_id": AGENT_ID, "host_id": "__sandbox__", "workspace": "/c"}
    )
    app.state.project_host_binding_store = Bindings([_binding(project_id, "h1", "/b")])
    app.state.feature_flags = FeatureFlags(frozenset({Feature.PROJECT_ASSIGNMENTS}))
    app.state.project_store.set_collaboration(
        project_id, user_id=ALICE, enabled=True, expected_revision=0
    )
    response = await client.post(
        "/v1/sessions", json={"project_id": project_id}, headers=_headers()
    )
    assert response.status_code == 201, response.text
    assert response.json()["host_id"] is None
    roots = await client.get(f"/v1/projects/{project_id}/host-roots", headers=_headers())
    assert roots.json()["default_host_reason"] == "none"


async def test_project_entry_fills_create_and_host_roots(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """An entry is the resolved workspace, and host-roots reports it plus checkout."""
    project_id = await _project(
        client, "entry-root", {"agent_id": AGENT_ID, "host_id": "h1", "workspace": "/c"}
    )
    app.state.project_host_binding_store = Bindings(
        [_binding(project_id, "h1", "/b")],
        [_entry(project_id, "h1", "/e"), _entry(project_id, "h2", "/e2")],
    )
    resolved = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(project_id=project_id, host_id="h1"),
        user_id=ALICE,
        project_store=app.state.project_store,
        binding_store=app.state.project_host_binding_store,
    )
    assert resolved.body.workspace == "/e"
    assert (resolved.entry, resolved.checkout) == ("/e", "/b")
    # An explicit workspace still resolves the entry and checkout on the host
    # for placement; the resolver keeps the caller's directory as sent.
    explicit = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(
            project_id=project_id, host_id="h1", workspace="/elsewhere"
        ),
        user_id=ALICE,
        project_store=app.state.project_store,
        binding_store=app.state.project_host_binding_store,
    )
    assert explicit.body.workspace == "/elsewhere"
    assert (explicit.entry, explicit.checkout) == ("/e", "/b")
    roots = await client.get(f"/v1/projects/{project_id}/host-roots", headers=_headers())
    assert roots.json()["roots"] == [
        {"host_id": "h1", "workspace": "/e", "source": "entry", "checkout": "/b"},
        {"host_id": "h2", "workspace": "/e2", "source": "entry", "checkout": "/e2"},
    ]


async def test_entry_on_another_host_only_refuses_create(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """Deleting a host's entry means "no directory on that host": never a fallback."""
    project_id = await _project(
        client, "entry-elsewhere", {"agent_id": AGENT_ID, "host_id": "h1", "workspace": "/c"}
    )
    app.state.project_host_binding_store = Bindings(
        [_binding(project_id, "h2", "/b")],
        [_entry(project_id, "h1", "/e")],
    )
    app.state.feature_flags = FeatureFlags(frozenset({Feature.PROJECT_ASSIGNMENTS}))
    app.state.project_store.set_collaboration(
        project_id, user_id=ALICE, enabled=True, expected_revision=0
    )
    response = await client.post(
        "/v1/sessions", json={"project_id": project_id, "host_id": "h2"}, headers=_headers()
    )
    assert response.status_code == 400
    assert response.json()["error"]["message"] == (
        "Project 'entry-elsewhere' has no directory on host 'h2'. Pass workspace, or set "
        "this host's directory in the project settings."
    )


# ── R-PLACE / R-INHERIT at JSON create ──────────────────────────────────
#
# The host-facing seams (workspace validation, worktree creation,
# canonicalisation) are faked here: the real ones are exercised against a
# fake host in tests/server/integration/test_host_runner_launch_worktree.py
# and the launch tests. These tests pin the placement decision around them.

_HOST = "1" * 32


class _PlacementSeams:
    """Fake host-side seams recording the create path's inputs."""

    def __init__(self) -> None:
        self.sources: list[str] = []
        self.validated: list[str | None] = []
        self.canonical_calls: list[str] = []
        # Rewrites paths under this prefix to ``/elsewhere`` on
        # canonicalisation (a symlinked worktrees directory).
        self.canonical_prefix: str | None = None
        # The agent's boundary root; a workspace outside it fails validation.
        self.allowed: str | None = None

    async def validate(self, *, workspace: str | None, **kwargs: object) -> str:
        """Stand-in for ``_validate_session_workspace`` (the agent boundary)."""
        self.validated.append(workspace)
        assert workspace is not None
        if self.allowed is not None and not (
            workspace == self.allowed or workspace.startswith(self.allowed + "/")
        ):
            raise OmnigentError(
                f"workspace {workspace!r} is outside the agent boundary",
                code=ErrorCode.INVALID_INPUT,
            )
        return workspace

    async def create_worktree(
        self, *, source_repo: str, git: object, **kwargs: object
    ) -> CreatedWorktree:
        """Stand-in for ``_create_session_worktree`` (host git create)."""
        self.sources.append(source_repo)
        branch = str(getattr(git, "branch_name", "feature/x"))
        return CreatedWorktree(f"{source_repo}-worktrees/{branch}", branch)

    async def canonicalise(self, *, worktree_path: str, **kwargs: object) -> str:
        """Stand-in for ``_canonical_worktree_path`` (host ``stat``)."""
        self.canonical_calls.append(worktree_path)
        prefix = self.canonical_prefix
        if prefix is not None and worktree_path.startswith(prefix):
            return "/elsewhere" + worktree_path[len(prefix) :]
        return worktree_path


@pytest.fixture()
def placement(monkeypatch: pytest.MonkeyPatch) -> _PlacementSeams:
    """Install the fake host seams the JSON create calls."""
    from omnigent.server.routes._sessions import orchestration

    seams = _PlacementSeams()
    monkeypatch.setattr(orchestration, "_validate_session_workspace", seams.validate)
    monkeypatch.setattr(orchestration, "_create_session_worktree", seams.create_worktree)
    monkeypatch.setattr(orchestration, "_canonical_worktree_path", seams.canonicalise)
    return seams


async def _post_create(
    client: httpx.AsyncClient, project_id: str, **payload: object
) -> httpx.Response:
    """POST a JSON create for a project, with the builtin agent bound."""
    return await client.post(
        "/v1/sessions",
        json={"project_id": project_id, "agent_id": AGENT_ID, **payload},
        headers=_headers(),
    )


async def test_entry_fills_create_and_records_no_worktree(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """Scenario 1: a no-git create at the entry launches there, worktree null."""
    project_id = await _project(client, "place-entry", {"agent_id": AGENT_ID})
    app.state.project_host_binding_store = Bindings([], [_entry(project_id, _HOST, "/entry")])
    response = await _post_create(client, project_id, host_id=_HOST)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workspace"] == "/entry"
    assert body["worktree"] is None
    assert body["git_branch"] is None
    # The entry was validated (as the target) and re-checked as the boundary.
    assert placement.validated == ["/entry", "/entry"]


async def test_git_create_sources_checkout_and_places_worktree_inside_entry(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """Scenario 3: the worktree comes from the checkout, the session from the entry."""
    project_id = await _project(client, "place-git", {"agent_id": AGENT_ID})
    app.state.project_host_binding_store = Bindings(
        [_binding(project_id, _HOST, "/entry/fork/omnigent")],
        [_entry(project_id, _HOST, "/entry")],
    )
    response = await _post_create(
        client, project_id, host_id=_HOST, git={"branch_name": "feature/x"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert placement.sources == ["/entry/fork/omnigent"]
    assert placement.canonical_calls == ["/entry/fork/omnigent-worktrees/feature/x"]
    assert body["workspace"] == "/entry"
    assert body["worktree"] == "/entry/fork/omnigent-worktrees/feature/x"
    assert body["git_branch"] == "feature/x"


async def test_checkout_still_sources_with_collaboration_off(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """Scenario 23: R-CHECKOUT is ungated — a primary binding sources the worktree."""
    project_id = await _project(client, "place-off", {"agent_id": AGENT_ID})
    # No FeatureFlags and no collaboration switch: bindings do not place, but
    # a registered primary checkout still sources the worktree.
    app.state.project_host_binding_store = Bindings(
        [_binding(project_id, _HOST, "/entry/fork/omnigent")],
        [_entry(project_id, _HOST, "/entry")],
    )
    response = await _post_create(
        client, project_id, host_id=_HOST, git={"branch_name": "feature/x"}
    )
    assert response.status_code == 201, response.text
    assert placement.sources == ["/entry/fork/omnigent"]
    assert response.json()["workspace"] == "/entry"
    assert response.json()["worktree"] == "/entry/fork/omnigent-worktrees/feature/x"


async def test_single_repository_worktree_stays_sibling_of_the_entry(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """Scenario 4: with no binding the entry is the checkout; its sibling T is the session."""
    project_id = await _project(client, "place-single", {"agent_id": AGENT_ID})
    app.state.project_host_binding_store = Bindings([], [_entry(project_id, _HOST, "/repo")])
    response = await _post_create(
        client, project_id, host_id=_HOST, git={"branch_name": "feature/x"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert placement.sources == ["/repo"]
    assert body["workspace"] == "/repo-worktrees/feature/x"
    assert body["worktree"] == "/repo-worktrees/feature/x"


async def test_symlinked_worktrees_directory_keeps_the_session_outside(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """Scenario 22: canonical T outside the entry launches there."""
    project_id = await _project(client, "place-symlink", {"agent_id": AGENT_ID})
    app.state.project_host_binding_store = Bindings(
        [_binding(project_id, _HOST, "/entry/fork/omnigent")],
        [_entry(project_id, _HOST, "/entry")],
    )
    placement.canonical_prefix = "/entry/fork/omnigent-worktrees"
    response = await _post_create(
        client, project_id, host_id=_HOST, git={"branch_name": "feature/x"}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workspace"] == "/elsewhere/feature/x"
    assert body["worktree"] == "/elsewhere/feature/x"


async def test_agent_boundary_keeps_the_session_in_the_bound_worktree(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """Scenario 21: the entry fails the agent boundary, so the session stays at T."""
    project_id = await _project(client, "place-boundary", {"agent_id": AGENT_ID})
    worktree = "/entry/fork/omnigent/worktrees/x"
    app.state.project_host_binding_store = Bindings([], [_entry(project_id, _HOST, "/entry")])
    placement.allowed = "/entry/fork/omnigent"
    response = await _post_create(
        client,
        project_id,
        host_id=_HOST,
        workspace=worktree,
        git={"branch_name": "feature/x", "existing_worktree": True},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workspace"] == worktree
    assert body["worktree"] == worktree
    # The bind path creates nothing; the worktree came from the caller.
    assert placement.sources == []
    assert placement.validated == [worktree, "/entry"]


async def test_explicit_nested_workspace_without_git_launches_at_the_entry(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """An explicit directory inside the entry launches at the entry, recorded as worktree."""
    project_id = await _project(client, "place-nested", {"agent_id": AGENT_ID})
    app.state.project_host_binding_store = Bindings([], [_entry(project_id, _HOST, "/entry")])
    response = await _post_create(client, project_id, host_id=_HOST, workspace="/entry/nested")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workspace"] == "/entry"
    assert body["worktree"] == "/entry/nested"
    assert body["git_branch"] is None


async def test_child_without_explicit_workspace_inherits_the_parent_worktree(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """Scenario 7: a child takes the parent's worktree and keeps its own directory."""
    project_id = await _project(client, "place-child", {"agent_id": AGENT_ID})
    worktree = "/entry/nested/wt"
    app.state.project_host_binding_store = Bindings([], [_entry(project_id, _HOST, "/entry")])
    parent = await _post_create(
        client,
        project_id,
        host_id=_HOST,
        workspace=worktree,
        git={"branch_name": "feature/x", "existing_worktree": True},
    )
    assert parent.status_code == 201, parent.text
    assert parent.json()["worktree"] == worktree

    child = await client.post(
        "/v1/sessions",
        json={"agent_id": AGENT_ID, "parent_session_id": parent.json()["id"]},
        headers=_headers(),
    )
    assert child.status_code == 201, child.text
    assert child.json()["project_id"] == project_id
    assert child.json()["worktree"] == worktree
    assert child.json()["workspace"] is None
    # The list surface carries the inherited worktree too.
    listed = await client.get("/v1/sessions", headers=_headers())
    row = next(item for item in listed.json()["data"] if item["id"] == parent.json()["id"])
    assert row["worktree"] == worktree


async def test_multipart_create_places_a_nested_workspace_at_the_entry(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundled create shares R-PLACE: a nested directory launches at the entry."""
    from omnigent.server.routes import _session_create_validation as create_validation

    async def _echo_workspace(**kwargs: object) -> str:
        return str(kwargs["workspace"])

    monkeypatch.setattr(
        create_validation, "validate_uploaded_bundle_host_workspace", _echo_workspace
    )
    project_id = await _project(client, "multipart-place", {"agent_id": AGENT_ID})
    app.state.project_host_binding_store = Bindings([], [_entry(project_id, _HOST, "/entry")])
    response = await client.post(
        "/v1/sessions",
        data={
            "metadata": json.dumps(
                {"project_id": project_id, "host_id": _HOST, "workspace": "/entry/nested"}
            )
        },
        files={"bundle": ("agent.tar.gz", build_agent_bundle(name="helper"), "application/gzip")},
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["worktree"] == "/entry/nested"
    session = await client.get(f"/v1/sessions/{response.json()['session_id']}", headers=_headers())
    assert session.status_code == 200, session.text
    assert session.json()["workspace"] == "/entry"
    assert session.json()["worktree"] == "/entry/nested"
