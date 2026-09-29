"""Runner REST paths for session archive / unarchive and the close fix."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from omnigent.runner.tool_dispatch import (
    _execute_session_query_tool,
    _find_existing_child_session,
    _session_archive_via_rest,
    build_native_relay_tool_schemas,
)
from omnigent.spec.types import AgentSpec
from omnigent.tools.manager import ToolManager
from omnigent.util.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Build an AsyncClient answered by ``handler``."""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")


@pytest.mark.asyncio
async def test_session_close_closes_created_child_with_plain_title() -> None:
    """
    ``sys_session_close`` closes a ``sys_session_create`` child whose title
    is not ``"<agent>:<title>"``.

    ``sys_session_create`` stores the caller's title verbatim, so a plain
    title such as ``"build fix"`` has no colon. The tree gate (same root,
    has a parent) is what makes it a sub-agent; the title shape must not
    refuse it. The tombstone keeps the display title and frees the
    duplicate-title slot.
    """
    from omnigent.runner.tool_dispatch import _execute_session_query_tool

    patched: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_child":
            return httpx.Response(
                200,
                json={
                    "id": "conv_child",
                    "title": "build fix",
                    "agent_name": "claude-native-ui",
                    "sub_agent_name": None,
                    "root_conversation_id": "conv_root",
                    "parent_session_id": "conv_root",
                },
            )
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_root":
            return httpx.Response(
                200, json={"id": "conv_root", "root_conversation_id": "conv_root"}
            )
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_child":
            patched.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "conv_child"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_close",
                json.dumps({"conversation_id": "conv_child"}),
                conversation_id="conv_root",
                server_client=client,
            )
        )
    assert out["closed"] is True
    assert out["conversation_id"] == "conv_child"
    assert out["title"] == "build fix"
    assert patched["title"] == "build fix:closed:conv_child"
    assert patched["labels"] == {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE}
    assert patched["archived"] is True


async def _archive(
    client: httpx.AsyncClient,
    *,
    tool_name: str = "sys_session_archive",
    args: dict[str, Any],
    conversation_id: str,
) -> dict[str, Any]:
    """Call the REST archive handler and decode its JSON result."""
    out = await _session_archive_via_rest(
        tool_name,
        args,
        conversation_id=conversation_id,
        server_client=client,
    )
    return json.loads(out)


@pytest.mark.asyncio
async def test_archive_other_session_patches_archived_without_idle_deferral() -> None:
    """Archiving another own session PATCHes the flag without a deferral.

    The target is not the caller or an ancestor, so the caller's turn is
    not at risk: no ``stop_when_idle``, and the result names the undo
    window as the stop condition.
    """
    patch_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_other":
            return httpx.Response(200, json={"id": "conv_other", "archived": False})
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(200, json={"id": "conv_caller", "parent_session_id": None})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_other":
            patch_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "conv_other"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = await _archive(
            client,
            args={"session_id": "conv_other"},
            conversation_id="conv_caller",
        )
    assert patch_bodies == [{"archived": True}]
    assert out["archived"] is True
    assert out["session_id"] == "conv_other"
    assert "undo window" in out["runner_stop"]
    assert out["undo"]


@pytest.mark.asyncio
async def test_archive_other_owner_maps_403_to_access_denied() -> None:
    """A shared-but-not-owned target is refused by the owner gate."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_shared":
            return httpx.Response(200, json={"id": "conv_shared", "archived": False})
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(200, json={"id": "conv_caller", "parent_session_id": None})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_shared":
            return httpx.Response(403, json={"error": {"message": "owner required"}})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = await _archive(
            client,
            args={"session_id": "conv_shared"},
            conversation_id="conv_caller",
        )
    assert out["error"] == "access_denied"
    assert out["session_id"] == "conv_shared"


@pytest.mark.asyncio
async def test_archive_self_patches_stop_when_idle() -> None:
    """Self-archive defers the teardown until the caller's turn ends."""
    patch_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_self":
            return httpx.Response(200, json={"id": "conv_self", "archived": False})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_self":
            patch_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "conv_self"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = await _archive(client, args={}, conversation_id="conv_self")
    assert patch_bodies == [{"archived": True, "stop_when_idle": True}]
    assert out["archived"] is True
    assert "current turn" in out["runner_stop"]


@pytest.mark.asyncio
async def test_archive_ancestor_patches_stop_when_idle() -> None:
    """Archiving the caller's parent defers to protect the caller's turn."""
    patch_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_parent":
            return httpx.Response(200, json={"id": "conv_parent", "archived": False})
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_child":
            return httpx.Response(
                200, json={"id": "conv_child", "parent_session_id": "conv_parent"}
            )
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_parent":
            patch_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "conv_parent"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = await _archive(
            client,
            args={"session_id": "conv_parent"},
            conversation_id="conv_child",
        )
    assert patch_bodies == [{"archived": True, "stop_when_idle": True}]
    assert out["archived"] is True


@pytest.mark.asyncio
async def test_archive_already_archived_still_patches() -> None:
    """An already-archived target still PATCHes so the owner gate answers."""
    patch_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_other":
            return httpx.Response(200, json={"id": "conv_other", "archived": True})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_other":
            patch_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "conv_other"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = await _archive(
            client,
            args={"session_id": "conv_other"},
            conversation_id="conv_caller",
        )
    assert patch_bodies == [{"archived": True}]
    assert out["already_archived"] is True
    assert "stop_when_idle" not in patch_bodies[0]


