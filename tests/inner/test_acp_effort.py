"""Advertised ACP effort is model-specific and must be acknowledged."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from omnigent.inner.acp_executor import AcpAgentConfig, AcpExecutor
from omnigent.inner.executor import ExecutorConfig, ExecutorError, TurnComplete
from omnigent.util.reasoning_effort import efforts_for_harness


def options(model="model-a", values=("quick", "deliberate"), current="quick"):
    result = [
        {
            "id": "model",
            "category": "model",
            "currentValue": model,
            "options": [{"value": "model-a", "name": "A"}, {"value": "model-b", "name": "B"}],
        }
    ]
    if values:
        result.append(
            {
                "id": "thinking-budget",
                "category": "thought_level",
                "type": "select",
                "currentValue": current,
                "options": [{"value": value, "name": value} for value in values],
            }
        )
    return result


@pytest.fixture
def executor(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    return AcpExecutor(
        AcpAgentConfig(command="neutral-acp-agent", omnigent_mcp=False), cwd=str(tmp_path)
    )


def test_acp_effort_is_deferred_to_the_session_advertisement():
    assert efforts_for_harness("acp") is None
    assert efforts_for_harness("acp:neutral") is None


def test_effort_without_a_model_option_is_visible(executor):
    executor._note_config_options(options()[1:])
    assert executor.model_options()[0]["isDefault"]
    assert executor.model_options()[0]["efforts"] == ["quick", "deliberate"]


@pytest.mark.asyncio
async def test_effort_uses_category_id_values_and_confirmation(executor):
    executor._note_config_options(options())
    executor._rpc = AsyncMock(
        return_value={"result": {"configOptions": options(current="deliberate")}}
    )
    await executor._apply_effort_override("fresh-session", "deliberate")
    executor._rpc.assert_awaited_once_with(
        "session/set_config_option",
        {"sessionId": "fresh-session", "configId": "thinking-budget", "value": "deliberate"},
    )
    assert executor.model_options()[0]["efforts"] == ["quick", "deliberate"]


@pytest.mark.asyncio
async def test_absent_category_skips_stored_effort_and_logs(executor, caplog):
    executor._note_config_options(
        [{"id": "effort", "type": "select", "options": [{"value": "high"}]}]
    )
    executor._rpc = AsyncMock()
    with caplog.at_level("INFO"):
        await executor._apply_effort_override("fresh-session", "high")
    executor._rpc.assert_not_awaited()
    assert "not advertised" in caplog.text


@pytest.mark.asyncio
async def test_model_change_replaces_effort_and_drops_absent_option(executor, caplog):
    executor._note_config_options(options())
    executor._rpc = AsyncMock(return_value={"result": {"configOptions": options("model-b", ())}})
    await executor._apply_model_override("fresh-session", "model-b")
    with pytest.raises(RuntimeError, match="not advertised"):
        await executor._apply_effort_override("fresh-session", "deliberate")
    assert executor._rpc.await_count == 1
    assert executor.model_options()[1]["efforts"] == []
    executor._note_config_options(options("model-b", ("precise",), "precise"))
    assert executor.model_options()[1]["efforts"] == ["precise"]


@pytest.mark.asyncio
async def test_model_default_stays_stable_after_switch(executor):
    executor._config = replace(executor._config, default_model="")
    executor._note_config_options(options())
    executor._rpc = AsyncMock(return_value={"result": {"configOptions": options("model-b")}})
    await executor._apply_model_override("fresh-session", "model-b")
    rows = executor.model_options()
    assert rows[0]["isDefault"] and not rows[0]["isActive"]
    assert rows[1]["isActive"] and not rows[1]["isDefault"]
    await executor._apply_model_override("fresh-session", "model-b")
    assert executor._rpc.await_count == 1
    executor._rpc.return_value = {"result": {"configOptions": options()}}
    await executor._apply_model_override("fresh-session", None)
    assert executor._active_model == "model-a"


@pytest.mark.asyncio
async def test_legacy_models_and_effort_only_options_keep_initial_reset(executor):
    executor._config = replace(executor._config, available_models=("model-a", "model-b"))
    executor._rpc = AsyncMock(
        return_value={
            "result": {
                "sessionId": "fresh-session",
                "models": {
                    "currentModelId": "model-a",
                    "availableModels": [
                        {"modelId": "model-a"},
                        {"modelId": "model-b"},
                    ],
                },
                "configOptions": options()[1:],
            }
        }
    )
    await executor._ensure_session()
    assert [row["id"] for row in executor.model_options()] == ["model-a", "model-b"]
    assert executor.model_options()[0]["efforts"] == ["quick", "deliberate"]
    executor._rpc.side_effect = [
        {"result": {"configOptions": options(current="deliberate")[1:]}},
        {"result": {"configOptions": options()[1:]}},
    ]
    await executor._apply_effort_override("fresh-session", "deliberate")
    await executor._apply_effort_override("fresh-session", None)
    assert executor._rpc.await_args is not None
    assert executor._rpc.await_args.args[1]["value"] == "quick"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {"error": {"code": -32602, "message": "effort not found"}},
        {"result": {"configOptions": options(current="quick")}},
        {"result": {}},
    ],
)
async def test_refusal_or_missing_confirmation_is_error(executor, response):
    executor._note_config_options(options())
    executor._rpc = AsyncMock(return_value=response)
    with pytest.raises(RuntimeError, match="effort"):
        await executor._apply_effort_override("fresh-session", "deliberate")


@pytest.mark.asyncio
async def test_clear_restores_agent_initial_effort(executor):
    executor._note_config_options(options())
    executor._rpc = AsyncMock(
        side_effect=[
            {"result": {"configOptions": options(current="deliberate")}},
            {"result": {"configOptions": options(current="quick")}},
        ]
    )
    await executor._apply_effort_override("fresh-session", "deliberate")
    await executor._apply_effort_override("fresh-session", None)
    assert executor._rpc.await_args is not None
    assert executor._rpc.await_args.args[1]["value"] == "quick"


@pytest.mark.asyncio
async def test_turn_transports_effort_before_prompt_and_blocks_refusal(executor):
    executor._proc = type("Running", (), {"returncode": None})()
    executor._ensure_initialized = AsyncMock()
    executor._ensure_session = AsyncMock(return_value="fresh-session")

    async def finish_prompt(message):
        executor._pending.pop(message["id"]).set_result({"result": {"stopReason": "end_turn"}})

    executor._send = AsyncMock(side_effect=finish_prompt)
    executor._note_config_options(options())
    executor._rpc = AsyncMock(return_value={"error": {"code": -32602, "message": "not available"}})
    events = [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "unused"}],
            [],
            "",
            ExecutorConfig(extra={"reasoning_effort": "deliberate"}),
        )
    ]
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert events[0].retryable is False
    assert "No prompt was sent" in events[0].message
    assert not any(isinstance(event, TurnComplete) for event in events)
    assert executor._rpc.await_args is not None
    assert executor._rpc.await_args.args[0] == "session/set_config_option"
    executor._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_turn_confirms_effort_before_prompt(executor):
    executor._proc = type("Running", (), {"returncode": None})()
    executor._ensure_initialized = AsyncMock()
    executor._ensure_session = AsyncMock(return_value="fresh-session")
    executor._note_config_options(options())
    executor._rpc = AsyncMock(
        return_value={"result": {"configOptions": options(current="deliberate")}}
    )

    async def finish_prompt(message):
        assert executor._effort_option["currentValue"] == "deliberate"
        executor._rpc.assert_awaited_once()
        assert message["method"] == "session/prompt"
        executor._pending.pop(message["id"]).set_result({"result": {"stopReason": "end_turn"}})

    executor._send = AsyncMock(side_effect=finish_prompt)
    events = [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "unused"}],
            [],
            "",
            ExecutorConfig(extra={"reasoning_effort": "deliberate"}),
        )
    ]
    assert isinstance(events[-1], TurnComplete)
    executor._send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog", [False, True])
async def test_review_major1_switch_without_echo_still_applies_effort(executor, catalog):
    executor._note_config_options(options())
    if catalog:
        executor._session_model_ids = {"model-a", "model-b"}
    executor._rpc = AsyncMock(
        side_effect=[
            {"result": {}},
            {"result": {"configOptions": options("model-b", ("precise",), "precise")}},
        ]
    )
    await executor._apply_model_override("fresh-session", "model-b")
    await executor._apply_effort_override("fresh-session", "precise")
    assert executor._rpc.await_count == 2
    assert executor._rpc.await_args is not None
    assert executor._rpc.await_args.args[1]["configId"] == "thinking-budget"


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog", [False, True])
@pytest.mark.parametrize("failure", ["error", "transport"])
async def test_review_major1_failed_switch_keeps_advertisement(executor, catalog, failure):
    executor._note_config_options(options())
    if catalog:
        executor._session_model_ids = {"model-a", "model-b"}
    original = executor._effort_option

    async def rpc(*args, **kwargs):
        # Even a notification arriving during the refused RPC must roll back.
        executor._note_config_options(options("model-b", ()))
        if failure == "transport":
            raise ConnectionError("neutral disconnected")
        return {"error": {"code": -32602, "message": "model refused"}}

    executor._rpc = AsyncMock(side_effect=rpc)
    if failure == "transport":
        with pytest.raises(ConnectionError):
            await executor._apply_model_override("fresh-session", "model-b")
    else:
        await executor._apply_model_override("fresh-session", "model-b")
    assert executor._active_model == "model-a"
    assert executor._effort_option == original
    assert executor.model_options()[0]["efforts"] == ["quick", "deliberate"]


@pytest.mark.asyncio
async def test_review_major1_new_model_without_effort_refuses_visibly(executor):
    executor._note_config_options(options())
    executor._rpc = AsyncMock(return_value={"result": {"configOptions": options("model-b", ())}})
    await executor._apply_model_override("fresh-session", "model-b")
    with pytest.raises(RuntimeError, match="not advertised"):
        await executor._apply_effort_override("fresh-session", "deliberate")
    assert executor._rpc.await_count == 1


@pytest.mark.asyncio
async def test_review_minor1_clear_does_not_capture_carried_override(executor):
    executor._note_config_options(options("model-b"))
    executor._note_config_options(options())
    executor._rpc = AsyncMock(
        side_effect=[
            {"result": {"configOptions": options(current="deliberate")}},
            {"result": {"configOptions": options("model-b", current="deliberate")}},
            {"result": {"configOptions": options("model-b")}},
        ]
    )
    await executor._apply_effort_override("fresh-session", "deliberate")
    await executor._apply_model_override("fresh-session", "model-b")
    await executor._apply_effort_override("fresh-session", None)
    assert executor._rpc.await_args is not None
    assert executor._rpc.await_args.args[1]["value"] == "quick"


@pytest.mark.asyncio
async def test_review_minor1_unknown_default_is_not_invented_from_override(executor):
    executor._note_config_options(options())
    executor._effort_overridden = True
    executor._rpc = AsyncMock(
        return_value={"result": {"configOptions": options("model-b", current="deliberate")}}
    )
    await executor._apply_model_override("fresh-session", "model-b")
    with pytest.raises(RuntimeError, match="no advertised initial value"):
        await executor._apply_effort_override("fresh-session", None)
    assert executor._rpc.await_count == 1


def test_review_minor2_inactive_models_keep_last_advertisement(executor):
    executor._note_config_options(options("model-b", ("precise",), "precise"))
    executor._note_config_options(options())
    assert executor.model_options()[1]["efforts"] == ["precise"]


def test_review_nit_b_full_config_state_replaces_removed_ids(executor):
    executor._note_config_options(options())
    executor._note_config_options(options()[1:])
    assert executor._config_option_ids == {"thinking-budget"}
    executor._note_config_options([])
    assert executor._config_option_ids == set()
    assert executor._effort_option is None


@pytest.mark.asyncio
async def test_review_nit_c_no_configured_default_keeps_previous_model(executor):
    executor._note_config_options(options())
    executor._rpc = AsyncMock(return_value={"result": {"configOptions": options("model-b")}})
    await executor._apply_model_override("fresh-session", "model-b")
    await executor._apply_model_override("fresh-session", None)
    assert executor._active_model == "model-b"
    assert executor._rpc.await_count == 1
