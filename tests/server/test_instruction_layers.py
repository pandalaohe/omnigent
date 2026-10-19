"""Session route resolution and layered text at the init composition seam."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.server.instruction_layers import session_instructions_text
from omnigent.server.runner_session_init import RunnerSessionInitializer
from omnigent.spec import AgentSpec, ExecutorSpec
from omnigent.stores.global_instructions_store.sqlalchemy_store import (
    SqlAlchemyGlobalInstructionsStore,
)


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
