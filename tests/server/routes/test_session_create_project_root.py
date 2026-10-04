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
from omnigent.host.frames import HostLaunchRunnerFrame, HostStatFrame, decode_host_frame
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import session_open_rate
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
BOB = "bob@example.com"
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


def _agent_headers(user: str = ALICE, origin: str = "agent") -> dict[str, str]:
    """Headers for an agent-initiated child create (the runner's POST)."""
    return {**_headers(user), "X-Omnigent-Create-Origin": origin}


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
        apply_calling_defaults=True,
    )
    assert config_root.body.workspace == "/c"
    explicit_host_type = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(project_id=project_id, host_type="external"),
        user_id=ALICE,
        project_store=app.state.project_store,
        fill_host=True,
        apply_calling_defaults=True,
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
    resolved = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(project_id=project_id, host_id="h1"),
        user_id=ALICE,
        project_store=app.state.project_store,
        binding_store=app.state.project_host_binding_store,
        feature_flags=app.state.feature_flags,
        apply_calling_defaults=True,
    )
    assert resolved.body.workspace == "/b"
    filled = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(project_id=project_id),
        user_id=ALICE,
        project_store=app.state.project_store,
        binding_store=app.state.project_host_binding_store,
        feature_flags=app.state.feature_flags,
        fill_host=True,
        apply_calling_defaults=True,
    )
    assert (filled.body.host_id, filled.body.workspace) == ("h1", "/b")


async def test_ambiguous_host_fill_with_binding_roots(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    project_id = await _project(client, "several", {"agent_id": AGENT_ID})
    app.state.project_host_binding_store = Bindings(
        [_binding(project_id, "h1", "/one"), _binding(project_id, "h2", "/two")]
    )
    app.state.feature_flags = FeatureFlags(frozenset({Feature.PROJECT_ASSIGNMENTS}))
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
        apply_calling_defaults=True,
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


class _Preferences:
    """Minimal ``user_preferences_store`` for the collab namespace."""

    def __init__(self, settings: dict[str, object]) -> None:
        self.settings = settings

    def get(self, _user_id: str) -> dict[str, object] | None:
        return {"version": 1, "settings": {"session_collab": self.settings}}


async def test_only_agent_child_creates_spend_the_open_rate_budget(
    client: httpx.AsyncClient,
) -> None:
    """Web-shaped and top-level creates are free; the sixth agent child create is 429."""
    session_open_rate._OPEN_TIMESTAMPS.clear()
    try:
        project_id = await _project(client, "rate", {"agent_id": AGENT_ID})
        parent = await client.post(
            "/v1/sessions", json={"project_id": project_id}, headers=_headers()
        )
        assert parent.status_code == 201, parent.text
        parent_id = parent.json()["id"]
        # Top-level creates never count.
        for index in range(6):
            top = await client.post(
                "/v1/sessions",
                json={"project_id": project_id, "title": f"top-{index}"},
                headers=_headers(),
            )
            assert top.status_code == 201, top.text
        # Web-shaped child creates (no origin header) never count.
        for index in range(10):
            web_child = await client.post(
                "/v1/sessions",
                json={
                    "agent_id": AGENT_ID,
                    "parent_session_id": parent_id,
                    "title": f"web-child-{index}",
                },
                headers=_headers(),
            )
            assert web_child.status_code == 201, web_child.text
        # Agent-initiated child creates spend the budget.
        for index in range(10):
            child = await client.post(
                "/v1/sessions",
                json={
                    "agent_id": AGENT_ID,
                    "parent_session_id": parent_id,
                    "title": f"child-{index}",
                },
                headers=_agent_headers(),
            )
            assert child.status_code == 201, child.text
        refused = await client.post(
            "/v1/sessions",
            json={
                "agent_id": AGENT_ID,
                "parent_session_id": parent_id,
                "title": "child-10",
            },
            headers=_agent_headers(),
        )
        assert refused.status_code == 429, refused.text
        assert "setting: 10 per 1 minute" in refused.json()["detail"]
        # With the window full, a web-shaped create is still admitted.
        web = await client.post(
            "/v1/sessions",
            json={
                "agent_id": AGENT_ID,
                "parent_session_id": parent_id,
                "title": "web-after-limit",
            },
            headers=_headers(),
        )
        assert web.status_code == 201, web.text
    finally:
        session_open_rate._OPEN_TIMESTAMPS.clear()


async def test_named_sub_agent_creates_do_not_spend_the_budget(
    client: httpx.AsyncClient,
) -> None:
    """Named sub-agent dispatch is not an open and never counts."""
    session_open_rate._OPEN_TIMESTAMPS.clear()
    try:
        project_id = await _project(client, "named", {"agent_id": AGENT_ID})
        parent = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps({"project_id": project_id})},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle(name="named-parent", sub_agents=[{"name": "worker"}]),
                    "application/gzip",
                )
            },
            headers=_headers(),
        )
        assert parent.status_code == 201, parent.text
        parent_id = parent.json()["session_id"]
        parent_agent = await client.get(f"/v1/sessions/{parent_id}/agent", headers=_headers())
        assert parent_agent.status_code == 200, parent_agent.text
        for index in range(6):
            child = await client.post(
                "/v1/sessions",
                json={
                    "agent_id": parent_agent.json()["id"],
                    "parent_session_id": parent_id,
                    "sub_agent_name": "worker",
                    "title": f"named-{index}",
                },
                headers=_headers(),
            )
            assert child.status_code == 201, child.text
    finally:
        session_open_rate._OPEN_TIMESTAMPS.clear()


