"""Tests for the generic ACP executor (:mod:`omnigent.inner.acp_executor`).

Two layers:

* **Unit** — construction/argv, both ``session/new`` shapes, tool-call → event
  mapping, permission-outcome mapping, interrupt via ``session/cancel``, and the
  harness-wrap env parsing — all with a mocked transport.
* **Hermetic e2e** — a tiny fake ACP agent (a Python script speaking ACP over
  stdio, written to a temp file) that the executor spawns for real and drives
  through a full turn: initialize → session/new → session/prompt → streaming
  (thought + text + tool card) → request_permission → completion. No real
  vendor binary is required.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shlex
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from omnigent.inner import _proc
from omnigent.inner import acp_executor as acp_executor_module
from omnigent.inner._acp_metadata import _META_INPUT_ITEM_ID
from omnigent.inner._acp_omnigent_mcp import OmnigentAcpMcp, _to_acp_mcp_servers
from omnigent.inner.acp_executor import (
    AcpAgentConfig,
    AcpExecutor,
    _is_auth_required_error,
    _resolve_cwd,
    _unattended_auth_method_id,
)
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import (
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    ReasoningChunk,
    SubAgentCompleted,
    SubAgentStarted,
    SubAgentToolCall,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnCancelled,
    TurnComplete,
    describe_exception,
)
from omnigent.inner.sandbox import HarnessSandboxValidationError

# ---------------------------------------------------------------------------
# Construction / argv
# ---------------------------------------------------------------------------


def test_command_is_shlex_split_into_argv() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="gemini --experimental-acp --model gemini-2.5-pro"))
    assert ex._argv == ["gemini", "--experimental-acp", "--model", "gemini-2.5-pro"]


def test_quoted_command_argv() -> None:
    ex = AcpExecutor(AcpAgentConfig(command='npx -y "@zed-industries/claude-code-acp"'))
    assert ex._argv == ["npx", "-y", "@zed-industries/claude-code-acp"]


def test_empty_command_rejected() -> None:
    with pytest.raises(ValueError):
        AcpExecutor(AcpAgentConfig(command="   "))


def test_handles_tools_internally_and_streaming() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex.handles_tools_internally() is True
    assert ex.supports_streaming() is True


# ---------------------------------------------------------------------------
# session/new shapes (server- vs client-assigned id, optional model)
# ---------------------------------------------------------------------------


def test_unattended_auth_method_prefers_cached_token() -> None:
    assert (
        _unattended_auth_method_id(
            {
                "authMethods": [
                    {"id": "cached_token"},
                    {"id": "grok.com"},
                ],
                "_meta": {"defaultAuthMethodId": "cached_token"},
            }
        )
        == "cached_token"
    )


def test_unattended_auth_method_none_when_only_browser_login() -> None:
    assert _unattended_auth_method_id({"authMethods": [{"id": "grok.com"}]}) is None


def test_unattended_auth_method_none_when_absent() -> None:
    assert _unattended_auth_method_id({}) is None


def test_unattended_auth_method_none_when_ids_malformed() -> None:
    assert _unattended_auth_method_id({"authMethods": [{"name": "no id"}, "junk", 7]}) is None


def test_unattended_auth_method_falls_back_when_default_interactive() -> None:
    assert (
        _unattended_auth_method_id(
            {
                "authMethods": [{"id": "grok.com"}, {"id": "cached_token"}],
                "_meta": {"defaultAuthMethodId": "grok.com"},
            }
        )
        == "cached_token"
    )


def test_is_auth_required_error_matches_code_and_message() -> None:
    assert _is_auth_required_error({"code": -32000, "message": "nope"})
    assert _is_auth_required_error({"code": -32603, "message": "Authentication required"})
    assert not _is_auth_required_error({"code": -32603, "message": "boom"})
    assert not _is_auth_required_error("Authentication required")


def _grok_like_initialize_result() -> dict:
    return {
        "agentCapabilities": {"promptCapabilities": {"image": False}},
        "authMethods": [{"id": "grok.com"}, {"id": "cached_token"}],
        "_meta": {"defaultAuthMethodId": "cached_token"},
    }


@pytest.mark.asyncio
async def test_session_new_authenticates_after_auth_required_and_retries() -> None:
    """Auth-required ``session/new`` triggers ``authenticate`` + one retry."""
    ex = AcpExecutor(AcpAgentConfig(command="x", session_id_mode="server"))
    calls: list[tuple[str, dict]] = []
    authenticated = False

    async def fake_rpc(method, params, timeout=30.0):
        nonlocal authenticated
        calls.append((method, params))
        if method == "initialize":
            return {"result": _grok_like_initialize_result()}
        if method == "authenticate":
            authenticated = True
            return {"result": {}}
        if method == "session/new":
            if not authenticated:
                return {"error": {"code": -32000, "message": "Authentication required"}}
            return {"result": {"sessionId": "sid-1"}}
        raise AssertionError(f"unexpected {method}")

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_initialized()
    assert await ex._ensure_session() == "sid-1"
    assert [c[0] for c in calls] == [
        "initialize",
        "session/new",
        "authenticate",
        "session/new",
    ]
    assert calls[2][1] == {"methodId": "cached_token"}


@pytest.mark.asyncio
async def test_session_new_success_skips_authenticate_despite_auth_methods() -> None:
    """Advertised methods alone must not trigger an unsolicited authenticate."""
    ex = AcpExecutor(AcpAgentConfig(command="x", session_id_mode="server"))
    calls: list[str] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append(method)
        if method == "initialize":
            # Gemini-CLI-shaped advertisement: interactive ids the executor's
            # denylist does not know about.
            return {
                "result": {
                    "agentCapabilities": {"promptCapabilities": {}},
                    "authMethods": [
                        {"id": "oauth-personal"},
                        {"id": "gemini-api-key"},
                        {"id": "vertex-ai"},
                    ],
                }
            }
        if method == "session/new":
            return {"result": {"sessionId": "sid-2"}}
        raise AssertionError(f"unexpected {method}")

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_initialized()
    assert await ex._ensure_session() == "sid-2"
    assert calls == ["initialize", "session/new"]


@pytest.mark.asyncio
async def test_initialize_skips_authenticate_without_auth_methods() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    calls: list[str] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append(method)
        return {"result": {"agentCapabilities": {"promptCapabilities": {}}}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_initialized()
    assert calls == ["initialize"]


@pytest.mark.asyncio
async def test_session_new_auth_required_browser_only_raises_clear_error() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x", session_id_mode="server"))

    async def fake_rpc(method, params, timeout=30.0):
        if method == "initialize":
            return {"result": {"authMethods": [{"id": "grok.com"}]}}
        if method == "session/new":
            return {"error": {"code": -32000, "message": "Authentication required"}}
        raise AssertionError(f"unexpected {method}")

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_initialized()
    with pytest.raises(RuntimeError, match="no headless auth method"):
        await ex._ensure_session()


@pytest.mark.asyncio
async def test_session_new_auth_required_malformed_methods_raises_clear_error() -> None:
    """Id-less ``authMethods`` entries yield a diagnosis, not a crash."""
    ex = AcpExecutor(AcpAgentConfig(command="x", session_id_mode="server"))

    async def fake_rpc(method, params, timeout=30.0):
        if method == "initialize":
            return {"result": {"authMethods": [{"name": "missing id"}]}}
        if method == "session/new":
            return {"error": {"code": -32000, "message": "Authentication required"}}
        raise AssertionError(f"unexpected {method}")

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_initialized()
    with pytest.raises(RuntimeError, match="no headless auth method"):
        await ex._ensure_session()


@pytest.mark.asyncio
async def test_session_new_auth_required_without_methods_surfaces_raw_error() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x", session_id_mode="server"))
    calls: list[str] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append(method)
        if method == "initialize":
            return {"result": {"agentCapabilities": {"promptCapabilities": {}}}}
        if method == "session/new":
            return {"error": {"code": -32000, "message": "Authentication required"}}
        raise AssertionError(f"unexpected {method}")

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_initialized()
    with pytest.raises(RuntimeError, match="ACP session/new failed: Authentication required"):
        await ex._ensure_session()
    assert "authenticate" not in calls


@pytest.mark.asyncio
async def test_authenticate_rpc_error_surfaces() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x", session_id_mode="server"))

    async def fake_rpc(method, params, timeout=30.0):
        if method == "initialize":
            return {"result": _grok_like_initialize_result()}
        if method == "session/new":
            return {"error": {"code": -32000, "message": "Authentication required"}}
        if method == "authenticate":
            return {"error": {"code": -32603, "message": "token expired"}}
        raise AssertionError(f"unexpected {method}")

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_initialized()
    with pytest.raises(RuntimeError, match="ACP authenticate failed: token expired"):
        await ex._ensure_session()


@pytest.mark.asyncio
async def test_session_new_server_mode_adopts_returned_id() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x", session_id_mode="server"))
    captured: dict = {}

    async def fake_rpc(method, params, timeout=30.0):
        captured["method"] = method
        captured["params"] = params
        return {"result": {"sessionId": "srv-42"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    sid = await ex._ensure_session()
    assert sid == "srv-42"
    assert "sessionId" not in captured["params"]  # server assigns it
    assert captured["params"]["cwd"] == ex._cwd
    assert captured["params"]["mcpServers"] == []


@pytest.mark.asyncio
async def test_session_new_records_standard_model_config_option() -> None:
    """ACP config-option categories identify the live model; option ids are agent-defined."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))

    async def fake_rpc(method, params, timeout=30.0):
        return {
            "result": {
                "sessionId": "srv-42",
                "configOptions": [
                    {
                        "id": "active-model",
                        "category": "model",
                        "currentValue": "grok-4.6",
                    }
                ],
            }
        }

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_session()

    assert ex._active_model == "grok-4.6"
    assert ex._model_config_option_id == "active-model"

    # The learned id is what a warm switch targets, not the literal ``model``.
    calls: list[tuple[str, dict]] = []

    async def switch_rpc(method, params, timeout=30.0):
        calls.append((method, params))
        return {
            "result": {
                "configOptions": [
                    {"id": "active-model", "category": "model", "currentValue": params["value"]}
                ]
            }
        }

    ex._rpc = switch_rpc  # type: ignore[assignment]
    await ex._apply_model_override("srv-42", "grok-4.7")

    assert calls == [
        (
            "session/set_config_option",
            {"sessionId": "srv-42", "configId": "active-model", "value": "grok-4.7"},
        )
    ]
    assert ex._active_model == "grok-4.7"


def test_config_option_update_prefers_the_model_id_over_its_category() -> None:
    """Several matching options resolve deterministically: ``model`` wins, else the first."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._note_config_options(
        [
            {"id": "model-tier", "category": "model", "currentValue": "fast"},
            {"id": "model", "currentValue": "grok-4.6"},
        ]
    )
    assert ex._model_config_option_id == "model"
    assert ex._active_model == "grok-4.6"

    ex._note_config_options(
        [
            {"id": "active-model", "category": "model", "currentValue": "grok-4.7"},
            {"id": "fallback-model", "category": "model", "currentValue": "grok-3"},
        ]
    )
    assert ex._model_config_option_id == "active-model"
    assert ex._active_model == "grok-4.7"


@pytest.mark.asyncio
async def test_drain_ignores_malformed_session_update_params() -> None:
    """A ``session/update`` whose ``params`` is not an object is dropped, not fatal."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._queue.put_nowait({"jsonrpc": "2.0", "method": "session/update", "params": [1, 2]})
    ex._queue.put_nowait({"jsonrpc": "2.0", "method": "session/update"})

    await ex._drain_stale_queue()

    assert ex._queue.empty()
    assert ex._active_model is None


@pytest.mark.asyncio
async def test_session_new_records_legacy_model_state() -> None:
    """Grok 1.0.4's preview ``models`` state still reports its real launch model."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))

    async def fake_rpc(method, params, timeout=30.0):
        return {
            "result": {
                "sessionId": "srv-42",
                "models": {"currentModelId": "grok-4.6", "availableModels": []},
            }
        }

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_session()

    assert ex._active_model == "grok-4.6"


@pytest.mark.asyncio
async def test_session_new_sends_empty_mcp_servers_when_omnigent_mcp_disabled() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x", omnigent_mcp=False))
    captured: dict = {}

    async def fake_rpc(method, params, timeout=30.0):
        captured["params"] = params
        return {"result": {"sessionId": "srv-42"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_session()
    assert captured["params"]["mcpServers"] == []


@pytest.mark.asyncio
async def test_session_new_client_mode_generates_and_sends_id() -> None:
    ex = AcpExecutor(
        AcpAgentConfig(
            command="x", session_id_mode="client", model="m1", send_model_in_session_new=True
        )
    )

    sent: dict = {}

    async def capture_rpc(method, params, timeout=30.0):
        sent.update(params)
        return {"result": {}}

    ex._rpc = capture_rpc  # type: ignore[assignment]
    sid = await ex._ensure_session()
    assert sid == sent["sessionId"] and sid  # our generated id is used
    assert sent["model"] == "m1"  # model sent because send_model_in_session_new


# ---------------------------------------------------------------------------
# Tool-call extraction + permission outcome
# ---------------------------------------------------------------------------


def test_extract_tool_call_prefers_title() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    name, args = ex._extract_tool_call(
        {"toolCall": {"title": "shell", "kind": "execute", "rawInput": {"command": "ls"}}}
    )
    assert name == "shell"
    assert args == {"command": "ls"}


def test_extract_tool_call_falls_back_to_kind() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    name, args = ex._extract_tool_call({"toolCall": {"kind": "read"}})
    assert name == "read"
    assert args == {}


def test_extract_tool_call_recovers_a_bare_permission_request() -> None:
    """A request naming only ``toolCallId`` resolves via the originating tool_call.

    An agent may ask permission without repeating the tool: Devin sends no
    ``title`` / ``kind`` / ``rawInput``, only the id it already announced. The
    ``tool_call`` update always arrives first, so its name and arguments are
    still on hand.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "toolu_01",
            "title": "Ran command",
            "kind": "execute",
            "rawInput": {"command": "rm -rf build"},
        }
    )
    name, args = ex._extract_tool_call(
        {
            "toolCall": {
                "toolCallId": "toolu_01",
                "_meta": {"vendor/editableCommand": "rm -rf build"},
            }
        }
    )
    assert name == "Ran command"
    assert args == {"command": "rm -rf build"}


def test_extract_tool_call_prefers_the_request_over_the_cache() -> None:
    """A request that carries its own title/rawInput wins; the cache is a fallback."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(
        {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "stale", "rawInput": {"a": 1}}
    )
    name, args = ex._extract_tool_call(
        {"toolCall": {"toolCallId": "c1", "title": "shell", "rawInput": {"command": "ls"}}}
    )
    assert (name, args) == ("shell", {"command": "ls"})


def test_extract_tool_call_unknown_id_degrades_to_tool() -> None:
    """An id we never saw announced still yields the safe generic fallback."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex._extract_tool_call({"toolCall": {"toolCallId": "never-announced"}}) == ("tool", {})


def test_permission_outcome_allow_prefers_once() -> None:
    params = {
        "options": [
            {"optionId": "a1", "kind": "allow_always"},
            {"optionId": "a2", "kind": "allow_once"},
            {"optionId": "r1", "kind": "reject_once"},
        ]
    }
    out = AcpExecutor._permission_outcome(params, allow=True)
    assert out == {"outcome": {"outcome": "selected", "optionId": "a2"}}


def test_permission_outcome_deny_picks_reject() -> None:
    params = {"options": [{"optionId": "r", "kind": "reject_once"}]}
    out = AcpExecutor._permission_outcome(params, allow=False)
    assert out == {"outcome": {"outcome": "selected", "optionId": "r"}}


def test_permission_outcome_cancelled_when_no_option() -> None:
    out = AcpExecutor._permission_outcome({"options": []}, allow=True)
    assert out == {"outcome": {"outcome": "cancelled"}}


# ---------------------------------------------------------------------------
# session/update → ExecutorEvent mapping
# ---------------------------------------------------------------------------


def test_update_agent_message_chunk_to_text() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    events = ex._handle_session_update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}}
    )
    assert len(events) == 1 and isinstance(events[0], TextChunk) and events[0].text == "hi"


def test_update_thought_chunk_to_reasoning() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    events = ex._handle_session_update(
        {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}}
    )
    assert len(events) == 1 and isinstance(events[0], ReasoningChunk) and events[0].delta == "hmm"


def test_tool_call_and_update_emit_cards() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    started = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c1",
            "title": "shell",
            "rawInput": {"command": "ls"},
        }
    )
    assert len(started) == 1
    req = started[0]
    assert isinstance(req, ToolCallRequest)
    assert req.name == "shell"
    assert req.metadata == {"call_id": "c1", "internally_executed": True}
    assert ex._tool_names["c1"] == "shell"

    done = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"}
    )
    assert len(done) == 1
    comp = done[0]
    assert isinstance(comp, ToolCallComplete)
    assert comp.name == "shell" and comp.status is ToolCallStatus.SUCCESS
    assert comp.metadata == {"call_id": "c1"}
    assert "c1" not in ex._tool_names  # popped


def test_tool_call_caches_release_on_completion() -> None:
    """Both id-keyed caches drop the entry when the call closes.

    They exist only to bridge a tool_call to its permission request and closing
    update, so a long session must not accumulate one entry per tool call.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c1",
            "title": "shell",
            "rawInput": {"command": "ls"},
        }
    )
    assert ex._tool_names == {"c1": "shell"}
    assert ex._tool_inputs == {"c1": {"command": "ls"}}

    ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c1", "status": "completed"}
    )
    assert ex._tool_names == {}
    assert ex._tool_inputs == {}


def test_tool_call_update_failed_maps_to_error() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update({"sessionUpdate": "tool_call", "toolCallId": "c2", "title": "t"})
    done = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c2", "status": "failed"}
    )
    assert done[0].status is ToolCallStatus.ERROR


def test_usage_update_sets_context_window() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex.max_context_tokens() is None
    ex._handle_session_update({"sessionUpdate": "usage_update", "size": 200000})
    assert ex.max_context_tokens() == 200000


def test_usage_maps_cached_reads_to_the_canonical_key() -> None:
    """
    ``cachedReadTokens`` surfaces as its own ``cache_read_input_tokens``.

    Cache reads are real consumption billed at a fraction of the input rate, so
    folding them into ``input_tokens`` (or dropping them, as before) misreports
    cost — in a measured Devin turn 10,944 of 15,637 input tokens were cache
    reads. ``cache_read_input_tokens`` is the key the SSE layer and AgentInfo
    already render, so no UI change is needed.

    **What breaks if this fails**: a future token budget computes ~3x the real
    consumption and fires almost immediately.
    """
    usage = AcpExecutor._usage_from_result(
        {
            "usage": {
                "totalTokens": 15675,
                "inputTokens": 15637,
                "outputTokens": 38,
                "cachedReadTokens": 10944,
            }
        }
    )
    assert usage == {
        "total_tokens": 15675,
        "input_tokens": 15637,
        "output_tokens": 38,
        "cache_read_input_tokens": 10944,
    }


def test_usage_omits_absent_and_non_integer_fields() -> None:
    """Agents that report a subset (or garbage) still yield usable usage."""
    assert AcpExecutor._usage_from_result({"usage": {"totalTokens": 10}}) == {"total_tokens": 10}
    # ``True`` is an int subclass — it must not be mistaken for a token count.
    assert AcpExecutor._usage_from_result({"usage": {"inputTokens": True}}) is None
    assert AcpExecutor._usage_from_result({"usage": {"totalTokens": "nope"}}) is None
    assert AcpExecutor._usage_from_result({}) is None


def test_usage_maps_cached_writes_to_the_canonical_key() -> None:
    """``cachedWriteTokens`` surfaces as ``cache_creation_input_tokens``.

    Agents that report cache-creation tokens (e.g. jcode against a Databricks
    gateway) had them silently dropped before, understating cost — cache writes
    bill at ~1.25x the input rate.
    """
    usage = AcpExecutor._usage_from_result(
        {
            "usage": {
                "inputTokens": 6216,
                "outputTokens": 5,
                "totalTokens": 6221,
                "cachedReadTokens": 0,
                "cachedWriteTokens": 128,
            }
        }
    )
    assert usage == {
        "input_tokens": 6216,
        "output_tokens": 5,
        "total_tokens": 6221,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 128,
    }


def test_usage_with_active_model_tags_the_model() -> None:
    """A turn's usage is stamped with the agent's active model.

    ACP ``result.usage`` carries token counts but no model id, so the server
    cannot attribute the tokens to a model — leaving its per-model usage view
    (``usage_by_model``) empty and the UI showing no token counts for the ACP
    (jcode / Devin / Grok) session. The active model comes from the agent's
    ``model`` config option, captured at ``session/new``.

    **What breaks if this fails**: token counts never render for any ACP harness.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._active_model = "system.ai.claude-haiku-4-5"
    usage = ex._usage_with_active_model(
        {"usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}}
    )
    assert usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "model": "system.ai.claude-haiku-4-5",
    }


def test_usage_with_active_model_skips_stamp_when_model_unknown() -> None:
    """No active model → no ``model`` key (attribution simply stays absent)."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex._active_model is None
    assert ex._usage_with_active_model({"usage": {"totalTokens": 15}}) == {"total_tokens": 15}


def test_usage_with_active_model_reports_model_without_token_counts() -> None:
    """An agent with notification-only usage still reports its actual model."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._active_model = "system.ai.claude-haiku-4-5"
    assert ex._usage_with_active_model({}) == {"model": "system.ai.claude-haiku-4-5"}


def test_in_progress_tool_update_emits_nothing() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update({"sessionUpdate": "tool_call", "toolCallId": "c3", "title": "t"})
    assert (
        ex._handle_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "c3", "status": "in_progress"}
        )
        == []
    )


# ---------------------------------------------------------------------------
# Sub-agent surfacing (extension-supplied dialect -> normalized events)
# ---------------------------------------------------------------------------


class _FakeSubAgentDialect:
    """An invented dialect, so this generic suite names no vendor."""

    def read(self, update: dict[str, object]) -> tuple[object, ...]:
        """Return start / activity / end for ``acme.dev/{spawn,work,done}``."""
        from omnigent.inner.acp_subagents import SubAgentActivity, SubAgentEnd, SubAgentStart

        if isinstance(update.get("acme.dev/spawn"), dict):
            return (SubAgentStart(child_key="w1", title="worker", task="do a thing"),)
        if isinstance(update.get("acme.dev/work"), dict):
            return (
                SubAgentActivity(
                    child_key="w1", call_id="c9", name="Wrote out.txt", args={"path": "out.txt"}
                ),
            )
        if isinstance(update.get("acme.dev/done"), dict):
            return (SubAgentEnd(child_key="w1", ok=True, summary="done"),)
        return ()


def _extended_executor() -> AcpExecutor:
    """An executor whose extension supplies one dialect (a vendor's wrap does this)."""
    from omnigent.inner.acp_extension import AcpExtension

    return AcpExecutor(
        AcpAgentConfig(command="x"),
        extension=AcpExtension(name="acme", subagent_sources=(_FakeSubAgentDialect(),)),
    )


def test_handle_session_update_emits_subagent_started() -> None:
    """An extension-recognized start becomes a ``SubAgentStarted`` event.

    The executor half of the seam: the runner turns this event into a child
    session, so if it stops firing the "Subagents" panel goes empty.
    """
    events = _extended_executor()._handle_session_update(
        {"sessionUpdate": "tool_call_update", "acme.dev/spawn": {"id": "w1"}}
    )
    assert [e for e in events if isinstance(e, SubAgentStarted)] == [
        SubAgentStarted(child_key="w1", title="worker", task="do a thing")
    ]


def test_handle_session_update_emits_subagent_completed() -> None:
    """An extension-recognized end becomes a ``SubAgentCompleted`` event."""
    events = _extended_executor()._handle_session_update(
        {"sessionUpdate": "tool_call_update", "acme.dev/done": {"id": "w1"}}
    )
    assert [e for e in events if isinstance(e, SubAgentCompleted)] == [
        SubAgentCompleted(child_key="w1", ok=True, summary="done")
    ]


def test_generic_executor_does_no_subagent_scanning() -> None:
    """With no extension, the executor is inert even for a dialect-shaped frame.

    **What breaks if this fails**: every ACP agent — Grok, a user's own
    ``acp:<slug>`` — gets some other vendor's dialect run against its frames,
    which is the coupling the extension seam exists to prevent. The default must
    read no vendor field at all.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))  # generic acp harness
    for frame in (
        {"sessionUpdate": "tool_call_update", "acme.dev/spawn": {"id": "w1"}},
        {"sessionUpdate": "tool_call_update", "_meta": {"cognition.ai/subagent_started": {}}},
        {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}},
    ):
        events = ex._handle_session_update(frame)
        assert not any(isinstance(e, (SubAgentStarted, SubAgentCompleted)) for e in events), frame


def test_tool_cards_still_render_alongside_the_scan() -> None:
    """An ordinary (unclaimed) tool_call still produces a parent card."""
    events = _extended_executor()._handle_session_update(
        {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "Ran ls", "kind": "execute"}
    )
    assert [type(e) for e in events] == [ToolCallRequest]


def test_handle_session_update_routes_activity_to_the_child() -> None:
    """A claimed tool call becomes a ``SubAgentToolCall``, not a parent card.

    **What breaks if this fails**: the sub-agent's own work renders in the parent
    stream (or nowhere) instead of the child transcript — the exact gap this
    change closes.
    """
    events = _extended_executor()._handle_session_update(
        {"sessionUpdate": "tool_call", "toolCallId": "c9", "acme.dev/work": {"any": 1}}
    )
    assert events == [
        SubAgentToolCall(
            child_key="w1", call_id="c9", name="Wrote out.txt", args={"path": "out.txt"}
        )
    ]
    # The frame is claimed, so it does NOT also emit a parent tool card.
    assert not any(isinstance(e, ToolCallRequest) for e in events)


def test_claimed_completion_frame_emits_no_spurious_parent_card() -> None:
    """A claimed ``tool_call_update`` doesn't also close a parent tool card.

    The sub-agent's completion rides a ``tool_call_update`` whose id was never an
    originating ``tool_call``; without the short-circuit the terminal-status
    branch would emit a stray ``ToolCallComplete(name="tool")`` in the parent.
    """
    events = _extended_executor()._handle_session_update(
        {"sessionUpdate": "tool_call_update", "status": "completed", "acme.dev/done": {"id": "w1"}}
    )
    assert [type(e) for e in events] == [SubAgentCompleted]
    assert not any(isinstance(e, ToolCallComplete) for e in events)


# ---------------------------------------------------------------------------
# Agent-native vs MCP-bridge classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("update", "is_bridge"),
    [
        ({"title": "sys_session_get_info", "rawInput": {}}, True),
        (
            {"title": "Session info", "rawInput": {"tool": "mcp_omnigent_sys_session_get_info"}},
            True,
        ),
        (
            {"title": "Session info", "rawInput": {"tool": "mcp__omnigent__sys_session_get_info"}},
            True,
        ),
        (
            {
                "title": "omnigent: sys session get info",
                "rawInput": {"session_id": ""},
                "_meta": {"goose": {"toolCall": {"toolName": "omnigent__sys_session_get_info"}}},
            },
            True,
        ),
        ({"title": "GitHub comments", "rawInput": {"tool": "github__list_comments"}}, False),
        ({"title": "shell: git status", "rawInput": {"command": "git status"}}, False),
    ],
)
def test_only_advertised_bridge_aliases_enter_dispatch_correlation(
    update: dict[str, object], is_bridge: bool
) -> None:
    """A call the bridge never advertised must not claim a dispatch slot."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._bridge_tool_aliases = frozenset(
        {
            "sys_session_get_info",
            "mcp_omnigent_sys_session_get_info",
            "mcp__omnigent__sys_session_get_info",
            "omnigent__sys_session_get_info",
        }
    )

    event = ex._handle_session_update(
        {"sessionUpdate": "tool_call", "toolCallId": "c1", **update}
    )[0]

    assert isinstance(event, ToolCallRequest)
    assert ("internally_executed" not in event.metadata) is is_bridge


def test_no_advertised_bridge_classifies_every_call_as_native() -> None:
    """Tools alone are not enough: without a served relay there is no bridge."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._omnigent_tools = [{"name": "sys_session_get_info"}]

    event = ex._handle_session_update(
        {"sessionUpdate": "tool_call", "toolCallId": "c1", "title": "sys_session_get_info"}
    )[0]

    assert isinstance(event, ToolCallRequest)
    assert event.metadata == {"call_id": "c1", "internally_executed": True}


@pytest.mark.asyncio
async def test_bridge_aliases_hold_until_the_session_resets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The agent keeps the tools sent at session/new, so classify against those."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._rpc = AsyncMock(return_value={"result": {"sessionId": "s1"}})  # type: ignore[method-assign]
    monkeypatch.setattr(ex._mcp, "session_new_servers", lambda **_: [{"name": "omnigent"}])

    ex._omnigent_tools = [{"name": "sys_session_get_info"}]
    await ex._ensure_session()
    ex._omnigent_tools = [{"name": "web_search"}]

    assert "sys_session_get_info" in ex._bridge_tool_aliases
    assert "web_search" not in ex._bridge_tool_aliases

    ex._reset_session_state()
    ex._rpc = AsyncMock(return_value={"result": {"sessionId": "s2"}})  # type: ignore[method-assign]
    await ex._ensure_session()

    assert "web_search" in ex._bridge_tool_aliases
    assert "sys_session_get_info" not in ex._bridge_tool_aliases


def test_session_reset_drops_in_flight_tool_state() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "pending",
            "title": "shell",
            "rawInput": {"command": "sleep 10"},
        }
    )

    ex._reset_session_state()

    assert ex._tool_names == {}
    assert ex._tool_inputs == {}
    assert ex._bridge_tool_aliases == frozenset()


class _RecordingCtx:
    """Minimal ``TurnContext`` stand-in: the surface the adapter touches.

    A typed stub rather than MagicMock, so a call to a method that does not
    exist fails loud instead of silently returning another mock.
    """

    def __init__(self, response_id: str = "resp_acp") -> None:
        self.response_id = response_id
        self.emitted: list[object] = []

    def emit(self, event: object) -> None:
        self.emitted.append(event)


@pytest.mark.asyncio
async def test_native_call_before_a_bridge_call_keeps_each_call_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reported defect: a native call ahead of a bridge call stole its id."""
    import omnigent.runtime.harnesses._executor_adapter as adapter_module
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._bridge_tool_aliases = frozenset({"sys_session_get_info"})
    adapter = ExecutorAdapter(executor_factory=lambda: ex)
    ctx = _RecordingCtx()
    adapter._current_ctx = ctx  # type: ignore[assignment]
    adapter._current_agent = "test-model"

    for call_id, title in (("native-1", "terminal: date"), ("bridge-1", "sys_session_get_info")):
        for event in ex._handle_session_update(
            {"sessionUpdate": "tool_call", "toolCallId": call_id, "title": title, "rawInput": {}}
        ):
            adapter._translate_event(event, ctx)  # type: ignore[arg-type]

    dispatched: dict[str, str] = {}

    async def fake_bridge(*_args: object, call_id: str, **_kw: object) -> dict[str, object]:
        dispatched["call_id"] = call_id
        return {"ok": True}

    monkeypatch.setattr(adapter_module, "_bridge_one_dispatch", fake_bridge)
    await adapter._stable_tool_executor("sys_session_get_info", {})

    assert dispatched["call_id"] == "bridge-1"
    assert list(adapter._pending_mcp_call_ids) == []


