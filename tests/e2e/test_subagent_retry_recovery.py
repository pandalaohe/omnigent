"""Real server/runner outage and post-commit wake loss, with a local scaffold executor."""

from __future__ import annotations

import asyncio
import json
import os
import time
import traceback
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from uuid import uuid4

import httpx
import uvicorn
from fastapi import FastAPI, Request

from omnigent.runtime.harnesses._scaffold import InboundEventRequest
from tests._helpers.live_server import find_free_port, terminate_process
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle


def server_boot() -> None:
    """Lose an actual HTTP acknowledgement after the final wake has been admitted."""
    import omnigent.server.app as server

    original = server.create_app
    root = Path(os.environ["RETRY_TEST_ROOT"])

    def create(*args, **kwargs):
        app = original(*args, **kwargs)

        class LoseWakeAcknowledgement:
            def __init__(self, app):
                self.app = app

            async def __call__(self, scope, receive, send):
                path = scope.get("path", "")
                if (
                    scope["type"] != "http"
                    or not path.endswith("/wake")
                    or (root / "wake-loss.json").exists()
                ):
                    await self.app(scope, receive, send)
                    return
                frames = []

                async def capture(frame):
                    frames.append(frame)

                await self.app(scope, receive, capture)
                chain_id = path.split("/")[-2]
                service = app.state.subagent_retry_admission
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    final = await asyncio.to_thread(service.journal.get_final, chain_id)
                    if final is not None and final.wake_receipt:
                        (root / "wake-loss.json").write_text(
                            json.dumps(
                                {
                                    "chain_id": chain_id,
                                    "notice_id": final.notice_id,
                                    "wake_receipt": final.wake_receipt,
                                }
                            )
                        )
                        raise ConnectionResetError(
                            "lost final wake acknowledgement after admission"
                        )
                    await asyncio.sleep(0.02)
                for frame in frames:
                    await send(frame)

        app.add_middleware(LoseWakeAcknowledgement)
        return app

    server.create_app = create
    from omnigent.cli import main

    main()


