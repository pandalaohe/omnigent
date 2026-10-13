"""Claude card answers must survive a runner accepting a failed injection."""

import asyncio
import contextlib
from typing import Any
from unittest.mock import Mock

import httpx
import pytest

from omnigent.runner.app import create_runner_app
from omnigent.server.routes._sessions import orchestration
from omnigent.server.schemas import SessionEventInput
from omnigent.spec.types import AgentSpec
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient, _sse
from tests.runner.helpers import NullServerClient

pytestmark = pytest.mark.asyncio


class _TransientHarness(_ScriptedHarnessClient):
    def __init__(self, *, failed_stream: bool) -> None:
        super().__init__(
            [
                _sse({"type": "response.created", "response": {"id": "resp_card"}}),
                _sse({"type": "response.completed", "response": {"id": "resp_card"}}),
            ]
        )
        self.attempts = 0
        self.failed_stream = failed_stream

    def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
        self.attempts += 1
        if self.attempts == 1:
            if self.failed_stream:
                return _ScriptedHarnessClient(
                    [_sse({"type": "response.failed", "error": {"message": "injection failed"}})]
                ).stream(method, url, json=json, timeout=timeout)

            class DroppedConnection:
                async def __aenter__(self) -> None:
                    raise httpx.RemoteProtocolError(
                        "Server disconnected without sending a response"
                    )

                async def __aexit__(self, *_: Any) -> None:
                    pass

            return DroppedConnection()
        return super().stream(method, url, json=json, timeout=timeout)


@pytest.mark.parametrize(
    "failed_stream", [False, True], ids=["connection-dropped", "inject-failed"]
)
async def test_card_delivery_retries_after_runner_accepts_failed_injection(
    monkeypatch: pytest.MonkeyPatch, failed_stream: bool
) -> None:
    harness = _TransientHarness(failed_stream=failed_stream)

    async def resolver(*_: Any) -> AgentSpec:
        return AgentSpec(spec_version=1, name="card-agent")

    app = create_runner_app(
        process_manager=_FakeProcessManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    monkeypatch.setattr(
        orchestration,
        "_native_terminal_runtime",
        lambda _: ("Claude", "claude-native", "claude-native"),
    )
    notices: list[str] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as runner:

        async def post_answer(
            session_id: str,
            text: str,
            *,
            conversation_store: Any,
            runner_router: Any,
            confirm_delivery: bool,
        ) -> bool:
            await orchestration._forward_native_terminal_message(
                runner,
                session_id,
                Mock(agent_id="card-agent", harness_override=None),
                SessionEventInput(
                    type="message",
                    data={
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                        "confirm_delivery": confirm_delivery,
                    },
                ),
            )
            return True

        async def no_sleep(_: float) -> None:
            await asyncio.sleep(0)

        async def notice(_: str, text: str, store: Any, *, status: str) -> None:
            notices.append(text)

        monkeypatch.setattr(orchestration, "_post_system_message", post_answer)
        monkeypatch.setattr(orchestration, "_delivery_retry_sleep", no_sleep)
        monkeypatch.setattr(orchestration, "_persist_answer_delivery_notice", notice)
        try:
            assert await orchestration._deliver_with_retry(
                "card_session",
                "[System: answers to your question card #q123456]\nContinue the work.",
                conversation_store=Mock(),
                runner_router=None,
            )
            tasks = [task for task in app.state.active_turns.values() if task is not None]
            if tasks:
                await asyncio.wait_for(asyncio.gather(*tasks), timeout=3)
            assert harness.attempts == 2, "queued acknowledgement falsely settled the card answer"
            assert notices == []
        finally:
            for task in list(app.state.active_turns.values()):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task


@pytest.mark.parametrize("stage", ["setup", "instructions", "harness"])
async def test_cancelled_confirmation_releases_turn_slot(stage: str) -> None:
    entered = asyncio.Event()

    class WaitingHarness(_ScriptedHarnessClient):
        def stream(self, method: str, url: str, *, json: dict[str, Any], timeout: Any) -> Any:
            if stage == "harness" and not entered.is_set():

                class WaitingConnection:
                    async def __aenter__(self) -> None:
                        entered.set()
                        await asyncio.Event().wait()

                    async def __aexit__(self, *_: Any) -> None:
                        pass

                return WaitingConnection()
            return super().stream(method, url, json=json, timeout=timeout)

    class WaitingManager(_FakeProcessManager):
        async def get_client(self, *args: Any, **kwargs: Any) -> Any:
            if stage == "setup" and not entered.is_set():
                entered.set()
                await asyncio.Event().wait()
            return await super().get_client(*args, **kwargs)

    class SnapshotClient(NullServerClient):
        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            return httpx.Response(200, json={"agent_id": "card-agent"})

    async def resolver(*_: Any) -> AgentSpec:
        if stage == "instructions" and not entered.is_set():
            entered.set()
            await asyncio.Event().wait()
        return AgentSpec(spec_version=1, name="card-agent")

    harness = WaitingHarness(
        [
            _sse({"type": "response.created", "response": {"id": "resp_followup"}}),
            _sse({"type": "response.completed", "response": {"id": "resp_followup"}}),
        ]
    )
    app = create_runner_app(
        process_manager=WaitingManager(harness),  # type: ignore[arg-type]
        spec_resolver=resolver,
        server_client=SnapshotClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as runner:
        url = "/v1/sessions/cancelled_card/events?stream=true"
        body = {
            "type": "message",
            "role": "user",
            "content": "Answer the card.",
            "harness": "omnigent",
        }
        task = asyncio.create_task(runner.post(url, json=body))
        try:
            await asyncio.wait_for(entered.wait(), timeout=3)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        assert "cancelled_card" not in app.state.active_turns
        response = await runner.post(url, json={**body, "content": "Follow up."})
        assert response.status_code == 200, response.text
        assert "response.completed" in response.text
