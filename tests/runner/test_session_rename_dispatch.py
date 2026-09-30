"""Runner dispatch and native-relay coverage for session renaming."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    build_native_relay_tool_schemas,
    dispatch_tool_locally,
    execute_tool,
)
from omnigent.spec.types import AgentSpec
from omnigent.tools.builtins.session_rename import SysSessionRenameTool

_TITLE_MAX_CHARS: int = SysSessionRenameTool().get_schema()["function"]["parameters"][
    "properties"
]["title"]["maxLength"]


def _top_level_session_handler(request: httpx.Request) -> httpx.Response | None:
    """Answer the dispatcher's top-level check for a parentless session.

    :param request: The intercepted request.
    :returns: A session snapshot for the GET probe, ``None`` for other
        requests (so the caller's handler decides).
    """
    if request.method == "GET" and request.url.path == "/v1/sessions/conv_current":
        return httpx.Response(200, json={"id": "conv_current", "parent_session_id": None})
    return None


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_relay_exposes_session_rename(spec: AgentSpec | None) -> None:
    schemas = build_native_relay_tool_schemas(spec)

    rename = next(schema for schema in schemas if schema["name"] == "sys_session_rename")

    assert rename["parameters"]["required"] == ["title"]
    assert rename["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_session_rename_dispatches_repeatable_policy_aware_request() -> None:
    rename_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        probe = _top_level_session_handler(request)
        if probe is not None:
            return probe
        rename_requests.append(request)
        return httpx.Response(
            200, json={"renamed": True, "reason": None, **json.loads(request.content)}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        outputs = [
            await execute_tool(
                tool_name="sys_session_rename",
                arguments=json.dumps({"title": title}),
                server_client=server_client,
                conversation_id="conv_current",
                agent_spec=AgentSpec(spec_version=1),
            )
            for title in ("Debug auth timeout", "Verify auth timeout fix")
        ]

    assert [json.loads(output) for output in outputs] == [
        {"renamed": True, "title": "Debug auth timeout", "reason": None},
        {"renamed": True, "title": "Verify auth timeout fix", "reason": None},
    ]
    assert len(rename_requests) == 2
    assert all(request.method == "POST" for request in rename_requests)
    assert all(
        request.url.path == "/v1/sessions/conv_current/agent-title" for request in rename_requests
    )
    assert [json.loads(request.content) for request in rename_requests] == [
        {"title": "Debug auth timeout"},
        {"title": "Verify auth timeout fix"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["generation_failed", "title_changed", "not_top_level"])
async def test_session_rename_preserves_server_refusal(reason: str) -> None:
    result = {"renamed": False, "title": None, "reason": reason}

    def handler(request: httpx.Request) -> httpx.Response:
        return _top_level_session_handler(request) or httpx.Response(200, json=result)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "Resolve PR 123 conflict"}),
            server_client=server_client,
            conversation_id="conv_current",
        )

    assert json.loads(output) == result


@pytest.mark.asyncio
async def test_session_rename_does_not_bypass_policy_on_older_servers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _top_level_session_handler(request) or httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "Resolve PR 123 conflict"}),
            server_client=server_client,
            conversation_id="conv_current",
        )

    assert "returned 404" in json.loads(output)["error"]
    assert [request.method for request in requests] == ["GET", "POST"]


@pytest.mark.asyncio
async def test_session_rename_refuses_child_sessions() -> None:
    """A sub-agent must not rename itself — its title is its address.

    A child's ``(parent, title)`` pair is how ``sys_session_send``
    continuations find it, so a self-rename would corrupt sibling
    addressing. The dispatcher refuses before issuing any rename request.
    """
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_child":
            return httpx.Response(
                200,
                json={"id": "conv_child", "parent_session_id": "conv_parent"},
            )
        patch_requests.append(request)
        return httpx.Response(200, json={"id": "conv_child", **json.loads(request.content)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "Debug auth timeout"}),
            server_client=server_client,
            conversation_id="conv_child",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {
        "renamed": False,
        "title": None,
        "reason": "not_top_level",
    }
    assert patch_requests == []


@pytest.mark.asyncio
async def test_session_rename_retitles_direct_child_with_address_prefix() -> None:
    """A parent retitles its child; the child's agent address prefix is kept."""
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_current":
            return httpx.Response(200, json={"id": "conv_current", "parent_session_id": None})
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_child":
            return httpx.Response(
                200,
                json={
                    "id": "conv_child",
                    "title": "researcher:auth",
                    "parent_session_id": "conv_current",
                },
            )
        if request.method == "GET" and request.url.path.endswith(
            "/v1/sessions/conv_current/child_sessions"
        ):
            return httpx.Response(
                200, json={"data": [{"id": "conv_child", "title": "researcher:auth"}]}
            )
        patch_requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "new label", "session_id": "conv_child"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {
        "renamed": True,
        "title": "researcher:new label",
        "reason": None,
    }
    assert len(patch_requests) == 1
    assert patch_requests[0].method == "PATCH"
    assert patch_requests[0].url.path == "/v1/sessions/conv_child"
    assert json.loads(patch_requests[0].content) == {"title": "researcher:new label"}


@pytest.mark.asyncio
async def test_session_rename_retitles_grandchild() -> None:
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_current":
            return httpx.Response(200, json={"id": "conv_current", "parent_session_id": None})
        if request.method == "GET" and path == "/v1/sessions/conv_grand":
            return httpx.Response(
                200,
                json={
                    "id": "conv_grand",
                    "title": "worker:task",
                    "parent_session_id": "conv_mid",
                },
            )
        if request.method == "GET" and path == "/v1/sessions/conv_mid":
            return httpx.Response(
                200, json={"id": "conv_mid", "parent_session_id": "conv_current"}
            )
        if request.method == "GET" and path == "/v1/sessions/conv_mid/child_sessions":
            return httpx.Response(
                200, json={"data": [{"id": "conv_grand", "title": "worker:task"}]}
            )
        patch_requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "renamed task", "session_id": "conv_grand"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {
        "renamed": True,
        "title": "worker:renamed task",
        "reason": None,
    }
    assert len(patch_requests) == 1
    assert patch_requests[0].url.path == "/v1/sessions/conv_grand"
    assert json.loads(patch_requests[0].content) == {"title": "worker:renamed task"}


@pytest.mark.asyncio
async def test_session_rename_refuses_unrelated_session() -> None:
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_current":
            return httpx.Response(
                200, json={"id": "conv_current", "parent_session_id": "conv_parent"}
            )
        if request.method == "GET" and path == "/v1/sessions/conv_sibling":
            return httpx.Response(
                200,
                json={
                    "id": "conv_sibling",
                    "title": "researcher:auth",
                    "parent_session_id": "conv_parent",
                },
            )
        if request.method == "GET" and path == "/v1/sessions/conv_parent":
            return httpx.Response(200, json={"id": "conv_parent", "parent_session_id": None})
        patch_requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "new label", "session_id": "conv_sibling"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {
        "renamed": False,
        "title": None,
        "reason": "not_descendant",
    }
    assert patch_requests == []


@pytest.mark.asyncio
async def test_session_rename_refuses_title_taken_by_sibling() -> None:
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_current":
            return httpx.Response(200, json={"id": "conv_current", "parent_session_id": None})
        if request.method == "GET" and path == "/v1/sessions/conv_child":
            return httpx.Response(
                200,
                json={
                    "id": "conv_child",
                    "title": "researcher:auth",
                    "parent_session_id": "conv_current",
                },
            )
        if request.method == "GET" and path == "/v1/sessions/conv_current/child_sessions":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "conv_child", "title": "researcher:auth"},
                        {"id": "conv_other", "title": "researcher:newlabel"},
                    ]
                },
            )
        patch_requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "newlabel", "session_id": "conv_child"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {"renamed": False, "title": None, "reason": "title_taken"}
    assert patch_requests == []


