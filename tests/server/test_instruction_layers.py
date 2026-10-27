"""Session route resolution and layered text at the init composition seam."""

import json
from functools import partial
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.server.instruction_layers import project_setup_line, session_instructions_text
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.spec import AgentSpec, ExecutorSpec
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.global_instructions_store.sqlalchemy_store import (
    SqlAlchemyGlobalInstructionsStore,
)
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore
from tests.server.helpers import create_test_agent
from tests.server.integration.test_sessions_harness_override import _stub_runner_client
from tests.server.routes.test_project_collaboration_routes import _build_app


@pytest.fixture
def instructions(db_uri, monkeypatch):
    store = SqlAlchemyGlobalInstructionsStore(db_uri)
    monkeypatch.setattr(
        "omnigent.server.instruction_layers.get_global_instructions_store", lambda: store
    )
    monkeypatch.setattr(
        "omnigent.server.instruction_layers.get_agent_store",
        lambda: SimpleNamespace(
            get=lambda agent_id: SimpleNamespace(
                id=agent_id, bundle_location="bundle", operator_authored=False, name="Custom agent"
            )
        ),
    )
    spec = AgentSpec(spec_version=1, name="Custom agent", spawn=True)
    monkeypatch.setattr(
        "omnigent.server.instruction_layers.get_agent_cache",
        lambda: SimpleNamespace(load=lambda *args, **kwargs: SimpleNamespace(spec=spec)),
    )
    store.save("S", created_by=None)
    store.save("A", created_by=None, scope="agent", agent_key="codex")
    store.save("H", created_by=None, scope="host", host_id="host-a")
    store.save("Mother", created_by=None, scope="mother")
    store.save("Child", created_by=None, scope="child")
    return store


def conversation(**overrides):
    return Conversation(
        id="session-a",
        agent_id="agent-a",
        created_at=1,
        updated_at=1,
        root_conversation_id="session-a",
        harness_override="native-codex",
        host_id="host-a",
        runner_id="runner-a",
        workspace="/opt/work/project",
        **overrides,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("empty_layers", [False, True, None])
async def test_project_setup_is_quoted_in_mother_init(
    instructions, db_uri, tmp_path, empty_layers, monkeypatch
):
    projects = SqlAlchemyProjectStore(db_uri)
    project = projects.create(
        "a" * 32,
        "Saved setup",
        None,
        config={"orchestration": {"setup": 'a\nStanding pass: "x"', "child_cap": 4}},
    )
    if empty_layers:
        for scope, agent, host in (
            ("shared", "", ""),
            ("agent", "codex", ""),
            ("host", "", "host-a"),
            ("mother", "", ""),
            ("child", "", ""),
        ):
            instructions.save("", created_by=None, scope=scope, agent_key=agent, host_id=host)
    app = _build_app(db_uri, tmp_path)
    if empty_layers is None:
        monkeypatch.setattr(
            "omnigent.server.instruction_layers.get_global_instructions_store", lambda: None
        )
    # Use the callback wired by create_app, rather than reconstructing its dependencies.
    initializer = app.state.runner_session_initializer
    payloads = []

    def receive(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "initialized"})

    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.ASGITransport(app=app)
    ) as server_client:
        agent = await create_test_agent(server_client)
    conversations = SqlAlchemyConversationStore(db_uri)
    conversations.set_conversation_project(agent["_session_id"], project.id)
    conv = conversations.get_conversation(agent["_session_id"])
    conv.harness_override = "codex-native"
    conv.host_id = "host-a"
    conv.runner_id = "runner-a"
    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(receive)
    ) as client:
        await initializer.initialize(conv, client, timeout=1)
    text = payloads[0]["session_init"]["snapshot"]["global_instructions"]
    assert '): "a Standing pass: \\"x\\""; child cap 4.' in text
    assert (
        sum(line.startswith("Saved setup for this project (") for line in text.splitlines()) == 1
    )
    assert "\nStanding pass:" not in text
    captured = _stub_runner_client(monkeypatch)
    from omnigent.server.routes import sessions as sessions_mod

    async def skip_relay(*args, **kwargs):
        return None

    monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", skip_relay)
    monkeypatch.setattr(
        sessions_mod, "_registered_runner_id", lambda _router, raw, **kwargs: raw.strip()
    )
    async with httpx.AsyncClient(
        base_url="http://test", transport=httpx.ASGITransport(app=app)
    ) as server_client:
        response = await server_client.patch(
            f"/v1/sessions/{conv.id}", json={"runner_id": "rebound-runner"}
        )
    assert response.status_code == 200, response.text
    rebound_text = captured["body"]["session_init"]["snapshot"]["global_instructions"]
    assert '): "a Standing pass: \\"x\\""; child cap 4.' in rebound_text
    resolver = partial(
        project_setup_line,
        project_store=projects,
        conversation_store=SqlAlchemyConversationStore(db_uri),
        permission_store=None,
        prefs_store=None,
    )
    child_text = await session_instructions_text(
        conversation(project_id=project.id, parent_conversation_id="parent"),
        setup_resolver=resolver,
    )
    assert "Saved setup for this project" not in (child_text or "")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent,mother,expected",
    [
        (None, "Mother", "Mother\n\nQuota evidence"),
        (None, "", "S\n\nA\n\nH"),
        ("parent", "Mother", "Child"),
    ],
)
async def test_quota_resolver_runs_only_for_nonblank_mother(
    instructions, parent, mother, expected
):
    instructions.save(mother, created_by=None, scope="mother")
    resolver = Mock(return_value="Quota evidence")
    result = await session_instructions_text(
        conversation(parent_conversation_id=parent), quota_resolver=resolver
    )
    assert result.endswith(expected)
    assert resolver.call_count == (1 if parent is None and mother else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["mother", "child"])
