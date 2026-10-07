"""Member snapshot labels at session create (SCC06 F1a, custom line).

A bundle whose spec projects 2+ members freezes each member as one
``omnigent.member.<role>`` label: the effective host / harness / model /
effort, the lead flag, and an availability reason when the member cannot
run. A 1-member bundle gets no member labels; a client seed is refused; an
over-cap key or value is a 400, never truncated. The scheduled
``launch_library_agent`` path is covered in
``tests/server/integration/test_scheduled_library_agents.py``.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from starlette.requests import HTTPConnection

from omnigent.member_snapshot import (
    MEMBER_LABEL_PREFIX,
    MEMBER_LOCK_LABEL_KEY,
    encode_member_entry,
    member_label_key,
    parse_member_entry,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import AuthProvider
from omnigent.server.custom_agents_store import CustomAgentsStore
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

_HOST_ID = "7a2b1c9dfe310a4bb2cc56d1a0e47b3c"
_WORKSPACE = "/repo"
_USER = "alice"


class HeaderAuth(AuthProvider):
    def get_user_id(self, request: HTTPConnection) -> str | None:
        return request.headers.get("x-test-user")


def _bundle(*, lead: str, worker: str | None) -> bytes:
    """One ``config.yaml`` plus an optional ``researcher`` sub-agent."""
    entries = {"config.yaml": lead.encode()}
    if worker is not None:
        entries["agents/researcher/config.yaml"] = worker.encode()
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return out.getvalue()


def joint_bundle(
    *,
    lead_model: str = "lead-model",
    lead_harness: str = "codex",
    worker_model: str = "worker-model",
    worker_harness: str = "claude-sdk",
    worker_effort: str | None = "medium",
    worker_name: str = "researcher",
) -> bytes:
    worker_effort_field = (
        f" reasoning_effort: {worker_effort}," if worker_effort is not None else ""
    )
    return _bundle(
        lead=f"""spec_version: 1
name: custom-reviewer
description: Lead reviewer
executor: {{type: omnigent, model: {lead_model}, reasoning_effort: high,
  config: {{harness: {lead_harness}}}}}
""",
        worker=f"""spec_version: 1
name: {worker_name}
description: Research support
executor: {{type: omnigent, model: {worker_model},{worker_effort_field}
  config: {{harness: {worker_harness}}}}}
""",
    )


def single_bundle() -> bytes:
    return _bundle(
        lead="""spec_version: 1
