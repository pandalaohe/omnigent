"""
Unit tests for :class:`omnigent.terminals.TerminalRegistry`.

Covers the registry's lifecycle invariants directly, without going
through the ``sys_terminal_*`` tools. The tools are tested separately
in ``tests/tools/builtins/test_sys_terminal.py``; these tests pin
down the registry's contract so a future tool refactor can't
silently regress the lifecycle.

All tests that need a running tmux are skipped when tmux is missing.
A handful of pure-bookkeeping tests (close/cleanup of unknown ids,
empty list, active_conversation_ids) run regardless.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import threading
from collections.abc import AsyncIterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalCreateResult, TerminalInstance
from omnigent.terminals import TerminalRegistry
from omnigent.terminals import registry as registry_mod
from omnigent.terminals.registry import TerminalListEntry, conversation_link_for_id

# ── Pure bookkeeping (no tmux) ────────────────────────────────


def test_active_conversation_ids_empty_on_fresh_registry() -> None:
    """
    A freshly-constructed registry reports no active conversations.

    What breaks if this fails: shutdown/cleanup paths that iterate
    ``active_conversation_ids()`` would surprise themselves with
    state from prior runs (registry is supposed to be a clean
    singleton at startup).
    """
    reg = TerminalRegistry()
    assert reg.active_conversation_ids() == []


def test_get_returns_none_for_unknown_triple() -> None:
    """
    ``get`` is total — never raises for unknown ids. Returns
    ``None`` so callers can use it as a presence check.
    """
    reg = TerminalRegistry()
    assert reg.get("conv_nope", "bash", "s1") is None


def test_list_for_conversation_returns_empty_for_unknown_id() -> None:
    """
    Listing a conversation that never registered terminals must
    return ``[]`` (not raise, not return ``None``). The
    ``sys_terminal_list`` tool serializes the result as JSON; an
    empty list keeps the LLM-facing shape stable.
    """
    reg = TerminalRegistry()
    assert reg.list_for_conversation("conv_nope") == []


def test_registration_snapshot_preserves_identity_and_copies_stopped_state(tmp_path: Path) -> None:
    """Owner moves retain identity; replacement and later flags cannot rewrite evidence."""
    reg = TerminalRegistry()
    assert reg.registration_snapshot() == ()
    original = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "original.sock",
        private_dir=tmp_path / "original",
        running=True,
    )
    reg._by_conversation["conv_old"] = {("claude", "main"): original}
    before = reg.registration_snapshot()
    candidates, overflow = reg.owner_uncertainty_snapshot()
    assert candidates == before and not overflow
    assert reg._uncertain_owners == {}
    assert before[0].instance_id == original.diagnostic_id
    assert not hasattr(before[0], "instance")
    with pytest.raises(FrozenInstanceError):
        before[0].conversation_id = "changed"  # type: ignore[misc]

    assert reg.transfer("conv_old", "conv_new", "claude", "main")
    original.running = False
    moved = reg.registration_snapshot()
    assert len(moved) == 1
    assert moved[0].conversation_id == "conv_new"
    assert moved[0].instance_id == before[0].instance_id
    assert (moved[0].terminal_name, moved[0].session_key) == ("claude", "main")
    assert moved[0].observed_running is False
    assert before[0].conversation_id == "conv_old"
    assert before[0].observed_running is True
    candidates, overflow = reg.owner_uncertainty_snapshot()
    assert not overflow
    assert {row.conversation_id for row in candidates} == {"conv_old", "conv_new"}
    assert {row.instance_id for row in candidates} == {original.diagnostic_id}
    assert not any(hasattr(row, "instance") for row in candidates)

    replacement = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "replacement.sock",
        private_dir=tmp_path / "replacement",
        running=True,
    )
    with reg._lock:
        reg._by_conversation["conv_new"][("claude", "main")] = replacement
    replaced = reg.registration_snapshot()
    assert replaced[0].instance_id == replacement.diagnostic_id
    assert replaced[0].instance_id != moved[0].instance_id


def test_registration_snapshot_cannot_lose_or_duplicate_a_transferred_owner(
    tmp_path: Path,
) -> None:
    """A transfer racing the full inventory always leaves exactly one owner in each sample."""
    reg = TerminalRegistry()
    instance = TerminalInstance(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "codex.sock",
        private_dir=tmp_path / "codex",
        running=True,
    )
    reg._by_conversation["conv_left"] = {("codex", "main"): instance}

    def transfer_repeatedly() -> None:
        for _ in range(500):
            reg.transfer("conv_left", "conv_right", "codex", "main")
            reg.transfer("conv_right", "conv_left", "codex", "main")

    thread = threading.Thread(target=transfer_repeatedly)
    thread.start()
    try:
        for _ in range(500):
            snapshot = reg.registration_snapshot()
            assert len(snapshot) == 1
            assert snapshot[0].instance_id == instance.diagnostic_id
            assert snapshot[0].conversation_id in {"conv_left", "conv_right"}
    finally:
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_conversation_link_for_id_uses_relative_path_by_default() -> None:
    """
    Conversation links stay relative when no Omnigent origin is known.

    This is the embedded/test-runner fallback: there is no stable
    hostname to show in the tmux status bar, but the path still points
    at the conversation route when rendered by the Omnigent web UI.
    """
    assert conversation_link_for_id("conv with/slash") == "/c/conv%20with%2Fslash"


def test_conversation_link_for_id_uses_base_url_when_provided() -> None:
    """
    Conversation links include the hostname when the runner knows one.

    This pins the out-of-process runner path: ``RUNNER_SERVER_URL`` is
    threaded into :class:`TerminalRegistry`, so managed tmux status bars
    show a complete URL instead of only ``/c/<id>``.
    """
    assert (
        conversation_link_for_id(
            "conv with/slash",
            base_url="http://127.0.0.1:8000/",
        )
        == "http://127.0.0.1:8000/c/conv%20with%2Fslash"
    )


def test_conversation_link_for_id_maps_workspace_hosted_server_to_ui_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Workspace-hosted runners link to the SPA mount, not the API mount.

    The runner threads ``RUNNER_SERVER_URL`` — the API proxy base
    (``/api/2.0/omnigent``) — into the registry. A naive
    ``{base}/c/<id>`` would put the JSON API path in the tmux status
    bar; the link must instead land on the ``/omnigent`` SPA mount and
    carry the ``?o=<org>`` selector ``omnigent login`` recorded, exactly
    like the CLI's ``Web UI:`` line. Pins parity with
    :func:`omnigent.conversation_browser.conversation_url`.

    :param tmp_path: Pytest tmp dir for the stubbed auth-token file.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.cli_auth import store_databricks_auth

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )
    server = "https://example.databricks.com/api/2.0/omnigent"
    store_databricks_auth(
        server,
        "https://example.databricks.com",
        org_id="2850744067564480",
    )

    assert (
        conversation_link_for_id("conv_abc123", base_url=server)
        == "https://example.databricks.com/omnigent/c/conv_abc123?o=2850744067564480"
    )


async def test_close_unknown_triple_returns_false() -> None:
    """
    Closing a never-launched (or already-closed) terminal returns
    ``False`` and does not raise. Idempotent close is the
    contract — workflow finally-blocks and LLM-driven cleanup
    both depend on it.
    """
    reg = TerminalRegistry()
    assert await reg.close("conv_nope", "bash", "s1") is False


async def test_cleanup_conversation_unknown_id_is_noop() -> None:
    """
    ``cleanup_conversation`` on an id with no terminals must
    return without raising — workflow finally-blocks always
    fire, even when the workflow never launched a terminal.
    """
    reg = TerminalRegistry()
    # Should not raise.
    await reg.cleanup_conversation("conv_nope")


async def test_shutdown_on_empty_registry_is_noop() -> None:
    """Shutdown of an empty registry is a no-op."""
    reg = TerminalRegistry()
    await reg.shutdown()
    assert reg.active_conversation_ids() == []


async def test_close_expected_leaves_a_successor_on_the_same_key_running(
    tmp_path: Path,
) -> None:
    """
    ``close(expected=...)`` retracts only the instance it was given.

    A creator whose work was superseded may try to retract the terminal it
    published after the key has already been reassigned to a successor built
    for the current agent. Closing by key alone would terminate that
    successor — a live terminal belonging to the session's current agent.
    Passing the original instance as *expected* makes the removal a
    compare-and-close, so the successor survives.
    """

    class _RecordingTerminal(TerminalInstance):
        closed: bool = False

        async def close(self) -> None:
            self.closed = True
            self.running = False

    reg = TerminalRegistry()
    superseded = _RecordingTerminal(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "old.sock",
        private_dir=tmp_path / "old",
        running=True,
    )
    successor = _RecordingTerminal(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "new.sock",
        private_dir=tmp_path / "new",
        running=True,
    )
    # The successor now occupies the key the superseded creator published to.
    reg._by_conversation["conv_close_expected_successor"] = {("claude", "main"): successor}
    reg._instance_locks[("conv_close_expected_successor", "claude", "main")] = threading.Lock()

    closed = await reg.close(
        "conv_close_expected_successor", "claude", "main", expected=superseded
    )

    assert closed is False, "compare-and-close must report that nothing was closed"
    assert successor.closed is False, (
        "the successor terminal belonging to the session's current agent must "
        "still be running — closing by key alone would have terminated it"
    )
    assert reg.get("conv_close_expected_successor", "claude", "main") is successor, (
        "the successor must remain registered under its key"
    )


async def test_close_expected_closes_its_own_instance(tmp_path: Path) -> None:
    """``close(expected=...)`` still closes when the key holds that instance."""

    class _RecordingTerminal(TerminalInstance):
        closed: bool = False

        async def close(self) -> None:
            self.closed = True
            self.running = False

    reg = TerminalRegistry()
    mine = _RecordingTerminal(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "mine.sock",
        private_dir=tmp_path / "mine",
        running=True,
    )
    reg._by_conversation["conv_close_expected_own"] = {("claude", "main"): mine}
    reg._instance_locks[("conv_close_expected_own", "claude", "main")] = threading.Lock()

    closed = await reg.close("conv_close_expected_own", "claude", "main", expected=mine)

    assert closed is True
    assert mine.closed is True
    assert reg.get("conv_close_expected_own", "claude", "main") is None
    (candidate,), overflow = reg.owner_uncertainty_snapshot()
    assert candidate.instance_id == mine.diagnostic_id and not overflow
    assert candidate.observed_running is True


@pytest.mark.parametrize("operation", ["cleanup", "shutdown"])
async def test_terminal_bulk_cancel_retains_all_removed_candidates(
    tmp_path, monkeypatch, operation
):
    reg = TerminalRegistry()
    instances = {
        name: TerminalInstance(
            name=name,
            session_key="main",
            socket_path=tmp_path / f"{name}.sock",
            private_dir=tmp_path / name,
            running=True,
        )
        for name in ("first", "second")
    }
    for instance in instances.values():
        monkeypatch.setattr(instance, "close", AsyncMock())
    instances["first"].close.side_effect = asyncio.CancelledError()
    reg._by_conversation["one"] = {
        (name, "main"): instance for name, instance in instances.items()
    }
    with pytest.raises(asyncio.CancelledError):
        if operation == "cleanup":
            await reg.cleanup_conversation("one")
        else:
            await reg.shutdown()
    assert reg.registration_snapshot() == ()
    candidates, overflow = reg.owner_uncertainty_snapshot()
    assert {row.instance_id for row in candidates} == {
        instance.diagnostic_id for instance in instances.values()
    }
    assert not overflow
    instances["second"].close.assert_not_awaited()


async def test_terminal_close_collision_keeps_old_candidate_and_successor(tmp_path, monkeypatch):
    reg = TerminalRegistry()
    old, new = (
        TerminalInstance(
            name="codex",
            session_key="main",
            socket_path=tmp_path / f"{name}.sock",
            private_dir=tmp_path / name,
            running=True,
        )
        for name in ("old", "new")
    )
    entered, resume = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await resume.wait()
        raise RuntimeError("close unconfirmed")

    monkeypatch.setattr(old, "close", close)
    reg._by_conversation["one"] = {("codex", "main"): old}
    task = asyncio.create_task(reg.close("one", "codex", "main", expected=old))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert reg.registration_snapshot() == ()
        assert reg.owner_uncertainty_snapshot()[0][0].instance_id == old.diagnostic_id
        reg._by_conversation["one"] = {("codex", "main"): new}
        resume.set()
        with pytest.raises(RuntimeError, match="close unconfirmed"):
            await asyncio.wait_for(task, 2)
        assert reg.get("one", "codex", "main") is new
        candidates, overflow = reg.owner_uncertainty_snapshot()
        assert {row.instance_id for row in candidates} == {old.diagnostic_id, new.diagnostic_id}
        assert not overflow
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_terminal_uncertainty_budget_never_becomes_empty_proof(tmp_path, monkeypatch):
    reg = TerminalRegistry()
    monkeypatch.setattr(registry_mod, "_MAX_UNCERTAIN_TERMINAL_OWNERS", 1)
    instance = TerminalInstance(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "term.sock",
        private_dir=tmp_path / "term",
        running=True,
    )
    reg._by_conversation["old"] = {("codex", "main"): instance}
    assert reg.transfer("old", "new", "codex", "main")
    candidates, overflow = reg.owner_uncertainty_snapshot()
    assert len(candidates) == 1 and candidates[0].conversation_id == "old"
    assert candidates[0].instance_id == instance.diagnostic_id and overflow
    assert reg.get("new", "codex", "main") is instance
    assert reg.owner_uncertainty_snapshot() == (candidates, overflow)


async def test_terminal_birth_candidate_precedes_launch_await(tmp_path, monkeypatch):
    reg = TerminalRegistry()
    instance = TerminalInstance(
        name="shell",
        session_key="main",
        socket_path=tmp_path / "term.sock",
        private_dir=tmp_path / "term",
        running=False,
    )
    entered, resume = asyncio.Event(), asyncio.Event()

    async def launch(**_kwargs):
        entered.set()
        await resume.wait()
        instance.running = True

    monkeypatch.setattr(instance, "launch", launch)
    monkeypatch.setattr(instance, "is_alive", AsyncMock(return_value=True))
    monkeypatch.setattr(instance, "server_pid", AsyncMock(return_value=None))
    monkeypatch.setattr(instance, "close", AsyncMock())
    monkeypatch.setattr(
        registry_mod,
        "create_terminal_instance",
        lambda *_args, **_kwargs: TerminalCreateResult(instance=instance, cwd=tmp_path),
    )
    task = asyncio.create_task(reg.launch("one", "shell", "main", TerminalEnvSpec(command="bash")))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert reg.registration_snapshot() == ()
        (candidate,), overflow = reg.owner_uncertainty_snapshot()
        assert candidate.instance_id == instance.diagnostic_id and not overflow
        assert candidate.observed_running is False
        before = dict(reg._uncertain_owners)
        assert reg.owner_uncertainty_snapshot() == ((candidate,), False)
        assert reg._uncertain_owners == before
        instance.is_alive.assert_not_awaited()
        instance.server_pid.assert_not_awaited()
        resume.set()
        assert await asyncio.wait_for(task, 2) is instance
        assert reg.registration_snapshot()[0].instance_id == candidate.instance_id
    finally:
        resume.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await reg.shutdown()


@pytest.mark.asyncio
async def test_launch_replaces_stale_running_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``launch`` verifies a cached running entry before returning it."""

    class _FlagTerminal(TerminalInstance):
        """Terminal instance whose liveness is controlled by a flag."""

        alive: bool = True
        closed: bool = False

        async def launch(self, *, cwd: Path | None = None) -> None:
            del cwd
            self.running = True

        async def is_alive(self) -> bool:
            if not self.alive:
                self.running = False
            return self.alive

        async def close(self) -> None:
            self.closed = True
            self.running = False

    reg = TerminalRegistry()
    stale = _FlagTerminal(
        name="shell",
        session_key="s1",
        socket_path=tmp_path / "stale.sock",
        private_dir=tmp_path / "stale",
        running=True,
    )
    stale.alive = False
    created = _FlagTerminal(
        name="shell",
        session_key="s1",
        socket_path=tmp_path / "created.sock",
        private_dir=tmp_path / "created",
        running=False,
    )
    reg._by_conversation["conv_x"] = {("shell", "s1"): stale}
    reg._instance_locks[("conv_x", "shell", "s1")] = threading.Lock()

    def _fake_create_terminal_instance(*_args: object, **_kwargs: object) -> TerminalCreateResult:
        return TerminalCreateResult(instance=created, cwd=tmp_path)

    monkeypatch.setattr(registry_mod, "create_terminal_instance", _fake_create_terminal_instance)

    result = await reg.launch(
        "conv_x",
        "shell",
        "s1",
        TerminalEnvSpec(command="bash"),
    )

    assert result is created
    assert stale.closed is True
    assert reg.get("conv_x", "shell", "s1") is created
    assert created.lifecycle_trace.session_id == "conv_x"


