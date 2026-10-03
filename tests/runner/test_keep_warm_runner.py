"""Runner keep-warm controls: ``keep_warm_touch`` / ``keep_warm_ping`` (SCC28).

The server's sweeper sends both through the same events route the web's
``btw_dismiss`` uses. The runner re-arms both idle reapers on every
control, gates synchronous prompts itself (authoritative at ping time),
forwards the ping to the live harness — never spawning one — and posts
the receipt back as ``external_keep_warm_receipt``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from omnigent.inner.terminal import TerminalInstance
from omnigent.native import native_cost_popup
from omnigent.runner import pending_approvals
from omnigent.runner.app import create_runner_app
from omnigent.runtime.harnesses.process_manager import (
    HarnessProcessManager,
    NoLiveHarnessError,
)
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.helpers import NullServerClient

_CONV = "conv_x"

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

_NO_LIVE_CLIENT_RECEIPT = {
    "attempt_id": "att-1",
    "outcome": "skipped",
    "reason": "no_live_client",
    "input_total": None,
    "cache_read": None,
    "cache_write": None,
    "cost_usd": None,
    "estimated": False,
}

_USER_ACTIVE_RECEIPT = {
    "attempt_id": "att-1",
    "outcome": "skipped",
    "reason": "user_active",
    "input_total": None,
    "cache_read": None,
    "cache_write": None,
    "cost_usd": None,
    "estimated": False,
}

_UNKNOWN_RECEIPT = {
    "attempt_id": "att-1",
    "outcome": "skipped",
    "reason": "unknown",
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


@pytest.fixture(autouse=True)
def _no_tmux_client_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """No regular tmux client took input — the runner gate's CLI half never fires."""
    monkeypatch.setattr(native_cost_popup, "_tmux_last_client_input", lambda *_: (True, None))


@asynccontextmanager
async def _runner_test_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Create a test client against a runner ASGI app.

    :param app: Runner app under test.
    :returns: Async context manager yielding an ``httpx.AsyncClient``.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


class _RecordingServerClient(NullServerClient):
    """Null server client that records POSTed urls and bodies."""

    def __init__(self) -> None:
        self.urls: list[str] = []
        self.posts: list[dict[str, Any]] = []

    async def post(self, url: str, **kwargs: Any) -> NullServerClient._Response:
        """Record the POST and return the benign stub 200.

        :param url: Request URL.
        :param kwargs: Carries the ``json`` body.
        :returns: Stub 200 response with empty JSON body.
        """
        self.urls.append(url)
        self.posts.append(kwargs.get("json"))
        return self._Response()


class _FakePaneReaper:
    """Native pane reaper stub recording ``note_activity`` calls."""

    def __init__(self) -> None:
        self.noted: list[str] = []

    def note_activity(self, conversation_id: str) -> None:
        self.noted.append(conversation_id)


class _KeepWarmProcessManager:
    """
    Process manager stub for keep-warm route tests.

    :param harness_client: Harness client returned by ``get_client``;
        ``None`` makes an unexpected call fail the test.
    :param live: When false, ``get_client`` raises
        :class:`NoLiveHarnessError` — the never-spawn ``"any"`` answer.
    """

    def __init__(self, harness_client: Any = None, *, live: bool = True) -> None:
        self._harness_client = harness_client
        self._live = live
        self.noted: list[str] = []
        self.get_client_calls: list[tuple[str, str]] = []

    def note_activity(self, conversation_id: str) -> None:
        self.noted.append(conversation_id)

    async def get_client(
        self,
        conversation_id: str,
        harness_name: str,
        *,
        env: dict[str, str] | None = None,
    ) -> Any:
        del env
        self.get_client_calls.append((conversation_id, harness_name))
        if not self._live:
            raise NoLiveHarnessError(
                f"no live harness subprocess for conversation {conversation_id!r}"
            )
        if self._harness_client is None:
            raise AssertionError("get_client should not be called")
        return self._harness_client


class _ReceiptHarnessClient:
    """Harness client stub answering a scripted status + receipt and recording posts."""

    def __init__(self, receipt: dict[str, Any], *, status_code: int = 200) -> None:
        self._receipt = receipt
        self._status_code = status_code
        self.posts: list[dict[str, Any]] = []

    async def post(
        self, url: str, *, json: dict[str, Any], timeout: float | None = None
    ) -> httpx.Response:
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        return httpx.Response(self._status_code, json=self._receipt)


