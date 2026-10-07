"""Unit tests for runner session-history conversion and attachment restoration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.inner.native_attachments import session_attachment_dir
from omnigent.runner.session_history import build_session_history

_OTHER_HOST_KEY = "a" * 32
_OTHER_HOST_LINE = (
    f"[Attached: /home/other/.omnigent/attachments/s-{_OTHER_HOST_KEY}/file_mp4/a.mp4]"
)


async def _noop_persist_cancellation_items(session_id: str, items: list[dict[str, Any]]) -> None:
    """Cancellation persistence is not exercised by history-load tests."""
    del session_id, items


def _compacted_items() -> list[dict[str, Any]]:
    """One compaction whose persisted user text carries an old-host path."""
    return [
        {
            "id": "cmp_1",
            "type": "compaction",
            "summary": "summary",
            "last_item_id": "msg_1",
            "model": "test-model",
            "token_count": 1,
            "compacted_messages": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": f"{_OTHER_HOST_LINE} please inspect"}
                    ],
                }
            ],
        },
        {
            "id": "msg_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "ok"}],
        },
    ]


@pytest.mark.asyncio
async def test_load_history_restores_an_unchanged_attached_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A line already pointing at this host still triggers the restore.

    Cleanup can delete the file without touching the persisted text, so a
    rewrite that leaves the text unchanged must not skip re-materialization.
    """
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    session_id = "conv_hist_local"
    file_bytes = b"hello"
    local_path = session_attachment_dir(session_id) / "file_mp4" / "a.mp4"
    assert not local_path.exists()
    content_gets = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal content_gets
        path = request.url.path
        if path.endswith("/resources/files"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "file_mp4",
                            "name": "a.mp4",
                            "metadata": {
                                "bytes": len(file_bytes),
                                "source_metadata": {"delivery": "filesystem"},
                            },
                        }
                    ],
                    "has_more": False,
                },
            )
        if path.endswith("/resources/files/file_mp4/content"):
            content_gets += 1
            return httpx.Response(200, content=file_bytes)
        if path.endswith(f"/sessions/{session_id}/items"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "cmp_1",
                            "type": "compaction",
                            "summary": "summary",
                            "last_item_id": "msg_1",
                            "model": "test-model",
                            "token_count": 1,
                            "compacted_messages": [
                                {
                                    "type": "message",
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": f"[Attached: {local_path}] inspect",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                    "has_more": False,
                },
            )
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        history = build_session_history(
            _background_tasks=set(),
            _last_server_item_id={},
            _persist_cancellation_items=_noop_persist_cancellation_items,
            _session_histories={},
            _session_spec_cache={},
            server_client=client,
        )
        converted = await history.load_history_as_input(session_id)

    assert converted[0]["content"][0]["text"] == f"[Attached: {local_path}] inspect"
    assert local_path.read_bytes() == file_bytes
    assert content_gets == 1


@pytest.mark.asyncio
async def test_load_history_rewrites_and_restores_persisted_attached_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compacted [Attached:] line from another home is redirected and restored.

    Design scenario 7: compaction persists host paths. On history load the
    runner must rewrite the layout path to this host's session dir and
    materialize the file before any harness consumes the history.
    """
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    session_id = "conv_hist"
    file_bytes = b"hello"
    content_gets = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal content_gets
        path = request.url.path
        if path.endswith("/resources/files"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "file_mp4",
                            "name": "a.mp4",
                            "metadata": {
                                "bytes": len(file_bytes),
                                "source_metadata": {"delivery": "filesystem"},
                            },
                        }
                    ],
                    "has_more": False,
                },
            )
        if path.endswith("/resources/files/file_mp4/content"):
            content_gets += 1
            return httpx.Response(200, content=file_bytes)
        if path.endswith(f"/sessions/{session_id}/items"):
            return httpx.Response(200, json={"data": _compacted_items(), "has_more": False})
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        history = build_session_history(
            _background_tasks=set(),
            _last_server_item_id={},
            _persist_cancellation_items=_noop_persist_cancellation_items,
            _session_histories={},
            _session_spec_cache={},
            server_client=client,
        )
        converted = await history.load_history_as_input(session_id)

    expected = session_attachment_dir(session_id) / "file_mp4" / "a.mp4"
    assert converted[0]["content"][0]["text"] == f"[Attached: {expected}] please inspect"
    assert expected.read_bytes() == file_bytes
    assert content_gets == 1


@pytest.mark.asyncio
async def test_load_history_restores_a_plain_string_message_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message whose content is a plain string still triggers the restore.

    Harness compactors may persist ``content`` as a raw string; the path line
    in it must count toward ``restore_needed`` and re-materialize the file.
    """
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    session_id = "conv_hist_str"
    file_bytes = b"hello"
    local_path = session_attachment_dir(session_id) / "file_mp4" / "a.mp4"
    assert not local_path.exists()
    content_gets = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal content_gets
        path = request.url.path
        if path.endswith("/resources/files"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "file_mp4",
                            "name": "a.mp4",
                            "metadata": {
                                "bytes": len(file_bytes),
                                "source_metadata": {"delivery": "filesystem"},
                            },
                        }
                    ],
                    "has_more": False,
                },
            )
        if path.endswith("/resources/files/file_mp4/content"):
            content_gets += 1
            return httpx.Response(200, content=file_bytes)
        if path.endswith(f"/sessions/{session_id}/items"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "msg_1",
                            "type": "message",
                            "role": "user",
                            "content": f"[Attached: {local_path}] inspect",
                        }
                    ],
                    "has_more": False,
                },
            )
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        history = build_session_history(
            _background_tasks=set(),
            _last_server_item_id={},
            _persist_cancellation_items=_noop_persist_cancellation_items,
            _session_histories={},
            _session_spec_cache={},
            server_client=client,
        )
        converted = await history.load_history_as_input(session_id)

    assert converted[0]["content"] == f"[Attached: {local_path}] inspect"
    assert local_path.read_bytes() == file_bytes
    assert content_gets == 1