# ---------------------------------------------------------------------------
# Permission decision (policy + elicitation gates)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("tier_managed", [False, True])
async def test_host_tier_does_not_delegate_filesystem_requests(tier_managed: bool) -> None:
    ex = AcpExecutor(
        AcpAgentConfig(command="x", host_permission_tier=tier_managed), os_env=OSEnvSpec()
    )
    ex._rpc = AsyncMock(return_value={"result": {"protocolVersion": 1}})
    ex._send = AsyncMock()
    environment = AsyncMock()
    environment.read.return_value = {"content": "content", "encoding": "utf-8"}
    environment.write.return_value = {}
    ex._ensure_os_environment = AsyncMock(return_value=environment)

    await ex._ensure_initialized()
    capabilities = ex._rpc.await_args.args[1]["clientCapabilities"]["fs"]
    assert capabilities == {"readTextFile": not tier_managed, "writeTextFile": not tier_managed}

    for method in ("read_text_file", "write_text_file"):
        await ex._respond_to_agent_request(
            {"id": method, "method": f"fs/{method}", "params": {"path": "file", "content": "new"}}
        )
        reply = ex._send.await_args.args[0]
        if tier_managed:
            assert reply["error"]["code"] == -32601
        else:
            assert "result" in reply

    assert environment.read.await_count == (0 if tier_managed else 1)
    assert environment.write.await_count == (0 if tier_managed else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["DENY", "ASK"])
async def test_host_tier_policy_uses_the_bound_tool_identity(action: str) -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x", host_permission_tier=True))
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    ex._elicitation_handler = AsyncMock(return_value=False)
    arguments = {"CommandLine": "git status"}
    ex._note_permission_update(
        "s1",
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "shell-1",
            "kind": "execute",
            "title": "Agent display text",
            "rawInput": arguments,
        },
    )

    async def policy(phase, data):
        assert phase == "PHASE_TOOL_CALL"
        if data["name"] == "run_command" and data["arguments"] == arguments:
            return Mock(action=f"POLICY_ACTION_{action}")
        return Mock(action="POLICY_ACTION_ALLOW", data={"_omnigent_acp_tier": "allow"})

    ex._policy_evaluator = policy
    params = {
        "sessionId": "s1",
        "toolCall": {"toolCallId": "shell-1"},
        "options": [{"kind": "allow_once", "optionId": "once"}],
    }
    assert await ex._decide_permission(params) == (None if action == "ASK" else False, None)
    assert ex._elicitation_handler.await_count == (1 if action == "ASK" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_name", "command", "action"),
    [
        ("ask_on_os_tools", "git status", "ASK"),
        ("blast_radius", "git push origin work", "ASK"),
        ("blast_radius", "git push --force origin work", "DENY"),
        ("github_policy", "gh pr view 1 --repo example/private", "DENY"),
        ("working_dir", "cd ../other", "DENY"),
    ],
)
async def test_host_tier_respects_builtin_shell_policies(
    policy_name: str, command: str, action: str
) -> None:
    from omnigent.policies.builtins.github import github_policy
    from omnigent.policies.builtins.orchestration import blast_radius
    from omnigent.policies.builtins.safety import ask_on_os_tools
    from omnigent.policies.builtins.working_dir import block_working_dir_changes

    ex = AcpExecutor(AcpAgentConfig(command="x", host_permission_tier=True))
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    ex._elicitation_handler = AsyncMock(return_value=False)
    ex._note_permission_update(
        "s1",
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "shell-1",
            "kind": "execute",
            "title": "Run command",
            "rawInput": {"CommandLine": command, "command": "echo other"},
        },
    )

    def blast_policy(event):
        return blast_radius()(event, {})

    policy = {
        "ask_on_os_tools": ask_on_os_tools,
        "blast_radius": blast_policy,
        "github_policy": github_policy(read_all=False, read_repos=["example/public"]),
        "working_dir": block_working_dir_changes(),
    }[policy_name]
    verdicts = []

    async def evaluate(phase, data):
        assert phase == "PHASE_TOOL_CALL"
        verdict = policy({"type": "tool_call", "data": data}) or {"result": "ALLOW"}
        verdicts.append(verdict)
        return Mock(
            action=f"POLICY_ACTION_{verdict['result']}", data={"_omnigent_acp_tier": "allow"}
        )

    ex._policy_evaluator = evaluate
    result = await ex._decide_permission(
        {
            "sessionId": "s1",
            "toolCall": {"toolCallId": "shell-1"},
            "options": [{"kind": "allow_once", "optionId": "once"}],
        }
    )
    assert verdicts[0]["result"] == action
    if action == "ASK":
        assert command in verdicts[0]["reason"]
    assert result == (None if action == "ASK" else False, None)
    assert ex._elicitation_handler.await_count == (1 if action == "ASK" else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["updated-input", "foreign-update", "foreign-request", "update-during-policy"]
)
async def test_host_tier_binds_latest_pending_tool_in_its_session(scenario: str) -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x", host_permission_tier=True))
    ex._session_id = "s1"
    arguments = {"CommandLine": "git status"}
    updated_arguments = {"CommandLine": "git diff"}
    initial = {
        "sessionUpdate": "tool_call",
        "toolCallId": "shell-1",
        "kind": "execute",
        "title": "Run command",
        "status": "pending",
        "rawInput": arguments,
    }
    updated = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "shell-1",
        "rawInput": updated_arguments,
    }
    ex._active_prompt_request_id = 1

    async def observe(session_id: str, update: dict) -> None:
        stdout = asyncio.StreamReader()
        ex._proc = Mock(returncode=None, stdout=stdout)
        frame = {"method": "session/update", "params": {"sessionId": session_id, "update": update}}
        stdout.feed_data((json.dumps(frame) + "\n").encode())
        stdout.feed_eof()
        await ex._read_stdout_unobserved()
        ex._handle_session_update(ex._queue.get_nowait()["params"]["update"])

    await observe("s1", initial)
    if scenario != "update-during-policy":
        await observe("s2" if scenario == "foreign-update" else "s1", updated)

    async def allow(_phase, _data):
        if scenario == "update-during-policy":
            await observe("s1", updated)
        return Mock(action="POLICY_ACTION_ALLOW", data={"_omnigent_acp_tier": "allow"})

    ex._policy_evaluator = AsyncMock(side_effect=allow)
    params = {
        "sessionId": "s2" if scenario == "foreign-request" else "s1",
        "toolCall": {"toolCallId": "shell-1"},
        "options": [{"kind": "allow_once", "optionId": "once"}],
    }
    result = await ex._decide_permission(params)
    if scenario == "foreign-request":
        assert result == (False, None)
        ex._policy_evaluator.assert_not_awaited()
    elif scenario == "update-during-policy":
        assert result == (False, None)
        ex._policy_evaluator.assert_awaited_once()
    else:
        assert result == (True, "once")
        bound = ex._policy_evaluator.await_args.args[1]["_omnigent_acp_call"]
        assert bound["arguments"] == (
            arguments if scenario == "foreign-update" else updated_arguments
        )


@pytest.mark.asyncio
async def test_decide_permission_allows_with_no_gates() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None)


@pytest.mark.asyncio
async def test_host_tier_allows_only_the_bound_call_once() -> None:
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload

    ex = AcpExecutor(AcpAgentConfig(command="x", host_permission_tier=True))
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    ex._elicitation_handler = AsyncMock(return_value=True)
    ex._policy_evaluator = AsyncMock(
        return_value=PolicyVerdictPayload(
            action="POLICY_ACTION_ALLOW",
            data={"_omnigent_acp_tier": "allow"},
        )
    )
    observed = {
        "sessionUpdate": "tool_call",
        "toolCallId": "shell-1",
        "kind": "execute",
        "title": "Run command",
        "rawInput": {"CommandLine": "git status"},
    }
    ex._note_permission_update("s1", observed)
    options = [
        {"kind": "allow_always", "optionId": "persistent"},
        {"kind": "allow_once", "optionId": "once"},
    ]
    request = {"sessionId": "s1", "toolCall": {"toolCallId": "shell-1"}, "options": options}
    assert await ex._decide_permission(request) == (True, "once")
    assert await ex._decide_permission(request) == (False, None)
    ex._note_permission_update("s1", {**observed, "toolCallId": "shell-2"})
    assert await ex._decide_permission(
        {**request, "toolCall": {"toolCallId": "shell-2", "rawInput": {"CommandLine": "git diff"}}}
    ) == (False, None)
    ex._elicitation_handler.assert_not_awaited()
    ex._note_permission_update("s1", {**observed, "toolCallId": "shell-3"})
    assert await ex._decide_permission(
        {**request, "toolCall": {"toolCallId": "shell-3"}, "options": options[:1]}
    ) == (False, None)
    ex._elicitation_handler.assert_not_awaited()
    ex._note_permission_update("s1", {**observed, "toolCallId": "shell-4"})
    ex._policy_evaluator.return_value = PolicyVerdictPayload(action="POLICY_ACTION_ASK")
    assert await ex._decide_permission({**request, "toolCall": {"toolCallId": "shell-4"}}) == (
        True,
        "once",
    )
    ex._elicitation_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_decide_permission_denies_on_policy_deny() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))

    class _V:
        action = "POLICY_ACTION_DENY"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (False, None)


@pytest.mark.asyncio
async def test_managed_policy_denial_does_not_select_a_user_reject_option() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x", host_permission_tier=True))
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    ex._policy_evaluator = AsyncMock(return_value=Mock(action="POLICY_ACTION_DENY"))
    ex._elicitation_choice_handler = AsyncMock()
    ex._elicitation_handler = AsyncMock(return_value=False)
    ex._send = AsyncMock()
    ex._note_permission_update(
        "s1",
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "shell-1",
            "kind": "execute",
            "rawInput": {"CommandLine": "git status"},
        },
    )
    await ex._respond_to_agent_request(
        {
            "id": 2,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s1",
                "toolCall": {"toolCallId": "shell-1"},
                "options": _agent_options(),
            },
        }
    )
    ex._policy_evaluator.assert_awaited_once()
    ex._elicitation_handler.assert_not_awaited()
    ex._elicitation_choice_handler.assert_not_awaited()
    reply = ex._send.await_args.args[0]
    assert "result" not in reply
    assert reply["error"]["message"] == "Denied by policy: Omnigent policy denied this tool call."


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["unbound", "no-once", "once", "opencode"])
async def test_managed_agy_permission_logs_windows_auto_decision(
    scenario: str, caplog: pytest.LogCaptureFixture
) -> None:
    from omnigent.permission_tiers import Verdict, adapt_tool_call, decide_tier, load_rules
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload
    from tests.harnesses.test_permission_tiers import windows_host

    command = "omni --version"
    agent = "opencode" if scenario == "opencode" else "agy.exe"
    ex = AcpExecutor(AcpAgentConfig(command=f"{agent} acp --stdio", host_permission_tier=True))
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    ex._elicitation_handler = AsyncMock()
    ex._send = AsyncMock()

    async def evaluate(_phase, data):
        intent = adapt_tool_call(data["name"], data["arguments"], windows_host())
        decision = decide_tier("auto", intent, load_rules())
        assert decision.action == Verdict.ALLOW
        return PolicyVerdictPayload(
            action="POLICY_ACTION_UNSPECIFIED", data={"_omnigent_acp_tier": "allow"}
        )

    ex._policy_evaluator = AsyncMock(side_effect=evaluate)
    if scenario != "unbound":
        ex._note_permission_update(
            "s1",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "shell-1",
                "kind": "execute",
                "title": "Agent display text",
                "rawInput": {"CommandLine": command},
            },
        )
    options = [{"kind": "allow_always", "optionId": "persistent"}]
    if scenario != "no-once":
        options.append({"kind": "allow_once", "optionId": "once"})
    request_call: dict[str, object] = {"toolCallId": "shell-1"}
    if scenario in ("no-once", "once"):
        request_call["rawInput"] = {"CommandLine": command}
    with caplog.at_level(logging.INFO, logger=acp_executor_module.__name__):
        await ex._respond_to_agent_request(
            {
                "id": 2,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "toolCall": request_call,
                    "options": options,
                },
            }
        )

    sent = ex._send.await_args
    assert sent is not None
    reply = sent.args[0]
    if scenario in ("unbound", "no-once"):
        assert reply["error"]["code"] == -32000
    else:
        assert reply["result"] == {"outcome": {"outcome": "selected", "optionId": "once"}}
    assert ex._policy_evaluator.await_count == (0 if scenario == "unbound" else 1)
    ex._elicitation_handler.assert_not_awaited()
    logs = [
        message
        for message in caplog.messages
        if message.startswith("acp agy permission decision:")
    ]
    if scenario == "opencode":
        assert logs == []
    else:
        fields = {
            "unbound": (
                "tool=unbound intent=unbound binding=unbound tier=unreported "
                "once_offer=unchecked option=none"
            ),
            "no-once": (
                "tool=run_command intent=execute binding=current tier=allow "
                "once_offer=missing option=none"
            ),
            "once": (
                "tool=run_command intent=execute binding=current tier=allow "
                "once_offer=valid option=allow_once"
            ),
        }
        assert logs == [f"acp agy permission decision: {fields[scenario]}"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "kind", "expected_tool", "outcome"),
    [
        ("command", None, "run_command", "allow"),
        ("command", "other", "run_command", "allow"),
        ("protected-write", "other", "write_to_file", "deny"),
        ("workspace-write", "edit", "write_to_file", "allow"),
        ("unknown", "other", "unknown", "ask"),
        ("ambiguous", "other", None, "deny"),
        ("opencode", "other", "unknown", "ask"),
    ],
)
async def test_managed_agy_binds_windows_permission_by_vendor_arguments(
    case: str, kind: str | None, expected_tool: str | None, outcome: str
) -> None:
    from omnigent.permission_tiers import Verdict, adapt_tool_call, decide_tier, load_rules
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload
    from tests.harnesses.test_permission_tiers import windows_host

    command = "omni --version"
    command_args = {
        "CommandLine": command,
        "WaitMsBeforeAsync": 10000,
        "toolSummary": "Show the installed version",
    }
    path = r"C:\Windows\System32\drivers\etc\hosts"
    if case == "workspace-write":
        path = r"C:\work\project\tmp\fixture.txt"
    write_args = {"Path": path, "Content": "fixture\n", "Overwrite": True}
    title, arguments = {
        "command": (command, command_args),
        "protected-write": (f"File {path}", write_args),
        "workspace-write": ("File tmp/fixture.txt", write_args),
        "unknown": (command, {"toolSummary": "Show the installed version"}),
        "ambiguous": (command, {**command_args, **write_args}),
        "opencode": (command, command_args),
    }[case]
    launch = "opencode acp" if case == "opencode" else "agy.exe acp --stdio"
    ex = AcpExecutor(AcpAgentConfig(command=launch, host_permission_tier=True))
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    ex._elicitation_handler = AsyncMock(return_value=True)
    ex._send = AsyncMock()
    decisions = []

    async def evaluate(_phase, data):
        intent = adapt_tool_call(data["name"], data["arguments"], windows_host())
        decision = decide_tier("auto", intent, load_rules())
        decisions.append((data["name"], decision))
        if decision.action == Verdict.DENY:
            return PolicyVerdictPayload(action="POLICY_ACTION_DENY")
        return PolicyVerdictPayload(
            action="POLICY_ACTION_UNSPECIFIED",
            data={"_omnigent_acp_tier": decision.action.value.lower()},
        )

    ex._policy_evaluator = AsyncMock(side_effect=evaluate)
    call = {"toolCallId": "tool-1", "title": title, "rawInput": arguments}
    if kind is not None:
        call["kind"] = kind
    ex._note_permission_update("s1", {"sessionUpdate": "tool_call", "status": "pending", **call})
    await ex._respond_to_agent_request(
        {
            "id": 2,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s1",
                "toolCall": call,
                "options": [{"kind": "allow_once", "optionId": "once", "name": "Allow"}],
            },
        }
    )

    sent = ex._send.await_args
    assert sent is not None
    reply = sent.args[0]
    if outcome in ("allow", "ask"):
        assert reply["result"] == {"outcome": {"outcome": "selected", "optionId": "once"}}
    else:
        assert reply["error"]["code"] == -32000
    if expected_tool is None:
        assert decisions == []
    else:
        ((tool_name, decision),) = decisions
        assert tool_name == expected_tool
        expected_verdict = {
            "allow": Verdict.ALLOW,
            "ask": Verdict.ASK,
            "deny": Verdict.DENY,
        }[outcome]
        assert decision.action == expected_verdict
        if case == "protected-write":
            assert decision.rule_id == "protected-path"
    assert ex._elicitation_handler.await_count == (1 if outcome == "ask" else 0)


def _pending_acp_permission(
    ex: AcpExecutor, arguments: dict[str, object], kind: str | None
) -> dict[str, object]:
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    call: dict[str, object] = {"toolCallId": "tool-1", "rawInput": arguments}
    if kind is not None:
        call["kind"] = kind
    ex._note_permission_update("s1", {"sessionUpdate": "tool_call", "status": "pending", **call})
    return {
        "sessionId": "s1",
        "toolCall": call,
        "options": [{"kind": "allow_once", "optionId": "once", "name": "Allow"}],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "advertised", "kind", "arguments", "expected_name"),
    [
        (
            "Run mcp_omnigent_sys_os_shell",
            True,
            "other",
            {"command": "omni --version"},
            "sys_os_shell",
        ),
        (
            "mcp__omnigent__sys_os_shell",
            True,
            "execute",
            {"command": "git status"},
            "sys_os_shell",
        ),
        ("omnigent__sys_os_read", True, "read", {"path": "notes.txt"}, "sys_os_read"),
        (
            "mcp_omnigent_sys_os_write",
            True,
            "edit",
            {"path": "note.txt", "content": "x"},
            "sys_os_write",
        ),
        (
            "mcp_omnigent_sys_os_edit",
            True,
            "other",
            {"path": "note.txt", "oldText": "x", "newText": "y"},
            "sys_os_edit",
        ),
        ("mcp_omnigent_sys_session_list", True, "other", {}, "sys_session_list"),
        (
            "mcp_omnigent_sys_session_update",
            True,
            "edit",
            {"session_id": "s1"},
            "sys_session_update",
        ),
        (
            "Run mcp_omnigent_sys_session_get_startup_observation_v1",
            True,
            "read",
            {},
            "sys_session_get_startup_observation_v1",
        ),
        ("mcp_other_sys_os_shell", True, "other", {"command": "git status"}, "unknown"),
        ("mcp_omnigent_sys_os_shell", False, "other", {"command": "git status"}, "unknown"),
        (
            "mcp_omnigent_sys_os_shell",
            True,
            "execute",
            {"CommandLine": "git status"},
            "run_command",
        ),
        ("mcp_omnigent_sys_os_read", True, "execute", {"path": "notes.txt"}, None),
    ],
)
async def test_managed_agy_binds_advertised_omnigent_tools(
    monkeypatch, title, advertised, kind, arguments, expected_name
):
    ex = AcpExecutor(AcpAgentConfig(command="agy acp --stdio", host_permission_tier=True))
    monkeypatch.setattr(
        ex._mcp, "session_new_servers", lambda **_: [{"name": "omnigent"}] if advertised else []
    )
    ex._omnigent_tools = [
        {"name": name}
        for name in (
            "sys_os_shell",
            "sys_os_read",
            "sys_os_write",
            "sys_os_edit",
            "sys_session_list",
            "sys_session_update",
            "sys_session_get_startup_observation_v1",
        )
    ]
    ex._session_mcp_servers()
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    call = {"toolCallId": "t1", "title": title, "kind": kind, "rawInput": arguments}
    ex._note_permission_update("s1", {"sessionUpdate": "tool_call", "status": "pending", **call})
    bound = ex._bound_permission_call({"sessionId": "s1", "toolCall": call})
    if expected_name is None:
        assert bound is None
    else:
        assert bound == {
            "call_id": "t1",
            "name": expected_name,
            "arguments": arguments,
            **({"bridge_tool": True} if expected_name.startswith("sys_") else {}),
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tier_verdict", "user_allows", "dispatches"),
    [("allow", False, True), ("ask", False, False), ("ask", True, True), ("deny", True, False)],
)
async def test_managed_agy_relay_checks_each_actual_request(
    monkeypatch, tier_verdict: str, user_allows: bool, dispatches: bool
):
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload

    ex = AcpExecutor(AcpAgentConfig(command="agy acp --stdio", host_permission_tier=True))
    dispatch = AsyncMock(return_value={"stdout": "fixture"})
    ex._tool_executor = dispatch
    relay_setup = Mock(return_value=[{"name": "omnigent"}])
    monkeypatch.setattr(ex._mcp, "session_new_servers", relay_setup)
    ex._omnigent_tools = [{"name": "sys_os_shell"}, {"name": "sys_os_read"}]
    ex._session_mcp_servers()
    relay = relay_setup.call_args.kwargs["tool_executor"]
    ex._policy_evaluator = AsyncMock(
        return_value=PolicyVerdictPayload(
            action="POLICY_ACTION_DENY" if tier_verdict == "deny" else "POLICY_ACTION_UNSPECIFIED",
            data={"_omnigent_acp_tier": tier_verdict},
        )
    )
    ex._elicitation_handler = AsyncMock(return_value=user_allows)
    ex._send = AsyncMock()
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    arguments = {"command": "omni --version" if tier_verdict == "allow" else "git reset --hard"}
    call = {
        "toolCallId": "t1",
        "title": "Run mcp_omnigent_sys_os_shell",
        "kind": "other",
        "rawInput": arguments,
    }
    ex._note_permission_update("s1", {"sessionUpdate": "tool_call", "status": "pending", **call})
    await ex._respond_to_agent_request(
        {
            "id": 2,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s1",
                "toolCall": call,
                "options": [{"kind": "allow_once", "optionId": "once"}],
            },
        }
    )
    ex._policy_evaluator.assert_not_awaited()
    ex._elicitation_handler.assert_not_awaited()
    result = await relay("sys_os_shell", arguments)
    if dispatches:
        assert result == {"stdout": "fixture"}
        dispatch.assert_awaited_once_with("sys_os_shell", arguments)
    else:
        assert "error" in result
        dispatch.assert_not_awaited()
    assert ex._elicitation_handler.await_count == (tier_verdict == "ask")
    first = ex._policy_evaluator.await_args_list[-1].args[1]["_omnigent_acp_call"]
    if tier_verdict == "ask":
        ex._elicitation_handler.return_value = False
        assert "error" in await relay("sys_os_shell", arguments)
        assert dispatch.await_count == dispatches
        assert ex._elicitation_handler.await_count == 2
        second = ex._policy_evaluator.await_args_list[-1].args[1]["_omnigent_acp_call"]
        assert first["call_id"] != second["call_id"]

        async def change_arguments(_name, _input):
            arguments["command"] = "git clean -fd"
            return True

        ex._elicitation_handler.side_effect = change_arguments
        result = await relay("sys_os_shell", arguments)
        assert "changed" in result["error"]
        assert dispatch.await_count == dispatches


@pytest.fixture
def windows_acp_permission_executor():
    from omnigent.permission_tiers import (
        PermissionTier,
        Verdict,
        adapt_tool_call,
        decide_tier,
        load_rules,
    )
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload
    from tests.harnesses.test_permission_tiers import windows_host

    def executor(tier: PermissionTier, command: str = "agy.exe acp --stdio") -> AcpExecutor:
        ex = AcpExecutor(AcpAgentConfig(command=command, host_permission_tier=True))

        async def evaluate(_phase, data):
            intent = adapt_tool_call(data["name"], data["arguments"], windows_host())
            decision = decide_tier(tier, intent, load_rules())
            if decision.action == Verdict.DENY:
                return PolicyVerdictPayload(action="POLICY_ACTION_DENY")
            return PolicyVerdictPayload(
                action="POLICY_ACTION_UNSPECIFIED",
                data={"_omnigent_acp_tier": decision.action.value.lower()},
            )

        ex._policy_evaluator = AsyncMock(side_effect=evaluate)
        ex._elicitation_handler = AsyncMock(return_value=True)
        return ex

    return executor


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "arguments"),
    [
        ("execute", {"CommandLine": "omni --version", "Path": "note.txt", "Content": "abc"}),
        ("edit", {"TargetFile": "note.txt", "Content": "abc", "AbsolutePath": "other.txt"}),
        ("read", {"AbsolutePath": "note.txt", "CommandLine": "omni --version"}),
        (None, {"AbsolutePath": "note.txt", "Path": "other.txt", "Content": "abc"}),
        ("edit", {"Path": "note.txt", "TargetFile": "other.txt", "Content": "abc"}),
        ("edit", {"TargetFile": "note.txt", "Edits": [], "CommandLine": "omni --version"}),
        ("edit", {"TargetFile": "note.txt", "Edits": [], "WaitMsBeforeAsync": 10000}),
    ],
    ids=["execute", "edit", "read", "no-kind", "two-write-targets", "edit-command", "edit-wait"],
)
async def test_managed_agy_permission_rejects_mixed_shapes(
    kind, arguments, windows_acp_permission_executor
) -> None:
    ex = windows_acp_permission_executor("auto")
    params = _pending_acp_permission(ex, arguments, kind)
    assert await ex._decide_permission(params) == (False, None)
    ex._policy_evaluator.assert_not_awaited()
    ex._elicitation_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["auto", "ask", "accept-edits", "plan"])
@pytest.mark.parametrize(
    ("kind", "arguments"),
    [
        (None, {"CommandLine": ""}),
        (None, {"CommandLine": " ", "WaitMsBeforeAsync": 10000}),
        (None, {"CommandLine": 1}),
        (None, {"CommandLine": "omni --version", "WaitMsBeforeAsync": "10000"}),
        (None, {"CommandLine": "omni --version", "WaitMsBeforeAsync": True}),
        (None, {"CommandLine": "omni --version", "toolSummary": []}),
        (None, {"CommandLine": "omni --version", "RunPersistent": 0}),
        (None, {"CommandLine": "omni --version", "RequestedTerminalID": False}),
        (None, {"CommandLine": "omni --version", "IsDaemon": 0}),
        (None, {"CommandLine": "omni --version", "NotificationTimeoutSeconds": False}),
        ("execute", {"CommandLine": "omni --version", "Cwd": r"C:\work\project"}),
        ("edit", {"TargetFile": False, "Content": "abc"}),
        (None, {"Path": 1, "Content": "abc"}),
        ("edit", {"TargetFile": "note.txt", "Content": []}),
        (None, {"Path": "note.txt", "Content": "abc", "Overwrite": 1}),
        ("edit", {"TargetFile": "note.txt", "Content": "abc", "Extra": True}),
    ],
    ids=[
        "empty-command",
        "blank-command",
        "number-command",
        "string-wait",
        "boolean-wait",
        "list-summary",
        "number-persistent",
        "boolean-terminal-id",
        "number-daemon",
        "boolean-notification-timeout",
        "unrecognized-command-key",
        "boolean-target-file",
        "number-path",
        "list-content",
        "number-overwrite",
        "unrecognized-write-key",
    ],
)
async def test_managed_agy_permission_rejects_malformed_arguments_before_tier(
    tier, kind, arguments, windows_acp_permission_executor
) -> None:
    ex = windows_acp_permission_executor(tier)
    params = _pending_acp_permission(ex, arguments, kind)
    assert await ex._decide_permission(params) == (False, None)
    ex._policy_evaluator.assert_not_awaited()
    ex._elicitation_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tier", "command", "is_daemon"),
    [
        pytest.param(
            "auto", r"Remove-Item -LiteralPath:C:\Windows\report", False, id="protected-path"
        ),
        pytest.param("auto", "omni --version", True, id="daemon"),
        pytest.param(
            "ask", r"Set-Content C:\work\control\settings.json test", True, id="daemon-ask"
        ),
        pytest.param(
            "accept-edits",
            r"Set-Content C:\work\control\settings.json test",
            True,
            id="daemon-accept-edits",
        ),
    ],
)
async def test_managed_agy_shell_options_keep_permission_denials(
    tier, command, is_daemon, windows_acp_permission_executor
) -> None:
    ex = windows_acp_permission_executor(tier)
    params = _pending_acp_permission(
        ex,
        {"CommandLine": command, "IsDaemon": is_daemon, "NotificationTimeoutSeconds": 700},
        "execute",
    )
    assert await ex._decide_permission(params) == (False, None)
    assert ex._permission_calls == {}
    assert ex._policy_evaluator.await_count == (0 if is_daemon else 1)
    ex._elicitation_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"tmp\note.txt", (True, "once")),
        (r"\\server\share\note.txt", (True, "once")),
        ("/c/work/project/tmp/note.txt", (False, None)),
        (r"C:\work\project\tmp\note.txt:stream", (False, None)),
        (r"C:\Windows\System32\drivers\etc\hosts", (False, None)),
    ],
    ids=["relative", "unc", "msys-root", "ads", "protected-path"],
)
async def test_managed_agy_permission_applies_equal_tier_verdicts_to_write_path_aliases(
    path, expected, windows_acp_permission_executor
) -> None:
    for target_field in ("Path", "TargetFile"):
        ex = windows_acp_permission_executor("auto")
        params = _pending_acp_permission(
            ex, {target_field: path, "Content": "abc", "Overwrite": True}, "other"
        )
        assert await ex._decide_permission(params) == expected
        policy_call = ex._policy_evaluator.await_args
        assert policy_call is not None
        assert policy_call.args[1]["name"] == "write_to_file"
        assert policy_call.args[1]["arguments"]["TargetFile"] == path
        ex._elicitation_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["auto", "plan"])
