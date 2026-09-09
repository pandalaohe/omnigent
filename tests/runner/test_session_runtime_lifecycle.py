"""Session runtime lifecycle generation and lock contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnigent.runner.session_runtime_lifecycle import SessionRuntimeLifecycle


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
        async with lock:
            release_started.set()
            assert lifecycle.claim_reclaim("session")
            await allow_release.wait()
            lifecycle.finish_reclaim("session")

    async def start() -> None:
        await release_started.wait()
        async with lock:
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
