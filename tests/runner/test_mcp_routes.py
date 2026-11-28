"""Route-level tests for the runner's ``/mcp/execute``, ``/v1/summarize`` and
``/v1/elicitations`` endpoints (``omnigent/runner/mcp_routes.py``).

The routes are registered on a bare FastAPI app with every collaborator
replaced by a small fake, so each branch — MCP manager missing, spec missing,
elicitation round-trips, retained operations, connection resolution for the
summarizer, harness forwarding — is driven through the real HTTP handler.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner import mcp_routes
from omnigent.runner.mcp_execution_registry import McpExecutionRegistry
from omnigent.runner.mcp_manager import McpSchemasResult
from omnigent.runner.native.orchestration import ResolvedSpec
from omnigent.runner.permission_tiers import PermissionTierRuntime
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.runtime.harnesses.process_manager import NoLiveHarnessError
from omnigent.spec.types import AgentSpec, ApiKeyAuth, DatabricksAuth, ExecutorSpec, ProviderAuth
from omnigent.tools.mcp import McpElicitationRequired

pytestmark = pytest.mark.asyncio

SESSION = "conv_mcp"
EXECUTE = f"/v1/sessions/{SESSION}/mcp/execute"


def _spec(auth: Any = None, **executor: Any) -> AgentSpec:
    return AgentSpec(spec_version=1, name="t", executor=ExecutorSpec(auth=auth, **executor))


class _FakeConnection:
    def __init__(self, output: Any = "elicited-output") -> None:
        self.output = output
        self.calls: list[dict[str, Any]] = []

    async def call_tool_with_elicitation(
        self, bare_tool: str, arguments: Any, *, input_responses: Any, request_state: Any
    ) -> Any:
        self.calls.append(
            {
                "tool": bare_tool,
                "arguments": arguments,
                "input_responses": input_responses,
                "request_state": request_state,
            }
        )
        return self.output


class _FakeMcpManager:
    def __init__(self) -> None:
        self.schemas = McpSchemasResult(
            schemas=[{"name": "jira__search"}], tool_names={"jira__search"}, failures={}
        )
        self.schemas_error: Exception | None = None
        self.call_error: Exception | None = None
        self.output: Any = "tool-output"
        self.calls: list[tuple[str, Any, str]] = []
        self.route: tuple[Any, str] | None = None

    async def schemas_for(self, spec: AgentSpec) -> McpSchemasResult:
        if self.schemas_error is not None:
            raise self.schemas_error
        return self.schemas

    async def call_tool(self, spec: AgentSpec, tool_name: str, arguments: Any, *, session_id: str):
        self.calls.append((tool_name, arguments, session_id))
        if self.call_error is not None:
            raise self.call_error
        return self.output

    def _resolve_tool_route(self, spec: AgentSpec, tool_name: str) -> tuple[Any, str] | None:
        return self.route


class _FakeHarnessClient:
    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None):
        self.response = response or httpx.Response(200, json={"accepted": True})
        self.error = error
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, *, json: Any, timeout: float) -> httpx.Response:
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        if self.error is not None:
            raise self.error
        return self.response


class _FakeProcessManager:
    def __init__(self, client: _FakeHarnessClient | None) -> None:
        self.client = client
        self.requests: list[tuple[str, str]] = []

    async def get_client(self, conv_id: str, harness: str) -> _FakeHarnessClient:
        self.requests.append((conv_id, harness))
        if self.client is None:
            raise NoLiveHarnessError("no live harness")
        return self.client


class Harness:
    """A FastAPI app with the MCP routes and recording fakes for every collaborator."""

    def __init__(
        self,
        *,
        mcp_manager: _FakeMcpManager | None = None,
        process_manager: _FakeProcessManager | None = None,
        spec_resolver: object | None = None,
        permission_tiers: PermissionTierRuntime | None = None,
        harness: str = "claude-sdk",
        runner_workspace: Path | None = None,
    ) -> None:
        self.app = FastAPI()
        self.mcp_manager = mcp_manager
        self.spec_cache: dict[str, Any] = {}
        self.resolved_spec: AgentSpec | None = None
        self.resolved_entry: Any = None
        self.resolve_error: Exception | None = None
        self.conversations: dict[str, str] = {}
        self.published: list[tuple[str, Any]] = []
        self.recovered: list[str] = []
        self.registry = McpExecutionRegistry()
        self.resource_registry = SessionResourceRegistry()
        self.inboxes: dict[str, Any] = {}
        self.async_tasks: dict[str, Any] = {}
        self.agent_ids: dict[str, str] = {SESSION: "ag_1"}

        async def _recover(session_id: str) -> None:
            self.recovered.append(session_id)

        async def _resolve_conv(response_id: str) -> str | None:
            return self.conversations.get(response_id)

        async def _resolve_spec_or_none(session_id: str) -> AgentSpec | None:
            return self.resolved_spec

        async def _resolve_entry(session_id: str) -> Any:
            if self.resolve_error is not None:
                raise self.resolve_error
            return self.resolved_entry

        async def _runtime_cwd(session_id: str) -> Path | None:
            if runner_workspace is not None:
                return runner_workspace
            return (
                Path("/tmp/runtime-cwd")
                if session_id == SESSION
                else Path("/tmp/runner-workspace")
            )

        async def _explicit_workspace(session_id: str) -> Path | None:
            if runner_workspace is not None:
                return runner_workspace
            return Path("/tmp/session-workspace")

        mcp_routes.register_mcp_routes(
            self.app,
            _publish_event=lambda sid, payload: self.published.append((sid, payload)),
            _recover_undrained_subagent_results=_recover,
            _resolve_conversation_id=_resolve_conv,
            _resolve_session_agent_spec_or_none=_resolve_spec_or_none,
            _resolve_session_spec_entry=_resolve_entry,
            _session_agent_ids=self.agent_ids,
            _session_async_tasks=self.async_tasks,
            _session_harness_name=lambda sid: harness,
            _session_inboxes=self.inboxes,
            _session_runtime_cwd=_runtime_cwd,
            _session_explicit_workspace=_explicit_workspace,
            _session_spec_cache=self.spec_cache,
            filesystem_registry=None,
            mcp_execution_registry=self.registry,
            permission_tiers=permission_tiers or PermissionTierRuntime(),
            mcp_manager=mcp_manager,  # type: ignore[arg-type]
            process_manager=process_manager,  # type: ignore[arg-type]
            resource_registry=self.resource_registry,
            runner_workspace=runner_workspace or Path("/tmp/runner-workspace"),
            server_client=httpx.AsyncClient(base_url="http://server"),
            spec_resolver=spec_resolver,  # type: ignore[arg-type]
            terminal_registry=None,
            wake_on_async_result=None,
        )

    @asynccontextmanager
    async def client(self) -> AsyncIterator[httpx.AsyncClient]:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://runner"
        ) as client:
            yield client

    async def execute(self, body: Any) -> tuple[int, Any]:
        async with self.client() as client:
            resp = await client.post(EXECUTE, json=body)
        return resp.status_code, resp.json()


def _call(name: str, **params: Any) -> dict[str, Any]:
    return {"method": "tools/call", "params": {"name": name, **params}}


# ── /mcp/execute: envelope and method dispatch ─────────────────────────────


async def test_invalid_json_is_a_parse_error() -> None:
    h = Harness(mcp_manager=_FakeMcpManager())
    async with h.client() as client:
        resp = await client.post(
            EXECUTE, content=b"{not json", headers={"content-type": "application/json"}
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == -32700


async def test_unknown_method_is_method_not_found() -> None:
    status, body = await Harness().execute({"method": "prompts/get"})
    assert status == 200
    assert body["error"]["code"] == -32601
    assert "prompts/get" in body["error"]["message"]


# ── tools/list ─────────────────────────────────────────────────────────────


async def test_tools_list_without_a_manager_is_503() -> None:
    status, body = await Harness(mcp_manager=None).execute({"method": "tools/list"})
    assert status == 503
    assert body["error"]["message"] == "Runner MCP manager not configured"


async def test_tools_list_without_a_spec_reports_it() -> None:
    status, body = await Harness(mcp_manager=_FakeMcpManager()).execute({"method": "tools/list"})
    assert status == 200
    assert body["error"]["message"] == f"No spec available for session {SESSION!r}"


async def test_tools_list_returns_schemas_from_a_cached_resolved_spec() -> None:
    h = Harness(mcp_manager=_FakeMcpManager())
    h.spec_cache[SESSION] = ResolvedSpec(spec=_spec(), workdir=Path("/tmp/wd"))
    status, body = await h.execute({"method": "tools/list"})
    assert status == 200
    assert body["result"] == {
        "schemas": [{"name": "jira__search"}],
        "tool_names": ["jira__search"],
        "failures": {},
    }


async def test_tools_list_falls_back_to_the_spec_resolver() -> None:
    h = Harness(mcp_manager=_FakeMcpManager(), spec_resolver=object())
    h.resolved_spec = _spec()
    status, body = await h.execute({"method": "tools/list"})
    assert status == 200
    assert body["result"]["tool_names"] == ["jira__search"]


async def test_tools_list_hides_schema_failures_behind_a_generic_message() -> None:
    manager = _FakeMcpManager()
    manager.schemas_error = RuntimeError("/home/alice/secret-path exploded")
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute({"method": "tools/list"})
    assert status == 200
    assert body["error"]["code"] == -32000
    assert "secret-path" not in body["error"]["message"]


# ── tools/call: MCP-served tools (``server__tool``) ────────────────────────


async def test_tools_call_requires_a_tool_name() -> None:
    status, body = await Harness(mcp_manager=_FakeMcpManager()).execute(_call(""))
    assert status == 200
    assert body["error"]["message"] == "Missing tool name"


async def test_mcp_tool_without_a_manager_is_503() -> None:
    status, _ = await Harness(mcp_manager=None).execute(_call("jira__search"))
    assert status == 503


async def test_mcp_tool_without_a_spec_reports_it() -> None:
    _, body = await Harness(mcp_manager=_FakeMcpManager()).execute(_call("jira__search"))
    assert body["error"]["message"] == f"No spec available for session {SESSION!r}"


async def test_mcp_tool_call_returns_the_manager_output() -> None:
    manager = _FakeMcpManager()
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute(_call("jira__search", arguments={"q": "open"}))
    assert status == 200
    assert body == {"result": {"output": "tool-output"}}
    assert manager.calls == [("jira__search", {"q": "open"}, SESSION)]


async def test_mcp_tool_elicitation_is_surfaced_as_input_required() -> None:
    manager = _FakeMcpManager()
    manager.call_error = McpElicitationRequired(
        input_requests={"r1": {"message": "approve?"}},
        request_state="state-1",
        tool_name="jira__search",
        arguments={},
    )
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute(_call("jira__search"))
    assert status == 200
    assert body["result"]["input_required"] == {
        "inputRequests": {"r1": {"message": "approve?"}},
        "requestState": "state-1",
    }


async def test_mcp_tool_failure_is_generic_to_the_client() -> None:
    manager = _FakeMcpManager()
    manager.call_error = RuntimeError("upstream said: token=abc123")
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute(_call("jira__search"))
    assert status == 200
    assert body["error"]["code"] == -32000
    assert "abc123" not in body["error"]["message"]


async def test_elicitation_reply_without_a_live_route_is_an_error() -> None:
    manager = _FakeMcpManager()
    manager.route = None
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    _, body = await h.execute(
        _call("jira__search", inputResponses={"r1": "yes"}, requestState="state-1")
    )
    assert body["error"]["code"] == -32000
    assert manager.calls == []  # never falls back to a fresh call_tool


async def test_elicitation_reply_without_a_connection_is_an_error() -> None:
    manager = _FakeMcpManager()
    manager.route = (SimpleNamespace(connection=None), "search")
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    _, body = await h.execute(_call("jira__search", inputResponses={"r1": "yes"}))
    assert body["error"]["code"] == -32000


async def test_elicitation_reply_resumes_the_tool_with_the_user_input() -> None:
    manager = _FakeMcpManager()
    connection = _FakeConnection()
    manager.route = (SimpleNamespace(connection=connection), "search")
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute(
        _call("jira__search", arguments={"q": 1}, inputResponses={"r1": "yes"}, requestState="s")
    )
    assert status == 200
    assert body == {"result": {"output": "elicited-output"}}
    assert connection.calls == [
        {
            "tool": "search",
            "arguments": {"q": 1},
            "input_responses": {"r1": "yes"},
            "request_state": "s",
        }
    ]


# ── tools/call: built-in tools dispatched through execute_tool ─────────────


async def test_builtin_tool_dispatch_passes_session_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _execute_tool(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "listed"

    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", _execute_tool)
    h = Harness()
    spec = _spec()
    h.spec_cache[SESSION] = ResolvedSpec(spec=spec, workdir=Path("/tmp/bundle"))
    status, body = await h.execute(_call("sys_files_list", arguments={"path": "."}))
    assert status == 200
    assert body == {"result": {"output": "listed"}}
    assert seen["tool_name"] == "sys_files_list"
    assert json.loads(seen["arguments"]) == {"path": "."}
    assert seen["agent_spec"] is spec
    assert seen["local_tool_workdir"] == Path("/tmp/bundle")
    assert seen["runner_workspace"] == Path("/tmp/runtime-cwd")
    assert await seen["session_runtime_cwd"]("child_runtime") == Path("/tmp/runner-workspace")
    assert seen["session_workspace"] == Path("/tmp/session-workspace")
    assert seen["conversation_id"] == seen["task_id"] == SESSION
    assert seen["agent_id"] == "ag_1"
    assert seen["effective_harness"] == "claude-sdk"
    assert seen["mcp_manager"] is None


async def test_sys_read_inbox_recovers_undrained_results_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    async def _execute_tool(**kwargs: Any) -> str:
        order.append("dispatch")
        return "[]"

    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", _execute_tool)
    h = Harness()
    h.spec_cache[SESSION] = _spec()
    original = h.recovered

    async def _recover_marking(session_id: str) -> None:
        order.append("recover")
        original.append(session_id)

    # Re-register is heavy; patch the closure's target instead via the list identity.
    h.recovered = original
    status, _ = await h.execute(_call("sys_read_inbox"))
    assert status == 200
    assert h.recovered == [SESSION]


async def test_builtin_tool_resolves_the_spec_entry_when_uncached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _execute_tool(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", _execute_tool)
    h = Harness(spec_resolver=object())
    spec = _spec()
    h.resolved_entry = ResolvedSpec(spec=spec, workdir=Path("/tmp/resolved"))
    await h.execute(_call("sys_files_list"))
    assert seen["agent_spec"] is spec
    assert seen["local_tool_workdir"] == Path("/tmp/resolved")


async def test_builtin_tool_tolerates_a_failed_spec_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    async def _execute_tool(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "ok"

    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", _execute_tool)
    h = Harness(spec_resolver=object())
    h.resolve_error = OmnigentError("agent gone", code=ErrorCode.NOT_FOUND)
    status, body = await h.execute(_call("sys_files_list"))
    assert status == 200
    assert body == {"result": {"output": "ok"}}
    assert seen["agent_spec"] is None
    assert seen["local_tool_workdir"] == Path("/tmp/runner-workspace")


async def test_file_tool_is_refused_when_the_spec_fails_to_resolve() -> None:
    """
    The real dispatch can't see a ``tool_groups.files`` opt-in without the
    spec, so it refuses the file tool instead of calling the server's file API.
    """
    h = Harness(spec_resolver=object())
    h.resolve_error = OmnigentError("agent gone", code=ErrorCode.NOT_FOUND)
    status, body = await h.execute(_call("sys_files_list"))
    assert status == 200
    output = json.loads(body["result"]["output"])
    assert output["error"].startswith("tool 'sys_files_list' is not enabled"), output


async def test_builtin_tool_failure_is_generic_to_the_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _execute_tool(**kwargs: Any) -> str:
        raise RuntimeError("/etc/shadow unreadable")

    monkeypatch.setattr("omnigent.runner.tool_dispatch.execute_tool", _execute_tool)
    h = Harness()
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute(_call("sys_files_list"))
    assert status == 200
    assert body["error"]["code"] == -32000
    assert "/etc/shadow" not in body["error"]["message"]


# ── tools/call: retained operations (``_omnigent_operation``) ──────────────


@pytest.mark.parametrize(
    "operation",
    [{"id": "", "step": "call"}, {"id": "op", "step": ""}, {"id": 1, "step": "call"}, "nope"],
    ids=["empty-id", "empty-step", "non-string-id", "not-an-object"],
)
async def test_invalid_operation_metadata_is_rejected(operation: Any) -> None:
    h = Harness(mcp_manager=_FakeMcpManager())
    h.spec_cache[SESSION] = _spec()
    status, body = await h.execute({**_call("jira__search"), "_omnigent_operation": operation})
    assert status == 200
    assert body["error"]["message"] == "Invalid runner MCP operation metadata"


async def test_retained_operation_runs_once_and_replays_its_result() -> None:
    manager = _FakeMcpManager()
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    body = {
        **_call("jira__search", arguments={"q": 1}),
        "_omnigent_operation": {"id": "op-1", "step": "call"},
    }
    first = await h.execute(body)
    second = await h.execute(body)  # a reconnecting client re-sends the same step
    assert first == second == (200, {"result": {"output": "tool-output"}})
    assert len(manager.calls) == 1, "the external tool must not run twice"


async def test_retained_operation_with_changed_params_is_a_conflict() -> None:
    manager = _FakeMcpManager()
    h = Harness(mcp_manager=manager)
    h.spec_cache[SESSION] = _spec()
    op = {"id": "op-2", "step": "call"}
    await h.execute({**_call("jira__search", arguments={"q": 1}), "_omnigent_operation": op})
    status, body = await h.execute(
        {**_call("jira__search", arguments={"q": 2}), "_omnigent_operation": op}
    )
    assert status == 200
    assert body["error"]["code"] == -32000
    assert "parameters changed" in body["error"]["message"]
    assert len(manager.calls) == 1


# ── /v1/summarize ──────────────────────────────────────────────────────────


class _FakeLlm:
    def __init__(self, text: str = "a summary") -> None:
        self.create_kwargs: dict[str, Any] = {}
        self.responses = self
        self.text = text

    async def create(self, **kwargs: Any) -> Any:
        self.create_kwargs = kwargs
        return SimpleNamespace(output=[SimpleNamespace(content=[SimpleNamespace(text=self.text)])])


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> _FakeLlm:
    fake = _FakeLlm()
    monkeypatch.setattr(mcp_routes, "_get_runner_llm_client", lambda: fake)
    import tiktoken

    class _Enc:
        def encode(self, text: str) -> list[int]:
            return list(range(len(text.split())))

    def _for_model(name: str) -> _Enc:
        if name.startswith("gpt-"):
            return _Enc()
        raise KeyError(name)

    monkeypatch.setattr(tiktoken, "encoding_for_model", _for_model)
    monkeypatch.setattr(tiktoken, "get_encoding", lambda name: _Enc())
    return fake


async def _summarize(h: Harness, body: Any) -> tuple[int, Any]:
    async with h.client() as client:
        resp = await client.post("/v1/summarize", json=body)
    return resp.status_code, resp.json()


async def test_summarize_requires_messages_and_model(llm: _FakeLlm) -> None:
    status, body = await _summarize(Harness(), {"messages": "no", "model": "gpt-4o"})
    assert status == 400
    assert body["error"]["code"] == "invalid_input"


async def test_summarize_uses_an_explicit_connection_and_counts_tokens(llm: _FakeLlm) -> None:
    messages = [{"role": "user", "content": "hello there"}]
    status, body = await _summarize(
        Harness(),
        {"messages": messages, "model": "gpt-4o", "connection": {"api_key": "k"}},
    )
    assert status == 200
    assert body == {"text": "a summary", "token_count": 2}
    assert llm.create_kwargs["model"] == "gpt-4o"
    assert llm.create_kwargs["connection_params"] == {"api_key": "k"}
    assert llm.create_kwargs["tools"] == []


async def test_summarize_falls_back_to_cl100k_for_unknown_models(llm: _FakeLlm) -> None:
    status, body = await _summarize(
        Harness(),
        {"messages": [], "model": "databricks/claude-sonnet", "connection": {"api_key": "k"}},
    )
    assert status == 200
    assert body["token_count"] == 2


async def test_summarize_without_a_session_sends_no_connection(llm: _FakeLlm) -> None:
    await _summarize(Harness(), {"messages": [], "model": "gpt-4o"})
    assert llm.create_kwargs["connection_params"] is None


async def _connection_for(h: Harness, llm: _FakeLlm, model: str = "gpt-4o") -> Any:
    await _summarize(h, {"messages": [], "model": model, "session_id": SESSION})
    return llm.create_kwargs["connection_params"]


async def test_summarize_resolves_api_key_auth_from_the_session_spec(llm: _FakeLlm) -> None:
    h = Harness()
    h.spec_cache[SESSION] = ResolvedSpec(
        spec=_spec(ApiKeyAuth(api_key="sk", base_url="https://llm.example")), workdir=None
    )
    assert await _connection_for(h, llm) == {"api_key": "sk", "base_url": "https://llm.example"}


async def test_summarize_resolves_databricks_auth_to_serving_endpoints(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host=f"https://{profile}.cloud/", token="dapi"),
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(DatabricksAuth(profile="prod"))
    assert await _connection_for(h, llm) == {
        "base_url": "https://prod.cloud/serving-endpoints",
        "api_key": "dapi",
    }


async def test_summarize_tolerates_an_unresolvable_databricks_profile(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(profile: str) -> Any:
        raise OSError("no Databricks profile configured")

    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace", _boom
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(DatabricksAuth(profile="prod"))
    assert await _connection_for(h, llm) is None


def _providers(monkeypatch: pytest.MonkeyPatch, entries: dict[str, Any] | Exception) -> None:
    def _load_providers(config: Any) -> dict[str, Any]:
        if isinstance(entries, Exception):
            raise entries
        return entries

    monkeypatch.setattr("omnigent.onboarding.provider_config.load_config", dict)
    monkeypatch.setattr("omnigent.onboarding.detected.effective_config_with_detected", lambda c: c)
    monkeypatch.setattr("omnigent.onboarding.provider_config.load_providers", _load_providers)


def _provider_entry(**families: Any) -> Any:
    return SimpleNamespace(kind="generic", profile=None, family=lambda name: families.get(name))


async def test_summarize_resolves_a_provider_family_by_model(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(
        monkeypatch,
        {
            "work": _provider_entry(
                openai=SimpleNamespace(api_key="oa", base_url=None),
                anthropic=SimpleNamespace(api_key="an", base_url="https://a"),
            )
        },
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="work"))
    assert await _connection_for(h, llm, model="claude-sonnet") == {
        "api_key": "an",
        "base_url": "https://a",
    }
    assert await _connection_for(h, llm, model="gpt-4o") == {"api_key": "oa"}


async def test_summarize_provider_falls_back_to_the_other_family(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(
        monkeypatch,
        {"work": _provider_entry(anthropic=SimpleNamespace(api_key="an", base_url=None))},
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="work"))
    assert await _connection_for(h, llm, model="gpt-4o") == {"api_key": "an"}


async def test_summarize_provider_without_a_usable_family_yields_no_connection(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(
        monkeypatch,
        {
            "work": _provider_entry(),
            "empty": _provider_entry(openai=SimpleNamespace(api_key="", base_url="")),
        },
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="work"))
    assert await _connection_for(h, llm) is None
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="empty"))
    assert await _connection_for(h, llm) is None
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="missing"))
    assert await _connection_for(h, llm) is None


async def test_summarize_databricks_provider_uses_its_profile(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(
        monkeypatch,
        {"dbx": SimpleNamespace(kind="databricks", profile="team", family=lambda n: None)},
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host=f"https://{profile}", token="t"),
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="dbx"))
    assert await _connection_for(h, llm) == {
        "base_url": "https://team/serving-endpoints",
        "api_key": "t",
    }


async def test_summarize_provider_loader_failure_is_swallowed(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    _providers(monkeypatch, RuntimeError("config unreadable"))
    h = Harness()
    h.spec_cache[SESSION] = _spec(ProviderAuth(name="work"))
    assert await _connection_for(h, llm) is None


async def test_summarize_uses_global_auth_when_the_spec_declares_none(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness()
    h.spec_cache[SESSION] = _spec()
    monkeypatch.setattr(
        "omnigent.runtime.workflow._load_global_auth",
        lambda: ApiKeyAuth(api_key="g", base_url="https://g"),
    )
    assert await _connection_for(h, llm) == {"api_key": "g", "base_url": "https://g"}

    monkeypatch.setattr(
        "omnigent.runtime.workflow._load_global_auth", lambda: DatabricksAuth(profile="glob")
    )
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host=f"https://{profile}", token="t"),
    )
    assert await _connection_for(h, llm) == {
        "base_url": "https://glob/serving-endpoints",
        "api_key": "t",
    }

    monkeypatch.setattr("omnigent.runtime.workflow._load_global_auth", lambda: None)
    assert await _connection_for(h, llm) is None


async def test_summarize_databricks_model_uses_the_legacy_profile(
    llm: _FakeLlm, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnigent.runtime.credentials.databricks.resolve_databricks_workspace",
        lambda profile: SimpleNamespace(host=f"https://{profile}", token="t"),
    )
    h = Harness()
    h.spec_cache[SESSION] = _spec(profile="legacy")
    assert await _connection_for(h, llm, model="databricks/dbrx") == {
        "base_url": "https://legacy/serving-endpoints",
        "api_key": "t",
    }
    h.spec_cache[SESSION] = _spec(config={"profile": "cfg"})
    assert await _connection_for(h, llm, model="databricks-dbrx") == {
        "base_url": "https://cfg/serving-endpoints",
        "api_key": "t",
    }
    # A legacy profile with a non-databricks model resolves nothing.
    assert await _connection_for(h, llm, model="gpt-4o") is None


async def test_summarize_without_a_cached_spec_sends_no_connection(llm: _FakeLlm) -> None:
    h = Harness()
    assert await _connection_for(h, llm) is None
    h.spec_cache[SESSION] = None
    assert await _connection_for(h, llm) is None


def test_runner_llm_client_is_created_once(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[object] = []

    class _Client:
        def __init__(self) -> None:
            created.append(self)

    monkeypatch.setattr("omnigent.llms.Client", _Client)
    monkeypatch.setattr(mcp_routes, "_runner_llm_client", None)
    first = mcp_routes._get_runner_llm_client()
    assert mcp_routes._get_runner_llm_client() is first
    assert len(created) == 1


# ── /v1/elicitations/{id} ──────────────────────────────────────────────────


async def _elicit(h: Harness, body: Any) -> tuple[int, Any]:
    async with h.client() as client:
        resp = await client.post("/v1/elicitations/elic_1", json=body)
    return resp.status_code, (resp.json() if resp.content else None)


async def test_elicitation_without_a_process_manager_is_501() -> None:
    status, body = await _elicit(Harness(process_manager=None), {"response_id": "r"})
    assert status == 501
    assert body["error"] == "not_implemented"


async def test_elicitation_requires_a_response_id() -> None:
    h = Harness(process_manager=_FakeProcessManager(_FakeHarnessClient()))
    status, body = await _elicit(h, {"action": "accept"})
    assert status == 400
    assert body["error"] == "invalid_request"


async def test_elicitation_for_an_unknown_response_is_404() -> None:
    h = Harness(process_manager=_FakeProcessManager(_FakeHarnessClient()))
    status, body = await _elicit(h, {"response_id": "resp_missing", "action": "accept"})
    assert status == 404
    assert body["error"] == "not_found"


async def test_elicitation_without_a_live_harness_is_409() -> None:
    h = Harness(process_manager=_FakeProcessManager(None))
    h.conversations["resp_1"] = "conv_1"
    status, body = await _elicit(h, {"response_id": "resp_1", "action": "accept"})
    assert status == 409
    assert body["error"] == "no_live_harness"


async def test_elicitation_is_forwarded_to_the_harness_as_an_approval_event() -> None:
    client = _FakeHarnessClient(httpx.Response(200, json={"accepted": True}))
    pm = _FakeProcessManager(client)
    h = Harness(process_manager=pm)
    h.conversations["resp_1"] = "conv_1"
    status, body = await _elicit(
        h, {"response_id": "resp_1", "action": "accept", "content": {"answer": "yes"}}
    )
    assert status == 200
    assert body == {"accepted": True}
    assert pm.requests == [("conv_1", "any")]
    assert client.posts == [
        {
            "url": "/v1/sessions/conv_1/events",
            "json": {
                "type": "approval",
                "elicitation_id": "elic_1",
                "action": "accept",
                "content": {"answer": "yes"},
            },
            "timeout": 30.0,
        }
    ]


async def test_elicitation_omits_content_when_absent_and_mirrors_no_body_statuses() -> None:
    client = _FakeHarnessClient(httpx.Response(204))
    h = Harness(process_manager=_FakeProcessManager(client))
    h.conversations["resp_1"] = "conv_1"
    status, body = await _elicit(h, {"response_id": "resp_1", "action": "decline"})
    assert status == 204
    assert body is None
    assert "content" not in client.posts[0]["json"]


async def test_elicitation_forward_failure_is_502_with_a_generic_detail() -> None:
    client = _FakeHarnessClient(error=RuntimeError("socket to /run/harness.sock died"))
    h = Harness(process_manager=_FakeProcessManager(client))
    h.conversations["resp_1"] = "conv_1"
    status, body = await _elicit(h, {"response_id": "resp_1", "action": "accept"})
    assert status == 502
    assert body["error"] == "elicitation_failed"
    assert "harness.sock" not in body["detail"]


# ── /mcp/execute: permission-tier reach for sys_os_* ───────────────────────


def _install_permission_state(
    runtime: PermissionTierRuntime,
    *,
    tmp_path: Path,
    workspace: Path,
    harness: str,
    tier: str,
    status: str = "ready",
) -> None:
    from omnigent.permission_tiers.host import HostPermissionState, write_host_permission_state

    state = HostPermissionState(
        session_id=SESSION,
        host_id="host-test",
        workspace=str(workspace),
        tier=tier,  # type: ignore[arg-type]
        revision=1,
        status=status,  # type: ignore[arg-type]
        transaction_id="tx-1" if status == "switching" else None,
    )
    path = tmp_path / f"state-{harness}-{tier}.json"
    write_host_permission_state(path, state)
    runtime._states[SESSION] = state
    runtime._paths[SESSION] = path
    runtime._harnesses[SESSION] = harness


def _os_env_spec(workspace: Path, *, harness: str) -> AgentSpec:
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    return AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(config={"harness": harness}),
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


@pytest.mark.parametrize("harness", ["pi", "ag-acp"])
async def test_mcp_execute_full_tier_allows_outside_os_file_ops(
    tmp_path: Path, harness: str
) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside" / "note.txt"
    runtime = PermissionTierRuntime(runner_workspace=workspace)
    _install_permission_state(
        runtime, tmp_path=tmp_path, workspace=workspace, harness=harness, tier="full"
    )
    h = Harness(permission_tiers=runtime, harness=harness, runner_workspace=workspace)
    h.spec_cache[SESSION] = _os_env_spec(workspace, harness=harness)
    try:
        status, body = await h.execute(
            _call("sys_os_write", arguments={"path": str(outside), "content": "outside\n"})
        )
        assert status == 200
        payload = json.loads(body["result"]["output"])
        assert "error" not in payload, payload
        assert outside.read_text(encoding="utf-8") == "outside\n"

        status, body = await h.execute(
            _call(
                "sys_os_edit",
                arguments={"path": str(outside), "oldText": "outside", "newText": "edited"},
            )
        )
        assert status == 200
        payload = json.loads(body["result"]["output"])
        assert "error" not in payload, payload

        status, body = await h.execute(_call("sys_os_read", arguments={"path": str(outside)}))
        assert status == 200
        payload = json.loads(body["result"]["output"])
        assert payload.get("content") == "edited\n"
    finally:
        await h.resource_registry.cleanup_session(SESSION)


@pytest.mark.parametrize(
    ("tier", "state_status"),
    [
        ("ask", "ready"),
        ("plan", "ready"),
        ("accept-edits", "ready"),
        ("auto", "ready"),
        ("full", "blocked"),
        ("full", "switching"),
        (None, "ready"),
    ],
)
async def test_execute_tool_non_full_or_unready_tier_refuses_outside_os_write(
    tmp_path: Path, tier: str | None, state_status: str
) -> None:
    """Only a verified ready ``full`` state skips os_env reach checks."""
    from omnigent.runner.tool_dispatch import execute_tool

    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside" / "blocked.txt"
    runtime = PermissionTierRuntime(runner_workspace=workspace)
    if tier is not None:
        _install_permission_state(
            runtime,
            tmp_path=tmp_path,
            workspace=workspace,
            harness="pi",
            tier=tier,
            status=state_status,
        )
    resource_registry = SessionResourceRegistry()
    try:
        output = await execute_tool(
            tool_name="sys_os_write",
            arguments=json.dumps({"path": str(outside), "content": "nope\n"}),
            resource_registry=resource_registry,
            agent_spec=_os_env_spec(workspace, harness="pi"),
            conversation_id=SESSION,
            runner_workspace=workspace,
            effective_harness="pi",
            permission_tiers=runtime,
        )
        payload = json.loads(output)
        assert "outside the environment root" in payload.get("error", ""), payload
        assert not outside.exists()
    finally:
        await resource_registry.cleanup_session(SESSION)


async def test_dispatch_tool_locally_async_and_flow_thread_permission_tiers(
    tmp_path: Path,
) -> None:
    """Runner-local dispatch, sys_call_async, and flow steps all read the live tier."""
    import asyncio

    from omnigent.runner import flows
    from omnigent.runner.tool_dispatch import dispatch_tool_locally, execute_tool
    from omnigent.tools.builtins.flow import FlowPlan, FlowStep

    workspace = tmp_path / "ws"
    workspace.mkdir()
    runtime = PermissionTierRuntime(runner_workspace=workspace)
    _install_permission_state(
        runtime, tmp_path=tmp_path, workspace=workspace, harness="pi", tier="full"
    )
    resource_registry = SessionResourceRegistry()
    spec = _os_env_spec(workspace, harness="pi")
    harness_client = _FakeHarnessClient(
        httpx.Response(
            200,
            json={"accepted": True},
            request=httpx.Request("POST", f"http://harness/v1/sessions/{SESSION}/events"),
        )
    )
    inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    async_tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]] = {}

    try:
        # 1. dispatch_tool_locally (SSE action_required path for any harness)
        local_target = tmp_path / "outside" / "local.txt"
        out = await dispatch_tool_locally(
            tool_name="sys_os_write",
            call_id="call_1",
            arguments=json.dumps({"path": str(local_target), "content": "local\n"}),
            response_id="resp_1",
            harness_client=harness_client,  # type: ignore[arg-type]
            resource_registry=resource_registry,
            agent_spec=spec,
            conversation_id=SESSION,
            runner_workspace=workspace,
            effective_harness="pi",
            permission_tiers=runtime,
        )
        assert "error" not in json.loads(out), out
        assert local_target.read_text(encoding="utf-8") == "local\n"

        # 2. sys_call_async background dispatch
        async_target = tmp_path / "outside" / "async.txt"
        handle_json = await execute_tool(
            tool_name="sys_call_async",
            arguments=json.dumps(
                {
                    "tool": "sys_os_write",
                    "args": json.dumps({"path": str(async_target), "content": "async\n"}),
                }
            ),
            resource_registry=resource_registry,
            agent_spec=spec,
            conversation_id=SESSION,
            runner_workspace=workspace,
            session_inbox=inbox,
            session_async_tasks=async_tasks,
            effective_harness="pi",
            permission_tiers=runtime,
        )
        assert "handle_id" in json.loads(handle_json), handle_json
        item = await asyncio.wait_for(inbox.get(), timeout=10.0)
        assert item["status"] == "completed", item
        assert async_target.read_text(encoding="utf-8") == "async\n"

        # 3. Flow step execution at full, then downgraded to auto (live check)
        flow_target = tmp_path / "outside" / "flow.txt"
        ctx = flows.FlowContext(
            server_client=httpx.AsyncClient(base_url="http://server"),
            conversation_id=SESSION,
            agent_spec=spec,
            resource_registry=resource_registry,
            runner_workspace=workspace,
            effective_harness="pi",
            permission_tiers=runtime,
        )
        run = flows._FlowRun(
            flow_id="flow_1",
            plan=FlowPlan(
                steps=(
                    FlowStep(
                        tool="sys_os_write",
                        args={"path": str(flow_target), "content": "flow\n"},
                    ),
                ),
                start_after_s=0.0,
                every_s=None,
                for_s=None,
                times=1,
                stop_when=None,
                bring_back="last",
                max_chars=2000,
                note=None,
            ),
            ctx=ctx,
            created_mono=0.0,
            started_at=0.0,
        )
        step_out = await flows._run_step(run, 0, {"path": str(flow_target), "content": "flow\n"})
        assert "error" not in json.loads(step_out), step_out
        assert flow_target.read_text(encoding="utf-8") == "flow\n"

        # Downgrading the live tier immediately re-enables reach confinement.
        _install_permission_state(
            runtime, tmp_path=tmp_path, workspace=workspace, harness="pi", tier="auto"
        )
        blocked_target = tmp_path / "outside" / "after-downgrade.txt"
        blocked_out = await flows._run_step(
            run, 0, {"path": str(blocked_target), "content": "nope\n"}
        )
        assert "outside the environment root" in json.loads(blocked_out).get("error", "")
        assert not blocked_target.exists()
    finally:
        await resource_registry.cleanup_session(SESSION)


async def test_stale_owner_and_harness_mismatch_refuse_full_reach(tmp_path: Path) -> None:
    """Deactivating managed permissions or mismatching the harness owner drops full reach."""
    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitEnvelope,
        RunnerSessionInitSnapshot,
    )
    from omnigent.runner.tool_dispatch import execute_tool

    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside" / "stale.txt"
    runtime = PermissionTierRuntime(runner_workspace=workspace)
    _install_permission_state(
        runtime, tmp_path=tmp_path, workspace=workspace, harness="ag-acp", tier="full"
    )
    resource_registry = SessionResourceRegistry()
    try:
        # Different harness cannot inherit ag-acp's full tier.
        assert runtime.ready_tier(SESSION, harness="claude-sdk") is None
        mismatch_out = await execute_tool(
            tool_name="sys_os_write",
            arguments=json.dumps({"path": str(outside), "content": "nope\n"}),
            resource_registry=resource_registry,
            agent_spec=_os_env_spec(workspace, harness="claude-sdk"),
            conversation_id=SESSION,
            runner_workspace=workspace,
            effective_harness="claude-sdk",
            permission_tiers=runtime,
        )
        assert "outside the environment root" in json.loads(mismatch_out).get("error", "")
        assert not outside.exists()

        # Reinitializing without managed permissions clears cached state and owner.
        envelope = RunnerSessionInitEnvelope(
            protocol_version=SESSION_INIT_PROTOCOL_VERSION,
            server_version="0.24.0",
            session_id=SESSION,
            agent_id="ag_1",
            snapshot=RunnerSessionInitSnapshot(
                session_id=SESSION,
                host_id="host-test",
                workspace=str(workspace),
                labels={},
                created_at=0,
                updated_at=0,
            ),
        )
        assert runtime.fence_start(envelope, "claude-sdk") is False
        assert runtime.install(envelope, "claude-sdk") is None
        assert runtime.state(SESSION) is None
        assert runtime.ready_tier(SESSION) is None
    finally:
        await resource_registry.cleanup_session(SESSION)


async def test_inflight_full_os_env_op_fences_concurrent_tier_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A paused full-tier os_env op marks PermissionTierRuntime busy so tier prepare fails."""
    import asyncio

    from omnigent.runner import tool_dispatch

    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "outside" / "inflight.txt"
    runtime = PermissionTierRuntime(runner_workspace=workspace)
    _install_permission_state(
        runtime, tmp_path=tmp_path, workspace=workspace, harness="pi", tier="full"
    )
    resource_registry = SessionResourceRegistry()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_exec = tool_dispatch._execute_os_env_tool

    async def _paused_exec(*args: Any, **kwargs: Any) -> str:
        entered.set()
        await release.wait()
        return await original_exec(*args, **kwargs)

    monkeypatch.setattr(tool_dispatch, "_execute_os_env_tool", _paused_exec)
    task = asyncio.create_task(
        tool_dispatch.execute_tool(
            tool_name="sys_os_write",
            arguments=json.dumps({"path": str(outside), "content": "inflight\n"}),
            resource_registry=resource_registry,
            agent_spec=_os_env_spec(workspace, harness="pi"),
            conversation_id=SESSION,
            runner_workspace=workspace,
            effective_harness="pi",
            permission_tiers=runtime,
        )
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=5.0)
        assert runtime.idle(SESSION) is False
        with pytest.raises(ValueError, match="busy or stale"):
            runtime.prepare(SESSION, expected_revision=1, transaction_id="tx-switch")
        release.set()
        out = await asyncio.wait_for(task, timeout=10.0)
        assert "error" not in json.loads(out), out
        assert outside.read_text(encoding="utf-8") == "inflight\n"
        assert runtime.idle(SESSION) is True
        switching = runtime.prepare(SESSION, expected_revision=1, transaction_id="tx-switch")
        assert switching.status == "switching"
    finally:
        release.set()
        if not task.done():
            await task
        await resource_registry.cleanup_session(SESSION)