class _TimeoutHarnessClient:
    """Harness client stub whose POST always times out."""

    async def post(
        self, url: str, *, json: dict[str, Any], timeout: float | None = None
    ) -> httpx.Response:
        del url, json, timeout
        raise httpx.TimeoutException("timed out")


def _pane_instance(tmp_path: Path, *, interaction_ago_s: float | None = None) -> TerminalInstance:
    """
    The conversation's registered native pane, with a scripted web-interaction stamp.

    :param tmp_path: Per-test temp directory.
    :param interaction_ago_s: Seconds since the web attach bridge last
        stamped an interaction; ``None`` = never interacted.
    :returns: The :class:`TerminalInstance`.
    """
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=True,
    )
    if interaction_ago_s is not None:
        instance._last_client_interaction_at = time.monotonic() - interaction_ago_s
    return instance


def _build_app(
    mgr: _KeepWarmProcessManager,
    server_client: _RecordingServerClient,
    *,
    instance: TerminalInstance | None = None,
) -> FastAPI:
    registry = TerminalRegistry()
    if instance is not None:
        registry._by_conversation[_CONV] = {("claude", "main"): instance}
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, mgr),
        terminal_registry=registry,
        server_client=server_client,  # type: ignore[arg-type]
    )
    app.state.native_pane_reaper = _FakePaneReaper()
    return app


async def _await_keep_warm_task(conv: str, *, timeout: float = 5.0) -> None:
    """Await the fire-and-forget keep-warm task for *conv* (named ``keep-warm-{conv}``)."""
    task = next(
        (t for t in asyncio.all_tasks() if t.get_name() == f"keep-warm-{conv}"),
        None,
    )
    if task is not None:
        await asyncio.wait_for(task, timeout=timeout)


@pytest.mark.asyncio
async def test_keep_warm_touch_rearms_both_reapers_and_returns_204() -> None:
    """``keep_warm_touch`` only re-arms the pane and harness idle clocks — nothing else."""
    mgr = _KeepWarmProcessManager()
    server = _RecordingServerClient()
    app = _build_app(mgr, server)
    async with _runner_test_client(app) as http:
        resp = await http.post(f"/v1/sessions/{_CONV}/events", json={"type": "keep_warm_touch"})

    assert resp.status_code == 204
    assert app.state.native_pane_reaper.noted == [_CONV]
    assert mgr.noted == [_CONV]
    assert mgr.get_client_calls == []
    assert server.posts == []


