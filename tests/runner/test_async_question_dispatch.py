"""Async question REST dispatch and transport diagnostics for companion UI tools."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool
from omnigent.spec.types import AgentSpec


@pytest.mark.asyncio
async def test_async_question_posts_payload_and_returns_card_id() -> None:
    questions = [{"question": "Proceed?", "options": [{"label": "Continue"}]}]
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"qid": "q123456"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        output = await execute_tool(
            tool_name="ask_user_async",
            arguments=json.dumps({"questions": questions, "context": "Review the change"}),
            server_client=client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert "Question card #q123456 posted" in output
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/sessions/conv_current/async-questions"
    assert json.loads(requests[0].content) == {
        "questions": questions,
        "context": "Review the change",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("ask_user_async", {"questions": [{"question": "Proceed?"}]}),
        ("open_in_panel", {"path": "report.html"}),
    ],
)
@pytest.mark.parametrize(
    ("error", "detail"),
    [
        (httpx.ReadTimeout(""), "ReadTimeout: "),
        (httpx.PoolTimeout(""), "PoolTimeout: "),
        (httpx.ConnectError("offline"), "ConnectError: offline"),
    ],
    ids=["read-timeout", "pool-timeout", "connection-error"],
)
async def test_ui_rest_tool_transport_failure_retains_exception_type(
    tool_name: str, arguments: dict[str, object], error: httpx.HTTPError, detail: str
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise error

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        output = await execute_tool(
            tool_name=tool_name,
            arguments=json.dumps(arguments),
            server_client=client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output)["error"] == f"{tool_name} failed: {detail}"