def runner_boot() -> None:
    """Inject the inactive runtime; keep the real runner, tunnel and harness scaffold."""
    import omnigent.runner.app as runner
    from omnigent.entities.subagent_terminal import TerminalEnvelope
    from omnigent.runner import subagent_work
    from omnigent.runner.subagent_retry import SubagentRetryRuntime
    from omnigent.runner.subagent_retry_spool import RetryFinalSpool
    from omnigent.runner.tool_dispatch import _drain_inbox
    from omnigent.runtime.harnesses._scaffold import HarnessApp
    from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
    from omnigent.server.schemas import OutputTextDeltaEvent

    root = Path(os.environ["RETRY_TEST_ROOT"])
    clients = {}
    control = FastAPI()

    class Echo(HarnessApp):
        async def _post_session_event(
            self, conversation_id: str, body: InboundEventRequest, request: Request
        ):
            with (root / "fixture-harness.log").open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "conversation_id": conversation_id,
                            "bound": getattr(request.app.state, "conversation_id", None),
                            "type": body.type,
                            "keys": sorted(body.model_dump()),
                            "input_id": getattr(body, "input_id", None),
                            "input_admission_required": getattr(
                                body, "input_admission_required", None
                            ),
                        }
                    )
                    + "\n"
                )
                try:
                    return await super()._post_session_event(conversation_id, body, request)
                except Exception:
                    traceback.print_exc(file=handle)
                    raise

        async def run_turn(self, request, ctx):
            # The production scaffold reaches this only after execution admission.
            with (root / "executions.jsonl").open("a") as handle:
                handle.write(
                    json.dumps(
                        {
                            "session_id": ctx.session_id,
                            "input_id": request.input_id,
                            "turn_id": ctx.response_id,
                        }
                    )
                    + "\n"
                )
            ctx.emit(
                OutputTextDeltaEvent(
                    type="response.output_text.delta", delta="parent received final wake"
                )
            )

    async def get_client(self, conversation_id, harness, env=None):
        if conversation_id not in clients:
            harness_app = Echo().build()
            harness_app.state.conversation_id = conversation_id

            @harness_app.post("/v1/permission-tier/bootstrap")
            async def bootstrap():
                return {"installed": True}

            mount = f"/harness/{conversation_id}"
            control.mount(mount, harness_app)
            # Admission needs SSE frames before the turn completes.
            clients[conversation_id] = httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{os.environ['RETRY_TEST_CONTROL_PORT']}{mount}",
                trust_env=False,
            )
        return clients[conversation_id]

    HarnessProcessManager.get_client = get_client
    original = runner.create_runner_app

    class ControlServer(uvicorn.Server):
        @contextmanager
        def capture_signals(self):
            yield

    def create(*args, **kwargs):
        app = original(*args, **kwargs)
        runtime = SubagentRetryRuntime(
            kwargs["server_client"],
            clock=lambda: float((root / "clock").read_text()),
            spool=RetryFinalSpool(root / "finals.sqlite3"),
        )
        app.state.subagent_retry_runtime = runtime
        runtime.on_delivered = app.state.retry_final_delivered

        @control.get("/health")
        async def health():
            return {"ready": True}

        @control.post("/seed")
        async def seed(request: Request):
            body = await request.json()
            terminal = TerminalEnvelope.model_validate(body["terminal"])
            subagent_work._session_inboxes_ref.setdefault(terminal.parent_id, asyncio.Queue())
            subagent_work.register_subagent_work(
                parent_session_id=terminal.parent_id,
                child_session_id=terminal.child_id,
                agent="worker",
                title="retry worker",
                registered_by="sys_session_send",
            )
            ack = await runtime.accept_terminal(terminal, "original provider error")
            return {"reason": ack.reason, "chain_id": ack.chain_id}

        @control.post("/deadline")
        async def deadline(request: Request):
            body = await request.json()
            await runtime._run(body["chain_id"])
            return {"stopped": runtime.chains[body["chain_id"]].stopped_reason}

        @control.post("/recover")
        async def recover(request: Request):
            parent = (await request.json())["parent_id"]
            inbox = subagent_work._session_inboxes_ref.setdefault(parent, asyncio.Queue())
            ready = await runtime.recover_parent(parent)
            return {
                "ready": ready,
                "pending": inbox.qsize(),
                "timers": sum(state.timer is not None for state in runtime.chains.values()),
            }

        @control.post("/drain")
        async def drain(request: Request):
            parent = (await request.json())["parent_id"]
            return {
                "text": await _drain_inbox(
                    subagent_work._session_inboxes_ref.get(parent),
                    server_client=kwargs["server_client"],
                    conversation_id=parent,
                )
            }

        control_server = ControlServer(
            uvicorn.Config(
                control,
                host="127.0.0.1",
                port=int(os.environ["RETRY_TEST_CONTROL_PORT"]),
                lifespan="off",
                log_level="warning",
            )
        )

        async def start():
            app.state.retry_test_control = asyncio.create_task(control_server.serve())

        async def stop():
            await runtime.close()
            control_server.should_exit = True
            await app.state.retry_test_control
            for client in clients.values():
                await client.aclose()

        original_lifespan = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(application):
            async with original_lifespan(application):
                await start()
                try:
                    yield
                finally:
                    await stop()

        app.router.lifespan_context = lifespan
        return app

    runner.create_runner_app = create
    from omnigent.runner._entry import main

    main()