@pytest.mark.asyncio
async def test_keep_warm_ping_with_pending_approval_skips_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A parked approval → receipt ``skipped``/``card``; the harness is never consulted."""
    monkeypatch.setitem(pending_approvals._session_pending, _CONV, 1)
    mgr = _KeepWarmProcessManager()
    server = _RecordingServerClient()
    app = _build_app(mgr, server)
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={
                "type": "keep_warm_ping",
                "attempt_id": "att-1",
                "family": "claude",
                "harness": "claude-native",
            },
        )
    await _await_keep_warm_task(_CONV)

    assert resp.status_code == 202
    assert mgr.get_client_calls == []
    assert server.urls == [f"/v1/sessions/{_CONV}/events"]
    assert server.posts == [{"type": "external_keep_warm_receipt", "data": _CARD_RECEIPT}]


@pytest.mark.asyncio
async def test_keep_warm_ping_with_pending_claude_prompt_waiter_skips_card() -> None:
    """A pending Claude prompt waiter is the same synchronous-card gate."""
    mgr = _KeepWarmProcessManager()
    server = _RecordingServerClient()
    app = _build_app(mgr, server)
    app.state.claude_prompt_waiters[_CONV] = object()
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "keep_warm_ping", "attempt_id": "att-1", "family": "claude"},
        )
    await _await_keep_warm_task(_CONV)

    assert resp.status_code == 202
    assert mgr.get_client_calls == []
    assert server.posts == [{"type": "external_keep_warm_receipt", "data": _CARD_RECEIPT}]


@pytest.mark.asyncio
async def test_keep_warm_ping_without_live_harness_skips_no_live_client(tmp_path: Path) -> None:
    """No live harness → ``skipped``/``no_live_client``; the ping never spawns one."""
    mgr = _KeepWarmProcessManager(live=False)
    server = _RecordingServerClient()
    app = _build_app(mgr, server, instance=_pane_instance(tmp_path))
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "keep_warm_ping", "attempt_id": "att-1", "family": "claude"},
        )
    await _await_keep_warm_task(_CONV)

    assert resp.status_code == 202
    # The never-spawn "any" form is the only client lookup the ping makes.
    assert mgr.get_client_calls == [(_CONV, "any")]
    assert server.posts == [
        {"type": "external_keep_warm_receipt", "data": _NO_LIVE_CLIENT_RECEIPT}
    ]


@pytest.mark.asyncio
async def test_keep_warm_ping_forwards_to_harness_and_posts_its_receipt(tmp_path: Path) -> None:
    """Happy path: the harness gets the ``keep_warm`` event; its receipt is relayed verbatim."""
    harness = _ReceiptHarnessClient(_OK_RECEIPT)
    mgr = _KeepWarmProcessManager(harness_client=harness)
    server = _RecordingServerClient()
    app = _build_app(mgr, server, instance=_pane_instance(tmp_path))
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "keep_warm_ping", "attempt_id": "att-1", "family": "claude"},
        )
    await _await_keep_warm_task(_CONV)

    assert resp.status_code == 202
    assert harness.posts == [
        {
            "url": f"/v1/sessions/{_CONV}/events",
            "json": {"type": "keep_warm", "attempt_id": "att-1", "family": "claude"},
            "timeout": 60.0,
        }
    ]
    assert server.posts == [{"type": "external_keep_warm_receipt", "data": _OK_RECEIPT}]
    # The ping re-arms both idle clocks even though the work is delegated.
    assert mgr.noted == [_CONV]
    assert app.state.native_pane_reaper.noted == [_CONV]


@pytest.mark.asyncio
async def test_keep_warm_ping_recent_web_interaction_skips_user_active(tmp_path: Path) -> None:
    """A web interaction 30 s ago → ``skipped``/``user_active``; the harness is never called."""
    harness = _ReceiptHarnessClient(_OK_RECEIPT)
    mgr = _KeepWarmProcessManager(harness_client=harness)
    server = _RecordingServerClient()
    app = _build_app(mgr, server, instance=_pane_instance(tmp_path, interaction_ago_s=30.0))
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "keep_warm_ping", "attempt_id": "att-1", "family": "claude"},
        )
    await _await_keep_warm_task(_CONV)

    assert resp.status_code == 202
    assert mgr.get_client_calls == []
    assert harness.posts == []
    assert server.posts == [{"type": "external_keep_warm_receipt", "data": _USER_ACTIVE_RECEIPT}]


@pytest.mark.asyncio
async def test_keep_warm_ping_stale_web_interaction_forwards(tmp_path: Path) -> None:
    """A web-terminal interaction 90 s ago is outside the 60 s window → forwarded."""
    harness = _ReceiptHarnessClient(_OK_RECEIPT)
    mgr = _KeepWarmProcessManager(harness_client=harness)
    server = _RecordingServerClient()
    app = _build_app(mgr, server, instance=_pane_instance(tmp_path, interaction_ago_s=90.0))
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "keep_warm_ping", "attempt_id": "att-1", "family": "claude"},
        )
    await _await_keep_warm_task(_CONV)

    assert resp.status_code == 202
    assert harness.posts[0]["json"] == {
        "type": "keep_warm",
        "attempt_id": "att-1",
        "family": "claude",
    }
    assert server.posts == [{"type": "external_keep_warm_receipt", "data": _OK_RECEIPT}]


@pytest.mark.asyncio
async def test_keep_warm_ping_without_pane_tracker_skips_unknown() -> None:
    """No readable pane-activity tracker → ``skipped``/``unknown``, never "no activity"."""
    harness = _ReceiptHarnessClient(_OK_RECEIPT)
    mgr = _KeepWarmProcessManager(harness_client=harness)
    server = _RecordingServerClient()
    app = _build_app(mgr, server)
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "keep_warm_ping", "attempt_id": "att-1", "family": "claude"},
        )
    await _await_keep_warm_task(_CONV)

    assert resp.status_code == 202
    assert mgr.get_client_calls == []
    assert harness.posts == []
    assert server.posts == [{"type": "external_keep_warm_receipt", "data": _UNKNOWN_RECEIPT}]


@pytest.mark.asyncio
async def test_keep_warm_ping_unreadable_tmux_activity_skips_unknown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed tmux client-activity query is unreadable evidence → ``skipped``/``unknown``."""
    monkeypatch.setattr(native_cost_popup, "_tmux_last_client_input", lambda *_: (False, None))
    harness = _ReceiptHarnessClient(_OK_RECEIPT)
    mgr = _KeepWarmProcessManager(harness_client=harness)
    server = _RecordingServerClient()
    app = _build_app(mgr, server, instance=_pane_instance(tmp_path))
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "keep_warm_ping", "attempt_id": "att-1", "family": "claude"},
        )
    await _await_keep_warm_task(_CONV)

    assert resp.status_code == 202
    assert mgr.get_client_calls == []
    assert harness.posts == []
    assert server.posts == [{"type": "external_keep_warm_receipt", "data": _UNKNOWN_RECEIPT}]