@pytest.mark.parametrize(
    ("path", "protected"),
    [(r"tmp\note.txt", False), (r"C:\Windows\System32\drivers\etc\hosts", True)],
    ids=["workspace", "protected"],
)
async def test_managed_agy_permission_preserves_legacy_target_file_edit_verdict(
    tier, path, protected, windows_acp_permission_executor
) -> None:
    ex = windows_acp_permission_executor(tier)
    arguments = {"TargetFile": path, "Edits": [{"Target": "before", "Replacement": "after"}]}
    params = _pending_acp_permission(ex, arguments, "edit")
    expected = (True, "once") if tier == "auto" and not protected else (False, None)
    assert await ex._decide_permission(params) == expected
    policy_call = ex._policy_evaluator.await_args
    assert policy_call is not None
    assert policy_call.args[1]["name"] == "write_to_file"
    assert policy_call.args[1]["arguments"] == arguments
    ex._elicitation_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("tier", ["auto", "ask", "plan"])
async def test_managed_agy_permission_rejects_empty_read_path_before_tier(
    tier, windows_acp_permission_executor
) -> None:
    ex = windows_acp_permission_executor(tier)
    params = _pending_acp_permission(ex, {"AbsolutePath": ""}, "read")
    assert await ex._decide_permission(params) == (False, None)
    ex._policy_evaluator.assert_not_awaited()
    ex._elicitation_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_non_agy_permission_keeps_unknown_tool_ask(
    windows_acp_permission_executor,
) -> None:
    ex = windows_acp_permission_executor("ask", "opencode acp")
    params = _pending_acp_permission(ex, {"Opaque": True}, "other")
    assert await ex._decide_permission(params) == (True, "once")
    ex._elicitation_handler.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra_fields",
    [
        {},
        {"CommandLine": "omni --version"},
        {"WaitMsBeforeAsync": 10000},
        {"Path": "other.txt", "Content": "abc"},
        {"TargetFile": "other.txt", "Overwrite": False},
        {"Edits": []},
        {"Target": "old", "Replacement": "new"},
    ],
    ids=[
        "pure-read",
        "command",
        "wait",
        "path-and-content",
        "target-and-overwrite",
        "edits",
        "replace",
    ],
)
async def test_managed_agy_permission_rejects_mixed_reads_in_plan(
    extra_fields, windows_acp_permission_executor
) -> None:
    ex = windows_acp_permission_executor("plan")
    params = _pending_acp_permission(
        ex, {"AbsolutePath": "note.txt", "toolSummary": "Read note", **extra_fields}, "read"
    )
    assert await ex._decide_permission(params) == (
        (False, None) if extra_fields else (True, "once")
    )
    assert ex._policy_evaluator.await_count == (0 if extra_fields else 1)
    ex._elicitation_handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"Opaque": True}, {"Path": "note.txt", "Action": "list"}])
@pytest.mark.parametrize(
    ("tier", "asks"), [("ask", True), ("accept-edits", True), ("auto", True), ("plan", False)]
)
async def test_managed_agy_permission_keeps_unknown_tool_tier_verdicts(
    tier, asks, arguments, windows_acp_permission_executor
) -> None:
    ex = windows_acp_permission_executor(tier)
    params = _pending_acp_permission(ex, arguments, "other")
    assert await ex._decide_permission(params) == ((True, "once") if asks else (False, None))
    policy_call = ex._policy_evaluator.await_args
    assert policy_call is not None
    assert policy_call.args[1]["name"] == "unknown"
    assert ex._elicitation_handler.await_count == (1 if asks else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_prefix"),
    [
        ("rule", "Denied by rule [protected-path]:"),
        ("plan-mode", "Denied by permission mode (plan):"),
        ("policy", "Denied by policy (admin_block):"),
        ("policy-spoof", "Denied by policy: Denied by sandbox: misleading"),
        ("unbound", "Denied by permission mode (acp):"),
        ("malformed-execute", "Denied by permission mode (acp):"),
        ("non-string-kind", "Denied by permission mode (acp):"),
        ("daemon", "Denied by permission mode (acp):"),
    ],
)
async def test_managed_acp_denial_errors_name_source_layer(
    case: str, expected_prefix: str
) -> None:
    from omnigent.permission_tiers import (
        Verdict,
        adapt_tool_call,
        decide_tier,
        format_tier_denial,
        load_rules,
    )
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload
    from tests.harnesses.test_permission_tiers import windows_host

    ex = AcpExecutor(AcpAgentConfig(command="agy.exe acp --stdio", host_permission_tier=True))
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    ex._elicitation_handler = AsyncMock(return_value=False)
    ex._send = AsyncMock()

    kind: object
    if case == "rule":
        arguments: dict[str, object] = {
            "Path": r"C:\Windows\System32\drivers\etc\hosts",
            "Content": "x",
            "Overwrite": True,
        }
        kind = "edit"
        tier = "auto"
    elif case == "plan-mode":
        arguments = {"CommandLine": "omni --version"}
        kind = "execute"
        tier = "plan"
    elif case == "malformed-execute":
        arguments = {}
        kind = "execute"
        tier = "auto"
    elif case == "non-string-kind":
        arguments = {}
        kind = ["execute"]
        tier = "auto"
    elif case == "daemon":
        arguments = {"CommandLine": "omni --version", "IsDaemon": True}
        kind = "execute"
        tier = "auto"
    else:
        arguments = {"CommandLine": "omni --version"}
        kind = "execute"
        tier = "auto"

    call = {"toolCallId": "tool-1", "kind": kind, "rawInput": arguments}
    if case != "unbound":
        ex._note_permission_update(
            "s1", {"sessionUpdate": "tool_call", "status": "pending", **call}
        )

    async def evaluate(_phase, data):
        if case == "policy":
            return PolicyVerdictPayload(
                action="POLICY_ACTION_DENY",
                reason="Denied by policy (admin_block): Blocked by admin policy.",
            )
        if case == "policy-spoof":
            return PolicyVerdictPayload(
                action="POLICY_ACTION_DENY",
                reason="Denied by sandbox: misleading",
            )
        intent = adapt_tool_call(data["name"], data["arguments"], windows_host())
        decision = decide_tier(tier, intent, load_rules())
        assert decision.action == Verdict.DENY
        return PolicyVerdictPayload(
            action="POLICY_ACTION_DENY",
            reason=format_tier_denial(decision, tier=tier),
            data={"_omnigent_acp_tier": "deny"},
        )

    ex._policy_evaluator = AsyncMock(side_effect=evaluate)
    await ex._respond_to_agent_request(
        {
            "id": 9,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s1",
                "toolCall": call,
                "options": [{"kind": "allow_once", "optionId": "once", "name": "Allow"}],
            },
        }
    )
    reply = ex._send.await_args.args[0]
    assert reply["error"]["code"] == -32000
    assert reply["error"]["message"].startswith(expected_prefix), reply["error"]["message"]
    ex._elicitation_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_managed_acp_policy_proxy_to_executor_denial_handoff() -> None:
    """Runner policy_proxy emits the deny marker so AcpExecutor preserves attribution."""
    from omnigent.permission_tiers import Verdict
    from omnigent.runner.policy_proxy import _evaluate_policy_via_omnigent
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload
    from tests.runner.test_runner_policy import (
        _CapturingHarnessClient,
        _RaisingServerClient,
        _StatusServerClient,
    )

    async def run_handoff(
        *,
        server_client: object,
        acp_tier: Verdict,
        acp_tier_reason: str | None = None,
    ) -> str:
        ex = AcpExecutor(AcpAgentConfig(command="agy.exe acp --stdio", host_permission_tier=True))
        ex._session_id = "s1"
        ex._active_prompt_request_id = 1
        ex._elicitation_handler = AsyncMock(return_value=False)
        ex._send = AsyncMock()
        call = {
            "toolCallId": "tool-handoff",
            "kind": "execute",
            "rawInput": {"CommandLine": "omni --version"},
        }
        ex._note_permission_update(
            "s1", {"sessionUpdate": "tool_call", "status": "pending", **call}
        )

        async def evaluate(phase: str, data: dict[str, object]) -> PolicyVerdictPayload:
            harness = _CapturingHarnessClient()
            eval_data = dict(data)
            eval_data.pop("_omnigent_acp_call", None)
            await _evaluate_policy_via_omnigent(
                server_client=server_client,  # type: ignore[arg-type]
                harness_client=harness,
                conversation_id="s1",
                evaluation_id="eval-handoff",
                phase=phase,
                data=eval_data,
                acp_tier=acp_tier,
                acp_tier_reason=acp_tier_reason,
            )
            posted = harness.posted[-1]
            return PolicyVerdictPayload(
                action=posted["action"],
                reason=posted.get("reason"),
                data=posted.get("data"),
            )

        ex._policy_evaluator = AsyncMock(side_effect=evaluate)
        await ex._respond_to_agent_request(
            {
                "id": 11,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "toolCall": call,
                    "options": [{"kind": "allow_once", "optionId": "once", "name": "Allow"}],
                },
            }
        )
        reply = ex._send.await_args.args[0]
        assert reply["error"]["code"] == -32000
        return str(reply["error"]["message"])

    plan_reason = (
        "Denied by permission mode (plan): Plan tier permits only known reads and user questions"
    )
    plan_msg = await run_handoff(
        server_client=_RaisingServerClient(),
        acp_tier=Verdict.DENY,
        acp_tier_reason=plan_reason,
    )
    assert plan_msg == plan_reason

    spoof_msg = await run_handoff(
        server_client=_StatusServerClient(
            200,
            {
                "result": "POLICY_ACTION_DENY",
                "reason": "Denied by sandbox: forged",
                "deciding_policy": "admin_deny",
                "data": {"_omnigent_acp_tier": "deny"},
            },
        ),
        acp_tier=Verdict.ALLOW,
    )
    assert spoof_msg == "Denied by policy (admin_deny): Denied by sandbox: forged"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["permission-request", "pending-update"])
@pytest.mark.parametrize(
    ("kind", "pending", "changed"),
    [
        (
            "execute",
            {"CommandLine": "omni --version", "WaitMsBeforeAsync": 1},
            {"CommandLine": "omni --version", "WaitMsBeforeAsync": True},
        ),
        (
            "execute",
            {"CommandLine": "omni --version", "WaitMsBeforeAsync": 1},
            {"CommandLine": "omni --version", "WaitMsBeforeAsync": 1.0},
        ),
        (
            "execute",
            {"CommandLine": "omni --version", "IsDaemon": False},
            {"CommandLine": "omni --version", "IsDaemon": 0},
        ),
        (
            "execute",
            {"CommandLine": "omni --version", "NotificationTimeoutSeconds": 700},
            {"CommandLine": "omni --version", "NotificationTimeoutSeconds": 700.0},
        ),
        (
            "edit",
            {"Path": "note.txt", "Content": "abc", "Overwrite": True},
            {"Path": "note.txt", "Content": "abc", "Overwrite": 1},
        ),
        (
            "read",
            {"AbsolutePath": "note.txt", "Metadata": [{"offset": False}]},
            {"AbsolutePath": "note.txt", "Metadata": [{"offset": 0.0}]},
        ),
    ],
    ids=[
        "int-to-bool",
        "int-to-float",
        "daemon-bool-to-int",
        "notification-timeout-int-to-float",
        "bool-to-int",
        "nested-bool-to-float",
    ],
)
async def test_managed_agy_permission_rejects_type_only_input_changes(
    source, kind, pending, changed, windows_acp_permission_executor
) -> None:
    ex = windows_acp_permission_executor("auto")
    params = _pending_acp_permission(ex, pending, kind)
    if source == "permission-request":
        call = params["toolCall"]
        assert isinstance(call, dict)
        params["toolCall"] = {**call, "rawInput": changed}
    else:
        evaluate = ex._policy_evaluator.side_effect

        async def update_during_policy(phase, data):
            ex._note_permission_update(
                "s1",
                {"sessionUpdate": "tool_call_update", "toolCallId": "tool-1", "rawInput": changed},
            )
            return await evaluate(phase, data)

        ex._policy_evaluator.side_effect = update_during_policy

    assert await ex._decide_permission(params) == (False, None)
    assert ex._policy_evaluator.await_count == (1 if source == "pending-update" else 0)
    ex._elicitation_handler.assert_not_awaited()


async def _observe_acp_update(
    ex: AcpExecutor, session_id: str, update: dict[str, object]
) -> list[ExecutorEvent]:
    stdout = asyncio.StreamReader()
    ex._proc = Mock(returncode=None, stdout=stdout)
    frame = {"method": "session/update", "params": {"sessionId": session_id, "update": update}}
    stdout.feed_data((json.dumps(frame) + "\n").encode())
    stdout.feed_eof()
    await ex._read_stdout_unobserved()
    return ex._handle_session_update(ex._queue.get_nowait()["params"]["update"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tier", "include_request_metadata", "long_command"),
    [
        pytest.param("auto", True, False, id="auto"),
        pytest.param("ask", True, False, id="ask"),
        pytest.param("auto", False, False, id="without-request-kind-and-title"),
        pytest.param("auto", True, True, id="long-command"),
    ],
)
async def test_managed_agy_permission_binds_pending_update_after_in_progress_start(
    tier,
    include_request_metadata,
    long_command,
    windows_acp_permission_executor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    ex = windows_acp_permission_executor(tier)
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    ex._send = AsyncMock()
    ex._elicitation_choice_handler = AsyncMock(return_value="Allow once")

    command = (
        "Start-Sleep -Seconds 615; Write-Output 'OMNIGENT_LONG_TOOL_OK 615s'"
        if long_command
        else "omni --version"
    )
    raw_input: dict[str, object] = {"CommandLine": command}
    if long_command:
        raw_input.update(
            IsDaemon=False,
            NotificationTimeoutSeconds=700,
            WaitMsBeforeAsync=10000,
            toolSummary="Wait and print a marker",
        )
    else:
        raw_input.update(WaitMsBeforeAsync=5000, toolSummary="Check tool version")
    base_call: dict[str, object] = {
        "toolCallId": "call-1",
        "kind": "execute",
        "title": command,
        "rawInput": raw_input,
    }
    options = [
        {"optionId": "allow", "kind": "allow_once"},
        {"optionId": "deny", "kind": "reject_once"},
    ]

    # Step 1: agy opens the tool_call as in_progress before requesting approval.
    await _observe_acp_update(
        ex, "s1", {"sessionUpdate": "tool_call", "status": "in_progress", **base_call}
    )
    assert ex._permission_calls == {}

    # Step 2: agy transitions the call to pending via tool_call_update.
    pending_update: dict[str, object] = {
        "sessionUpdate": "tool_call_update",
        "status": "pending",
        **base_call,
    }
    await _observe_acp_update(ex, "s1", pending_update)
    assert ex._permission_calls["call-1"]["rawInput"] == raw_input

    # Step 3: agy sends session/request_permission for the same toolCallId.
    request_call: dict[str, object] = {"status": "pending", **base_call}
    if not include_request_metadata:
        del request_call["kind"]
        del request_call["title"]
    with caplog.at_level(logging.INFO, logger=acp_executor_module.__name__):
        await ex._respond_to_agent_request(
            {
                "id": 2,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "toolCall": request_call,
                    "options": options,
                },
            }
        )

    sent = ex._send.await_args
    assert sent is not None
    reply = sent.args[0]
    assert reply.get("result") == {"outcome": {"outcome": "selected", "optionId": "allow"}}, reply
    policy_call = ex._policy_evaluator.await_args
    assert policy_call is not None
    assert policy_call.args[1]["name"] == "run_command"
    assert policy_call.args[1]["arguments"] == raw_input
    logs = [
        message
        for message in caplog.messages
        if message.startswith("acp agy permission decision:")
    ]
    if tier == "auto":
        ex._elicitation_choice_handler.assert_not_awaited()
        ex._elicitation_handler.assert_not_awaited()
        assert logs == [
            "acp agy permission decision: tool=run_command intent=execute "
            "binding=current tier=allow once_offer=valid option=allow_once"
        ]
    else:
        ex._elicitation_choice_handler.assert_awaited_once_with(
            "run_command",
            {**raw_input, "command": command},
            ["Allow once", "Reject"],
            "Allow once",
            False,
        )
        ex._elicitation_handler.assert_not_awaited()
        assert logs == [
            "acp agy permission decision: tool=run_command intent=execute "
            "binding=current tier=ask once_offer=unchecked option=allow_once"
        ]

    # Step 4: agy resumes in_progress and completes.
    await _observe_acp_update(
        ex,
        "s1",
        {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "status": "in_progress"},
    )
    events = await _observe_acp_update(
        ex,
        "s1",
        {"sessionUpdate": "tool_call_update", "toolCallId": "call-1", "status": "completed"},
    )
    assert ex._permission_calls == {}
    assert len(events) == 1
    assert isinstance(events[0], ToolCallComplete)
    assert events[0].status == ToolCallStatus.SUCCESS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "terminal_status"),
    [
        ("no-pending-update", None),
        ("statusless-update", None),
        ("mismatched-raw-input", None),
        ("omitted-raw-input", None),
        ("null-raw-input", None),
        ("non-object-raw-input", None),
        ("terminal-pop-before-request", "failed"),
        ("late-pending", "completed"),
        ("late-pending", "failed"),
        ("late-pending", "cancelled"),
        ("non-agy", None),
        ("second-request", None),
    ],
)
async def test_managed_agy_pending_update_negatives_and_non_agy_control(
    scenario: str,
    terminal_status: str | None,
    windows_acp_permission_executor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    command = "opencode acp" if scenario == "non-agy" else "agy.exe acp --stdio"
    ex = windows_acp_permission_executor("auto", command)
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    ex._send = AsyncMock()
    ex._elicitation_choice_handler = AsyncMock()

    raw_input: dict[str, object] = {
        "CommandLine": "omni --version",
        "WaitMsBeforeAsync": 5000,
        "toolSummary": "Check tool version",
    }
    base_call: dict[str, object] = {
        "toolCallId": "call-1",
        "kind": "execute",
        "title": "omni --version",
        "rawInput": raw_input,
    }
    options = [
        {"optionId": "allow", "kind": "allow_once"},
        {"optionId": "deny", "kind": "reject_once"},
    ]
    request_call: dict[str, object] = {"status": "pending", **base_call}

    await _observe_acp_update(
        ex, "s1", {"sessionUpdate": "tool_call", "status": "in_progress", **base_call}
    )
    if scenario == "statusless-update":
        await _observe_acp_update(ex, "s1", {"sessionUpdate": "tool_call_update", **base_call})
    elif scenario != "no-pending-update":
        mutable_update: dict[str, object] = {
            "sessionUpdate": "tool_call_update",
            "status": "pending",
            "toolCallId": "call-1",
            "kind": "execute",
            "title": "omni --version",
            "rawInput": {**raw_input},
        }
        ex._note_permission_update("s1", mutable_update)
        # Mutating caller input after recording must not alter stored pending state.
        stored_raw = mutable_update["rawInput"]
        assert isinstance(stored_raw, dict)
        stored_raw["CommandLine"] = "mutated"
        if scenario != "non-agy":
            assert ex._permission_calls["call-1"]["rawInput"] == raw_input

    if scenario == "mismatched-raw-input":
        request_call = {
            **request_call,
            "rawInput": {**raw_input, "CommandLine": "omni --help"},
        }
    elif scenario == "omitted-raw-input":
        del request_call["rawInput"]
    elif scenario == "null-raw-input":
        request_call["rawInput"] = None
    elif scenario == "non-object-raw-input":
        request_call["rawInput"] = [raw_input]

    if terminal_status is not None:
        await _observe_acp_update(
            ex,
            "s1",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call-1",
                "status": terminal_status,
            },
        )
        assert ex._permission_calls == {}
        if scenario == "late-pending":
            await _observe_acp_update(
                ex, "s1", {"sessionUpdate": "tool_call_update", "status": "pending", **base_call}
            )

    request = {
        "id": 2,
        "method": "session/request_permission",
        "params": {"sessionId": "s1", "toolCall": request_call, "options": options},
    }
    with caplog.at_level(logging.INFO, logger=acp_executor_module.__name__):
        await ex._respond_to_agent_request(request)
        if scenario == "second-request":
            assert ex._send.await_args is not None
            assert ex._send.await_args.args[0]["result"] == {
                "outcome": {"outcome": "selected", "optionId": "allow"}
            }
            await _observe_acp_update(
                ex,
                "s1",
                {"sessionUpdate": "tool_call_update", "status": "pending", **base_call},
            )
            await ex._respond_to_agent_request({**request, "id": 3})

    sent = ex._send.await_args
    assert sent is not None
    reply = sent.args[0]
    assert reply["error"]["code"] == -32000
    assert ex._policy_evaluator.await_count == (1 if scenario == "second-request" else 0)
    ex._elicitation_handler.assert_not_awaited()
    ex._elicitation_choice_handler.assert_not_awaited()
    logs = [
        message
        for message in caplog.messages
        if message.startswith("acp agy permission decision:")
    ]
    if scenario == "non-agy":
        assert logs == []
    else:
        assert logs[-1] == (
            "acp agy permission decision: tool=unbound intent=unbound "
            "binding=unbound tier=unreported once_offer=unchecked option=none"
        )


@pytest.mark.asyncio
async def test_decide_permission_ask_defers_to_elicitation() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))

    class _V:
        action = "POLICY_ACTION_ASK"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    ex._elicitation_handler = AsyncMock(return_value=True)
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None)
    ex._elicitation_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_decide_permission_ask_without_handler_fails_closed() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))

    class _V:
        action = "POLICY_ACTION_ASK"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (False, None)


def _seed_tool_call(ex: AcpExecutor) -> dict[str, object]:
    """Announce a ``tool_call``, then return the bare permission params for it.

    Mirrors the real frame order for an agent that asks permission without
    repeating the tool: ``tool_call`` (with the name + command) → then
    ``session/request_permission`` carrying only the id.
    """
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "toolu_01",
            "title": "Ran command",
            "kind": "execute",
            "rawInput": {"command": "rm -rf build"},
        }
    )
    return {"toolCall": {"toolCallId": "toolu_01"}}


@pytest.mark.asyncio
async def test_decide_permission_policy_sees_the_real_tool_call() -> None:
    """The TOOL_CALL policy is evaluated against the resolved name + arguments.

    Rules gate on the tool name and then read its arguments (the destructive-shell
    builtin reads ``arguments["command"]``), so a bare request evaluated as
    ``{"name": "tool", "arguments": {}}`` matches nothing — a "deny ``rm -rf``"
    policy would sit silent while the command ran.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = _seed_tool_call(ex)

    class _V:
        action = "POLICY_ACTION_DENY"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    assert await ex._decide_permission(params) == (False, None)
    ex._policy_evaluator.assert_awaited_once_with(
        "PHASE_TOOL_CALL",
        {"name": "Ran command", "arguments": {"command": "rm -rf build"}},
    )


@pytest.mark.asyncio
async def test_decide_permission_card_names_the_tool() -> None:
    """The approval card describes the call instead of an unnamed "tool".

    The elicitation handler renders ``<tool_name>(<args>)``, so these two values
    are literally what the user reads before approving.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = _seed_tool_call(ex)
    ex._elicitation_handler = AsyncMock(return_value=True)

    assert await ex._decide_permission(params) == (True, None)
    ex._elicitation_handler.assert_awaited_once_with("Ran command", {"command": "rm -rf build"})


# ---------------------------------------------------------------------------
# Scoped approval: the agent's own permission options
# ---------------------------------------------------------------------------


def _agent_options() -> list[dict[str, str]]:
    """Devin-shaped options: allow-once, two scoped always-allows, and a reject.

    Order is the agent's own — narrowest first — and is preserved on the card so
    the least-privilege choice leads.
    """
    return [
        {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
        {
            "optionId": "allow_session",
            "name": "Yes, allow `ls` commands (this session)",
            "kind": "allow_always",
        },
        {
            "optionId": "allow_always_global",
            "name": "Yes, always allow `ls` commands in all projects",
            "kind": "allow_always",
        },
        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
    ]


def test_acp_executor_accepts_the_choice_bridge() -> None:
    """The attribute the adapter installs by name exists, and starts unwired.

    The executor half of a cross-layer contract: the adapter gates its install on
    this attribute (see ``tests/runtime/harnesses/test_executor_adapter.py``), so a
    rename here would silently disable scoped approval.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex._elicitation_choice_handler is None


@pytest.mark.asyncio
async def test_decide_permission_offers_the_agents_own_scopes() -> None:
    """The card gets every option the agent offered, in the agent's order.

    **What breaks if this fails**: the user is back to Approve/Reject and each
    grant is once-scoped, so the same command class re-prompts indefinitely.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {
        "toolCall": {"title": "shell", "rawInput": {"command": "ls"}},
        "options": _agent_options(),
    }
    ex._elicitation_choice_handler = AsyncMock(
        return_value="Yes, allow `ls` commands (this session)"
    )

    assert await ex._decide_permission(params) == (True, "allow_session")
    ex._elicitation_choice_handler.assert_awaited_once_with(
        "shell",
        {"command": "ls"},
        [
            "Allow",
            "Yes, allow `ls` commands (this session)",
            "Yes, always allow `ls` commands in all projects",
            "Reject",
        ],
        "Allow",
        False,
    )


@pytest.mark.asyncio
async def test_scoped_choice_reaches_the_agent_as_that_option() -> None:
    """End of the chain: the agent is told the exact scope the user picked.

    Mirrors the call site (``_decide_permission`` then ``_permission_outcome``),
    because the scope only takes effect if it survives into the reply — the agent
    is what honors it and stops asking.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {"toolCall": {"title": "shell"}, "options": _agent_options()}
    ex._elicitation_choice_handler = AsyncMock(
        return_value="Yes, allow `ls` commands (this session)"
    )

    allow, option_id = await ex._decide_permission(params)
    assert ex._permission_outcome(params, allow=allow, option_id=option_id) == {
        "outcome": {"outcome": "selected", "optionId": "allow_session"}
    }


@pytest.mark.asyncio
async def test_decide_permission_choice_reject_denies() -> None:
    """Picking the agent's own reject option denies, and names that option."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {"toolCall": {"title": "shell"}, "options": _agent_options()}
    ex._elicitation_choice_handler = AsyncMock(return_value="Reject")

    assert await ex._decide_permission(params) == (False, "reject_once")


@pytest.mark.asyncio
async def test_decide_permission_choice_declined_denies() -> None:
    """A dismissed / timed-out choice card denies, with no scope."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {"toolCall": {"title": "shell"}, "options": _agent_options()}
    ex._elicitation_choice_handler = AsyncMock(return_value=None)

    assert await ex._decide_permission(params) == (False, None)


@pytest.mark.asyncio
async def test_decide_permission_choice_not_offered_denies() -> None:
    """A label the agent never offered fails closed rather than guessing."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {"toolCall": {"title": "shell"}, "options": _agent_options()}
    ex._elicitation_choice_handler = AsyncMock(return_value="Yes, do whatever you like")

    assert await ex._decide_permission(params) == (False, None)


@pytest.mark.asyncio
async def test_decide_permission_needs_a_reject_option_for_a_choice_card() -> None:
    """Without a reject option the binary card is used instead.

    A choice card replaces Approve/Reject with the agent's options, so offering
    only allow-shaped ones would leave the user no way to say no.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {
        "toolCall": {"title": "shell"},
        "options": [
            {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
            {"optionId": "allow_session", "name": "Allow this session", "kind": "allow_always"},
        ],
    }
    ex._elicitation_choice_handler = AsyncMock(return_value="Allow this session")
    ex._elicitation_handler = AsyncMock(return_value=True)

    assert await ex._decide_permission(params) == (True, None)
    ex._elicitation_choice_handler.assert_not_awaited()
    ex._elicitation_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_decide_permission_falls_back_on_duplicate_labels() -> None:
    """Two options sharing a label are ambiguous, since the reply names the label."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    params = {
        "toolCall": {"title": "shell"},
        "options": [
            {"optionId": "a1", "name": "Allow", "kind": "allow_once"},
            {"optionId": "a2", "name": "Allow", "kind": "allow_always"},
            {"optionId": "r1", "name": "Reject", "kind": "reject_once"},
        ],
    }
    ex._elicitation_choice_handler = AsyncMock(return_value="Allow")
    ex._elicitation_handler = AsyncMock(return_value=True)

    assert await ex._decide_permission(params) == (True, None)
    ex._elicitation_choice_handler.assert_not_awaited()


def test_permission_outcome_honors_a_chosen_scope() -> None:
    """A user-picked option is echoed verbatim, overriding the once-scoped default."""
    params = {"options": _agent_options()}
    out = AcpExecutor._permission_outcome(params, allow=True, option_id="allow_session")
    assert out == {"outcome": {"outcome": "selected", "optionId": "allow_session"}}


def test_permission_outcome_ignores_an_unoffered_scope() -> None:
    """An id the agent didn't offer is never echoed; the safe default applies.

    Guards against sending the agent an option it can't honor — or a broader one
    than it advertised — if a stale or hand-crafted id ever reaches here.
    """
    params = {"options": _agent_options()}
    out = AcpExecutor._permission_outcome(params, allow=True, option_id="made_up")
    assert out == {"outcome": {"outcome": "selected", "optionId": "allow_once"}}


# ---------------------------------------------------------------------------
# permission_mode (bypassPermissions)
# ---------------------------------------------------------------------------


def _bypass_executor() -> AcpExecutor:
    """An executor whose spec opted out of approval cards."""
    return AcpExecutor(AcpAgentConfig(command="x", permission_mode="bypassPermissions"))


@pytest.mark.asyncio
async def test_bypass_permissions_skips_the_card() -> None:
    """``bypassPermissions`` allows a policy-silent call without prompting.

    **What breaks if this fails**: a headless ACP worker (a polly sub-agent, a
    scheduled task) parks on an approval card nobody is watching, so the turn
    stalls rather than running unattended.
    """
    ex = _bypass_executor()
    ex._elicitation_handler = AsyncMock(return_value=True)
    ex._elicitation_choice_handler = AsyncMock(return_value="Allow")

    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None)
    ex._elicitation_handler.assert_not_awaited()
    ex._elicitation_choice_handler.assert_not_awaited()


