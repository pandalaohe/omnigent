"""Cold Claude SDK startup receipts come from the CLI's current main turn."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, TextBlock

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor, _ClaudeClientState
from omnigent.inner.executor import TextChunk, ToolCallRequest, TurnComplete
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload, TurnContext
from omnigent.server.schemas import CreateResponseRequest
from omnigent.startup_evidence import startup_evidence_scope
from omnigent.startup_protocol import StartupAttemptRef


@pytest.fixture(autouse=True)
def fake_sdk_only(monkeypatch):
    def refuse_real_sdk():
        pytest.fail("This test must supply a fake SDK before running a query")

    monkeypatch.setattr("omnigent.inner.claude_sdk_executor._ensure_sdk", refuse_real_sdk)
    monkeypatch.setattr("omnigent.inner.claude_sdk_executor._find_system_claude", lambda: None)


def _attempt() -> StartupAttemptRef:
    return StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="context",
        host_generation="generation",
    )


@pytest.mark.asyncio
async def test_claude_startup_requires_vendor_prompt_then_main_work():
    from omnigent.startup_evidence import ClaudeTurnEvidence

    emitted = []
    with startup_evidence_scope(_attempt(), emitted.append):
        evidence = ClaudeTurnEvidence("current prompt")
        evidence.prompt_submitted(
            {"hook_event_name": "UserPromptSubmit", "session_id": "old", "prompt": "old prompt"}
        )
        evidence.message(
            StreamEvent(
                uuid="old",
                session_id="old",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "old"},
                },
            )
        )
        evidence.prompt_submitted(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "cli-session",
                "prompt": "current prompt",
                "agent_id": "child",
            }
        )
        assert emitted == []
        evidence.prompt_submitted(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "cli-session",
                "prompt": "current prompt",
            }
        )
        assert [item.phase for item in emitted] == [
            "input_dispatched",
            "runtime_ready",
            "tools_ready",
        ]
        evidence.message(
            StreamEvent(
                uuid="child",
                session_id="cli-session",
                parent_tool_use_id="tool",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "child"},
                },
            )
        )
        evidence.message(
            StreamEvent(
                uuid="other",
                session_id="other",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "other"},
                },
            )
        )
        evidence.message(
            StreamEvent(
                uuid="empty",
                session_id="cli-session",
                event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": ""}},
            )
        )
        evidence.message(
            StreamEvent(
                uuid="main",
                session_id="cli-session",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "working"},
                },
            )
        )
        evidence.message(
            AssistantMessage(
                content=[TextBlock(text="done")], model="claude-test", session_id="cli-session"
            )
        )
    assert [item.phase for item in emitted] == [
        "input_dispatched",
        "runtime_ready",
        "tools_ready",
        "input_accepted",
        "work_started",
    ]
    assert all(
        item.source == "claude-sdk"
        and item.vendor_session_id == "cli-session"
        and item.vendor_turn_id == "cli-session"
        for item in emitted
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_work", [False, True, "queue"])
@pytest.mark.parametrize("replacement", ["permission", "keep_warm", "receipt_warm"])
async def test_claude_executor_claims_before_connect_and_ignores_enqueue_only(
    provider_work, replacement
):
    emitted = []
    order = []

    class FakeOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeSDK:
        ClaudeAgentOptions = FakeOptions
        AssistantMessage = AssistantMessage
        UserMessage = type("UserMessage", (), {})
        SystemMessage = type("SystemMessage", (), {})
        ResultMessage = ResultMessage
        StreamEvent = StreamEvent
        TextBlock = TextBlock
        ThinkingBlock = type("ThinkingBlock", (), {})
        ToolUseBlock = type("ToolUseBlock", (), {})
        ToolResultBlock = type("ToolResultBlock", (), {})

        @staticmethod
        def HookMatcher(*, hooks, **kwargs):
            return type("Matcher", (), {"hooks": hooks})()

        class ClaudeSDKClient:
            def __init__(self, options):
                self.options = options

            async def connect(self):
                order.append("connect")

            async def query(self, prompt, session_id="default"):
                order.append("query")

            async def receive_response(self):
                if provider_work is True:
                    for matcher in self.options.hooks["UserPromptSubmit"]:
                        for hook in matcher.hooks:
                            await hook(
                                {
                                    "hook_event_name": "UserPromptSubmit",
                                    "session_id": "cli-session",
                                    "prompt": "current prompt",
                                },
                                None,
                                None,
                            )
                    yield StreamEvent(
                        uuid="main",
                        session_id="cli-session",
                        event={
                            "type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": "working"},
                        },
                    )
                yield ResultMessage(
                    subtype="success",
                    duration_ms=1,
                    duration_api_ms=1,
                    is_error=False,
                    num_turns=1,
                    session_id="cli-session",
                    result="done",
                )

    async def claim(attempt, boundary):
        order.append("claim")
        return True

    executor = ClaudeSDKExecutor()
    with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=FakeSDK):
        ref = _attempt()
        if provider_work == "queue":
            ref = ref.model_copy(update={"mode": "startup_only", "policy_revision": 1})
        with startup_evidence_scope(ref, emitted.append, claim):
            events = [
                event
                async for event in executor.run_turn(
                    [{"role": "user", "content": "current prompt", "session_id": "session"}],
                    [],
                    "",
                )
            ]
        if provider_work == "queue":
            assert [item.phase for item in emitted] == ["opening_settled"]
            assert emitted[0].source == "runner" and emitted[0].attempt == ref
            assert order == ["claim", "connect", "query"]
            assert any(isinstance(event, TurnComplete) for event in events)
            return
        with startup_evidence_scope(None, emitted.append, claim, required=True):
            followup = [
                event
                async for event in executor.run_turn(
                    [{"role": "user", "content": "followup", "session_id": "session"}],
                    [],
                    "",
                )
            ]
        assert followup
        previous = executor._clients["session"]
        slot = executor._query_slot("session")
        if replacement == "keep_warm":
            slot.maintenance = True
            slot.ping_done.clear()
        adapter = ExecutorAdapter(
            lambda: executor, session_key="session", startup_source="claude-sdk"
        )
        adapter._executor = executor
        request = CreateResponseRequest(
            model="agent",
            input="work",
            startup_required=True,
            permission_mode="plan" if replacement == "permission" else None,
            startup_attempt=_attempt() if replacement == "receipt_warm" else None,
        )
        ctx = TurnContext("resp_" + "a" * 32, asyncio.Queue(), asyncio.Event(), "session")
        ctx.evaluate_policy = AsyncMock(
            return_value=PolicyVerdictPayload(action="POLICY_ACTION_ALLOW")
        )
        with (
            patch.object(executor, "_close_live_client", new_callable=AsyncMock) as close,
            patch("omnigent.inner.claude_sdk_executor._KEEP_WARM_PREEMPT_WAIT_S", 0),
        ):
            refusal = (
                "startup proof unavailable"
                if replacement == "receipt_warm"
                else "Startup permission unavailable"
            )
            with pytest.raises(RuntimeError, match=refusal):
                await adapter.run_turn(request, ctx)
            close.assert_not_awaited()
        assert executor._clients["session"] is previous
        assert adapter._executor is executor and adapter._abandoned_executor_cleanup is None
        assert "session" not in executor._crashed_sessions
        assert slot.maintenance == (replacement == "keep_warm")
        assert order.count("query") == 2
    assert order[:3] == ["claim", "connect", "query"]
    assert [item.phase for item in emitted] == (
        ["input_dispatched", "runtime_ready", "tools_ready", "input_accepted", "work_started"]
        if provider_work
        else []
    )
    assert events


@pytest.mark.asyncio
async def test_claude_executor_refuses_unprovable_startup_before_claim():
    executor = ClaudeSDKExecutor()
    claim = AsyncMock(return_value=True)

    class NoHookSDK:
        pass

    with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=NoHookSDK):
        with startup_evidence_scope(_attempt(), lambda _: None, claim):
            with pytest.raises(RuntimeError, match="startup proof unavailable"):
                _ = [
                    event
                    async for event in executor.run_turn(
                        [{"role": "user", "content": "hello"}], [], ""
                    )
                ]
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_claude_executor_denied_claim_never_constructs_client():
    executor = ClaudeSDKExecutor()
    claim = AsyncMock(return_value=False)

    class HookSDK:
        @staticmethod
        def HookMatcher(*, hooks):
            return hooks

        @staticmethod
        def ClaudeSDKClient(options):
            raise AssertionError("client must not be constructed")

    with patch("omnigent.inner.claude_sdk_executor._ensure_sdk", return_value=HookSDK):
        with startup_evidence_scope(_attempt(), lambda _: None, claim):
            with pytest.raises(RuntimeError, match="Startup permission unavailable"):
                _ = [
                    event
                    async for event in executor.run_turn(
                        [{"role": "user", "content": "hello"}], [], ""
                    )
                ]
    claim.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_startup", "second_startup", "second_allowed"),
    [
        (False, False, True),
        (True, False, False),
        (False, True, False),
        (True, True, False),
    ],
)
async def test_claude_executor_keeps_startup_stream_exclusive(
    first_startup, second_startup, second_allowed
):
    executor = ClaudeSDKExecutor()
    messages = [{"role": "user", "content": "hello", "session_id": "session"}]
    emitted = []

    async def fake_impl(*args):
        yield TextChunk(text="started")

    with patch.object(executor, "_run_turn_impl", fake_impl):
        first = executor.run_turn(messages, [], "")
        with startup_evidence_scope(_attempt() if first_startup else None, emitted.append):
            assert (await anext(first)).text == "started"
        second = executor.run_turn(messages, [], "")
        with startup_evidence_scope(_attempt() if second_startup else None, emitted.append):
            if second_allowed:
                assert (await anext(second)).text == "started"
            else:
                with pytest.raises(RuntimeError, match="query already active"):
                    await anext(second)
        await second.aclose()
        await first.aclose()
    assert not executor._active_turns
    assert emitted == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_startup", "second_startup"),
    [(True, False), (True, True), (False, False)],
)
async def test_rejected_overlap_preserves_first_adapter_stream_and_client(
    first_startup, second_startup
):
    executor = ClaudeSDKExecutor()
    client = AsyncMock()
    state = _ClaudeClientState(client=client, model=None)
    executor._clients["session"] = state
    adapter = ExecutorAdapter(lambda: executor, session_key="session", startup_source="claude-sdk")
    adapter._executor = executor
    ready = asyncio.Event()
    release = asyncio.Event()
    bridge_results = []
    policy_verdicts = []
    turn_count = 0

    async def fake_impl(*args):
        nonlocal turn_count
        turn_count += 1
        if turn_count > 1:
            yield TurnComplete(response="unexpected overlapping turn")
            return
        yield TextChunk(text="first response")
        yield ToolCallRequest(
            name="mcp__omnigent__search.web",
            args={"q": "first"},
            metadata={"call_id": "call_first"},
        )
        ready.set()
        await release.wait()
        bridge_results.append(await executor._tool_executor("search.web", {"q": "first"}))
        policy_verdicts.append(
            await executor._policy_evaluator("PHASE_TOOL_CALL", {"name": "search.web"})
        )
        yield TurnComplete(response="finished")

    first = CreateResponseRequest(
        model="agent", input="first", startup_attempt=_attempt() if first_startup else None
    )
    second = CreateResponseRequest(
        model="agent",
        input="second",
        startup_attempt=_attempt() if second_startup else None,
    )
    first_ctx = TurnContext("resp_" + "a" * 32, asyncio.Queue(), asyncio.Event(), "session")
    second_ctx = TurnContext("resp_" + "b" * 32, asyncio.Queue(), asyncio.Event(), "session")
    first_ctx.dispatch_tool = AsyncMock(return_value="first tool output")
    second_ctx.dispatch_tool = AsyncMock(return_value="wrong turn")
    first_ctx.evaluate_policy = AsyncMock(
        return_value=PolicyVerdictPayload(action="POLICY_ACTION_ALLOW")
    )
    second_ctx.evaluate_policy = AsyncMock(
        return_value=PolicyVerdictPayload(action="POLICY_ACTION_DENY")
    )
    with (
        patch.object(executor, "_run_turn_impl", fake_impl),
        patch.object(adapter, "_safe_interrupt", new_callable=AsyncMock) as cleanup,
        patch.object(executor, "_force_close_client", new_callable=AsyncMock),
    ):
        first_task = asyncio.create_task(adapter.run_turn(first, first_ctx))
        try:
            await asyncio.wait_for(ready.wait(), timeout=2)
            with pytest.raises(RuntimeError, match="query already active"):
                await adapter.run_turn(second, second_ctx)
        finally:
            release.set()
            await first_task
        assert bridge_results == [{"result": "first tool output"}]
        assert [verdict.action for verdict in policy_verdicts] == ["POLICY_ACTION_ALLOW"]
        assert turn_count == 1
        first_ctx.dispatch_tool.assert_awaited_once_with(
            call_id="call_first",
            name="search.web",
            arguments='{"q": "first"}',
            agent="agent",
        )
        second_ctx.dispatch_tool.assert_not_awaited()
        first_ctx.evaluate_policy.assert_awaited_once()
        second_ctx.evaluate_policy.assert_not_awaited()
        await asyncio.sleep(0)
        cleanup.assert_not_awaited()
        assert adapter._executor is executor
        assert executor._clients["session"] is state
        assert "session" not in executor._crashed_sessions
        await executor.close_session("session")
        client.disconnect.assert_awaited_once()
