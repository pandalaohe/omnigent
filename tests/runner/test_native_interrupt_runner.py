"""Unit tests for ``NativeInterruptRunner`` (PR 1.6 interrupt/stop seam).

These drive the runner class directly with lightweight fakes, complementing the
HTTP-path tests in ``test_app_sessions_native_events_lifecycle.py`` /
``test_app_sessions_native_supervision.py`` (which POST to ``/events`` and patch
the bridge-module control functions). The focus here is the registry dispatch
and the descriptor-collapsed uniform handlers: which harnesses route where, the
soft-interrupt fall-through contract, and the 503 mapping.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.responses import Response

from omnigent.runner.native.interrupt import NativeInterruptRunner


@dataclass
class _FakeAck:
    """Stand-in for ``_SubagentDeliveryAck``."""

    delivered: bool = True
    entry: object | None = None
    reason: str = "delivered"


class _FakeTerminalRegistry:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def list_for_conversation(self, conv_id: str) -> list[Any]:
        return []


class _FakeResourceRegistry:
    def __init__(self) -> None:
        self.terminal_registry = _FakeTerminalRegistry()

    async def close_terminal(self, conv_id: str, terminal_id: str) -> bool:
        return True

    async def close_terminal_detailed(self, *args: Any, **kwargs: Any) -> str:
        return "closed"


def _make_runner(**overrides: Any) -> tuple[NativeInterruptRunner, dict[str, Any]]:
    """Build a runner with recording fakes; return it plus a capture dict."""
    captured: dict[str, Any] = {
        "published": [],
        "wakes": [],
        "wake_calls": [],
        "superseded": [],
        # The work_id the runner's ``subagent_work_id_for_session`` callback
        # reports; flip it to simulate a newer send replacing the dispatch.
        "current_work_id": None,
    }

    def _publish(conv_id: str, event: dict[str, Any]) -> None:
        captured["published"].append((conv_id, event))

    def _mark_and_wake(
        child_session_id: str,
        *,
        status: str,
        output: str | None,
        only_if_work_id: str | None = None,
    ) -> _FakeAck:
        if only_if_work_id is not None and only_if_work_id != captured["current_work_id"]:
            # A newer dispatch replaced the entry: drop the delayed op untouched.
            captured["superseded"].append((child_session_id, status, only_if_work_id))
            return _FakeAck(delivered=False, reason="superseded_dispatch")
        captured["wakes"].append((child_session_id, status, output))
        captured["wake_calls"].append(
            {
                "child_session_id": child_session_id,
                "status": status,
                "output": output,
                "only_if_work_id": only_if_work_id,
            }
        )
        return _FakeAck()

    async def _codex_bridge_state(conv_id: str, *, action: str, **_kw: Any) -> Any | None:
        return None

    def _client_safe(exc: BaseException, *, context: str) -> str:
        return f"safe:{context}"

    def _work_id_for_session(conv_id: str) -> str | None:
        return captured["current_work_id"]

    from omnigent.runner.session_runtime_lifecycle import SessionRuntimeLifecycle

    lifecycle = SessionRuntimeLifecycle()

    async def _cancel_turn(conv_id: str) -> None:
        return None

    kwargs: dict[str, Any] = {
        "server_client": SimpleNamespace(),
        "resource_registry": _FakeResourceRegistry(),
        "publish_event": _publish,
        "mark_subagent_terminal_and_wake": _mark_and_wake,
        "session_sub_agent_names": {},
        "session_has_active_work": lambda _session_id: True,
        "codex_bridge_state_for_session": _codex_bridge_state,
        "client_safe_error_detail": _client_safe,
        "logger": logging.getLogger("test.interrupt"),
        "subagent_work_id_for_session": _work_id_for_session,
        "runtime_lock_for_session": lifecycle.lock_for,
        "cancel_session_turn": _cancel_turn,
    }
    kwargs.update(overrides)
    return NativeInterruptRunner(**kwargs), captured


def test_native_cancel_capability_follows_stop_registry() -> None:
    """Parent cancel capability must track ``_UNIFORM_STOP`` plus Claude."""
    from omnigent.native.native_coding_agents import NATIVE_CODING_AGENTS
    from omnigent.runner.native.interrupt import (
        _UNIFORM_STOP,
        native_cancel_capability,
    )

    for agent in NATIVE_CODING_AGENTS:
        capability = native_cancel_capability(agent.wrapper_label)
        if agent.key in {"claude", "antigravity", "opencode"} or agent.key in _UNIFORM_STOP:
            assert capability == "stop", agent.key
        else:
            assert capability == "best_effort", agent.key
        if agent.subagent_wrapper_label:
            assert native_cancel_capability(agent.subagent_wrapper_label) == capability

    assert native_cancel_capability(None) == "inprocess"
    assert native_cancel_capability("not-a-native-wrapper") == "inprocess"


@pytest.mark.parametrize(
    ("pane_status", "has_active_turn", "expected"),
    [
        ("running", False, True),
        ("waiting", False, True),
        ("idle", True, False),
        (None, True, True),
        (None, False, False),
    ],
)
def test_native_session_has_active_work_prefers_observed_pane_status(
    pane_status: str | None,
    has_active_turn: bool,
    expected: bool,
) -> None:
    """An explicit idle pane wins over a lagging active-turn cleanup slot."""
    from omnigent.runner.native.interrupt import native_session_has_active_work

    assert native_session_has_active_work(pane_status, has_active_turn) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["antigravity-native", "opencode-native", "claude-sdk", None])
async def test_no_handler_harnesses_return_none(harness: str | None) -> None:
    """Harnesses without an interrupt/stop handler return None (caller falls through)."""
    runner, _ = _make_runner()
    assert await runner.interrupt(harness, "conv_x") is None
    if harness in {"claude-sdk", None}:
        assert await runner.stop(harness, "conv_x") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["antigravity-native", "opencode-native"])
@pytest.mark.parametrize("already_exited", [False, True])
async def test_owned_native_stop_ends_only_target_process(
    harness: str, already_exited: bool, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two sessions share a runner, but own separate CLI panes and servers."""

    import psutil

    from omnigent.harnesses.opencode_native.app_server import OpenCodeNativeServer
    from omnigent.inner.datamodel import TerminalEnvSpec
    from omnigent.runner.native import orchestration
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.terminals import TerminalRegistry

    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the process ownership check")
    registry = TerminalRegistry()
    resources = SessionResourceRegistry(terminal_registry=registry)
    servers = {}
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", servers)
    monkeypatch.setattr(orchestration, "_AUTO_FORWARDER_TASKS", {})

    async def ready(self: OpenCodeNativeServer) -> None:
        return None

    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", ready)
    name = harness.removesuffix("-native")
    spec = TerminalEnvSpec(command=sys.executable, args=["-c", "import time; time.sleep(60)"])
    processes = []
    try:
        target = await registry.launch("conv_target", name, "main", spec)
        sibling = await registry.launch("conv_sibling", name, "main", spec)
        target_pid = target.pane_pid_sync()
        sibling_pid = sibling.pane_pid_sync()
        assert target_pid is not None and sibling_pid is not None
        target_process = psutil.Process(target_pid)
        sibling_process = psutil.Process(sibling_pid)
        if harness == "opencode-native":
            for session_id in ["conv_target", "conv_sibling"]:
                server = OpenCodeNativeServer(
                    bridge_dir=tmp_path / session_id,
                    workspace=tmp_path,
                    opencode_path=sys.executable,
                    verify_version=False,
                )
                monkeypatch.setattr(
                    server,
                    "build_argv",
                    lambda: [sys.executable, "-c", "import time; time.sleep(60)"],
                )
                await server.start()
                process = server.process
                assert process is not None
                processes.append(process)
                server.process = process
                servers[session_id] = server
        if already_exited:
            target_process.terminate()
            await asyncio.to_thread(target_process.wait, 2)
            if processes:
                processes[0].terminate()
                await asyncio.to_thread(processes[0].wait, 2)
        runner, captured = _make_runner(resource_registry=resources)
        response = await runner.stop(harness, "conv_target")
        assert response is not None and response.status_code == 204
        await asyncio.to_thread(target_process.wait, 2)
        assert sibling_process.is_running()
        assert await sibling.is_alive()
        if processes:
            assert processes[0].poll() is not None
            assert processes[1].poll() is None
            assert "conv_sibling" in servers
        assert captured["wakes"] == [("conv_target", "cancelled", None)]
        response = await runner.stop(harness, "conv_target")
        assert response is not None and response.status_code == 204
    finally:
        await registry.shutdown()
        for process in processes:
            if process.poll() is None:
                process.kill()
            await asyncio.to_thread(process.wait, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["antigravity-native", "opencode-native"])