@pytest.mark.asyncio
async def test_keep_warm_ping_harness_timeout_reports_failed_timeout(tmp_path: Path) -> None:
    """A harness that doesn't answer in time → ``failed``/``timeout``."""
    mgr = _KeepWarmProcessManager(harness_client=_TimeoutHarnessClient())
    server = _RecordingServerClient()
    app = _build_app(mgr, server, instance=_pane_instance(tmp_path))
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "keep_warm_ping", "attempt_id": "att-1", "family": "claude"},
        )
    await _await_keep_warm_task(_CONV)

    assert resp.status_code == 202
    assert server.posts == [
        {
            "type": "external_keep_warm_receipt",
            "data": {
                "attempt_id": "att-1",
                "outcome": "failed",
                "reason": "timeout",
                "input_total": None,
                "cache_read": None,
                "cache_write": None,
                "cost_usd": None,
                "estimated": False,
            },
        }
    ]


@pytest.mark.asyncio
async def test_keep_warm_ping_harness_error_status_reports_failed_harness_error(
    tmp_path: Path,
) -> None:
    """A non-200 harness answer → ``failed``/``harness_error``."""
    mgr = _KeepWarmProcessManager(harness_client=_ReceiptHarnessClient({}, status_code=500))
    server = _RecordingServerClient()
    app = _build_app(mgr, server, instance=_pane_instance(tmp_path))
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "keep_warm_ping", "attempt_id": "att-1", "family": "claude"},
        )
    await _await_keep_warm_task(_CONV)

    assert resp.status_code == 202
    assert server.posts == [
        {
            "type": "external_keep_warm_receipt",
            "data": {
                "attempt_id": "att-1",
                "outcome": "failed",
                "reason": "harness_error",
                "input_total": None,
                "cache_read": None,
                "cache_write": None,
                "cost_usd": None,
                "estimated": False,
            },
        }
    ]


@pytest.mark.asyncio
async def test_keep_warm_ping_requires_a_string_attempt_id() -> None:
    """A missing/non-string ``attempt_id`` → 400; the idle clocks were still re-armed."""
    mgr = _KeepWarmProcessManager()
    server = _RecordingServerClient()
    app = _build_app(mgr, server)
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "keep_warm_ping", "attempt_id": 7, "family": "claude"},
        )

    assert resp.status_code == 400
    assert mgr.noted == [_CONV]
    assert app.state.native_pane_reaper.noted == [_CONV]
    assert mgr.get_client_calls == []
    assert server.posts == []


@pytest.mark.asyncio
async def test_unknown_event_type_still_reaches_the_generic_fallthrough() -> None:
    """The keep-warm branches don't swallow unknown types: they forward verbatim as before."""
    harness = _ReceiptHarnessClient({}, status_code=204)
    mgr = _KeepWarmProcessManager(harness_client=harness)
    server = _RecordingServerClient()
    app = _build_app(mgr, server)
    async with _runner_test_client(app) as http:
        resp = await http.post(
            f"/v1/sessions/{_CONV}/events",
            json={"type": "some_future_event", "extra": 1},
        )

    assert resp.status_code == 204
    assert harness.posts[0]["json"] == {"type": "some_future_event", "extra": 1}
    assert server.posts == []
