"""Instruction updates ask by default; only admin policy settings can relax it."""

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.builder import (
    any_policies_apply,
    build_policy_engine,
    invalidate_default_policy_specs_cache,
)
from omnigent.spec import AgentSpec
from omnigent.spec.types import Phase, PolicyAction
from omnigent.stores.policy_store.sqlalchemy_store import SqlAlchemyPolicyStore

_HANDLER = "omnigent.policies.builtins.safety.ask_on_instruction_layer_update"


@pytest.fixture(autouse=True)
def clear_default_policy_cache():
    invalidate_default_policy_specs_cache()
    yield
    invalidate_default_policy_specs_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,enabled", [(False, False), (True, True), (True, False)])
async def test_update_approval_uses_existing_default_policy_setting(
    conversation_store, db_uri, configured, enabled
):
    spec = AgentSpec(spec_version=1)
    conversation = conversation_store.create_conversation()
    policies = SqlAlchemyPolicyStore(db_uri)
    if configured:
        policies.create_default(
            "a" * 32,
            "instruction approval",
            "python",
            _HANDLER,
            enabled=enabled,
        )
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conversation.id,
        conversation_store=conversation_store,
        policy_store=policies,
    )
    for name in ("sys_instruction_layer_update", "sys_instruction_layers_read"):
        assert any_policies_apply(
            spec=spec,
            conversation_id=conversation.id,
            default_policies=None,
            policy_store=policies,
            phase=Phase.TOOL_CALL,
            tool_name=name,
        ) == (name.endswith("update") or (configured and enabled))
        result = await engine.evaluate(
            EvaluationContext(
                phase=Phase.TOOL_CALL,
                tool_name=name,
                content={"name": name, "arguments": {"scope": "child", "text": "New"}},
            )
        )
        expected = (
            PolicyAction.ASK
            if name.endswith("update") and (not configured or enabled)
            else PolicyAction.ALLOW
        )
        assert result.action == expected


@pytest.mark.asyncio
async def test_session_policy_cannot_disable_default_approval(conversation_store, db_uri):
    conversation = conversation_store.create_conversation()
    policies = SqlAlchemyPolicyStore(db_uri)
    policies.create(
        "b" * 32, conversation.id, "local instruction approval", "python", _HANDLER, enabled=False
    )
    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1),
        conversation_id=conversation.id,
        conversation_store=conversation_store,
        policy_store=policies,
    )
    result = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="sys_instruction_layer_update",
            content={"name": "different_transformed_name", "arguments": {"scope": "shared"}},
        )
    )
    assert result.action == PolicyAction.ASK


@pytest.mark.asyncio
async def test_child_keeps_default_update_approval(conversation_store, db_uri):
    root = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        parent_conversation_id=root.id, kind="sub_agent"
    )
    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1),
        conversation_id=child.id,
        conversation_store=conversation_store,
        policy_store=SqlAlchemyPolicyStore(db_uri),
    )
    result = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="sys_instruction_layer_update",
            content={
                "name": "sys_instruction_layer_update",
                "arguments": {"scope": "mother", "text": "New"},
            },
        )
    )
    assert result.action == PolicyAction.ASK