async def test_owned_native_stop_failure_does_not_claim_idle(
    harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from omnigent.runner.native import orchestration

    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    resources = _FakeResourceRegistry()
    instance = SimpleNamespace(is_alive=AsyncMock(return_value=True))
    entry = SimpleNamespace(terminal_name="native", session_key="main", instance=instance)
    monkeypatch.setattr(resources.terminal_registry, "list_for_conversation", lambda _: [entry])
    monkeypatch.setattr(
        resources, "close_terminal_detailed", AsyncMock(return_value="close_failed")
    )
    runner, captured = _make_runner(resource_registry=resources)
    response = await runner.stop(harness, "conv_target")
    assert response is not None and response.status_code == 503
    assert captured["wakes"] == []
    assert captured["published"] == []
    resources.close_terminal_detailed.assert_awaited_once_with(
        "conv_target", "terminal_native_main", expected_instance=instance
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["antigravity-native", "opencode-native"])
async def test_owned_native_stop_waits_for_launch_registration(
    harness: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import AsyncMock

    from omnigent.runner.native import orchestration
    from omnigent.runner.session_runtime_lifecycle import SessionRuntimeLifecycle

    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    monkeypatch.setattr(orchestration, "_AUTO_FORWARDER_TASKS", {})
    resources = _FakeResourceRegistry()
    entries = []
    instance = SimpleNamespace(is_alive=AsyncMock(return_value=False))
    monkeypatch.setattr(resources.terminal_registry, "list_for_conversation", lambda _: entries)
    close = AsyncMock(return_value="closed")
    monkeypatch.setattr(resources, "close_terminal_detailed", close)
    lifecycle = SessionRuntimeLifecycle()
    runner, captured = _make_runner(
        resource_registry=resources, runtime_lock_for_session=lifecycle.lock_for
    )
    async with lifecycle.lock_for("conv_target").shared():
        stop = asyncio.create_task(runner.stop(harness, "conv_target"))
        await asyncio.sleep(0)
        assert not stop.done()
        assert captured["wakes"] == []
        entries.append(
            SimpleNamespace(terminal_name="native", session_key="main", instance=instance)
        )
    response = await stop
    assert response is not None and response.status_code == 204
    close.assert_awaited_once_with(
        "conv_target", "terminal_native_main", expected_instance=instance
    )


@pytest.mark.asyncio
async def test_owned_native_stop_keeps_replacement_forwarder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from omnigent.runner.native import orchestration

    old_task = asyncio.create_task(asyncio.Event().wait())
    new_task = asyncio.create_task(asyncio.Event().wait())
    new_server = SimpleNamespace(close=AsyncMock())
    servers = {}
    tasks = {"conv_target": old_task}

    async def close_old_server() -> None:
        servers["conv_target"] = new_server
        tasks["conv_target"] = new_task

    old_server = SimpleNamespace(close=close_old_server)
    servers["conv_target"] = old_server
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", servers)
    monkeypatch.setattr(orchestration, "_AUTO_FORWARDER_TASKS", tasks)
    runner, _ = _make_runner()
    try:
        response = await runner.stop("opencode-native", "conv_target")
        assert response is not None and response.status_code == 204
        assert old_task.cancelled()
        assert not new_task.done()
        assert tasks["conv_target"] is new_task
        assert servers["conv_target"] is new_server
        new_server.close.assert_not_awaited()
    finally:
        old_task.cancel()
        new_task.cancel()
        await asyncio.gather(old_task, new_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["antigravity-native", "opencode-native"])
async def test_http_owned_native_stop_waits_for_runtime_registration(
    harness: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.runner.native import orchestration
    from omnigent.terminals import TerminalRegistry
    from tests.runner.conftest import _build_app_for_spec, _runner_client
    from tests.runner.helpers import make_test_terminal_instance
    from tests.runner.native_helpers import _harness_spec

    async def no_vendor_launch(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(orchestration, "_auto_create_antigravity_terminal", no_vendor_launch)
    monkeypatch.setattr(orchestration, "_auto_create_opencode_terminal", no_vendor_launch)
    monkeypatch.setattr(orchestration, "_AUTO_FORWARDER_TASKS", {})
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    registry = TerminalRegistry()
    app, _ = await _build_app_for_spec(_harness_spec(harness), terminal_registry=registry)
    session_id = "conv_starting"
    name = harness.removesuffix("-native")
    instance = make_test_terminal_instance(name, "main", tmp_path)
    async with _runner_client(app) as client:
        created = await client.post(
            "/v1/sessions", json={"session_id": session_id, "agent_id": "agent"}
        )
        assert created.status_code == 201
        async with app.state.cli_runtime_lifecycle.lock_for(session_id).shared():
            stop = asyncio.create_task(
                client.post(f"/v1/sessions/{session_id}/events", json={"type": "stop_session"})
            )
            await asyncio.sleep(0)
            assert not stop.done()
            registry._by_conversation[session_id] = {(name, "main"): instance}
        response = await stop
        assert response.status_code == 204, response.text
        assert not instance.running
        assert registry.get(session_id, name, "main") is None


@pytest.mark.asyncio
async def test_uniform_interrupt_defers_parent_wake_until_outcome_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt injects the Escape but must not report a terminal status yet.

    Injecting an Escape does not confirm the agent stopped, so no ``cancelled``
    may be delivered until the harness's terminal edge or the grace timer
    resolves the outcome.
    """
    import omnigent.harnesses.goose_native.bridge as goose_bridge

    calls: list[Any] = []

    def _inject(bridge_dir: Any, *, timeout_s: float) -> None:
        calls.append((bridge_dir, timeout_s))

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", _inject)

    runner, captured = _make_runner()
    resp = await runner.interrupt("goose-native", "conv_g")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert calls == [("dir/conv_g", 1.0)]
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_g")[0] is True
    # Consumed: a second take finds nothing and the grace timer is disarmed.
    assert runner.take_pending_interrupt("conv_g")[0] is False


@pytest.mark.asyncio
async def test_interrupt_grace_timer_delivers_unconfirmed_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no terminal edge after the interrupt, the grace timer reports cancelled."""
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_g"  # a live dispatch to bind the cancel to
    resp = await runner.interrupt("goose-native", "conv_g")
    assert isinstance(resp, Response) and resp.status_code == 204
    assert captured["wakes"] == []

    await asyncio.sleep(0.1)
    assert captured["wakes"] == [("conv_g", "cancelled", None)]
    assert runner.take_pending_interrupt("conv_g")[0] is False


@pytest.mark.asyncio
async def test_grace_timer_does_not_cancel_superseded_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old interrupt's grace timer must not cancel a newer dispatch.

    Interrupt turn A, then a new send registers turn B on the same child before
    A's timer fires. The timer is bound to A's ``work_id``, so when it fires the
    newer dispatch B is left untouched and can still complete — instead of B
    being cancelled and its result lost.
    """
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_A"
    await runner.interrupt("goose-native", "conv_g")  # defers, bound to work_A
    # A new send reuses the child session before the timer fires.
    captured["current_work_id"] = "work_B"

    await asyncio.sleep(0.1)
    assert captured["wakes"] == [], "the newer dispatch must not be cancelled"
    assert captured["superseded"] == [("conv_g", "cancelled", "work_A")]


@pytest.mark.asyncio
async def test_resolve_pending_interrupt_only_consumes_matching_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending interrupt resolves only for its own dispatch, else is dropped.

    An idle for a NEWER dispatch (the interrupted one exited or was superseded)
    must not consume the stale pending — for a legacy harness that idle is the
    new dispatch's completion. ``resolve_pending_interrupt`` reports pending
    only on a work_id match; otherwise it drops the stale record and reports
    not-pending so the caller handles the idle normally.
    """
    import omnigent.harnesses.goose_native.bridge as goose_bridge

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_A"
    await runner.interrupt("goose-native", "conv_g")  # pending bound to work_A

    # An idle arrives for a NEWER dispatch: the pending is stale and must be
    # dropped, letting the idle be handled as work_B's own outcome.
    resolved, work_id = runner.resolve_pending_interrupt("conv_g", "work_B")
    assert (resolved, work_id) == (False, None)
    assert runner.take_pending_interrupt("conv_g")[0] is False  # stale record dropped

    # A matching dispatch's idle DOES resolve its interrupt.
    captured["current_work_id"] = "work_C"
    await runner.interrupt("goose-native", "conv_h")
    resolved, work_id = runner.resolve_pending_interrupt("conv_h", "work_C")
    assert (resolved, work_id) == (True, "work_C")


@pytest.mark.asyncio
async def test_repeated_interrupt_on_reused_child_gives_new_dispatch_its_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale pending interrupt must not deny a new dispatch its cancellation.

    Interrupt A and leave its pending record (its launch was reaped before the
    timer fired). Reuse the child for B and interrupt it: B must get its OWN
    pending record and grace timer — the earlier record for A is replaced, not
    dedup-suppressed — so B still has a cancellation fallback and its parent is
    not left waiting indefinitely.
    """
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.05)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_A"
    await runner.interrupt("goose-native", "conv_g")  # A's pending (work_A) lingers
    # The child is reused for dispatch B and interrupted before A's timer fires.
    captured["current_work_id"] = "work_B"
    await runner.interrupt("goose-native", "conv_g")  # must replace A with work_B

    await asyncio.sleep(0.2)
    # B receives its OWN grace-period cancellation (bound to work_B, delivered),
    # rather than being suppressed by A's stale record.
    assert captured["wakes"] == [("conv_g", "cancelled", None)]
    assert captured["superseded"] == []


@pytest.mark.asyncio
async def test_grace_timer_skips_cancel_when_dispatch_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt with no bound dispatch must not cancel anything on timeout.

    If no work entry existed when the interrupt fired (e.g. a runner restart had
    not recovered it), the timer captures ``None`` and cannot bind. Delivering a
    cancel then could settle a newer send that reused the child, so the timer
    must skip entirely — the restart recovery scan owns unbound children.
    """
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    captured["current_work_id"] = None  # no dispatch to bind to
    await runner.interrupt("goose-native", "conv_g")

    await asyncio.sleep(0.1)
    assert captured["wakes"] == [], "an unbound interrupt must deliver no cancel"
    assert captured["superseded"] == []


@pytest.mark.asyncio
async def test_resolved_interrupt_disarms_grace_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal edge that consumed the pending interrupt silences the timer."""
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    await runner.interrupt("goose-native", "conv_g")
    assert runner.take_pending_interrupt("conv_g")[0] is True

    await asyncio.sleep(0.1)
    assert captured["wakes"] == []


@pytest.mark.asyncio
async def test_pi_interrupt_uses_enqueue_without_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pi's uniform interrupt uses enqueue_interrupt with no timeout kwarg."""
    import omnigent.harnesses.pi_native.bridge as pi_bridge

    calls: list[Any] = []
    monkeypatch.setattr(pi_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(
        pi_bridge, "enqueue_interrupt", lambda bridge_dir: calls.append(bridge_dir)
    )

    runner, _ = _make_runner()
    resp = await runner.interrupt("pi-native", "conv_p")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert calls == ["dir/conv_p"]


@pytest.mark.asyncio
async def test_uniform_interrupt_bridge_error_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RuntimeError from the bridge inject maps to a 503 with the error code."""
    import json

    import omnigent.harnesses.qwen_native.bridge as qwen_bridge

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(qwen_bridge, "bridge_dir_for_session_id", lambda conv: "d")
    monkeypatch.setattr(qwen_bridge, "inject_interrupt", _boom)

    runner, captured = _make_runner()
    resp = await runner.interrupt("qwen-native", "conv_q")

    assert resp is not None and resp.status_code == 503
    body = json.loads(bytes(resp.body))
    assert body["error"] == "qwen_native_interrupt_failed"
    assert body["detail"] == "safe:qwen-native interrupt"
    # No parent wake on failure.
    assert captured["wakes"] == []


@pytest.mark.asyncio
async def test_uniform_stop_kills_tears_down_and_goes_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A uniform stop kills the bridge, publishes idle, and wakes the parent."""
    import omnigent.harnesses.cursor_native.bridge as cursor_bridge

    killed: list[Any] = []
    monkeypatch.setattr(cursor_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(
        cursor_bridge,
        "kill_session",
        lambda bridge_dir, *, timeout_s: killed.append((bridge_dir, timeout_s)),
    )

    runner, captured = _make_runner()
    # A stop that follows an unresolved interrupt settles it: the kill is
    # confirmed, so no grace-timer cancel may fire later.
    runner._pending_interrupts["conv_c"] = 0.0
    resp = await runner.stop("cursor-native", "conv_c")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert runner.take_pending_interrupt("conv_c")[0] is False
    assert killed == [("dir/conv_c", 1.0)]
    idle = [e for _, e in captured["published"] if e.get("status") == "idle"]
    assert idle == [{"type": "session.status", "status": "idle"}]
    assert captured["wakes"] == [("conv_c", "cancelled", None)]


@pytest.mark.asyncio
async def test_uniform_stop_kill_failure_returns_503_without_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed kill returns 503 and does NOT publish idle (no lie to the UI)."""
    import json

    import omnigent.harnesses.hermes_native.bridge as hermes_bridge

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(hermes_bridge, "bridge_dir_for_session_id", lambda conv: "d")
    monkeypatch.setattr(hermes_bridge, "kill_session", _boom)

    runner, captured = _make_runner()
    resp = await runner.stop("hermes-native", "conv_h")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "hermes_native_stop_failed"
    assert [e for _, e in captured["published"] if e.get("status") == "idle"] == []


@pytest.mark.asyncio
async def test_codex_and_pi_stop_route_to_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex/pi have no distinct stop — stop() routes to their interrupt handler."""
    import omnigent.harnesses.pi_native.bridge as pi_bridge

    calls: list[str] = []
    monkeypatch.setattr(pi_bridge, "bridge_dir_for_session_id", lambda conv: conv)
    monkeypatch.setattr(
        pi_bridge, "enqueue_interrupt", lambda bridge_dir: calls.append(bridge_dir)
    )

    runner, _ = _make_runner()
    resp = await runner.stop("pi-native", "conv_p")

    assert isinstance(resp, Response) and resp.status_code == 204
    # The interrupt path ran (enqueue_interrupt), not a kill_session.
    assert calls == ["conv_p"]


@pytest.mark.asyncio
async def test_codex_interrupt_defers_to_runner_when_no_bridge_state() -> None:
    """A missing native bridge must not suppress cancellation of runner setup."""
    runner, _ = _make_runner()  # default codex_bridge_state returns None
    resp = await runner.interrupt("codex-native", "conv_cx")
    assert resp is None


@pytest.mark.asyncio
async def test_codex_interrupt_defers_to_runner_before_first_native_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from omnigent.harnesses.codex_native import bridge
    from tests.runner.helpers import NullServerClient

    async def idle_bridge(*args, **kwargs):
        return SimpleNamespace(active_turn_id=None)

    monkeypatch.setattr(bridge, "bridge_dir_for_bridge_id", lambda _id: tmp_path)
    monkeypatch.setattr(bridge, "cancel_pending_mcp_startup", lambda _path: [])
    runner, captured = _make_runner(
        server_client=NullServerClient(), codex_bridge_state_for_session=idle_bridge
    )
    assert await runner.interrupt("codex-native", "conv_cx") is None
    assert captured["published"] == []


@pytest.mark.asyncio
async def test_claude_stop_is_idempotent_without_advertised_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-absent Claude pane still completes stop teardown."""
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client, session_id
        return "bridge123"

    def _absent(bridge_dir: Any, *, timeout_s: float) -> None:
        del bridge_dir, timeout_s
        raise claude_bridge.TmuxSessionNotAdvertised("not advertised")

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bridge_id: bridge_id)
    monkeypatch.setattr(claude_bridge, "kill_session", _absent)

    runner, captured = _make_runner()
    resp = await runner.stop("claude-native", "conv_cn")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert captured["wakes"] == [("conv_cn", "cancelled", None)]


@pytest.mark.asyncio
async def test_claude_stop_kill_failure_returns_503_without_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Claude kill ``RuntimeError`` is 503; only ``TmuxSessionNotAdvertised`` is 204."""
    import json

    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client, session_id
        return "bridge123"

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        del bridge_dir, timeout_s
        raise RuntimeError("tmux kill-session failed: connection refused")

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bridge_id: bridge_id)
    monkeypatch.setattr(claude_bridge, "kill_session", _boom)

    runner, captured = _make_runner()
    resp = await runner.stop("claude-native", "conv_cn")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "claude_native_stop_failed"
    assert captured["wakes"] == []
    assert [e for _, e in captured["published"] if e.get("status") == "idle"] == []


@pytest.mark.asyncio
async def test_claude_interrupt_resolves_bridge_id_and_injects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude interrupt resolves the bridge id and injects, deferring the wake."""
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        return f"bid-{session_id}"

    injected: list[Any] = []
    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(
        claude_bridge,
        "inject_interrupt",
        lambda bridge_dir, *, timeout_s: injected.append((bridge_dir, timeout_s)),
    )

    runner, captured = _make_runner()
    resp = await runner.interrupt("claude-native", "conv_cl")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert injected == [("dir/bid-conv_cl", 1.0)]
    # No optimistic 'cancelled': the outcome is unknown until an edge lands.
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_cl")[0] is True


@pytest.mark.asyncio
async def test_claude_interrupt_skips_stale_stop_after_terminal_turns_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle edge immediately before Stop prevents Ctrl+C injection."""
    from omnigent.runner.native import interrupt as interrupt_mod

    bridge_lookups: list[str] = []

    async def _unexpected_bridge_lookup(*, server_client: Any, session_id: str) -> str:
        del server_client
        bridge_lookups.append(session_id)
        return "unexpected"

    monkeypatch.setattr(
        interrupt_mod,
        "_claude_native_bridge_id_for_session",
        _unexpected_bridge_lookup,
    )
    from omnigent.runner.native.interrupt import native_session_has_active_work

    pane_status = {"conv_cl": "running"}
    active_turns = {"conv_cl"}
    runner, captured = _make_runner(
        session_has_active_work=lambda session_id: native_session_has_active_work(
            pane_status.get(session_id), session_id in active_turns
        )
    )

    # The terminal settles after the UI issued Stop but before Runner handles it;
    # the task-slot cleanup intentionally still lags behind the authoritative pane.
    pane_status["conv_cl"] = "idle"
    resp = await runner.interrupt("claude-native", "conv_cl")

    assert isinstance(resp, Response) and resp.status_code == 200
    assert resp.body == b'{"interrupted":false,"reason":"idle"}'
    assert bridge_lookups == []
    assert captured["wakes"] == []


@pytest.mark.asyncio
async def test_claude_interrupt_rechecks_idle_after_bridge_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal edge during bridge lookup closes the last injection race."""
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod
    from omnigent.runner.native.interrupt import native_session_has_active_work

    lookup_started = asyncio.Event()
    release_lookup = asyncio.Event()
    injected: list[str] = []

    async def _slow_bridge_lookup(*, server_client: Any, session_id: str) -> str:
        del server_client
        lookup_started.set()
        await release_lookup.wait()
        return f"bid-{session_id}"

    monkeypatch.setattr(
        interrupt_mod,
        "_claude_native_bridge_id_for_session",
        _slow_bridge_lookup,
    )
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(
        claude_bridge,
        "inject_interrupt",
        lambda bridge_dir, *, timeout_s: injected.append(bridge_dir),
    )
    pane_status = {"conv_cl": "running"}
    active_turns = {"conv_cl"}
    runner, captured = _make_runner(
        session_has_active_work=lambda session_id: native_session_has_active_work(
            pane_status.get(session_id), session_id in active_turns
        )
    )

    pending = asyncio.create_task(runner.interrupt("claude-native", "conv_cl"))
    await asyncio.wait_for(lookup_started.wait(), timeout=1.0)
    pane_status["conv_cl"] = "idle"
    release_lookup.set()
    resp = await pending

    assert isinstance(resp, Response) and resp.status_code == 200
    assert resp.body == b'{"interrupted":false,"reason":"idle"}'
    assert injected == []
    assert captured["wakes"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt_still_pending", [False, True])
async def test_claude_interrupt_rechecks_pending_prompt_after_bridge_lookup(
    monkeypatch: pytest.MonkeyPatch,
    prompt_still_pending: bool,
) -> None:
    """A question answered during bridge lookup must not take Ctrl+C at the composer.

    ``prompt_pending`` is a snapshot taken before the async bridge-id lookup;
    only the post-lookup ``has_pending_user_prompt`` re-check may authorise
    injecting into a pane with no active work.
    """
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    release_lookup = asyncio.Event()
    lookup_started = asyncio.Event()
    events: list[Any] = []
    injected: list[str] = []

    async def _slow_bridge_lookup(*, server_client: Any, session_id: str) -> str:
        del server_client
        events.append("lookup-start")
        lookup_started.set()
        await release_lookup.wait()
        events.append("lookup-end")
        return f"bid-{session_id}"

    def _pending_prompt(bridge_dir: Any) -> bool:
        events.append(("prompt-check", bridge_dir))
        return prompt_still_pending

    monkeypatch.setattr(
        interrupt_mod,
        "_claude_native_bridge_id_for_session",
        _slow_bridge_lookup,
    )
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(claude_bridge, "has_pending_user_prompt", _pending_prompt)
    monkeypatch.setattr(
        claude_bridge,
        "inject_interrupt",
        lambda bridge_dir, *, timeout_s: injected.append(bridge_dir),
    )
    runner, captured = _make_runner(session_has_active_work=lambda _session_id: False)

    pending = asyncio.create_task(
        runner.interrupt("claude-native", "conv_cl", prompt_pending=True)
    )
    await asyncio.wait_for(lookup_started.wait(), timeout=1.0)
    release_lookup.set()
    resp = await pending

    # The predicate the waiter polls is consulted only after the lookup, and only
    # with the resolved bridge directory.
    assert events == ["lookup-start", "lookup-end", ("prompt-check", "dir/bid-conv_cl")]
    if prompt_still_pending:
        assert isinstance(resp, Response) and resp.status_code == 204
        assert injected == ["dir/bid-conv_cl"]
        assert captured["wakes"] == []
        assert runner.take_pending_interrupt("conv_cl")[0] is True
    else:
        assert isinstance(resp, Response) and resp.status_code == 200
        assert resp.body == b'{"interrupted":false,"reason":"idle"}'
        assert injected == []
        assert captured["wakes"] == []