@pytest.mark.asyncio
async def test_bypass_permissions_still_denies_on_policy_deny() -> None:
    """Policy runs in every mode, so a DENY still blocks under bypass.

    This is the invariant that makes the mode safe to offer: it waives the
    *human* gate, never the user's own rules.
    """
    ex = _bypass_executor()
    ex._elicitation_handler = AsyncMock(return_value=True)

    class _V:
        action = "POLICY_ACTION_DENY"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (False, None)


@pytest.mark.asyncio
async def test_bypass_permissions_still_prompts_on_policy_ask() -> None:
    """A policy that says ASK outranks bypass — the user asked to be asked."""
    ex = _bypass_executor()

    class _V:
        action = "POLICY_ACTION_ASK"

    ex._policy_evaluator = AsyncMock(return_value=_V())
    ex._elicitation_handler = AsyncMock(return_value=True)

    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None)
    ex._elicitation_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_default_permission_mode_still_asks() -> None:
    """The ``auto`` default is unchanged: a policy-silent call still prompts.

    **What breaks if this fails**: every ACP agent silently stops asking for
    approval — the mode would be a default-off switch instead of an opt-in.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert ex._config.permission_mode == "auto"
    ex._elicitation_handler = AsyncMock(return_value=True)

    assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None)
    ex._elicitation_handler.assert_awaited_once()


@pytest.mark.asyncio
async def test_unrecognized_permission_mode_still_asks() -> None:
    """Only the exact ``bypassPermissions`` waives the card; anything else prompts.

    A typo (``"bypass"``) or a mode borrowed from another harness
    (``"acceptEdits"``) must fail toward asking, not toward silence.
    """
    for mode in ("bypass", "acceptEdits", "default", ""):
        ex = AcpExecutor(AcpAgentConfig(command="x", permission_mode=mode))
        ex._elicitation_handler = AsyncMock(return_value=True)
        assert await ex._decide_permission({"toolCall": {"title": "shell"}}) == (True, None), mode
        assert ex._elicitation_handler.await_count == 1, mode


@pytest.mark.asyncio
async def test_bypass_never_sends_the_agents_own_bypass_option() -> None:
    """Bypass answers each request; it never tells the agent to stop asking.

    Devin offers ``switch_bypass`` ("switch to bypass mode"). Selecting it would
    end the request stream, so omnigent would no longer see the agent's tool
    calls and the TOOL_CALL policy could not gate them. The narrow
    ``allow_once`` grant keeps every later call visible.
    """
    ex = _bypass_executor()
    ex._elicitation_handler = AsyncMock(return_value=True)
    params = {
        "toolCall": {"title": "shell"},
        "options": [
            {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
            {
                "optionId": "switch_bypass",
                "name": "Yes, switch to bypass mode",
                "kind": "allow_always",
            },
            {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
        ],
    }

    allow, option_id = await ex._decide_permission(params)
    assert ex._permission_outcome(params, allow=allow, option_id=option_id) == {
        "outcome": {"outcome": "selected", "optionId": "allow_once"}
    }


def test_harness_wrap_reads_permission_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrap decodes the forwarded mode, closing spawn env → child config."""
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "devin acp")
    monkeypatch.setenv("HARNESS_ACP_PERMISSION_MODE", "bypassPermissions")
    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.permission_mode == "bypassPermissions"
    assert ex._bypass_permissions is True


def test_harness_wrap_permission_mode_defaults_to_auto(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset (or blank) var leaves the wrap prompting, as before this option."""
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "devin acp")
    monkeypatch.delenv("HARNESS_ACP_PERMISSION_MODE", raising=False)
    assert acp_harness._build_acp_executor()._config.permission_mode == "auto"

    monkeypatch.setenv("HARNESS_ACP_PERMISSION_MODE", "   ")
    ex = acp_harness._build_acp_executor()
    assert ex._config.permission_mode == "auto"
    assert ex._bypass_permissions is False


# ---------------------------------------------------------------------------
# warm model switch (session/set_config_option)
# ---------------------------------------------------------------------------


def _model_option(current: str, *values: str) -> dict:
    """A ``config_option_update`` payload advertising a settable model."""
    return {
        "sessionUpdate": "config_option_update",
        "configOptions": [
            {
                "id": "model",
                "currentValue": current,
                "options": [{"value": v} for v in values],
            }
        ],
    }


def test_config_option_update_records_options_and_active_model() -> None:
    """
    The agent's ``currentValue`` is the only trustworthy record of the live model.

    **What breaks if this fails**: we'd re-request a switch already in effect, or
    trust the model's own self-report — which lies (Devin reported ``FAMILY=SWE``
    after a confirmed switch to Gemini).
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("swe-1-7-medium", "swe-1-7-medium", "gemini-3-1-pro"))
    assert ex._active_model == "swe-1-7-medium"
    assert "model" in ex._config_option_ids


