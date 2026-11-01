"""Real Claude hook admission survives forwarding and a cold history replay."""

import asyncio
import json
import os
import shlex
import shutil
import sys
import time

import httpx
import pexpect
import pytest
from fastapi import FastAPI

from omnigent.harnesses.claude_native import bridge, forwarder
from omnigent.native.auto_retry import (
    AutoRetryGuard,
    auto_retry_scope,
    observe_turn,
    prepare_submission,
)
from omnigent.runtime import pending_inputs
from tests._helpers.terminal_screen import TerminalScreen
from tests.e2e.test_claude_native_billing_notice_e2e import _local_gateway
from tests.server.integration.mock_llm_server import anthropic_sse_text_response
from tests.server.routes.test_native_retry_echo import accept_native, prepared_native
from tests.server.routes.test_session_event_batches import _make_client


class _TextGateway:
    def response(self, body):
        return anthropic_sse_text_response(
            "Echo complete.", model=body.get("model")
        ).encode(), "text/event-stream"


@pytest.mark.asyncio
async def test_real_claude_automatic_echo_and_human_retry_survive_reload(tmp_path, db_uri):
    binary = shutil.which("claude")
    assert binary, "Pinned Claude CLI required"
    journal, chain, claim = prepared_native(db_uri)
    workspace, config = tmp_path / "workspace", tmp_path / "claude-config"
    workspace.mkdir()
    config.mkdir()
    directory = bridge.prepare_bridge_dir(journal.child, workspace=workspace)
    command = shlex.join(
        [
            sys.executable,
            "-m",
            "omnigent.harnesses.claude_native.hook",
            "--bridge-dir",
            str(directory),
        ]
    )
    (config / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    name: [{"hooks": [{"type": "command", "command": command}]}]
                    for name in ("SessionStart", "UserPromptSubmit", "Stop")
                }
            }
        )
    )
    (config / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(config),
        "ANTHROPIC_AUTH_TOKEN": "local-echo-test",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "DISABLE_AUTOUPDATER": "1",
        "TERM": "xterm-256color",
    }
    server = _make_client(journal.conversations)
    app = FastAPI()
    app.mount("/v1", server.app)
    history = []
    with _local_gateway(_TextGateway()) as url:
        env["ANTHROPIC_BASE_URL"] = url
        child = pexpect.spawn(
            binary,
            [
                "--model",
                "claude-sonnet-4-6",
                "--strict-mcp-config",
                "--mcp-config",
                '{"mcpServers":{}}',
            ],
            cwd=str(workspace),
            env=env,
            encoding="utf-8",
            timeout=60,
            dimensions=(40, 160),
        )
        screen = TerminalScreen(child)

        def hooks():
            return bridge.read_hook_events_since_with_position(directory, 0).records

        def parsed():
            path = bridge.read_transcript_path(directory)
            if path is None or not path.exists():
                return None
            return bridge.read_transcript_items_from_offset(
                path, 0, start_line=0, agent_name="claude"
            )

        try:
            screen.wait_for(
                lambda text: "❯" in text and "shift+tab" in text, timeout=45, what="composer"
            )
            snapshot = observe_turn(directory, journal.child, "root-turn", idle=True)
            guard = AutoRetryGuard(
                session_id=journal.child,
                chain_id=chain.chain_id,
                input_id=claim.input_id,
                expected_turn_id="root-turn",
                expected_input_generation=1,
                expected_native_generation=snapshot.generation,
                admission_deadline=time.time() + 60,
                attempt=1,
                of=chain.limit,
            )
            with auto_retry_scope(guard):
                prepare_submission(directory, native_idle=True)
            pending_inputs.record(
                journal.child,
                [{"type": "input_text", "text": "retry"}],
                stable_id=claim.input_id,
                auto_retry=claim.retry.metadata,
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://server",
                headers=dict(server.headers),
            ) as client:
                statuses = []

                async def record_status(response):
                    if response.request.url.path.endswith("/events"):
                        statuses.append(response.status_code)

                client.event_hooks["response"].append(record_status)
                for turn in (1, 2):
                    child.send("retry")
                    screen.wait_for(
                        lambda text: (
                            "❯" in text
                            and text.rsplit("❯", 1)[1].splitlines()[0].strip() == "retry"
                        ),
                        timeout=10,
                        what="retry draft",
                    )
                    child.send("\r")
                    screen.wait_for(
                        lambda _text, expected=turn: (
                            sum(record.event_name == "Stop" for record in hooks()) >= expected
                        ),
                        timeout=60,
                        what=f"Stop {turn}",
                    )
                    screen.wait_for(
                        lambda _text, expected=turn: (
                            (result := parsed()) is not None
                            and sum(
                                item.item_type == "message"
                                and item.data.get("role") == "user"
                                and not item.data.get("is_meta")
                                for item in result.items
                            )
                            >= expected
                        ),
                        timeout=15,
                        what=f"durable user record {turn}",
                    )
                    submits = [
                        record for record in hooks() if record.event_name == "UserPromptSubmit"
                    ]
                    result = parsed()
                    state = forwarder.TranscriptForwardState(
                        bridge.read_transcript_path(directory), 0, byte_offset=0
                    )
                    tracker = forwarder._PostRetryTracker()
                    if turn == 1:
                        assert submits[0].retry_input_id == claim.input_id
                        [user] = [
                            item
                            for item in result.items
                            if item.item_type == "message"
                            and item.data.get("role") == "user"
                            and not item.data.get("is_meta")
                        ]
                        blocked = await forwarder._forward_transcript_item_batch(
                            client=client,
                            session_id=journal.child,
                            bridge_dir=directory,
                            state=state,
                            retry_tracker=tracker,
                            compaction_retry_tracker=tracker,
                            skip_user_messages=False,
                            dedupe=forwarder._ForwardDedupeState(),
                            result=result,
                        )
                        assert 503 in statuses
                        assert user.source_id not in blocked.seen_source_ids
                        assert journal.conversations.list_items(journal.child).data == []
                        assert (
                            pending_inputs.pending_id_for_stable_id(journal.child, claim.input_id)
                            is not None
                        )
                        accept_native(journal, claim, submits[0].terminal_turn_id)
                        invalid = await client.post(
                            f"/v1/sessions/{journal.child}/events",
                            json={
                                "type": "external_conversation_item",
                                "data": {
                                    "item_type": "message",
                                    "item_data": user.data,
                                    "source_id": "invalid-recovery-source:0:message",
                                    "response_id": user.response_id,
                                    "recovery_after": "",
                                    "input_id": claim.input_id,
                                    "input_turn_id": submits[0].terminal_turn_id,
                                },
                            },
                        )
                        assert invalid.status_code == 400, invalid.text
                        assert (
                            journal.store.get_input(claim.input_id).payload.get("native_source_id")
                            is None
                        )
                        await asyncio.sleep(tracker.retry_delay_s(f"item:{user.source_id}") or 0)
                    else:
                        assert submits[1].retry_input_id is None
                    # Start with a cold cursor each time: the server absorbs history replay.
                    await forwarder._forward_transcript_item_batch(
                        client=client,
                        session_id=journal.child,
                        bridge_dir=directory,
                        state=state,
                        retry_tracker=tracker,
                        compaction_retry_tracker=tracker,
                        skip_user_messages=False,
                        dedupe=forwarder._ForwardDedupeState(),
                        result=result,
                    )
                    response = await client.get(f"/v1/sessions/{journal.child}/items")
                    assert response.status_code == 200, response.text
                    history = response.json()["data"]
                    users = [
                        item
                        for item in history
                        if item["type"] == "message"
                        and item["role"] == "user"
                        and not item.get("is_meta")
                    ]
                    assert len(users) == turn
                    marked = [item for item in users if item.get("auto_retry")]
                    assert (
                        len(marked) == 1
                        and marked[0]["auto_retry"] == claim.retry.metadata.model_dump()
                    )
                    assert (
                        pending_inputs.pending_id_for_stable_id(journal.child, claim.input_id)
                        is None
                    )
                assert journal.store.get_chain(chain.chain_id).accepted_attempts == 1
        finally:
            report = {
                "screen": screen.text(),
                "history": history,
                "hooks": [record.__dict__ for record in hooks()],
                "transcript": (
                    path.read_text()
                    if (path := bridge.read_transcript_path(directory)) and path.exists()
                    else None
                ),
            }
            (tmp_path / "retry-echo-diagnostics.json").write_text(
                json.dumps(report, default=str, indent=2) + "\n"
            )
            child.close(force=True)
