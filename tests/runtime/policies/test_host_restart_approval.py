"""Host restart approval cannot be disabled by an empty policy configuration."""

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.builder import any_policies_apply, build_policy_engine
from omnigent.spec import AgentSpec
from omnigent.spec.types import Phase, PolicyAction


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "dry_run,expected", [(True, PolicyAction.ALLOW), (False, PolicyAction.ASK)]
)
async def test_restart_gate_is_required_without_guardrails(conversation_store, dry_run, expected):
    conversation = conversation_store.create_conversation()
    spec = AgentSpec(spec_version=1)
    assert any_policies_apply(
        spec=spec,
        conversation_id=conversation.id,
        default_policies=None,
        policy_store=None,
        phase=Phase.TOOL_CALL,
        tool_name="sys_host_restart",
        conversation=conversation,
    )
    engine = build_policy_engine(
        spec=spec, conversation_id=conversation.id, conversation_store=conversation_store
    )
    result = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="sys_host_restart",
            content={
                "name": "sys_host_restart",
                "arguments": {"host": "test", "dry_run": dry_run},
            },
        )
    )
    assert result.action == expected
