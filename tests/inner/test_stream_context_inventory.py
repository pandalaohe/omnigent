"""History IDs and request streams must not become canonical context records."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.inner import openai_agents_sdk_executor as agents_owner
from omnigent.inner.databricks_executor import DatabricksExecutor
from omnigent.inner.executor import ExecutorConfig, TextChunk
from omnigent.inner.open_responses_sdk import OpenResponsesExecutor
from omnigent.inner.openai_agents_sdk_executor import OpenAIAgentsSDKExecutor
from tests.inner.test_databricks_executor import FakeClient as DatabricksClient
from tests.inner.test_databricks_executor import _make_text_stream
from tests.inner.test_open_responses_sdk import FakeClient as ResponsesClient
from tests.inner.test_open_responses_sdk import FakeResponse
from tests.inner.test_openai_agents_sdk_executor import (
    _fake_agents_sdk,
    _FakeRawEvent,
    _FakeRawTextDelta,
    _FakeResult,
    _FakeRunner,
)


class _ClosableIterator:
    """Only the transport-close boundary is missing from the existing list fakes."""

    def __init__(self, items):
        self._items = iter(items)
        self.close = Mock()

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._items)


@pytest.fixture(params=["agents", "responses", "databricks"])
def owner(request, monkeypatch):
    family = request.param
    if family == "agents":
        monkeypatch.setattr(agents_owner, "_ensure_agents_sdk", _fake_agents_sdk)
        monkeypatch.setattr(_FakeRunner, "last_calls", [])
        monkeypatch.setattr(_FakeRunner, "next_results", [])
        monkeypatch.setattr(
            _FakeRunner,
            "next_result",
            _FakeResult(
                events=[_FakeRawEvent(_FakeRawTextDelta("hello"))],
                final_output="hello",
            ),
        )
        executor = OpenAIAgentsSDKExecutor(client=object(), model="fixture-model")
    elif family == "responses":
        stream = _ClosableIterator(
            [
                SimpleNamespace(
                    type="response.output_text.delta",
                    delta="hello",
                    item_id="message",
                    output_index=0,
                ),
                SimpleNamespace(
                    type="response.completed", response=FakeResponse(id="response-chain")
                ),
            ]
        )
        executor = OpenResponsesExecutor(client=ResponsesClient(stream))
    else:
        stream = _ClosableIterator(_make_text_stream("hello"))
        client = DatabricksClient([])
        monkeypatch.setattr(client.chat.completions, "create", Mock(return_value=stream))
        executor = DatabricksExecutor(client=client)
    return family, executor


async def test_stream_source_is_explicitly_unsupported_without_touching_client(owner):
    family, executor = owner
    executor._get_or_create_session_state = Mock(side_effect=AssertionError("no history creation"))
    executor._client = SimpleNamespace(
        responses=SimpleNamespace(create=Mock(side_effect=AssertionError("no response request"))),
        chat=SimpleNamespace(
            completions=SimpleNamespace(create=Mock(side_effect=AssertionError("no chat request")))
        ),
    )
    read = await executor.prepare_context_inventory_v2("session", "read")
    for _ in range(2):
        evidence = read()
        (source,) = evidence.sources
        assert source.coverage == "unsupported" and source.contexts == ()
        assert source.unresolved[0].reason.startswith(family + "_")
        assert source.unresolved[0].reason != "context_source_unimplemented"
        assert source.producer_incarnation is None
        assert source.revision_authority == "observation_only"
        assert (
            source.work_bindings is source.membership_revision is source.age_upper_bound_s is None
        )
        assert evidence.read_id == "read" and evidence.authority.session_id == "session"
    executor._get_or_create_session_state.assert_not_called()
    executor._client.responses.create.assert_not_called()
    executor._client.chat.completions.create.assert_not_called()
    invalid = await executor.prepare_context_inventory_v2("session", "bad", timeout_s=0)
    assert invalid().sources[0].coverage == "partial"
    assert invalid().sources[0].unresolved[0].reason == "invalid_read_budget"


async def test_actual_history_and_active_stream_never_prove_context_or_stop(owner):
    family, executor = owner
    read = await executor.prepare_context_inventory_v2("session", "read")
    turn = executor.run_turn(
        [{"role": "user", "content": "hello", "session_id": "execution"}],
        [],
        "",
        ExecutorConfig(model="fixture-model"),
    )
    try:
        event = await asyncio.wait_for(anext(turn), 5)
        assert isinstance(event, TextChunk) and event.text == "hello"
        state = executor._session_states["execution"]
        if family == "agents":
            assert state.sdk_session.session_id == "execution" and state.active_result is not None
            state.sdk_session.get_items = AsyncMock(
                side_effect=AssertionError("no transcript read")
            )
        else:
            assert state.active_stream is not None
            if family == "responses":
                state.previous_response_id = "opaque-replay-id"
        assert read().sources[0].coverage == "unsupported"
        assert not read().sources[0].contexts
        assert await executor.interrupt_session("execution")
        assert read().sources[0].coverage == "unsupported"
        await executor.close_session("execution")
        assert executor._session_states == {}
        source = read().sources[0]
        assert source.coverage == "unsupported" and source.contexts == ()
        assert source.unresolved and source.work_bindings is None
    finally:
        await turn.aclose()
