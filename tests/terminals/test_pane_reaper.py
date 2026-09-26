"""Tests for the native-pane idle reaper (issue #1349)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.inner.terminal import TerminalInstance
from omnigent.native import native_cost_popup
from omnigent.runner.app import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.terminals.pane_reaper import (
    _DEFAULT_IDLE_TIMEOUT_S,
    _IDLE_TIMEOUT_ENV,
    PANE_OUTPUT_BUSY_WINDOW_S,
    PENDING_RETIRE_MAX_AGE_S,
    RETENTION_LEASE_S,
    NativePaneReaper,
    NativePaneStillAlive,
    PaneRef,
    resolve_native_pane_idle_timeout_s,
)
from omnigent.terminals.registry import TerminalRegistry
from tests.runner.helpers import NullServerClient


def _pane(conv: str, name: str = "claude") -> PaneRef:
    return PaneRef(conv, f"terminal:{name}:main", name, Path(f"/tmp/omni-test/{conv}.sock"))


class _Fakes:
    """Mutable test doubles so a test can flip busy/panes between scans."""

    def __init__(self) -> None:
        self.panes: list[PaneRef] = []
        self.busy: set[str] = set()
        self.reaped: list[str] = []
        # Optional per-call override: (pane, call_index) -> bool. Lets a test make
        # is_busy answer differently on the classify pass vs the re-check pass.
        self.busy_override: Callable[[PaneRef, int], bool] | None = None
        self.busy_calls = 0

    async def is_busy(self, pane: PaneRef) -> bool:
        self.busy_calls += 1
        if self.busy_override is not None:
            return self.busy_override(pane, self.busy_calls)
        return pane.conversation_id in self.busy

    async def reap(self, pane: PaneRef) -> None:
        self.reaped.append(pane.conversation_id)
        self.panes = [p for p in self.panes if p.conversation_id != pane.conversation_id]


def _make(fakes: _Fakes, *, timeout: float = 100.0, interval: float = 0.01) -> NativePaneReaper:
    return NativePaneReaper(
        list_native_panes=lambda: list(fakes.panes),
        is_busy=fakes.is_busy,
        reap=fakes.reap,
        idle_timeout_s=timeout,
        reaper_interval_s=interval,
    )


# ── Pure idle-clock decision (_classify) ────────────────────────────────────


def test_classify_reaps_only_after_full_window() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    f.panes = [p]
    r = _make(f, timeout=100.0)
    assert r._classify(1000.0, [p], busy_convs=set()) == []  # first obs: grace
    assert r._classify(1099.0, [p], busy_convs=set()) == []  # 99s < 100s
    assert r._classify(1100.0, [p], busy_convs=set()) == [p]  # window elapsed


def test_classify_busy_rearms_clock() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    r = _make(f, timeout=10.0)
    r._classify(0.0, [p], busy_convs={"conv_a"})
    assert r._classify(1000.0, [p], busy_convs={"conv_a"}) == []  # busy re-arms
    r._classify(1000.0, [p], busy_convs=set())  # now idle, grace
    assert r._classify(1010.0, [p], busy_convs=set()) == [p]


def test_classify_first_observation_grace() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    r = _make(f, timeout=0.001)
    assert r._classify(5.0, [p], busy_convs=set()) == []  # clock seeded this pass


def test_classify_forgets_gone_panes() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    r = _make(f, timeout=10.0)
    r._classify(0.0, [p], busy_convs=set())
    assert "conv_a" in r._last_busy_at
    r._classify(1.0, [], busy_convs=set())  # pane gone
    assert "conv_a" not in r._last_busy_at


# ── Scan behaviour (_scan_once): reap, skip-busy, TOCTOU re-check ────────────


async def test_scan_reaps_idle_unbusy_pane() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    f.panes = [p]
    r = _make(f, timeout=10.0)
    r._last_busy_at["conv_a"] = time.monotonic() - 1000  # already idle past window
    await r._scan_once()
    assert f.reaped == ["conv_a"]


async def test_scan_skips_busy_pane() -> None:
    f = _Fakes()
    p = _pane("conv_a")
    f.panes = [p]
    f.busy = {"conv_a"}
    r = _make(f, timeout=10.0)
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    await r._scan_once()
    assert f.reaped == []  # busy → not reaped, clock re-armed


async def test_scan_recheck_spares_pane_that_became_busy() -> None:
    """TOCTOU guard: a pane idle at selection but busy at the pre-reap re-check
    must NOT be reaped (a turn/client/autonomous run started in between)."""
    f = _Fakes()
    p = _pane("conv_a")
    f.panes = [p]
    # is_busy: False on the classify-phase call (call 1), True on the re-check
    # call (call 2) — simulating a turn starting between selection and teardown.
    f.busy_override = lambda pane, n: n >= 2
    r = _make(f, timeout=10.0)
    r._last_busy_at["conv_a"] = time.monotonic() - 1000
    await r._scan_once()
    assert f.reaped == []  # spared by the re-check
    assert f.busy_calls == 2  # classify + re-check


async def test_retention_release_failure_keeps_pane_managed_for_retry() -> None:
    f = _Fakes()
    pane = _pane("conv_a")
    f.panes = [pane]
    reaper = _make(f, timeout=10.0)
    reaper.manage("conv_a")

    async def _fail(_pane: PaneRef) -> None:
        raise RuntimeError("close timeout")

    reaper._reap = _fail
    reaper._last_busy_at["conv_a"] = time.monotonic() - 100
    activity_token = f"{reaper._last_busy_at['conv_a']:.9f}"

    assert (
        await reaper.release_if_idle(
            "conv_a",
            idle_threshold_s=10,
            expected_activity_token=activity_token,
        )
        == "failed"
    )
    assert reaper.has_managed_panes() is True
    assert f"{reaper._last_busy_at['conv_a']:.9f}" == activity_token


async def test_retention_lease_renews_and_expiry_restores_legacy_scan(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from omnigent.terminals import pane_reaper

    clock = [100.0]
    monkeypatch.setattr(pane_reaper.time, "monotonic", lambda: clock[0])
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    reaper = _make(f, timeout=3600)
    reaper._last_busy_at["conv_a"] = clock[0] - 7200
    for _ in range(30):
        reaper.manage("conv_a")
        clock[0] += 60
        await reaper._scan_once()
    assert f.reaped == []
    assert reaper.has_managed_panes()
    clock[0] += RETENTION_LEASE_S + 1
    with caplog.at_level("INFO"):
        assert not reaper.has_managed_panes()
        await reaper._scan_once()
    assert f.reaped == ["conv_a"]
    assert sum("retention lease expired" in row.message for row in caplog.records) == 1


async def test_expired_lease_keeps_recently_busy_pane(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.terminals import pane_reaper

    clock = [100.0]
    monkeypatch.setattr(pane_reaper.time, "monotonic", lambda: clock[0])
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    reaper = _make(f, timeout=3600)
    reaper.manage("conv_a")
    clock[0] += 1260
    reaper._last_busy_at["conv_a"] = clock[0] - 600
    await reaper._scan_once()
    assert f.reaped == []


async def test_failed_release_restores_saved_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.terminals import pane_reaper

    clock = [100.0]
    monkeypatch.setattr(pane_reaper.time, "monotonic", lambda: clock[0])
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    reaper = _make(f)
    reaper.manage("conv_a")
    expiry = reaper._managed_conversations["conv_a"]
    reaper._last_busy_at["conv_a"] = clock[0] - 100

    async def fail(_pane: PaneRef) -> None:
        raise RuntimeError("close failed")

    reaper._reap = fail
    clock[0] += 19 * 60
    assert await reaper.release_now("conv_a") == "failed"
    assert reaper._managed_conversations["conv_a"] == expiry
    clock[0] = expiry + 1
    assert not reaper.has_managed_panes()


@pytest.mark.parametrize("release_kind", ["now", "idle"])
@pytest.mark.parametrize("raises", [False, True])
async def test_failed_release_preserves_concurrent_renewal(
    release_kind: str, raises: bool
) -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    reaper = _make(f)
    reaper.manage("conv_a")
    saved_expiry = reaper._managed_conversations["conv_a"]
    reaper._last_busy_at["conv_a"] = time.monotonic() - 100
    token = f"{reaper._last_busy_at['conv_a']:.9f}"
    entered = asyncio.Event()
    resume = asyncio.Event()

    async def fail(_pane: PaneRef) -> None:
        entered.set()
        await resume.wait()
        if raises:
            raise RuntimeError("close failed")
        raise NativePaneStillAlive(object(), True)

    reaper._reap = fail
    if release_kind == "now":
        releasing = asyncio.create_task(reaper.release_now("conv_a"))
    else:
        releasing = asyncio.create_task(
            reaper.release_if_idle("conv_a", idle_threshold_s=1, expected_activity_token=token)
        )
    await asyncio.wait_for(entered.wait(), timeout=1)
    reaper.manage("conv_a")
    renewed_expiry = reaper._managed_conversations["conv_a"]
    resume.set()
    assert await releasing == "failed"
    assert renewed_expiry > saved_expiry
    assert reaper._managed_conversations["conv_a"] == renewed_expiry


class _RetiringInstance:
    def __init__(self, probes: list[bool | None]) -> None:
        self.probes = probes
        self.closed = 0
        self.killed = 0
        self.kill_error = False

    async def probe_alive(self) -> bool | None:
        return self.probes.pop(0) if len(self.probes) > 1 else self.probes[0]

    async def kill_server(self) -> None:
        self.killed += 1
        if self.kill_error:
            raise RuntimeError("kill failed")

    async def close(self) -> None:
        self.closed += 1


async def _pending_reaper(instance: _RetiringInstance):
    pane = PaneRef("conv_a", "terminal:codex:main", "codex", Path("/tmp/test.sock"), instance)
    listed = [pane]
    tails: list[tuple[str, str, str]] = []

    async def reap(_pane: PaneRef) -> None:
        raise NativePaneStillAlive(instance, True)

    async def finish(conv: str, name: str, token: str) -> None:
        tails.append((conv, name, token))

    reaper = NativePaneReaper(
        list_native_panes=lambda: list(listed),
        is_busy=lambda _pane: asyncio.sleep(0, result=False),
        reap=reap,
        runtime_token=lambda _conv: "boot:1",
        finish_retired=finish,
        idle_timeout_s=0,
        reaper_interval_s=0.01,
    )
    assert await reaper.release_now("conv_a") == "failed"
    return reaper, listed, pane, tails


async def test_pending_retirement_runs_with_legacy_ttl_disabled() -> None:
    instance = _RetiringInstance([False])
    reaper, listed, _pane_ref, tails = await _pending_reaper(instance)
    listed.clear()
    await reaper.start()
    try:
        for _ in range(100):
            if tails:
                break
            await asyncio.sleep(0.01)
    finally:
        await reaper.shutdown()
    assert tails == [("conv_a", "codex", "boot:1")]
    assert instance.closed == 1
    assert not reaper._pending_retire


@pytest.mark.parametrize("close_mode", ["hang", "error"])
async def test_dead_pending_close_failure_still_runs_tail(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, close_mode: str
) -> None:
    from omnigent.terminals import pane_reaper

    class _StuckClose(_RetiringInstance):
        async def close(self) -> None:
            self.closed += 1
            if close_mode == "error":
                raise RuntimeError("cleanup failed")
            await asyncio.Event().wait()

    instance = _StuckClose([False])
    reaper, listed, _pane_ref, _tails = await _pending_reaper(instance)
    listed.clear()
    monkeypatch.setattr(pane_reaper, "PENDING_RETIRE_CLOSE_TIMEOUT_S", 0.01)
    tails: list[str] = []

    async def finish(_conv: str, _name: str, token: str) -> str | None:
        tails.append(token)
        return token if len(tails) == 1 else None

    reaper._finish_retired = finish
    with caplog.at_level("WARNING"):
        await asyncio.wait_for(reaper._retire_pending(), timeout=0.2)
        assert reaper._pending_retire
        await asyncio.wait_for(reaper._retire_pending(), timeout=0.2)
    assert tails == ["boot:1", "boot:1"]
    assert instance.closed == 2
    assert not reaper._pending_retire
    assert sum("retirement cleanup failed" in row.message for row in caplog.records) == 1


async def test_repeated_pending_close_timeouts_reap_tmux_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.terminals import pane_reaper

    shared_lock = asyncio.Lock()
    clients: list[_HeldClient] = []

    class _HeldClient:
        returncode: int | None = None

        def __init__(self) -> None:
            self.killed = False
            self.waited = False
            self.exited = asyncio.Event()

        async def communicate(self) -> tuple[bytes, bytes]:
            async with shared_lock:
                return b"", b""

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9
            self.exited.set()

        async def wait(self) -> int | None:
            self.waited = True
            await self.exited.wait()
            return self.returncode

    async def spawn(*_cmd: str, **_kwargs: object) -> _HeldClient:
        client = _HeldClient()
        clients.append(client)
        return client

    terminal = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path / "terminal",
        running=True,
    )

    class _HangingClose(_RetiringInstance):
        async def close(self) -> None:
            self.closed += 1
            await terminal.close()

    instance = _HangingClose([False])
    reaper, listed, _pane_ref, _tails = await _pending_reaper(instance)
    listed.clear()
    monkeypatch.setattr(terminal_mod.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(pane_reaper, "PENDING_RETIRE_CLOSE_TIMEOUT_S", 0.01)

    async def keep_pending(_conv: str, _name: str, token: str) -> str:
        return token

    reaper._finish_retired = keep_pending
    async with shared_lock:
        for _ in range(3):
            await asyncio.wait_for(reaper._retire_pending(), timeout=0.2)

    assert instance.closed == 3
    assert len(clients) == 3
    assert all(client.killed and client.waited for client in clients)
    assert not any(client.returncode is None for client in clients)


async def test_pending_retirement_drops_only_relisted_instance() -> None:
    instance = _RetiringInstance([True])
    reaper, _listed, _pane_ref, tails = await _pending_reaper(instance)
    await reaper._retire_pending()
    assert not reaper._pending_retire
    assert instance.killed == 0
    assert tails == []


async def test_successor_does_not_drop_orphan_record() -> None:
    instance = _RetiringInstance([True, False])
    reaper, listed, _pane_ref, tails = await _pending_reaper(instance)
    listed[:] = [
        PaneRef("conv_a", "terminal:codex:main", "codex", Path("/tmp/next.sock"), object())
    ]
    record = reaper._pending_retire[id(instance)]
    record.first_seen -= PENDING_RETIRE_MAX_AGE_S + 1
    await reaper._retire_pending()
    assert instance.killed == 1
    assert instance.closed == 1
    assert tails == [("conv_a", "codex", "boot:1")]


@pytest.mark.parametrize("post_kill_probe", [None, True])
async def test_pending_retirement_waits_for_confirmed_death(
    post_kill_probe: bool | None, caplog: pytest.LogCaptureFixture
) -> None:
    instance = _RetiringInstance([True, post_kill_probe])
    reaper, listed, _pane_ref, tails = await _pending_reaper(instance)
    listed.clear()
    reaper._pending_retire[id(instance)].first_seen -= PENDING_RETIRE_MAX_AGE_S + 1
    with caplog.at_level("WARNING"):
        await reaper._retire_pending()
        await reaper._retire_pending()
    assert instance.closed == 0
    assert reaper._pending_retire
    assert tails == []
    assert sum("could not confirm death" in row.message for row in caplog.records) == 1


async def test_pending_retirement_keeps_record_when_kill_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    instance = _RetiringInstance([True])
    instance.kill_error = True
    reaper, listed, _pane_ref, tails = await _pending_reaper(instance)
    listed.clear()
    reaper._pending_retire[id(instance)].first_seen -= PENDING_RETIRE_MAX_AGE_S + 1
    with caplog.at_level("WARNING"):
        await reaper._retire_pending()
        await reaper._retire_pending()
    assert instance.closed == 0
    assert reaper._pending_retire
    assert tails == []
    assert sum("could not confirm death" in row.message for row in caplog.records) == 1


async def test_absent_release_fails_while_pending_tail_is_owed() -> None:
    instance = _RetiringInstance([True])
    reaper, listed, _pane_ref, _tails = await _pending_reaper(instance)
    listed.clear()
    assert await reaper.release_now("conv_a") == "failed"
    assert (
        await reaper.release_if_idle(
            "conv_a", idle_threshold_s=60, expected_activity_token="unused"
        )
        == "failed"
    )


# ── Env resolver ────────────────────────────────────────────────────────────


def test_resolve_default_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_IDLE_TIMEOUT_ENV, raising=False)
    assert resolve_native_pane_idle_timeout_s() == float(_DEFAULT_IDLE_TIMEOUT_S)


def test_resolve_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "120")
    assert resolve_native_pane_idle_timeout_s() == 120.0


def test_resolve_zero_disables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_IDLE_TIMEOUT_ENV, "0")
    assert resolve_native_pane_idle_timeout_s() == 0.0


@pytest.mark.parametrize("bad", ["abc", "-5", ""])
def test_resolve_invalid_falls_back(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv(_IDLE_TIMEOUT_ENV, bad)
    assert resolve_native_pane_idle_timeout_s() == float(_DEFAULT_IDLE_TIMEOUT_S)


# ── Loop smoke (start/shutdown + disable) ───────────────────────────────────


async def test_loop_reaps_idle_pane() -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    r = _make(f, timeout=0.0001, interval=0.01)
    # Windows' event-loop clock resolution can treat 10 ms timers as ready in
    # the same tick, so seed an already-idle clock instead of relying on delay.
    r._last_busy_at["conv_a"] = time.monotonic() - 1
    await r.start()
    try:
        for _ in range(100):
            if f.reaped:
                break
            await asyncio.sleep(0.01)
    finally:
        await r.shutdown()
    assert f.reaped == ["conv_a"]


async def test_loop_disabled_when_timeout_non_positive() -> None:
    f = _Fakes()
    f.panes = [_pane("conv_a")]
    r = _make(f, timeout=0.0, interval=0.01)  # 0 disables
    await r.start()
    try:
        await asyncio.sleep(0.1)
    finally:
        await r.shutdown()
    assert f.reaped == []


def test_kimi_is_exempt_from_pane_reaping() -> None:
    # kimi records no resumable chat id, so a reaped pane cannot be re-created
    # with its context; the name filter must never offer kimi panes to the reaper.
    from omnigent.terminals.pane_reaper import NATIVE_PANE_TERMINAL_NAMES

    assert "kimi" not in NATIVE_PANE_TERMINAL_NAMES
    assert "claude" in NATIVE_PANE_TERMINAL_NAMES


async def test_runner_busy_check_spares_a_pane_parked_on_an_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The runner's busy check treats a fresh approval-wait marker as busy.

    A pane parked on a permission prompt has no active turn, no ``running``
    status, no attached client and no output, so every other signal reads idle
    and the reaper would kill the prompt under a still-answerable card.
    """
    # Bound by name when the app is built, so stub before building: no tmux here.
    monkeypatch.setattr(native_cost_popup, "_tmux_last_client_input_at", lambda *_args: None)
    monkeypatch.setattr(native_cost_popup, "_tmux_window_activity_at", lambda *_args: None)
    monkeypatch.setattr(claude_native_bridge, "_APPROVAL_WAIT_ROOT", tmp_path / "approval-waits")
    registry = TerminalRegistry()
    app = create_runner_app(
        terminal_registry=registry,
        resource_registry=SessionResourceRegistry(terminal_registry=registry),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    pane = PaneRef(
        "conv_parked", terminal_resource_id("claude", "main"), "claude", tmp_path / "tmux.sock"
    )

    assert not await reaper._is_busy(pane)

    marker = claude_native_bridge.approval_wait_marker_path("conv_parked")
    marker.parent.mkdir(parents=True)
    claude_native_bridge.touch_approval_wait_marker(marker)
    assert await reaper._is_busy(pane)
    # Another session's parked prompt does not spare this pane.
    other = PaneRef("conv_other", pane.terminal_id, "claude", pane.socket_path)
    assert not await reaper._is_busy(other)


async def test_runner_busy_check_counts_a_viewer_only_on_recent_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An attached viewer spares the pane only while a human recently drove it.

    A CLI tab's keypress (tmux ``client_activity``) or a web-bridge event (the
    terminal's interaction stamp) inside the busy window reads busy; an idle
    attached viewer, or stale input, does not — a tab left open overnight must
    not keep the native stack resident.
    """
    tmux_input_at: dict[str, float | None] = {"value": None}
    monkeypatch.setattr(
        native_cost_popup, "_tmux_last_client_input_at", lambda *_args: tmux_input_at["value"]
    )
    monkeypatch.setattr(native_cost_popup, "_tmux_window_activity_at", lambda *_args: None)
    monkeypatch.setattr(claude_native_bridge, "_APPROVAL_WAIT_ROOT", tmp_path / "approval-waits")
    registry = TerminalRegistry()
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "tmux.sock",
        private_dir=tmp_path,
        running=True,
    )
    registry._by_conversation["conv_viewed"] = {("claude", "main"): instance}
    app = create_runner_app(
        terminal_registry=registry,
        resource_registry=SessionResourceRegistry(terminal_registry=registry),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    reaper = app.state.native_pane_reaper
    assert reaper is not None
    pane = PaneRef(
        "conv_viewed", terminal_resource_id("claude", "main"), "claude", tmp_path / "tmux.sock"
    )

    # Attached but idle on both signals: not busy.
    assert not await reaper._is_busy(pane)
    # A CLI keypress inside the window spares the pane; a stale one does not.
    tmux_input_at["value"] = time.time() - 1.0
    assert await reaper._is_busy(pane)
    tmux_input_at["value"] = time.time() - PANE_OUTPUT_BUSY_WINDOW_S - 1.0
    assert not await reaper._is_busy(pane)
    # A web-bridge event on the pane's terminal spares it; a stale one does not.
    instance.note_client_interaction()
    assert await reaper._is_busy(pane)
    instance._last_client_interaction_at = time.monotonic() - PANE_OUTPUT_BUSY_WINDOW_S - 1.0
    assert not await reaper._is_busy(pane)
