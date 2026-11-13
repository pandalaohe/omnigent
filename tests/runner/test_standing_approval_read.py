"""The pre-approved read surface cannot mutate standing approval."""

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import _execute_standing_approval_tool


@pytest.mark.parametrize(
    "arguments", [{}, {"op": "grant"}, {"op": "revoke"}, {"kinds": ["merge"]}]
)
async def test_standing_read_cannot_post(arguments):
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"record": {}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        result = json.loads(
            await _execute_standing_approval_tool(
                json.dumps(arguments),
                conversation_id="session",
                server_client=client,
                read_only=True,
            )
        )
    if arguments:
        assert "error" in result
        assert requests == []
    else:
        assert result == {"record": {}}
        assert [request.method for request in requests] == ["GET"]
