"""Integration tests for ``POST /v1/sessions/{source_id}/continue``.

Continuing an archived session mints a fresh top-level session on the
same host / launch directory / project with the same model settings and
no history, carrying the archived session's agent over: a template agent
is bound by id, a session-scoped agent is cloned into a fresh row. Uses
the shared ``client`` fixture from ``tests/server/conftest.py`` (real
stores + mock LLM), plus a header-auth variant for the permission gate.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from omnigent.db.utils import generate_agent_id
from omnigent.member_snapshot import member_label_key
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.server.routes._sessions.common import _CLAUDE_NATIVE_PERMISSION_MODE_LABEL_KEY
from omnigent.server.routes.sessions import routes_core
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store import (
    CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY,
    pinned_label_key,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import ControllableMockClient
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio

ALICE = "alice@example.com"
BOB = "bob@example.com"


# ── Helpers ──────────────────────────────────────────────


def _template_agent(db_uri: str, *, bundle_location: str, name: str) -> str:
    """Register a template (built-in) agent sharing *bundle_location*.

    :param db_uri: Per-test SQLite URI.
    :param bundle_location: Real bundle key to bind.
    :param name: Unique template name.
    :returns: The new template agent id.
    """
    agent = SqlAlchemyAgentStore(db_uri).create(
        agent_id=generate_agent_id(),
        name=name,
        bundle_location=bundle_location,
    )
    return agent.id


def _real_template(agent_json: dict[str, Any], db_uri: str, name: str) -> str:
    """Register a template agent over a real uploaded bundle.

    A ``create_test_agent`` result carries a session-scoped agent whose
    bundle was uploaded for real; reusing its key gives the template a
    loadable spec (needed by the snapshot and the switch route).

    :param agent_json: A ``create_test_agent`` result (agent JSON).
    :param db_uri: Per-test SQLite URI.
    :param name: Unique template name.
    :returns: The new template agent id.
    """
    row = SqlAlchemyAgentStore(db_uri).get(agent_json["id"])
    assert row is not None and row.bundle_location
    return _template_agent(db_uri, bundle_location=row.bundle_location, name=name)


async def _archive(client: httpx.AsyncClient, session_id: str) -> None:
    """Archive *session_id* through the real PATCH route."""
    resp = await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["archived"] is True


async def _continue(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    """POST the continue route and return the raw response."""
    return await client.post(f"/v1/sessions/{session_id}/continue", headers=headers)


# ── Route behaviour ──────────────────────────────────────


async def test_continue_refuses_unarchived_session(client: httpx.AsyncClient) -> None:
    """A live session cannot be continued — its state conflicts."""
    agent = await create_test_agent(client, name="continue-live")
    resp = await _continue(client, agent["_session_id"])

    assert resp.status_code == 409, (
        f"Expected 409 for a non-archived source, got {resp.status_code}: {resp.text}"
    )
    assert resp.json()["error"]["code"] == "conflict"


async def test_continue_binds_template_agent_by_id(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A template agent carries over by id, and nothing else carries over.

    The continuation is a fresh top-level session: same workspace, empty
    transcript, and the archived row points at it via
    ``omnigent.continued_to``.
    """
    seed = await create_test_agent(client, name="continue-template-seed")
    template_id = _real_template(seed, db_uri, "continue-template")

    create = await client.post(
        "/v1/sessions",
        json={
            "agent_id": template_id,
            "title": "Archived work",
            "workspace": "/tmp/continue-proj",
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "old history"}],
                    },
                }
            ],
        },
    )
    assert create.status_code == 201, create.text
    source = create.json()
    await _archive(client, source["id"])

    resp = await _continue(client, source["id"])
    assert resp.status_code == 201, resp.text
    continued = resp.json()

    assert continued["id"] != source["id"]
    assert continued["agent_id"] == template_id, (
        "A template/shared agent must be bound by id, not cloned"
    )
    assert continued["workspace"] == "/tmp/continue-proj"
    assert continued["status"] == "idle"

    # No history: the archived transcript seeded one item; the
    # continuation must start empty.
    items = await client.get(f"/v1/sessions/{continued['id']}/items")
    assert items.status_code == 200, items.text
    assert items.json()["data"] == []

    source_row = SqlAlchemyConversationStore(db_uri).get_conversation(source["id"])
    assert source_row is not None
    assert source_row.labels["omnigent.continued_to"] == continued["id"]


