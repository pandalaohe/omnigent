"""Project-default updates ask unless an admin disables the default policy."""

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

_HANDLER = "omnigent.policies.builtins.safety.ask_on_project_settings_update"


@pytest.fixture(autouse=True)
def clear_default_policy_cache():
    invalidate_default_policy_specs_cache()
    yield
    invalidate_default_policy_specs_cache()


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,enabled", [(False, False), (True, True), (True, False)])
async def test_project_update_approval_default_can_be_changed_only_by_admin_policy(
    conversation_store, db_uri, configured, enabled
):
    conversation = conversation_store.create_conversation()
    policies = SqlAlchemyPolicyStore(db_uri)
    if configured:
        policies.create_default("a" * 32, "project approval", "python", _HANDLER, enabled=enabled)
    spec = AgentSpec(spec_version=1)
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conversation.id,
        conversation_store=conversation_store,
        policy_store=policies,
    )
    for name in ("sys_project_settings_read", "sys_project_settings_update"):
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
                content={"name": name, "arguments": {"project_id": "project-a"}},
            )
        )
        assert result.action == (
            PolicyAction.ASK
            if name.endswith("update") and (not configured or enabled)
            else PolicyAction.ALLOW
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "orchestration",
    [
        {"setup": "Saved"},
        {"child_cap": None},
        {"standing_pass": {"remove": None}},
        {"standing_pass": {"exceptions": ["production release"]}},
    ],
)
async def test_child_and_session_disabled_policy_cannot_remove_default_ask(
    conversation_store, db_uri, orchestration
):
    root = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        parent_conversation_id=root.id, kind="sub_agent"
    )
    policies = SqlAlchemyPolicyStore(db_uri)
    policies.create("b" * 32, root.id, "session override", "python", _HANDLER, enabled=False)
    engine = build_policy_engine(
        spec=AgentSpec(spec_version=1),
        conversation_id=child.id,
        conversation_store=conversation_store,
        policy_store=policies,
    )
    result = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="sys_project_settings_update",
            content={
                "name": "different_transformed_name",
                "arguments": {"project_id": "project-a", "orchestration": orchestration},
            },
        )
    )
    assert result.action == PolicyAction.ASK