async def test_multipart_child_create_spends_the_budget(
    client: httpx.AsyncClient,
) -> None:
    """The agent's multipart (config_path) child create counts too."""
    session_open_rate._OPEN_TIMESTAMPS.clear()
    try:
        project_id = await _project(client, "multipart-rate", {"agent_id": AGENT_ID})
        parent = await client.post(
            "/v1/sessions", json={"project_id": project_id}, headers=_headers()
        )
        assert parent.status_code == 201, parent.text
        parent_id = parent.json()["id"]
        for index in range(10):
            response = await client.post(
                "/v1/sessions",
                data={"metadata": json.dumps({"parent_session_id": parent_id})},
                files={
                    "bundle": (
                        "agent.tar.gz",
                        build_agent_bundle(name=f"helper-{index}"),
                        "application/gzip",
                    )
                },
                headers=_agent_headers(),
            )
            assert response.status_code == 201, response.text
        refused = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps({"parent_session_id": parent_id})},
            files={
                "bundle": (
                    "agent.tar.gz",
                    build_agent_bundle(name="helper-refused"),
                    "application/gzip",
                )
            },
            headers=_agent_headers(),
        )
        assert refused.status_code == 429, refused.text
        assert "Opening sessions too fast" in refused.json()["detail"]
    finally:
        session_open_rate._OPEN_TIMESTAMPS.clear()


