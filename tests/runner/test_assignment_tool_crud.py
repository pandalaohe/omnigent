"""Tests for the five passthrough ``sys_assignment_*`` tools.

Each tool proxies one ``/v1/assignments`` REST call with the calling
session id derived from ``conversation_id``. A recording fake client
pins the method, URL, query and body of every call.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.runner.assignment_tools import execute_assignment_tool
from omnigent.runner.tool_dispatch import execute_tool, should_dispatch_locally

_ASSIGNMENT_ID = "0123456789abcdef0123456789abcdef"
_CONV = "11111111111111111111111111111111"


class _Resp:
    def __init__(self, *, status_code: int = 200, body: object | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}

    @property
    def text(self) -> str:
        return json.dumps(self._body)

    def json(self) -> object:
        return self._body


class _RecordingClient:
    """Records verb/url/params/json of each call; replays one response."""

    def __init__(self, response: _Resp | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self._response = response or _Resp(body={"ok": True})

    async def get(
        self, url: str, *, params: dict[str, Any] | None = None, timeout: object = None
    ) -> _Resp:
        self.calls.append(("GET", url, params))
        return self._response

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: object = None,
    ) -> _Resp:
        self.calls.append(("POST", url, json if json is not None else params))
        return self._response


async def _run(
    tool_name: str, args: dict[str, Any] | str, client: _RecordingClient | None
) -> tuple[Any, list[tuple[str, str, dict[str, Any] | None]]]:
    arguments = args if isinstance(args, str) else json.dumps(args)
    out = await execute_assignment_tool(
        tool_name,
        arguments,
        conversation_id=_CONV,
        runner_workspace=None,
        server_client=client,  # type: ignore[arg-type]
    )
    calls = client.calls if client is not None else []
    return json.loads(out), calls


@pytest.mark.asyncio
async def test_get_reads_one_assignment() -> None:
    client = _RecordingClient(_Resp(body={"id": _ASSIGNMENT_ID}))
    out, calls = await _run("sys_assignment_get", {"assignment_id": _ASSIGNMENT_ID}, client)
    assert calls == [("GET", f"/v1/assignments/{_ASSIGNMENT_ID}", None)]
    assert out["id"] == _ASSIGNMENT_ID


@pytest.mark.asyncio
async def test_list_sent_scopes_to_calling_session() -> None:
    client = _RecordingClient(_Resp(body={"data": []}))
    out, calls = await _run(
        "sys_assignment_list",
        {"role": "sent", "state": "waiting", "after": "a", "limit": 5},
        client,
    )
    assert calls == [
        (
            "GET",
            "/v1/assignments",
            {
                "state": "waiting",
                "after": "a",
                "limit": 5,
                "role": "sent",
                "source_session_id": _CONV,
            },
        )
    ]
    assert out == {"data": []}


@pytest.mark.asyncio
async def test_list_received_scopes_to_calling_session() -> None:
    client = _RecordingClient(_Resp(body={"data": []}))
    _, calls = await _run("sys_assignment_list", {"role": "received"}, client)
    assert calls == [("GET", "/v1/assignments", {"role": "received", "session_id": _CONV})]


@pytest.mark.asyncio
async def test_send_posts_sender_session() -> None:
    client = _RecordingClient(_Resp(body={"id": "m1"}))
    out, calls = await _run(
        "sys_assignment_send",
        {"assignment_id": _ASSIGNMENT_ID, "body": "progress", "idempotency_key": "k1"},
        client,
    )
    assert calls == [
        (
            "POST",
            f"/v1/assignments/{_ASSIGNMENT_ID}/messages",
            {"sender_session_id": _CONV, "body": "progress", "idempotency_key": "k1"},
        )
    ]
    assert out["id"] == "m1"


@pytest.mark.asyncio
async def test_read_messages_gets_with_cursor() -> None:
    client = _RecordingClient(_Resp(body={"data": []}))
    _, calls = await _run(
        "sys_assignment_read_messages",
        {"assignment_id": _ASSIGNMENT_ID, "after": "m0", "limit": 10},
        client,
    )
    assert calls == [
        ("GET", f"/v1/assignments/{_ASSIGNMENT_ID}/messages", {"after": "m0", "limit": 10})
    ]


@pytest.mark.asyncio
async def test_cancel_posts_reason() -> None:
    client = _RecordingClient(_Resp(body={"state": "cancelled"}))
    out, calls = await _run(
        "sys_assignment_cancel", {"assignment_id": _ASSIGNMENT_ID, "reason": "nope"}, client
    )
    assert calls == [("POST", f"/v1/assignments/{_ASSIGNMENT_ID}/cancel", {"reason": "nope"})]
    assert out["state"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_without_reason_posts_empty_object() -> None:
    client = _RecordingClient(_Resp(body={"state": "cancelled"}))
    _, calls = await _run("sys_assignment_cancel", {"assignment_id": _ASSIGNMENT_ID}, client)
    assert calls == [("POST", f"/v1/assignments/{_ASSIGNMENT_ID}/cancel", {})]


@pytest.mark.asyncio
async def test_missing_args_error_before_http() -> None:
    client = _RecordingClient()
    for tool_name, args in [
        ("sys_assignment_get", {}),
        ("sys_assignment_send", {"assignment_id": _ASSIGNMENT_ID}),
        ("sys_assignment_read_messages", {}),
        ("sys_assignment_cancel", {}),
    ]:
        out, calls = await _run(tool_name, args, client)
        assert "error" in out
        assert calls == []


@pytest.mark.asyncio
async def test_missing_session_or_client_errors() -> None:
    out = json.loads(
        await execute_assignment_tool(
            "sys_assignment_get",
            json.dumps({"assignment_id": _ASSIGNMENT_ID}),
            conversation_id=None,
            runner_workspace=None,
            server_client=_RecordingClient(),  # type: ignore[arg-type]
        )
    )
    assert "session id" in out["error"]
    out = json.loads(
        await execute_assignment_tool(
            "sys_assignment_get",
            json.dumps({"assignment_id": _ASSIGNMENT_ID}),
            conversation_id=_CONV,
            runner_workspace=None,
            server_client=None,
        )
    )
    assert "server access" in out["error"]
    out = json.loads(
        await execute_assignment_tool(
            "sys_assignment_get",
            "{not json",
            conversation_id=_CONV,
            runner_workspace=None,
            server_client=_RecordingClient(),  # type: ignore[arg-type]
        )
    )
    assert "malformed" in out["error"]


@pytest.mark.asyncio
async def test_server_error_maps_status_and_details() -> None:
    client = _RecordingClient(_Resp(status_code=409, body={"error": {"message": "conflict"}}))
    out, _ = await _run("sys_assignment_get", {"assignment_id": _ASSIGNMENT_ID}, client)
    assert "server returned 409" in out["error"]
    assert "conflict" in out["details"]


@pytest.mark.asyncio
async def test_execute_tool_branch_routes_assignment_tools() -> None:
    """The ``execute_tool`` chain reaches the assignment executor."""
    client = _RecordingClient(_Resp(body={"id": _ASSIGNMENT_ID}))
    out = await execute_tool(
        tool_name="sys_assignment_get",
        arguments=json.dumps({"assignment_id": _ASSIGNMENT_ID}),
        server_client=client,  # type: ignore[arg-type]
        conversation_id=_CONV,
    )
    assert json.loads(out)["id"] == _ASSIGNMENT_ID
    assert client.calls == [("GET", f"/v1/assignments/{_ASSIGNMENT_ID}", None)]
    assert should_dispatch_locally("sys_assignment_dispatch")
    assert should_dispatch_locally("sys_assignment_complete")


# Grant gate (_granted_tool_names/_ungranted_tool_reason) mirrors the flag-gated advertisement.
@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_grant_gate_mirrors_project_assignments_flag(
    monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    """The gate admits ``sys_assignment_list`` only while the session flag is on."""
    from omnigent.runner import assignment_tools, tool_dispatch
    from omnigent.spec.types import AgentSpec

    reached: list[str] = []

    async def _record(tool_name: str, _arguments: str, **_kwargs: Any) -> str:
        reached.append(tool_name)
        return json.dumps({"ok": True})

    monkeypatch.setattr(assignment_tools, "execute_assignment_tool", _record)
    monkeypatch.setattr(tool_dispatch, "_project_assignments_enabled_for", lambda _cid: enabled)

    out = await execute_tool(
        tool_name="sys_assignment_list",
        arguments=json.dumps({"role": "sent"}),
        agent_spec=AgentSpec(spec_version=1),
        conversation_id=_CONV,
    )

    if enabled:
        assert json.loads(out) == {"ok": True}
        assert reached == ["sys_assignment_list"]
    else:
        assert "not enabled" in out
        assert reached == []
