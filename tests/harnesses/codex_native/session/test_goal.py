"""Goal-notification tests for Codex session."""

from __future__ import annotations

import asyncio
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


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("active", "active"),
        ("paused", "paused"),
        ("blocked", "paused"),
        ("usageLimited", "paused"),
        ("budgetLimited", "paused"),
        ("complete", None),
    ],
)
def test_normalized_codex_goal_state(status: str, expected: str | None) -> None:
    observed, state = codex_native_forwarder._normalized_codex_goal_state({"status": status})
    assert observed is True
    assert state == expected


@pytest.mark.parametrize("goal", [{}, {"status": []}, {"status": "future"}, False])
def test_normalized_codex_goal_state_ignores_unknown(goal: object) -> None:
    assert codex_native_forwarder._normalized_codex_goal_state(goal) == (False, None)


def test_forwarder_posts_goal_notifications_and_dedupes(tmp_path: Path) -> None:
    posted: list[dict[str, Any]] = []
    forwarder_state = codex_native_forwarder._CodexForwarderState(parent_session_id="conv_123")

    def handler(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": False})

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            for event in [
                {"method": "thread/goal/updated", "params": {"threadId": "thread_123"}},
                {
                    "method": "thread/goal/updated",
                    "params": {"threadId": "thread_123", "goal": {"status": "active"}},
                },
                {
                    "method": "thread/goal/updated",
                    "params": {"threadId": "thread_123", "goal": {"status": "active"}},
                },
                {
                    "method": "thread/goal/updated",
                    "params": {"threadId": "thread_123", "goal": {"status": "blocked"}},
                },
                {
                    "method": "thread/goal/cleared",
                    "params": {"threadId": "thread_123"},
                },
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    session_id="conv_123",
                    bridge_dir=tmp_path,
                    usage_coalescer=_usage_coalescer(client),
                    elicitation_tracker=_elicitation_tracker(),
                    event=event,
                    expected_thread_id="thread_123",
                    forwarder_state=forwarder_state,
                )

    asyncio.run(run())
    assert posted == [
        {"type": "external_goal_state", "data": {"state": "active"}},
        {"type": "external_goal_state", "data": {"state": "paused"}},
        {"type": "external_goal_state", "data": {"state": None}},
    ]


def test_forwarder_parent_rotation_resets_goal_dedupe() -> None:
    forwarder_state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_old",
        posted_goal_state="active",
        posted_goal_state_known=True,
    )
    forwarder_state.note_parent_rotation("conv_new")
    assert forwarder_state.parent_session_id == "conv_new"
    assert forwarder_state.posted_goal_state is None
    assert forwarder_state.posted_goal_state_known is False