name: solo
executor: {type: omnigent, model: lead-model, config: {harness: codex}}
""",
        worker=None,
    )


@dataclass
class _MemberServer:
    app: FastAPI
    conversations: SqlAlchemyConversationStore
    hosts: HostStore
    custom: CustomAgentsStore
    projects: SqlAlchemyProjectStore
    prefs: SqlAlchemyUserPreferencesStore


@pytest.fixture()
def member_server(runtime_init: None, db_uri: str, tmp_path: Path) -> _MemberServer:
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    conversations = SqlAlchemyConversationStore(db_uri)
    hosts = HostStore(db_uri)
    projects = SqlAlchemyProjectStore(db_uri)
    prefs = SqlAlchemyUserPreferencesStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=conversations,
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=HeaderAuth(),
        host_store=hosts,
        project_store=projects,
        user_preferences_store=prefs,
    )
    # The snapshot resolution is this file's subject; the multipart host
    # workspace round-trip and runner launch have their own coverage, so a
    # hostless registry skips both.
    app.state.host_registry = None
    return _MemberServer(
        app=app,
        conversations=conversations,
        hosts=hosts,
        custom=CustomAgentsStore(db_uri),
        projects=projects,
        prefs=prefs,
    )


@pytest_asyncio.fixture()
async def client(member_server: _MemberServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=member_server.app), base_url="http://test"
    ) as http:
        yield http


@pytest.fixture(autouse=True)
def _stub_workspace_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    """Echo the requested workspace instead of the host.stat round-trip."""
    from omnigent.server.routes import _session_create_validation as validation

    async def _validate(**kwargs: object) -> str:
        workspace = kwargs["workspace"]
        assert isinstance(workspace, str)
        return workspace

    monkeypatch.setattr(validation, "validate_uploaded_bundle_host_workspace", _validate)


def _arm_host(hosts: HostStore, *, configured_harnesses: dict[str, object] | None = None) -> None:
    hosts.upsert_on_connect(
        _HOST_ID,
        "member-laptop",
        _USER,
        configured_harnesses=configured_harnesses,  # type: ignore[arg-type]
    )


async def _create(
    client: httpx.AsyncClient,
    bundle_bytes: bytes,
    *,
    metadata: dict[str, object] | None = None,
    expect: int = 201,
) -> httpx.Response:
    body = metadata if metadata is not None else {"host_id": _HOST_ID, "workspace": _WORKSPACE}
    response = await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(body)},
        files={"bundle": ("agent.tar.gz", bundle_bytes, "application/gzip")},
        headers={"x-test-user": _USER},
    )
    assert response.status_code == expect, response.text
    return response


async def _member_labels_after_create(
    member_server: _MemberServer,
    bundle_bytes: bytes,
    *,
    metadata: dict[str, object] | None = None,
) -> tuple[dict[str, dict[str, object]], dict[str, str]]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=member_server.app), base_url="http://test"
    ) as client:
        response = await _create(client, bundle_bytes, metadata=metadata)
    conversation = member_server.conversations.get_conversation(response.json()["session_id"])
    assert conversation is not None
    entries: dict[str, dict[str, object]] = {}
    for key, value in conversation.labels.items():
        if not key.startswith(MEMBER_LABEL_PREFIX) or key == MEMBER_LOCK_LABEL_KEY:
            continue
        parsed = parse_member_entry(value)
        assert parsed is not None
        entries[key[len(MEMBER_LABEL_PREFIX) :]] = parsed
    return entries, dict(conversation.labels)


_CATALOGS: dict[str, list[dict[str, object]]] = {
    "codex": [{"id": "lead-model", "model": "lead-model", "isDefault": True}],
    "claude-sdk": [
        {"id": "worker-model", "model": "worker-model", "isDefault": False},
        {"id": "claude-sonnet-4-6", "model": "claude-sonnet-4-6", "isDefault": False},
        {"id": "claude-opus-4-8", "model": "claude-opus-4-8", "isDefault": True},
    ],
}


def _stub_catalog(
    monkeypatch: pytest.MonkeyPatch,
    catalogs: dict[str, list[dict[str, object]]] | None = None,
) -> None:
    from omnigent.server.routes._sessions import helpers

    table = _CATALOGS if catalogs is None else catalogs

    async def _fake_options(host_id: str, harness: str) -> list[dict[str, object]] | None:
        assert host_id == _HOST_ID
        return table.get(harness)

    monkeypatch.setattr(helpers, "_host_model_options_via_registry", _fake_options)


_MEMBER_HOST = "5c4d3e2f1a0b9c8d7e6f5a4b3c2d1e0f"
_TEMPLATE_LABEL = "omnigent:agent-template-id"


def _arm_member_host(hosts: HostStore) -> None:
    hosts.upsert_on_connect(_MEMBER_HOST, "member-worker-laptop", _USER)


def _member_snapshot_reads(monkeypatch: pytest.MonkeyPatch, replacement: object) -> None:
    """Route the member-snapshot phase through *replacement* host store.

    Upstream #8675's create-time readiness check reads the app's registered
    host first; these tests pin the snapshot's own host-store behaviour after
    it, so only the snapshot call is given the replacement.
    """
    from omnigent.server.routes.sessions import routes_core

    real = routes_core._member_snapshot_labels

    async def _snapshot(spec: object, *, host_store: object, **kwargs: object) -> object:
        return await real(spec, host_store=replacement, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(routes_core, "_member_snapshot_labels", _snapshot)


def _stub_member_catalogs(
    monkeypatch: pytest.MonkeyPatch,
    table: dict[tuple[str, str], list[dict[str, object]]],
) -> None:
    from omnigent.server.routes._sessions import helpers

    async def _fake_options(host_id: str, harness: str) -> list[dict[str, object]] | None:
        return table.get((host_id, harness))

    monkeypatch.setattr(helpers, "_host_model_options_via_registry", _fake_options)


def _create_template(
    store: CustomAgentsStore,
    agent_id: str,
    *,
    worker_host: str | None,
    owner: str = _USER,
) -> None:
    """A saved joint Agent row whose worker may carry a library-only host."""
    members: list[dict[str, object]] = [
        {
            "name": "custom-reviewer",
            "description": "Lead reviewer",
            "harness": "codex",
            "model": "lead-model",
            "reasoning_effort": "high",
            "lead": True,
        },
        {
            "name": "researcher",
            "description": "Research support",
            "harness": "claude-sdk",
            "model": "worker-model",
            "reasoning_effort": "medium",
            "lead": False,
        },
    ]
    if worker_host is not None:
        members[1]["host_id"] = worker_host
    store.create(
        owner,
        {
            "id": agent_id,
            "name": "custom-reviewer",
            "description": "Lead reviewer",
            "harness": "codex",
            "model": "lead-model",
            "bundle_location": f"unused/{agent_id}",
            "members": members,
        },
    )


def _template_metadata(agent_id: str) -> dict[str, object]:
    return {
        "host_id": _HOST_ID,
        "workspace": _WORKSPACE,
        "labels": {_TEMPLATE_LABEL: agent_id},
    }


@pytest.mark.asyncio
async def test_multipart_create_writes_one_label_per_member(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 2-member bundle freezes the lead and the worker under their roles."""
    _arm_host(member_server.hosts)
    _stub_catalog(monkeypatch)

    entries, labels = await _member_labels_after_create(member_server, joint_bundle())

    assert entries == {
        "custom-reviewer": {
            "host": _HOST_ID,
            "harness": "codex",
            "model": "lead-model",
            "effort": "high",
            "lead": True,
        },
        "researcher": {
            "host": _HOST_ID,
            "harness": "claude-sdk",
            "model": "worker-model",
            "effort": "medium",
            "lead": False,
        },
    }
    assert labels[MEMBER_LOCK_LABEL_KEY] == "false"


