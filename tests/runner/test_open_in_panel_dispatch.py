"""Runner dispatch and registration coverage for ``open_in_panel``.

Covers the runner side of the artifact panel-open flow:

- ``_open_in_panel_via_rest``: the POST to the server's ``artifacts/open``
  route — correct relative/absolute body, the success text with the viewer
  count, error envelopes for HTTP/transport/response failures, and a result
  that never carries an artifact URL.
- ``should_dispatch_locally``: the runner owns the call on the
  ``dispatch=None`` path too, instead of relaying it upstream.
- Registration in ``omnigent.tools.builtins``: the name is reserved and
  framework-owned, always registered on a bare spec, and relayed to native
  harnesses (their only tool surface).
"""

from __future__ import annotations

import json

import httpx
import pytest

import omnigent.tools.builtins as builtins_mod
from omnigent.runner.tool_dispatch import (
    build_native_relay_tool_schemas,
    execute_tool,
    should_dispatch_locally,
)
from omnigent.spec.types import AgentSpec, BuiltinToolConfig, ToolsConfig
from omnigent.tools.manager import ToolManager

_TOOL_NAME = "open_in_panel"


def _ok_handler(body: dict[str, object] | None = None):
    """Return a handler that records POSTs and answers with ``{viewers: 1}``."""
    requests: list[httpx.Request] = []
    response_body = body if body is not None else {"viewers": 1}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=response_body)

    return requests, handler


async def _dispatch(
    handler,
    *,
    path: object = "out/report.html",
    conversation_id: str | None = "conv_current",
) -> str:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        return await execute_tool(
            tool_name=_TOOL_NAME,
            arguments=json.dumps({"path": path}),
            server_client=server_client,
            conversation_id=conversation_id,
            agent_spec=AgentSpec(spec_version=1),
        )


# ── Dispatch ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_in_panel_posts_relative_path_and_returns_viewer_count() -> None:
    """A workspace-relative path is posted as-is and the viewer count is reported."""
    requests, handler = _ok_handler({"viewers": 2})

    output = await _dispatch(handler)

    assert output == "Asked the web UI to open out/report.html (2 viewer(s) connected)"
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/sessions/conv_current/artifacts/open"
    assert json.loads(request.content) == {"path": "out/report.html"}


@pytest.mark.asyncio
async def test_open_in_panel_posts_absolute_path_with_host_base() -> None:
    """A leading slash marks an absolute host path: slash stripped, base named."""
    requests, handler = _ok_handler({"viewers": 1})

    output = await _dispatch(handler, path="/Users/x/notes/report.html")

    assert output == (
        "Asked the web UI to open /Users/x/notes/report.html (1 viewer(s) connected)"
    )
    assert json.loads(requests[0].content) == {
        "path": "Users/x/notes/report.html",
        "base": "host",
    }


@pytest.mark.asyncio
async def test_open_in_panel_result_carries_no_url() -> None:
    """The tool result never exposes an artifact URL — clients mint their own."""
    _, handler = _ok_handler({"viewers": 3})

    output = await _dispatch(handler)

    assert "/v1/artifacts" not in output
    assert "http" not in output


@pytest.mark.asyncio
async def test_open_in_panel_rejects_missing_path_before_any_request() -> None:
    """A missing or non-string path is an envelope; the server is never called."""
    requests, handler = _ok_handler()

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name=_TOOL_NAME,
            arguments=json.dumps({"path": 42}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert "requires a string 'path'" in json.loads(output)["error"]
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 404, 500, 503])
async def test_open_in_panel_http_failure_is_error_envelope(status: int) -> None:
    """A 4xx/5xx becomes a tool-result envelope, never a raised exception."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="refused")

    output = await _dispatch(handler)

    assert f"open_in_panel returned {status}" in json.loads(output)["error"]


@pytest.mark.asyncio
async def test_open_in_panel_transport_failure_is_error_envelope() -> None:
    """A transport error becomes a tool-result envelope, never a raised exception."""

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server unavailable")

    output = await _dispatch(handler)

    assert "open_in_panel failed" in json.loads(output)["error"]


@pytest.mark.asyncio
async def test_open_in_panel_malformed_response_is_error_envelope() -> None:
    """A non-JSON body or a payload without a viewer count is a clean error."""

    def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    output = await _dispatch(non_json)
    assert "returned invalid JSON" in json.loads(output)["error"]

    _, handler = _ok_handler({})
    output = await _dispatch(handler)
    assert "omitted the viewer count" in json.loads(output)["error"]


# ── Registration and reservation ─────────────────────────────────


def test_toolmanager_always_registers_open_in_panel() -> None:
    """Every session — even a spec declaring no tools — has the tool."""
    mgr = ToolManager(AgentSpec(spec_version=1))
    tool = mgr.get_tool(_TOOL_NAME)
    assert tool is not None
    schema = tool.get_schema()["function"]
    assert schema["name"] == _TOOL_NAME
    assert schema["description"]
    assert schema["parameters"]["required"] == ["path"]
    assert schema["parameters"]["additionalProperties"] is False


def test_open_in_panel_name_reserved_framework_owned() -> None:
    """The name is reserved and framework-owned: no registry factory, so a
    spec cannot instantiate (and thus shadow) it through the builtin path."""
    assert _TOOL_NAME in builtins_mod.BUILTIN_NAMES
    assert _TOOL_NAME not in builtins_mod.INSTANTIABLE_BUILTINS
    assert builtins_mod.get_builtin_tool(_TOOL_NAME) is None


def test_open_in_panel_declared_builtin_cannot_shadow_framework_tool() -> None:
    """A spec declaring the name still gets the framework-owned registration."""
    spec = AgentSpec(
        spec_version=1,
        tools=ToolsConfig(builtins=[BuiltinToolConfig(name=_TOOL_NAME)]),
    )
    mgr = ToolManager(spec)
    tool = mgr.get_tool(_TOOL_NAME)
    assert tool is not None
    assert type(tool).__name__ == "OpenInPanelTool"


# ── Runner routing ───────────────────────────────────────────────


def test_open_in_panel_dispatches_locally() -> None:
    """The runner must own the call on the ``dispatch=None`` event-turn path.

    ``should_dispatch_locally`` gates both runner dispatch and the server's
    skip-own-dispatch check; a False here would relay the call upstream and
    the POST would never run.
    """
    assert should_dispatch_locally(_TOOL_NAME) is True


# ── Native-relay exposure ────────────────────────────────────────


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_relay_includes_open_in_panel(spec: AgentSpec | None) -> None:
    """A bare spec and the spec-less fallback both surface the schema."""
    schemas = build_native_relay_tool_schemas(spec)
    panel = next(schema for schema in schemas if schema["name"] == _TOOL_NAME)
    assert panel["description"]
    assert panel["parameters"]["required"] == ["path"]