def test_transfer_moves_terminal_without_closing_tmux(tmp_path: Path) -> None:
    """
    Terminal transfer changes ownership without touching the instance.

    This is the load-bearing invariant for native Claude ``/clear``:
    moving ``claude/main`` from the old Omnigent conversation to the fresh
    one must not call ``close()``, because closing kills the tmux pane
    that still contains the live Claude process.
    """
    reg = TerminalRegistry()
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "claude.sock",
        private_dir=tmp_path / "claude",
        running=True,
    )
    lock = threading.Lock()
    reg._by_conversation["conv_old"] = {("claude", "main"): instance}
    reg._instance_locks[("conv_old", "claude", "main")] = lock

    moved = reg.transfer("conv_old", "conv_new", "claude", "main")

    assert moved is True
    assert reg.get("conv_old", "claude", "main") is None
    assert reg.get("conv_new", "claude", "main") is instance
    assert instance.running is True
    assert reg.get_instance_lock("conv_old", "claude", "main") is None
    assert reg.get_instance_lock("conv_new", "claude", "main") is lock
    assert reg.active_conversation_ids() == ["conv_new"]


def test_transfer_rejects_target_collision_without_moving_source(tmp_path: Path) -> None:
    """
    Transfer refuses to overwrite an existing target terminal.

    A collision means two live panes would claim the same
    ``(session, terminal, key)`` resource. The registry must fail loud
    and leave both original owners intact.
    """
    reg = TerminalRegistry()
    source = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "source.sock",
        private_dir=tmp_path / "source",
        running=True,
    )
    target = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "target.sock",
        private_dir=tmp_path / "target",
        running=True,
    )
    reg._by_conversation["conv_old"] = {("claude", "main"): source}
    reg._by_conversation["conv_new"] = {("claude", "main"): target}

    with pytest.raises(RuntimeError, match="already exists"):
        reg.transfer("conv_old", "conv_new", "claude", "main")

    assert reg.get("conv_old", "claude", "main") is source
    assert reg.get("conv_new", "claude", "main") is target