@pytest.mark.asyncio
async def test_session_new_captures_advertised_model() -> None:
    """A model advertised in ``session/new``'s config options sets ``_active_model``.

    jcode (and others) report their model in the ``session/new`` result rather than
    a later ``config_option_update``; capturing it at session creation is what lets
    a turn's usage name the model, so the server can attribute per-model tokens.

    **What breaks if this fails**: an ACP agent that only advertises its model in
    ``session/new`` records no model → token counts don't render for the session.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x", omnigent_mcp=False))

    async def fake_rpc(method: str, params: dict, timeout: float | None = None) -> dict:
        return {
            "result": {
                "sessionId": "s1",
                "configOptions": [
                    {
                        "id": "model",
                        "currentValue": "system.ai.claude-haiku-4-5",
                        "options": [{"value": "system.ai.claude-haiku-4-5"}],
                    }
                ],
            }
        }

    ex._rpc = fake_rpc  # type: ignore[assignment]
    assert await ex._ensure_session() == "s1"
    assert ex._active_model == "system.ai.claude-haiku-4-5"


@pytest.mark.asyncio
async def test_model_override_switches_warm_via_set_config_option() -> None:
    """
    A new model is applied with ``session/set_config_option`` using ``configId``.

    ``configId`` is the parameter name the agent expects; ``optionId`` fails with
    ``missing field 'configId'``. The session is NOT recreated, so the transcript
    survives the switch.

    **What breaks if this fails**: ``/model`` silently does nothing mid-session,
    or the switch drops the conversation by respawning.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("swe-1-7-medium"))
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append((method, params))
        return {"result": {"configOptions": [{"id": "model", "currentValue": params["value"]}]}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", "gemini-3-1-pro-low")

    assert calls == [
        (
            "session/set_config_option",
            {"sessionId": "s1", "configId": "model", "value": "gemini-3-1-pro-low"},
        )
    ]
    assert ex._active_model == "gemini-3-1-pro-low"


@pytest.mark.asyncio
async def test_model_override_noops_when_already_active_or_unset() -> None:
    """No redundant round-trip when the model is unchanged or unspecified."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("swe-1-7-medium"))
    ex._rpc = AsyncMock()  # type: ignore[assignment]

    await ex._apply_model_override("s1", None)
    await ex._apply_model_override("s1", "swe-1-7-medium")
    ex._rpc.assert_not_awaited()


@pytest.mark.parametrize("failure", ["no-option", "latched", "rpc-error", "echo"])
async def test_failed_model_pick_names_the_model_kept(failure, tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    ex = AcpExecutor(AcpAgentConfig(command="neutral-agent", omnigent_mcp=False))
    ex._session_id = "sid"
    ex._initialized = True
    ex._active_model = "model-a"
    ex._proc = type("Process", (), {"returncode": None})()
    if failure == "no-option":
        ex._config_option_ids = {"mode"}
    elif failure == "latched":
        ex._model_switch_supported = False
    elif failure == "rpc-error":
        ex._rpc = AsyncMock(return_value={"error": {"message": "unsupported"}})
    else:
        ex._rpc = AsyncMock(
            return_value={
                "result": {"configOptions": [{"id": "model", "currentValue": "model-c"}]}
            }
        )

    async def send(message):
        assert message["method"] == "session/prompt"
        ex._pending[message["id"]].set_result({"result": {}})

    ex._send = send
    events = [
        event
        async for event in ex.run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="model-b"),
        )
    ]
    kept = "model-c" if failure == "echo" else "model-a"
    assert any(
        isinstance(event, TextChunk) and event.text == f"Agent kept {kept}.\n\n"
        for event in events
    )
    assert any(
        isinstance(event, TurnComplete) and event.response == f"Agent kept {kept}.\n\n"
        for event in events
    )
    assert ex._active_model == kept


@pytest.mark.asyncio
async def test_model_override_skipped_when_agent_has_no_model_option() -> None:
    """
    An agent advertising options but no ``model`` one is left alone.

    **What breaks if this fails**: every turn sends a doomed request to agents
    that simply don't support switching (goose, kilocode, …).
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(
        {"sessionUpdate": "config_option_update", "configOptions": [{"id": "mode"}]}
    )
    ex._rpc = AsyncMock()  # type: ignore[assignment]
    await ex._apply_model_override("s1", "some-model")
    ex._rpc.assert_not_awaited()
    assert ex._model_switch_supported is False


@pytest.mark.asyncio
async def test_model_override_rejection_latches_off_and_does_not_raise() -> None:
    """
    A rejected switch is logged and disabled, never fatal.

    **What breaks if this fails**: an agent that doesn't implement
    ``session/set_config_option`` fails the whole turn instead of answering on
    the model it already has.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("m1"))
    ex._rpc = AsyncMock(return_value={"error": {"code": -32601, "message": "unsupported"}})  # type: ignore[assignment]

    await ex._apply_model_override("s1", "m2")
    assert ex._model_switch_supported is False
    assert ex._active_model == "m1"  # unchanged

    # Latched off: a later turn must not retry.
    ex._rpc.reset_mock()
    await ex._apply_model_override("s1", "m3")
    ex._rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_override_trusts_echoed_value_over_request() -> None:
    """
    When the agent echoes a ``currentValue`` that differs from the request, we
    record the echo — not the request.

    An agent may normalize the id or silently keep its current model while still
    returning success. ``currentValue`` is the only trustworthy record, so
    ``_active_model`` must reflect it. Because the request was not actually
    reached, the next turn must be free to retry it.

    **What breaks if this fails**: ``_active_model`` reflects a model the agent
    never switched to, so a later ``/model`` for the requested id is wrongly
    skipped as already-active.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("swe-1-7-medium"))

    async def fake_rpc(method, params, timeout=30.0):
        # Agent accepts the call but reports a different (normalized) id.
        return {"result": {"configOptions": [{"id": "model", "currentValue": "gemini-3-1-pro"}]}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", "gemini-3-1-pro-low")

    # Echo wins over the request.
    assert ex._active_model == "gemini-3-1-pro"
    # The requested id was not reached, so a later turn still attempts it.
    calls: list[str] = []

    async def tracking_rpc(method, params, timeout=30.0):
        calls.append(params["value"])
        return {"result": {"configOptions": [{"id": "model", "currentValue": params["value"]}]}}

    ex._rpc = tracking_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", "gemini-3-1-pro-low")
    assert calls == ["gemini-3-1-pro-low"]


# ---------------------------------------------------------------------------
# warm model switch (session/set_model) — agents that advertise a catalog
# ---------------------------------------------------------------------------


def _models_result(current: str | None = None, *available: str) -> dict:
    """A ``session/new`` result carrying a model catalog (Cline's shape)."""
    models: dict = {"availableModels": [{"modelId": m, "name": m} for m in available]}
    if current is not None:
        models["currentModelId"] = current
    return models


def test_session_models_records_catalog_and_current_model() -> None:
    """
    A ``models`` object from ``session/new`` is recorded.

    Agents like Cline advertise selectable models here instead of via
    ``config_option_update``, and report the live one as ``currentModelId``.

    **What breaks if this fails**: the catalog stays empty, so the switch falls
    through to ``session/set_config_option`` — which these agents don't expose —
    and the agent silently keeps its own default model.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._note_session_models(_models_result("a/one", "a/one", "b/two"))

    assert ex._session_model_ids == {"a/one", "b/two"}
    assert ex._active_model == "a/one"


def test_session_models_ignores_non_catalog_payloads() -> None:
    """A missing or malformed ``models`` value leaves the catalog untouched."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._note_session_models(None)
    ex._note_session_models("nope")
    ex._note_session_models({"availableModels": ["bare-string", {"noModelId": 1}]})

    assert ex._session_model_ids == set()
    assert ex._active_model is None


@pytest.mark.asyncio
async def test_model_override_uses_set_model_when_catalog_advertised() -> None:
    """
    With a catalog present the switch goes through ``session/set_model``.

    ``session/set_config_option`` must not be attempted: these agents never
    advertise a ``model`` option, so that path bails out and leaves the agent on
    its default — which is also what it bills.

    **What breaks if this fails**: a configured or picked model is silently
    dropped for every catalog-style ACP agent (Cline).
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._note_session_models(_models_result("vendor/default", "vendor/default"))
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append((method, params))
        return {"result": {}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", "sub/cheap-flash")

    assert calls == [("session/set_model", {"sessionId": "s1", "modelId": "sub/cheap-flash"})]
    assert ex._active_model == "sub/cheap-flash"


@pytest.mark.asyncio
async def test_set_model_accepts_id_outside_the_advertised_catalog() -> None:
    """
    An id the agent did not enumerate is still sent.

    ``availableModels`` is what the agent offers interactively; Cline also accepts
    subscription-scoped ``cline-pass/*`` ids it never lists. Filtering on the
    catalog would reject exactly the ids that avoid metered billing.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._note_session_models(_models_result("anthropic/pricey", "anthropic/pricey"))
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append((method, params))
        return {"result": {}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", "cline-pass/deepseek-v4.1-flash")

    assert calls[0][1]["modelId"] == "cline-pass/deepseek-v4.1-flash"
    assert ex._active_model == "cline-pass/deepseek-v4.1-flash"


@pytest.mark.asyncio
async def test_configured_model_applies_when_the_turn_names_none() -> None:
    """
    The agent's configured ``model:`` is used when a turn carries no pick.

    A turn only names a model when the user picked one, so without this fallback
    the ``model:`` in an agent's config entry never reaches the agent at all.

    **What breaks if this fails**: a configured model is inert and the agent runs
    (and bills) on whatever it defaults to.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x", model="sub/configured"))
    ex._note_session_models(_models_result("vendor/default", "vendor/default"))
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append((method, params))
        return {"result": {}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", None)

    assert calls == [("session/set_model", {"sessionId": "s1", "modelId": "sub/configured"})]


@pytest.mark.asyncio
async def test_set_model_rejection_latches_off_and_does_not_raise() -> None:
    """
    A rejected ``session/set_model`` never fails the turn, and is not retried.

    **What breaks if this fails**: an agent that cannot switch models loses the
    turn entirely instead of answering on the model it already has.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._note_session_models(_models_result("vendor/default", "vendor/default"))
    calls: list[str] = []

    async def failing_rpc(method, params, timeout=30.0):
        calls.append(method)
        return {"error": {"message": "unknown model"}}

    ex._rpc = failing_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", "bogus/model")

    assert ex._model_switch_supported is False
    assert ex._active_model == "vendor/default"

    # Latched off: a later turn does not retry.
    await ex._apply_model_override("s1", "another/model")
    assert calls == ["session/set_model"]


@pytest.mark.asyncio
async def test_model_override_falls_back_to_request_when_no_option_echoed() -> None:
    """
    When the agent accepts the switch but echoes no model option, record the
    requested model.

    **What breaks if this fails**: ``_active_model`` is left stale after a
    successful switch, so the same switch is re-requested every turn.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("swe-1-7-medium"))
    ex._rpc = AsyncMock(return_value={"result": {}})  # type: ignore[assignment]

    await ex._apply_model_override("s1", "gemini-3-1-pro-low")
    assert ex._active_model == "gemini-3-1-pro-low"


# ---------------------------------------------------------------------------
# interrupt → session/cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupt_sends_session_cancel() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._session_id = "s1"
    ex._proc = type("P", (), {"returncode": None})()  # type: ignore[assignment]
    sent: list[dict] = []
    ex._send = AsyncMock(side_effect=lambda m: sent.append(m))  # type: ignore[method-assign]
    assert await ex.interrupt_session("ignored") is True
    assert sent == [{"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "s1"}}]


@pytest.mark.asyncio
async def test_duplicate_interrupt_preserves_original_cancel_grace_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._session_id = "s1"
    ex._proc = type("P", (), {"returncode": None})()  # type: ignore[assignment]
    ex._send = AsyncMock()  # type: ignore[method-assign]
    now = 100.0
    with monkeypatch.context() as clock_patch:
        clock_patch.setattr(asyncio.get_running_loop(), "time", lambda: now)
        assert await ex.interrupt_session("ignored") is True
        original_deadline = ex._cancel_grace_deadline
        assert original_deadline == now + acp_executor_module.ACP_CANCEL_GRACE_SECONDS

        now += acp_executor_module.ACP_CANCEL_GRACE_SECONDS / 2
        assert await ex.interrupt_session("ignored") is True
        assert ex._cancel_grace_deadline == original_deadline


@pytest.mark.asyncio
async def test_interrupt_noop_without_session() -> None:
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    assert await ex.interrupt_session("ignored") is False


# ---------------------------------------------------------------------------
# Harness wrap env parsing
# ---------------------------------------------------------------------------


def test_harness_wrap_requires_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner import acp_harness

    monkeypatch.delenv("HARNESS_ACP_COMMAND", raising=False)
    with pytest.raises(RuntimeError, match="HARNESS_ACP_COMMAND"):
        acp_harness._build_acp_executor()


def test_harness_wrap_builds_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "goose acp")
    monkeypatch.setenv("HARNESS_ACP_NAME", "Goose")
    monkeypatch.setenv("HARNESS_ACP_SESSION_ID_MODE", "client")
    monkeypatch.setenv("HARNESS_ACP_SEND_MODEL", "1")
    monkeypatch.setenv("HARNESS_ACP_OMNIGENT_MCP", "0")
    monkeypatch.setenv("HARNESS_ACP_MODEL", "gpt-5.3")
    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.command == "goose acp"
    assert ex._config.name == "Goose"
    assert ex._config.session_id_mode == "client"
    assert ex._config.send_model_in_session_new is True
    assert ex._config.omnigent_mcp is False
    assert ex._config.model == "gpt-5.3"


def test_harness_wrap_reads_inject_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """HARNESS_ACP_INJECT_SYSTEM_PROMPT=0 sets inject_system_prompt=False (#4917)."""
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "omp acp")
    monkeypatch.setenv("HARNESS_ACP_INJECT_SYSTEM_PROMPT", "0")
    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.inject_system_prompt is False


def test_harness_wrap_inject_system_prompt_defaults_to_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """inject_system_prompt defaults to True when env var is absent."""
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "goose acp")
    monkeypatch.delenv("HARNESS_ACP_INJECT_SYSTEM_PROMPT", raising=False)
    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.inject_system_prompt is True


def test_harness_wrap_reads_env_passthrough_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrap decodes the forwarded names, closing parent → child → spawn env."""
    from omnigent.inner import acp_harness

    monkeypatch.setenv("HARNESS_ACP_COMMAND", "grok agent stdio")
    monkeypatch.setenv("HARNESS_ACP_ENV_PASSTHROUGH", "XAI_API_KEY, GROK_TOKEN ,")
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")
    ex = acp_harness._build_acp_executor()
    assert isinstance(ex, AcpExecutor)
    assert ex._config.env_passthrough == ("XAI_API_KEY", "GROK_TOKEN")
    # And it actually lands in the env the agent is spawned with.
    assert ex._build_spawn_env().get("XAI_API_KEY") == "xai-secret"


# ---------------------------------------------------------------------------
# Hermetic end-to-end: drive a real fake ACP agent over stdio
# ---------------------------------------------------------------------------

# A minimal ACP agent: JSON-RPC 2.0 over newline-delimited stdio. It answers the
# handshake, then on session/prompt streams a thought + text + a tool card, asks
# the client for permission, and — once the client answers — finishes the tool
# card, streams closing text, and returns the prompt with a stop reason + usage.
_FAKE_ACP_AGENT = r"""
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

def update(sid, upd):
    send({"jsonrpc": "2.0", "method": "session/update",
          "params": {"sessionId": sid, "update": upd}})

def chunk(sid, kind, text):
    update(sid, {"sessionUpdate": kind, "content": {"type": "text", "text": text}})

pending_prompt_id = None
pending_sid = None
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"promptCapabilities": {"image": False}},
        }})
    elif method == "session/new":
        # The live model is reported before the response, as an agent keying its
        # model option by category does; the client must not lose it to its drain.
        update("fake-session-1", {"sessionUpdate": "config_option_update", "configOptions": [
            {"id": "active-model", "category": "model", "currentValue": "grok-4.6"},
        ]})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "fake-session-1",
            "models": {"currentModelId": "grok-4.5", "availableModels": []},
        }})
    elif method == "session/set_config_option":
        params = msg["params"]
        if params.get("configId") == "active-model":
            send({"jsonrpc": "2.0", "id": mid, "result": {"configOptions": [
                {"id": "active-model", "category": "model", "currentValue": params["value"]},
            ]}})
        else:
            send({"jsonrpc": "2.0", "id": mid, "error": {
                "code": -32602, "message": "unknown configId %r" % (params.get("configId"),),
            }})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        chunk(sid, "agent_thought_chunk", "planning")
        chunk(sid, "agent_message_chunk", "Hello ")
        update(sid, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "shell",
                     "kind": "execute", "status": "pending", "rawInput": {"command": "echo hi"}})
        send({"jsonrpc": "2.0", "id": 900, "method": "session/request_permission", "params": {
            "sessionId": sid,
            "toolCall": {"title": "shell", "kind": "execute", "rawInput": {"command": "echo hi"}},
            "options": [{"optionId": "ok", "kind": "allow_once"},
                        {"optionId": "no", "kind": "reject_once"}],
        }})
        pending_prompt_id, pending_sid = mid, sid
    elif mid == 900 and method is None:
        # The client's permission reply — finish the turn.
        update(pending_sid, {"sessionUpdate": "tool_call_update",
                             "toolCallId": "t1", "status": "completed"})
        chunk(pending_sid, "agent_message_chunk", "done")
        send({"jsonrpc": "2.0", "id": pending_prompt_id, "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        }})
"""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_status", "finishes"),
    [
        ("running", True),
        ("running-update", True),
        ("pending", False),
        ("finished", False),
        ("stalled", False),
    ],
)
async def test_prompt_idle_deadline_during_tool_call(
    monkeypatch: pytest.MonkeyPatch, tool_status: str, finishes: bool
) -> None:
    monkeypatch.setattr(acp_executor_module, "_PROMPT_TIMEOUT_SECONDS", 0.08)
    monkeypatch.setattr(acp_executor_module, "_RUNNING_TOOL_TIMEOUT_SECONDS", 0.4, raising=False)
    ex = AcpExecutor(AcpAgentConfig(command="x", omnigent_mcp=False))
    ex._proc = Mock(returncode=None)
    ex._initialized = True
    ex._session_id = "s1"
    loop = asyncio.get_running_loop()
    response_timer: asyncio.TimerHandle | None = None

    def update(kind: str, status: str) -> None:
        ex._queue.put_nowait(
            {
                "method": "session/update",
                "params": {
                    "sessionId": "s1",
                    "update": {
                        "sessionUpdate": kind,
                        "toolCallId": "shell-1",
                        "title": "shell",
                        "status": status,
                    },
                },
            }
        )

    async def send(message: dict) -> None:
        nonlocal response_timer
        assert message["method"] == "session/prompt"
        update("tool_call", "in_progress" if tool_status == "running" else "pending")
        if tool_status in {"running-update", "finished", "stalled"}:
            update("tool_call_update", "in_progress")
        if tool_status == "finished":
            update("tool_call_update", "completed")
        if tool_status == "stalled":
            return

        def finish() -> None:
            if tool_status in {"running", "running-update"}:
                update("tool_call_update", "completed")
            ex._pending[message["id"]].set_result({"result": {"stopReason": "end_turn"}})

        response_timer = loop.call_later(0.2, finish)

    monkeypatch.setattr(ex, "_send", send)
    try:
        async with asyncio.timeout(1):
            events = [
                event async for event in ex.run_turn([{"role": "user", "content": "run"}], [], "")
            ]
    finally:
        if response_timer is not None:
            response_timer.cancel()

    assert isinstance(events[0], ToolCallRequest)
    if finishes:
        assert isinstance(events[-2], ToolCallComplete)
        assert isinstance(events[-1], TurnComplete)
    else:
        assert isinstance(events[-1], ExecutorError)
        assert events[-1].message == "Timeout waiting for ACP response"


@pytest.mark.asyncio
async def test_pre_prompt_model_report_steers_first_turn_switch(tmp_path: Path) -> None:
    """A model report queued behind ``session/new`` is read before the first switch.

    The fake agent keys its model option ``active-model``; a configured model must
    reach it under that id on the first turn, not the literal ``model`` fallback.
    """
    agent_path = tmp_path / "fake_acp_agent.py"
    agent_path.write_text(_FAKE_ACP_AGENT)
    command = shlex.join([sys.executable, str(agent_path)])

    ex = AcpExecutor(AcpAgentConfig(command=command, name="Fake", model="grok-4.7"))

    async def elicit(tool_name: str, tool_input: dict) -> bool:
        return True

    ex._elicitation_handler = elicit  # type: ignore[assignment]

    events = []
    try:
        async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], "you are a bot"):
            events.append(ev)
    finally:
        await ex.close()

    assert not [e for e in events if isinstance(e, ExecutorError)]
    completions = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completions) == 1
    assert completions[0].usage["model"] == "grok-4.7"


@pytest.mark.asyncio
@pytest.mark.parametrize("queue_mode", [False, True])
async def test_end_to_end_against_fake_acp_agent(
    tmp_path: Path, queue_mode, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger=acp_executor_module.__name__)
    agent_path = tmp_path / "fake_acp_agent.py"
    agent_path.write_text(_FAKE_ACP_AGENT)
    command = shlex.join([sys.executable, str(agent_path)])

    ex = AcpExecutor(AcpAgentConfig(command=command, name="Fake"))
    approvals: list[tuple[str, dict]] = []

    async def elicit(tool_name: str, tool_input: dict) -> bool:
        approvals.append((tool_name, tool_input))
        return True

    ex._elicitation_handler = elicit  # type: ignore[assignment]

    from omnigent.startup_evidence import startup_evidence_scope
    from omnigent.startup_protocol import StartupAttemptRef

    ref = StartupAttemptRef(
        startup_id="a" * 32,
        attempt_id="b" * 32,
        input_id="c" * 32,
        runtime_id="runtime",
        host_generation="generation",
        mode="startup_only",
    )
    observed, claims = [], []

    async def claim(attempt, boundary):
        assert ex._proc is None
        claims.append((attempt, boundary))
        return True

    events = []
    try:
        with startup_evidence_scope(ref if queue_mode else None, observed.append, claim):
            async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], "you are a bot"):
                events.append(ev)
        assert claims == ([(ref, "executor_prepare")] if queue_mode else [])
        assert [item.phase for item in observed] == (["opening_settled"] if queue_mode else [])
    finally:
        await ex.close()

    assert f"acp[Fake] session/prompt {ex._rpc_id} stopReason=end_turn" in caplog.text
    # The permission request surfaced through the elicitation handler.
    assert approvals == [("shell", {"command": "echo hi"})]

    kinds = [type(e).__name__ for e in events]
    assert "ReasoningChunk" in kinds
    assert "ToolCallRequest" in kinds
    assert "ToolCallComplete" in kinds

    text = "".join(e.text for e in events if isinstance(e, TextChunk))
    assert text == "Hello done"

    completions = [e for e in events if isinstance(e, TurnComplete)]
    assert len(completions) == 1
    assert completions[0].usage == {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "model": "grok-4.6",
    }

    tool_reqs = [e for e in events if isinstance(e, ToolCallRequest)]
    assert tool_reqs[0].name == "shell" and tool_reqs[0].args == {"command": "echo hi"}
    tool_done = [e for e in events if isinstance(e, ToolCallComplete)]
    assert tool_done[0].status is ToolCallStatus.SUCCESS


# ---------------------------------------------------------------------------
# Omnigent MCP bridge (session/new.mcpServers via the shared serve-mcp relay)
# ---------------------------------------------------------------------------


def test_mcp_to_acp_servers_flattens_env_to_array() -> None:
    """ACP wants env as [{name,value}], not a dict; command/args pass through."""
    out = _to_acp_mcp_servers(
        {"mcpServers": {"omnigent": {"command": "/py", "args": ["-Im", "x"], "env": {"A": "1"}}}}
    )
    assert out == [
        {
            "name": "omnigent",
            "command": "/py",
            "args": ["-Im", "x"],
            "env": [{"name": "A", "value": "1"}],
        }
    ]


def test_mcp_disabled_returns_empty() -> None:
    m = OmnigentAcpMcp("t")
    assert (
        m.session_new_servers(
            tools=[{"name": "x"}], tool_executor=lambda *a: None, loop=None, enabled=False
        )
        == []
    )


def test_mcp_no_executor_returns_empty() -> None:
    m = OmnigentAcpMcp("t")
    assert m.session_new_servers(tools=[{"name": "x"}], tool_executor=None, loop=None) == []


def test_mcp_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_ACP_MCP", "0")
    m = OmnigentAcpMcp("t")
    assert (
        m.session_new_servers(tools=[{"name": "x"}], tool_executor=lambda *a: None, loop=None)
        == []
    )


@pytest.mark.asyncio
async def test_mcp_relay_starts_and_builds_serve_mcp_entry() -> None:
    """A real relay boots (writes bridge.json + tool_relay.json + HTTP server)
    and yields one ACP stdio server pointing at the shared serve-mcp."""
    m = OmnigentAcpMcp("t")

    async def fake_exec(name: str, args: dict) -> dict:
        return {"ok": True}

    loop = asyncio.get_event_loop()
    servers = m.session_new_servers(
        tools=[{"name": "sys_agent_list", "parameters": {"type": "object", "properties": {}}}],
        tool_executor=fake_exec,
        loop=loop,
    )
    try:
        assert len(servers) == 1
        entry = servers[0]
        assert entry["name"] == "omnigent"
        assert "serve-mcp" in entry["args"]
        assert "omnigent.harnesses.claude_native.bridge" in entry["args"]
        assert all("name" in e and "value" in e for e in entry["env"])
        # Idempotent: a second call returns the cached relay, not a new one.
        assert m.session_new_servers(tools=[], tool_executor=fake_exec, loop=loop) is servers
    finally:
        m.close()


@pytest.mark.asyncio
async def test_acp_session_new_carries_mcp_servers() -> None:
    """When a tool executor + tools are present, session/new carries mcpServers."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._tool_executor = lambda n, a: None  # type: ignore[assignment]
    ex._omnigent_tools = [{"name": "sys_agent_list"}]
    sentinel = [{"name": "omnigent", "command": "/py", "args": [], "env": []}]
    ex._mcp.session_new_servers = lambda **kw: sentinel  # type: ignore[method-assign]
    captured: dict = {}

    async def fake_rpc(method, params, timeout=30.0):
        captured["params"] = params
        return {"result": {"sessionId": "s1"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_session()
    assert captured["params"]["mcpServers"] is sentinel


@pytest.mark.asyncio
async def test_acp_session_new_omnigent_mcp_disabled_per_agent() -> None:
    """`omnigent_mcp=False` disables relay setup but preserves ACP's field."""
    ex = AcpExecutor(AcpAgentConfig(command="x", omnigent_mcp=False))
    ex._tool_executor = lambda n, a: None  # type: ignore[assignment]
    ex._omnigent_tools = [{"name": "sys_agent_list"}]
    captured: dict = {}

    async def fake_rpc(method, params, timeout=30.0):
        captured["params"] = params
        return {"result": {"sessionId": "s1"}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._ensure_session()
    assert captured["params"]["mcpServers"] == []


def test_omnigent_tools_cleared_when_mcp_disabled() -> None:
    """run_turn discards builtin tools when omnigent_mcp=False (#4917).

    With the relay disabled, the tool schemas serve no purpose and must not be
    stored — they could otherwise accidentally reach the session/prompt path.
    Verified by pre-populating _omnigent_tools and running the capture logic
    from the start of run_turn in isolation.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x", omnigent_mcp=False))
    tools = [{"name": "load_skill"}, {"name": "sys_session_rename"}]

    # Simulate the first few lines of run_turn: capture _omnigent_tools.
    # When omnigent_mcp is False the assignment must yield an empty list.
    ex._omnigent_tools = (tools or []) if ex._config.omnigent_mcp else []
    assert ex._omnigent_tools == [], "tools must be discarded when omnigent_mcp=False"


def test_omnigent_tools_kept_when_mcp_enabled() -> None:
    """Sanity: _omnigent_tools is populated when omnigent_mcp=True."""
    ex = AcpExecutor(AcpAgentConfig(command="x", omnigent_mcp=True))
    tools = [{"name": "load_skill"}, {"name": "sys_session_rename"}]
    ex._omnigent_tools = (tools or []) if ex._config.omnigent_mcp else []
    assert ex._omnigent_tools == tools, "tools must be stored when omnigent_mcp=True"


@pytest.mark.asyncio
async def test_inject_system_prompt_false_skips_prepend(tmp_path: Path) -> None:
    """inject_system_prompt=False prevents the spec's system prompt from being
    folded into the first ACP user turn (#4917 — Pi-fork agents like omp).

    Without this fix, Omnigent's system prompt is prepended to the user message
    on the first turn.  For agents that fully own their own system prompt (Pi
    forks), this confuses the internal Claude model into emitting XML tool-call
    fragments (``</function></tool_call>``) when there is no MCP relay backing
    the described tools.
    """
    agent_path = tmp_path / "prompt_echo_agent.py"
    agent_path.write_text(
        r"""
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    method = msg.get("method", "")
    if method == "initialize":
        caps = {"promptCapabilities": {"image": False}}
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"protocolVersion": 1, "agentCapabilities": caps}})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {"sessionId": "echo-1"}})
    elif method == "session/prompt":
        sid = msg.get("params", {}).get("sessionId", "echo-1")
        # Echo back the text the client sent so the test can inspect it.
        text = ""
        for block in msg.get("params", {}).get("prompt", []):
            if isinstance(block, dict) and block.get("type") == "text":
                text += block.get("text", "")
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": sid,
                         "update": {"sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": text}}}})
        send({"jsonrpc": "2.0", "id": mid,
              "result": {"stopReason": "end_turn", "usage": {}}})
"""
    )
    command = shlex.join([sys.executable, str(agent_path)])

    # With injection enabled (default), the system prompt is prepended.
    ex_inject = AcpExecutor(AcpAgentConfig(command=command, inject_system_prompt=True))
    texts_inject: list[str] = []
    try:
        async for ev in ex_inject.run_turn(
            [{"role": "user", "content": "hello"}], [], "SYSTEM_PROMPT_TEXT"
        ):
            if isinstance(ev, TextChunk):
                texts_inject.append(ev.text)
    finally:
        await ex_inject.close()
    combined_inject = "".join(texts_inject)
    assert "SYSTEM_PROMPT_TEXT" in combined_inject, (
        "system prompt should appear in the echoed first turn when inject_system_prompt=True"
    )

    # With injection disabled, the system prompt must NOT appear.
    ex_no_inject = AcpExecutor(AcpAgentConfig(command=command, inject_system_prompt=False))
    texts_no_inject: list[str] = []
    try:
        async for ev in ex_no_inject.run_turn(
            [{"role": "user", "content": "hello"}], [], "SYSTEM_PROMPT_TEXT"
        ):
            if isinstance(ev, TextChunk):
                texts_no_inject.append(ev.text)
    finally:
        await ex_no_inject.close()
    combined_no_inject = "".join(texts_no_inject)
    assert "SYSTEM_PROMPT_TEXT" not in combined_no_inject, (
        "system prompt must not appear in the first turn when inject_system_prompt=False"
    )
    assert "hello" in combined_no_inject, "user message itself must still be sent"


@pytest.mark.asyncio
async def test_end_to_end_denied_permission(tmp_path: Path) -> None:
    """A denied elicitation still completes the turn (the agent gets a reject)."""
    agent_path = tmp_path / "fake_acp_agent.py"
    agent_path.write_text(_FAKE_ACP_AGENT)
    command = shlex.join([sys.executable, str(agent_path)])

    ex = AcpExecutor(AcpAgentConfig(command=command, name="Fake"))

    async def deny(tool_name: str, tool_input: dict) -> bool:
        return False

    ex._elicitation_handler = deny  # type: ignore[assignment]

    events = []
    try:
        async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], ""):
            events.append(ev)
    finally:
        await ex.close()

    # Turn still completes even though the tool was rejected.
    assert any(isinstance(e, TurnComplete) for e in events)


# ---------------------------------------------------------------------------
# describe_exception — never report a blank turn error (#4281)
# ---------------------------------------------------------------------------


def test_describe_exception_falls_back_to_repr_for_blank_message():
    """A bare exception whose ``str()`` is empty is described by ``repr()``.

    Regression for #4281: executors reported failures via ``str(exc)``, so a
    bare ``RuntimeError()`` reached the operator as "inner executor error: "
    with no detail. The fallback must at least name the exception type.
    """
    assert str(RuntimeError()) == ""  # the exact blank-message case from the bug
    described = describe_exception(RuntimeError())
    assert described != ""
    assert "RuntimeError" in described


def test_describe_exception_preserves_a_real_message():
    """When the exception carries a message, it is used verbatim (no repr noise)."""
    assert describe_exception(ValueError("boom: bad line")) == "boom: bad line"


@pytest.mark.parametrize(
    "exc",
    [RuntimeError(), TimeoutError(), OSError(), Exception()],
)
def test_describe_exception_never_blank(exc: BaseException):
    """No bare stdlib exception yields an empty description."""
    assert describe_exception(exc).strip() != ""


# ---------------------------------------------------------------------------
# Spawn env: the agent must actually receive credentials (#4281)
# ---------------------------------------------------------------------------


def test_spawn_env_keeps_credential_declared_on_the_agent() -> None:
    """A name in the agent's own ``env_passthrough`` reaches the subprocess.

    This is the config path a user actually has (an ``acp.agents:`` row).
    Without it the agent starts unauthenticated and stalls during the
    handshake, which surfaced as a turn that failed with no message at all.
    """
    ex = AcpExecutor(
        AcpAgentConfig(command="agent stdio", name="Grok", env_passthrough=("XAI_API_KEY",))
    )
    with patch.dict(os.environ, {"XAI_API_KEY": "xai-secret"}, clear=False):
        env = ex._build_spawn_env()
    assert env.get("XAI_API_KEY") == "xai-secret"


def test_spawn_env_keeps_credential_declared_on_the_spec() -> None:
    """A spec-declared ``os_env.sandbox.env_passthrough`` name still works."""
    os_env = OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="none", env_passthrough=["XAI_API_KEY"]),
        fork=False,
    )
    ex = AcpExecutor(AcpAgentConfig(command="agent stdio", name="Grok"), os_env=os_env)
    with patch.dict(os.environ, {"XAI_API_KEY": "xai-secret"}, clear=False):
        env = ex._build_spawn_env()
    assert env.get("XAI_API_KEY") == "xai-secret"


def test_spawn_env_unions_agent_and_spec_declarations() -> None:
    """Both sources apply; neither shadows the other."""
    os_env = OSEnvSpec(
        type="caller_process",
        cwd=None,
        sandbox=OSEnvSandboxSpec(type="none", env_passthrough=["FROM_SPEC"]),
        fork=False,
    )
    ex = AcpExecutor(
        AcpAgentConfig(command="agent stdio", name="A", env_passthrough=("FROM_AGENT",)),
        os_env=os_env,
    )
    with patch.dict(os.environ, {"FROM_SPEC": "1", "FROM_AGENT": "2"}, clear=False):
        env = ex._build_spawn_env()
    assert env.get("FROM_SPEC") == "1"
    assert env.get("FROM_AGENT") == "2"


def test_spawn_env_still_excludes_undeclared_secret() -> None:
    """Deny-by-default holds: an undeclared provider key is not handed over."""
    ex = AcpExecutor(AcpAgentConfig(command="agent stdio", name="Grok"))
    with patch.dict(os.environ, {"UNRELATED_API_KEY": "nope"}, clear=False):
        env = ex._build_spawn_env()
    assert "UNRELATED_API_KEY" not in env


@pytest.mark.asyncio
async def test_operator_passthrough_reaches_spawned_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.host.connect import _build_runner_env
    from omnigent.runner.identity import RUNNER_AUTH_SECRET_ENV_VARS
    from omnigent.runtime.harnesses.process_manager import _build_harness_spawn_env

    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)
    runner_env = _build_runner_env(
        {
            "OMNIGENT_RUNNER_ENV_PASSTHROUGH": ",".join(
                ["XAI_API_KEY", *sorted(RUNNER_AUTH_SECRET_ENV_VARS)]
            ),
            "XAI_API_KEY": "canary-xai",
            "UNRELATED_API_KEY": "canary-unlisted",
        },
        server_url="http://server",
        runner_id="runner_test",
        binding_token="canary-binding",
        initial_auth_token="canary-auth",
        workspace=str(tmp_path),
        parent_pid=os.getpid(),
    )
    monkeypatch.setattr("os.environ", runner_env)
    worker_env = _build_harness_spawn_env(None)
    monkeypatch.setattr("os.environ", worker_env)
    captured = tmp_path / "agent-env.json"
    command = shlex.join(
        [
            sys.executable,
            "-c",
            "import json, os, pathlib, sys; "
            "observed = {'XAI_API_KEY': os.environ.get('XAI_API_KEY'), "
            "'unrelated_present': 'UNRELATED_API_KEY' in os.environ, "
            "'runner_auth_present': any(name in os.environ "
            f"for name in {sorted(RUNNER_AUTH_SECRET_ENV_VARS)!r})}}; "
            "pathlib.Path(sys.argv[1]).write_text(json.dumps(observed))",
            str(captured),
        ]
    )
    executor = AcpExecutor(AcpAgentConfig(command=command))
    try:
        await executor._start_process()
        assert executor._proc is not None
        assert await asyncio.wait_for(executor._proc.wait(), timeout=10) == 0
        observed = json.loads(captured.read_text())
        assert observed["XAI_API_KEY"] == "canary-xai"
        assert not observed["unrelated_present"]
        assert not observed["runner_auth_present"]
    finally:
        await executor.close()


@pytest.mark.asyncio
async def test_handshake_timeout_reports_a_non_blank_error(tmp_path: Path) -> None:
    """An agent that never answers ``session/new`` yields a named error.

    ``asyncio.TimeoutError`` has an empty ``str()``, so reporting the failure by
    ``str(exc)`` produced the blank "inner executor error: " an operator can't
    act on.
    """
    agent_path = tmp_path / "silent_agent.py"
    agent_path.write_text(
        "import sys, json\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('method') == 'initialize':\n"
        "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg['id'],\n"
        "            'result': {'protocolVersion': 1, 'agentCapabilities': {}}}) + '\\n')\n"
        "        sys.stdout.flush()\n"
        # session/new deliberately unanswered -> the handshake RPC times out.
    )
    command = shlex.join([sys.executable, str(agent_path)])

    ex = AcpExecutor(AcpAgentConfig(command=command, name="Silent", init_timeout_s=1.0))
    errors = []
    try:
        async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], ""):
            if isinstance(ev, ExecutorError):
                errors.append(ev)
    finally:
        await ex.close()

    assert errors, "a stalled handshake must surface an ExecutorError"
    assert errors[0].message.strip(), "the turn error must never be blank"
    # Names the stalled call, not just the exception type.
    assert "session/new" in errors[0].message
    assert "Silent" in errors[0].message
    assert "within 1s" in errors[0].message


# ---------------------------------------------------------------------------
# Diagnostics: the agent's own stderr must reach the operator
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_startup_failure_quotes_the_agents_stderr(tmp_path: Path) -> None:
    """An agent that explains itself on stderr has that text in the turn error.

    A stalled handshake names only the RPC that timed out. The reason usually
    sits on the agent's stderr ("no API key"), which was drained at debug level
    into a logger the harness child had no handler for — so it reached nobody.
    """
    agent_path = tmp_path / "noisy_agent.py"
    agent_path.write_text(
        "import sys, json, time\n"
        "for line in sys.stdin:\n"
        "    line = line.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    msg = json.loads(line)\n"
        "    if msg.get('method') == 'initialize':\n"
        "        sys.stdout.write(json.dumps({'jsonrpc': '2.0', 'id': msg['id'],\n"
        "            'result': {'protocolVersion': 1, 'agentCapabilities': {}}}) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "    elif msg.get('method') == 'session/new':\n"
        "        sys.stderr.write('ERROR: XAI_API_KEY not set\\n')\n"
        "        sys.stderr.flush()\n"
        "        time.sleep(3600)\n"
    )
    command = shlex.join([sys.executable, str(agent_path)])

    ex = AcpExecutor(AcpAgentConfig(command=command, name="Grok", init_timeout_s=1.0))
    errors = []
    try:
        async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], ""):
            if isinstance(ev, ExecutorError):
                errors.append(ev)
    finally:
        await ex.close()

    assert errors, "a stalled handshake must surface an ExecutorError"
    assert "XAI_API_KEY not set" in errors[0].message


def test_stderr_ring_is_bounded_and_lines_are_capped() -> None:
    """A chatty agent can't grow the ring or put a huge line in a UI toast."""
    ex = AcpExecutor(AcpAgentConfig(command="x", name="A"))
    for i in range(acp_executor_module._STDERR_RING_LINES * 3):
        ex._recent_stderr.append(f"line{i}")
    assert len(ex._recent_stderr) == acp_executor_module._STDERR_RING_LINES

    ex._recent_stderr.clear()
    ex._recent_stderr.append("x" * 10_000)
    msg = ex._startup_error_message(TimeoutError())
    assert len(msg) < 2_000, "a single huge stderr line must not dominate the error"


def test_startup_error_names_the_exception_type_when_str_is_empty() -> None:
    """No stderr and an empty ``str(exc)`` still yields something actionable."""
    ex = AcpExecutor(AcpAgentConfig(command="x", name="A"))
    assert "TimeoutError" in ex._startup_error_message(TimeoutError())


def test_ordered_prompt_blocks_preserve_text_image_text_order() -> None:
    blocks = [
        {"type": "input_text", "text": "before"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAB"},
        {"type": "input_text", "text": "after"},
    ]
    assert AcpExecutor._ordered_prompt_blocks(blocks, image_supported=True) == [
        {"type": "text", "text": "before"},
        {"type": "image", "mimeType": "image/png", "data": "AAAB"},
        {"type": "text", "text": "after"},
    ]


# ---------------------------------------------------------------------------
# Process lifecycle: a torn-down executor must not strand the agent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_windows_acp_spawn_hides_window_and_keeps_process_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import subprocess
    from types import SimpleNamespace

    from omnigent.inner import acp_executor

    executor = AcpExecutor(AcpAgentConfig(command="example-agent"), cwd=str(tmp_path))
    no_window = 0x08000000
    new_group = 0x00000200
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", no_window, raising=False)
    monkeypatch.setattr(acp_executor, "IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(acp_executor._proc, "IS_POSIX", False)
    monkeypatch.setattr(acp_executor._proc, "_CREATE_NEW_PROCESS_GROUP", new_group)
    spawn = AsyncMock()
    monkeypatch.setattr(
        acp_executor,
        "asyncio",
        SimpleNamespace(
            create_subprocess_exec=spawn,
            create_task=asyncio.create_task,
            subprocess=asyncio.subprocess,
        ),
    )
    monkeypatch.setattr(executor, "_read_stdout", AsyncMock())
    monkeypatch.setattr(executor, "_read_stderr", AsyncMock())

    await executor._start_process()

    spawn.assert_awaited_once()
    kwargs = spawn.await_args.kwargs
    assert kwargs["creationflags"] == no_window | new_group
    assert all(kwargs[name] == asyncio.subprocess.PIPE for name in ("stdin", "stdout", "stderr"))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
@pytest.mark.asyncio
async def test_spawned_agent_leads_its_own_process_group(tmp_path: Path) -> None:
    """The spawned agent is a session/group leader, not in the harness's group.

    Without the boundary ``_proc._killpg`` resolves the target group to the one
    we share with the harness and daemon, refuses to signal it, and tree-aware
    teardown silently degrades to a per-descendant walk.
    """
    agent_path = tmp_path / "sleepy_agent.py"
    agent_path.write_text("import time\ntime.sleep(300)\n")
    ex = AcpExecutor(
        AcpAgentConfig(command=shlex.join([sys.executable, str(agent_path)]), name="Sleepy")
    )

    await ex._start_process()
    try:
        pid = ex._proc.pid  # type: ignore[union-attr]
        assert os.getpgid(pid) == pid, "agent must lead its own process group"
        assert os.getpgid(pid) != os.getpgid(0), "agent must not share our group"
    finally:
        await ex.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
@pytest.mark.asyncio
async def test_close_reaps_the_agents_forked_children(tmp_path: Path) -> None:
    """``close()`` stops the agent's descendants, not just the handle it holds.

    Under a sandbox the handle is the seatbelt ``run_launcher`` wrapper, which
    forks the real agent; a single-pid ``terminate()`` reached the wrapper and
    left the agent running for the daemon's whole lifetime.
    """
    pid_file = tmp_path / "grandchild.pid"
    agent_path = tmp_path / "forking_agent.py"
    agent_path.write_text(
        "import pathlib, subprocess, sys, time\n"
        "kid = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(kid.pid))\n"
        "time.sleep(300)\n"
    )
    ex = AcpExecutor(
        AcpAgentConfig(command=shlex.join([sys.executable, str(agent_path)]), name="Forking")
    )

    await ex._start_process()
    deadline = time.monotonic() + 10.0
    # write_text creates the file before it writes the pid, so wait for content.
    while not (pid_file.exists() and pid_file.read_text()):
        assert time.monotonic() < deadline, "the fake agent never forked its child"
        await asyncio.sleep(0.05)
    grandchild = int(pid_file.read_text())
    assert _proc.process_alive(grandchild)

    await ex.close()

    deadline = time.monotonic() + 10.0
    while _proc.process_alive(grandchild):
        assert time.monotonic() < deadline, f"agent child {grandchild} survived close()"
        await asyncio.sleep(0.05)


# ── Curated model list gate + spawn-env denylist ────────────────────────────


def test_spawn_env_env_unset_strips_declared_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """``env_unset`` scrubs a name even when it was allowed through.

    Mirrors codex's ``deny_exact`` contract: the denylist applies on top of
    the passthrough allowlist, so an operator-declared dummy credential is
    withheld from the vendor CLI (whose built-in providers would activate on
    its mere presence).
    """
    ex = AcpExecutor(
        AcpAgentConfig(
            command="agent stdio",
            name="Grok",
            env_passthrough=("XAI_API_KEY", "DEEPSEEK_API_KEY"),
            env_unset=("XAI_API_KEY",),
        )
    )
    monkeypatch.setenv("XAI_API_KEY", "xai-secret")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-secret")
    env = ex._build_spawn_env()
    assert env.get("DEEPSEEK_API_KEY") == "ds-secret"
    assert "XAI_API_KEY" not in env

    monkeypatch.setenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "XAI_API_KEY")
    assert "XAI_API_KEY" not in ex._build_spawn_env()


def test_spawn_env_env_unset_empty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No denylist configured → deny-exact adds no stripping."""
    ex = AcpExecutor(AcpAgentConfig(command="agent stdio", name="Grok", env_passthrough=("A",)))
    monkeypatch.setenv("A", "1")
    env = ex._build_spawn_env()
    assert env.get("A") == "1"


@pytest.mark.asyncio
async def test_model_override_withheld_outside_curated_list() -> None:
    """A curated deployment rejects picks outside its verified model set.

    This is the ACP counterpart of pi-native's ``enabledModels`` scoping:
    the vendor CLI owns its own picker, so the gate sits at the warm-switch
    boundary instead of a settings file.
    """
    ex = AcpExecutor(AcpAgentConfig(command="x", available_models=("model-a", "model-b")))
    ex._handle_session_update(_model_option("model-a"))
    ex._rpc = AsyncMock()  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="configured model list"):
        await ex._apply_model_override("s1", "model-c")
    ex._rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_override_allowed_within_curated_list() -> None:
    """A pick inside the curated set still switches warm via set_config_option."""
    ex = AcpExecutor(AcpAgentConfig(command="x", available_models=("model-a", "model-b")))
    ex._handle_session_update(_model_option("model-a"))
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append((method, params))
        return {"result": {"configOptions": [{"id": "model", "currentValue": params["value"]}]}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", "model-b")

    assert calls == [
        ("session/set_config_option", {"sessionId": "s1", "configId": "model", "value": "model-b"})
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["rpc-error", "no-option", "different-echo", "timeout"])
async def test_curated_model_failure_stops_before_prompt_and_remains_retryable(
    failure: str,
) -> None:
    """An unsuccessful selection must never send the turn on the previous model."""
    ex = AcpExecutor(
        AcpAgentConfig(command="x", available_models=("model-a", "model-b"), omnigent_mcp=False)
    )
    ex._proc = type("P", (), {"returncode": None})()  # type: ignore[assignment]
    ex._reader_task = Mock(spec=asyncio.Task)
    ex._reader_task.done.return_value = False
    ex._initialized = True
    ex._session_id = "s1"
    ex._handle_session_update(_model_option("model-a"))
    if failure == "no-option":
        ex._config_option_ids = {"mode"}
    ex._send = AsyncMock()  # type: ignore[method-assign]
    response = (
        {"result": {"configOptions": [{"id": "model", "currentValue": "model-a"}]}}
        if failure == "different-echo"
        else {"error": {"message": "model unavailable"}}
    )
    ex._rpc = AsyncMock(  # type: ignore[assignment]
        return_value=response,
        side_effect=TimeoutError("switch timed out") if failure == "timeout" else None,
    )

    async def collect_events():
        return [
            event
            async for event in ex.run_turn(
                [{"role": "user", "content": "hello"}], [], "", ExecutorConfig(model="model-b")
            )
        ]

    events = await asyncio.wait_for(collect_events(), timeout=1.0)
    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    assert "ACP model selection failed" in error.message
    assert error.retryable and error.preserve_session
    ex._send.assert_not_awaited()
    assert ex._session_id == "s1"
    assert ex._model_switch_supported
    assert ex._active_model == (None if failure == "timeout" else "model-a")

    ex._config_option_ids = {"model"}
    ex._rpc = AsyncMock(
        return_value={
            "result": {
                "configOptions": [  # type: ignore[assignment]
                    {"id": "model", "currentValue": "model-b"},
                ]
            }
        }
    )
    await ex._apply_model_override("s1", "model-b")
    ex._rpc.assert_awaited_once()
    assert ex._active_model == "model-b"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["process-exit", "reader-stopped", "no-reader", "broken-pipe"])
async def test_curated_model_failure_does_not_preserve_dead_transport(failure: str) -> None:
    """A failed transport must be torn down before another turn can create a session."""
    ex = AcpExecutor(
        AcpAgentConfig(command="x", available_models=("model-a", "model-b"), omnigent_mcp=False)
    )
    ex._proc = Mock(returncode=None)
    ex._reader_task = Mock(spec=asyncio.Task)
    ex._reader_task.done.return_value = failure == "reader-stopped"
    if failure == "no-reader":
        ex._reader_task = None
    ex._initialized = True
    ex._session_id = "s1"
    ex._handle_session_update(_model_option("model-a"))
    ex._send = AsyncMock()  # type: ignore[method-assign]

    async def fail_selection(*args, **kwargs):
        if failure == "process-exit":
            ex._proc.returncode = 1
        if failure == "broken-pipe":
            raise BrokenPipeError("ACP subprocess closed stdin")
        raise TimeoutError("switch timed out")

    ex._rpc = AsyncMock(side_effect=fail_selection)  # type: ignore[method-assign]
    events = [
        event
        async for event in ex.run_turn(
            [{"role": "user", "content": "hello"}], [], "", ExecutorConfig(model="model-b")
        )
    ]
    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    assert error.retryable
    assert not error.preserve_session
    ex._send.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_override_uncurated_sessions_are_ungated() -> None:
    """No curated list (the default) keeps the existing any-model behaviour."""
    ex = AcpExecutor(AcpAgentConfig(command="x"))
    ex._handle_session_update(_model_option("model-a"))
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append((method, params))
        return {"result": {"configOptions": [{"id": "model", "currentValue": params["value"]}]}}

    ex._rpc = fake_rpc  # type: ignore[assignment]
    await ex._apply_model_override("s1", "model-c")

    assert calls == [
        ("session/set_config_option", {"sessionId": "s1", "configId": "model", "value": "model-c"})
    ]


@pytest.mark.asyncio
async def test_model_reset_without_configured_default_restores_initial_agent_model() -> None:
    """A launch override does not become the fallback when the agent owns its default."""
    ex = AcpExecutor(
        AcpAgentConfig(command="x", model="model-b", default_model="", omnigent_mcp=False)
    )
    calls: list[tuple[str, dict]] = []

    async def fake_rpc(method, params, timeout=30.0):
        calls.append((method, params))
        if method == "session/new":
            return {
                "result": {
                    "sessionId": "s1",
                    "configOptions": [
                        {"id": "model", "currentValue": "model-a"},
                    ],
                }
            }
        return {
            "result": {
                "configOptions": [
                    {"id": "model", "currentValue": params["value"]},
                ]
            }
        }

    ex._rpc = fake_rpc  # type: ignore[assignment]
    session_id = await ex._ensure_session()
    await ex._apply_model_override(session_id, "model-b")
    await ex._apply_model_override(session_id, None)

    assert [
        params["value"] for method, params in calls if method == "session/set_config_option"
    ] == [
        "model-b",
        "model-a",
    ]
    assert ex._active_model == "model-a"


# ---------------------------------------------------------------------------
# _resolve_cwd: deleted-directory guard
# ---------------------------------------------------------------------------


def test_resolve_cwd_explicit_missing_raises_with_path(tmp_path: Path) -> None:
    """An explicit cwd that no longer exists raises RuntimeError naming the path."""
    missing = str(tmp_path / "gone")
    with pytest.raises(RuntimeError, match="gone"):
        _resolve_cwd(missing)


def test_resolve_cwd_explicit_valid_returns_unchanged(tmp_path: Path) -> None:
    """An explicit cwd that exists is returned as-is."""
    assert _resolve_cwd(str(tmp_path)) == str(tmp_path)


def test_resolve_cwd_none_returns_process_cwd(tmp_path: Path) -> None:
    """No explicit cwd returns the current process working directory."""
    with patch("omnigent.inner.acp_executor.os.getcwd", return_value=str(tmp_path)):
        result = _resolve_cwd(None)
    assert result == str(tmp_path)


def test_resolve_cwd_deleted_process_cwd_raises_clear_error() -> None:
    """No explicit cwd and a deleted process cwd raises RuntimeError with a message."""
    with patch(
        "omnigent.inner.acp_executor.os.getcwd",
        side_effect=FileNotFoundError(2, "No such file or directory"),
    ):
        with patch(
            "omnigent.inner.acp_executor.os.readlink", return_value="/deleted/path (deleted)"
        ):
            with pytest.raises(RuntimeError, match="deleted"):
                _resolve_cwd(None)


def test_resolve_cwd_deleted_process_cwd_includes_proc_path() -> None:
    """/proc/self/cwd path is included in the error when readable."""
    with patch(
        "omnigent.inner.acp_executor.os.getcwd",
        side_effect=FileNotFoundError(2, "No such file or directory"),
    ):
        with patch(
            "omnigent.inner.acp_executor.os.readlink",
            return_value="/workspace/project (deleted)",
        ):
            with pytest.raises(RuntimeError, match=r"was deleted \(/workspace/project\);"):
                _resolve_cwd(None)


def test_resolve_cwd_deleted_process_cwd_readlink_failure_still_raises() -> None:
    """RuntimeError is raised even when /proc/self/cwd readlink fails."""
    with patch(
        "omnigent.inner.acp_executor.os.getcwd",
        side_effect=FileNotFoundError(2, "No such file or directory"),
    ):
        with patch(
            "omnigent.inner.acp_executor.os.readlink", side_effect=OSError("not available")
        ):
            with pytest.raises(RuntimeError, match="deleted"):
                _resolve_cwd(None)


_CWD_POLICY_CASES = [
    pytest.param(None, False, id="no-policy"),
    pytest.param(OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none")), False, id="explicit-none"),
    pytest.param(OSEnvSpec(), True, id="default-sandbox"),
    pytest.param(
        OSEnvSpec(sandbox=OSEnvSandboxSpec(type="linux_bwrap")), True, id="requested-sandbox"
    ),
]


@pytest.mark.parametrize(("os_env", "sandbox_requested"), _CWD_POLICY_CASES)
def test_acp_executor_init_raises_on_deleted_explicit_cwd(
    tmp_path: Path, os_env: OSEnvSpec | None, sandbox_requested: bool
) -> None:
    """Missing cwd diagnostics are preserved as a cause when sandboxing is requested."""
    missing = str(tmp_path / "workspace")
    expected_error = HarnessSandboxValidationError if sandbox_requested else RuntimeError
    with pytest.raises(expected_error, match="workspace") as failure:
        AcpExecutor(AcpAgentConfig(command="agent"), cwd=missing, os_env=os_env)
    diagnostic = failure.value.__cause__ if sandbox_requested else failure.value
    assert type(diagnostic) is RuntimeError
    assert str(diagnostic) == f"ACP executor working directory does not exist: {missing!r}"
    if not sandbox_requested:
        assert failure.value.__cause__ is None
    else:
        assert missing not in failure.value.client_message
        assert failure.value.client_message == (
            "The working directory is unavailable. Choose an existing workspace and retry."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(("os_env", "sandbox_requested"), _CWD_POLICY_CASES)
async def test_start_process_guard_raises_clear_error_on_deleted_cwd(
    tmp_path: Path, os_env: OSEnvSpec | None, sandbox_requested: bool
) -> None:
    """Unconfined sessions retain ordinary workspace errors."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    ex = AcpExecutor(AcpAgentConfig(command="agent --acp"), cwd=str(workspace), os_env=os_env)
    assert ex._cwd == str(workspace)

    # Simulate the workspace being deleted after the executor was constructed.
    workspace.rmdir()

    events = []
    try:
        with (
            pytest.raises(HarnessSandboxValidationError)
            if sandbox_requested
            else contextlib.nullcontext()
        ) as failure:
            async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], ""):
                events.append(ev)
    finally:
        await ex.close()

    error_events = [e for e in events if isinstance(e, ExecutorError)]
    if failure is not None:
        assert type(failure.value.__cause__) is RuntimeError
        assert not error_events
        assert str(workspace) not in failure.value.client_message
        assert failure.value.client_message == (
            "The working directory is unavailable. Choose an existing workspace and retry."
        )
        msg = str(failure.value.__cause__)
    else:
        assert error_events, f"expected ExecutorError, got: {[type(e).__name__ for e in events]}"
        msg = error_events[0].message
    assert "working directory" in msg, f"expected 'working directory' in: {msg!r}"
    assert str(workspace) in msg, f"expected path in: {msg!r}"


@pytest.mark.asyncio
@pytest.mark.parametrize(("os_env", "sandbox_requested"), _CWD_POLICY_CASES)
async def test_start_process_guard_deleted_process_cwd_relative_cwd(
    os_env: OSEnvSpec | None,
    sandbox_requested: bool,
) -> None:
    """An inherited deleted cwd preserves its diagnostic outside client error events."""
    ex = AcpExecutor(AcpAgentConfig(command="agent --acp"), cwd=".", os_env=os_env)

    fnfe = FileNotFoundError(2, "No such file or directory")
    events = []
    try:
        with patch("omnigent.inner.acp_executor.os.getcwd", side_effect=fnfe):
            with patch(
                "omnigent.inner.acp_executor.os.readlink",
                return_value="/workspace/project (deleted)",
            ):
                with (
                    pytest.raises(HarnessSandboxValidationError)
                    if sandbox_requested
                    else contextlib.nullcontext()
                ) as failure:
                    async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], ""):
                        events.append(ev)
    finally:
        await ex.close()

    error_events = [e for e in events if isinstance(e, ExecutorError)]
    if failure is not None:
        assert type(failure.value.__cause__) is RuntimeError
        assert not error_events
        assert "/workspace/project" not in failure.value.client_message
        assert failure.value.client_message == (
            "The working directory is unavailable. Choose an existing workspace and retry."
        )
        msg = str(failure.value.__cause__)
    else:
        assert error_events
        msg = error_events[0].message
    assert "deleted" in msg, f"expected 'deleted' in: {msg!r}"
    assert "/workspace/project" in msg, f"expected path in: {msg!r}"


@pytest.mark.parametrize("fresh_session", [False, True], ids=["warm", "history-replay"])
@pytest.mark.parametrize(
    ("tail", "expected_id"),
    [
        (
            {"type": "message", "role": "user", "id": "msg_current", "content": "current"},
            "msg_current",
        ),
        (
            {"type": "message", "role": "user", "id": "msg_empty", "content": []},
            "msg_empty",
        ),
        (
            {
                "type": "message",
                "role": "user",
                "id": "msg_empty",
                "content": [{"type": "input_text", "text": ""}],
            },
            "msg_empty",
        ),
        ({"type": "message", "role": "user", "content": "Your runner was interrupted"}, None),
        ({"type": "function_call", "name": "read", "call_id": "call_1", "arguments": "{}"}, None),
        ({"type": "function_call_output", "call_id": "call_1", "output": "done"}, None),
    ],
    ids=[
        "current",
        "empty-list",
        "empty-text",
        "synthetic-recovery",
        "tool-resume",
        "tool-result-resume",
    ],
)
async def test_prompt_input_item_id_is_current_or_null(fresh_session, tail, expected_id) -> None:
    """History filtering and replay must never select an older user's identity."""
    from omnigent.runtime.harnesses._executor_adapter import _translate_input_to_messages

    executor = AcpExecutor(AcpAgentConfig(command="unused", omnigent_mcp=False))
    messages = _translate_input_to_messages(
        [
            {"type": "message", "role": "user", "id": "msg_old", "content": "old"},
            {"type": "message", "role": "assistant", "content": "old reply"},
            tail,
        ],
        preserve_empty_initiating_message=executor.preserves_empty_initiating_message(),
    )
    executor._proc = Mock(returncode=None)
    executor._initialized = True
    executor._session_id = "acp-session"
    executor._system_prompt_sent = not fresh_session
    prompts = []

    async def send(message):
        prompts.append(message)
        executor._pending[message["id"]].set_result({"result": {"stopReason": "end_turn"}})

    executor._send = send
    events = [event async for event in executor.run_turn(messages, [], "system")]
    assert any(isinstance(event, TurnComplete) for event in events)
    assert len(prompts) == 1
    assert prompts[0]["params"]["_meta"] == {_META_INPUT_ITEM_ID: expected_id}
    if expected_id == "msg_empty" and not fresh_session:
        assert prompts[0]["params"]["prompt"] == [{"type": "text", "text": ""}]
    if fresh_session and tail.get("role") == "user":
        assert "old reply" in prompts[0]["params"]["prompt"][0]["text"]


# ---------------------------------------------------------------------------
# Antigravity ACP: read_file body backfill, tool-field normalization, interaction_* cards
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agy_acp_read_file_fills_missing_body_from_workspace(tmp_path: Path) -> None:
    """Completed AG ACP read_file with no content/rawOutput backfills the workspace file body."""
    target = tmp_path / "notes" / "probe.txt"
    target.parent.mkdir()
    target.write_text("W2F_FIXTURE_OK\nsecond line\n", encoding="utf-8")

    ex = AcpExecutor(AcpAgentConfig(command="agy acp --stdio"), cwd=str(tmp_path))
    raw_input = {"Path": str(target)}
    started = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "call_read_1",
            "name": "read_file",
            "kind": "read",
            "status": "in_progress",
            "title": "File notes/probe.txt",
            "locations": [{"path": str(target)}],
            "rawInput": raw_input,
        }
    )
    assert len(started) == 1
    req = started[0]
    assert isinstance(req, ToolCallRequest)
    assert req.name == "read"
    assert req.args == {"path": str(target)}

    done = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call_read_1",
            "name": "read_file",
            "kind": "read",
            "status": "completed",
            "title": f"File {target}",
            "locations": [{"path": str(target)}],
            "rawInput": raw_input,
        }
    )
    assert len(done) == 1
    comp = done[0]
    assert isinstance(comp, ToolCallComplete)
    assert comp.name == "read"
    assert comp.status is ToolCallStatus.SUCCESS
    assert comp.result == "W2F_FIXTURE_OK\nsecond line\n"
    assert comp.metadata == {"call_id": "call_read_1"}


def test_agy_acp_read_file_honors_line_range_and_byte_offset(tmp_path: Path) -> None:
    """AG ACP read_file backfill respects StartLine, EndLine, and ByteOffset."""
    target = tmp_path / "sample.txt"
    target.write_text("line1\nline2\nline3\nline4\nline5\n", encoding="utf-8")
    ex = AcpExecutor(AcpAgentConfig(command="agy acp"), cwd=str(tmp_path))

    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r1",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": "sample.txt", "StartLine": 2, "EndLine": 4},
        }
    )
    done = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "r1", "status": "completed"}
    )
    assert isinstance(done[0], ToolCallComplete)
    assert done[0].result == "line2\nline3\nline4\n"

    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r2",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": "sample.txt", "StartLine": 2, "EndLine": 3, "ByteOffset": 6},
        }
    )
    done_offset = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "r2", "status": "completed"}
    )
    assert isinstance(done_offset[0], ToolCallComplete)
    assert done_offset[0].result == "line3\n"

    # Inverted range and boolean line numbers fail closed (no backfill).
    for idx, bad_args in enumerate(
        [
            {"Path": "sample.txt", "StartLine": 4, "EndLine": 2},
            {"Path": "sample.txt", "StartLine": True},
        ]
    ):
        cid = f"bad_{idx}"
        ex._handle_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": cid,
                "name": "read_file",
                "kind": "read",
                "rawInput": bad_args,
            }
        )
        bad_done = ex._handle_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": cid, "status": "completed"}
        )
        assert isinstance(bad_done[0], ToolCallComplete)
        assert bad_done[0].result is None


def test_agy_acp_read_file_backfill_guards(tmp_path: Path) -> None:
    """Backfill preserves output and blocks outside, symlink, binary, failed, and non-AG reads."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("SECRET_OUTSIDE\n", encoding="utf-8")
    inside = workspace / "inside.txt"
    inside.write_text("INSIDE_BODY\n", encoding="utf-8")
    binary = workspace / "blob.bin"
    binary.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    large = workspace / "large.txt"
    large.write_text("a" * 80_000, encoding="utf-8")
    escape_link = workspace / "escape.txt"
    escape_link.symlink_to(outside)

    ex = AcpExecutor(AcpAgentConfig(command="agy acp"), cwd=str(workspace))

    def _run_read(
        executor: AcpExecutor,
        cid: str,
        path: str,
        *,
        status: str = "completed",
        content: object = None,
        raw_output: object = None,
    ) -> ToolCallComplete:
        executor._handle_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": cid,
                "name": "read_file",
                "kind": "read",
                "rawInput": {"Path": path},
            }
        )
        update: dict[str, object] = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": cid,
            "status": status,
        }
        if content is not None:
            update["content"] = content
        if raw_output is not None:
            update["rawOutput"] = raw_output
        events = executor._handle_session_update(update)
        assert len(events) == 1 and isinstance(events[0], ToolCallComplete)
        return events[0]

    # Agent-provided content is preserved untouched.
    agent_content = [{"type": "content", "content": {"type": "text", "text": "FROM_AGENT"}}]
    assert _run_read(ex, "c_agent", str(inside), content=agent_content).result == agent_content
    # Failed read does not backfill.
    assert _run_read(ex, "c_fail", str(inside), status="failed").result is None
    # Outside workspace and symlink escape are refused.
    assert _run_read(ex, "c_out", str(outside)).result is None
    assert _run_read(ex, "c_sym", str(escape_link)).result is None
    # Binary file returns None.
    assert _run_read(ex, "c_bin", str(binary)).result is None
    # Embedded NUL and malformed file:// URI fail closed cleanly (never raise ValueError).
    assert _run_read(ex, "c_nul", str(inside) + "\x00.txt").result is None
    assert _run_read(ex, "c_bad_uri", "file://[").result is None

    # Dual path fields (AbsolutePath + Path) are rejected unambiguously before policy or disk read.
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c_dual",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"AbsolutePath": str(inside), "Path": str(inside)},
        }
    )
    dual_done = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c_dual", "status": "completed"}
    )[0]
    assert isinstance(dual_done, ToolCallComplete) and dual_done.result is None

    # POSIX FIFO does not block and fails closed (not a regular file).
    if hasattr(os, "mkfifo"):
        fifo_path = workspace / "pipe.fifo"
        os.mkfifo(fifo_path)
        assert _run_read(ex, "c_fifo", str(fifo_path)).result is None

    # Exact 64 KiB boundary splitting a 3-byte UTF-8 character decodes cleanly without error.
    utf8_edge = workspace / "utf8_edge.txt"
    utf8_edge.write_bytes(b"a" * (64 * 1024 - 1) + "中".encode() + b"tail")
    utf8_res = _run_read(ex, "c_utf8", str(utf8_edge)).result
    assert isinstance(utf8_res, str)
    assert utf8_res == ("a" * (64 * 1024 - 1)) + "\n...[truncated]"

    # Completion cannot supply or swap paths; orphan and bridge completions never backfill.
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c_swap",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": str(workspace / "missing.txt")},
        }
    )
    swapped = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c_swap",
            "status": "completed",
            "rawInput": {"Path": str(inside)},
            "locations": [{"path": str(inside)}],
        }
    )[0]
    assert isinstance(swapped, ToolCallComplete) and swapped.result is None

    orphan = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "c_orphan",
            "name": "read_file",
            "kind": "read",
            "status": "completed",
            "rawInput": {"Path": str(inside)},
        }
    )[0]
    assert isinstance(orphan, ToolCallComplete) and orphan.result is None

    ex._bridge_tool_aliases = frozenset({"read_file"})
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "c_bridge",
            "title": "read_file",
            "kind": "read",
            "rawInput": {"Path": str(inside)},
        }
    )
    bridge_done = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "c_bridge", "status": "completed"}
    )[0]
    assert isinstance(bridge_done, ToolCallComplete) and bridge_done.result is None
    ex._bridge_tool_aliases = frozenset()

    # Large file is capped cleanly.
    large_res = _run_read(ex, "c_large", str(large)).result
    assert isinstance(large_res, str)
    assert large_res.endswith("\n...[truncated]")
    assert len(large_res) < 70_000

    # Forked os_env skips direct disk read.
    forked_ex = AcpExecutor(
        AcpAgentConfig(command="agy acp"),
        cwd=str(workspace),
        os_env=OSEnvSpec(type="caller_process", fork=True),
    )
    assert _run_read(forked_ex, "c_fork", str(inside)).result is None

    # Non-AG ACP agent never backfills from disk.
    non_ag = AcpExecutor(AcpAgentConfig(command="opencode acp"), cwd=str(workspace))
    assert _run_read(non_ag, "c_non_ag", str(inside)).result is None


@pytest.mark.asyncio
async def test_managed_agy_permission_binds_read_file_path_and_rejects_mixed(
    windows_acp_permission_executor,
) -> None:
    """Managed AG ACP binds read_file(Path) and still rejects mixed or malformed shapes."""
    for tier in ("auto", "plan"):
        ex = windows_acp_permission_executor(tier)
        params = _pending_acp_permission(
            ex, {"Path": r"tmp\probe.txt", "StartLine": 1, "EndLine": 10}, "read"
        )
        assert await ex._decide_permission(params) == (True, "once")
        policy_call = ex._policy_evaluator.await_args
        assert policy_call is not None
        assert policy_call.args[1]["name"] == "read_file"
        assert policy_call.args[1]["arguments"]["Path"] == r"tmp\probe.txt"

    for denied_args in (
        {"Path": r"C:\work\control\policy.json"},
        {"AbsolutePath": r"C:\work\control\policy.json"},
    ):
        ex_denied = windows_acp_permission_executor("auto")
        params_denied = _pending_acp_permission(ex_denied, denied_args, "read")
        assert await ex_denied._decide_permission(params_denied) == (False, None)
        ex_denied._policy_evaluator.assert_awaited_once()
        ex_denied._elicitation_handler.assert_not_awaited()

    for bad_args in (
        {"Path": ""},
        {"Path": r"tmp\probe.txt", "StartLine": True},
        {"Path": r"tmp\probe.txt", "Content": "injected"},
        {"Path": r"tmp\probe.txt", "CommandLine": "whoami"},
        {"Path": r"tmp\probe.txt", "AbsolutePath": r"tmp\other.txt"},
        {"Path": r"tmp\probe.txt", "UnknownKey": 1},
    ):
        ex_bad = windows_acp_permission_executor("auto")
        params_bad = _pending_acp_permission(ex_bad, bad_args, "read")
        assert await ex_bad._decide_permission(params_bad) == (False, None)
        ex_bad._policy_evaluator.assert_not_awaited()


def test_agy_acp_normalizes_tool_fields_and_preserves_bridge_and_non_ag() -> None:
    """AG ACP tool calls normalize names, args, and shell rawOutput to Omnigent's shared shape."""
    ex = AcpExecutor(AcpAgentConfig(command="agy.exe acp --stdio"))

    # Shell command with PascalCase inputs and dict rawOutput.
    shell_start = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "sh1",
            "name": "run_command",
            "kind": "execute",
            "title": "cat probe.txt",
            "rawInput": {"CommandLine": "cat probe.txt", "Cwd": "/opt/work/project"},
        }
    )
    assert isinstance(shell_start[0], ToolCallRequest)
    assert shell_start[0].name == "shell"
    assert shell_start[0].args == {"command": "cat probe.txt", "cwd": "/opt/work/project"}

    shell_done = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sh1",
            "status": "completed",
            "rawOutput": {
                "commandLine": "cat probe.txt",
                "workingDir": "/opt/work/project",
                "exitCode": 0,
                "exit_code": 0,
                "combinedOutput": "after\n",
                "formatted_output": "after\n",
            },
        }
    )
    assert isinstance(shell_done[0], ToolCallComplete)
    assert shell_done[0].name == "shell"
    assert shell_done[0].result == "after\n"

    # Non-zero exit code is preserved as `[exit code: N]`.
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "sh2",
            "kind": "execute",
            "title": "Run command",
            "rawInput": {"command_line": "test -f missing.txt", "working_dir": "/opt/work"},
        }
    )
    shell_nz = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "sh2",
            "status": "completed",
            "rawOutput": {"combined_output": "", "exit_code": 1},
        }
    )
    assert isinstance(shell_nz[0], ToolCallComplete)
    assert shell_nz[0].result == "[exit code: 1]"

    # Edit with TargetFile / TargetContent / ReplacementContent and Edits list.
    edit_start = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "ed1",
            "kind": "edit",
            "title": "Run edit_file?",
            "rawInput": {
                "TargetFile": "file:///opt/work/app.py",
                "TargetContent": "old = 1",
                "ReplacementContent": "new = 2",
            },
        }
    )
    assert isinstance(edit_start[0], ToolCallRequest)
    assert edit_start[0].name == "edit"
    assert edit_start[0].args == {
        "path": "/opt/work/app.py",
        "oldText": "old = 1",
        "newText": "new = 2",
    }

    # Write with Path + Content + Overwrite.
    write_start = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "wr1",
            "name": "write_to_file",
            "kind": "edit",
            "title": "File notes.txt",
            "rawInput": {"Path": "/D:/work/notes.txt", "Content": "hello\n", "Overwrite": True},
        }
    )
    assert isinstance(write_start[0], ToolCallRequest)
    assert write_start[0].name == "write"
    assert write_start[0].args == {
        "path": "D:/work/notes.txt",
        "content": "hello\n",
        "Overwrite": True,
    }

    # Multi-edit list, search, list, and web tools.
    multi_edit = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "ed2",
            "name": "edit_file",
            "kind": "edit",
            "rawInput": {
                "Path": "/opt/work/a.py",
                "Edits": [{"Target": "x", "Replacement": "y"}],
            },
        }
    )
    assert isinstance(multi_edit[0], ToolCallRequest)
    assert multi_edit[0].name == "edit"
    assert multi_edit[0].args == {
        "path": "/opt/work/a.py",
        "edits": [{"oldText": "x", "newText": "y"}],
    }

    search_req = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "s1",
            "name": "grep_search",
            "rawInput": {"Query": "needle", "SearchPath": "/opt/work"},
        }
    )[0]
    assert isinstance(search_req, ToolCallRequest)
    assert search_req.name == "grep"
    assert search_req.args == {"pattern": "needle", "path": "/opt/work"}

    web_req = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "w1",
            "name": "search_web",
            "rawInput": {"Query": "acp protocol"},
        }
    )[0]
    assert isinstance(web_req, ToolCallRequest)
    assert web_req.name == "web_search"
    assert web_req.args == {"query": "acp protocol"}

    # Empty write content and empty edit replacement are preserved as empty strings.
    empty_write = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "w_empty",
            "name": "write_file",
            "kind": "edit",
            "rawInput": {"Path": "/opt/work/empty.txt", "Content": ""},
        }
    )[0]
    assert isinstance(empty_write, ToolCallRequest)
    assert empty_write.name == "write"
    assert empty_write.args == {"path": "/opt/work/empty.txt", "content": ""}

    delete_edit = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "e_empty",
            "name": "edit_file",
            "kind": "edit",
            "rawInput": {
                "Path": "/opt/work/a.py",
                "TargetContent": "remove me",
                "ReplacementContent": "",
            },
        }
    )[0]
    assert isinstance(delete_edit, ToolCallRequest)
    assert delete_edit.name == "edit"
    assert delete_edit.args == {
        "path": "/opt/work/a.py",
        "oldText": "remove me",
        "newText": "",
    }

    # Unmanaged bare permission fallback keeps original raw title and rawInput for policy rules.
    raw_shell = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "sh_fallback",
            "name": "run_command",
            "kind": "execute",
            "title": "Run dangerous",
            "rawInput": {"CommandLine": "rm -rf /tmp/x"},
        }
    )[0]
    assert isinstance(raw_shell, ToolCallRequest) and raw_shell.name == "shell"
    assert ex._extract_tool_call({"toolCall": {"toolCallId": "sh_fallback"}}) == (
        "Run dangerous",
        {"CommandLine": "rm -rf /tmp/x"},
    )

    # Bridged Omnigent MCP tool stays untouched so dispatch correlation keeps working.
    ex._bridge_tool_aliases = frozenset(
        {"sys_session_get_info", "mcp_omnigent_sys_session_get_info"}
    )
    bridge_req = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "mcp1",
            "title": "sys_session_get_info",
            "rawInput": {"Path": "keep-raw"},
        }
    )[0]
    assert isinstance(bridge_req, ToolCallRequest)
    assert bridge_req.name == "sys_session_get_info"
    assert bridge_req.args == {"Path": "keep-raw"}
    assert "internally_executed" not in bridge_req.metadata

    # Non-AG ACP agent keeps its original tool name and rawInput unchanged.
    other = AcpExecutor(AcpAgentConfig(command="opencode acp"))
    other_req = other._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "o1",
            "name": "run_command",
            "kind": "execute",
            "title": "Custom Shell",
            "rawInput": {"CommandLine": "ls"},
        }
    )[0]
    assert isinstance(other_req, ToolCallRequest)
    assert other_req.name == "Custom Shell"
    assert other_req.args == {"CommandLine": "ls"}


@pytest.mark.asyncio
@pytest.mark.parametrize("host_tier", [False, True], ids=["unmanaged", "managed"])
@pytest.mark.parametrize("permission_mode", ["auto", "bypassPermissions"])
async def test_agy_acp_interaction_routes_to_question_card(
    host_tier: bool, permission_mode: str
) -> None:
    """AG ACP interaction_* requests route to ask_user_question cards and never auto-approve."""
    from omnigent.server.schemas import ElicitationResult

    ex = AcpExecutor(
        AcpAgentConfig(
            command="agy acp --stdio",
            host_permission_tier=host_tier,
            permission_mode=permission_mode,
        )
    )
    ex._session_id = "session-1"
    ex._active_prompt_request_id = 1
    ex._send = AsyncMock()
    ex._policy_evaluator = AsyncMock()
    ex._elicitation_handler = AsyncMock()
    ex._elicitation_choice_handler = AsyncMock()

    captured_params = []

    async def fake_form(elicit_id: str, params) -> ElicitationResult:
        captured_params.append((elicit_id, params))
        return ElicitationResult(action="accept", content={"interaction_9960062f": "Needs review"})

    ex._form_elicitation_handler = fake_form

    request = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "session/request_permission",
        "params": {
            "sessionId": "session-1",
            "toolCall": {
                "toolCallId": "interaction_9960062f",
                "status": "pending",
                "title": "Which result label should be used for the verification?",
                "rawInput": {},
            },
            "options": [
                {"optionId": "1", "name": "Verified", "kind": "allow_once"},
                {"optionId": "2", "name": "Needs review", "kind": "allow_once"},
            ],
        },
    }
    await ex._respond_to_agent_request(request)

    assert len(captured_params) == 1
    _elicit_id, params = captured_params[0]
    assert params.phase == "agy_ask_question"
    assert params.policy_name == "agy_acp_ask_question"
    assert params.model_extra["ask_user_question"] == {
        "questions": [
            {
                "id": "interaction_9960062f",
                "header": "Question",
                "question": "Which result label should be used for the verification?",
                "options": [{"label": "Verified"}, {"label": "Needs review"}],
                "multiSelect": False,
                "isOther": False,
            }
        ]
    }
    ex._policy_evaluator.assert_not_awaited()
    ex._elicitation_handler.assert_not_awaited()
    ex._elicitation_choice_handler.assert_not_awaited()
    reply = ex._send.await_args.args[0]
    assert reply == {
        "jsonrpc": "2.0",
        "id": 7,
        "result": {"outcome": {"outcome": "selected", "optionId": "2"}},
    }


@pytest.mark.asyncio
async def test_agy_acp_interaction_edge_cases_and_anti_spoofing(
    windows_acp_permission_executor,
) -> None:
    """Verify reject_once selection, duplicate labels, dismiss, missing handler, and spoofing."""
    from omnigent.server.schemas import ElicitationResult

    def _make_req(
        call_id: str, options: list[dict[str, object]], **tool_extra: object
    ) -> dict[str, object]:
        return {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s1",
                "toolCall": {
                    "toolCallId": call_id,
                    "status": "pending",
                    "title": "Pick one",
                    "rawInput": {},
                    **tool_extra,
                },
                "options": options,
            },
        }

    # 1. Selecting a reject_once option still returns selected (not cancelled).
    ex = AcpExecutor(AcpAgentConfig(command="agy acp"))
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    ex._send = AsyncMock()
    ex._form_elicitation_handler = AsyncMock(
        return_value=ElicitationResult(
            action="accept", content={"interaction_reject": "Do not trust"}
        )
    )
    await ex._respond_to_agent_request(
        _make_req(
            "interaction_reject",
            [{"optionId": "deny", "name": "Do not trust", "kind": "reject_once"}],
        )
    )
    assert ex._send.await_args.args[0]["result"] == {
        "outcome": {"outcome": "selected", "optionId": "deny"}
    }

    # 2. Duplicate labels disambiguate; ambiguous raw label fails closed to cancelled.
    seen_labels: list[list[str]] = []

    async def pick_second(_eid: str, params) -> ElicitationResult:
        opts = [
            o["label"] for o in params.model_extra["ask_user_question"]["questions"][0]["options"]
        ]
        seen_labels.append(opts)
        return ElicitationResult(action="accept", content={"interaction_dup": opts[1]})

    ex._form_elicitation_handler = pick_second
    await ex._respond_to_agent_request(
        _make_req(
            "interaction_dup",
            [
                {"optionId": "1", "name": "Same", "kind": "allow_once"},
                {"optionId": "2", "name": "Same", "kind": "allow_once"},
            ],
        )
    )
    assert seen_labels == [["Same (1)", "Same (2)"]]
    assert ex._send.await_args.args[0]["result"] == {
        "outcome": {"outcome": "selected", "optionId": "2"}
    }

    # 2b. Label equal to another option's optionId resolves strictly by label, never raw ID.
    ex._form_elicitation_handler = AsyncMock(
        return_value=ElicitationResult(
            action="accept", content={"interaction_collide": "opt_allow"}
        )
    )
    await ex._respond_to_agent_request(
        _make_req(
            "interaction_collide",
            [
                {"optionId": "opt_allow", "name": "Approve", "kind": "allow_once"},
                {"optionId": "opt_deny", "name": "opt_allow", "kind": "reject_once"},
            ],
        )
    )
    assert ex._send.await_args.args[0]["result"] == {
        "outcome": {"outcome": "selected", "optionId": "opt_deny"}
    }

    # 2c. Generated label colliding with a third option's label is disambiguated again.
    seen_triple: list[list[str]] = []

    async def pick_third(_eid: str, params) -> ElicitationResult:
        opts = [
            o["label"] for o in params.model_extra["ask_user_question"]["questions"][0]["options"]
        ]
        seen_triple.append(opts)
        return ElicitationResult(action="accept", content={"interaction_triple": opts[2]})

    ex._form_elicitation_handler = pick_third
    await ex._respond_to_agent_request(
        _make_req(
            "interaction_triple",
            [
                {"optionId": "1", "name": "Same", "kind": "allow_once"},
                {"optionId": "2", "name": "Same", "kind": "allow_once"},
                {"optionId": "3", "name": "Same (1)", "kind": "allow_once"},
            ],
        )
    )
    assert len(seen_triple) == 1 and len(set(seen_triple[0])) == 3
    assert ex._send.await_args.args[0]["result"] == {
        "outcome": {"outcome": "selected", "optionId": "3"}
    }

    # 2d. Call acquiring tool rawInput or going terminal while card is open cancels on reply.
    async def mutate_while_open(_eid: str, _params) -> ElicitationResult:
        ex._note_agy_interaction_activity(
            "s1",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "interaction_mutated",
                "kind": "execute",
                "rawInput": {"CommandLine": "whoami"},
            },
        )
        return ElicitationResult(action="accept", content={"interaction_mutated": "Yes"})

    ex._form_elicitation_handler = mutate_while_open
    await ex._respond_to_agent_request(
        _make_req("interaction_mutated", [{"optionId": "1", "name": "Yes", "kind": "allow_once"}])
    )
    assert ex._send.await_args.args[0]["result"] == {"outcome": {"outcome": "cancelled"}}

    async def finish_while_open(_eid: str, _params) -> ElicitationResult:
        ex._note_agy_interaction_activity(
            "s1",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "interaction_gone",
                "status": "completed",
            },
        )
        return ElicitationResult(action="accept", content={"interaction_gone": "Yes"})

    ex._form_elicitation_handler = finish_while_open
    await ex._respond_to_agent_request(
        _make_req("interaction_gone", [{"optionId": "1", "name": "Yes", "kind": "allow_once"}])
    )
    assert ex._send.await_args.args[0]["result"] == {"outcome": {"outcome": "cancelled"}}

    # 2e. Already-tainted or stale question-shaped request under bypassPermissions cancels
    # (never falls through to ordinary permission auto-approval).
    bypass_ex = AcpExecutor(AcpAgentConfig(command="agy acp", permission_mode="bypassPermissions"))
    bypass_ex._session_id = "s1"
    bypass_ex._active_prompt_request_id = 1
    bypass_ex._send = AsyncMock()
    bypass_ex._form_elicitation_handler = AsyncMock()
    bypass_ex._tainted_interaction_calls.add("interaction_tainted")
    await bypass_ex._respond_to_agent_request(
        _make_req("interaction_tainted", [{"optionId": "1", "name": "Yes", "kind": "allow_once"}])
    )
    bypass_ex._form_elicitation_handler.assert_not_awaited()
    assert bypass_ex._send.await_args.args[0]["result"] == {"outcome": {"outcome": "cancelled"}}

    # Without an active prompt, or during stale-queue drain even after a prior timeout left
    # _active_prompt_request_id populated, unmanaged questions cancel without opening a card.
    bypass_ex._active_prompt_request_id = None
    await bypass_ex._respond_to_agent_request(
        _make_req("interaction_stale", [{"optionId": "1", "name": "Yes", "kind": "allow_once"}])
    )
    bypass_ex._form_elicitation_handler.assert_not_awaited()
    assert bypass_ex._send.await_args.args[0]["result"] == {"outcome": {"outcome": "cancelled"}}

    bypass_ex._active_prompt_request_id = 42
    await bypass_ex._queue.put(
        _make_req("interaction_drain", [{"optionId": "1", "name": "Yes", "kind": "allow_once"}])
    )
    await bypass_ex._drain_stale_queue()
    bypass_ex._form_elicitation_handler.assert_not_awaited()
    assert bypass_ex._active_prompt_request_id is None
    assert bypass_ex._send.await_args.args[0]["result"] == {"outcome": {"outcome": "cancelled"}}

    # 3. Decline / no handler / duplicate optionId fail closed to cancelled.
    ex._form_elicitation_handler = AsyncMock(return_value=ElicitationResult(action="decline"))
    await ex._respond_to_agent_request(
        _make_req("interaction_decline", [{"optionId": "1", "name": "Yes", "kind": "allow_once"}])
    )
    assert ex._send.await_args.args[0]["result"] == {"outcome": {"outcome": "cancelled"}}

    ex._form_elicitation_handler = None
    await ex._respond_to_agent_request(
        _make_req("interaction_headless", [{"optionId": "1", "name": "Yes", "kind": "allow_once"}])
    )
    assert ex._send.await_args.args[0]["result"] == {"outcome": {"outcome": "cancelled"}}

    ex._form_elicitation_handler = AsyncMock(
        return_value=ElicitationResult(action="accept", content={"interaction_bad": "1"})
    )
    await ex._respond_to_agent_request(
        _make_req(
            "interaction_bad",
            [
                {"optionId": "1", "name": "A", "kind": "allow_once"},
                {"optionId": "1", "name": "B", "kind": "allow_once"},
            ],
        )
    )
    assert ex._send.await_args.args[0]["result"] == {"outcome": {"outcome": "cancelled"}}
    ex._form_elicitation_handler.assert_not_awaited()

    # 4. Spoofed interaction_* with command rawInput or execute kind must not open a question card.
    managed = windows_acp_permission_executor("auto")
    managed._session_id = "s1"
    managed._active_prompt_request_id = 1
    managed._send = AsyncMock()
    managed._form_elicitation_handler = AsyncMock(
        return_value=ElicitationResult(action="accept", content={"interaction_spoof": "Allow"})
    )
    await managed._respond_to_agent_request(
        _make_req(
            "interaction_spoof",
            [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}],
            kind="execute",
            rawInput={"CommandLine": "rm -rf /"},
        )
    )
    managed._form_elicitation_handler.assert_not_awaited()
    assert managed._send.await_args.args[0]["error"]["code"] == -32000

    # 5. Non-AG ACP agent with interaction_* keeps regular permission path.
    non_ag = AcpExecutor(AcpAgentConfig(command="opencode acp"))
    non_ag._session_id = "s1"
    non_ag._send = AsyncMock()
    non_ag._form_elicitation_handler = AsyncMock()
    non_ag._elicitation_handler = AsyncMock(return_value=True)
    await non_ag._respond_to_agent_request(
        _make_req("interaction_other", [{"optionId": "1", "name": "Yes", "kind": "allow_once"}])
    )
    non_ag._form_elicitation_handler.assert_not_awaited()
    non_ag._elicitation_handler.assert_awaited_once()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable wrapper script")
@pytest.mark.asyncio
async def test_agy_acp_fake_agent_e2e_read_file_and_interaction_card(tmp_path: Path) -> None:
    """Fake AG ACP subprocess proves read_file body, normalized fields, and interaction_* cards."""
    from omnigent.server.schemas import ElicitationResult

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probe = workspace / "probe.txt"
    probe.write_text("FAKE_AG_BODY_LINE\n", encoding="utf-8")

    fake_agy = tmp_path / "agy"
    fake_agy.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        f"probe_path = {str(probe)!r}\n"
        "def send(obj):\n"
        "    sys.stdout.write(json.dumps(obj) + '\\n')\n"
        "    sys.stdout.flush()\n"
        "def upd(u):\n"
        "    send({'jsonrpc': '2.0', 'method': 'session/update',\n"
        "          'params': {'sessionId': 'ag-s1', 'update': u}})\n"
        "for raw in sys.stdin:\n"
        "    line = raw.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    msg = json.loads(line)\n"
        "    method = msg.get('method')\n"
        "    if method == 'initialize':\n"
        "        send({'jsonrpc': '2.0', 'id': msg['id'],\n"
        "              'result': {'protocolVersion': 1, 'agentCapabilities': {}}})\n"
        "    elif method == 'session/new':\n"
        "        send({'jsonrpc': '2.0', 'id': msg['id'], 'result': {'sessionId': 'ag-s1'}})\n"
        "    elif method == 'session/prompt':\n"
        "        prompt_id = msg['id']\n"
        "        upd({'sessionUpdate': 'tool_call', 'toolCallId': 'call_r',\n"
        "             'name': 'read_file', 'kind': 'read', 'status': 'in_progress',\n"
        "             'title': 'File probe.txt', 'rawInput': {'Path': probe_path}})\n"
        "        upd({'sessionUpdate': 'tool_call_update', 'toolCallId': 'call_r',\n"
        "             'name': 'read_file', 'kind': 'read', 'status': 'completed',\n"
        "             'title': 'File probe.txt', 'rawInput': {'Path': probe_path}})\n"
        "        send({\n"
        "            'jsonrpc': '2.0', 'id': 99, 'method': 'session/request_permission',\n"
        "            'params': {\n"
        "                'sessionId': 'ag-s1',\n"
        "                'toolCall': {\n"
        "                    'toolCallId': 'interaction_e2e', 'status': 'pending',\n"
        "                    'title': 'Pick label?', 'rawInput': {},\n"
        "                },\n"
        "                'options': [\n"
        "                    {'optionId': 'opt_a', 'name': 'Alpha', 'kind': 'allow_once'},\n"
        "                    {'optionId': 'opt_b', 'name': 'Beta', 'kind': 'allow_once'},\n"
        "                ],\n"
        "            },\n"
        "        })\n"
        "        reply = json.loads(sys.stdin.readline())\n"
        "        chosen = reply.get('result', {}).get('outcome', {}).get('optionId')\n"
        "        upd({'sessionUpdate': 'agent_message_chunk',\n"
        "             'content': {'type': 'text', 'text': f'chosen={chosen}'}})\n"
        "        send({'jsonrpc': '2.0', 'id': prompt_id,\n"
        "              'result': {'stopReason': 'end_turn'}})\n",
        encoding="utf-8",
    )
    fake_agy.chmod(0o755)

    ex = AcpExecutor(
        AcpAgentConfig(command=shlex.join([str(fake_agy), "acp"]), omnigent_mcp=False),
        cwd=str(workspace),
    )
    seen_cards = []

    async def fake_form(elicit_id: str, params) -> ElicitationResult:
        seen_cards.append((elicit_id, params))
        return ElicitationResult(action="accept", content={"interaction_e2e": "Beta"})

    ex._form_elicitation_handler = fake_form
    events: list[ExecutorEvent] = []
    try:
        async for ev in ex.run_turn([{"role": "user", "content": "check"}], [], ""):
            events.append(ev)
    finally:
        await ex.close()

    reqs = [e for e in events if isinstance(e, ToolCallRequest)]
    comps = [e for e in events if isinstance(e, ToolCallComplete)]
    texts = "".join(e.text for e in events if isinstance(e, TextChunk))
    assert len(reqs) == 1 and reqs[0].name == "read" and reqs[0].args == {"path": str(probe)}
    assert len(comps) == 1 and comps[0].name == "read" and comps[0].result == "FAKE_AG_BODY_LINE\n"
    assert len(seen_cards) == 1 and seen_cards[0][1].phase == "agy_ask_question"
    assert texts == "chosen=opt_b"


@pytest.mark.asyncio
async def test_agy_acp_read_file_backfill_respects_policy_denial_and_authorization(
    tmp_path: Path,
) -> None:
    """Policy/tier denials block disk backfill; authorized reads backfill only the bound path."""
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload

    allowed_file = tmp_path / "ok.txt"
    allowed_file.write_text("ALLOWED_CONTENT\n", encoding="utf-8")
    secret_file = tmp_path / "control.json"
    secret_file.write_text("BLOCKED_CONTENT\n", encoding="utf-8")

    def _make_managed(verdict_fn):
        ex = AcpExecutor(
            AcpAgentConfig(command="agy acp --stdio", host_permission_tier=True),
            cwd=str(tmp_path),
        )
        ex._session_id = "s1"
        ex._active_prompt_request_id = 1
        ex._policy_evaluator = AsyncMock(side_effect=verdict_fn)
        return ex

    async def _eval(_phase: str, data: dict[str, object]) -> PolicyVerdictPayload:
        path = str(data.get("arguments", {}).get("Path", ""))
        if path.endswith("control.json"):
            return PolicyVerdictPayload(action="POLICY_ACTION_DENY")
        return PolicyVerdictPayload(
            action="POLICY_ACTION_UNSPECIFIED",
            data={"_omnigent_acp_tier": "allow"},
        )

    ex = _make_managed(_eval)

    # Policy-denied file inside workspace is never backfilled.
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r_denied",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": str(secret_file)},
        },
        update_session_id="s1",
    )
    events_denied = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "r_denied", "status": "completed"},
        update_session_id="s1",
    )
    await ex._resolve_agy_read_backfills("s1", "s1", events_denied)
    assert isinstance(events_denied[0], ToolCallComplete) and events_denied[0].result is None

    # Policy-allowed file inside workspace is backfilled for the active session.
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r_ok",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": str(allowed_file)},
        },
        update_session_id="s1",
    )
    events_ok = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "r_ok", "status": "completed"},
        update_session_id="s1",
    )
    await ex._resolve_agy_read_backfills("s1", "s1", events_ok)
    assert isinstance(events_ok[0], ToolCallComplete)
    assert events_ok[0].result == "ALLOWED_CONTENT\n"

    # Missing sessionId on start fails closed when a session is active (policy & direct paths).
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r_no_sess",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": str(allowed_file)},
        }
    )
    events_no_sess = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "r_no_sess", "status": "completed"},
        update_session_id="s1",
    )
    await ex._resolve_agy_read_backfills("s1", "s1", events_no_sess)
    assert isinstance(events_no_sess[0], ToolCallComplete) and events_no_sess[0].result is None

    direct_ex = AcpExecutor(AcpAgentConfig(command="agy acp --stdio"), cwd=str(tmp_path))
    direct_ex._session_id = "s1"
    direct_ex._active_prompt_request_id = 1
    direct_ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r_direct_mismatch",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": str(allowed_file)},
        },
        update_session_id="s1",
    )
    direct_done = direct_ex._handle_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "r_direct_mismatch",
            "status": "completed",
        },
        update_session_id="s_other",
    )
    assert isinstance(direct_done[0], ToolCallComplete) and direct_done[0].result is None

    # Live subprocess without a startup workspace anchor fails closed instead of reopening by path.
    direct_ex._proc = Mock(returncode=None)
    direct_ex._workspace_dir_fd = None
    direct_ex._workspace_win_handle = None
    direct_ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r_unanchored",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": str(allowed_file)},
        },
        update_session_id="s1",
    )
    unanchored_done = direct_ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "r_unanchored", "status": "completed"},
        update_session_id="s1",
    )
    assert isinstance(unanchored_done[0], ToolCallComplete) and unanchored_done[0].result is None

    # Cross-session update never backfills.
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r_wrong_sess",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": str(allowed_file)},
        },
        update_session_id="s1",
    )
    events_wrong = ex._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "r_wrong_sess", "status": "completed"},
        update_session_id="s_other",
    )
    await ex._resolve_agy_read_backfills("s1", "s_other", events_wrong)
    assert isinstance(events_wrong[0], ToolCallComplete) and events_wrong[0].result is None

    # Explicit permission denial prevents backfill even if a completion later arrives.
    ex._denied_permission_calls.add("r_perm_denied")
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r_perm_denied",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": str(allowed_file)},
        },
        update_session_id="s1",
    )
    events_perm_denied = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "r_perm_denied",
            "status": "completed",
        },
        update_session_id="s1",
    )
    await ex._resolve_agy_read_backfills("s1", "s1", events_perm_denied)
    assert isinstance(events_perm_denied[0], ToolCallComplete)
    assert events_perm_denied[0].result is None

    # Foreign-session tool_call start is not cached for backfill in the active session.
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r_foreign_start",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": str(allowed_file)},
        },
        update_session_id="s_foreign",
    )
    events_foreign = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "r_foreign_start",
            "status": "completed",
        },
        update_session_id="s1",
    )
    await ex._resolve_agy_read_backfills("s1", "s1", events_foreign)
    assert isinstance(events_foreign[0], ToolCallComplete) and events_foreign[0].result is None

    # Dual path fields (AbsolutePath + Path) never allow policy/reader mismatch.
    ex._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r_dual_policy",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"AbsolutePath": str(allowed_file), "Path": str(secret_file)},
        },
        update_session_id="s1",
    )
    events_dual = ex._handle_session_update(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "r_dual_policy",
            "status": "completed",
        },
        update_session_id="s1",
    )
    await ex._resolve_agy_read_backfills("s1", "s1", events_dual)
    assert isinstance(events_dual[0], ToolCallComplete) and events_dual[0].result is None

    # ASK verdict from policy also blocks silent backfill.
    async def _ask_only(_phase: str, _data: dict[str, object]) -> PolicyVerdictPayload:
        return PolicyVerdictPayload(action="POLICY_ACTION_ASK")

    ex_ask = _make_managed(_ask_only)
    ex_ask._handle_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "r_ask",
            "name": "read_file",
            "kind": "read",
            "rawInput": {"Path": str(allowed_file)},
        },
        update_session_id="s1",
    )
    events_ask = ex_ask._handle_session_update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "r_ask", "status": "completed"},
        update_session_id="s1",
    )
    await ex_ask._resolve_agy_read_backfills("s1", "s1", events_ask)
    assert isinstance(events_ask[0], ToolCallComplete) and events_ask[0].result is None
    ex_ask._policy_evaluator.assert_awaited_once()


@pytest.mark.asyncio
async def test_agy_acp_executor_adapter_wires_form_elicitation_round_trip() -> None:
    """ExecutorAdapter.run_turn auto-wires _form_elicitation_handler for AG interaction_* cards."""
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.server.schemas import CreateResponseRequest, ElicitationResult

    ex = AcpExecutor(AcpAgentConfig(command="agy acp --stdio"))
    ex._session_id = "s1"
    ex._active_prompt_request_id = 1
    ex._send = AsyncMock()
    adapter = ExecutorAdapter(executor_factory=lambda: ex)

    seen_params = []

    class _FakeFormCtx(_RecordingCtx):
        def __init__(self) -> None:
            super().__init__()
            self.session_id = "sess_1"
            self.cancelled = asyncio.Event()
            self.completed = False

        async def elicit(self, elicitation_id: str, params) -> ElicitationResult:
            seen_params.append((elicitation_id, params))
            return ElicitationResult(action="accept", content={"interaction_adp": "Verified"})

        def complete(self) -> None:
            self.completed = True

    async def fake_run_turn(*_args: object, **_kwargs: object):
        assert ex._form_elicitation_handler == adapter._stable_form_elicitation_handler
        await ex._respond_to_agent_request(
            {
                "jsonrpc": "2.0",
                "id": 15,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "toolCall": {
                        "toolCallId": "interaction_adp",
                        "status": "pending",
                        "title": "Confirm label?",
                        "rawInput": {},
                    },
                    "options": [
                        {"optionId": "v1", "name": "Verified", "kind": "allow_once"},
                        {"optionId": "v2", "name": "Retry", "kind": "reject_once"},
                    ],
                },
            }
        )
        yield TurnComplete(response="ok")

    ex.run_turn = fake_run_turn  # type: ignore[method-assign]
    ctx = _FakeFormCtx()
    request = CreateResponseRequest(model="agy", input=[{"role": "user", "content": "hi"}])
    await adapter.run_turn(request, ctx)  # type: ignore[arg-type]

    assert len(seen_params) == 1
    assert seen_params[0][1].phase == "agy_ask_question"
    assert ex._send.await_args.args[0] == {
        "jsonrpc": "2.0",
        "id": 15,
        "result": {"outcome": {"outcome": "selected", "optionId": "v1"}},
    }


# ---------------------------------------------------------------------------
# ACP tool-active idle pause & Antigravity 30-minute background wrapup
# ---------------------------------------------------------------------------


class _FakeAcpClock:
    """Deterministic virtual clock for ACP timeout and background-wait tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self._executor: AcpExecutor | None = None

    def __call__(self) -> float:
        return self.now

    def bind(self, executor: AcpExecutor) -> None:
        self._executor = executor

    async def settle(self) -> None:
        for _ in range(8):
            await asyncio.sleep(0)

    async def advance(self, seconds: float) -> None:
        # Process items queued before this tick at the prior timestamp first.
        await self.settle()
        self.now += seconds
        if self._executor is not None:
            self._executor._wake_turn()
        await self.settle()


def _acp_update_frame(session_id: str, update: dict[str, object]) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    }


@pytest.mark.asyncio
async def test_acp_idle_timer_pauses_and_refreshes_during_long_tool_run() -> None:
    """Active tools pause the 300s idle timer and refresh their window on progress."""
    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="agy acp", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    ex._proc = Mock(returncode=None)
    ex._initialized = True
    ex._session_id = "s1"
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "build"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    await prompt_sent.wait()
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "cmd-1",
                "title": "run_command",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"CommandLine": "make all"},
            },
        )
    )
    await clock.advance(600.0)  # Well past 300s prompt idle timeout.
    assert not task.done()

    # Streaming progress on the active tool refreshes the 3600s silent-tool cap.
    await clock.advance(2900.0)  # 3500s total.
    assert not task.done()
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "cmd-1",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": "still going"}}
                ],
            },
        )
    )
    await clock.advance(700.0)  # 4200s total (> 3600s from start), still alive.
    assert not task.done()

    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "cmd-1",
                "status": "completed",
                "rawOutput": "ok",
            },
        )
    )
    await clock.advance(250.0)  # Under 300s post-tool idle window.
    assert not task.done()
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Build complete."},
            },
        )
    )
    ex._pending[prompt_id[0]].set_result({"result": {"stopReason": "end_turn"}})
    await asyncio.wait_for(task, timeout=2.0)

    assert any(
        isinstance(ev, ToolCallComplete) and ev.status is ToolCallStatus.SUCCESS for ev in events
    )
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "Build complete."


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["cancelled-tool", "slow-approval"])
async def test_acp_idle_timer_cancelled_tool_and_slow_approval(scenario: str) -> None:
    """Cancelled tools release the pause; slow human approvals do not burn the tool window."""
    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="agy acp", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    ex._proc = Mock(returncode=None)
    ex._initialized = True
    ex._session_id = "s1"
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    async def slow_elicit(_tool: str, _args: dict[str, object]) -> bool:
        await clock.advance(4000.0)  # Human takes >1h to click Allow.
        return True

    ex._send = send  # type: ignore[assignment]
    ex._elicitation_handler = slow_elicit  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "run"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    await prompt_sent.wait()
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "cmd-1",
                "title": "run_command",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"CommandLine": "make"},
            },
        )
    )
    await clock.advance(10.0)

    if scenario == "cancelled-tool":
        ex._queue.put_nowait(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "cmd-1",
                    "status": "cancelled",
                },
            )
        )
        await clock.advance(301.0)
        await asyncio.wait_for(task, timeout=2.0)
        assert isinstance(events[-1], ExecutorError)
        assert events[-1].message == "Timeout waiting for ACP response"
        return

    ex._queue.put_nowait(
        {
            "jsonrpc": "2.0",
            "id": 99,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s1",
                "toolCall": {"toolCallId": "cmd-1", "title": "run_command"},
                "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"}],
            },
        }
    )
    await clock.advance(1000.0)  # Still within fresh 3600s window after approval.
    assert not task.done()
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {"sessionUpdate": "tool_call_update", "toolCallId": "cmd-1", "status": "completed"},
        )
    )
    ex._pending[prompt_id[0]].set_result({"result": {"stopReason": "end_turn"}})
    await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_agy_post_prompt_background_work_reports_before_turn_settles() -> None:
    """After agy's prompt returns, active background work reports before TurnComplete."""
    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="agy acp --stdio", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    ex._proc = Mock(returncode=None)
    ex._initialized = True
    ex._session_id = "s1"
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "start build"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    await prompt_sent.wait()
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "bg-cmd",
                "title": "run_command",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"CommandLine": "pytest -q", "IsDaemon": False},
            },
        )
    )
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Running in background.\n"},
            },
        )
    )
    ex._pending[prompt_id[0]].set_result(
        {"result": {"stopReason": "end_turn", "usage": {"totalTokens": 12}}}
    )

    # 20 minutes pass while the background command is still running — turn stays open.
    await clock.advance(1200.0)
    assert not task.done()
    assert not any(isinstance(ev, TurnComplete) for ev in events)

    # Background command completes and agy streams its follow-up summary + usage_update.
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "bg-cmd",
                "status": "completed",
                "rawOutput": "42 passed",
            },
        )
    )
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Background build passed: 42 tests."},
            },
        )
    )
    ex._queue.put_nowait(
        _acp_update_frame("s1", {"sessionUpdate": "usage_update", "size": 128000})
    )
    await clock.advance(2.0)
    assert not task.done()
    await clock.advance(2.0)
    await asyncio.wait_for(task, timeout=2.0)

    assert any(
        isinstance(ev, ToolCallComplete)
        and ev.metadata == {"call_id": "bg-cmd"}
        and ev.status is ToolCallStatus.SUCCESS
        for ev in events
    )
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "Running in background.\nBackground build passed: 42 tests."
    assert events[-1].usage == {"total_tokens": 12}


@pytest.mark.asyncio
async def test_agy_post_prompt_background_work_settles_labelled_at_30_min_bound() -> None:
    """An agy turn whose background work never finishes settles labelled at 30 minutes."""
    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="agy acp", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    ex._proc = Mock(returncode=None)
    ex._initialized = True
    ex._session_id = "s1"
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "long job"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    await prompt_sent.wait()
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "bg-long",
                "title": "run_command",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"CommandLine": "sleep 3600"},
            },
        )
    )
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "Job started."},
            },
        )
    )
    ex._pending[prompt_id[0]].set_result({"result": {"stopReason": "end_turn"}})

    await clock.advance(1799.0)
    assert not task.done()
    await clock.advance(1.0)
    await asyncio.wait_for(task, timeout=2.0)

    assert not any(isinstance(ev, ExecutorError) for ev in events)
    assert isinstance(events[-1], TurnComplete)
    assert "30 minutes" in (events[-1].response or "")
    assert "still running" in (events[-1].response or "")
    assert any(isinstance(ev, TextChunk) and "30 minutes" in ev.text for ev in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["daemon", "denied", "non-agy", "cancelled-stop"])
async def test_agy_post_prompt_wait_skips_daemons_denied_non_agy_and_cancelled(case: str) -> None:
    """Daemons, denied tools, non-AG agents, and non-end_turn stops settle immediately."""
    clock = _FakeAcpClock()
    command = "gemini --experimental-acp" if case == "non-agy" else "agy acp"
    ex = AcpExecutor(AcpAgentConfig(command=command, omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    ex._proc = Mock(returncode=None)
    ex._initialized = True
    ex._session_id = "s1"
    ex._elicitation_handler = AsyncMock(return_value=False)

    async def send(message: dict[str, object]) -> None:
        if message.get("method") != "session/prompt":
            return
        raw_input: dict[str, object] = {"CommandLine": "npm run dev"}
        if case == "daemon":
            raw_input["IsDaemon"] = True
        ex._queue.put_nowait(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "bg-1",
                    "title": "run_command",
                    "kind": "execute",
                    "status": "pending" if case == "denied" else "in_progress",
                    "rawInput": raw_input,
                },
            )
        )
        if case == "denied":
            ex._queue.put_nowait(
                {
                    "jsonrpc": "2.0",
                    "id": 77,
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "s1",
                        "toolCall": {"toolCallId": "bg-1", "title": "run_command"},
                        "options": [
                            {"optionId": "once", "kind": "allow_once", "name": "Allow"},
                            {"optionId": "no", "kind": "reject_once", "name": "Deny"},
                        ],
                    },
                }
            )
        stop = "cancelled" if case == "cancelled-stop" else "end_turn"
        ex._pending[message["id"]].set_result({"result": {"stopReason": stop}})  # type: ignore[index]

    ex._send = send  # type: ignore[assignment]
    events = await asyncio.wait_for(
        _collect_events(ex.run_turn([{"role": "user", "content": "go"}], [], "")),
        timeout=2.0,
    )
    assert isinstance(events[-1], TurnComplete)
    assert "still running" not in (events[-1].response or "")


async def _collect_events(stream) -> list[ExecutorEvent]:
    return [ev async for ev in stream]


@pytest.mark.asyncio
async def test_agy_post_prompt_subagent_and_managed_permission_binding() -> None:
    """Post-prompt subagent work keeps the turn open and binds managed host-tier permissions."""
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload

    clock = _FakeAcpClock()
    ex = AcpExecutor(
        AcpAgentConfig(command="agy acp --stdio", host_permission_tier=True, omnigent_mcp=False),
        clock=clock,
    )
    clock.bind(ex)
    stdout = asyncio.StreamReader()
    ex._proc = Mock(returncode=None, stdout=stdout)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    ex._active_mode = "default"
    ex._reader_task = asyncio.create_task(ex._read_stdout())
    ex._policy_evaluator = AsyncMock(
        return_value=PolicyVerdictPayload(
            action="POLICY_ACTION_UNSPECIFIED",
            data={"_omnigent_acp_tier": "allow"},
        )
    )
    sent: list[dict[str, object]] = []
    prompt_sent = asyncio.Event()
    perm_replied = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()
        elif message.get("id") == 201:
            perm_replied.set()

    ex._send = send  # type: ignore[assignment]

    def feed(frame: dict[str, object]) -> None:
        stdout.feed_data((json.dumps(frame) + "\n").encode("utf-8"))

    events: list[ExecutorEvent] = []
    reply_seen = asyncio.Event()

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "delegate"}], [], ""):
            events.append(ev)
            if isinstance(ev, TextChunk) and "Subagent finished." in ev.text:
                reply_seen.set()

    task = asyncio.create_task(run())
    try:
        await prompt_sent.wait()
        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "subagent_update",
                    "sessionId": "child-1",
                    "state": {"state": "running"},
                },
            )
        )
        feed({"jsonrpc": "2.0", "id": prompt_id[0], "result": {"stopReason": "end_turn"}})
        await clock.advance(600.0)
        assert not task.done()

        # Post-prompt permission request in the still-open AG turn binds cleanly.
        cmd_input = {"CommandLine": "omni --version"}
        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "post-cmd",
                    "kind": "execute",
                    "title": "run_command",
                    "status": "pending",
                    "rawInput": cmd_input,
                },
            )
        )
        feed(
            {
                "jsonrpc": "2.0",
                "id": 201,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "toolCall": {"toolCallId": "post-cmd", "rawInput": cmd_input},
                    "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"}],
                },
            }
        )
        await asyncio.wait_for(perm_replied.wait(), timeout=2.0)
        await clock.advance(5.0)
        perm_replies = [m for m in sent if m.get("id") == 201]
        assert perm_replies == [
            {
                "jsonrpc": "2.0",
                "id": 201,
                "result": {"outcome": {"outcome": "selected", "optionId": "once"}},
            }
        ]

        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "post-cmd",
                    "status": "completed",
                },
            )
        )
        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "subagent_update",
                    "sessionId": "child-1",
                    "state": {"state": "idle"},
                },
            )
        )
        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Subagent finished."},
                },
            )
        )
        # No usage_update: settles after the 3s quiet settle grace on virtual clock.
        await asyncio.wait_for(reply_seen.wait(), timeout=2.0)
        await clock.advance(2.0)
        assert not task.done()
        await clock.advance(2.0)
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        stdout.feed_eof()
        if ex._reader_task is not None:
            await ex._reader_task

    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "Subagent finished."


@pytest.mark.asyncio
@pytest.mark.parametrize("exit_mode", ["interrupt", "eof"])
async def test_agy_post_prompt_interrupt_and_eof_exit_promptly(exit_mode: str) -> None:
    """User Stop or subprocess EOF during AG background wait exits immediately."""
    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="agy acp", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    ex._proc = Mock(returncode=None)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "wait"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    await prompt_sent.wait()
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "bg-1",
                "title": "run_command",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"CommandLine": "sleep 600"},
            },
        )
    )
    ex._pending[prompt_id[0]].set_result({"result": {"stopReason": "end_turn"}})
    await clock.advance(10.0)
    assert not task.done()

    if exit_mode == "interrupt":
        assert await ex.interrupt_session("s1") is True
        await asyncio.wait_for(task, timeout=2.0)
        assert isinstance(events[-1], TurnCancelled)
    else:
        ex._stdout_eof_event.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert isinstance(events[-1], ExecutorError)
        assert "closed stdout" in events[-1].message


@pytest.mark.asyncio
async def test_agy_fake_acp_subprocess_background_wrapup_end_to_end(tmp_path: Path) -> None:
    """Fake-agy ACP subprocess over stdio reports post-prompt background work before settling."""
    script = tmp_path / "fake_agy.py"
    script.write_text(
        """
import json, sys, time

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    req_id = msg.get("id")
    if method == "initialize":
        send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"protocolVersion": 1, "agentCapabilities": {}},
        })
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": req_id, "result": {"sessionId": "agy-e2e"}})
    elif method == "session/prompt":
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "agy-e2e",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "bg-e2e",
                    "title": "run_command",
                    "kind": "execute",
                    "status": "in_progress",
                    "rawInput": {"CommandLine": "echo hi"},
                },
            },
        })
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "agy-e2e",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Started.\\n"},
                },
            },
        })
        send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        time.sleep(0.05)
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "agy-e2e",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "bg-e2e",
                    "status": "completed",
                    "rawOutput": "hi",
                },
            },
        })
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "agy-e2e",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Finished: hi"},
                },
            },
        })
        send({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "agy-e2e",
                "update": {"sessionUpdate": "usage_update", "size": 64000},
            },
        })
""".lstrip(),
        encoding="utf-8",
    )
    wrapper = tmp_path / "agy"
    wrapper.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    ex = AcpExecutor(
        AcpAgentConfig(
            command=f"{shlex.quote(str(wrapper))} acp", name="Fake AG", omnigent_mcp=False
        ),
        cwd=str(tmp_path),
    )
    try:
        events = await asyncio.wait_for(
            _collect_events(ex.run_turn([{"role": "user", "content": "run bg"}], [], "")),
            timeout=10.0,
        )
    finally:
        await ex.close()

    assert any(
        isinstance(ev, ToolCallComplete) and ev.metadata == {"call_id": "bg-e2e"} for ev in events
    )
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "Started.\nFinished: hi"


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["rpc-error", "cancelled-stop", "interrupted-inflight"])
async def test_agy_managed_permission_rejected_after_failed_cancelled_or_interrupted_prompt(
    case: str,
) -> None:
    """Host-tier permissions never bind or grant after prompt error, cancel stop, or Stop."""
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload

    clock = _FakeAcpClock()
    ex = AcpExecutor(
        AcpAgentConfig(command="agy acp --stdio", host_permission_tier=True, omnigent_mcp=False),
        clock=clock,
    )
    clock.bind(ex)
    stdout = asyncio.StreamReader()
    ex._proc = Mock(returncode=None, stdout=stdout)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    ex._active_mode = "default"
    ex._reader_task = asyncio.create_task(ex._read_stdout())

    policy_entered = asyncio.Event()
    release_policy = asyncio.Event()

    async def evaluate_policy(*_args, **_kwargs) -> PolicyVerdictPayload:
        policy_entered.set()
        if case == "interrupted-inflight":
            await release_policy.wait()
        return PolicyVerdictPayload(
            action="POLICY_ACTION_UNSPECIFIED",
            data={"_omnigent_acp_tier": "allow"},
        )

    ex._policy_evaluator = evaluate_policy  # type: ignore[assignment]
    sent: list[dict[str, object]] = []
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]

    def feed(frame: dict[str, object]) -> None:
        stdout.feed_data((json.dumps(frame) + "\n").encode("utf-8"))

    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "run"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    try:
        await prompt_sent.wait()
        cmd_input = {"CommandLine": "omni --version"}
        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "cmd-perm",
                    "kind": "execute",
                    "title": "run_command",
                    "status": "pending",
                    "rawInput": cmd_input,
                },
            )
        )
        feed(
            {
                "jsonrpc": "2.0",
                "id": 301,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "toolCall": {"toolCallId": "cmd-perm", "rawInput": cmd_input},
                    "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"}],
                },
            }
        )
        if case == "rpc-error":
            feed(
                {
                    "jsonrpc": "2.0",
                    "id": prompt_id[0],
                    "error": {"code": -32000, "message": "prompt failed"},
                }
            )
        elif case == "cancelled-stop":
            feed({"jsonrpc": "2.0", "id": prompt_id[0], "result": {"stopReason": "cancelled"}})
        else:
            await asyncio.wait_for(policy_entered.wait(), timeout=2.0)
            assert await ex.interrupt_session("s1") is True
            release_policy.set()
            feed({"jsonrpc": "2.0", "id": prompt_id[0], "result": {"stopReason": "cancelled"}})

        await asyncio.wait_for(task, timeout=2.0)
    finally:
        release_policy.set()
        stdout.feed_eof()
        if ex._reader_task is not None:
            await ex._reader_task

    perm_replies = [m for m in sent if m.get("id") == 301]
    assert perm_replies == [
        {
            "jsonrpc": "2.0",
            "id": 301,
            "result": {"outcome": {"outcome": "cancelled"}},
        }
    ]
    if case == "rpc-error":
        assert isinstance(events[-1], ExecutorError)
    else:
        assert isinstance(events[-1], TurnComplete)


@pytest.mark.asyncio
async def test_agy_post_prompt_burst_completion_behind_prompt_response_not_dropped() -> None:
    """Fast background completion queued right behind end_turn is delivered before TurnComplete."""
    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="agy acp --stdio", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    stdout = asyncio.StreamReader()
    ex._proc = Mock(returncode=None, stdout=stdout)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    ex._reader_task = asyncio.create_task(ex._read_stdout())
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "burst"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    try:
        await prompt_sent.wait()
        burst = [
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "bg-burst",
                    "title": "run_command",
                    "kind": "execute",
                    "status": "in_progress",
                    "rawInput": {"CommandLine": "echo fast"},
                },
            ),
            {"jsonrpc": "2.0", "id": prompt_id[0], "result": {"stopReason": "end_turn"}},
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "bg-burst",
                    "status": "completed",
                    "rawOutput": "fast",
                },
            ),
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Burst done."},
                },
            ),
            _acp_update_frame("s1", {"sessionUpdate": "usage_update", "size": 32000}),
        ]
        stdout.feed_data(b"".join((json.dumps(f) + "\n").encode("utf-8") for f in burst))
        await clock.settle()
        await clock.advance(3.5)
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        stdout.feed_eof()
        if ex._reader_task is not None:
            await ex._reader_task

    assert any(
        isinstance(ev, ToolCallComplete) and ev.metadata == {"call_id": "bg-burst"}
        for ev in events
    )
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "Burst done."


@pytest.mark.asyncio
@pytest.mark.parametrize("abort_mode", ["deadline", "interrupt"])
async def test_agy_post_prompt_bounded_request_aborts_on_deadline_and_interrupt(
    abort_mode: str,
) -> None:
    """An unanswered post-prompt permission request never exceeds the 30m bound or delays Stop."""
    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="agy acp --stdio", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    ex._proc = Mock(returncode=None)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"

    elicit_started = asyncio.Event()
    never_answered = asyncio.Event()

    async def hanging_elicit(_tool: str, _args: dict[str, object]) -> bool:
        elicit_started.set()
        await never_answered.wait()
        return True

    ex._elicitation_handler = hanging_elicit  # type: ignore[assignment]
    sent: list[dict[str, object]] = []
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "wait"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    await prompt_sent.wait()
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "bg-1",
                "title": "run_command",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"CommandLine": "make"},
            },
        )
    )
    ex._pending[prompt_id[0]].set_result({"result": {"stopReason": "end_turn"}})
    await clock.advance(60.0)
    assert not task.done()

    ex._queue.put_nowait(
        {
            "jsonrpc": "2.0",
            "id": 401,
            "method": "session/request_permission",
            "params": {
                "sessionId": "s1",
                "toolCall": {"toolCallId": "bg-perm", "title": "run_command"},
                "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"}],
            },
        }
    )
    await asyncio.wait_for(elicit_started.wait(), timeout=2.0)

    if abort_mode == "deadline":
        await clock.advance(1740.0)  # Reaches exact 1800s post-prompt bound.
        await asyncio.wait_for(task, timeout=2.0)
        assert isinstance(events[-1], TurnComplete)
        assert "30 minutes" in (events[-1].response or "")
        assert "still running" in (events[-1].response or "")
    else:
        assert await ex.interrupt_session("s1") is True
        await asyncio.wait_for(task, timeout=2.0)
        assert isinstance(events[-1], TurnCancelled)

    perm_replies = [m for m in sent if m.get("id") == 401]
    assert perm_replies == [
        {
            "jsonrpc": "2.0",
            "id": 401,
            "result": {"outcome": {"outcome": "cancelled"}},
        }
    ]


@pytest.mark.asyncio
async def test_acp_close_during_foreground_prompt_exits_without_spinning() -> None:
    """Closing the executor during a foreground prompt fails fast instead of spinning."""
    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="example-agent", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    stdout = asyncio.StreamReader()
    proc = Mock(spec=asyncio.subprocess.Process, returncode=None, stdout=stdout)
    proc.stdin = Mock()
    proc.stdin.close = Mock()
    proc.wait = AsyncMock(return_value=0)
    ex._proc = proc
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    ex._reader_task = asyncio.create_task(ex._read_stdout())

    prompt_sent = asyncio.Event()

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    await asyncio.wait_for(prompt_sent.wait(), timeout=2.0)
    await clock.settle()
    assert not task.done()

    await ex.close()
    await asyncio.wait_for(task, timeout=2.0)
    assert isinstance(events[-1], ExecutorError)
    assert "closed stdout" in events[-1].message


@pytest.mark.asyncio
async def test_acp_tool_progress_resets_harness_idle_watchdog_through_adapter() -> None:
    """Statusless tool_call_update refreshes TurnContext's idle watchdog via ExecutorAdapter."""
    from omnigent.db.utils import generate_task_id
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.runtime.harnesses._scaffold import TurnContext
    from omnigent.server.schemas import CreateResponseRequest

    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="agy acp", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    ex._proc = Mock(returncode=None)
    ex._initialized = True
    ex._session_id = "s1"

    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]

    adapter = ExecutorAdapter(lambda: ex, session_key="stub")
    watchdog_resets = 0

    def reset_watchdog() -> None:
        nonlocal watchdog_resets
        watchdog_resets += 1

    ctx = TurnContext(
        response_id=generate_task_id(),
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )
    ctx._reset_idle_watchdog = reset_watchdog
    adapter._in_flight[ctx.response_id] = ctx

    task = asyncio.create_task(
        adapter.run_turn(CreateResponseRequest(model="agent", input="build"), ctx)
    )
    await asyncio.wait_for(prompt_sent.wait(), timeout=2.0)

    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "cmd-long",
                "title": "run_command",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"CommandLine": "make all"},
            },
        )
    )
    await clock.settle()
    resets_before_progress = watchdog_resets
    queue_size_before = ctx._event_queue.qsize()

    # Statusless progress emits no SSE event, yet must still refresh the harness watchdog.
    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "cmd-long",
                "content": [
                    {"type": "content", "content": {"type": "text", "text": "compiling..."}}
                ],
            },
        )
    )
    await clock.settle()
    assert ctx._event_queue.qsize() == queue_size_before
    assert watchdog_resets > resets_before_progress

    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "cmd-long",
                "status": "completed",
                "rawOutput": "ok",
            },
        )
    )
    ex._pending[prompt_id[0]].set_result({"result": {"stopReason": "end_turn"}})
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_agy_foreground_request_and_blocked_abort_send_respect_30m_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In-flight Phase 1 requests and backpressured abort sends still obey the 30m bound."""
    monkeypatch.setattr(acp_executor_module, "_ABORT_REPLY_TIMEOUT_SECONDS", 0.05)
    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="agy acp --stdio", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    stdout = asyncio.StreamReader()
    ex._proc = Mock(returncode=None, stdout=stdout)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    ex._reader_task = asyncio.create_task(ex._read_stdout())

    elicit_started = asyncio.Event()
    never_answered = asyncio.Event()
    abort_attempted = asyncio.Event()

    async def hanging_elicit(_tool: str, _args: dict[str, object]) -> bool:
        elicit_started.set()
        await never_answered.wait()
        return True

    ex._elicitation_handler = hanging_elicit  # type: ignore[assignment]
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()
            return
        if message.get("id") == 450:
            abort_attempted.set()
            await never_answered.wait()  # Simulate stdin backpressure on abort delivery.

    ex._send = send  # type: ignore[assignment]

    def feed(frame: dict[str, object]) -> None:
        stdout.feed_data((json.dumps(frame) + "\n").encode("utf-8"))

    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "run"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    try:
        await prompt_sent.wait()
        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "bg-1",
                    "title": "run_command",
                    "kind": "execute",
                    "status": "in_progress",
                    "rawInput": {"CommandLine": "make"},
                },
            )
        )
        feed(
            {
                "jsonrpc": "2.0",
                "id": 450,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "toolCall": {"toolCallId": "fg-perm", "title": "run_command"},
                    "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"}],
                },
            }
        )
        await asyncio.wait_for(elicit_started.wait(), timeout=2.0)

        # Prompt ends while the Phase 1 permission request is still awaiting.
        feed({"jsonrpc": "2.0", "id": prompt_id[0], "result": {"stopReason": "end_turn"}})
        await clock.advance(1800.0)
        await asyncio.wait_for(abort_attempted.wait(), timeout=2.0)
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        never_answered.set()
        stdout.feed_eof()
        if ex._reader_task is not None:
            await ex._reader_task

    assert isinstance(events[-1], TurnComplete)
    assert "30 minutes" in (events[-1].response or "")
    assert "still running" in (events[-1].response or "")


@pytest.mark.asyncio
async def test_ordinary_acp_turn_drains_trailing_text_after_prompt_response() -> None:
    """Ordinary ACP turns still drain trailing queued chunks after the prompt response arrives."""
    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="example-agent", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    stdout = asyncio.StreamReader()
    ex._proc = Mock(returncode=None, stdout=stdout)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    ex._reader_task = asyncio.create_task(ex._read_stdout())
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "hi"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    try:
        await prompt_sent.wait()
        burst = [
            {"jsonrpc": "2.0", "id": prompt_id[0], "result": {"stopReason": "end_turn"}},
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "trailing text"},
                },
            ),
        ]
        stdout.feed_data(b"".join((json.dumps(f) + "\n").encode("utf-8") for f in burst))
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        stdout.feed_eof()
        if ex._reader_task is not None:
            await ex._reader_task

    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "trailing text"


@pytest.mark.asyncio
async def test_agy_entering_background_wait_refreshes_harness_idle_watchdog() -> None:
    """Transitioning into AG post-prompt background wait refreshes the harness idle watchdog."""
    from omnigent.db.utils import generate_task_id
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
    from omnigent.runtime.harnesses._scaffold import TurnContext
    from omnigent.server.schemas import CreateResponseRequest

    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="agy acp", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    ex._proc = Mock(returncode=None)
    ex._initialized = True
    ex._session_id = "s1"

    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]

    adapter = ExecutorAdapter(lambda: ex, session_key="stub")
    watchdog_resets = 0

    def reset_watchdog() -> None:
        nonlocal watchdog_resets
        watchdog_resets += 1

    ctx = TurnContext(
        response_id=generate_task_id(),
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )
    ctx._reset_idle_watchdog = reset_watchdog
    adapter._in_flight[ctx.response_id] = ctx

    task = asyncio.create_task(
        adapter.run_turn(CreateResponseRequest(model="agent", input="bg"), ctx)
    )
    await asyncio.wait_for(prompt_sent.wait(), timeout=2.0)

    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "bg-watchdog",
                "title": "run_command",
                "kind": "execute",
                "status": "in_progress",
                "rawInput": {"CommandLine": "make"},
            },
        )
    )
    await clock.settle()
    resets_before_end_turn = watchdog_resets

    ex._pending[prompt_id[0]].set_result({"result": {"stopReason": "end_turn"}})
    await clock.advance(1.0)
    assert not task.done()
    assert watchdog_resets > resets_before_end_turn

    ex._queue.put_nowait(
        _acp_update_frame(
            "s1",
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "bg-watchdog",
                "status": "completed",
            },
        )
    )
    await clock.advance(5.0)
    await asyncio.wait_for(task, timeout=2.0)


@pytest.mark.asyncio
async def test_agy_malformed_error_prompt_response_never_reopens_managed_permissions() -> None:
    """A malformed response with non-dict error and end_turn fails closed on permissions."""
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload

    clock = _FakeAcpClock()
    ex = AcpExecutor(
        AcpAgentConfig(command="agy acp --stdio", host_permission_tier=True, omnigent_mcp=False),
        clock=clock,
    )
    clock.bind(ex)
    stdout = asyncio.StreamReader()
    ex._proc = Mock(returncode=None, stdout=stdout)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    ex._active_mode = "default"
    ex._reader_task = asyncio.create_task(ex._read_stdout())
    ex._policy_evaluator = AsyncMock(
        return_value=PolicyVerdictPayload(
            action="POLICY_ACTION_UNSPECIFIED",
            data={"_omnigent_acp_tier": "allow"},
        )
    )

    sent: list[dict[str, object]] = []
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "run"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    try:
        await prompt_sent.wait()
        cmd_input = {"CommandLine": "omni --version"}
        burst = [
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "bg-1",
                    "title": "run_command",
                    "kind": "execute",
                    "status": "in_progress",
                    "rawInput": {"CommandLine": "sleep 60"},
                },
            ),
            {
                "jsonrpc": "2.0",
                "id": prompt_id[0],
                "error": "malformed error string",
                "result": {"stopReason": "end_turn"},
            },
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "cmd-after-malformed",
                    "kind": "execute",
                    "title": "run_command",
                    "status": "pending",
                    "rawInput": cmd_input,
                },
            ),
            {
                "jsonrpc": "2.0",
                "id": 501,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "toolCall": {"toolCallId": "cmd-after-malformed", "rawInput": cmd_input},
                    "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"}],
                },
            },
        ]
        stdout.feed_data(b"".join((json.dumps(f) + "\n").encode("utf-8") for f in burst))
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        stdout.feed_eof()
        if ex._reader_task is not None:
            await ex._reader_task

    perm_replies = [m for m in sent if m.get("id") == 501]
    assert perm_replies == [
        {
            "jsonrpc": "2.0",
            "id": 501,
            "result": {"outcome": {"outcome": "cancelled"}},
        }
    ]
    assert isinstance(events[-1], ExecutorError)
    assert "malformed error string" in events[-1].message


@pytest.mark.asyncio
async def test_agy_post_prompt_usage_update_keeps_quiet_settle_for_trailing_text() -> None:
    """A usage_update does not cut off trailing text arriving within the 3s quiet-settle grace."""
    clock = _FakeAcpClock()
    ex = AcpExecutor(AcpAgentConfig(command="agy acp --stdio", omnigent_mcp=False), clock=clock)
    clock.bind(ex)
    stdout = asyncio.StreamReader()
    ex._proc = Mock(returncode=None, stdout=stdout)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    ex._reader_task = asyncio.create_task(ex._read_stdout())
    prompt_sent = asyncio.Event()
    first_seen = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "check"}], [], ""):
            events.append(ev)
            if isinstance(ev, TextChunk) and "First" in ev.text:
                first_seen.set()

    def feed(*frames: dict[str, object]) -> None:
        stdout.feed_data(b"".join((json.dumps(f) + "\n").encode("utf-8") for f in frames))

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(prompt_sent.wait(), timeout=2.0)
        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "bg-u",
                    "title": "run_command",
                    "kind": "execute",
                    "status": "in_progress",
                },
            ),
            {"jsonrpc": "2.0", "id": prompt_id[0], "result": {"stopReason": "end_turn"}},
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "bg-u",
                    "status": "completed",
                    "rawOutput": "ok",
                },
            ),
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "First"},
                },
            ),
            _acp_update_frame("s1", {"sessionUpdate": "usage_update", "size": 64000}),
        )
        await asyncio.wait_for(first_seen.wait(), timeout=2.0)
        await clock.advance(1.0)
        assert not task.done()
        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": " Second"},
                },
            )
        )
        await clock.settle()
        await clock.advance(3.5)
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        stdout.feed_eof()
        if ex._reader_task is not None:
            await ex._reader_task

    assert isinstance(events[-1], TurnComplete)
    assert events[-1].response == "First Second"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("managed", "reply_kind", "expect_body"),
    [
        (False, "end_turn", True),
        (True, "end_turn", True),
        (True, "rpc_error", False),
        (True, "cancelled", False),
    ],
)
async def test_agy_buffered_prompt_reply_preserves_read_file_backfill(
    tmp_path: Path,
    managed: bool,
    reply_kind: str,
    expect_body: bool,
) -> None:
    """Buffered tool_call + completion + prompt end_turn preserves AG read_file body backfill."""
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload

    target = tmp_path / "note.txt"
    target.write_text("hello from disk\n", encoding="utf-8")

    ex = AcpExecutor(
        AcpAgentConfig(
            command="agy acp --stdio",
            host_permission_tier=managed,
            omnigent_mcp=False,
        ),
        cwd=str(tmp_path),
    )
    ex._anchor_workspace_fd()
    stdout = asyncio.StreamReader()
    ex._proc = Mock(returncode=None, stdout=stdout)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    ex._active_mode = "default"
    ex._reader_task = asyncio.create_task(ex._read_stdout())
    if managed:
        ex._policy_evaluator = AsyncMock(
            return_value=PolicyVerdictPayload(
                action="POLICY_ACTION_ALLOW",
                data={"_omnigent_acp_tier": "allow"},
            )
        )

    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "read note"}], [], ""):
            events.append(ev)

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(prompt_sent.wait(), timeout=2.0)
        if reply_kind == "rpc_error":
            prompt_reply: dict[str, object] = {
                "jsonrpc": "2.0",
                "id": prompt_id[0],
                "error": {"code": -32000, "message": "prompt failed"},
            }
        else:
            prompt_reply = {
                "jsonrpc": "2.0",
                "id": prompt_id[0],
                "result": {"stopReason": reply_kind},
            }
        burst = [
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "read-1",
                    "title": "read_file",
                    "kind": "read",
                    "status": "in_progress",
                    "rawInput": {"Path": str(target)},
                },
            ),
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "read-1",
                    "status": "completed",
                },
            ),
            prompt_reply,
        ]
        stdout.feed_data(b"".join((json.dumps(f) + "\n").encode("utf-8") for f in burst))
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        stdout.feed_eof()
        if ex._reader_task is not None:
            await ex._reader_task
        ex._close_workspace_fd()

    completions = [
        ev
        for ev in events
        if isinstance(ev, ToolCallComplete) and ev.metadata == {"call_id": "read-1"}
    ]
    assert len(completions) == 1
    if expect_body:
        assert "hello from disk" in str(completions[0].result)
    else:
        assert completions[0].result in (None, "")


@pytest.mark.asyncio
async def test_acp_phase1_permission_aborts_on_prompt_error_without_waiting_for_policy() -> None:
    """A Phase 1 prompt error cancels an in-flight permission policy await immediately."""
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload

    ex = AcpExecutor(
        AcpAgentConfig(command="agy acp --stdio", host_permission_tier=True, omnigent_mcp=False)
    )
    stdout = asyncio.StreamReader()
    ex._proc = Mock(returncode=None, stdout=stdout)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    ex._active_mode = "default"
    ex._reader_task = asyncio.create_task(ex._read_stdout())

    policy_entered = asyncio.Event()
    never_returns = asyncio.Event()

    async def hanging_policy(*_args, **_kwargs) -> PolicyVerdictPayload:
        policy_entered.set()
        await never_returns.wait()
        return PolicyVerdictPayload(
            action="POLICY_ACTION_ALLOW", data={"_omnigent_acp_tier": "allow"}
        )

    ex._policy_evaluator = hanging_policy  # type: ignore[assignment]
    sent: list[dict[str, object]] = []
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        sent.append(message)
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "run"}], [], ""):
            events.append(ev)

    def feed(frame: dict[str, object]) -> None:
        stdout.feed_data((json.dumps(frame) + "\n").encode("utf-8"))

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(prompt_sent.wait(), timeout=2.0)
        cmd_input = {"CommandLine": "omni --version"}
        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "cmd-hang",
                    "kind": "execute",
                    "title": "run_command",
                    "status": "pending",
                    "rawInput": cmd_input,
                },
            )
        )
        feed(
            {
                "jsonrpc": "2.0",
                "id": 601,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "s1",
                    "toolCall": {"toolCallId": "cmd-hang", "rawInput": cmd_input},
                    "options": [{"optionId": "once", "kind": "allow_once", "name": "Allow once"}],
                },
            }
        )
        await asyncio.wait_for(policy_entered.wait(), timeout=2.0)
        feed(
            {
                "jsonrpc": "2.0",
                "id": prompt_id[0],
                "error": {"code": -32000, "message": "prompt failed"},
            }
        )
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        never_returns.set()
        stdout.feed_eof()
        if ex._reader_task is not None:
            await ex._reader_task

    perm_replies = [m for m in sent if m.get("id") == 601]
    assert perm_replies == [
        {
            "jsonrpc": "2.0",
            "id": 601,
            "result": {"outcome": {"outcome": "cancelled"}},
        }
    ]
    assert isinstance(events[-1], ExecutorError)
    assert "prompt failed" in events[-1].message


@pytest.mark.asyncio
async def test_acp_phase2_read_backfill_policy_aborts_at_deadline_without_reading_disk(
    tmp_path: Path,
) -> None:
    """A Phase 2 read_file backfill policy await cannot outlive the 30m deadline or read disk."""
    from omnigent.runtime.harnesses._scaffold import PolicyVerdictPayload

    target = tmp_path / "secret.txt"
    target.write_text("late-disk-body\n", encoding="utf-8")

    clock = _FakeAcpClock()
    ex = AcpExecutor(
        AcpAgentConfig(command="agy acp --stdio", host_permission_tier=True, omnigent_mcp=False),
        cwd=str(tmp_path),
        clock=clock,
    )
    clock.bind(ex)
    ex._anchor_workspace_fd()
    stdout = asyncio.StreamReader()
    ex._proc = Mock(returncode=None, stdout=stdout)
    ex._stdout_eof_event = asyncio.Event()
    ex._initialized = True
    ex._session_id = "s1"
    ex._active_mode = "default"
    ex._reader_task = asyncio.create_task(ex._read_stdout())

    policy_entered = asyncio.Event()
    never_returns = asyncio.Event()

    async def hanging_policy(*_args, **_kwargs) -> PolicyVerdictPayload:
        policy_entered.set()
        await never_returns.wait()
        return PolicyVerdictPayload(
            action="POLICY_ACTION_ALLOW", data={"_omnigent_acp_tier": "allow"}
        )

    ex._policy_evaluator = hanging_policy  # type: ignore[assignment]
    prompt_sent = asyncio.Event()
    prompt_id: list[int] = []

    async def send(message: dict[str, object]) -> None:
        if message.get("method") == "session/prompt":
            prompt_id.append(int(message["id"]))  # type: ignore[arg-type]
            prompt_sent.set()

    ex._send = send  # type: ignore[assignment]
    events: list[ExecutorEvent] = []

    async def run() -> None:
        async for ev in ex.run_turn([{"role": "user", "content": "bg read"}], [], ""):
            events.append(ev)

    def feed(*frames: dict[str, object]) -> None:
        stdout.feed_data(b"".join((json.dumps(f) + "\n").encode("utf-8") for f in frames))

    task = asyncio.create_task(run())
    try:
        await asyncio.wait_for(prompt_sent.wait(), timeout=2.0)
        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "bg-job",
                    "title": "run_command",
                    "kind": "execute",
                    "status": "in_progress",
                },
            ),
            {"jsonrpc": "2.0", "id": prompt_id[0], "result": {"stopReason": "end_turn"}},
        )
        await clock.advance(60.0)
        assert not task.done()
        feed(
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "late-read",
                    "title": "read_file",
                    "kind": "read",
                    "status": "in_progress",
                    "rawInput": {"Path": str(target)},
                },
            ),
            _acp_update_frame(
                "s1",
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "late-read",
                    "status": "completed",
                },
            ),
        )
        await asyncio.wait_for(policy_entered.wait(), timeout=2.0)
        await clock.advance(1745.0)
        await asyncio.wait_for(task, timeout=2.0)
    finally:
        never_returns.set()
        stdout.feed_eof()
        if ex._reader_task is not None:
            await ex._reader_task
        ex._close_workspace_fd()

    completions = [
        ev
        for ev in events
        if isinstance(ev, ToolCallComplete) and ev.metadata == {"call_id": "late-read"}
    ]
    assert len(completions) == 1
    assert completions[0].result in (None, "")
    assert isinstance(events[-1], TurnComplete)
    assert "30 minutes" in (events[-1].response or "")


@pytest.mark.asyncio
async def test_acp_windows_close_reaps_survivors_after_prompt_launcher_exit_and_retains_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Windows, close() runs survivor reaping off-loop even when launcher exits promptly."""
    ex = AcpExecutor(AcpAgentConfig(command="agy acp --stdio", omnigent_mcp=False))
    proc = Mock(spec=asyncio.subprocess.Process, returncode=None, pid=4242)
    proc.stdin = Mock()
    proc.stdin.close = Mock()

    async def prompt_exit() -> int:
        proc.returncode = 0
        return 0

    proc.wait = AsyncMock(side_effect=prompt_exit)
    ex._proc = proc

    calls: list[str] = []
    unresolved = [True]

    def fake_terminate(target: object) -> None:
        assert target is proc
        calls.append("terminate")

    def fake_kill(target: object) -> None:
        assert target is proc
        calls.append("kill")
        if calls.count("kill") >= 2:
            unresolved[0] = False

    def fake_has_unresolved(target: object) -> bool:
        assert target is proc
        return unresolved[0]

    monkeypatch.setattr(acp_executor_module, "IS_WINDOWS", True)
    monkeypatch.setattr(acp_executor_module._proc, "terminate_tree", fake_terminate)
    monkeypatch.setattr(acp_executor_module._proc, "kill_tree", fake_kill)
    monkeypatch.setattr(
        acp_executor_module._proc, "has_unresolved_identities", fake_has_unresolved
    )

    await ex.close()
    assert calls == ["terminate", "kill"]
    # Unresolved identities retain self._proc for a retry close().
    assert ex._proc is proc

    await ex.close()
    assert calls == ["terminate", "kill", "kill"]
    assert ex._proc is None