@pytest.mark.asyncio
async def test_unarchive_patches_archived_false() -> None:
    """Unarchive always PATCHes the false flag."""
    patch_bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_other":
            return httpx.Response(200, json={"id": "conv_other", "archived": True})
        if request.method == "PATCH" and request.url.path == "/v1/sessions/conv_other":
            patch_bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "conv_other"})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = await _archive(
            client,
            tool_name="sys_session_unarchive",
            args={"session_id": "conv_other"},
            conversation_id="conv_caller",
        )
    assert patch_bodies == [{"archived": False}]
    assert out["archived"] is False
    assert "already_unarchived" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [{}, {"session_id": 123}])
async def test_unarchive_requires_a_string_session_id(args: dict[str, Any]) -> None:
    """Unarchive with a missing or non-string session id errors without a request."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = await _archive(
            client,
            tool_name="sys_session_unarchive",
            args=args,
            conversation_id="conv_caller",
        )
    assert "requires a non-empty 'session_id' string" in out["error"]


@pytest.mark.asyncio
async def test_session_list_archived_only_maps_to_visibility_archived() -> None:
    """``archived="only"`` queries the server's archived visibility."""
    global_params: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(200, json={"id": "conv_caller", "parent_session_id": None})
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_caller/child_sessions"
        ):
            return httpx.Response(200, json={"data": [], "has_more": False})
        if request.method == "GET" and request.url.path == "/v1/sessions":
            global_params.append(request.url.params)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "conv_gone", "archived": True, "archived_at": 42},
                    ],
                    "has_more": False,
                    "last_id": "conv_gone",
                },
            )
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_list",
                json.dumps({"archived": "only"}),
                conversation_id="conv_caller",
                server_client=client,
            )
        )
    assert global_params[0]["visibility"] == "archived"
    assert "include_archived" not in global_params[0]
    assert out["sessions"][0]["archived"] is True
    assert out["sessions"][0]["archived_at"] == 42


@pytest.mark.asyncio
async def test_session_list_archived_include_adds_include_archived_param() -> None:
    """``archived="include"`` adds ``include_archived=true`` to the query."""
    global_params: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/v1/sessions/conv_caller":
            return httpx.Response(200, json={"id": "conv_caller", "parent_session_id": None})
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_caller/child_sessions"
        ):
            return httpx.Response(200, json={"data": [], "has_more": False})
        if request.method == "GET" and request.url.path == "/v1/sessions":
            global_params.append(request.url.params)
            return httpx.Response(200, json={"data": [], "has_more": False})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_list",
                json.dumps({"archived": "include"}),
                conversation_id="conv_caller",
                server_client=client,
            )
        )
    assert global_params[0]["visibility"] == "all"
    assert global_params[0]["include_archived"] == "true"
    assert out["sessions"] == []


@pytest.mark.asyncio
async def test_session_list_rejects_a_bad_archived_value() -> None:
    """An unknown ``archived`` value fails fast with a typed error."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = json.loads(
            await _execute_session_query_tool(
                "sys_session_list",
                json.dumps({"archived": "all"}),
                conversation_id="conv_caller",
                server_client=client,
            )
        )
    assert out["error"] == "sys_session_list 'archived' must be one of exclude, include, only"


@pytest.mark.asyncio
async def test_find_existing_child_session_reports_archived_child() -> None:
    """An archived named child is reported instead of silently re-created."""
    seen_params: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.method == "GET"
            and request.url.path == "/v1/sessions/conv_parent/child_sessions"
        ):
            seen_params.append(request.url.params)
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "conv_kid", "title": "claude:x", "labels": {}, "archived": True},
                    ],
                    "has_more": False,
                    "last_id": "conv_kid",
                },
            )
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with _client(handler) as client:
        out = await _find_existing_child_session(
            server_client=client,
            conversation_id="conv_parent",
            agent="claude",
            title="x",
        )
    assert isinstance(out, str)
    payload = json.loads(out)
    assert payload["error"] == "session_archived"
    assert payload["conversation_id"] == "conv_kid"
    assert "sys_session_unarchive" in payload["message"]
    assert seen_params[0]["include_archived"] == "true"


def _spec() -> AgentSpec:
    """A no-spawn spec, so the peer-messaging flag is the only archive grant."""
    return AgentSpec(spec_version=1)


def test_archive_tools_follow_the_peer_messaging_flag() -> None:
    """``ToolManager`` advertises both archive tools iff the flag is on."""
    names_on = {
        schema["function"]["name"]
        for schema in ToolManager(_spec(), peer_messaging_enabled=True).get_tool_schemas()
    }
    assert {"sys_session_archive", "sys_session_unarchive"} <= names_on
    names_off = {
        schema["function"]["name"]
        for schema in ToolManager(_spec(), peer_messaging_enabled=False).get_tool_schemas()
    }
    assert not {"sys_session_archive", "sys_session_unarchive"} & names_off


def test_archive_tools_in_the_spec_less_relay_fallback() -> None:
    """The spec-less relay fallback carries both archive tools iff flagged on."""
    on = {s["name"] for s in build_native_relay_tool_schemas(None, peer_messaging_enabled=True)}
    assert {"sys_session_archive", "sys_session_unarchive"} <= on
    off = {s["name"] for s in build_native_relay_tool_schemas(None, peer_messaging_enabled=False)}
    assert not {"sys_session_archive", "sys_session_unarchive"} & off