# ── Real-tmux lifecycle tests ─────────────────────────────────


async def test_failed_launch_cleanup_retains_ownership_without_replacing_racer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg = TerminalRegistry()
    created = TerminalInstance(
        name="cli",
        session_key="main",
        socket_path=tmp_path / "old.sock",
        private_dir=tmp_path / "old",
    )
    racer = TerminalInstance(
        name="cli",
        session_key="main",
        socket_path=tmp_path / "new.sock",
        private_dir=tmp_path / "new",
        running=True,
    )
    racer_lock = threading.Lock()

    async def cancelled_launch(**_kwargs: object) -> None:
        reg._by_conversation["target"] = {("cli", "main"): racer}
        reg._instance_locks[("target", "cli", "main")] = racer_lock
        raise asyncio.CancelledError

    created.launch = cancelled_launch  # type: ignore[method-assign]
    created.close = AsyncMock(side_effect=RuntimeError("cleanup unavailable"))  # type: ignore[method-assign]
    monkeypatch.setattr(
        registry_mod,
        "create_terminal_instance",
        lambda *_args, **_kwargs: TerminalCreateResult(instance=created, cwd=tmp_path),
    )
    with pytest.raises(asyncio.CancelledError):
        await reg.launch("target", "cli", "main", TerminalEnvSpec(command="python"))
    assert reg.get("target", "cli", "main") is racer
    assert reg.get_instance_lock("target", "cli", "main") is racer_lock
    retained = [
        entry for entry in reg.list_for_conversation("target") if entry.instance is created
    ]
    assert len(retained) == 1
    created.close = AsyncMock()  # type: ignore[method-assign]
    assert await reg.close("target", retained[0].terminal_name, retained[0].session_key)
    assert reg.get("target", "cli", "main") is racer


pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux not installed; registry lifecycle tests need a real tmux on PATH",
)


@pytest.fixture
def bash_spec(tmp_path: Path) -> TerminalEnvSpec:
    """A minimal :class:`TerminalEnvSpec` with sandbox=none anchored at tmp_path.

    :param tmp_path: Pytest's tmpdir — the working directory for the
        spawned tmux. Sandbox is forced off so the test doesn't depend
        on bwrap / macOS sandbox availability.
    :returns: A :class:`TerminalEnvSpec` ready to launch.
    """
    return TerminalEnvSpec(
        command="bash",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


@pytest.fixture
async def cleanup(reg_with_tmux: TerminalRegistry) -> AsyncIterator[None]:
    """
    Tear down every terminal at test exit. Must come AFTER the
    registry fixture so its teardown runs before pytest discards
    the registry.

    :param reg_with_tmux: The registry fixture.
    :yields: ``None`` — value isn't consumed.
    """
    yield
    await reg_with_tmux.shutdown()


@pytest.fixture
def reg_with_tmux() -> TerminalRegistry:
    """Fresh registry for each tmux-backed test."""
    return TerminalRegistry()


@pytestmark_tmux
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_cancel_unregistered_launch_cleans_only_its_process(
    reg_with_tmux: TerminalRegistry,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_fails: bool,
) -> None:
    import psutil

    reg = reg_with_tmux
    spec = TerminalEnvSpec(
        command=sys.executable,
        args=["-c", "import time; time.sleep(60)"],
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
    )
    sibling = await reg.launch("sibling", "cli", "main", spec)
    original_launch = TerminalInstance.launch
    original_close = TerminalInstance.close
    ready = asyncio.Event()
    target: TerminalInstance | None = None
    process: psutil.Process | None = None
    fail_once = cleanup_fails

    async def park_before_registration(instance: TerminalInstance, **kwargs: object) -> None:
        nonlocal target, process
        await original_launch(instance, **kwargs)
        target = instance
        pid = await asyncio.to_thread(instance.pane_pid_sync)
        assert pid is not None
        process = psutil.Process(pid)
        # Launch can be cancelled before its advisory running flag is set.
        instance.running = False
        ready.set()
        await asyncio.Event().wait()

    async def close_with_failure(instance: TerminalInstance) -> None:
        nonlocal fail_once
        if instance is target and fail_once:
            fail_once = False
            raise RuntimeError("cleanup temporarily unavailable")
        await original_close(instance)

    monkeypatch.setattr(TerminalInstance, "launch", park_before_registration)
    monkeypatch.setattr(TerminalInstance, "close", close_with_failure)
    task = asyncio.create_task(reg.launch("target", "cli", "main", spec))
    try:
        await asyncio.wait_for(ready.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert target is not None and process is not None
        if cleanup_fails:
            assert reg.get("target", "cli", "main") is target
            assert reg.get_instance_lock("target", "cli", "main") is not None
            assert await reg.close("target", "cli", "main")
        await asyncio.to_thread(process.wait, timeout=2)
        assert not process.is_running()
        assert reg.get("target", "cli", "main") is None
        assert await sibling.is_alive()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if target is not None:
            await original_close(target)
        await reg.shutdown()


@pytestmark_tmux
async def test_launch_get_close_round_trip(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
    cleanup: None,
) -> None:
    """
    ``launch`` registers the instance, ``get`` finds it, ``close``
    removes it. The most basic happy path through the registry.
    """
    del cleanup  # consumed for teardown side-effect
    instance = await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    assert instance.running, (
        "Instance should be running after launch; if not, tmux spawn failed silently."
    )
    # Same instance is returned on get.
    assert reg_with_tmux.get("conv_a", "bash", "s1") is instance

    closed = await reg_with_tmux.close("conv_a", "bash", "s1")
    assert closed is True, (
        "Close should return True for a live instance. False "
        "would mean the registry forgot the entry between "
        "launch and close."
    )
    # And the entry is gone.
    assert reg_with_tmux.get("conv_a", "bash", "s1") is None


@pytestmark_tmux
async def test_launch_idempotent_returns_existing_instance(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
    cleanup: None,
) -> None:
    """
    Launching the same triple twice returns the SAME instance
    (no second tmux spawned). Required so the LLM's retries on
    transient errors don't leak orphan tmux sessions.
    """
    del cleanup
    first = await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    second = await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    assert first is second, (
        "Idempotent launch must return the existing instance. If "
        "first is not second, the registry is spawning a fresh "
        "tmux on every launch — a leak."
    )


@pytestmark_tmux
async def test_distinct_session_keys_get_distinct_instances(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
    cleanup: None,
) -> None:
    """
    Two different session_keys for the same terminal name produce
    two independent :class:`TerminalInstance` objects with distinct
    sockets. Validates the (name, session_key) keying.
    """
    del cleanup
    s1 = await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    s2 = await reg_with_tmux.launch("conv_a", "bash", "s2", bash_spec)
    assert s1 is not s2
    assert s1.socket_path != s2.socket_path, (
        "Distinct session keys must spawn distinct tmux sockets. "
        "If they match, the registry collapsed (name, key) to "
        "name-only."
    )


@pytestmark_tmux
async def test_distinct_conversations_isolated(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
    cleanup: None,
) -> None:
    """
    Two conversations launching ``bash:s1`` get two distinct
    instances. Conversations must not share terminal state.
    """
    del cleanup
    inst_a = await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    inst_b = await reg_with_tmux.launch("conv_b", "bash", "s1", bash_spec)
    assert inst_a is not inst_b
    # Cross-conversation get returns None — no leakage.
    assert reg_with_tmux.get("conv_a", "bash", "s1") is inst_a
    assert reg_with_tmux.get("conv_b", "bash", "s1") is inst_b


@pytestmark_tmux
async def test_list_for_conversation_returns_only_owners_terminals(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
    cleanup: None,
) -> None:
    """
    ``list_for_conversation`` returns only the requested
    conversation's terminals — no leakage of other conversations'
    sessions.
    """
    del cleanup
    await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    await reg_with_tmux.launch("conv_a", "bash", "s2", bash_spec)
    await reg_with_tmux.launch("conv_b", "bash", "s1", bash_spec)

    listed_a = reg_with_tmux.list_for_conversation("conv_a")
    listed_b = reg_with_tmux.list_for_conversation("conv_b")
    a_keys = {(e.terminal_name, e.session_key) for e in listed_a}
    b_keys = {(e.terminal_name, e.session_key) for e in listed_b}
    assert a_keys == {("bash", "s1"), ("bash", "s2")}
    assert b_keys == {("bash", "s1")}


@pytestmark_tmux
async def test_cleanup_conversation_closes_all_owners_terminals(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
) -> None:
    """
    ``cleanup_conversation`` closes every terminal owned by the
    conversation and drops the per-conversation slot. Other
    conversations' terminals are untouched.

    What breaks if this fails: workflow exit doesn't release tmux
    sessions; orphans accumulate across the Omnigent process lifetime.
    """
    await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    await reg_with_tmux.launch("conv_a", "bash", "s2", bash_spec)
    inst_b = await reg_with_tmux.launch("conv_b", "bash", "s1", bash_spec)

    await reg_with_tmux.cleanup_conversation("conv_a")

    # conv_a slot is gone — both entries cleared.
    assert reg_with_tmux.list_for_conversation("conv_a") == []
    assert reg_with_tmux.get("conv_a", "bash", "s1") is None
    assert reg_with_tmux.get("conv_a", "bash", "s2") is None

    # conv_b is untouched — isolation invariant.
    assert reg_with_tmux.get("conv_b", "bash", "s1") is inst_b

    # Cleanup the surviving instance.
    await reg_with_tmux.cleanup_conversation("conv_b")


@pytestmark_tmux
async def test_close_after_cleanup_returns_false(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
) -> None:
    """
    Closing a terminal that ``cleanup_conversation`` already
    removed returns ``False`` — no double-close errors.
    """
    await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    await reg_with_tmux.cleanup_conversation("conv_a")
    # Subsequent close on the now-cleared entry: idempotent False.
    assert await reg_with_tmux.close("conv_a", "bash", "s1") is False


@pytestmark_tmux
async def test_shutdown_clears_all_conversations(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
) -> None:
    """
    ``shutdown`` closes every terminal in every conversation and
    leaves the registry empty. Drives the FastAPI lifespan
    shutdown path.
    """
    await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    await reg_with_tmux.launch("conv_b", "bash", "s1", bash_spec)
    assert sorted(reg_with_tmux.active_conversation_ids()) == ["conv_a", "conv_b"]

    await reg_with_tmux.shutdown()
    assert reg_with_tmux.active_conversation_ids() == []
    # Confirm the per-conversation slot is also gone (not just
    # emptied with a stale key).
    assert reg_with_tmux.list_for_conversation("conv_a") == []
    assert reg_with_tmux.list_for_conversation("conv_b") == []


# ── Additional pure-bookkeeping tests (no tmux) ─────────────


def test_get_instance_lock_returns_none_for_unknown_triple() -> None:
    """
    ``get_instance_lock`` returns ``None`` for a triple that was never
    registered. Callers treat ``None`` as "not running" and surface
    an error to the LLM.
    """
    reg = TerminalRegistry()
    assert reg.get_instance_lock("conv_nope", "bash", "s1") is None


def test_get_instance_lock_returns_lock_after_manual_registration(tmp_path: Path) -> None:
    """
    After manually inserting an instance and its lock (as ``launch``
    would), ``get_instance_lock`` returns the same lock object.
    """
    reg = TerminalRegistry()
    lock = threading.Lock()
    instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "bash.sock",
        private_dir=tmp_path / "bash",
        running=True,
    )
    reg._by_conversation["conv_a"] = {("bash", "s1"): instance}
    reg._instance_locks[("conv_a", "bash", "s1")] = lock

    assert reg.get_instance_lock("conv_a", "bash", "s1") is lock


def test_transfer_nonexistent_source_returns_false() -> None:
    """
    Transferring from a conversation that has no terminals returns
    ``False`` without raising. This is the expected path when the
    source conversation was already cleaned up.
    """
    reg = TerminalRegistry()
    assert reg.transfer("no_such_conv", "target_conv", "bash", "s1") is False


def test_transfer_nonexistent_terminal_in_source_returns_false(tmp_path: Path) -> None:
    """
    Transferring a specific terminal that doesn't exist in the source
    conversation returns ``False``. The source conversation itself
    exists but the named terminal does not.
    """
    reg = TerminalRegistry()
    instance = TerminalInstance(
        name="other",
        session_key="s1",
        socket_path=tmp_path / "other.sock",
        private_dir=tmp_path / "other",
        running=True,
    )
    reg._by_conversation["conv_a"] = {("other", "s1"): instance}

    assert reg.transfer("conv_a", "conv_b", "bash", "s1") is False


def test_transfer_cleans_up_empty_source_slot(tmp_path: Path) -> None:
    """
    After transferring the last terminal from a conversation, the
    source conversation's slot is removed from ``_by_conversation``
    entirely — not left as an empty dict.
    """
    reg = TerminalRegistry()
    instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "bash.sock",
        private_dir=tmp_path / "bash",
        running=True,
    )
    lock = threading.Lock()
    reg._by_conversation["conv_old"] = {("bash", "s1"): instance}
    reg._instance_locks[("conv_old", "bash", "s1")] = lock

    reg.transfer("conv_old", "conv_new", "bash", "s1")

    assert "conv_old" not in reg._by_conversation
    assert reg.active_conversation_ids() == ["conv_new"]