@pytest.mark.asyncio
async def test_session_rename_sibling_check_requests_the_route_maximum() -> None:
    """The sibling scan asks for the route's 1000-row max.

    The safe title is only withheld by the mock unless the request asks for
    ``limit=1000``, so the refusal proves the widened scan was requested.
    """
    child_list_requests: list[httpx.Request] = []
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_current":
            return httpx.Response(200, json={"id": "conv_current", "parent_session_id": None})
        if request.method == "GET" and path == "/v1/sessions/conv_child":
            return httpx.Response(
                200,
                json={
                    "id": "conv_child",
                    "title": "researcher:auth",
                    "parent_session_id": "conv_current",
                },
            )
        if request.method == "GET" and path == "/v1/sessions/conv_current/child_sessions":
            child_list_requests.append(request)
            data = [{"id": "conv_child", "title": "researcher:auth"}]
            if request.url.params.get("limit") == "1000":
                data.append({"id": "conv_other", "title": "researcher:newlabel"})
            return httpx.Response(200, json={"data": data})
        patch_requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "newlabel", "session_id": "conv_child"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {"renamed": False, "title": None, "reason": "title_taken"}
    assert [request.url.params.get("limit") for request in child_list_requests] == ["1000"]
    assert patch_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot",
    [
        {"title": "researcher:auth:closed:conv_child"},
        {"title": "researcher:auth", "labels": {"omnigent.closed": "true"}},
    ],
    ids=["legacy-title-marker", "closed-label"],
)
async def test_session_rename_refuses_closed_child(snapshot: dict[str, object]) -> None:
    """A closed descendant must not drop its marker and reopen via rename."""
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_current":
            return httpx.Response(200, json={"id": "conv_current", "parent_session_id": None})
        if request.method == "GET" and path == "/v1/sessions/conv_child":
            return httpx.Response(
                200,
                json={"id": "conv_child", "parent_session_id": "conv_current", **snapshot},
            )
        patch_requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "new label", "session_id": "conv_child"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {"renamed": False, "title": None, "reason": "session_closed"}
    assert patch_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch_status", "reason"),
    [(404, "session_not_found"), (403, "access_denied")],
)
async def test_session_rename_patch_refusal_keeps_structured_reason(
    patch_status: int,
    reason: str,
) -> None:
    """A refused descendant PATCH maps to the same typed reasons as the GET."""
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/v1/sessions/conv_current":
            return httpx.Response(200, json={"id": "conv_current", "parent_session_id": None})
        if request.method == "GET" and path == "/v1/sessions/conv_child":
            return httpx.Response(
                200,
                json={
                    "id": "conv_child",
                    "title": "researcher:auth",
                    "parent_session_id": "conv_current",
                },
            )
        if request.method == "GET" and path.endswith("/child_sessions"):
            return httpx.Response(
                200, json={"data": [{"id": "conv_child", "title": "researcher:auth"}]}
            )
        if request.method == "PATCH":
            patch_requests.append(request)
            return httpx.Response(patch_status, text="refused")
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "new label", "session_id": "conv_child"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {"renamed": False, "title": None, "reason": reason}
    assert len(patch_requests) == 1


