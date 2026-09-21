"""Session runtime lifecycle generation and lock contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnigent.runner.session_runtime_lifecycle import (
    SessionRuntimeLifecycle,
    SessionRuntimeLock,
)


@pytest.mark.asyncio
async def test_two_runtime_users_hold_the_session_at_the_same_time() -> None:
    """A handshake must not queue behind a turn: both only USE the runtime."""
    lock = SessionRuntimeLock()
    both_inside = asyncio.Event()
    inside = 0

    async def user() -> None:
        nonlocal inside
        async with lock.shared():
            inside += 1
            if inside == 2:
                both_inside.set()
            await asyncio.wait_for(both_inside.wait(), timeout=1)

    await asyncio.gather(user(), user())
    assert not lock.locked()


@pytest.mark.asyncio
async def test_reclaim_waits_for_every_runtime_user_to_leave() -> None:
    """Teardown destroys the runtime, so no user may still be mid-flight."""
    lock = SessionRuntimeLock()
    allow_release = asyncio.Event()
    order: list[str] = []

    async def user(tag: str) -> None:
        async with lock.shared():
            order.append(tag)
            await allow_release.wait()

    async def reclaim() -> None:
        async with lock.exclusive():
            order.append("reclaim")

    users = [asyncio.create_task(user("a")), asyncio.create_task(user("b"))]
    await asyncio.sleep(0)
    reclaim_task = asyncio.create_task(reclaim())
    await asyncio.sleep(0.05)
    assert order == ["a", "b"], "reclaim ran while the runtime was still in use"

    allow_release.set()
    await asyncio.gather(*users, reclaim_task)
    assert order == ["a", "b", "reclaim"]
    assert not lock.locked()


@pytest.mark.asyncio
async def test_a_waiting_reclaim_is_not_starved_by_a_later_user() -> None:
    """Grants follow arrival order, so back-to-back turns cannot hold off an
    archive teardown indefinitely."""
    lock = SessionRuntimeLock()
    allow_release = asyncio.Event()
    order: list[str] = []

    async def first_user() -> None:
        async with lock.shared():
            order.append("first")
            await allow_release.wait()

    async def reclaim() -> None:
        async with lock.exclusive():
            order.append("reclaim")

    async def late_user() -> None:
        async with lock.shared():
            order.append("late")

    first = asyncio.create_task(first_user())
    await asyncio.sleep(0.01)
    reclaim_task = asyncio.create_task(reclaim())
    await asyncio.sleep(0.01)
    late = asyncio.create_task(late_user())
    await asyncio.sleep(0.01)

    allow_release.set()
    await asyncio.gather(first, reclaim_task, late)
    assert order == ["first", "reclaim", "late"]
    assert not lock.locked()


@pytest.mark.asyncio
async def test_a_cancelled_turn_hands_the_runtime_back() -> None:
    """Archive teardown reclaims by cancelling the turn, so the release path
    has to survive cancellation at every point it can arrive."""
    lock = SessionRuntimeLock()
    inside = asyncio.Event()

    async def holder() -> None:
        async with lock.shared():
            inside.set()
            await asyncio.sleep(10)

    held = asyncio.create_task(holder())
    await asyncio.wait_for(inside.wait(), timeout=1)
    held.cancel()
    with pytest.raises(asyncio.CancelledError):
        await held
    assert not lock.locked(), "a cancelled holder left the runtime held"

    # Cancelled while queued, before any grant.
    async with lock.exclusive():
        queued = asyncio.create_task(holder())
        await asyncio.sleep(0.01)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
    assert not lock.locked(), "a cancelled waiter left the runtime held"

    # Cancelled after the grant landed but before the task ever resumed: the
    # hold is already taken on its behalf, and only _acquire can give it back.
    await lock._acquire(exclusive=True)
    granted = asyncio.create_task(holder())
    await asyncio.sleep(0.01)
    lock._release(exclusive=True)
    assert lock._shared_holders == 1, "the grant never landed; the case is untested"
    granted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await granted
    assert not lock.locked(), "a grant landed on a cancelled waiter and stuck"
    async with lock.exclusive():
        pass


def test_runtime_token_is_scoped_to_runner_boot() -> None:
    first = SessionRuntimeLifecycle(boot_id="boot-a")
    second = SessionRuntimeLifecycle(boot_id="boot-b")

    assert first.runtime_token("session") == "boot-a:0"
    assert second.runtime_token("session") == "boot-b:0"
    assert first.runtime_token("session") != second.runtime_token("session")


def test_policy_revision_scope_resets_when_session_moves_hosts() -> None:
    lifecycle = SessionRuntimeLifecycle(boot_id="boot")
    lifecycle.observe_policy("session", host_id="host-a", revision=10)
    lifecycle.observe_policy("session", host_id="host-b", revision=2)

    assert lifecycle.policy_matches("session", host_id="host-b", revision=2)
    assert not lifecycle.policy_matches("session", host_id="host-a", revision=10)


def test_reclaim_advances_generation_and_rejects_stale_token() -> None:
    lifecycle = SessionRuntimeLifecycle(boot_id="boot")
    old = lifecycle.runtime_token("session")

    assert lifecycle.claim_reclaim("session", expected_runtime_token=old)
    lifecycle.finish_reclaim("session")

    assert lifecycle.runtime_token("session") == "boot:1"
    assert not lifecycle.claim_reclaim("session", expected_runtime_token=old)


def test_runtime_start_invalidates_an_older_idle_snapshot() -> None:
    lifecycle = SessionRuntimeLifecycle(boot_id="boot")
    old = lifecycle.runtime_token("session")

    lifecycle.mark_starting("session")
    lifecycle.mark_live("session")

    assert lifecycle.runtime_token("session") == "boot:1"
    assert not lifecycle.claim_reclaim("session", expected_runtime_token=old)


def test_newer_unarchive_clears_fence_and_stale_archive_cannot_restore_it() -> None:
    lifecycle = SessionRuntimeLifecycle(boot_id="boot")
    before_archive = lifecycle.runtime_token("session")

    assert lifecycle.observe_archive_state("session", scope_id="root", revision=3, archived=True)
    assert not lifecycle.runtime_start_allowed("session")
    assert not lifecycle.runtime_token_matches("session", before_archive)
    assert lifecycle.observe_archive_state("session", scope_id="root", revision=4, archived=False)
    assert lifecycle.runtime_start_allowed("session")

    assert not lifecycle.observe_archive_state(
        "session", scope_id="root", revision=3, archived=True
    )
    assert lifecycle.runtime_start_allowed("session")


def test_archive_revisions_are_scoped_by_archived_root() -> None:
    lifecycle = SessionRuntimeLifecycle(boot_id="boot")
    assert lifecycle.observe_archive_state("child", scope_id="child", revision=2, archived=False)

    assert lifecycle.observe_archive_state("child", scope_id="parent", revision=1, archived=True)
    assert lifecycle.archive_fence_matches("child", scope_id="parent", revision=1)
    assert not lifecycle.runtime_start_allowed("child")


@pytest.mark.asyncio
async def test_release_first_blocks_runtime_start_until_cleanup_finishes() -> None:
    lifecycle = SessionRuntimeLifecycle(boot_id="boot")
    lock = lifecycle.lock_for("session")
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    runtime_started = asyncio.Event()

    async def release() -> None:
        # Reclaim replaces the runtime, so it is the exclusive side.
        async with lock.exclusive():
            release_started.set()
            assert lifecycle.claim_reclaim("session")
            await allow_release.wait()
            lifecycle.finish_reclaim("session")

    async def start() -> None:
        await release_started.wait()
        async with lock.shared():
            lifecycle.mark_live("session")
            runtime_started.set()

    release_task = asyncio.create_task(release())
    start_task = asyncio.create_task(start())
    await release_started.wait()
    await asyncio.sleep(0)
    assert not runtime_started.is_set()

    allow_release.set()
    await asyncio.gather(release_task, start_task)
    assert runtime_started.is_set()
    assert lifecycle.phase("session") == "live"


@pytest.mark.asyncio
async def test_late_codex_forwarder_closes_only_its_app_server(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from omnigent.runner import native as native_runtime
    from omnigent.runner.native import orchestration

    class _Server:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    async def _forwarder(**_kwargs) -> None:
        return None

    old = _Server()
    new = _Server()
    session_id = "a1b2c3d4e5f61234567890abcdef0123"
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://server.test")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.forwarder.supervise_forwarder",
        _forwarder,
    )
    native_runtime._AUTO_CODEX_APP_SERVERS[session_id] = new  # type: ignore[assignment]
    try:
        await orchestration._codex_forward_known_thread(
            session_id=session_id,
            bridge_dir=tmp_path,
            codex_ws_url="ws://127.0.0.1:1",
            thread_id="thread-old",
            owned_app_server=old,  # type: ignore[arg-type]
        )
        assert native_runtime._AUTO_CODEX_APP_SERVERS[session_id] is new
        assert old.closed is True
        assert new.closed is False
    finally:
        native_runtime._AUTO_CODEX_APP_SERVERS.pop(session_id, None)


@pytest.mark.asyncio
async def test_late_opencode_forwarder_closes_only_its_server() -> None:
    from omnigent.runner.native import orchestration

    class _Server:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class _Forwarder:
        async def run(self) -> None:
            return None

    old = _Server()
    new = _Server()
    session_id = "b1b2c3d4e5f61234567890abcdef0123"
    orchestration._AUTO_OPENCODE_SERVERS[session_id] = new  # type: ignore[assignment]
    try:
        await orchestration._supervise_opencode_forwarder(
            session_id,
            old,  # type: ignore[arg-type]
            _Forwarder(),  # type: ignore[arg-type]
        )
        assert orchestration._AUTO_OPENCODE_SERVERS[session_id] is new
        assert old.closed is True
        assert new.closed is False
    finally:
        orchestration._AUTO_OPENCODE_SERVERS.pop(session_id, None)
