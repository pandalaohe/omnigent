"""Runner-side idle CLI inspection and release contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from omnigent.runner import create_runner_app
from omnigent.terminals.pane_reaper import NativePaneReaper, PaneRef
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


class _FakePaneReaper:
    def __init__(self) -> None:
        self.managed: list[str] = []
        self.released: list[tuple[str, float, str]] = []
        self.released_now: list[str] = []

    def manage(self, conversation_id: str) -> None:
        self.managed.append(conversation_id)

    async def retention_snapshot(self, conversation_id: str, *, idle_threshold_s: float):
        del idle_threshold_s
        return {
            "family": "claude",
            "busy": False,
            "eligible": True,
            "idle_seconds": 120.0,
            "activity_token": "activity-1",
        }

    async def release_if_idle(
        self,
        conversation_id: str,
        *,
        idle_threshold_s: float,
        expected_activity_token: str,
    ) -> str:
        self.released.append((conversation_id, idle_threshold_s, expected_activity_token))
        return "released"

    async def release_now(self, conversation_id: str) -> str:
        self.released_now.append(conversation_id)
        return "released"

    def unmanage(self, conversation_id: str) -> None:
        if conversation_id in self.managed:
            self.managed.remove(conversation_id)

    def note_activity(self, conversation_id: str) -> None:
        del conversation_id


class _MissingPaneReaper(_FakePaneReaper):
    async def retention_snapshot(self, conversation_id: str, *, idle_threshold_s: float):
        del conversation_id, idle_threshold_s


@pytest.mark.asyncio
async def test_runner_reports_and_conditionally_releases_idle_native_cli() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "a1b2c3d4e5f61234567890abcdef0123"

    async with _runner_client(app) as client:
        snapshot = await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 7,
            },
        )
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={
                "reason": "idle_pool_overflow",
                "idle_threshold_seconds": 60,
                "expected_activity_token": "pane:activity-1",
                "runtime_generation": snapshot.json()["runtime_generation"],
                "host_id": "host-a",
                "policy_revision": 7,
            },
        )

    assert snapshot.status_code == 200
    assert snapshot.json()["eligible"] is True
    assert snapshot.json()["family"] == "claude"
    assert snapshot.json()["activity_token"] == "pane:activity-1"
    assert snapshot.json()["policy_revision"] == 7
    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert reaper.managed == [session_id]
    assert pm.managed_for_retention_calls == [session_id]
    assert session_id not in pm.managed_for_retention
    assert reaper.released == [(session_id, 60.0, "activity-1")]
    assert pm.released == [session_id]


@pytest.mark.asyncio
async def test_runner_archive_release_is_status_independent() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "b1b2c3d4e5f61234567890abcdef0123"

    async with _runner_client(app) as client:
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={"reason": "archive"},
        )

    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert reaper.released_now == [session_id]
    assert pm.released == [session_id]


@pytest.mark.asyncio
async def test_runner_reports_cli_release_failure_when_harness_process_survives() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm.release = AsyncMock(return_value=False)  # type: ignore[method-assign]
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="harness subprocess did not exit"):
        await app.state.finish_cli_release("session-a")


@pytest.mark.asyncio
async def test_runner_keeps_archive_fence_until_newer_unarchive_revision() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "ba1b2c3d4e5f61234567890abcdef012"

    async with _runner_client(app) as client:
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={"reason": "archive", "archive_revision": 3},
        )
        blocked = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "message", "role": "user", "content": []},
        )
        stale = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/archive-state",
            json={
                "archive_scope_id": session_id,
                "archive_revision": 2,
                "archived": False,
            },
        )
        cleared = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/archive-state",
            json={
                "archive_scope_id": session_id,
                "archive_revision": 4,
                "archived": False,
            },
        )

    assert released.status_code == 200
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "session_archived"
    assert stale.status_code == 409
    assert cleared.status_code == 200
    assert app.state.cli_runtime_lifecycle.runtime_start_allowed(session_id)


@pytest.mark.asyncio
async def test_newer_unarchive_wins_while_old_archive_waits_to_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runner.native.interrupt import NativeInterruptRunner

    interrupt_started = asyncio.Event()
    allow_interrupt = asyncio.Event()

    async def _parked_stop(self, harness, session_id):
        del self, harness, session_id
        interrupt_started.set()
        await allow_interrupt.wait()

    monkeypatch.setattr(NativeInterruptRunner, "stop", _parked_stop)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "bb1b2c3d4e5f61234567890abcdef012"

    async with _runner_client(app) as client:
        old_archive = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/cli-retention/release",
                json={
                    "reason": "archive",
                    "archive_scope_id": "root",
                    "archive_revision": 3,
                },
            )
        )
        await interrupt_started.wait()
        cleared = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/archive-state",
            json={
                "archive_scope_id": "root",
                "archive_revision": 4,
                "archived": False,
            },
        )
        allow_interrupt.set()
        stale = await old_archive

    assert cleared.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["status"] == "stale_archive"
    assert reaper.released_now == []


@pytest.mark.asyncio
async def test_runner_does_not_retain_process_when_native_cli_is_absent() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _MissingPaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "c1b2c3d4e5f61234567890abcdef0123"
    pm.manage_for_retention(session_id)

    async with _runner_client(app) as client:
        snapshot = await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 3,
            },
        )

    assert snapshot.status_code == 200
    assert snapshot.json()["present"] is False
    assert session_id not in pm.managed_for_retention
    assert session_id not in reaper.managed


@pytest.mark.asyncio
async def test_runner_retains_and_releases_supported_resident_harness() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm.retention_snapshot_result = {
        "present": True,
        "supported": True,
        "family": "codex",
        "busy": False,
        "eligible": True,
        "idle_seconds": 120.0,
        "activity_token": "0:123.0",
    }
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    app.state.native_pane_reaper = None
    session_id = "d1b2c3d4e5f61234567890abcdef0123"

    async with _runner_client(app) as client:
        snapshot = await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 11,
            },
        )
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={
                "reason": "idle_pool_overflow",
                "idle_threshold_seconds": 60,
                "expected_activity_token": "harness:0:123.0",
                "runtime_generation": snapshot.json()["runtime_generation"],
                "host_id": "host-a",
                "policy_revision": 11,
            },
        )

    assert snapshot.json()["family"] == "codex"
    assert snapshot.json()["activity_token"] == "harness:0:123.0"
    assert released.json()["status"] == "released"
    assert pm.retention_releases == [(session_id, 60.0, "0:123.0")]


@pytest.mark.asyncio
async def test_runner_rejects_release_from_superseded_policy_revision() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "e1b2c3d4e5f61234567890abcdef0123"

    async with _runner_client(app) as client:
        await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 8,
            },
        )
        stale = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={
                "reason": "idle_pool_overflow",
                "idle_threshold_seconds": 60,
                "expected_activity_token": "pane:activity-1",
                "runtime_generation": "stale-boot:0",
                "host_id": "host-a",
                "policy_revision": 7,
            },
        )

    assert stale.status_code == 409
    assert stale.json()["status"] == "stale_policy"
    assert reaper.released == []


@pytest.mark.asyncio
async def test_runner_reset_returns_pane_and_harness_to_legacy_reapers() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "f1b2c3d4e5f61234567890abcdef0123"

    async with _runner_client(app) as client:
        await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 4,
            },
        )
        reset = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/reset",
            json={"host_id": "host-a", "policy_revision": 5},
        )

    assert reset.status_code == 200
    assert reset.json()["status"] == "reset"
    assert session_id not in reaper.managed
    assert session_id not in pm.managed_for_retention


@pytest.mark.asyncio
async def test_runner_rejects_reset_older_than_observed_policy() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "ab1b2c3d4e5f61234567890abcdef012"

    async with _runner_client(app) as client:
        await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 9,
            },
        )
        stale = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/reset",
            json={"host_id": "host-a", "policy_revision": 8},
        )

    assert stale.status_code == 409
    assert stale.json()["status"] == "stale_policy"
    assert session_id in reaper.managed
    assert session_id in pm.managed_for_retention


async def test_stale_get_does_not_manage_or_snapshot() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    snapshot = AsyncMock(wraps=reaper.retention_snapshot)
    note_activity = Mock()
    reaper.retention_snapshot = snapshot
    reaper.note_activity = note_activity
    app.state.native_pane_reaper = reaper
    session_id = "session-stale-get"
    async with _runner_client(app) as client:
        reset = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/reset",
            json={"host_id": "host-a", "policy_revision": 5},
        )
        stale = await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={"idle_threshold_seconds": 60, "host_id": "host-a", "policy_revision": 4},
        )
    assert reset.status_code == 200
    assert stale.status_code == 409
    assert stale.json() == {"session_id": session_id, "status": "stale_policy"}
    assert reaper.managed == []
    assert pm.managed_for_retention_calls == []
    snapshot.assert_not_awaited()
    note_activity.assert_not_called()


async def test_cross_host_reset_rejects_without_unmanaging() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "session-cross-host-reset"
    async with _runner_client(app) as client:
        await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={"idle_threshold_seconds": 60, "host_id": "host-b", "policy_revision": 1},
        )
        stale = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/reset",
            json={"host_id": "host-a", "policy_revision": 7},
        )
    assert stale.status_code == 409
    assert stale.json()["status"] == "stale_host"
    assert session_id in reaper.managed
    assert session_id in pm.managed_for_retention
    assert app.state.cli_runtime_lifecycle.policy_matches(session_id, host_id="host-b", revision=1)


async def test_reset_without_stored_host_is_accepted() -> None:
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    session_id = "session-after-restart"
    async with _runner_client(app) as client:
        reset = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/reset",
            json={"host_id": "host-a", "policy_revision": 3},
        )
    assert reset.status_code == 200
    assert reset.json()["status"] == "reset"


class _AbsentHarnessProcessManager(_FakeProcessManager):
    """ProcessManager stub whose resident harness died before the release."""

    async def release_if_retention_idle(
        self,
        conversation_id: str,
        *,
        idle_threshold_s: float,
        expected_activity_token: str,
    ) -> str:
        self.retention_releases.append(
            (conversation_id, idle_threshold_s, expected_activity_token)
        )
        self.managed_for_retention.discard(conversation_id)
        return "absent"


@pytest.mark.asyncio
async def test_runner_idle_release_cleans_up_when_harness_died_first() -> None:
    """An "absent" harness release still runs the session-level cleanup tail.

    The harness died between snapshot and release
    (``release_if_retention_idle`` -> ``"absent"``), so there is no
    subprocess left to close — but the forwarder, relay, codex
    app-server teardown, bridge dirs, router and spawn family are
    session-level pieces no reaper covers, and the lifecycle must not
    keep reporting a dead runtime as live.
    """
    pm = _AbsentHarnessProcessManager(_ScriptedHarnessClient([]))
    pm.retention_snapshot_result = {
        "present": True,
        "supported": True,
        "family": "codex",
        "busy": False,
        "eligible": True,
        "idle_seconds": 120.0,
        "activity_token": "0:123.0",
    }
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    app.state.native_pane_reaper = None
    session_id = "a91b2c3d4e5f61234567890abcdef012"

    async with _runner_client(app) as client:
        snapshot = await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 11,
            },
        )
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={
                "reason": "idle_pool_overflow",
                "idle_threshold_seconds": 60,
                "expected_activity_token": "harness:0:123.0",
                "runtime_generation": snapshot.json()["runtime_generation"],
                "host_id": "host-a",
                "policy_revision": 11,
            },
        )

    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert pm.released == [session_id]
    assert app.state.cli_runtime_lifecycle.phase(session_id) == "absent"


async def _never_busy(pane: PaneRef) -> bool:
    del pane
    return False


async def _never_reap(pane: PaneRef) -> None:
    del pane
    raise AssertionError("absent pane must not be reaped")


@pytest.mark.asyncio
async def test_runner_idle_release_cleans_up_when_pane_vanished_first() -> None:
    """An "absent" pane release still runs cleanup and retires reaper state.

    The pane was closed between snapshot and release
    (``release_if_idle`` -> ``"absent"``). The session-level cleanup
    tail must still run, the lifecycle must not keep reporting the
    runtime as live, and the reaper must drop its management and idle
    clock entries for the gone pane.
    """
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = NativePaneReaper(
        list_native_panes=list,
        is_busy=_never_busy,
        reap=_never_reap,
    )
    app.state.native_pane_reaper = reaper
    session_id = "a92b2c3d4e5f61234567890abcdef012"
    reaper.manage(session_id)
    reaper.note_activity(session_id)
    lifecycle = app.state.cli_runtime_lifecycle
    lifecycle.observe_policy(session_id, host_id="host-a", revision=3)

    async with _runner_client(app) as client:
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={
                "reason": "idle_pool_overflow",
                "idle_threshold_seconds": 60,
                "expected_activity_token": "pane:stale-token",
                "runtime_generation": lifecycle.runtime_token(session_id),
                "host_id": "host-a",
                "policy_revision": 3,
            },
        )

    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert pm.released == [session_id]
    assert lifecycle.phase(session_id) == "absent"
    assert session_id not in reaper._managed_conversations
    assert session_id not in reaper._last_busy_at


async def test_archive_release_does_not_run_tail_while_orphan_owes_it() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    lifecycle = app.state.cli_runtime_lifecycle
    reaper = NativePaneReaper(
        list_native_panes=list,
        is_busy=_never_busy,
        reap=_never_reap,
        runtime_token=lifecycle.runtime_token,
    )
    app.state.native_pane_reaper = reaper
    session_id = "session-owed-tail"
    instance = object()
    reaper._record_pending(
        PaneRef(session_id, "terminal:codex:main", "codex", Path("/tmp/test.sock"), instance),
        instance,
    )
    async with _runner_client(app) as client:
        archive = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/archive-state",
            json={"archive_scope_id": "root", "archive_revision": 1, "archived": True},
        )
        release = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={"reason": "archive", "archive_scope_id": "root", "archive_revision": 1},
        )
    assert archive.status_code == 200
    assert release.status_code == 500
    assert release.json()["status"] == "failed"
    assert pm.released == []