@pytest.mark.parametrize("independent", ["", " Role cell "])
@pytest.mark.parametrize("route_independent", [False, True])
async def test_scoped_roles_compose_after_route_and_gate_quota(
    instructions, role, independent, route_independent
):
    instructions.save(" \n", created_by=None, scope=role)
    instructions.save(" Column ", created_by=None, scope=role, agent_key="codex")
    instructions.save(" Host ", created_by=None, scope=role, host_id="host-a")
    instructions.save("Other agent", created_by=None, scope=role, agent_key="claude")
    instructions.save(
        independent, created_by=None, scope=role, agent_key="codex", host_id="host-a"
    )
    if route_independent:
        instructions.save(
            "Route cell", created_by=None, scope="independent", agent_key="codex", host_id="host-a"
        )
    resolver = Mock(return_value="Quota")
    result = await session_instructions_text(
        conversation(parent_conversation_id="parent" if role == "child" else None),
        quota_resolver=resolver,
    )
    route_text = "Route cell" if route_independent else "S\n\nA\n\nH"
    role_text = independent if independent else " Column \n\n Host "
    assert result == route_text + "\n\n" + role_text + ("\n\nQuota" if role == "mother" else "")
    assert resolver.call_count == (1 if role == "mother" else 0)


@pytest.mark.asyncio
async def test_unmatched_mother_column_does_not_trigger_quota(instructions):
    instructions.save(" \n", created_by=None, scope="mother")
    instructions.save("Other mother", created_by=None, scope="mother", agent_key="claude")
    resolver = Mock(return_value="Quota")
    assert (
        await session_instructions_text(conversation(), quota_resolver=resolver) == "S\n\nA\n\nH"
    )
    resolver.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", [None, "parent"])
async def test_legacy_role_startup_preserves_exact_padding(instructions, parent):
    instructions.save("  Route\n", created_by=None)
    instructions.save("", created_by=None, scope="agent", agent_key="codex")
    instructions.save("", created_by=None, scope="host", host_id="host-a")
    instructions.save("  Mother \n", created_by=None, scope="mother")
    instructions.save("\tChild  ", created_by=None, scope="child")
    result = await session_instructions_text(
        conversation(parent_conversation_id=parent), quota_resolver=lambda _: " Quota \n"
    )
    assert result == (
        "  Route\n\n\n\tChild  " if parent else "  Route\n\n\n  Mother \n\n\n Quota \n"
    )


