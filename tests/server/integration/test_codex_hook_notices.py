"""Hook notices survive session reload without duplicate items or agent input."""

from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as fwd
from tests.server.helpers import create_test_agent


@pytest.mark.asyncio
async def test_hook_notice_persists_once_and_publishes_without_input(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Replay the native notification through the real HTTP route and store."""
    agent = await create_test_agent(client)
    response = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert response.status_code < 300, response.text
    session_id = response.json()["id"]
    published = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda sid, event: published.append((sid, event)),
    )
    event = {
        "method": "hook/completed",
        "params": {
            "threadId": "native-thread",
            "run": {
                "id": "health-hook",
                "entries": [
                    {"kind": "warning", "text": "Maintenance needs attention"},
                    {"kind": "context", "text": "Do not mirror model context"},
                ],
            },
        },
    }
    for replay in (False, True):
        await fwd._handle_event(
            client,
            session_id=session_id,
            bridge_dir=tmp_path,
            event=event,
            usage_coalescer=fwd._SessionUsageCoalescer(client, session_id),
            elicitation_tracker=fwd._CodexElicitationTaskTracker(),
            expected_thread_id="native-thread",
            is_replay=replay,
        )
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    assert len(items) == 1
    assert items[0]["type"] == "error"
    assert items[0]["message"] == "Maintenance needs attention"
    assert items[0]["level"] == "info"
    assert [(sid, item["type"]) for sid, item in published] == [
        (session_id, "response.output_item.done")
    ]
    assert published[0][1]["item"]["id"] == items[0]["id"]