def test_conversation_link_for_id_treats_whitespace_only_as_no_base() -> None:
    """
    A base_url of whitespace-only is treated the same as ``None`` —
    falls back to a relative path. This guards against config entries
    that are accidentally blank.
    """
    assert conversation_link_for_id("conv_x", base_url="   ") == "/c/conv_x"


def test_list_for_conversation_returns_terminal_list_entries(tmp_path: Path) -> None:
    """
    ``list_for_conversation`` returns :class:`TerminalListEntry`
    dataclass instances with the correct fields populated.
    """
    reg = TerminalRegistry()
    inst = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "bash.sock",
        private_dir=tmp_path / "bash",
        running=True,
    )
    reg._by_conversation["conv_a"] = {("bash", "s1"): inst}

    entries = reg.list_for_conversation("conv_a")
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, TerminalListEntry)
    assert entry.terminal_name == "bash"
    assert entry.session_key == "s1"
    assert entry.instance is inst


def test_list_for_conversation_snapshot_isolation(tmp_path: Path) -> None:
    """
    The list returned by ``list_for_conversation`` is a snapshot:
    mutating the returned list does not affect the registry's internal
    state.
    """
    reg = TerminalRegistry()
    inst = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "bash.sock",
        private_dir=tmp_path / "bash",
        running=True,
    )
    reg._by_conversation["conv_a"] = {("bash", "s1"): inst}

    entries = reg.list_for_conversation("conv_a")
    entries.clear()  # mutate the returned list

    # Internal state should be unchanged.
    assert len(reg.list_for_conversation("conv_a")) == 1


