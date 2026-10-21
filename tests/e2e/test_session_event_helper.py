"""Reply-shape coverage for :func:`send_user_message_to_session`.

The live e2e tests need a real runner; these pin the helper's reading of the
two answers the events endpoint gives — a turn it queued (``item_id``) and a
message a still-starting session holds in its startup queue (``pending_id``,
the answer pi-native's runner boot produces) — without one.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.e2e import conftest as e2e_conftest
from tests.e2e.conftest import _pending_input_response_id, send_user_message_to_session

_SESSION_ID = "conv_helper"


def _client(
    post_reply: dict[str, Any],
    snapshots: list[dict[str, Any]],
) -> tuple[httpx.Client, list[str]]:
    """Serve one POST reply, then each snapshot in turn (the last repeats)."""
    paths: list[str] = []
    polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(200, json=post_reply)
        snapshot = snapshots[min(polls, len(snapshots) - 1)]
        polls += 1
        return httpx.Response(200, json=snapshot)

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://test"), paths


def _user_item(item_id: str, response_id: str, text: str) -> dict[str, Any]:
    return {
        "id": item_id,
        "response_id": response_id,
        "type": "message",
        "status": "completed",
        "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
    }


def _send(client: httpx.Client, **kwargs: Any) -> str:
    return send_user_message_to_session(client, session_id=_SESSION_ID, content="hello", **kwargs)


def test_queued_turn_returns_the_persisted_items_response_id() -> None:
    client, _ = _client(
        {"queued": True, "item_id": "item_1"},
        [{"items": [_user_item("item_1", "resp_1", "hello")]}],
    )
    assert _send(client) == "resp_1"


def test_startup_queued_message_returns_the_dispatched_response_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(e2e_conftest, "POLL_INTERVAL_S", 0)
    client, _ = _client(
        {"queued": True, "pending_id": "pending_" + "a" * 32},
        [
            {"items": [_user_item("item_queued", "pending_input", "hello")]},
            {"items": [_user_item("item_1", "resp_1", "hello")]},
        ],
    )
    assert _send(client) == "resp_1"


def test_startup_queued_message_matches_its_own_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """A newer message is not mistaken for the queued one."""
    monkeypatch.setattr(e2e_conftest, "POLL_INTERVAL_S", 0)
    newer = _user_item("item_newer", "resp_newer", "a later message")
    client, _ = _client(
        {"queued": True, "pending_id": "pending_" + "a" * 32},
        [
            {"items": [_user_item("item_queued", "pending_input", "hello"), newer]},
            {"items": [_user_item("item_1", "resp_1", "hello"), newer]},
        ],
    )
    assert _send(client) == "resp_1"


def test_undispatched_startup_message_fails_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(e2e_conftest, "POLL_INTERVAL_S", 0)
    client, _ = _client(
        {"queued": True, "pending_id": "pending_" + "a" * 32},
        [{"items": [_user_item("item_queued", "pending_input", "hello")]}],
    )
    with pytest.raises(AssertionError, match="was not dispatched"):
        _pending_input_response_id(
            client,
            session_id=_SESSION_ID,
            content=[{"type": "input_text", "text": "hello"}],
            pending_id="pending_" + "a" * 32,
            timeout=0.05,
        )


def test_synchronous_verdict_still_fails_loud() -> None:
    client, _ = _client({"denied": True, "reason": "policy"}, [{"items": []}])
    with pytest.raises(AssertionError, match="did not queue a turn"):
        _send(client)