async def test_changed_open_rate_setting_applies_to_child_creates(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """A changed ``openRateCount`` takes effect on the next child create."""
    session_open_rate._OPEN_TIMESTAMPS.clear()
    prefs = _Preferences({"openRateCount": 2})
    app.state.user_preferences_store = prefs
    try:
        project_id = await _project(client, "rate-setting", {"agent_id": AGENT_ID})
        parent = await client.post(
            "/v1/sessions", json={"project_id": project_id}, headers=_headers()
        )
        assert parent.status_code == 201, parent.text
        for index in range(2):
            child = await client.post(
                "/v1/sessions",
                json={"agent_id": AGENT_ID, "parent_session_id": parent.json()["id"]},
                headers=_agent_headers(),
            )
            assert child.status_code == 201, child.text
        refused = await client.post(
            "/v1/sessions",
            json={"agent_id": AGENT_ID, "parent_session_id": parent.json()["id"]},
            headers=_agent_headers(),
        )
        assert refused.status_code == 429, refused.text
        assert "setting: 2 per 1 minute" in refused.json()["detail"]
        prefs.settings = {"openRateCount": 10}
        admitted = await client.post(
            "/v1/sessions",
            json={"agent_id": AGENT_ID, "parent_session_id": parent.json()["id"]},
            headers=_agent_headers(),
        )
        assert admitted.status_code == 201, admitted.text
    finally:
        session_open_rate._OPEN_TIMESTAMPS.clear()


async def test_child_create_authorizes_the_parent_before_charging_rate(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """A forged agent child create is refused without spending the owner's window."""
    session_open_rate._OPEN_TIMESTAMPS.clear()
    prefs = _Preferences({"openRateCount": 1})
    app.state.user_preferences_store = prefs
    bob = "bob@example.com"
    try:
        project_id = await _project(client, "forged-rate", {"agent_id": AGENT_ID})
        parent = await client.post(
            "/v1/sessions", json={"project_id": project_id}, headers=_headers()
        )
        assert parent.status_code == 201, parent.text
        parent_id = parent.json()["id"]
        forged = await client.post(
            "/v1/sessions",
            json={"agent_id": AGENT_ID, "parent_session_id": parent_id},
            headers=_agent_headers(bob),
        )
        assert forged.status_code == 404, forged.text
        # A web-shaped forged create is not admitted by this path at all, but
        # the create route's own parent authorization still refuses it.
        web_forged = await client.post(
            "/v1/sessions",
            json={"agent_id": AGENT_ID, "parent_session_id": parent_id},
            headers=_headers(bob),
        )
        assert web_forged.status_code == 404, web_forged.text
        admitted = await client.post(
            "/v1/sessions",
            json={"agent_id": AGENT_ID, "parent_session_id": parent_id},
            headers=_agent_headers(),
        )
        assert admitted.status_code == 201, admitted.text
        refused = await client.post(
            "/v1/sessions",
            json={"agent_id": AGENT_ID, "parent_session_id": parent_id},
            headers=_agent_headers(),
        )
        assert refused.status_code == 429, refused.text
    finally:
        session_open_rate._OPEN_TIMESTAMPS.clear()


async def test_create_origin_header_value_is_case_insensitive(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    """An ``AGENT`` origin still spends the budget (value matched case-insensitively)."""
    session_open_rate._OPEN_TIMESTAMPS.clear()
    app.state.user_preferences_store = _Preferences({"openRateCount": 1})
    try:
        project_id = await _project(client, "origin-case", {"agent_id": AGENT_ID})
        parent = await client.post(
            "/v1/sessions", json={"project_id": project_id}, headers=_headers()
        )
        assert parent.status_code == 201, parent.text
        parent_id = parent.json()["id"]
        first = await client.post(
            "/v1/sessions",
            json={"agent_id": AGENT_ID, "parent_session_id": parent_id},
            headers=_agent_headers(origin="AGENT"),
        )
        assert first.status_code == 201, first.text
        second = await client.post(
            "/v1/sessions",
            json={"agent_id": AGENT_ID, "parent_session_id": parent_id},
            headers=_agent_headers(origin="Agent"),
        )
        assert second.status_code == 429, second.text
    finally:
        session_open_rate._OPEN_TIMESTAMPS.clear()


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
        "roots": [
            {"host_id": "h1", "workspace": "/c", "source": "config", "checkout": None},
            {"host_id": "h2", "workspace": "/b", "source": "binding", "checkout": "/b"},
        ],
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
        apply_calling_defaults=True,
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
        apply_calling_defaults=True,
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
        # Host-canonical path per raw workspace for the ownership lookup.
        self.canonical_workspaces: dict[str, str] = {}

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


class _WorkspaceHost:
    """Fake host registry answering ``host.stat`` and runner launches."""

    def __init__(self, canonical_workspaces: dict[str, str]) -> None:
        self.canonical_workspaces = canonical_workspaces
        self.conn = SimpleNamespace(host_id=_HOST, pending_stats={}, pending_launches={})
        self.stats: list[str] = []

    def get(self, host_id: str) -> object | None:
        """The fake connection for the test host, ``None`` for other hosts."""
        return self.conn if host_id == _HOST else None

    async def admit_launch(self, conn: object, session_id: str) -> None:
        """Accept the runner launch admission for the fake host."""

    def send_text(self, conn: object, frame: str) -> None:
        """Resolve a pending stat or launch frame with a canned success."""
        decoded = decode_host_frame(frame)
        if isinstance(decoded, HostStatFrame):
            self.stats.append(decoded.path)
            future = self.conn.pending_stats.pop(decoded.request_id, None)
            if future is not None and not future.done():
                future.set_result(
                    {
                        "status": "ok",
                        "exists": True,
                        "type": "directory",
                        "canonical_path": self.canonical_workspaces.get(
                            decoded.path, decoded.path
                        ),
                        "error": None,
                    }
                )
            return
        assert isinstance(decoded, HostLaunchRunnerFrame)
        future = self.conn.pending_launches.pop(decoded.request_id, None)
        if future is not None and not future.done():
            future.set_result({"status": "ok"})


@pytest.fixture()
def placement(app: FastAPI, monkeypatch: pytest.MonkeyPatch, db_uri: str) -> _PlacementSeams:
    """Install the fake host seams the JSON create calls."""
    from omnigent.server.routes._sessions import orchestration

    seams = _PlacementSeams()
    monkeypatch.setattr(orchestration, "_validate_session_workspace", seams.validate)
    monkeypatch.setattr(orchestration, "_create_session_worktree", seams.create_worktree)
    monkeypatch.setattr(orchestration, "_canonical_worktree_path", seams.canonicalise)
    app.state.host_registry = _WorkspaceHost(seams.canonical_workspaces)
    # The child workspace lookup authorizes its target host first.
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(_HOST, "desktop", ALICE)
    app.state.host_store = host_store
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
    # The entry is validated once, as the target.
    assert placement.validated == ["/entry"]


async def test_git_create_sources_checkout_and_launches_in_the_worktree(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """Scenario 3: the worktree comes from the checkout and the session launches in it."""
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
    created = "/entry/fork/omnigent-worktrees/feature/x"
    assert placement.sources == ["/entry/fork/omnigent"]
    assert placement.canonical_calls == [created]
    assert body["workspace"] == created
    assert body["worktree"] == created
    assert body["git_branch"] == "feature/x"
    # The picked directory and the checkout meet the boundary; the worktree
    # inherits the checkout's pass.
    assert placement.validated == ["/entry", "/entry/fork/omnigent"]


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
    assert response.json()["workspace"] == "/entry/fork/omnigent-worktrees/feature/x"
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


async def test_bound_worktree_inside_the_entry_launches_in_it(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """Scenario 21: a bound worktree launches in place; the entry is never checked."""
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
    assert placement.validated == [worktree]


async def test_created_worktree_outside_the_agent_boundary_still_launches(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """A created worktree is not re-checked against the agent's boundary.

    Its source passed; a server-made worktree may sit outside the agent's
    declared cwd (here its directory is a symlink out of the entry).
    """
    project_id = await _project(client, "place-escape", {"agent_id": AGENT_ID})
    app.state.project_host_binding_store = Bindings(
        [_binding(project_id, _HOST, "/entry/fork/omnigent")],
        [_entry(project_id, _HOST, "/entry")],
    )
    placement.allowed = "/entry"
    placement.canonical_prefix = "/entry/fork/omnigent-worktrees"
    response = await _post_create(
        client, project_id, host_id=_HOST, git={"branch_name": "feature/x"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["workspace"] == "/elsewhere/feature/x"
    assert placement.validated == ["/entry", "/entry/fork/omnigent"]


@pytest.mark.parametrize(
    ("checkout", "allowed"),
    [("/entry/fork/omnigent", True), ("/other/omnigent", False)],
)
async def test_project_checkout_meets_the_agent_boundary_before_the_cut(
    app: FastAPI,
    client: httpx.AsyncClient,
    placement: _PlacementSeams,
    checkout: str,
    allowed: bool,
) -> None:
    """A create naming its project validates the checkout it swaps to.

    The entry passes the agent's boundary; a checkout outside it is refused
    before the host is asked to cut a worktree.
    """
    project_id = await _project(client, "place-checkout-scope", {"agent_id": AGENT_ID})
    app.state.project_host_binding_store = Bindings(
        [_binding(project_id, _HOST, checkout)],
        [_entry(project_id, _HOST, "/entry")],
    )
    placement.allowed = "/entry"
    response = await _post_create(
        client, project_id, host_id=_HOST, git={"branch_name": "feature/x"}
    )
    assert placement.validated == ["/entry", checkout]
    if allowed:
        assert response.status_code == 201, response.text
        assert placement.sources == [checkout]
    else:
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "invalid_input"
        assert placement.sources == []


async def test_explicit_nested_workspace_without_git_launches_there(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """An explicit directory inside the entry launches there and records no worktree."""
    project_id = await _project(client, "place-nested", {"agent_id": AGENT_ID})
    app.state.project_host_binding_store = Bindings([], [_entry(project_id, _HOST, "/entry")])
    response = await _post_create(client, project_id, host_id=_HOST, workspace="/entry/nested")
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workspace"] == "/entry/nested"
    assert body["worktree"] is None
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


async def test_child_with_git_options_records_its_own_created_worktree(
    app: FastAPI, client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """F-B1: a child that creates its own worktree keeps it, never the parent's.

    R-INHERIT applies only to a child with no ``git`` options; one that
    creates or binds its own worktree goes through R-PLACE instead, even
    when it names no explicit ``workspace`` (a project child defaults its
    workspace to the project root, same as a top-level create).
    """
    project_id = await _project(client, "place-child-git", {"agent_id": AGENT_ID})
    parent_worktree = "/entry/nested/parent-wt"
    app.state.project_host_binding_store = Bindings([], [_entry(project_id, _HOST, "/entry")])
    parent = await _post_create(
        client,
        project_id,
        host_id=_HOST,
        workspace=parent_worktree,
        git={"branch_name": "feature/parent", "existing_worktree": True},
    )
    assert parent.status_code == 201, parent.text
    assert parent.json()["worktree"] == parent_worktree

    child = await _post_create(
        client,
        project_id,
        host_id=_HOST,
        parent_session_id=parent.json()["id"],
        git={"branch_name": "feature/child"},
    )
    assert child.status_code == 201, child.text
    assert child.json()["worktree"] != parent_worktree
    assert child.json()["worktree"] == "/entry-worktrees/feature/child"
    assert child.json()["git_branch"] == "feature/child"


def _calling_config(workspace: str, model: str, effort: str) -> dict[str, object]:
    """A project root on the test host whose per-host row names model / effort."""
    return {
        "agent_id": AGENT_ID,
        "host_id": _HOST,
        "workspace": workspace,
        "calling_defaults": {
            _HOST: {"harnesses": {"claude-sdk": {"model": model, "effort": effort}}}
        },
    }


async def _child(client: httpx.AsyncClient, parent_id: str, **payload: object) -> httpx.Response:
    """POST an agent-initiated child create of the builtin agent."""
    return await client.post(
        "/v1/sessions",
        json={"agent_id": AGENT_ID, "parent_session_id": parent_id, **payload},
        headers=_headers(),
    )


async def test_child_workspace_adopts_owning_project_and_calling_defaults(
    client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """A child workspace inside another project's root takes that project / its defaults."""
    project_a = await _project(
        client, "child-root-a", _calling_config("/repo-a", "model-a", "high")
    )
    project_b = await _project(
        client, "child-root-b", _calling_config("/repo-b", "model-b", "low")
    )
    parent = await _post_create(client, project_a, host_id=_HOST)
    assert parent.status_code == 201, parent.text
    child = await _child(client, parent.json()["id"], host_id=_HOST, workspace="/repo-b/nested")
    assert child.status_code == 201, child.text
    body = child.json()
    assert body["project_id"] == project_b
    assert body["model_override"] == "model-b"
    assert body["reasoning_effort"] == "low"


async def test_child_explicit_project_beats_the_workspace_owner(
    client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """An explicit project_id on the child wins over the workspace's owner."""
    project_a = await _project(
        client, "child-pin-a", _calling_config("/repo-a", "model-a", "high")
    )
    await _project(client, "child-pin-b", _calling_config("/repo-b", "model-b", "low"))
    parent = await _post_create(client, project_a, host_id=_HOST)
    assert parent.status_code == 201, parent.text
    child = await _child(
        client,
        parent.json()["id"],
        project_id=project_a,
        host_id=_HOST,
        workspace="/repo-b/nested",
    )
    assert child.status_code == 201, child.text
    assert child.json()["project_id"] == project_a
    assert child.json()["model_override"] == "model-a"
    assert child.json()["reasoning_effort"] == "high"


async def test_child_workspace_in_no_project_keeps_the_parents_project(
    client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """A workspace no project owns leaves the child in the parent's project."""
    project_a = await _project(
        client, "child-unowned-a", _calling_config("/repo-a", "model-a", "high")
    )
    await _project(client, "child-unowned-b", _calling_config("/repo-b", "model-b", "low"))
    parent = await _post_create(client, project_a, host_id=_HOST)
    assert parent.status_code == 201, parent.text
    child = await _child(client, parent.json()["id"], host_id=_HOST, workspace="/unowned/nested")
    assert child.status_code == 201, child.text
    assert child.json()["project_id"] == project_a
    assert child.json()["model_override"] == "model-a"


async def test_child_without_workspace_keeps_the_parents_project(
    client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """A child that names no workspace keeps the parent's project and defaults."""
    project_a = await _project(
        client, "child-nows-a", _calling_config("/repo-a", "model-a", "high")
    )
    await _project(client, "child-nows-b", _calling_config("/repo-b", "model-b", "low"))
    parent = await _post_create(client, project_a, host_id=_HOST)
    assert parent.status_code == 201, parent.text
    child = await _child(client, parent.json()["id"])
    assert child.status_code == 201, child.text
    assert child.json()["project_id"] == project_a
    assert child.json()["model_override"] == "model-a"


async def test_child_workspace_takes_the_deepest_owning_root(
    client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """When roots nest, the deepest project root owning the workspace wins."""
    project_outer = await _project(
        client, "child-deep-outer", _calling_config("/repo", "model-outer", "high")
    )
    project_inner = await _project(
        client, "child-deep-inner", _calling_config("/repo/sub", "model-inner", "low")
    )
    parent = await _post_create(client, project_outer, host_id=_HOST)
    assert parent.status_code == 201, parent.text
    child = await _child(client, parent.json()["id"], host_id=_HOST, workspace="/repo/sub/nested")
    assert child.status_code == 201, child.text
    body = child.json()
    assert body["project_id"] == project_inner
    assert body["model_override"] == "model-inner"


async def test_child_workspace_tie_keeps_the_parents_project(
    client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """Two projects sharing one root leave the child in the parent's project."""
    project_a = await _project(client, "child-tie-a", _calling_config("/repo", "model-a", "high"))
    await _project(client, "child-tie-b", _calling_config("/repo", "model-b", "low"))
    parent = await _post_create(client, project_a, host_id=_HOST)
    assert parent.status_code == 201, parent.text
    child = await _child(client, parent.json()["id"], host_id=_HOST, workspace="/repo/nested")
    assert child.status_code == 201, child.text
    assert child.json()["project_id"] == project_a
    assert child.json()["model_override"] == "model-a"


async def test_child_workspace_is_canonicalised_before_the_owner_lookup(
    client: httpx.AsyncClient, placement: _PlacementSeams
) -> None:
    """A raw child workspace takes its host-canonical owner, not the raw one."""
    project_a = await _project(
        client, "child-canon-a", _calling_config("/repo-a", "model-a", "high")
    )
    project_b = await _project(
        client, "child-canon-b", _calling_config("/repo-b", "model-b", "low")
    )
    placement.canonical_workspaces["/repo-a/../repo-b/nested"] = "/repo-b/nested"
    parent = await _post_create(client, project_a, host_id=_HOST)
    assert parent.status_code == 201, parent.text
    child = await _child(
        client, parent.json()["id"], host_id=_HOST, workspace="/repo-a/../repo-b/nested"
    )
    assert child.status_code == 201, child.text
    assert child.json()["project_id"] == project_b
    assert child.json()["model_override"] == "model-b"


async def test_child_workspace_lookup_failure_keeps_the_parents_project(
    app: FastAPI,
    client: httpx.AsyncClient,
    placement: _PlacementSeams,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing ownership lookup still files the child under the parent's project."""
    project_a = await _project(
        client, "child-fail-a", _calling_config("/repo-a", "model-a", "high")
    )
    await _project(client, "child-fail-b", _calling_config("/repo-b", "model-b", "low"))
    parent = await _post_create(client, project_a, host_id=_HOST)
    assert parent.status_code == 201, parent.text

    def unavailable(*, user_id: str | None) -> list[object]:
        raise RuntimeError("project store unavailable")

    monkeypatch.setattr(app.state.project_store, "list", unavailable)
    child = await _child(client, parent.json()["id"], host_id=_HOST, workspace="/repo-b/nested")
    assert child.status_code == 201, child.text
    assert child.json()["project_id"] == project_a
    assert child.json()["model_override"] == "model-a"


async def test_child_foreign_host_is_refused_before_any_stat(
    app: FastAPI,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A child naming another user's host is rejected without probing it.

    The workspace-owner lookup runs before the normal host validation, so it
    must authorize its target host the same way. Alice's child names Bob's
    host: the create is rejected exactly as it is on a tree without the
    lookup, and Bob's host never sees a ``host.stat``.
    """
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(_HOST, "bob-desktop", BOB)
    app.state.host_store = host_store
    registry = _WorkspaceHost({})
    app.state.host_registry = registry

    parent = await client.post("/v1/sessions", json={"agent_id": AGENT_ID}, headers=_headers())
    assert parent.status_code == 201, parent.text

    child = await _child(client, parent.json()["id"], host_id=_HOST, workspace="/repo-a/nested")
    assert child.status_code == 403, child.text
    assert child.json()["detail"] == "not your host"
    assert registry.stats == []


async def test_multipart_create_launches_a_nested_workspace_there(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundled create shares the rule: a nested directory launches where picked."""
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
    assert response.json()["worktree"] is None
    session = await client.get(f"/v1/sessions/{response.json()['session_id']}", headers=_headers())
    assert session.status_code == 200, session.text
    assert session.json()["workspace"] == "/entry/nested"
    assert session.json()["worktree"] is None