def test_conversation_link_for_id_method_delegates_to_module_function() -> None:
    """
    The instance method ``TerminalRegistry.conversation_link_for_id``
    delegates to the module-level function with the registry's
    configured base URL.
    """
    reg = TerminalRegistry(conversation_link_base_url=None)
    assert reg.conversation_link_for_id("conv_abc") == "/c/conv_abc"

    reg2 = TerminalRegistry(conversation_link_base_url="http://localhost:6767")
    link = reg2.conversation_link_for_id("conv_abc")
    assert link.startswith("http://localhost:6767")
    assert "conv_abc" in link


async def test_close_removes_instance_lock(tmp_path: Path) -> None:
    """
    After ``close``, the per-instance lock is removed so
    ``get_instance_lock`` returns ``None``. Subsequent tool calls
    that try to acquire the lock see "not running" rather than
    operating on a closed tmux session.
    """
    reg = TerminalRegistry()
    instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "bash.sock",
        private_dir=tmp_path / "bash",
        running=True,
    )
    instance.close = AsyncMock()  # type: ignore[method-assign]
    lock = threading.Lock()
    reg._by_conversation["conv_a"] = {("bash", "s1"): instance}
    reg._instance_locks[("conv_a", "bash", "s1")] = lock

    result = await reg.close("conv_a", "bash", "s1")

    assert result is True
    assert reg.get_instance_lock("conv_a", "bash", "s1") is None
    instance.close.assert_awaited_once()