@pytest.mark.asyncio
async def test_session_rename_does_not_double_the_address_prefix() -> None:
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_current":
            return httpx.Response(200, json={"id": "conv_current", "parent_session_id": None})
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_child":
            return httpx.Response(
                200,
                json={
                    "id": "conv_child",
                    "title": "researcher:auth",
                    "parent_session_id": "conv_current",
                },
            )
        if request.method == "GET" and request.url.path.endswith(
            "/v1/sessions/conv_current/child_sessions"
        ):
            return httpx.Response(
                200, json={"data": [{"id": "conv_child", "title": "researcher:auth"}]}
            )
        patch_requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "researcher:newlabel", "session_id": "conv_child"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {
        "renamed": True,
        "title": "researcher:newlabel",
        "reason": None,
    }
    assert json.loads(patch_requests[0].content) == {"title": "researcher:newlabel"}


@pytest.mark.asyncio
async def test_session_rename_replaces_a_no_colon_title_whole() -> None:
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_current":
            return httpx.Response(200, json={"id": "conv_current", "parent_session_id": None})
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_child":
            return httpx.Response(
                200,
                json={"id": "conv_child", "title": "auth", "parent_session_id": "conv_current"},
            )
        if request.method == "GET" and request.url.path.endswith(
            "/v1/sessions/conv_current/child_sessions"
        ):
            return httpx.Response(200, json={"data": [{"id": "conv_child", "title": "auth"}]})
        patch_requests.append(request)
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "new label", "session_id": "conv_child"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {"renamed": True, "title": "new label", "reason": None}
    assert json.loads(patch_requests[0].content) == {"title": "new label"}


