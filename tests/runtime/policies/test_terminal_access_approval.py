"""Admin access always asks through the application, including independent SDKs."""

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.builder import any_policies_apply, build_policy_engine
from omnigent.spec import AgentSpec
from omnigent.spec.types import Phase, PolicyAction


@pytest.mark.asyncio
async def test_terminal_access_never_takes_the_no_policy_fast_path(conversation_store):
    spec = AgentSpec(spec_version=1)
    conversation = conversation_store.create_conversation()
    name = "sys_terminal_request_access"
    assert any_policies_apply(
        spec=spec,
        conversation_id=conversation.id,
        default_policies=None,
        policy_store=None,
        phase=Phase.TOOL_CALL,
        tool_name=name,
    )
    engine = build_policy_engine(
        spec=spec, conversation_id=conversation.id, conversation_store=conversation_store
    )
    result = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name=name,
            content={
                "name": name,
                "arguments": {
                    "terminal": "bash",
                    "session": "admin",
                    "instance_id": "one",
                    "reason": "Requested work",
                },
            },
        )
    )
    assert result.action == PolicyAction.ASK
    assert "__ask_on_terminal_agent_access" in result.deciding_policies