@pytest.mark.asyncio
async def test_one_member_bundle_writes_no_member_labels(
    member_server: _MemberServer,
) -> None:
    """A 1-member Agent keeps its harness controls: no member snapshot at all."""
    _arm_host(member_server.hosts)

    entries, _ = await _member_labels_after_create(member_server, single_bundle())

    assert entries == {}


@pytest.mark.asyncio
async def test_default_model_resolves_to_host_catalog_is_default(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A member model of ``default`` freezes the catalog's ``isDefault`` row."""
    _arm_host(member_server.hosts)
    _stub_catalog(monkeypatch)

    entries, _ = await _member_labels_after_create(
        member_server, joint_bundle(worker_model="default")
    )

    assert entries["researcher"]["model"] == "claude-opus-4-8"


@pytest.mark.asyncio
async def test_missing_default_row_keeps_model_null(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalog with no ``isDefault`` row leaves a ``default`` member null."""
    _arm_host(member_server.hosts)
    _stub_catalog(
        monkeypatch,
        {
            "codex": [{"id": "lead-model", "model": "lead-model"}],
            "claude-sdk": [{"id": "claude-sonnet-4-6", "model": "claude-sonnet-4-6"}],
        },
    )

    entries, _ = await _member_labels_after_create(
        member_server, joint_bundle(worker_model="default")
    )

    assert entries["researcher"]["model"] is None


@pytest.mark.asyncio
async def test_member_chain_keeps_saved_model_and_fills_unset_effort(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scenario 14: the member's saved model wins; the project chain fills effort."""
    _arm_host(member_server.hosts)
    _stub_catalog(monkeypatch)
    project = member_server.projects.create(
        "d1" * 16,
        "Chain project",
        _USER,
        config={
            "calling_defaults": {
                _HOST_ID: {
                    "harnesses": {"claude-sdk": {"model": "chain-model", "effort": "xhigh"}}
                }
            }
        },
    )

    entries, _ = await _member_labels_after_create(
        member_server,
        joint_bundle(worker_effort=None),
        metadata={
            "host_id": _HOST_ID,
            "workspace": _WORKSPACE,
            "project_id": project.id,
        },
    )

    assert entries["researcher"]["model"] == "worker-model"
    assert entries["researcher"]["effort"] == "xhigh"
    # No chain entry for the lead's harness, so its saved values stay.
    assert entries["custom-reviewer"]["model"] == "lead-model"
    assert entries["custom-reviewer"]["effort"] == "high"


@pytest.mark.asyncio
async def test_chain_model_precedes_the_catalog_is_default_row(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``default`` member model takes the chain's model before the catalog row."""
    _arm_host(member_server.hosts)
    _stub_catalog(monkeypatch)
    project = member_server.projects.create(
        "d2" * 16,
        "Chain project",
        _USER,
        config={
            "calling_defaults": {_HOST_ID: {"harnesses": {"claude-sdk": {"model": "chain-model"}}}}
        },
    )

    entries, _ = await _member_labels_after_create(
        member_server,
        joint_bundle(worker_model="default"),
        metadata={
            "host_id": _HOST_ID,
            "workspace": _WORKSPACE,
            "project_id": project.id,
        },
    )

    assert entries["researcher"]["model"] == "chain-model"


@pytest.mark.asyncio
async def test_host_store_failure_still_writes_labels_without_availability(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising host store degrades to missing facts, never a failed create."""
    _arm_host(member_server.hosts)

    class _BrokenHostStore:
        def get_host(self, _host_id: str) -> None:
            raise RuntimeError("host store down")

    _member_snapshot_reads(monkeypatch, _BrokenHostStore())

    entries, _ = await _member_labels_after_create(member_server, joint_bundle())

    assert entries == {
        "custom-reviewer": {
            "host": _HOST_ID,
            "harness": "codex",
            "model": "lead-model",
            "effort": "high",
            "lead": True,
        },
        "researcher": {
            "host": _HOST_ID,
            "harness": "claude-sdk",
            "model": "worker-model",
            "effort": "medium",
            "lead": False,
        },
    }


@pytest.mark.asyncio
async def test_catalog_lookup_failure_still_writes_declared_models(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising catalog lookup degrades to no catalog, never a failed create."""
    _arm_host(member_server.hosts)

    from omnigent.server.routes._sessions import helpers

    async def _broken_options(_host_id: str, _harness: str) -> None:
        raise RuntimeError("catalog down")

    monkeypatch.setattr(helpers, "_host_model_options_via_registry", _broken_options)

    entries, _ = await _member_labels_after_create(member_server, joint_bundle())

    assert entries["custom-reviewer"]["model"] == "lead-model"
    assert entries["researcher"]["model"] == "worker-model"
    assert "unavailable" not in entries["custom-reviewer"]
    assert "unavailable" not in entries["researcher"]


@pytest.mark.asyncio
async def test_catalog_lookups_for_distinct_harnesses_run_concurrently(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every needed catalog is in flight together: one lookup timeout, not N."""
    _arm_host(member_server.hosts)

    from omnigent.server.routes._sessions import helpers

    started: list[str] = []
    both_started = asyncio.Event()
    timed_out: list[str] = []

    async def _gate(_host_id: str, harness: str) -> None:
        started.append(harness)
        if len(started) >= 2:
            both_started.set()
            return
        try:
            await asyncio.wait_for(both_started.wait(), timeout=2.0)
        except TimeoutError:
            timed_out.append(harness)

    monkeypatch.setattr(helpers, "_host_model_options_via_registry", _gate)

    entries, _ = await _member_labels_after_create(member_server, joint_bundle())

    assert sorted(started) == ["claude-sdk", "codex"]
    assert timed_out == []
    assert entries["researcher"]["model"] == "worker-model"


@pytest.mark.asyncio
async def test_hostless_default_model_is_null(member_server: _MemberServer) -> None:
    """A hostless create stores null, never the literal ``"default"``."""
    entries, _ = await _member_labels_after_create(
        member_server, joint_bundle(worker_model="default"), metadata={}
    )

    assert entries["researcher"]["model"] is None
    assert entries["custom-reviewer"]["model"] == "lead-model"


@pytest.mark.asyncio
async def test_live_host_without_catalog_default_model_is_null(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness with no catalog row keeps ``"default"`` as null, not literal."""
    _arm_host(member_server.hosts)
    _stub_catalog(monkeypatch, {"codex": [{"id": "lead-model", "model": "lead-model"}]})

    entries, _ = await _member_labels_after_create(
        member_server, joint_bundle(worker_model="default")
    )

    assert entries["researcher"]["model"] is None
    assert "unavailable" not in entries["researcher"]
    assert entries["custom-reviewer"]["model"] == "lead-model"


@pytest.mark.asyncio
async def test_offline_host_marks_every_member_host_offline(
    member_server: _MemberServer,
) -> None:
    _arm_host(member_server.hosts)
    member_server.hosts.set_offline(_HOST_ID)

    entries, _ = await _member_labels_after_create(member_server, joint_bundle())

    assert entries["custom-reviewer"]["unavailable"] == "host_offline"
    assert entries["researcher"]["unavailable"] == "host_offline"


@pytest.mark.asyncio
async def test_unknown_host_marks_members_host_offline(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host row the snapshot cannot read counts as not live."""
    # Readiness resolves the registered host first; the snapshot sees none.
    _arm_host(member_server.hosts)

    class _UnknownHostStore:
        def get_host(self, _host_id: str) -> None:
            return None

    _member_snapshot_reads(monkeypatch, _UnknownHostStore())

    entries, _ = await _member_labels_after_create(member_server, joint_bundle())

    assert entries["researcher"]["unavailable"] == "host_offline"


@pytest.mark.asyncio
async def test_unconfigured_harness_maps_false_to_reason_code(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``False`` readiness becomes ``harness_not_configured``; a reported string
    reason is stored as-is."""
    # Readiness must pass on the registered host; the snapshot then reads the
    # reported-harness map below.
    _arm_host(member_server.hosts)
    host = member_server.hosts.get_host(_HOST_ID)
    assert host is not None
    snapshot_host = replace(
        host, configured_harnesses={"codex": False, "claude-sdk": "binary-missing"}
    )

    class _ConfiguredHarnessStore:
        def get_host(self, _host_id: str) -> object:
            return snapshot_host

    _member_snapshot_reads(monkeypatch, _ConfiguredHarnessStore())

    entries, _ = await _member_labels_after_create(member_server, joint_bundle())

    assert entries["custom-reviewer"]["unavailable"] == "harness_not_configured"
    assert entries["researcher"]["unavailable"] == "binary-missing"


@pytest.mark.asyncio
async def test_unreported_harness_is_not_unavailable(
    member_server: _MemberServer,
) -> None:
    """A harness the host's readiness map omits stays unknown, not blocked."""
    _arm_host(member_server.hosts, configured_harnesses={"codex": True})

    entries, _ = await _member_labels_after_create(member_server, joint_bundle())

    assert "unavailable" not in entries["researcher"]


@pytest.mark.asyncio
async def test_explicit_model_missing_from_catalog_is_unavailable(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    _arm_host(member_server.hosts)
    _stub_catalog(
        monkeypatch,
        {
            "codex": [{"id": "lead-model", "model": "lead-model"}],
            "claude-sdk": [{"id": "claude-sonnet-4-6", "model": "claude-sonnet-4-6"}],
        },
    )

    entries, _ = await _member_labels_after_create(member_server, joint_bundle())

    assert entries["researcher"]["unavailable"] == "model_missing"


@pytest.mark.asyncio
async def test_member_label_key_over_cap_is_a_400(
    member_server: _MemberServer, client: httpx.AsyncClient
) -> None:
    """A role whose label key exceeds 128 chars is rejected, never truncated."""
    _arm_host(member_server.hosts)
    role = "r" * 113
    response = await _create(client, joint_bundle(worker_name=role), expect=400)

    assert response.json()["error"]["code"] == "invalid_input"
    assert role[:20] in response.text


@pytest.mark.asyncio
async def test_member_label_value_over_cap_is_a_400(
    member_server: _MemberServer, client: httpx.AsyncClient
) -> None:
    """A member value over the 256-char label cap is a 400 naming the role."""
    _arm_host(member_server.hosts)
    response = await _create(client, joint_bundle(worker_model="m" * 240), expect=400)

    body = response.json()
    assert body["error"]["code"] == "invalid_input"
    assert "researcher" in body["error"]["message"]


@pytest.mark.asyncio
async def test_member_entry_at_the_value_cap_creates_with_the_session_lock() -> None:
    """A member entry within 14 chars of the cap still fits: the lock is a
    session label, not a field inside every entry."""
    from omnigent.server.routes._sessions import helpers

    model = "worker-model" + "x" * 153
    expected_entry = {
        "host": None,
        "harness": "claude-sdk",
        "model": model,
        "effort": "medium",
        "lead": False,
    }
    encoded = encode_member_entry(expected_entry)
    assert 242 <= len(encoded) <= 256

    spec = AgentSpec(
        spec_version=1,
        name="lead",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex"}),
        sub_agents=[
            AgentSpec(
                spec_version=1,
                name="researcher",
                executor=ExecutorSpec(
                    type="omnigent",
                    config={"harness": "claude-sdk"},
                    model=model,
                    reasoning_effort="medium",
                ),
            )
        ],
    )

    for locked, marker in ((True, "true"), (False, "false")):
        labels = await helpers._member_snapshot_labels(
            spec, host_id=None, host_store=None, locked=locked
        )
        assert labels[member_label_key("researcher")] == encoded
        assert labels[MEMBER_LOCK_LABEL_KEY] == marker


@pytest.mark.asyncio
async def test_client_seeded_member_label_is_rejected(
    client: httpx.AsyncClient,
) -> None:
    """The ``omnigent.member.*`` namespace is server-reserved on create."""
    response = await _create(
        client,
        joint_bundle(),
        metadata={
            "host_id": _HOST_ID,
            "workspace": _WORKSPACE,
            "labels": {"omnigent.member.researcher": "{}"},
        },
        expect=400,
    )

    assert response.json()["error"]["code"] == "invalid_input"


@pytest.mark.asyncio
async def test_manual_launch_agent_style_metadata_writes_member_labels(
    member_server: _MemberServer,
) -> None:
    """A hostless create (the interactive shape without a host chip) stores the
    snapshot with a null host and no availability resolution."""
    entries, _ = await _member_labels_after_create(member_server, joint_bundle(), metadata={})

    assert entries["researcher"] == {
        "host": None,
        "harness": "claude-sdk",
        "model": "worker-model",
        "effort": "medium",
        "lead": False,
    }


@pytest.mark.asyncio
async def test_member_host_decides_host_and_catalog_default(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A saved member host wins over the session host for that member's
    ``host`` and for its ``"default"`` model resolution."""
    _arm_host(member_server.hosts)
    _arm_member_host(member_server.hosts)
    _stub_member_catalogs(
        monkeypatch,
        {
            (_HOST_ID, "codex"): [{"id": "lead-model", "model": "lead-model", "isDefault": True}],
            (_MEMBER_HOST, "claude-sdk"): [
                {"id": "member-host-model", "model": "member-host-model", "isDefault": True}
            ],
        },
    )
    _create_template(member_server.custom, "ca_joint", worker_host=_MEMBER_HOST)

    entries, labels = await _member_labels_after_create(
        member_server,
        joint_bundle(worker_model="default"),
        metadata=_template_metadata("ca_joint"),
    )

    assert entries["custom-reviewer"] == {
        "host": _HOST_ID,
        "harness": "codex",
        "model": "lead-model",
        "effort": "high",
        "lead": True,
    }
    assert entries["researcher"] == {
        "host": _MEMBER_HOST,
        "harness": "claude-sdk",
        "model": "member-host-model",
        "effort": "medium",
        "lead": False,
    }
    assert labels[MEMBER_LOCK_LABEL_KEY] == "true"


@pytest.mark.asyncio
async def test_offline_member_host_marks_only_its_member(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Availability resolves per member host: an offline worker host leaves the
    session-host lead untouched."""
    _arm_host(member_server.hosts)
    _arm_member_host(member_server.hosts)
    member_server.hosts.set_offline(_MEMBER_HOST)
    _stub_member_catalogs(
        monkeypatch,
        {(_HOST_ID, "codex"): [{"id": "lead-model", "model": "lead-model", "isDefault": True}]},
    )
    _create_template(member_server.custom, "ca_joint", worker_host=_MEMBER_HOST)

    entries, _ = await _member_labels_after_create(
        member_server,
        joint_bundle(worker_model="default"),
        metadata=_template_metadata("ca_joint"),
    )

    assert "unavailable" not in entries["custom-reviewer"]
    assert entries["researcher"]["host"] == _MEMBER_HOST
    assert entries["researcher"]["unavailable"] == "host_offline"
    # An unavailable host never reaches the catalog: the model stays unresolved.
    assert entries["researcher"]["model"] is None


@pytest.mark.asyncio
async def test_failed_member_host_lookup_contributes_no_catalog_facts(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A raising ``get_host`` for the member host skips it entirely: no catalog
    call, no availability, no catalog default — and its host id still freezes."""
    from omnigent.server.routes._sessions import helpers

    _arm_host(member_server.hosts)
    _arm_member_host(member_server.hosts)
    _create_template(member_server.custom, "ca_joint", worker_host=_MEMBER_HOST)

    calls: list[tuple[str, str]] = []

    async def _spy_options(host_id: str, harness: str) -> list[dict[str, object]] | None:
        calls.append((host_id, harness))
        return {
            (_HOST_ID, "codex"): [{"id": "lead-model", "model": "lead-model"}],
            (_MEMBER_HOST, "claude-sdk"): [
                {"id": "member-host-model", "model": "member-host-model", "isDefault": True}
            ],
        }.get((host_id, harness))

    monkeypatch.setattr(helpers, "_host_model_options_via_registry", _spy_options)

    original_get_host = member_server.hosts.get_host

    def _broken_member_lookup(host_id: str):
        if host_id == _MEMBER_HOST:
            raise RuntimeError("member host lookup down")
        return original_get_host(host_id)

    monkeypatch.setattr(member_server.hosts, "get_host", _broken_member_lookup)

    # A "default" model stays null: the failed host's catalog default must not
    # leak into the snapshot.
    default_entries, default_labels = await _member_labels_after_create(
        member_server,
        joint_bundle(worker_model="default"),
        metadata=_template_metadata("ca_joint"),
    )
    assert calls == [(_HOST_ID, "codex")]
    assert default_entries["researcher"] == {
        "host": _MEMBER_HOST,
        "harness": "claude-sdk",
        "model": None,
        "effort": "medium",
        "lead": False,
    }
    assert default_labels[MEMBER_LOCK_LABEL_KEY] == "true"

    # An explicit model is kept as saved — never model_missing.
    calls.clear()
    entries, labels = await _member_labels_after_create(
        member_server, joint_bundle(), metadata=_template_metadata("ca_joint")
    )
    assert calls == [(_HOST_ID, "codex")]
    assert entries["researcher"] == {
        "host": _MEMBER_HOST,
        "harness": "claude-sdk",
        "model": "worker-model",
        "effort": "medium",
        "lead": False,
    }
    assert labels[MEMBER_LOCK_LABEL_KEY] == "true"


@pytest.mark.asyncio
async def test_unknown_foreign_and_non_library_template_ids_keep_the_session_host(
    member_server: _MemberServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only an owned ``ca_`` template contributes member hosts."""
    _arm_host(member_server.hosts)
    _arm_member_host(member_server.hosts)
    _stub_member_catalogs(
        monkeypatch,
        {
            (_HOST_ID, "codex"): [{"id": "lead-model", "model": "lead-model", "isDefault": True}],
            (_HOST_ID, "claude-sdk"): [
                {"id": "worker-model", "model": "worker-model", "isDefault": True}
            ],
            (_MEMBER_HOST, "claude-sdk"): [
                {"id": "member-host-model", "model": "member-host-model", "isDefault": True}
            ],
        },
    )
    _create_template(member_server.custom, "ca_bob", worker_host=_MEMBER_HOST, owner="bob")
    _create_template(member_server.custom, "ca_plain", worker_host=None)

    for template_id in ("ca_bob", "ca_missing", "ag_session_scoped", "ca_plain"):
        entries, labels = await _member_labels_after_create(
            member_server, joint_bundle(), metadata=_template_metadata(template_id)
        )
        assert entries["researcher"]["host"] == _HOST_ID, template_id
        assert entries["custom-reviewer"]["host"] == _HOST_ID, template_id
        # The create request's template label is the launch provenance: a
        # ``ca_`` id writes the locked marker; a non-library id does not.
        expected_marker = "true" if template_id.startswith("ca_") else "false"
        assert labels[MEMBER_LOCK_LABEL_KEY] == expected_marker, template_id