def test_real_outage_restart_and_lost_wake_ack_keep_one_final(tmp_path):
    from omnigent.entities.subagent_terminal import TerminalEnvelope
    from omnigent.runner.subagent_retry_spool import RetryFinalSpool
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.subagent_retry_store.sqlalchemy_store import SqlAlchemySubagentRetryStore

    checkout = Path(__file__).resolve().parents[2]
    (tmp_path / "clock").write_text("100")
    control_port = find_free_port()
    env = {
        "RETRY_TEST_ROOT": str(tmp_path),
        "RETRY_TEST_CONTROL_PORT": str(control_port),
        "PYTHONPATH": str(checkout),
        "OMNIGENT_SUBAGENT_AUTO_RETRY_ATTEMPTS": "3",
    }
    with server_runner(
        tmp_path / "stack",
        server_cwd=checkout,
        server_bootstrap=(
            "from tests.e2e.test_subagent_retry_recovery import server_boot; server_boot()"
        ),
        server_env={"RETRY_TEST_ROOT": str(tmp_path), "PYTHONPATH": str(checkout)},
    ) as stack:

        def start_runner():
            stack.start_runner(
                bootstrap=(
                    "from tests.e2e.test_subagent_retry_recovery import runner_boot; runner_boot()"
                ),
                env=env,
                cwd=checkout,
                wait_ready=False,
            )

        start_runner()
        with (
            httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client,
            httpx.Client(
                base_url=f"http://127.0.0.1:{control_port}", trust_env=False, timeout=30
            ) as control,
        ):

            def ready():
                assert stack.runner is None or stack.runner.poll() is None, stack.log_tail()
                try:
                    return control.get("/health").status_code == 200
                except httpx.HTTPError:
                    return False

            def wait_for_control():
                try:
                    poll_until(ready, timeout_s=60, interval_s=0.1)
                except AssertionError as exc:
                    raise AssertionError(f"{exc}\n{stack.log_tail()}") from exc

            wait_for_control()
            stack._wait_ready(runner=True)
            bundle = bundle_files(
                {
                    "test.yaml": (
                        b"name: retry-test\nprompt: Test\nexecutor:\n"
                        b"  harness: claude-sdk\n  model: fake-model\n"
                    )
                }
            )
            created = post_session_bundle(client.post, "/v1/sessions", bundle)
            assert created.status_code == 201, created.text
            parent = created.json()["session_id"]
            conversations = SqlAlchemyConversationStore(stack.database_uri)
            assert conversations.set_runner_id(parent, stack.runner_id)
            agent_id = conversations.get_conversation(parent).agent_id
            child = conversations.create_conversation(
                parent_conversation_id=parent, agent_id=agent_id, runner_id=stack.runner_id
            )
            journal = SqlAlchemySubagentRetryStore(stack.database_uri)
            source = uuid4().hex
            admitted = journal.record_input(
                child.id,
                input_id=source,
                turn_id="fixture-admitted-turn",
                expected_generation=0,
                payload={"text": "fixture original input"},
            )
            terminal = TerminalEnvelope(
                child_id=child.id,
                parent_id=parent,
                turn_id=admitted.turn_id,
                input_id=source,
                input_generation=admitted.generation,
                status="failed",
                error={"code": "provider_failure", "message": "original provider error"},
            )
            seeded = control.post("/seed", json={"terminal": terminal.model_dump(mode="json")})
            assert seeded.status_code == 200, seeded.text + stack.log_tail()
            assert seeded.json()["reason"] == "held"
            chain_id = seeded.json()["chain_id"]
            terminate_process(stack.server)
            (tmp_path / "clock").write_text("160")
            stopped = control.post("/deadline", json={"chain_id": chain_id})
            assert stopped.status_code == 200 and stopped.json()["stopped"] == "server_unreachable"
            [candidate] = RetryFinalSpool(tmp_path / "finals.sqlite3").pending(parent)
            terminate_process(stack.runner)
            stack.runner = None
            start_runner()
            stack.start_server()
            stack._wait_ready(runner=True)
            wait_for_control()
            recovered = control.post("/recover", json={"parent_id": parent})
            assert recovered.status_code == 200 and recovered.json()["ready"], (
                recovered.text + stack.log_tail()
            )

            def delivered():
                final = journal.get_final(chain_id)
                return final if final and final.wake_receipt else None

            poll_until(delivered, timeout_s=30, interval_s=0.1)
            final = journal.get_final(chain_id)
            assert final is not None
            assert final.notice_id == candidate.notice_id and final.wake_receipt == final.notice_id
            assert (
                final.outcome.reason == "server_unreachable"
                and final.outcome.error == terminal.error
            )
            assert final.attempts == 0 and journal.get_chain(chain_id).retry_closed

            def wake_lost():
                try:
                    return (
                        json.loads((tmp_path / "wake-loss.json").read_text()).get("notice_id")
                        == final.notice_id
                    )
                except (FileNotFoundError, json.JSONDecodeError):
                    return False

            poll_until(wake_lost, timeout_s=30, interval_s=0.1)

            def parent_executed():
                path = tmp_path / "executions.jsonl"
                try:
                    return any(
                        json.loads(line).get("input_id") == final.notice_id
                        for line in path.read_text().splitlines()
                    )
                except (FileNotFoundError, json.JSONDecodeError):
                    return False

            poll_until(parent_executed, timeout_s=30, interval_s=0.1)
            assert (
                json.loads((tmp_path / "wake-loss.json").read_text())["wake_receipt"]
                == final.notice_id
            )
            executions = [
                json.loads(line)
                for line in (tmp_path / "executions.jsonl").read_text().splitlines()
            ]
            parent_executions = [event for event in executions if event["session_id"] == parent]
            assert (
                len(parent_executions) == 1 and parent_executions[0]["input_id"] == final.notice_id
            )
            assert control.post("/recover", json={"parent_id": parent}).json() == {
                "ready": True,
                "pending": 1,
                "timers": 0,
            }
            assert (
                "original provider error"
                in control.post("/drain", json={"parent_id": parent}).json()["text"]
            )
            assert journal.get_final(chain_id).drain_receipt == final.result_id
            terminate_process(stack.runner)
            stack.runner = None
            start_runner()
            stack._wait_ready(runner=True)
            wait_for_control()
            assert control.post("/recover", json={"parent_id": parent}).json() == {
                "ready": True,
                "pending": 0,
                "timers": 0,
            }
            assert (
                sum(
                    json.loads(line)["session_id"] == parent
                    for line in (tmp_path / "executions.jsonl").read_text().splitlines()
                )
                == 1
            )
            assert RetryFinalSpool(tmp_path / "finals.sqlite3").pending(parent) == []
