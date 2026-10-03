"""The scaffold's ``keep_warm`` inbound event: dispatch, card guard, hook default.

The runner forwards a keep-warm ping as a ``keep_warm`` event on
``POST /v1/sessions/{id}/events``; the harness answers 200 with the
normalized receipt dict. While any in-flight turn waits on a human
(elicitation reply or policy verdict) the scaffold skips ``card``
without calling the hook, so a pending prompt is never disturbed.
Harnesses without a keep-warm channel fall back to the base hook's
``skipped`` / ``unsupported``.

These tests build the scaffold app directly (no ``/tmp`` socket
manager), so they run on every platform.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from starlette.testclient import TestClient

from omnigent.inner.executor import (
    Executor,
    ExecutorConfig,
    ExecutorEvent,
    Message,
    ToolSpec,
)
from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter
from omnigent.runtime.harnesses._scaffold import TurnContext
from tests.runtime.harnesses._test_scaffold_harnesses import _EchoHarness

_URL = "/v1/sessions/conv_x/events"
_BODY = {"type": "keep_warm", "attempt_id": "att-1", "family": "claude"}

_UNSUPPORTED_RECEIPT = {
    "attempt_id": "att-1",
    "outcome": "skipped",
    "reason": "unsupported",
    "input_total": None,
    "cache_read": None,
    "cache_write": None,
    "cost_usd": None,
    "estimated": False,
}

_CARD_RECEIPT = {
    "attempt_id": "att-1",
    "outcome": "skipped",
    "reason": "card",
    "input_total": None,
    "cache_read": None,
    "cache_write": None,
    "cost_usd": None,
    "estimated": False,
}

_OK_RECEIPT = {
    "attempt_id": "att-1",
    "outcome": "ok",
    "reason": None,
    "input_total": None,
    "cache_read": 37000,
    "cache_write": None,
    "cost_usd": 0.0111,
    "estimated": True,
}


class _KeepWarmFakeExecutor(Executor):
    """Inner executor stub recording keep-warm calls and answering a fixed receipt."""

    def __init__(self, receipt: dict[str, Any]) -> None:
        self._receipt = receipt
        self.keep_warm_calls: list[dict[str, str]] = []

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        del messages, tools, system_prompt, config
        return
        yield  # pragma: no cover — async generator shape

    async def keep_warm(self, *, attempt_id: str, family: str) -> dict[str, Any]:
        self.keep_warm_calls.append({"attempt_id": attempt_id, "family": family})
        return dict(self._receipt)


def _adapter_and_app(executor: Executor) -> tuple[ExecutorAdapter, Any]:
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    app = adapter.build()
    app.state.conversation_id = "conv_x"
    return adapter, app


def test_keep_warm_event_returns_the_executors_receipt() -> None:
    """A ``keep_warm`` event dispatches to the executor and returns its dict as JSON 200."""
    executor = _KeepWarmFakeExecutor(_OK_RECEIPT)
    _adapter, app = _adapter_and_app(executor)
    with TestClient(app) as client:
        resp = client.post(_URL, json=_BODY)
    assert resp.status_code == 200
    assert resp.json() == _OK_RECEIPT
    assert executor.keep_warm_calls == [{"attempt_id": "att-1", "family": "claude"}]


def test_keep_warm_event_skips_card_while_a_human_wait_is_pending() -> None:
    """A parked elicitation/policy wait skips ``card`` without calling the hook."""
    executor = _KeepWarmFakeExecutor(_OK_RECEIPT)
    adapter, app = _adapter_and_app(executor)
    ctx = TurnContext(
        response_id="resp_parked",
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )
    ctx._pending_human_waits = 1
    adapter._in_flight["resp_parked"] = ctx
    with TestClient(app) as client:
        resp = client.post(_URL, json=_BODY)
    assert resp.status_code == 200
    assert resp.json() == _CARD_RECEIPT
    assert executor.keep_warm_calls == []


def test_keep_warm_event_default_hook_is_unsupported() -> None:
    """The base ``HarnessApp`` hook answers ``skipped`` / ``unsupported``."""
    app = _EchoHarness().build()
    app.state.conversation_id = "conv_x"
    with TestClient(app) as client:
        resp = client.post(_URL, json=_BODY)
    assert resp.status_code == 200
    assert resp.json() == _UNSUPPORTED_RECEIPT


async def test_base_executor_keep_warm_is_unsupported() -> None:
    """The base ``Executor.keep_warm`` answers ``skipped`` / ``unsupported``."""
    receipt = await Executor().keep_warm(attempt_id="att-1", family="claude")
    assert receipt == _UNSUPPORTED_RECEIPT
