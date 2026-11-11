"""Real native input identities survive forwarding and a cold history replay."""

import asyncio
import faulthandler
import json
import os
import shlex
import shutil
import sys
import time
from types import SimpleNamespace

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


def pytest_generate_tests(metafunc):
    if "codex_binary" in metafunc.fixturenames:
        binaries = metafunc.config.getoption("--codex-native-binary") or ["PATH"]
        metafunc.parametrize(
            "codex_binary",
            binaries,
            ids=["pinned" if binary == "PATH" else "selected" for binary in binaries],
        )


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


@pytest.mark.asyncio
@pytest.mark.parametrize("echo_before_acceptance", [False, True])
async def test_real_codex_retry_echo_identity_and_cold_replay(
    tmp_path,
    db_uri,
    monkeypatch,
    isolated_mock_llm_server_url,
    echo_before_acceptance,
    codex_binary,
):
    from omnigent.harnesses.codex_native import bridge as cb
    from omnigent.harnesses.codex_native import forwarder as cf
    from omnigent.harnesses.codex_native.app_server import (
        CodexNativeAppServer,
        client_for_transport,
    )
    from omnigent.inner.codex_native_executor import _native_delivery_request_once
    from tests.e2e.conftest import configure_mock_llm, release_mock_gate

    binary = shutil.which("codex") if codex_binary == "PATH" else codex_binary
    assert binary, "Pinned Codex CLI required"
    journal, chain, claim = prepared_native(db_uri, harness="codex-native")
    model = "mock-retry-echo"
    configure_mock_llm(
        isolated_mock_llm_server_url,
        [{"text": "Automatic complete.", "block": True}, {"text": "Human complete."}],
        key=model,
    )
    directory = tmp_path / "bridge"
    source_home = tmp_path / "source-home"
    source_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    backend = CodexNativeAppServer(
        codex_path=binary,
        socket_path=tmp_path / "codex.sock",
        codex_home=tmp_path / "native-home",
        env={**os.environ, "CODEX_HOME": str(source_home)},
        cwd=tmp_path,
        bridge_dir=directory,
        reconcile_process_registry=False,
        config_overrides=[
            'model_provider="retry-test"',
            'model_providers.retry-test={name="Retry test",base_url='
            + json.dumps(f"{isolated_mock_llm_server_url}/v1")
            + ',wire_api="responses",requires_openai_auth=false}',
            "analytics.enabled=false",
            "feedback.enabled=false",
        ],
    )
    native = None
    submitted = None
    protocol = []
    stack_log = (tmp_path / "codex-retry-stacks.log").open("w")
    faulthandler.dump_traceback_later(120, repeat=True, file=stack_log)

    def record_protocol(event):
        protocol.append(event)
        with (tmp_path / "codex-retry-protocol.jsonl").open("a") as handle:
            handle.write(json.dumps(event) + "\n")

    record_protocol({"phase": "cli-version-start"})
    version_process = await asyncio.create_subprocess_exec(
        binary, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    version, version_error = await version_process.communicate()
    assert version_process.returncode == 0, version_error.decode()
    record_protocol({"method": "capture/cli-version", "version": version.decode().strip()})
    try:
        record_protocol({"phase": "backend-start"})
        await backend.start()
        record_protocol({"phase": "backend-ready"})
        native = client_for_transport(backend.listen_url or str(backend.socket_path))
        await native.connect()
        record_protocol({"phase": "native-connected"})

        async def request_protocol(method, params):
            record_protocol({"method": "capture/rpc-start", "rpc": method})
            response = await native.request(method, params)
            record_protocol({"method": "capture/" + method, "response": response})
            return response

        response = await request_protocol(
            "thread/start",
            {
                "model": model,
                "cwd": str(tmp_path),
                "approvalPolicy": "never",
                "sandbox": "read-only",
            },
        )
        thread_id = response["result"]["thread"]["id"]
        state = cb.CodexNativeBridgeState(
            session_id=journal.child,
            socket_path=backend.listen_url or str(backend.socket_path),
            thread_id=thread_id,
            codex_home=str(backend.codex_home),
            cwd=str(tmp_path),
        )
        cb.write_bridge_state(directory, state)
        snapshot = observe_turn(directory, journal.child, "codex_failed", idle=True)
        guard = AutoRetryGuard(
            session_id=journal.child,
            chain_id=chain.chain_id,
            input_id=claim.input_id,
            expected_turn_id="codex_failed",
            expected_input_generation=1,
            expected_native_generation=snapshot.generation,
            admission_deadline=time.time() + 60,
            attempt=1,
            of=chain.limit,
        )
        pending_inputs.record(
            journal.child,
            [{"type": "input_text", "text": "retry"}],
            stable_id=claim.input_id,
            auto_retry=claim.retry.metadata,
        )
        pending_seen = asyncio.Event()

        async def settle(outcome, turn_id, reason, detail):
            assert outcome == "accepted"
            if echo_before_acceptance:
                await asyncio.wait_for(pending_seen.wait(), 30)
            accept_native(journal, claim, turn_id)
            return True

        async def next_event(method):
            async for event in native.iter_events():
                record_protocol(event)
                if event.get("method") == method:
                    return event["params"]
            raise AssertionError(f"Codex disconnected before {method}")

        async def next_user(exclude_turn=None):
            while True:
                params = await next_event("item/completed")
                if (
                    params.get("item", {}).get("type") == "userMessage"
                    and params.get("turnId") != exclude_turn
                ):
                    return params

        router = _make_client(journal.conversations)
        app = FastAPI()
        app.mount("/v1", router.app)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://server",
            headers=dict(router.headers),
        ) as client:

            async def note_pending(response):
                if response.request.url.path.endswith("/events"):
                    await response.aread()
                    data = json.loads(response.request.content).get("data", {})
                    record_protocol(
                        {
                            "method": "capture/AP-response",
                            "status": response.status_code,
                            "source_id": data.get("source_id"),
                            "input_id": data.get("input_id"),
                            "input_turn_id": data.get("input_turn_id"),
                            "body": response.text[:1000],
                        }
                    )
                if response.status_code == 503:
                    assert journal.conversations.list_items(journal.child).data == []
                    pending_seen.set()

            client.event_hooks["response"].append(note_pending)
            with auto_retry_scope(guard, settle_execution=settle):
                submitted = asyncio.create_task(
                    _native_delivery_request_once(
                        native,
                        "turn/start",
                        {"threadId": thread_id, "input": [{"type": "text", "text": "retry"}]},
                        state,
                        bridge_dir=directory,
                    )
                )
            record_protocol({"phase": "wait-first-user"})
            live = await asyncio.wait_for(next_user(), 45)
            record_protocol({"phase": "first-user-received"})
            record_protocol(
                {
                    "method": "capture/thread-read-in-progress",
                    "response": await request_protocol(
                        "thread/read", {"threadId": thread_id, "includeTurns": True}
                    ),
                }
            )
            assert live["item"].get("clientId") == guard.input_id, (
                "CLI did not echo the requested client input identity"
            )
            if not echo_before_acceptance:
                await asyncio.wait_for(submitted, 30)
            await asyncio.wait_for(
                cf._handle_completed_item(
                    client,
                    journal.child,
                    live,
                    bridge_dir=directory,
                    forwarder_state=cf._CodexForwarderState(),
                ),
                45,
            )
            await asyncio.wait_for(submitted, 30)
            assert pending_seen.is_set() is echo_before_acceptance

            recovering = cf._CodexForwarderState(
                codex_client=SimpleNamespace(request=request_protocol),
                child_usage_baseline_dir=directory,
            )
            record_protocol({"phase": "recover-user-start"})
            try:
                async with asyncio.timeout(15):
                    await cf._ensure_user_message_posted(client, journal.child, live, recovering)
            except TimeoutError as exc:
                record_protocol({"phase": "recover-user-timeout", "bound_seconds": 15})
                raise AssertionError(
                    "User-message recovery did not receive AP acceptance within 15s; "
                    f"last protocol events: {json.dumps(protocol[-12:])}"
                ) from exc
            record_protocol({"phase": "recover-user-returned"})
            assert len(journal.conversations.list_items(journal.child).data) == 1
            release_mock_gate(isolated_mock_llm_server_url)
            await asyncio.wait_for(next_event("turn/completed"), 45)
            resumed = await request_protocol(
                "thread/read", {"threadId": thread_id, "includeTurns": True}
            )
            record_protocol({"method": "capture/thread-read-completed", "response": resumed})
            user = cf._find_turn_user_message(resumed, live["turnId"])
            assert user is not None and isinstance(user["id"], str) and user["id"]
            assert user.get("clientId") == guard.input_id
            assert journal.store.get_input(claim.input_id).payload[
                "native_source_id"
            ] == cf._source_id(live, live["item"])
            for _ in range(2):
                await cf._handle_completed_item(
                    client,
                    journal.child,
                    {**live, "item": user},
                    bridge_dir=directory,
                    forwarder_state=cf._CodexForwarderState(),
                )
            await request_protocol(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": "retry"}],
                },
            )
            human = await asyncio.wait_for(next_user(exclude_turn=live["turnId"]), 45)
            assert human["item"]["id"] != live["item"]["id"]
            assert human["item"].get("clientId") != guard.input_id
            await cf._handle_completed_item(client, journal.child, human, bridge_dir=directory)
            response = await client.get(f"/v1/sessions/{journal.child}/items")
            automatic, ordinary = response.json()["data"]
            assert automatic["auto_retry"] == claim.retry.metadata.model_dump()
            assert ordinary.get("auto_retry") is None
            assert pending_inputs.pending_id_for_stable_id(journal.child, claim.input_id) is None
            assert journal.store.get_chain(chain.chain_id).accepted_attempts == 1
    finally:
        record_protocol({"phase": "cleanup-start"})
        release_mock_gate(isolated_mock_llm_server_url)
        if submitted is not None and not submitted.done():
            submitted.cancel()
            await asyncio.gather(submitted, return_exceptions=True)
        if native is not None:
            await native.close()
        await backend.close()
        record_protocol({"phase": "cleanup-finished"})
        (tmp_path / "codex-retry-protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
        faulthandler.cancel_dump_traceback_later()
        stack_log.close()


@pytest.mark.asyncio
async def test_real_pi_retry_identity_reaches_server_and_history(
    tmp_path, isolated_mock_llm_server_url
):
    from omnigent.harnesses.pi_native import bridge as pb
    from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
    from omnigent.runner.transports.ws_tunnel.serve import serve_tunnel
    from omnigent.server.permission_tiers import PERMISSION_PROTOCOL_HEADER
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from tests._helpers.live_server import isolated_local_server, isolated_process_env
    from tests.e2e.conftest import configure_mock_llm
    from tests.harnesses.pi_native.helpers import ready_permission_state
    from tests.stores.test_subagent_retry_execution import _prepared
    from tests.stores.test_subagent_retry_store import _journal

    binary = shutil.which("pi")
    assert binary, "Pinned Pi CLI required"
    model = "claude-sonnet-4-5"
    configure_mock_llm(
        isolated_mock_llm_server_url,
        [{"text": "Automatic complete."}, {"text": "Human complete."}],
        key=model,
    )
    server_root = tmp_path / "server"
    agent_dir = tmp_path / "pi-agent"
    agent_dir.mkdir()
    (agent_dir / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "retry-test": {
                        "baseUrl": isolated_mock_llm_server_url,
                        "api": "anthropic-messages",
                        "apiKey": "synthetic",
                        "models": [{"id": model}],
                    }
                }
            }
        )
    )
    protocol = []
    with isolated_local_server(server_root, poll_interval=0.1) as base_url:
        uri = f"sqlite:///{server_root / 'chat.db'}"
        journal = _journal(uri, SqlAlchemyConversationStore(uri))
        journal.conversations.update_conversation(journal.child, harness_override="pi-native")
        chain, claim = _prepared(journal)
        token = "pi-native-retry-echo-fixture"
        runner_id = token_bound_runner_id(token)
        assert journal.conversations.set_runner_id(journal.child, runner_id)
        claim = claim.model_copy(update={"boundary": "native_reserve", "native_generation": 1})
        assert (
            journal.store.admit_execution(journal.child, claim, runner_id=runner_id).state
            == "reserved"
        )
        runner_app = FastAPI()

        @runner_app.post("/v1/sessions/{session_id}/events")
        async def _runner_events(session_id: str):
            return {"queued": False}

        tunnel_task = asyncio.create_task(
            serve_tunnel(
                runner_app,
                server_url=base_url,
                runner_id=runner_id,
                runner_version="0.1.0",
                tunnel_token=token,
            )
        )
        directory = tmp_path / "pi-bridge"
        directory.mkdir()
        (directory / "inbox").mkdir()
        ready_permission_state(directory, journal.child)
        extension, config_path = pb.write_extension_files(
            directory,
            session_id=journal.child,
            server_url=base_url,
            conversation_url="",
            auth_headers={RUNNER_TUNNEL_TOKEN_HEADER: token},
        )
        snapshot = observe_turn(directory, journal.child, "failed-turn", idle=True)
        guard = AutoRetryGuard(
            session_id=journal.child,
            chain_id=chain.chain_id,
            input_id=claim.input_id,
            expected_turn_id="failed-turn",
            expected_input_generation=1,
            expected_native_generation=snapshot.generation,
            admission_deadline=time.time() + 60,
            attempt=1,
            of=chain.limit,
        )
        with auto_retry_scope(guard):
            prepare_submission(directory, native_idle=True)
        native_home = tmp_path / "pi-home"
        native_home.mkdir()
        env = isolated_process_env(
            native_home,
            {
                "PI_CODING_AGENT_DIR": str(agent_dir),
                "PI_OFFLINE": "1",
                "OMNIGENT_PI_NATIVE_CONFIG": str(config_path),
            },
            None,
        )
        stderr = (tmp_path / "pi-retry-stderr.log").open("w")
        process = await asyncio.create_subprocess_exec(
            binary,
            "--mode",
            "rpc",
            "--model",
            f"retry-test/{model}",
            "--no-extensions",
            "--extension",
            str(extension),
            "--no-skills",
            "--no-prompt-templates",
            "--session",
            str(tmp_path / "pi-session.jsonl"),
            cwd=tmp_path,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=stderr,
        )

        async def turn():
            process.stdin.write(b'{"type":"prompt","message":"retry"}\n')
            await process.stdin.drain()
            while line := await process.stdout.readline():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                protocol.append(event)
                assert event.get("success") is not False, event
                if event.get("type") == "agent_end":
                    return
            raise AssertionError(f"Pi exited before agent_end: {process.returncode}")

        try:
            async with httpx.AsyncClient(
                base_url=base_url,
                trust_env=False,
                headers={
                    PERMISSION_PROTOCOL_HEADER: "1",
                    RUNNER_TUNNEL_TOKEN_HEADER: token,
                },
            ) as client:
                await asyncio.wait_for(turn(), 60)
                [automatic] = [
                    item
                    for item in journal.conversations.list_items(journal.child).data
                    if getattr(item.data, "role", None) == "user"
                ]
                assert automatic.data.auto_retry == claim.retry.metadata
                receipt = journal.store.get_input(claim.input_id)
                assert receipt.state == "accepted" and receipt.payload["native_source_id"]
                await asyncio.wait_for(turn(), 60)
                users = [
                    item
                    for item in journal.conversations.list_items(journal.child).data
                    if getattr(item.data, "role", None) == "user"
                ]
                assert len(users) == 2 and users[1].data.auto_retry is None
                for _ in range(2):
                    replay = await client.post(
                        f"/v1/sessions/{journal.child}/events",
                        json={
                            "type": "external_conversation_item",
                            "data": {
                                "item_type": "message",
                                "item_data": automatic.data.model_dump(mode="json"),
                                "source_id": receipt.payload["native_source_id"],
                                "response_id": automatic.response_id,
                                "input_id": claim.input_id,
                                "input_turn_id": receipt.turn_id,
                            },
                        },
                    )
                    assert replay.status_code == 202, replay.text
                response = await client.get(f"/v1/sessions/{journal.child}/items")
                reloaded = [item for item in response.json()["data"] if item.get("role") == "user"]
                assert len(reloaded) == 2
                assert reloaded[0]["auto_retry"] == claim.retry.metadata.model_dump()
                assert reloaded[1].get("auto_retry") is None
                assert journal.store.get_chain(chain.chain_id).accepted_attempts == 1
        finally:
            tunnel_task.cancel()
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 10)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            stderr.close()
            (tmp_path / "pi-retry-protocol.json").write_text(json.dumps(protocol, indent=2) + "\n")