async def test_continue_repeat_returns_same_session(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A repeat continue returns the already-minted session, not a new one."""
    seed = await create_test_agent(client, name="continue-repeat-seed")
    template_id = _real_template(seed, db_uri, "continue-repeat")
    create = await client.post("/v1/sessions", json={"agent_id": template_id})
    assert create.status_code == 201, create.text
    source = create.json()
    await _archive(client, source["id"])

    first = await _continue(client, source["id"])
    second = await _continue(client, source["id"])
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"], (
        "A repeat continue must be idempotent via omnigent.continued_to"
    )


async def test_continue_clones_session_scoped_agent_and_switch_keeps_archived_row(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A session-scoped agent is cloned; a later switch leaves the source row.

    Binding the archived session's raw agent id would make the new
    session share that row, and switching its agent would delete it —
    breaking the archived session. The clone must be its own row with
    the same bundle. The clone path takes no run-config overrides, so the
    source's harness / cost-control / routing overrides must still land.
    """
    source = await create_test_agent(client, name="continue-scoped")
    source_session_id = source["_session_id"]
    agent_store = SqlAlchemyAgentStore(db_uri)
    conv_store = SqlAlchemyConversationStore(db_uri)
    source_agent_row = agent_store.get(source["id"])
    assert source_agent_row is not None
    assert source_agent_row.session_id == source_session_id, (
        "Precondition: create_test_agent mints a session-scoped agent"
    )
    assert (
        conv_store.update_conversation(
            source_session_id,
            harness_override="claude-code",
            cost_control_mode_override="on",
            subagent_routing_override="on",
        )
        is not None
    )

    switch_seed = await create_test_agent(client, name="continue-switch-seed")
    switch_target = _real_template(switch_seed, db_uri, "continue-switch-target")

    await _archive(client, source_session_id)
    resp = await _continue(client, source_session_id)
    assert resp.status_code == 201, resp.text
    continued = resp.json()

    assert continued["agent_id"] != source["id"], (
        "Continuation must never bind the archived session's session-scoped agent id"
    )
    assert continued["cost_control_mode_override"] == "on"
    assert continued["subagent_routing_override"] == "on"
    continued_row = conv_store.get_conversation(continued["id"])
    assert continued_row is not None
    assert continued_row.harness_override == "claude-code"
    assert continued_row.cost_control_mode_override == "on"
    assert continued_row.subagent_routing_override == "on"
    clone_row = agent_store.get(continued["agent_id"])
    assert clone_row is not None
    assert clone_row.session_id == continued["id"]
    assert clone_row.bundle_location == source_agent_row.bundle_location

    switch = await client.post(
        f"/v1/sessions/{continued['id']}/switch-agent",
        json={"agent_id": switch_target},
    )
    assert switch.status_code == 200, switch.text

    survivor = agent_store.get(source["id"])
    assert survivor is not None, (
        "Switching the continuation's agent deleted the archived session's agent row"
    )
    assert survivor.session_id == source_session_id


async def test_continue_copies_workspace_project_and_model_overrides(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The continuation keeps the archived session's workspace and settings."""
    seed = await create_test_agent(client, name="continue-settings-seed")
    template_id = _real_template(seed, db_uri, "continue-settings")
    conv_store = SqlAlchemyConversationStore(db_uri)
    project_id = generate_agent_id()
    source = conv_store.create_conversation(
        agent_id=template_id,
        workspace="/tmp/continue-settings",
        project_id=project_id,
        model_override="databricks-claude-sonnet-4-6",
        reasoning_effort="high",
    )
    await _archive(client, source.id)

    resp = await _continue(client, source.id)
    assert resp.status_code == 201, resp.text
    continued = resp.json()

    assert continued["workspace"] == "/tmp/continue-settings"
    assert continued["model_override"] == "databricks-claude-sonnet-4-6"
    assert continued["reasoning_effort"] == "high"
    continued_row = conv_store.get_conversation(continued["id"])
    assert continued_row is not None
    assert continued_row.project_id == project_id, (
        "The continuation must stay filed in the archived session's project"
    )


#: Stored shape of a saved inference configuration; ``owner_id`` names the
#: user whose provider credentials it carries.
_SNAPSHOT_LOCAL = {
    "owner_id": "local",
    "harness": "claude-native",
    "runtime_config": {"providers": {}},
}


async def test_continue_copies_run_configuration_and_allowlisted_labels(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The archive's run configuration and allowlisted labels carry over.

    A native session's permission mode, a library agent's template id and a
    joint agent's member snapshot are run configuration, so the continuation
    keeps them (with the rest of the overrides and the caller's own inference
    snapshot). The pin and codex-bypass labels are instance-scoped state and
    the archive marker is placement state, so none may ride along.
    """
    seed = await create_test_agent(client, name="continue-config-seed")
    template_id = _real_template(seed, db_uri, "continue-config")
    conv_store = SqlAlchemyConversationStore(db_uri)
    source = conv_store.create_conversation(
        agent_id=template_id,
        workspace="/tmp/continue-config",
        model_override="databricks-claude-sonnet-4-6",
        reasoning_effort="high",
        harness_override="claude-code",
        cost_control_mode_override="on",
        subagent_routing_override="on",
        terminal_launch_args=["--permission-mode", "acceptEdits"],
        inference_snapshot=dict(_SNAPSHOT_LOCAL),
        labels={
            _CLAUDE_NATIVE_PERMISSION_MODE_LABEL_KEY: "acceptEdits",
            "omnigent:agent-template-id": template_id,
            member_label_key("researcher"): '{"lead": false}',
            CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY: "1",
            pinned_label_key(None): "1730000000000",
        },
    )
    await _archive(client, source.id)

    resp = await _continue(client, source.id)
    assert resp.status_code == 201, resp.text
    continued = conv_store.get_conversation(resp.json()["id"])
    assert continued is not None

    assert continued.harness_override == "claude-code"
    assert continued.cost_control_mode_override == "on"
    assert continued.subagent_routing_override == "on"
    assert continued.terminal_launch_args == ["--permission-mode", "acceptEdits"]
    assert continued.inference_snapshot == _SNAPSHOT_LOCAL
    assert continued.labels == {
        _CLAUDE_NATIVE_PERMISSION_MODE_LABEL_KEY: "acceptEdits",
        "omnigent:agent-template-id": template_id,
        member_label_key("researcher"): '{"lead": false}',
    }, "Only the allowlisted run-config labels may carry over"


async def test_continue_uses_only_the_callers_inference_snapshot(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The caller's own saved inference config carries; another's is refused.

    A snapshot binds provider credentials to its owner, so an editor of the
    archived session may only continue with a snapshot that names them — the
    same rule and error as fork.
    """
    seed = await create_test_agent(auth_client, name="continue-snapshot-seed", user=ALICE)
    template_id = _real_template(seed, db_uri, "continue-snapshot")
    conv_store = SqlAlchemyConversationStore(db_uri)
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(ALICE)
    alice = {"X-Forwarded-Email": ALICE}

    def _archived_source(owner_id: str) -> str:
        conv = conv_store.create_conversation(
            agent_id=template_id,
            workspace="/tmp/continue-snapshot",
            inference_snapshot={
                "owner_id": owner_id,
                "harness": "claude-native",
                "runtime_config": {"providers": {}},
            },
        )
        perm_store.grant(ALICE, conv.id, LEVEL_EDIT)
        assert conv_store.update_conversation(conv.id, archived=True) is not None
        return conv.id

    carried = await _continue(auth_client, _archived_source(ALICE), headers=alice)
    assert carried.status_code == 201, carried.text
    continued = conv_store.get_conversation(carried.json()["id"])
    assert continued is not None
    assert continued.inference_snapshot is not None
    assert continued.inference_snapshot["owner_id"] == ALICE

    refused = await _continue(auth_client, _archived_source(BOB), headers=alice)
    assert refused.status_code == 400, (
        f"Another user's inference snapshot must be refused like fork, got "
        f"{refused.status_code}: {refused.text}"
    )
    assert "belongs to another user" in refused.json()["error"]["message"]


async def test_continue_copies_host_and_validates_workspace(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A host-bound source keeps its host and its WORKTREE as the launch dir.

    The workspace must go through the create path's host validator, so a
    missing directory or an offline host fails the same way it does on
    create instead of persisting a dead session. The validator is stubbed
    here (the test harness has no live host); this test pins that it is
    invoked with the archived session's host and working tree, while the
    live-host error path is covered by the create-path suite.
    """
    seed = await create_test_agent(client, name="continue-host-seed")
    template_id = _real_template(seed, db_uri, "continue-host")
    conv_store = SqlAlchemyConversationStore(db_uri)
    host_id = "0123456789abcdef0123456789abcdef"
    source = conv_store.create_conversation(
        agent_id=template_id,
        host_id=host_id,
        workspace="/tmp/continue-launch",
        worktree="/tmp/continue-tree",
    )
    await _archive(client, source.id)

    calls: list[dict[str, Any]] = []

    async def _recording_validator(**kwargs: Any) -> str:
        calls.append(kwargs)
        return kwargs["workspace"]

    monkeypatch.setattr(routes_core, "_validate_session_workspace", _recording_validator)

    resp = await _continue(client, source.id)
    assert resp.status_code == 201, resp.text
    continued = resp.json()

    assert continued["host_id"] == host_id
    assert continued["workspace"] == "/tmp/continue-tree", (
        "The working tree must win over the launch directory"
    )
    assert len(calls) == 1, "Host-bound continue must run the create-path workspace validator"
    assert calls[0]["host_id"] == host_id
    assert calls[0]["workspace"] == "/tmp/continue-tree"


# ── Edit-level gate (multi-user) ─────────────────────────


@pytest.fixture()
def auth_app(runtime_init: None, db_uri: str, tmp_path: Path) -> FastAPI:
    """App with a permission store + header auth provider enabled."""
    from omnigent.server.auth import UnifiedAuthProvider

    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    return create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifact_store,
        agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        auth_provider=UnifiedAuthProvider(source="header"),
    )


@pytest_asyncio.fixture()
async def auth_client(
    auth_app: FastAPI,
    mock_llm: ControllableMockClient,
    tmp_path: Path,
) -> AsyncIterator[httpx.AsyncClient]:
    """Async HTTP client wired to the auth-enabled app."""
    from omnigent.runtime import set_harness_process_manager
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

    pm = HarnessProcessManager(tmp_parent=tmp_path / "harness_pm")
    await pm.start()
    set_harness_process_manager(pm)

    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c

    mock_llm.release_all()
    set_harness_process_manager(None)
    await pm.shutdown()


async def test_continue_requires_edit_access(
    auth_client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A reader may not continue an archived session; an editor may.

    The archived source is owned by one user (edit-or-higher) and shared
    read-only with another — the same gate session editing uses.
    """
    agent = await create_test_agent(auth_client, name="continue-auth", user=ALICE)
    session_id = agent["_session_id"]
    alice = {"X-Forwarded-Email": ALICE}
    bob = {"X-Forwarded-Email": BOB}

    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(BOB)
    perm_store.grant(BOB, session_id, LEVEL_READ)

    archive = await auth_client.patch(
        f"/v1/sessions/{session_id}", json={"archived": True}, headers=alice
    )
    assert archive.status_code == 200, archive.text

    editor = await _continue(auth_client, session_id, headers=alice)
    assert editor.status_code == 201, editor.text

    reader = await _continue(auth_client, session_id, headers=bob)
    assert reader.status_code == 403, (
        f"A read-only caller must not continue the session, got "
        f"{reader.status_code}: {reader.text}"
    )


async def test_continue_missing_session_returns_404(
    auth_client: httpx.AsyncClient,
) -> None:
    """Continuing an unknown session is a 404 for any caller."""
    resp = await _continue(
        auth_client,
        "1d0b12236c77f69f5073a53583de1a3f",
        headers={"X-Forwarded-Email": ALICE},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_continue_refuses_source_without_agent(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An archived conversation with no agent binding cannot be continued.

    The PATCH archive route itself refuses a row with no agent binding
    (it is not a session), so the flag is set through the store directly.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()
    assert conv_store.update_conversation(conv.id, archived=True) is not None

    resp = await _continue(client, conv.id)
    assert resp.status_code == 400
    assert "agent" in resp.json()["error"]["message"].lower()