@pytest.mark.asyncio
async def test_session_rename_with_own_session_id_uses_self_path() -> None:
    rename_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        probe = _top_level_session_handler(request)
        if probe is not None:
            return probe
        rename_requests.append(request)
        return httpx.Response(
            200, json={"renamed": True, "reason": None, **json.loads(request.content)}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "Debug auth timeout", "session_id": "conv_current"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {
        "renamed": True,
        "title": "Debug auth timeout",
        "reason": None,
    }
    assert [request.method for request in rename_requests] == ["POST"]
    assert rename_requests[0].url.path == "/v1/sessions/conv_current/agent-title"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "info_payload",
    [
        ["unexpected"],
        {"id": "conv_current"},
        {"id": "conv_current", "parent_session_id": ""},
        {"id": "conv_current", "parent_session_id": 0},
    ],
    ids=["non-dict", "missing-parent-field", "empty-string-parent", "non-string-parent"],
)
async def test_session_rename_fails_closed_on_unverifiable_session(
    info_payload: object,
) -> None:
    """A snapshot that can't prove the session is top-level blocks the PATCH.

    A malformed or version-skewed GET payload must not be read as "no
    parent" — failing open here would let a child rename slip through and
    corrupt its continuation address.
    """
    patch_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=info_payload)
        patch_requests.append(request)
        return httpx.Response(200, json={"id": "conv_current", **json.loads(request.content)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "Debug auth timeout"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert "could not verify the session is top-level" in json.loads(output)["error"]
    assert patch_requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("title", "expected_error"),
    [
        ("x", f"2-{_TITLE_MAX_CHARS} characters"),
        ("  ", f"2-{_TITLE_MAX_CHARS} characters"),
        ("x" * (_TITLE_MAX_CHARS + 1), f"2-{_TITLE_MAX_CHARS} characters"),
        ("Debug auth\ntimeout", "single line"),
    ],
)
async def test_session_rename_rejects_invalid_titles_before_request(
    title: str,
    expected_error: str,
) -> None:
    requests: list[httpx.Request] = []

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(500)
        ),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": title}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert expected_error in json.loads(output)["error"]
    assert requests == []


@pytest.mark.asyncio
async def test_session_rename_accepts_titles_up_to_the_generated_cap() -> None:
    """The dispatcher enforces exactly the cap the tool schema advertises."""
    title = "T" * _TITLE_MAX_CHARS

    def handler(request: httpx.Request) -> httpx.Response:
        probe = _top_level_session_handler(request)
        if probe is not None:
            return probe
        return httpx.Response(200, json={"id": "conv_current", **json.loads(request.content)})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": title}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {"renamed": True, "title": title, "reason": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected_error"),
    [
        (httpx.Response(503, text="server unavailable"), "returned 503"),
        (httpx.Response(200, text="not-json"), "returned invalid JSON"),
        (httpx.Response(200, json=["unexpected"]), "returned a non-object response"),
        (
            httpx.Response(200, json={"id": "conv_current", "title": None}),
            "response omitted the updated title",
        ),
    ],
)
async def test_session_rename_server_failures_are_tool_results(
    response: httpx.Response,
    expected_error: str,
) -> None:
    """Rename metadata failures never escape into the active session turn."""

    def handler(request: httpx.Request) -> httpx.Response:
        probe = _top_level_session_handler(request)
        if probe is not None:
            return probe
        return response

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "Debug auth timeout"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert expected_error in json.loads(output)["error"]


@pytest.mark.asyncio
async def test_session_rename_transport_failure_is_delivered_to_harness() -> None:
    """A failed rename still resolves the harness tool call so the turn continues."""
    delivered: list[dict[str, object]] = []

    def server_handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server unavailable")

    def harness_handler(request: httpx.Request) -> httpx.Response:
        delivered.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(server_handler),
            base_url="http://server",
        ) as server_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(harness_handler),
            base_url="http://harness",
        ) as harness_client,
    ):
        output = await dispatch_tool_locally(
            tool_name="sys_session_rename",
            call_id="call_rename",
            arguments=json.dumps({"title": "Debug auth timeout"}),
            response_id="response_1",
            harness_client=harness_client,
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert "sys_session_rename failed" in json.loads(output)["error"]
    assert delivered == [
        {
            "type": "tool_result",
            "call_id": "call_rename",
            "output": output,
        }
    ]
