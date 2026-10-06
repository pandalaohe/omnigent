"""Thread-name auto-title tests for the Codex forwarder."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from tests.harnesses.codex_native.session._support import (
    _elicitation_tracker,
    _usage_coalescer,
)

_MISSING = object()


def _thread_name_event(thread_id: str | None, thread_name: object = _MISSING) -> dict[str, Any]:
    """Build one ``thread/name/updated`` notification envelope."""
    params: dict[str, Any] = {}
    if thread_id is not None:
        params["threadId"] = thread_id
    if thread_name is not _MISSING:
        params["threadName"] = thread_name
    return {"method": "thread/name/updated", "params": params}


def _recording_transport(posted: list[dict[str, Any]]) -> httpx.MockTransport:
    """Record method, path and JSON body of every forwarder post."""

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(
            {
                "method": request.method,
                "path": request.url.path,
                "json": json.loads(request.content),
            }
        )
        return httpx.Response(
            200,
            json={"renamed": True, "title": "Debug login timeout", "reason": None},
        )

    return httpx.MockTransport(handler)


async def _drive(
    client: httpx.AsyncClient,
    tmp_path: Path,
    events: list[dict[str, Any]],
    *,
    forwarder_state: codex_native_forwarder._CodexForwarderState,
    expected_thread_id: str | None,
    is_replay: bool = False,
) -> None:
    for event in events:
        await codex_native_forwarder._handle_event(
            client,
            session_id="conv_main",
            bridge_dir=tmp_path,
            usage_coalescer=_usage_coalescer(client, "conv_main"),
            elicitation_tracker=_elicitation_tracker(),
            event=event,
            expected_thread_id=expected_thread_id,
            forwarder_state=forwarder_state,
            is_replay=is_replay,
        )


async def test_main_thread_name_posts_auto_title(tmp_path: Path) -> None:
    posted: list[dict[str, Any]] = []
    forwarder_state = codex_native_forwarder._CodexForwarderState(parent_session_id="conv_main")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        transport=_recording_transport(posted),
    ) as client:
        await _drive(
            client,
            tmp_path,
            [_thread_name_event("thread_main", "  Debug   login timeout ")],
            forwarder_state=forwarder_state,
            expected_thread_id="thread_main",
        )
    assert posted == [
        {
            "method": "POST",
            "path": "/v1/sessions/conv_main/auto-title",
            "json": {"title": "Debug login timeout"},
        }
    ]


async def test_child_thread_name_is_not_posted(tmp_path: Path) -> None:
    posted: list[dict[str, Any]] = []
    forwarder_state = codex_native_forwarder._CodexForwarderState(parent_session_id="conv_main")
    forwarder_state.note_child_thread("thread_child", "conv_child")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        transport=_recording_transport(posted),
    ) as client:
        await _drive(
            client,
            tmp_path,
            [_thread_name_event("thread_child", "Debug login timeout")],
            forwarder_state=forwarder_state,
            expected_thread_id="thread_main",
        )
    assert posted == []


async def test_other_thread_name_is_not_posted(tmp_path: Path) -> None:
    posted: list[dict[str, Any]] = []
    forwarder_state = codex_native_forwarder._CodexForwarderState(parent_session_id="conv_main")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        transport=_recording_transport(posted),
    ) as client:
        await _drive(
            client,
            tmp_path,
            [_thread_name_event("thread_other", "Debug login timeout")],
            forwarder_state=forwarder_state,
            expected_thread_id="thread_main",
        )
    assert posted == []


async def test_thread_name_without_expected_thread_is_not_posted(tmp_path: Path) -> None:
    posted: list[dict[str, Any]] = []
    forwarder_state = codex_native_forwarder._CodexForwarderState(parent_session_id="conv_main")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        transport=_recording_transport(posted),
    ) as client:
        await _drive(
            client,
            tmp_path,
            [_thread_name_event("thread_main", "Debug login timeout")],
            forwarder_state=forwarder_state,
            expected_thread_id=None,
        )
    assert posted == []


async def test_replayed_thread_name_is_not_posted(tmp_path: Path) -> None:
    posted: list[dict[str, Any]] = []
    forwarder_state = codex_native_forwarder._CodexForwarderState(parent_session_id="conv_main")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        transport=_recording_transport(posted),
    ) as client:
        await _drive(
            client,
            tmp_path,
            [_thread_name_event("thread_main", "Debug login timeout")],
            forwarder_state=forwarder_state,
            expected_thread_id="thread_main",
            is_replay=True,
        )
    assert posted == []


@pytest.mark.parametrize(
    "event",
    [
        _thread_name_event("thread_main", None),
        _thread_name_event("thread_main"),
        _thread_name_event("thread_main", "x"),
    ],
    ids=["null-name", "missing-name", "one-character"],
)
async def test_unusable_thread_name_is_not_posted(tmp_path: Path, event: dict[str, Any]) -> None:
    posted: list[dict[str, Any]] = []
    forwarder_state = codex_native_forwarder._CodexForwarderState(parent_session_id="conv_main")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        transport=_recording_transport(posted),
    ) as client:
        await _drive(
            client,
            tmp_path,
            [event],
            forwarder_state=forwarder_state,
            expected_thread_id="thread_main",
        )
    assert posted == []


async def test_auto_title_server_error_does_not_raise(tmp_path: Path) -> None:
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append({"method": request.method, "path": request.url.path})
        return httpx.Response(500, json={"detail": "boom"})

    forwarder_state = codex_native_forwarder._CodexForwarderState(parent_session_id="conv_main")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    ) as client:
        await _drive(
            client,
            tmp_path,
            [_thread_name_event("thread_main", "Debug login timeout")],
            forwarder_state=forwarder_state,
            expected_thread_id="thread_main",
        )
    assert posted == [{"method": "POST", "path": "/v1/sessions/conv_main/auto-title"}]


async def test_auto_title_transport_error_does_not_raise(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    forwarder_state = codex_native_forwarder._CodexForwarderState(parent_session_id="conv_main")
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    ) as client:
        await _drive(
            client,
            tmp_path,
            [_thread_name_event("thread_main", "Debug login timeout")],
            forwarder_state=forwarder_state,
            expected_thread_id="thread_main",
        )