async def test_close_timeout_keeps_exact_instance_retryable(tmp_path: Path) -> None:
    """
    If ``instance.close()`` times out, ``close`` returns ``False`` and
    restores the exact instance and lock so durable cleanup can retry it.
    """
    reg = TerminalRegistry()
    instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "bash.sock",
        private_dir=tmp_path / "bash",
        running=True,
    )

    async def _hang_forever() -> None:
        await asyncio.sleep(999)

    instance.close = _hang_forever  # type: ignore[method-assign]
    reg._by_conversation["conv_a"] = {("bash", "s1"): instance}
    lock = threading.Lock()
    reg._instance_locks[("conv_a", "bash", "s1")] = lock

    # The close should not hang — it uses asyncio.wait_for with _CLOSE_TIMEOUT_S.
    # We patch _CLOSE_TIMEOUT_S to a tiny value so the test finishes quickly.
    import omnigent.terminals.registry as reg_mod

    original = reg_mod._CLOSE_TIMEOUT_S
    reg_mod._CLOSE_TIMEOUT_S = 0.01
    try:
        result = await reg.close("conv_a", "bash", "s1")
    finally:
        reg_mod._CLOSE_TIMEOUT_S = original

    assert result is False
    assert reg.get("conv_a", "bash", "s1") is instance
    assert reg.get_instance_lock("conv_a", "bash", "s1") is lock


