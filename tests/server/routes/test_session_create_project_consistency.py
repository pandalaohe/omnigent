"""Project-aware session creation defaults and consistency boundaries."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI

from omnigent.db.utils import builtin_agent_id
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.routes._session_create_validation import (
    resolve_project_session_create,
)
from omnigent.server.schemas import (
    ProjectSessionCreateRequest,
    SessionCreateRequest,
    SessionResponse,
)
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_model_catalog_cache_store import HostModelCatalogCacheStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"
CUSTOM_AGENT_ID = "187b7cb7ac30abf4debfaa578d052ec6"
OTHER_AGENT_ID = "287b7cb7ac30abf4debfaa578d052ec6"
BUILTIN_AGENT_NAME = "generic-builtin"
BUILTIN_AGENT_ID = builtin_agent_id(BUILTIN_AGENT_NAME)


@pytest.fixture()
def project_create_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db_uri)
    agent_store.create(CUSTOM_AGENT_ID, "project-custom", f"{CUSTOM_AGENT_ID}/bundle")
    agent_store.create(OTHER_AGENT_ID, "explicit-custom", f"{OTHER_AGENT_ID}/bundle")
    agent_store.create(
        BUILTIN_AGENT_ID,
        BUILTIN_AGENT_NAME,
        f"{BUILTIN_AGENT_ID}/bundle",
    )
    return create_app(
        agent_store=agent_store,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def project_create_client(project_create_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=project_create_app), base_url="http://test"
    ) as client:
        yield client


def _headers(user: str = ALICE) -> dict[str, str]:
    return {"X-Forwarded-Email": user}


async def _project(
    client: httpx.AsyncClient, config: dict[str, object], *, user: str = ALICE
) -> str:
    response = await client.post(
        "/v1/projects",
        json={"name": f"project-{user}", "config": config},
        headers=_headers(user),
    )
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def test_omitted_values_are_filled_and_membership_is_immediate(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(
        project_create_client,
        {"agent_id": CUSTOM_AGENT_ID, "workspace": "/work/project"},
    )
    response = await project_create_client.post(
        "/v1/sessions", json={"project_id": project_id}, headers=_headers()
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["agent_id"] == CUSTOM_AGENT_ID
    assert body["workspace"] == "/work/project"
    assert body["project_id"] == project_id


async def test_explicit_values_are_not_overridden(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(
        project_create_client,
        {"agent_id": CUSTOM_AGENT_ID, "workspace": "/work/project"},
    )
    response = await project_create_client.post(
        "/v1/sessions",
        json={
            "project_id": project_id,
            "agent_id": OTHER_AGENT_ID,
            "workspace": "/work/project/subdir",
        },
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["agent_id"] == OTHER_AGENT_ID
    assert response.json()["workspace"] == "/work/project/subdir"


async def test_explicit_null_is_not_defaulted(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(project_create_client, {"agent_id": CUSTOM_AGENT_ID})
    response = await project_create_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "agent_id": None},
        headers=_headers(),
    )
    assert response.status_code == 400
    assert "agent_id is required" in response.text


async def test_unknown_and_unowned_project_are_404(
    project_create_client: httpx.AsyncClient,
) -> None:
    bob_project = await _project(project_create_client, {"agent_id": CUSTOM_AGENT_ID}, user=BOB)
    for project_id in ("0" * 32, bob_project):
        response = await project_create_client.post(
            "/v1/sessions",
            json={"project_id": project_id},
            headers=_headers(),
        )
        assert response.status_code == 404
        assert "Project not found" in response.text


async def test_workspace_outside_configured_root_is_allowed_silently(
    project_create_client: httpx.AsyncClient,
) -> None:
    """A per-session working directory outside the project root is a deliberate
    choice — the session is created with no warning surfaced."""
    project_id = await _project(
        project_create_client,
        {"agent_id": CUSTOM_AGENT_ID, "workspace": "/work/project"},
    )
    payload = {
        "project_id": project_id,
        "agent_id": CUSTOM_AGENT_ID,
        "workspace": "/work/other",
    }
    response = await project_create_client.post("/v1/sessions", json=payload, headers=_headers())
    assert response.status_code == 201, response.text
    assert "warnings" not in response.json()


@pytest.mark.parametrize("agent_id", [BUILTIN_AGENT_ID, OTHER_AGENT_ID])
async def test_explicit_agent_differing_from_pin_is_allowed_silently(
    project_create_client: httpx.AsyncClient,
    agent_id: str,
) -> None:
    """An explicit agent differing from the project's pin (builtin or custom)
    binds as requested and surfaces no warning."""
    project_id = await _project(project_create_client, {"agent_id": CUSTOM_AGENT_ID})
    response = await project_create_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "agent_id": agent_id},
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["agent_id"] == agent_id
    assert "warnings" not in response.json()


async def test_fork_of_mismatched_session_stays_clean(
    project_create_client: httpx.AsyncClient,
) -> None:
    """Forking a session whose agent differs from its project files into the
    same project and surfaces no warning."""
    project_id = await _project(project_create_client, {"agent_id": CUSTOM_AGENT_ID})
    created = await project_create_client.post(
        "/v1/sessions", json={"agent_id": BUILTIN_AGENT_ID}, headers=_headers()
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    moved = await project_create_client.patch(
        f"/v1/sessions/{session_id}",
        json={"project_id": project_id},
        headers=_headers(),
    )
    assert moved.status_code == 200, moved.text
    fork = await project_create_client.post(
        f"/v1/sessions/{session_id}/fork", json={}, headers=_headers()
    )
    assert fork.status_code == 201, fork.text
    body = fork.json()
    assert "warnings" not in body
    assert body["project_id"] == project_id


async def test_without_project_id_response_is_unchanged(
    project_create_client: httpx.AsyncClient,
) -> None:
    response = await project_create_client.post(
        "/v1/sessions", json={"agent_id": CUSTOM_AGENT_ID}, headers=_headers()
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert "warnings" not in body
    # Full response round-trip pins the legacy response projection rather than
    # checking only the fields touched by this feature.
    assert body == SessionResponse.model_validate(body).model_dump(mode="json")


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({}, {"type": "missing", "loc": ["agent_id"], "msg": "Field required"}),
        (
            {"agent_id": None},
            {
                "type": "string_type",
                "loc": ["agent_id"],
                "msg": "Input should be a valid string",
            },
        ),
    ],
)
async def test_without_project_id_retains_legacy_agent_validation_detail(
    project_create_client: httpx.AsyncClient,
    payload: dict[str, object],
    expected: dict[str, object],
) -> None:
    response = await project_create_client.post("/v1/sessions", json=payload, headers=_headers())
    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert {key: error[key] for key in ("type", "loc", "msg")} == expected
    assert "agent_id" in SessionCreateRequest.model_json_schema()["required"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"project_id": None}, {"type": "missing", "loc": ["agent_id"], "msg": "Field required"}),
        (
            {"project_id": None, "agent_id": None},
            {
                "type": "string_type",
                "loc": ["agent_id"],
                "msg": "Input should be a valid string",
            },
        ),
    ],
)
async def test_null_project_id_matches_key_absent_422(
    project_create_client: httpx.AsyncClient,
    payload: dict[str, object],
    expected: dict[str, object],
) -> None:
    """A null project_id body keeps the legacy 422 contract identical.

    Only the ``input`` echo may differ between the two responses: a
    missing-field error echoes the raw request body verbatim, exactly as the
    legacy shape did for the same body.
    """
    key_absent = {key: value for key, value in payload.items() if key != "project_id"}
    absent_response = await project_create_client.post(
        "/v1/sessions", json=key_absent, headers=_headers()
    )
    null_response = await project_create_client.post(
        "/v1/sessions", json=payload, headers=_headers()
    )
    assert absent_response.status_code == 422
    assert null_response.status_code == 422

    def _without_input(detail: list[dict[str, object]]) -> list[dict[str, object]]:
        return [{key: value for key, value in error.items() if key != "input"} for error in detail]

    assert _without_input(null_response.json()["detail"]) == _without_input(
        absent_response.json()["detail"]
    )
    error = null_response.json()["detail"][0]
    assert {key: error[key] for key in ("type", "loc", "msg")} == expected
    # The missing-field echo carries the raw body, matching the legacy shape.
    assert error["input"] == (payload if expected["type"] == "missing" else None)


async def test_null_project_id_with_valid_agent_matches_key_absent_create(
    project_create_client: httpx.AsyncClient,
) -> None:
    """A null project_id create behaves exactly like one without the key."""
    absent_response = await project_create_client.post(
        "/v1/sessions", json={"agent_id": CUSTOM_AGENT_ID}, headers=_headers()
    )
    null_response = await project_create_client.post(
        "/v1/sessions",
        json={"project_id": None, "agent_id": CUSTOM_AGENT_ID},
        headers=_headers(),
    )
    assert absent_response.status_code == 201, absent_response.text
    assert null_response.status_code == 201, null_response.text
    body = null_response.json()
    assert "warnings" not in body
    assert body["project_id"] is None
    volatile = {"id", "created_at", "updated_at", "root_conversation_id"}
    assert {key: value for key, value in body.items() if key not in volatile} == {
        key: value for key, value in absent_response.json().items() if key not in volatile
    }


@pytest.mark.parametrize(
    ("raw_body", "input_value"),
    [("null", None), ("5", 5), ('"project_id"', "project_id")],
)
async def test_non_object_json_retains_exact_legacy_422(
    project_create_client: httpx.AsyncClient,
    raw_body: str,
    input_value: object,
) -> None:
    response = await project_create_client.post(
        "/v1/sessions",
        content=raw_body,
        headers={**_headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json()["detail"] == [
        {
            "type": "model_type",
            "loc": [],
            "msg": "Input should be a valid dictionary or instance of SessionCreateRequest",
            "input": input_value,
            "url": "https://errors.pydantic.dev/2.13/v/model_type",
        }
    ]


async def test_legacy_multi_error_order_starts_with_agent_id(
    project_create_client: httpx.AsyncClient,
) -> None:
    response = await project_create_client.post(
        "/v1/sessions",
        json={"agent_id": None, "labels": "not-a-dict"},
        headers=_headers(),
    )
    assert response.status_code == 422
    projected = [
        {key: error[key] for key in ("type", "loc", "msg")} for error in response.json()["detail"]
    ]
    assert projected == [
        {
            "type": "string_type",
            "loc": ["agent_id"],
            "msg": "Input should be a valid string",
        },
        {
            "type": "dict_type",
            "loc": ["labels"],
            "msg": "Input should be a valid dictionary",
        },
    ]


@pytest.mark.parametrize("config_field", [("workspace", 123), ("git", "yes")])
async def test_malformed_project_config_is_structured_400(
    project_create_client: httpx.AsyncClient,
    config_field: tuple[str, object],
) -> None:
    field, value = config_field
    project_id = await _project(
        project_create_client,
        {"agent_id": CUSTOM_AGENT_ID, field: value},
    )
    response = await project_create_client.post(
        "/v1/sessions", json={"project_id": project_id}, headers=_headers()
    )
    assert response.status_code == 400
    assert f"Invalid project config field '{field}'" in response.text


async def test_explicit_null_workspace_is_not_defaulted(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(
        project_create_client,
        {"agent_id": CUSTOM_AGENT_ID, "workspace": "/work/project"},
    )
    response = await project_create_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "workspace": None},
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    assert response.json()["workspace"] is None


async def test_git_default_fill_with_differing_workspace(db_uri: str) -> None:
    project_store = SqlAlchemyProjectStore(db_uri)
    project = project_store.create(
        "487b7cb7ac30abf4debfaa578d052ec6",
        "git-defaults",
        ALICE,
        {
            "agent_id": CUSTOM_AGENT_ID,
            "workspace": "/work/project",
            "git": {"branch_name": "feature/project"},
        },
    )
    resolved = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(
            project_id=project.id,
            host_id="host_abc",
            workspace="/work/other",
        ),
        user_id=ALICE,
        project_store=project_store,
        apply_calling_defaults=True,
    )
    # The omitted git block is default-filled from config; an explicit workspace
    # outside the project root is a deliberate choice and is left untouched.
    assert resolved.body.git is not None
    assert resolved.body.git.branch_name == "feature/project"
    assert resolved.body.workspace == "/work/other"


async def test_multipart_create_defaults_workspace_and_files_atomically(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(project_create_client, {"workspace": "/work/upload"})
    response = await project_create_client.post(
        "/v1/sessions",
        data={"metadata": f'{{"project_id":"{project_id}"}}'},
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="project-upload"),
                "application/gzip",
            )
        },
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    session = await project_create_client.get(
        f"/v1/sessions/{response.json()['session_id']}", headers=_headers()
    )
    assert session.status_code == 200, session.text
    assert session.json()["workspace"] == "/work/upload"
    assert session.json()["project_id"] == project_id


async def test_multipart_malformed_project_config_is_structured_400(
    project_create_client: httpx.AsyncClient,
) -> None:
    project_id = await _project(project_create_client, {"workspace": 123})
    response = await project_create_client.post(
        "/v1/sessions",
        data={"metadata": f'{{"project_id":"{project_id}"}}'},
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(name="malformed-project-upload"),
                "application/gzip",
            )
        },
        headers=_headers(),
    )
    assert response.status_code == 400
    assert "Invalid project config field 'workspace'" in response.text


async def test_shared_chokepoint_is_reusable_by_non_route_creators(
    db_uri: str,
) -> None:
    """Non-route creators can pass their create body through the same resolver."""
    project_store = SqlAlchemyProjectStore(db_uri)
    project = project_store.create(
        "387b7cb7ac30abf4debfaa578d052ec6",
        "scheduled",
        ALICE,
        {"agent_id": CUSTOM_AGENT_ID, "workspace": "/scheduled"},
    )
    resolved = await resolve_project_session_create(
        body=ProjectSessionCreateRequest(project_id=project.id),
        user_id=ALICE,
        project_store=project_store,
        apply_calling_defaults=True,
    )
    assert resolved.body.agent_id == CUSTOM_AGENT_ID
    assert resolved.body.workspace == "/scheduled"


def _import_payload(external_session_id: str, **extra: object) -> dict[str, object]:
    return {
        "source": "claude",
        "external_session_id": external_session_id,
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
        **extra,
    }


def _seed_claude_import_agent(db_uri: str) -> None:
    SqlAlchemyAgentStore(db_uri).create(
        builtin_agent_id("claude-native-ui"),
        name="claude-native-ui",
        bundle_location="builtin://claude-native-ui",
    )


async def test_import_with_project_defaults_workspace_and_files_session(
    project_create_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An import naming a project consumes the resolver's decisions for real."""
    _seed_claude_import_agent(db_uri)
    project_id = await _project(project_create_client, {"workspace": "/work/import"})
    response = await project_create_client.post(
        "/v1/imports",
        json=_import_payload("project-import-1", project_id=project_id),
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    session = await project_create_client.get(
        f"/v1/sessions/{response.json()['session_id']}", headers=_headers()
    )
    assert session.status_code == 200, session.text
    assert session.json()["workspace"] == "/work/import"
    assert session.json()["project_id"] == project_id


async def test_import_with_unowned_or_unknown_project_is_404(
    project_create_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    _seed_claude_import_agent(db_uri)
    bob_project = await _project(project_create_client, {"agent_id": CUSTOM_AGENT_ID}, user=BOB)
    for project_id in ("0" * 32, bob_project):
        response = await project_create_client.post(
            "/v1/imports",
            json=_import_payload("project-import-denied", project_id=project_id),
            headers=_headers(),
        )
        assert response.status_code == 404, response.text
        assert "Project not found" in response.text


# ── Calling defaults on the opted-in JSON create (K1, K4, K5, K7a/b) ──────
#
# The host-facing seams are faked (no live host on this replica): workspace
# validation is replaced so a host-bound create can persist, and the create
# route's launch attempt is skipped by clearing ``host_registry``.

HDS = "a" * 32
TMB = "b" * 32
CODEX_AGENT_ID = "587b7cb7ac30abf4debfaa578d052ec6"
CLAUDE_AGENT_ID = "687b7cb7ac30abf4debfaa578d052ec7"


def _harness_bundle(harness: str) -> bytes:
    """A minimal agent bundle whose executor declares *harness*."""
    config = yaml.safe_dump(
        {
            "spec_version": 1,
            "name": f"calling-{harness}",
            "executor": {"type": "omnigent", "config": {"harness": harness}},
            "prompt": "hi",
        }
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tf:
        data = config.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture()
def calling_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with the calling-defaults stores wired on ``app.state``."""
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    agents = SqlAlchemyAgentStore(db_uri)
    for agent_id, harness in (
        (CODEX_AGENT_ID, "codex"),
        (CLAUDE_AGENT_ID, "claude-native"),
    ):
        location = f"{agent_id}/bundle"
        artifacts.put(location, _harness_bundle(harness))
        agents.create(agent_id, f"calling-{harness}", location)
    hosts = HostStore(db_uri)
    # Upstream #8675's create-time readiness check resolves the host first, so
    # the hosts these tests create sessions on must be registered.
    hosts.upsert_on_connect(HDS, "hds", ALICE)
    hosts.upsert_on_connect(TMB, "tmb", ALICE)
    app = create_app(
        agent_store=agents,
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        project_store=SqlAlchemyProjectStore(db_uri),
        host_store=hosts,
        host_model_catalog_cache_store=HostModelCatalogCacheStore(db_uri),
        user_preferences_store=SqlAlchemyUserPreferencesStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )
    app.state.artifact_store = artifacts
    # No live host: the create route skips the launch attempt.
    app.state.host_registry = None
    return app


@pytest_asyncio.fixture()
async def calling_client(calling_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=calling_app), base_url="http://test"
    ) as client:
        yield client


@pytest.fixture()
def calling_seams(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.server.routes._sessions import orchestration

    async def echo_workspace(**kwargs: object) -> str:
        return str(kwargs["workspace"])

    monkeypatch.setattr(orchestration, "_validate_session_workspace", echo_workspace)


def _per_host_set(agent_id: str, harness: str, model: str, effort: str) -> dict[str, object]:
    return {"agent_id": agent_id, "harnesses": {harness: {"model": model, "effort": effort}}}


async def test_per_host_set_fills_agent_model_and_effort(
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """Scenarios 1 + 2: each host's own set supplies the whole triple."""
    project_id = await _project(
        calling_client,
        {
            "calling_defaults": {
                HDS: _per_host_set(CODEX_AGENT_ID, "codex", "gpt-6-sol", "high"),
                TMB: _per_host_set(CLAUDE_AGENT_ID, "claude-native", "opus-5-5", "xhigh"),
            }
        },
    )
    for host_id, agent_id, model, effort in (
        (HDS, CODEX_AGENT_ID, "gpt-6-sol", "high"),
        (TMB, CLAUDE_AGENT_ID, "opus-5-5", "xhigh"),
    ):
        response = await calling_client.post(
            "/v1/sessions",
            json={"project_id": project_id, "host_id": host_id, "workspace": "/work"},
            headers=_headers(),
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["agent_id"] == agent_id
        assert body["model_override"] == model
        assert body["reasoning_effort"] == effort


async def test_explicit_model_wins_and_project_supplies_effort(
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """Scenario 3: resolution is per field — explicit model, project effort."""
    project_id = await _project(
        calling_client,
        {"calling_defaults": {HDS: _per_host_set(CODEX_AGENT_ID, "codex", "gpt-6-sol", "high")}},
    )
    response = await calling_client.post(
        "/v1/sessions",
        json={
            "project_id": project_id,
            "host_id": HDS,
            "workspace": "/work",
            "model_override": "gpt-6-luna",
        },
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["model_override"] == "gpt-6-luna"
    assert body["reasoning_effort"] == "high"


async def test_explicit_null_effort_stays_null(
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """Scenario 4: an explicit JSON null is never replaced by a default."""
    project_id = await _project(
        calling_client,
        {"calling_defaults": {HDS: _per_host_set(CODEX_AGENT_ID, "codex", "gpt-6-sol", "high")}},
    )
    response = await calling_client.post(
        "/v1/sessions",
        json={
            "project_id": project_id,
            "host_id": HDS,
            "workspace": "/work",
            "reasoning_effort": None,
        },
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["model_override"] == "gpt-6-sol"
    assert body["reasoning_effort"] is None


async def test_library_default_agent_refused_on_json_create(
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """K1: an agent-omitted create refuses a saved joint agent default."""
    project_id = await _project(
        calling_client,
        {"calling_defaults": {HDS: {"agent_id": "ca_polly"}}},
    )
    response = await calling_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "host_id": HDS, "workspace": "/work"},
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    message = response.json()["error"]["message"]
    assert "ca_polly" in message
    assert "saved joint agent" in message
    assert "POST /v1/sessions cannot launch it" in message
    assert "Pass agent_id" in message


async def test_default_agent_unready_harness_refused(
    calling_app: FastAPI,
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """K5 readiness: a host-reported unavailable harness refuses the default."""
    calling_app.state.host_store.upsert_on_connect(HDS, "hds", ALICE)
    calling_app.state.host_store.update_harness_readiness(HDS, {"codex": False})
    project_id = await _project(
        calling_client,
        {"calling_defaults": {HDS: {"agent_id": CODEX_AGENT_ID}}},
    )
    response = await calling_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "host_id": HDS, "workspace": "/work"},
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    message = response.json()["error"]["message"]
    assert "not ready on host 'hds'" in message
    assert "harness 'codex' is False" in message


async def test_fresh_catalog_missing_default_model_refused(
    calling_app: FastAPI,
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """Scenario 11: a fresh catalog that lacks the default model refuses it."""
    calling_app.state.host_store.upsert_on_connect(HDS, "hds", ALICE)
    calling_app.state.host_model_catalog_cache_store.upsert(
        HDS, "codex", [{"id": "gpt-5.5", "supportedReasoningEfforts": ["high"]}], 1700000000
    )
    project_id = await _project(
        calling_client,
        {"calling_defaults": {HDS: _per_host_set(CODEX_AGENT_ID, "codex", "gpt-6-sol", "high")}},
    )
    response = await calling_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "host_id": HDS, "workspace": "/work"},
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    message = response.json()["error"]["message"]
    assert "Default model 'gpt-6-sol'" in message
    assert "project 'project-alice@example.com' host settings" in message
    assert f"host '{HDS}'" in message
    assert "codex" in message
    assert "last sync" in message
    assert "Sync models" in message


async def test_fresh_catalog_missing_legacy_all_hosts_model_refused(
    calling_app: FastAPI,
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """The legacy All hosts row model is refused when the fresh catalog lacks it."""
    calling_app.state.host_store.upsert_on_connect(HDS, "hds", ALICE)
    calling_app.state.host_model_catalog_cache_store.upsert(
        HDS, "codex", [{"id": "gpt-5.5"}], 1700000000
    )
    project_id = await _project(
        calling_client,
        {"agent_id": CODEX_AGENT_ID, "model": "gpt-6-sol"},
    )
    response = await calling_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "host_id": HDS, "workspace": "/work"},
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    message = response.json()["error"]["message"]
    assert "Default model 'gpt-6-sol'" in message
    assert "All hosts row" in message
    assert "is not offered by host" in message
    assert "Sync models" in message


async def test_stale_catalog_does_not_block(
    calling_app: FastAPI,
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """Scenario 13: a catalog row with an error skips the offered checks."""
    calling_app.state.host_store.upsert_on_connect(HDS, "hds", ALICE)
    calling_app.state.host_model_catalog_cache_store.upsert(
        HDS, "codex", [{"id": "gpt-5.5"}], 1700000000
    )
    calling_app.state.host_model_catalog_cache_store.mark_error(HDS, "codex", "unsupported")
    project_id = await _project(
        calling_client,
        {"calling_defaults": {HDS: _per_host_set(CODEX_AGENT_ID, "codex", "gpt-6-sol", "high")}},
    )
    response = await calling_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "host_id": HDS, "workspace": "/work"},
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["model_override"] == "gpt-6-sol"
    assert body["reasoning_effort"] == "high"


async def test_child_takes_parent_project_host_default(
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """Scenario 10: a child with no agent takes the parent's host default."""
    project_id = await _project(
        calling_client,
        {"calling_defaults": {HDS: _per_host_set(CODEX_AGENT_ID, "codex", "gpt-6-sol", "high")}},
    )
    parent = await calling_client.post(
        "/v1/sessions",
        json={"project_id": project_id, "host_id": HDS, "workspace": "/work"},
        headers=_headers(),
    )
    assert parent.status_code == 201, parent.text
    child = await calling_client.post(
        "/v1/sessions",
        json={"parent_session_id": parent.json()["id"]},
        headers=_headers(),
    )
    assert child.status_code == 201, child.text
    body = child.json()
    assert body["agent_id"] == CODEX_AGENT_ID
    assert body["model_override"] == "gpt-6-sol"
    assert body["reasoning_effort"] == "high"
    assert body["project_id"] == project_id


async def test_sub_agent_child_keeps_an_unset_model(
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """F2: a named sub-agent create never takes server-side default fill.

    The runner deliberately omits the worker's model so the member's own spec
    decides; filling it from the project would pin a model the worker's spec
    left unset.
    """
    project_id = await _project(
        calling_client,
        {
            "calling_defaults": {
                HDS: {
                    "agent_id": CODEX_AGENT_ID,
                    "harnesses": {
                        "codex": {
                            "model": "gpt-6-sol",
                            "effort": "high",
                            "speed": "fast",
                            "permission": "approve-for-me",
                        }
                    },
                }
            }
        },
    )
    parent = await calling_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({"project_id": project_id})},
        files={
            "bundle": (
                "agent.tar.gz",
                build_agent_bundle(
                    name="sub-agent-parent",
                    sub_agents=[{"name": "impl"}],
                    executor={"type": "omnigent", "config": {"harness": "codex"}},
                ),
                "application/gzip",
            )
        },
        headers=_headers(),
    )
    assert parent.status_code == 201, parent.text
    parent_session_id = parent.json()["session_id"]
    parent_agent = await calling_client.get(
        f"/v1/sessions/{parent_session_id}/agent", headers=_headers()
    )
    assert parent_agent.status_code == 200, parent_agent.text

    child = await calling_client.post(
        "/v1/sessions",
        json={
            "agent_id": parent_agent.json()["id"],
            "parent_session_id": parent_session_id,
            "sub_agent_name": "impl",
            "host_id": HDS,
            "workspace": "/work",
        },
        headers=_headers(),
    )
    assert child.status_code == 201, child.text
    assert child.json()["model_override"] is None
    assert child.json()["reasoning_effort"] is None
    assert "omnigent.speed_tier" not in child.json()["labels"]
    assert "omnigent.codex_sdk.approval_mode" not in child.json()["labels"]


async def test_routing_on_create_does_not_fill_default_model(
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """F3a: a routing-on create owns model / effort; defaults must not pin them."""
    project_id = await _project(
        calling_client,
        {
            "calling_defaults": {
                HDS: {
                    "agent_id": CODEX_AGENT_ID,
                    "harnesses": {
                        "codex": {
                            "model": "gpt-6-sol",
                            "effort": "high",
                            "speed": "fast",
                            "permission": "approve-for-me",
                        }
                    },
                }
            }
        },
    )
    response = await calling_client.post(
        "/v1/sessions",
        json={
            "project_id": project_id,
            "host_id": HDS,
            "workspace": "/work",
            "cost_control_mode_override": "on",
        },
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    # The agent still fills; a pinned model would silently disable the router.
    assert body["agent_id"] == CODEX_AGENT_ID
    assert body["model_override"] is None
    assert body["reasoning_effort"] is None
    assert "omnigent.speed_tier" not in body["labels"]
    assert "omnigent.codex_sdk.approval_mode" not in body["labels"]


async def test_child_without_default_names_the_project_setting(
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """Scenario 10: no default agent → an error naming the missing setting."""
    project_id = await _project(calling_client, {})
    parent = await calling_client.post(
        "/v1/sessions",
        json={
            "project_id": project_id,
            "host_id": HDS,
            "workspace": "/work",
            "agent_id": CLAUDE_AGENT_ID,
        },
        headers=_headers(),
    )
    assert parent.status_code == 201, parent.text
    child = await calling_client.post(
        "/v1/sessions",
        json={"parent_session_id": parent.json()["id"]},
        headers=_headers(),
    )
    assert child.status_code == 400, child.text
    message = child.json()["error"]["message"]
    # The fixture registers the host, so the error names its friendly host name.
    assert "has no default agent on host 'hds'" in message
    assert "Pass agent_id" in message


async def test_child_with_unfiled_parent_names_both(
    calling_client: httpx.AsyncClient,
    calling_seams: None,
) -> None:
    """A child whose parent has no project names both missing pieces."""
    parent = await calling_client.post(
        "/v1/sessions",
        json={"agent_id": CLAUDE_AGENT_ID},
        headers=_headers(),
    )
    assert parent.status_code == 201, parent.text
    child = await calling_client.post(
        "/v1/sessions",
        json={"parent_session_id": parent.json()["id"]},
        headers=_headers(),
    )
    assert child.status_code == 400, child.text
    message = child.json()["error"]["message"]
    assert "agent_id is required" in message
    assert "parent session has no project" in message


async def test_import_ignores_calling_defaults(
    calling_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Import keeps its source agent: the chain never fills its fields."""
    _seed_claude_import_agent(db_uri)
    project_id = await _project(
        calling_client,
        {
            "workspace": "/work/import",
            "model": "gpt-6-sol",
            "calling_defaults": {HDS: _per_host_set(CODEX_AGENT_ID, "codex", "gpt-6-sol", "high")},
        },
    )
    response = await calling_client.post(
        "/v1/imports",
        json=_import_payload("calling-defaults-import", project_id=project_id),
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    session = await calling_client.get(
        f"/v1/sessions/{response.json()['session_id']}", headers=_headers()
    )
    assert session.status_code == 200, session.text
    assert session.json()["agent_id"] == builtin_agent_id("claude-native-ui")
    assert session.json()["model_override"] is None
    assert session.json()["reasoning_effort"] is None


@pytest.mark.parametrize(
    "harness,permission,expected_args,label",
    [
        ("codex-native", "approve-for-me", ["--approve-for-me"], None),
        (
            "codex-native",
            "ask-for-approval",
            [
                "--ask-for-approval",
                "on-request",
                "--sandbox",
                "workspace-write",
                "-c",
                'approvals_reviewer="user"',
            ],
            None,
        ),
        (
            "codex-native",
            "full-access",
            ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
            None,
        ),
        (
            "codex-native",
            "read-only",
            ["--sandbox", "read-only", "--ask-for-approval", "on-request"],
            None,
        ),
        ("claude-native", "plan", ["--permission-mode", "plan"], None),
        ("codex", "approve-for-me", None, "omnigent.codex_sdk.approval_mode"),
        ("claude-sdk", "auto", None, "omnigent.claude_sdk.permission_mode"),
    ],
)
async def test_session_create_applies_default_modes_and_persists(
    calling_client: httpx.AsyncClient,
    calling_app: FastAPI,
    calling_seams: None,
    harness: str,
    permission: str,
    expected_args: list[str] | None,
    label: str | None,
) -> None:
    location = f"{CODEX_AGENT_ID}/bundle"
    calling_app.state.artifact_store.put(location, _harness_bundle(harness))
    modes = {"permission": permission}
    if harness.startswith("codex"):
        modes["speed"] = "fast"
    project_id = await _project(
        calling_client,
        {
            "calling_defaults": {
                HDS: {
                    "agent_id": CODEX_AGENT_ID,
                    "harnesses": {harness: modes},
                }
            }
        },
    )
    parent = await calling_client.post(
        "/v1/sessions",
        json={
            "project_id": project_id,
            "host_id": HDS,
            "workspace": "/opt/work/project",
        },
        headers=_headers(),
    )
    assert parent.status_code == 201, parent.text
    from omnigent.runner.tool_dispatch import _build_session_create_body

    child = await calling_client.post(
        "/v1/sessions",
        json=_build_session_create_body(
            None,
            parent.json()["id"],
            "child",
        ),
        headers=_headers(),
    )
    assert child.status_code == 201, child.text
    for created in (parent, child):
        persisted = await calling_client.get(
            f"/v1/sessions/{created.json()['id']}", headers=_headers()
        )
        assert persisted.status_code == 200, persisted.text
        body = persisted.json()
        assert body["terminal_launch_args"] == expected_args
        if label:
            assert body["labels"][label] == permission
        if harness.startswith("codex"):
            assert body["labels"]["omnigent.speed_tier"] == "fast"


@pytest.mark.parametrize(
    "explicit",
    [
        {"approval_mode": "read-only"},
        {"terminal_launch_args": ["--ask-for-approval=never"]},
        {"labels": {"omnigent.codex_native.bypass_sandbox": "0"}},
    ],
)
async def test_explicit_permission_and_speed_win_over_defaults(
    calling_client: httpx.AsyncClient,
    calling_seams: None,
    explicit: dict,
) -> None:
    project_id = await _project(
        calling_client,
        {
            "calling_defaults": {
                HDS: {
                    "agent_id": CODEX_AGENT_ID,
                    "harnesses": {"codex": {"permission": "approve-for-me", "speed": "fast"}},
                }
            }
        },
    )
    explicit = dict(explicit)
    explicit["labels"] = {**explicit.get("labels", {}), "omnigent.speed_tier": "standard"}
    response = await calling_client.post(
        "/v1/sessions",
        json={
            "project_id": project_id,
            "host_id": HDS,
            "workspace": "/opt/work/project",
            **explicit,
        },
        headers=_headers(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["labels"]["omnigent.speed_tier"] == "standard"
    assert body["labels"].get("omnigent.codex_sdk.approval_mode") == explicit.get("approval_mode")
    assert body["terminal_launch_args"] == explicit.get("terminal_launch_args")


@pytest.mark.parametrize(
    "harness,args",
    [
        *(
            ("codex-native", args)
            for args in [
                ["-a", "never"],
                ["--ask-for-approval=never"],
                ["-s", "read-only"],
                ["--sandbox=read-only"],
                ["--approve-for-me"],
                ["--not-so-yolo"],
                ["--dangerously-bypass-approvals-and-sandbox"],
                ["--yolo"],
                ["-sread-only"],
                ["-aon-request"],
                ["-c=approval_policy=never"],
                ["-capproval_policy=never"],
                ["--config=sandbox_mode=read-only"],
                ["--profile", "strict"],
                *[
                    ["-c", f"{key}=value"]
                    for key in [
                        "approval_policy",
                        "sandbox_mode",
                        "approvals_reviewer",
                        "default_permissions",
                    ]
                ],
            ]
        ),
        *(
            ("claude-native", [flag])
            for flag in [
                "--permission-mode=plan",
                "--dangerously-skip-permissions",
                "--allow-dangerously-skip-permissions",
            ]
        ),
    ],
)
async def test_permission_default_recognizes_explicit_launch_args(
    harness: str, args: list[str]
) -> None:
    from omnigent.server.routes._sessions.orchestration import _launch_args_set_permission

    assert _launch_args_set_permission(args, harness)
    assert not _launch_args_set_permission(["--model", "test-model"], harness)


@pytest.mark.parametrize(
    "args",
    [
        ["--", "--yolo", "-sread-only"],
        ["-m", "--yolo"],
        ["-c", 'developer_instructions="-sread-only --yolo"'],
        ["-c=model=gpt-5.4", "--add-dir", "/opt/work/extra"],
    ],
)
async def test_permission_default_ignores_option_values_and_prompt(args: list[str]) -> None:
    from omnigent.server.routes._sessions.orchestration import _launch_args_set_permission

    assert not _launch_args_set_permission(args, "codex-native")


async def test_native_permission_default_respects_launch_arg_count_bounds(
    calling_client: httpx.AsyncClient,
    calling_app: FastAPI,
    calling_seams: None,
) -> None:
    from omnigent.server.routes._sessions.helpers import _MAX_TERMINAL_LAUNCH_ARGS

    calling_app.state.artifact_store.put(
        f"{CODEX_AGENT_ID}/bundle", _harness_bundle("codex-native")
    )
    project_id = await _project(
        calling_client,
        {
            "calling_defaults": {
                HDS: {
                    "agent_id": CODEX_AGENT_ID,
                    "harnesses": {"codex-native": {"permission": "approve-for-me"}},
                }
            }
        },
    )
    response = await calling_client.post(
        "/v1/sessions",
        json={
            "project_id": project_id,
            "host_id": HDS,
            "workspace": "/opt/work/project",
            "terminal_launch_args": ["--verbose"] * _MAX_TERMINAL_LAUNCH_ARGS,
        },
        headers=_headers(),
    )
    assert response.status_code == 400, response.text
    assert "terminal_launch_args exceeds" in response.text