@pytest.mark.asyncio
async def test_initializer_transports_quota_after_mother_block(instructions):
    payloads = []

    def receive(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "initialized"})

    initializer = RunnerSessionInitializer(
        SimpleNamespace(get=lambda _: SimpleNamespace(generation=1)),
        server_version="test",
        quota_resolver=lambda _: "Quota evidence",
    )
    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(receive)
    ) as client:
        await initializer.initialize(conversation(), client, timeout=1)
    assert payloads[0]["session_init"]["snapshot"]["global_instructions"].endswith(
        "Mother\n\nQuota evidence"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope,agent,host,text,expected",
    [
        ("shared", "", "", "new S", "new S\n\nA\n\nH\n\nMother"),
        ("agent", "codex", "", "new A", "S\n\nnew A\n\nH\n\nMother"),
        ("host", "", "host-a", "new H", "S\n\nA\n\nnew H\n\nMother"),
        ("independent", "codex", "host-a", "I", "I\n\nMother"),
        ("mother", "", "", "new Mother", "S\n\nA\n\nH\n\nnew Mother"),
        ("child", "", "", "new Child", "S\n\nA\n\nH\n\nMother"),
    ],
)
async def test_each_layer_is_composed_for_its_session(
    instructions, scope, agent, host, text, expected
):
    instructions.save(text, created_by=None, scope=scope, agent_key=agent, host_id=host)
    assert await session_instructions_text(conversation()) == expected
    payloads = []

    def receive(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(201, json={"status": "initialized"})

    initializer = RunnerSessionInitializer(
        SimpleNamespace(get=lambda _: SimpleNamespace(generation=1)), server_version="test"
    )
    async with httpx.AsyncClient(
        base_url="http://runner", transport=httpx.MockTransport(receive)
    ) as client:
        await initializer.initialize(conversation(), client, timeout=1)
    assert payloads[0]["session_init"]["snapshot"]["global_instructions"] == expected


@pytest.mark.asyncio
async def test_children_take_their_own_route_and_only_the_child_role(instructions):
    instructions.save(
        "I", created_by=None, scope="independent", agent_key="codex", host_id="host-a"
    )
    assert (
        await session_instructions_text(conversation(parent_conversation_id="parent"))
        == "I\n\nChild"
    )


@pytest.mark.asyncio
async def test_unknown_spec_still_receives_shared_and_host_without_mother(
    instructions, monkeypatch
):
    monkeypatch.setattr(
        "omnigent.server.instruction_layers.get_agent_cache",
        lambda: SimpleNamespace(load=Mock(side_effect=ValueError("corrupt bundle"))),
    )
    conv = conversation()
    conv.harness_override = None
    # The existing resolver uses the same runtime cache seam.
    monkeypatch.setattr(
        "omnigent.runtime.get_agent_cache",
        lambda: SimpleNamespace(load=Mock(side_effect=ValueError("corrupt bundle"))),
    )
    assert await session_instructions_text(conv) == "S\n\nH"


@pytest.mark.asyncio
async def test_bundle_subagent_harness_selects_its_product(instructions, monkeypatch):
    from omnigent.runtime import _globals

    head = AgentSpec(
        spec_version=1,
        name="Worker",
        executor=ExecutorSpec(type="omnigent", config={"harness": "codex"}),
    )
    spec = AgentSpec(
        name="Brain",
        spec_version=1,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[head],
    )
    cache = SimpleNamespace(load=lambda *args, **kwargs: SimpleNamespace(spec=spec))
    agents = SimpleNamespace(
        get=lambda agent_id: SimpleNamespace(
            id=agent_id, bundle_location="bundle", operator_authored=False
        )
    )
    monkeypatch.setattr(_globals, "_agent_store", agents)
    monkeypatch.setattr("omnigent.runtime.get_agent_cache", lambda: cache)
    monkeypatch.setattr("omnigent.server.instruction_layers.get_agent_store", lambda: agents)
    monkeypatch.setattr("omnigent.server.instruction_layers.get_agent_cache", lambda: cache)
    conv = conversation(parent_conversation_id="parent", sub_agent_name="Worker")
    conv.harness_override = None
    assert await session_instructions_text(conv) == "S\n\nA\n\nH\n\nChild"


@pytest.mark.asyncio
async def test_missing_store_is_no_injection(monkeypatch):
    monkeypatch.setattr(
        "omnigent.server.instruction_layers.get_global_instructions_store", lambda: None
    )
    assert await session_instructions_text(conversation()) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("override", [True, False])
async def test_acp_layer_reaches_session_composition(instructions, monkeypatch, override):
    monkeypatch.setattr(
        "omnigent.onboarding.acp_auth.acp_agents",
        lambda: [SimpleNamespace(slug="neutral_agent", name="Neutral agent")],
    )
    spec = AgentSpec(
        spec_version=1,
        name="ACP worker",
        executor=ExecutorSpec(type="omnigent", config={"harness": "acp:neutral_agent"}),
    )
    monkeypatch.setattr("omnigent.server.instruction_layers._session_spec", lambda _conv: spec)

    async def resolve(_conv):
        return "acp"

    monkeypatch.setattr("omnigent.server.routes._sessions.helpers._resolve_harness_async", resolve)
    instructions.save("ACP layer", created_by=None, scope="agent", agent_key="acp:neutral_agent")
    conv = conversation()
    conv.harness_override = "acp:neutral_agent" if override else None
    assert await session_instructions_text(conv) == "S\n\nACP layer\n\nH"