async def test_cleanup_conversation_tolerates_close_exception(tmp_path: Path) -> None:
    """
    ``cleanup_conversation`` swallows exceptions from individual
    ``instance.close()`` calls and continues closing the remaining
    terminals. This ensures a wedged terminal cannot block cleanup
    of its siblings.
    """
    reg = TerminalRegistry()
    good_instance = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "good.sock",
        private_dir=tmp_path / "good",
        running=True,
    )
    good_instance.close = AsyncMock()  # type: ignore[method-assign]
    bad_instance = TerminalInstance(
        name="bash",
        session_key="s2",
        socket_path=tmp_path / "bad.sock",
        private_dir=tmp_path / "bad",
        running=True,
    )

    async def _explode() -> None:
        raise RuntimeError("tmux gone")

    bad_instance.close = _explode  # type: ignore[method-assign]

    reg._by_conversation["conv_a"] = {
        ("bash", "s1"): good_instance,
        ("bash", "s2"): bad_instance,
    }
    reg._instance_locks[("conv_a", "bash", "s1")] = threading.Lock()
    reg._instance_locks[("conv_a", "bash", "s2")] = threading.Lock()

    # Should not raise despite bad_instance.close() exploding.
    await reg.cleanup_conversation("conv_a")

    assert reg.active_conversation_ids() == []
    # The good instance's close was still called.
    good_instance.close.assert_awaited_once()


async def test_shutdown_tolerates_close_exception(tmp_path: Path) -> None:
    """
    ``shutdown`` swallows exceptions from individual ``instance.close()``
    calls, just like ``cleanup_conversation``. A stuck instance in one
    conversation must not prevent cleanup of instances in other
    conversations.
    """
    reg = TerminalRegistry()
    good = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "good.sock",
        private_dir=tmp_path / "good",
        running=True,
    )
    good.close = AsyncMock()  # type: ignore[method-assign]
    bad = TerminalInstance(
        name="bash",
        session_key="s1",
        socket_path=tmp_path / "bad.sock",
        private_dir=tmp_path / "bad",
        running=True,
    )

    async def _explode() -> None:
        raise RuntimeError("tmux gone")

    bad.close = _explode  # type: ignore[method-assign]

    reg._by_conversation["conv_a"] = {("bash", "s1"): bad}
    reg._by_conversation["conv_b"] = {("bash", "s1"): good}
    reg._instance_locks[("conv_a", "bash", "s1")] = threading.Lock()
    reg._instance_locks[("conv_b", "bash", "s1")] = threading.Lock()

    await reg.shutdown()

    assert reg.active_conversation_ids() == []
    good.close.assert_awaited_once()


def test_multiple_terminals_per_conversation(tmp_path: Path) -> None:
    """
    A single conversation can hold multiple terminals with different
    names and session keys. ``list_for_conversation`` returns all of
    them and ``get`` retrieves each individually.
    """
    reg = TerminalRegistry()
    instances = {}
    for name, key in [("bash", "s1"), ("bash", "s2"), ("python", "s1")]:
        inst = TerminalInstance(
            name=name,
            session_key=key,
            socket_path=tmp_path / f"{name}_{key}.sock",
            private_dir=tmp_path / f"{name}_{key}",
            running=True,
        )
        instances[(name, key)] = inst

    reg._by_conversation["conv_a"] = dict(instances)

    entries = reg.list_for_conversation("conv_a")
    assert len(entries) == 3
    entry_keys = {(e.terminal_name, e.session_key) for e in entries}
    assert entry_keys == {("bash", "s1"), ("bash", "s2"), ("python", "s1")}

    for (name, key), inst in instances.items():
        assert reg.get("conv_a", name, key) is inst
