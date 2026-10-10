"""Integration tests for session archive lifecycle and agent contents download.

Covers:
- ``PATCH /v1/sessions/{id}`` with ``archived=True/False``
- ``GET /v1/sessions`` with ``include_archived`` filtering
- ``GET /v1/sessions/{id}/agent/contents`` returning a valid gzip tarball

Uses the shared ``client`` fixture from ``tests/server/conftest.py``
(real stores + mock LLM) so the tests hit the real route-to-store
pipeline without subprocesses.
"""

from __future__ import annotations

import asyncio
import dataclasses
import gzip
import io
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from omnigent.cli_retention import CliRetentionPolicy
from omnigent.server.archive_close import ArchiveCloseCoordinator
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.cli_release_store import CliReleaseIntentStore
from omnigent.server.routes import sessions as _sessions_facade
from omnigent.server.routes._host_worktree import (
    WORKTREE_ROOT_LABEL_KEY,
    worktree_root_fingerprint,
)
from omnigent.server.routes._sessions import common as _sessions_common
from omnigent.server.routes._sessions import orchestration as _sessions_orchestration
from omnigent.server.user_preferences_store import SqlAlchemyUserPreferencesStore
from omnigent.stores.conversation_store import (
    ARCHIVE_DELETE_WORKTREE_LABEL_KEY,
    ARCHIVE_KEEP_WORKTREE_LABEL_KEY,
    ARCHIVE_REMOVED_WORKTREE_LABEL_KEY,
    ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.host_store import HostStore
from tests.server.helpers import CapturingRunnerClient, create_test_session

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_archive_stop_grace() -> object:
    """
    Fire the deferred archive teardown immediately in these tests.

    The handler defers the runner teardown past the Undo window (see
    ``_ARCHIVE_STOP_UNDO_GRACE_S``); zeroing it here keeps the stop-runs /
    stop-skipped assertions fast. Tests that need the timer to stay pending
    (to observe or cancel it) set their own grace.
    """
    with patch.object(_sessions_facade, "_ARCHIVE_STOP_UNDO_GRACE_S", 0.0):
        yield


# ── Archive / unarchive lifecycle ────────────────────────


async def test_session_not_archived_by_default(
    client: httpx.AsyncClient,
) -> None:
    """A freshly created session has ``archived=False``."""
    session = await create_test_session(client, name="archive-default")
    assert session["archived"] is False


async def test_archive_hides_session_from_default_listing(
    client: httpx.AsyncClient,
) -> None:
    """Archiving a session removes it from the default GET /v1/sessions listing."""
    session = await create_test_session(client, name="archive-hide")
    session_id = session["id"]

    # Archive it.
    patch_resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": True},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["archived"] is True

    # Default listing (include_archived=False) should not contain it.
    listing = await client.get("/v1/sessions")
    assert listing.status_code == 200
    listed_ids = [s["id"] for s in listing.json()["data"]]
    assert session_id not in listed_ids


async def test_archived_session_appears_with_include_archived(
    client: httpx.AsyncClient,
) -> None:
    """An archived session is returned when ``include_archived=True``."""
    session = await create_test_session(client, name="archive-include")
    session_id = session["id"]

    await client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": True},
    )

    listing = await client.get("/v1/sessions", params={"include_archived": "true"})
    assert listing.status_code == 200
    listed_ids = [s["id"] for s in listing.json()["data"]]
    assert session_id in listed_ids


async def test_unarchive_restores_session_to_default_listing(
    client: httpx.AsyncClient,
) -> None:
    """Unarchiving a session makes it visible in the default listing again."""
    session = await create_test_session(client, name="archive-restore")
    session_id = session["id"]

    # Archive then unarchive.
    await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
    patch_resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={"archived": False},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["archived"] is False

    # Back in the default listing.
    listing = await client.get("/v1/sessions")
    assert listing.status_code == 200
    listed_ids = [s["id"] for s in listing.json()["data"]]
    assert session_id in listed_ids


# ── Best-effort stop before archive ───────────────────────


async def _drain_detached_stops() -> None:
    """
    Wait out the archive PATCH's detached best-effort stop.

    The handler spawns the stop as a retained background task and responds
    immediately, so assertions about the stop must let it finish first.
    """
    await asyncio.gather(
        *list(_sessions_orchestration._detached_stop_tasks),
        return_exceptions=True,
    )


async def test_archive_running_session_attempts_stop(
    client: httpx.AsyncClient,
) -> None:
    """Archiving a running session calls ``_stop_session_via_runner``."""
    session = await create_test_session(client, name="archive-running")
    session_id = session["id"]

    mock_stop = AsyncMock(return_value=True)
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            )
            await _drain_detached_stops()
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        mock_stop.assert_awaited_once()
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_can_leave_cli_running_when_host_policy_disables_close(
    app,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Explicit close_on_archive=false gates the existing archive stop path."""
    session = await create_test_session(client, name="archive-without-close")
    session_id = session["id"]
    host_id = "7a2b3c4d5e6f1234567890abcdef0123"
    host_store = HostStore(db_uri)
    app.state.host_store = host_store
    host_store.upsert_on_connect(host_id, "archive-test-host", "local")
    host_store.replace_cli_retention_policy(
        host_id,
        CliRetentionPolicy(
            idle_threshold_minutes=60,
            max_idle_clis=10,
            close_on_archive=False,
        ),
        expected_revision=0,
    )
    SqlAlchemyConversationStore(db_uri).set_host_id(
        session_id,
        host_id,
        workspace="/tmp/archive-without-close",
    )

    mock_stop = AsyncMock(return_value=True)
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
            await _drain_detached_stops()
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        mock_stop.assert_not_awaited()
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_does_not_block_on_slow_stop(
    client: httpx.AsyncClient,
) -> None:
    """
    The PATCH responds while the best-effort stop is still in flight.

    The stop carries per-runner timeouts of several seconds against a
    wedged or asleep runner; awaiting it inline made every archive of a
    running session eat those timeouts before the flag flipped. The
    handler detaches the stop instead — the response must not wait for
    it, and the stop must still run.
    """
    session = await create_test_session(client, name="archive-slow-stop")
    session_id = session["id"]

    release = asyncio.Event()
    stop_started = asyncio.Event()
    stopped: list[str] = []

    async def _parked_stop(sid: str, *_args: object) -> None:
        stopped.append(sid)
        stop_started.set()
        await release.wait()

    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_facade, "_best_effort_stop", _parked_stop):
            # Would exhaust the timeout here if the handler awaited the
            # stop inline (the fake stop parks until released below).
            resp = await asyncio.wait_for(
                client.patch(f"/v1/sessions/{session_id}", json={"archived": True}),
                timeout=5.0,
            )
            assert resp.status_code == 200
            assert resp.json()["archived"] is True
            # The detached task crosses a worker-thread DB read before it
            # reaches the stop. Wait for its explicit start signal rather than
            # assuming a fixed number of event-loop yields schedules a Windows
            # executor thread.
            await asyncio.wait_for(stop_started.wait(), timeout=5.0)
            assert stopped == [session_id]
            assert _sessions_orchestration._archive_close_in_progress(session_id) is True
            release.set()
            await _drain_detached_stops()
            assert _sessions_orchestration._archive_close_in_progress(session_id) is False
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_idle_parent_stops_running_child(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """Archiving an idle parent with a running child stops the child.

    Regression test: ``_best_effort_stop`` previously used the child
    rollup only to decide whether to act, then always issued the stop
    against the parent's own session id. A parent that has already gone
    idle while its sub-agent child keeps running would get a no-op stop,
    leaving the child orphaned once the parent (and its DB row, via the
    cascading subtree delete/archive) is gone.

    The child row now also forces the archive's idle deferral; the child
    keeps running here, so the teardown proceeds at the leak guard and
    must still stop the child, not the (idle) parent.
    """
    session = await create_test_session(client, name="archive-idle-parent-child")
    session_id = session["id"]

    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=session_id,
        agent_id=session["agent_id"],
    )

    mock_stop = AsyncMock(return_value=True)
    _sessions_common._session_status_cache[child.id] = "running"
    try:
        with (
            # The child stays running, so release the deferral at the leak
            # guard; the stop itself is what this test pins.
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_MAX_WAIT_S", 0.2),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_POLL_S", 0.02),
            patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop),
        ):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            )
            row = conv_store.get_conversation(session_id)
            assert row is not None
            # The child row forces the deferral even without the flag.
            assert row.labels.get(ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY) == str(row.archive_revision)
            await _drain_detached_stops()
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        # The child must be the one stopped, not the (idle) parent.
        mock_stop.assert_awaited_once()
        assert mock_stop.await_args is not None
        assert mock_stop.await_args.args[0] == child.id
    finally:
        _sessions_common._session_status_cache.pop(child.id, None)


async def test_archive_tears_down_host_spawned_runner(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    Archiving a host-spawned session tears down its dedicated runner.

    Killing the pane alone leaves the host-launched runner connected, so
    ``/health`` keeps reporting ``runner_online: true`` and a later
    message hangs on "working" against a dead pane. Archive is the one
    lifecycle action with no client-side stop, so the server carries the
    teardown itself rather than racing a second stop against the same
    runner.
    """
    session = await create_test_session(client, name="archive-host-spawned")
    session_id = session["id"]

    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_host_id(
        session_id, "a1b2c3d4e5f61234567890abcdef0123", workspace="/tmp/archive-ws"
    )
    conv_store.set_runner_id(session_id, "b1b2c3d4e5f61234567890abcdef0123")

    mock_teardown = AsyncMock(return_value="acked")
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with (
            patch.object(
                _sessions_orchestration, "_stop_session_via_runner", AsyncMock(return_value=True)
            ),
            patch.object(_sessions_facade, "_stop_session_host_runner_outcome", mock_teardown),
        ):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            )
            await _drain_detached_stops()
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
        mock_teardown.assert_awaited_once()
        assert mock_teardown.await_args is not None
        assert mock_teardown.await_args.args[:3] == (
            session_id,
            "a1b2c3d4e5f61234567890abcdef0123",
            "b1b2c3d4e5f61234567890abcdef0123",
        )
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)
        _sessions_common._intentional_stop_sessions.pop(session_id, None)


async def test_failed_archive_leaves_session_running(
    client: httpx.AsyncClient,
) -> None:
    """
    A rejected archive PATCH must not stop the session.

    The stop is spawned only after the archived flag commits, so a
    request that fails a later validation (here a server-derived
    per-user pin key) leaves the session both unarchived and untouched.
    """
    session = await create_test_session(client, name="archive-rejected")
    session_id = session["id"]

    mock_stop = AsyncMock(return_value=True)
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True, "labels": {"omnigent.pinned.someone": "1"}},
            )
            await _drain_detached_stops()
        assert resp.status_code >= 400
        mock_stop.assert_not_awaited()
        listed = await client.get(f"/v1/sessions/{session_id}")
        assert listed.json()["archived"] is False
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_proceeds_when_stop_fails(
    client: httpx.AsyncClient,
) -> None:
    """Archive succeeds even when the runner stop raises."""
    session = await create_test_session(client, name="archive-stop-fail")
    session_id = session["id"]

    mock_stop = AsyncMock(side_effect=ConnectionError("runner gone"))
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True},
            )
            await _drain_detached_stops()
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_proceeds_when_child_lookup_fails(
    client: httpx.AsyncClient,
) -> None:
    """Archive succeeds even when the child-id DB lookup raises."""
    session = await create_test_session(client, name="archive-db-fail")
    session_id = session["id"]

    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(
            _sessions_orchestration,
            "_best_effort_stop",
            wraps=_sessions_orchestration._best_effort_stop,
        ):
            orig = _sessions_orchestration._best_effort_stop

            async def _patched_stop(sid, cs, rr):
                with patch.object(
                    cs,
                    "list_child_conversation_ids_by_parent",
                    side_effect=RuntimeError("transient DB error"),
                ):
                    await orig(sid, cs, rr)

            with patch.object(_sessions_facade, "_best_effort_stop", _patched_stop):
                resp = await client.patch(
                    f"/v1/sessions/{session_id}",
                    json={"archived": True},
                )
                await _drain_detached_stops()
        assert resp.status_code == 200
        assert resp.json()["archived"] is True
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_idle_session(
    client: httpx.AsyncClient,
) -> None:
    """An idle session can be archived normally (no stop needed)."""
    session = await create_test_session(client, name="archive-idle")
    session_id = session["id"]

    mock_stop = AsyncMock()
    with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
        resp = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"archived": True},
        )
        await _drain_detached_stops()
    assert resp.status_code == 200
    assert resp.json()["archived"] is True
    mock_stop.assert_not_awaited()


async def test_archive_releases_idle_hostless_cli_resources(
    client: httpx.AsyncClient,
) -> None:
    """Archive closes an idle CLI even when no Host runner can be terminated."""
    session = await create_test_session(client, name="archive-idle-hostless-cli")
    session_id = session["id"]
    posts: list[tuple[str, dict[str, object]]] = []

    class _RunnerClient:
        async def post(self, url, *, json, timeout):
            del timeout
            posts.append((url, json))

            class _Response:
                status_code = 200
                text = ""

            return _Response()

    async def _runner_client(*_args, **_kwargs):
        return _RunnerClient()

    with patch.object(_sessions_facade, "_get_runner_client", _runner_client):
        resp = await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
        await _drain_detached_stops()

    assert resp.status_code == 200
    assert posts == [
        (
            f"/v1/sessions/{session_id}/cli-retention/release",
            {
                "reason": "archive",
                "archive_scope_id": session_id,
                "archive_revision": 1,
            },
        )
    ]


async def test_unarchive_skips_stop(
    client: httpx.AsyncClient,
) -> None:
    """Unarchiving does not attempt a stop, even if the session is running."""
    session = await create_test_session(client, name="unarchive-running")
    session_id = session["id"]

    await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
    # Let the archive operation that owns the stop settle before isolating the
    # unarchive request. A durable archive worker may start after the PATCH
    # response; that stop still belongs to the preceding archive transition.
    await _drain_detached_stops()

    mock_stop = AsyncMock()
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": False},
            )
        assert resp.status_code == 200
        assert resp.json()["archived"] is False
        mock_stop.assert_not_awaited()
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archived_session_rejects_new_user_work_until_unarchived(
    client: httpx.AsyncClient,
) -> None:
    """A new message cannot race an archive close by relaunching its CLI."""
    session = await create_test_session(client, name="archive-read-only")
    session_id = session["id"]
    archived = await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
    await _drain_detached_stops()
    assert archived.status_code == 200

    rejected = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "wake anyway"}],
            },
        },
    )
    assert rejected.status_code == 409
    assert "unarchive" in rejected.text.lower()


async def test_undo_within_grace_keeps_runner_alive(
    client: httpx.AsyncClient,
) -> None:
    """
    Unarchiving before the deferred stop fires keeps the runner alive.

    Same-replica fast path: an Undo (which re-PATCHes ``archived=false``)
    cancels the pending stop, so it never runs.
    """
    session = await create_test_session(client, name="archive-undo-keep")
    session_id = session["id"]

    mock_stop = AsyncMock(return_value=True)
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with (
            patch.object(_sessions_facade, "_ARCHIVE_STOP_UNDO_GRACE_S", 30.0),
            patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop),
        ):
            await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
            assert session_id in _sessions_orchestration._pending_archive_stops
            undo = await client.patch(f"/v1/sessions/{session_id}", json={"archived": False})
            assert undo.json()["archived"] is False
            # The pending stop is cancelled and drops out of the registry.
            assert session_id not in _sessions_orchestration._pending_archive_stops
            await _drain_detached_stops()
        mock_stop.assert_not_awaited()
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_stop_skips_when_row_unarchived(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    The deferred stop re-reads the store and skips a no-longer-archived row.

    Cross-replica backstop: a timer on another replica than the Undo can't
    be cancelled in memory, so firing ``_archive_stop`` against a row that
    is no longer archived must tear down nothing. Reading the persisted
    flag (not a per-replica entry) is what makes it safe.
    """
    session = await create_test_session(client, name="archive-stop-unarchived")
    session_id = session["id"]

    # Archive, then unarchive so the persisted flag reads false.
    await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
    await _drain_detached_stops()
    await client.patch(f"/v1/sessions/{session_id}", json={"archived": False})

    conv_store = SqlAlchemyConversationStore(db_uri)
    mock_stop = AsyncMock(return_value=True)
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop):
            # Simulate the deferred stop firing after the grace elapsed.
            await _sessions_orchestration._archive_stop(
                session_id, conv_store, runner_router=None, host_registry=None
            )
        mock_stop.assert_not_awaited()
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_stop_retries_transient_read_then_honors_undo(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A transient read failure is retried, then the confirmed flag is honored.

    Neither guessing stop nor guessing skip on a failed read is safe. So the
    teardown retries the row read; here the retry succeeds and sees the
    session was unarchived (a late Undo persisted on ``another replica``),
    so it must NOT stop the runner.
    """
    session = await create_test_session(client, name="archive-stop-retry-undo")
    session_id = session["id"]
    await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
    await _drain_detached_stops()
    # Persist the Undo, as another replica would.
    await client.patch(f"/v1/sessions/{session_id}", json={"archived": False})

    conv_store = SqlAlchemyConversationStore(db_uri)
    real_get = conv_store.get_conversation
    calls = {"n": 0}

    def _flaky_once(sid: str) -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient store failure")
        return real_get(sid)

    mock_stop = AsyncMock(return_value=True)
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with (
            patch.object(conv_store, "get_conversation", _flaky_once),
            patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop),
        ):
            await _sessions_orchestration._archive_stop(
                session_id, conv_store, runner_router=None, host_registry=None
            )
        assert calls["n"] >= 2  # retried past the transient failure
        mock_stop.assert_not_awaited()  # confirmed unarchive → left alone
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_archive_stop_skips_when_read_never_succeeds(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A sustained read outage gives up and skips, never blindly stopping.

    If every retry fails we can't confirm the archived state, so the
    conservative choice is to leave the runner (a later lifecycle event
    reaps it) rather than risk killing a session that was just unarchived.
    """
    session = await create_test_session(client, name="archive-stop-read-outage")
    session_id = session["id"]
    await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
    await _drain_detached_stops()

    conv_store = SqlAlchemyConversationStore(db_uri)
    mock_stop = AsyncMock(return_value=True)
    _sessions_common._session_status_cache[session_id] = "running"

    def _always_raise(*_a: object, **_k: object) -> None:
        raise RuntimeError("sustained store outage")

    try:
        with (
            patch.object(_sessions_orchestration, "_ARCHIVE_STOP_LOOKUP_RETRY_S", 0.0),
            patch.object(conv_store, "get_conversation", _always_raise),
            patch.object(_sessions_orchestration, "_stop_session_via_runner", mock_stop),
        ):
            await _sessions_orchestration._archive_stop(
                session_id, conv_store, runner_router=None, host_registry=None
            )
        mock_stop.assert_not_awaited()  # gave up, did not blind-stop
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_cancel_cannot_interrupt_teardown_in_flight(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A cancel racing an in-flight teardown can't strand the intentional-stop
    marker.

    Once ``_archive_stop`` passes its archived-flag guard it unregisters
    from the pending map, so a late ``_cancel_pending_archive_stop`` (an
    Undo racing the teardown) is a no-op and the stop runs to completion —
    the marker it sets is only cleared by the stop's own logic, never left
    dangling by a cancellation mid-await.
    """
    session = await create_test_session(client, name="archive-cancel-race")
    session_id = session["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
    await _drain_detached_stops()

    entered = asyncio.Event()
    release = asyncio.Event()

    async def _parked_best_effort(*_args: object, **_kwargs: object) -> None:
        entered.set()
        await release.wait()

    _sessions_common._session_status_cache[session_id] = "running"
    try:
        # Drive the REAL registered path: _spawn_archive_stop registers a task
        # in _pending_archive_stops (grace=0 so it starts at once), and the
        # parked best-effort stop holds it at the in-flight point.
        with (
            patch.object(_sessions_facade, "_ARCHIVE_STOP_UNDO_GRACE_S", 0.0),
            patch.object(_sessions_facade, "_best_effort_stop", _parked_best_effort),
        ):
            _sessions_orchestration._spawn_archive_stop(session_id, conv_store, None, None)
            registered = _sessions_orchestration._pending_archive_stops.get(session_id)
            assert registered is not None
            await asyncio.wait_for(entered.wait(), timeout=5.0)
            # The registered task is now mid-teardown; it must have removed
            # ITSELF from the map, so a cancel here can't reach and interrupt it.
            assert session_id not in _sessions_orchestration._pending_archive_stops
            _sessions_orchestration._cancel_pending_archive_stop(session_id)
            release.set()
            await asyncio.wait_for(registered, timeout=5.0)
            # The very task the map held ran to completion, not cancelled.
            assert not registered.cancelled()
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_delete_worktree_requires_archive(
    client: httpx.AsyncClient,
) -> None:
    """``delete_worktree`` without ``archived=true`` is rejected."""
    session = await create_test_session(client, name="archive-worktree-invalid")
    resp = await client.patch(
        f"/v1/sessions/{session['id']}",
        json={"title": "x", "delete_worktree": True},
    )
    assert resp.status_code == 400


async def test_worktree_status_includes_archived_child_and_requires_a_removal_receipt(
    app, client: httpx.AsyncClient, db_uri: str
) -> None:
    parent = await create_test_session(client, name="status-parent")
    assert (await client.get("/v1/info")).json()["worktree_status"] is True
    store = SqlAlchemyConversationStore(db_uri)
    child = store.create_conversation(
        kind="sub_agent",
        title="Past child",
        parent_conversation_id=parent["id"],
        host_id="0123456789abcdef0123456789abcdef",
        workspace="/opt/work/sample-app/tree",
        git_branch="feature/status",
    )
    store.update_conversation(child.id, archived=True)
    conn = SimpleNamespace(hello=SimpleNamespace(capabilities=["worktree_safe_archive_v1"]))
    app.state.host_registry = SimpleNamespace(get=lambda _host_id: conn)
    with patch(
        "omnigent.server.routes._host_worktree.list_worktrees_on_host",
        AsyncMock(return_value=[]),
    ):
        response = await client.get(
            f"/v1/sessions/{parent['id']}/worktree-status", params={"refresh": "true"}
        )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session_count"] == 2
    assert payload["aggregate"]["state"] == "unknown"
    assert payload["blockers"][0]["session_id"] == child.id
    assert payload["blockers"][0]["state"] == "unknown"

    store.set_labels(child.id, {ARCHIVE_REMOVED_WORKTREE_LABEL_KEY: "1"})
    removed = await client.get(f"/v1/sessions/{child.id}/worktree-status")
    assert removed.status_code == 200
    assert removed.json()["own"]["state"] == "removed"
    assert removed.json()["aggregate"]["state"] == "removed"


async def test_keep_worktree_archive_override_survives_delete_safe_preference(
    app, client: httpx.AsyncClient, db_uri: str
) -> None:
    session = await create_test_session(client, name="archive-keep-override")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(
        session["id"],
        "0123456789abcdef0123456789abcdef",
        workspace="/opt/work/sample-app/keep",
        git_branch="feature/keep",
    )
    preferences = SqlAlchemyUserPreferencesStore(db_uri)
    app.state.user_preferences_store = preferences
    app.state.archive_close_coordinator.set_archive_preferences(preferences, None)
    preferences.patch_namespace(RESERVED_USER_LOCAL, "worktree_archive", {"mode": "delete_safe"})
    remove = AsyncMock(return_value=True)
    with patch(
        "omnigent.server.routes._sessions.helpers.remove_archived_worktree_best_effort", remove
    ):
        response = await client.patch(
            f"/v1/sessions/{session['id']}",
            json={"archived": True, "keep_worktree": True},
        )
        await _drain_detached_stops()
    assert response.status_code == 200
    row = store.get_conversation(session["id"])
    assert row is not None
    assert row.labels[ARCHIVE_KEEP_WORKTREE_LABEL_KEY] == str(row.archive_revision)
    remove.assert_not_awaited()


async def test_never_preference_preserves_worktree_despite_legacy_delete_request(
    app, client: httpx.AsyncClient, db_uri: str
) -> None:
    session = await create_test_session(client, name="archive-never")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(
        session["id"],
        "0123456789abcdef0123456789abcdef",
        workspace="/opt/work/sample-app/never",
        git_branch="feature/never",
    )
    remove = AsyncMock(return_value=True)
    with patch(
        "omnigent.server.routes._sessions.helpers.remove_archived_worktree_best_effort", remove
    ):
        response = await client.patch(
            f"/v1/sessions/{session['id']}",
            json={"archived": True, "delete_worktree": True},
        )
        await _drain_detached_stops()
    assert response.status_code == 200
    row = store.get_conversation(session["id"])
    assert row is not None
    assert ARCHIVE_DELETE_WORKTREE_LABEL_KEY not in row.labels
    remove.assert_not_awaited()


async def test_worktree_status_aggregate_prioritizes_dirty_over_unknown(
    app, client: httpx.AsyncClient, db_uri: str
) -> None:
    parent = await create_test_session(client, name="status-priority")
    store = SqlAlchemyConversationStore(db_uri)
    host_id = "0123456789abcdef0123456789abcdef"
    dirty = store.create_conversation(
        kind="sub_agent",
        title="dirty child",
        parent_conversation_id=parent["id"],
        host_id=host_id,
        workspace="/opt/work/sample-app/dirty",
        git_branch="feature/dirty",
    )
    unknown = store.create_conversation(
        kind="sub_agent",
        title="unknown child",
        parent_conversation_id=parent["id"],
        host_id=host_id,
        workspace="/opt/work/sample-app/unknown",
        git_branch="feature/unknown",
    )
    conn = SimpleNamespace(hello=SimpleNamespace(capabilities=["worktree_safe_archive_v1"]))
    app.state.host_registry = SimpleNamespace(get=lambda _host_id: conn)

    async def list_for_path(**kwargs):
        if kwargs["repo_path"].endswith("/dirty"):
            return [
                {
                    "path": kwargs["repo_path"],
                    "branch": "feature/dirty",
                    "is_main": False,
                    "detached": False,
                    "files": [{"path": "draft.txt", "status": "??"}],
                }
            ]
        return []

    with patch("omnigent.server.routes._host_worktree.list_worktrees_on_host", list_for_path):
        response = await client.get(f"/v1/sessions/{parent['id']}/worktree-status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["aggregate"]["state"] == "dirty"
    assert {item["session_id"]: item["state"] for item in payload["blockers"]} == {
        dirty.id: "dirty",
        unknown.id: "unknown",
    }


async def test_worktree_status_protects_sibling_project_entry_under_recorded_root(
    app, client: httpx.AsyncClient, db_uri: str
) -> None:
    session = await create_test_session(client, name="status-project-root")
    store = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    host_id = "0123456789abcdef0123456789abcdef"
    store.set_host_id(
        session["id"],
        host_id,
        workspace=f"{root}/packages/app",
        git_branch="feature/topic",
        worktree=f"{root}/packages/app",
    )
    store.set_labels(session["id"], {WORKTREE_ROOT_LABEL_KEY: worktree_root_fingerprint(root)})
    app.state.project_host_binding_store = SimpleNamespace(
        entry_at_or_under=lambda _host, path: path == root
    )
    response = await client.get(f"/v1/sessions/{session['id']}/worktree-status")
    assert response.status_code == 200
    assert response.json()["own"]["state"] == "protected"


async def test_worktree_status_without_recorded_branch_uses_live_linked_binding(
    app, client: httpx.AsyncClient, db_uri: str
) -> None:
    session = await create_test_session(client, name="status-unrecorded-branch")
    store = SqlAlchemyConversationStore(db_uri)
    root = "/opt/work/sample-app/topic"
    host_id = "0123456789abcdef0123456789abcdef"
    store.set_host_id(session["id"], host_id, workspace=f"{root}/packages/app", worktree=root)
    store.set_labels(session["id"], {WORKTREE_ROOT_LABEL_KEY: worktree_root_fingerprint(root)})
    conn = SimpleNamespace(hello=SimpleNamespace(capabilities=["worktree_safe_archive_v1"]))
    app.state.host_registry = SimpleNamespace(get=lambda _host_id: conn)
    with patch(
        "omnigent.server.routes._host_worktree.list_worktrees_on_host",
        AsyncMock(
            return_value=[
                {
                    "path": root,
                    "branch": "feature/topic",
                    "is_main": False,
                    "detached": False,
                    "files": [],
                }
            ]
        ),
    ):
        response = await client.get(f"/v1/sessions/{session['id']}/worktree-status")
    assert response.status_code == 200
    assert response.json()["own"]["state"] == "protected"
    assert response.json()["own"]["branch"] == "feature/topic"


async def test_worktree_status_protects_archived_runner_before_release(
    app, client: httpx.AsyncClient, db_uri: str
) -> None:
    session = await create_test_session(client, name="status-runner-held")
    store = SqlAlchemyConversationStore(db_uri)
    store.set_host_id(
        session["id"],
        "0123456789abcdef0123456789abcdef",
        workspace="/opt/work/sample-app/topic",
        git_branch="feature/topic",
    )
    store.set_runner_id(session["id"], "b1b2c3d4e5f61234567890abcdef0123")
    store.update_conversation(session["id"], archived=True, close_cli_on_archive=False)
    list_host = AsyncMock()
    with patch("omnigent.server.routes._host_worktree.list_worktrees_on_host", list_host):
        response = await client.get(f"/v1/sessions/{session['id']}/worktree-status")
    assert response.status_code == 200
    assert response.json()["own"]["state"] == "protected"
    assert response.json()["own"]["reason"] == "archived runner may still use worktree"
    list_host.assert_not_awaited()


@pytest.mark.parametrize("delete_worktree", [True, False])
async def test_archive_stop_removes_worktree_when_requested(
    client: httpx.AsyncClient,
    db_uri: str,
    delete_worktree: bool,
) -> None:
    """The archive teardown removes the worktree (keeping the branch) only on opt-in."""
    session = await create_test_session(client, name=f"archive-worktree-{delete_worktree}")
    session_id = session["id"]
    await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})

    conv_store = SqlAlchemyConversationStore(db_uri)
    real_conv = conv_store.get_conversation(session_id)
    assert real_conv is not None
    worktree_conv = dataclasses.replace(
        real_conv,
        git_branch="feature/x",
        workspace="/repo-worktrees/feature-x",
        host_id="host_1",
        runner_id=None,
    )
    mock_remove = AsyncMock()
    with (
        patch.object(conv_store, "get_conversation", return_value=worktree_conv),
        patch.object(_sessions_facade, "_best_effort_stop", AsyncMock()),
        patch.object(_sessions_facade, "_remove_session_worktree_best_effort", mock_remove),
    ):
        await _sessions_orchestration._archive_stop(
            session_id,
            conv_store,
            runner_router=None,
            host_registry=None,
            delete_worktree=delete_worktree,
        )
    if not delete_worktree:
        mock_remove.assert_not_awaited()
        return
    mock_remove.assert_awaited_once()
    kwargs = mock_remove.await_args.kwargs
    assert kwargs["worktree_path"] == "/repo-worktrees/feature-x"
    assert kwargs["branch"] == "feature/x"
    assert kwargs["delete_branch"] is False
    assert kwargs["exclude_conversation_id"] == session_id


def _keep_cli_host(app, db_uri: str, host_id: str) -> HostStore:
    """A host whose policy keeps CLIs running on archive."""
    host_store = HostStore(db_uri)
    app.state.host_store = host_store
    host_store.upsert_on_connect(host_id, "archive-delete-host", "local")
    host_store.replace_cli_retention_policy(
        host_id,
        CliRetentionPolicy(
            idle_threshold_minutes=60,
            max_idle_clis=10,
            close_on_archive=False,
        ),
        expected_revision=0,
    )
    return host_store


async def test_delete_worktree_forces_the_teardown_on_a_keep_cli_host(
    app,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A-X1: the explicit delete overrides ``close_on_archive=false``."""
    session = await create_test_session(client, name="archive-delete-forces-close")
    session_id = session["id"]
    host_id = "8a2b3c4d5e6f1234567890abcdef0123"
    _keep_cli_host(app, db_uri, host_id)
    preferences = SqlAlchemyUserPreferencesStore(db_uri)
    app.state.user_preferences_store = preferences
    app.state.archive_close_coordinator.set_archive_preferences(preferences, None)
    preferences.patch_namespace(RESERVED_USER_LOCAL, "worktree_archive", {"mode": "delete_safe"})
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_host_id(
        session_id,
        host_id,
        workspace="/opt/work/omnigent/fork/archive-delete-forces-close",
        git_branch="feature/forced",
    )
    conv_store.set_runner_id(session_id, "b8b2c3d4e5f61234567890abcdef0123")

    teardown = AsyncMock(return_value="acked")
    remove = AsyncMock()
    with (
        patch.object(_sessions_facade, "_stop_session_host_runner_outcome", teardown),
        patch.object(_sessions_facade, "_remove_session_worktree_best_effort", remove),
    ):
        resp = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"archived": True, "delete_worktree": True},
        )
        await _drain_detached_stops()

    assert resp.status_code == 200
    row = conv_store.get_conversation(session_id)
    assert row is not None
    assert row.archive_revision == 1
    assert row.archive_close_requested_revision == 1
    assert row.labels[ARCHIVE_DELETE_WORKTREE_LABEL_KEY] == "1"
    assert row.archive_close_completed_revision == 1
    teardown.assert_awaited_once()
    remove.assert_awaited_once()


async def test_delete_worktree_on_an_already_archived_session_requests_the_teardown(
    app,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """W2: a delete on an archived session is not a silent no-op."""
    session = await create_test_session(client, name="archive-delete-again")
    session_id = session["id"]
    host_id = "9a2b3c4d5e6f1234567890abcdef0123"
    _keep_cli_host(app, db_uri, host_id)
    preferences = SqlAlchemyUserPreferencesStore(db_uri)
    app.state.user_preferences_store = preferences
    app.state.archive_close_coordinator.set_archive_preferences(preferences, None)
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_host_id(
        session_id,
        host_id,
        workspace="/opt/work/omnigent/fork/archive-delete-again",
        git_branch="feature/again",
    )
    conv_store.set_runner_id(session_id, "c8b2c3d4e5f61234567890abcdef0123")

    teardown = AsyncMock(return_value="acked")
    remove = AsyncMock()
    with (
        patch.object(_sessions_facade, "_stop_session_host_runner_outcome", teardown),
        patch.object(_sessions_facade, "_remove_session_worktree_best_effort", remove),
    ):
        first = await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
        await _drain_detached_stops()
        assert first.status_code == 200
        row = conv_store.get_conversation(session_id)
        assert row is not None
        # The keep-CLI policy left the first archive with no teardown at all.
        assert row.archive_close_requested_revision is None
        teardown.assert_not_awaited()

        preferences.patch_namespace(
            RESERVED_USER_LOCAL, "worktree_archive", {"mode": "delete_safe"}
        )

        second = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"archived": True, "delete_worktree": True},
        )
        await _drain_detached_stops()

    assert second.status_code == 200
    row = conv_store.get_conversation(session_id)
    assert row is not None
    assert row.archive_revision == 1  # no transition, no revision bump
    assert row.archive_close_requested_revision == 1
    assert row.labels[ARCHIVE_DELETE_WORKTREE_LABEL_KEY] == "1"
    assert row.archive_close_completed_revision == 1
    teardown.assert_awaited_once()
    remove.assert_awaited_once()
    kwargs = remove.await_args.kwargs
    assert kwargs["worktree_path"] == "/opt/work/omnigent/fork/archive-delete-again"
    assert kwargs["branch"] == "feature/again"


# ── Agent contents download ──────────────────────────────


async def test_agent_contents_returns_valid_gzip_tarball(
    client: httpx.AsyncClient,
) -> None:
    """GET /v1/sessions/{id}/agent/contents returns a valid tar.gz bundle."""
    session = await create_test_session(client, name="contents-download")
    session_id = session["id"]

    resp = await client.get(f"/v1/sessions/{session_id}/agent/contents")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/gzip"

    # Verify the bytes are valid gzip.
    decompressed = gzip.decompress(resp.content)
    assert len(decompressed) > 0

    # Verify the bytes are a valid tar archive containing config.yaml.
    with tarfile.open(fileobj=io.BytesIO(resp.content), mode="r:gz") as tf:
        names = tf.getnames()
        assert "config.yaml" in names


async def test_agent_contents_404_for_nonexistent_session(
    client: httpx.AsyncClient,
) -> None:
    """GET /v1/sessions/{id}/agent/contents returns 404 for a missing session."""
    resp = await client.get("/v1/sessions/conv_nonexistent/agent/contents")
    assert resp.status_code == 404


# ── stop_when_idle deferral ──────────────────────────────


async def test_stop_when_idle_defers_teardown_until_the_tree_settles(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """
    A ``stop_when_idle`` archive parks the teardown while the tree runs.

    The PATCH writes the durable deferral label naming the archive
    revision; the deferred teardown only proceeds after the status has
    read idle continuously for the settle window.
    """
    session = await create_test_session(client, name="archive-idle-defer")
    session_id = session["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    stopped: list[str] = []
    real_wait = _sessions_orchestration._wait_for_archive_idle
    wait_started = asyncio.Event()
    wait_revisions: list[int] = []

    async def _recording_stop(sid: str, *_args: object, **_kwargs: object) -> None:
        stopped.append(sid)

    async def _recording_wait(
        wait_session_id: str,
        revision: int,
        conversation_store: object,
    ) -> None:
        # Prove the deferral wait actually started for this revision, so the
        # empty-stop assertions below cannot pass on a crashed task.
        wait_revisions.append(revision)
        wait_started.set()
        await real_wait(wait_session_id, revision, conversation_store)

    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with (
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_SETTLE_S", 0.5),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_POLL_S", 0.02),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_MAX_WAIT_S", 20.0),
            patch.object(_sessions_facade, "_best_effort_stop", _recording_stop),
            patch.object(_sessions_facade, "_wait_for_archive_idle", _recording_wait),
            patch.object(_sessions_orchestration, "_wait_for_archive_idle", _recording_wait),
        ):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True, "stop_when_idle": True},
            )
            assert resp.status_code == 200
            row = conv_store.get_conversation(session_id)
            assert row is not None
            assert row.labels.get(ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY) == str(row.archive_revision)
            await asyncio.wait_for(wait_started.wait(), timeout=5.0)
            assert wait_revisions == [row.archive_revision]

            # The detached teardown passes the (zeroed) undo grace and parks in
            # the idle wait: a running tree keeps it there across several polls.
            await asyncio.sleep(0.25)
            assert stopped == []

            _sessions_common._session_status_cache[session_id] = "idle"
            # Inside the settle window nothing releases...
            await asyncio.sleep(0.25)
            assert stopped == []
            # ...and after it the teardown proceeds.
            for _ in range(100):
                if stopped:
                    break
                await asyncio.sleep(0.05)
            assert stopped == [session_id]
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)
        await _drain_detached_stops()


async def test_stop_when_idle_idle_blip_does_not_release_the_teardown(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A quiet-pane idle blip shorter than the settle window is not turn end."""
    session = await create_test_session(client, name="archive-idle-blip")
    session_id = session["id"]
    stopped: list[str] = []

    async def _recording_stop(sid: str, *_args: object, **_kwargs: object) -> None:
        stopped.append(sid)

    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with (
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_SETTLE_S", 0.4),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_POLL_S", 0.02),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_MAX_WAIT_S", 20.0),
            patch.object(_sessions_facade, "_best_effort_stop", _recording_stop),
        ):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True, "stop_when_idle": True},
            )
            assert resp.status_code == 200
            await asyncio.sleep(0.1)

            # A brief idle reading inside the settle window...
            _sessions_common._session_status_cache[session_id] = "idle"
            await asyncio.sleep(0.2)
            assert stopped == []

            # ...then busy again: the settle clock must restart, so the
            # original near-window reading cannot release the teardown.
            _sessions_common._session_status_cache[session_id] = "running"
            await asyncio.sleep(0.3)
            assert stopped == []

            _sessions_common._session_status_cache[session_id] = "idle"
            for _ in range(100):
                if stopped:
                    break
                await asyncio.sleep(0.05)
            assert stopped == [session_id]
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)
        await _drain_detached_stops()


async def test_stop_when_idle_unarchive_during_the_wait_skips_teardown(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """An unarchive mid-wait drops the deferral and tears nothing down."""
    session = await create_test_session(client, name="archive-idle-unarchive")
    session_id = session["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    stopped: list[str] = []

    async def _recording_stop(sid: str, *_args: object, **_kwargs: object) -> None:
        stopped.append(sid)

    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with (
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_SETTLE_S", 5.0),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_POLL_S", 0.02),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_MAX_WAIT_S", 20.0),
            patch.object(_sessions_facade, "_best_effort_stop", _recording_stop),
        ):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True, "stop_when_idle": True},
            )
            assert resp.status_code == 200
            await asyncio.sleep(0.1)

            undo = await client.patch(f"/v1/sessions/{session_id}", json={"archived": False})
            assert undo.status_code == 200
            assert undo.json()["archived"] is False
            row = conv_store.get_conversation(session_id)
            assert row is not None
            assert ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY not in row.labels

            # Even after the settle window the tree is not torn down: the
            # wait's root re-read saw the unarchive and returned.
            _sessions_common._session_status_cache[session_id] = "idle"
            await asyncio.sleep(0.3)
            assert stopped == []
            await _drain_detached_stops()
            assert stopped == []
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_rearchive_while_waiting_restarts_the_wait_for_the_new_revision(
    app,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A re-archive during a parked wait is evaluated on its own revision.

    The first wait's completion for revision 1 must not cover revision 3: if
    an unarchive and re-archive land while the old wait is parked, the
    expansion has to wait again for the new revision's tree to settle.
    """
    session = await create_test_session(client, name="archive-rearchive-wait")
    session_id = session["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    stopped: list[str] = []
    wait_revisions: list[int] = []
    first_wait_started = asyncio.Event()
    release_first_wait = asyncio.Event()
    real_wait = _sessions_orchestration._wait_for_archive_idle

    async def _recording_stop(sid: str, *_args: object, **_kwargs: object) -> None:
        stopped.append(sid)

    async def _gated_wait(
        wait_session_id: str,
        revision: int,
        conversation_store: object,
    ) -> None:
        wait_revisions.append(revision)
        if len(wait_revisions) == 1:
            # Park the first revision's wait while the test changes revisions.
            first_wait_started.set()
            await release_first_wait.wait()
            return
        await real_wait(wait_session_id, revision, conversation_store)

    coordinator = ArchiveCloseCoordinator(
        conversation_store=conv_store,
        host_store=None,
        host_registry=None,
        runner_router=None,
        intent_store=CliReleaseIntentStore(db_uri),
        scan_interval_seconds=3600,
    )
    app.state.archive_close_coordinator = coordinator
    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with (
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_SETTLE_S", 0.3),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_POLL_S", 0.02),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_MAX_WAIT_S", 20.0),
            patch.object(_sessions_facade, "_best_effort_stop", _recording_stop),
            patch.object(_sessions_facade, "_wait_for_archive_idle", _gated_wait),
            patch.object(_sessions_orchestration, "_wait_for_archive_idle", _gated_wait),
        ):
            first = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True, "stop_when_idle": True},
            )
            assert first.status_code == 200
            first_row = conv_store.get_conversation(session_id)
            assert first_row is not None
            assert first_row.labels.get(ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY) == str(
                first_row.archive_revision
            )
            await asyncio.wait_for(first_wait_started.wait(), timeout=5.0)
            assert wait_revisions == [1]

            # Revision 2 (unarchive) then revision 3 (re-archive with the
            # deferral) while the revision-1 wait is parked.
            undo = await client.patch(f"/v1/sessions/{session_id}", json={"archived": False})
            assert undo.status_code == 200
            again = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True, "stop_when_idle": True},
            )
            assert again.status_code == 200
            row = conv_store.get_conversation(session_id)
            assert row is not None
            assert row.archive_revision == 3
            assert row.labels.get(ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY) == "3"

            release_first_wait.set()
            # The expansion must wait for revision 3, not release on the
            # revision-1 wait it already honoured.
            for _ in range(100):
                if len(wait_revisions) >= 2:
                    break
                await asyncio.sleep(0.01)
            assert wait_revisions == [1, 3]
            await asyncio.sleep(0.2)
            assert stopped == []

            # A running tree keeps the new wait parked; only its own settle
            # window of idle releases the teardown.
            _sessions_common._session_status_cache[session_id] = "idle"
            await asyncio.sleep(0.15)
            assert stopped == []
            for _ in range(100):
                if stopped:
                    break
                await asyncio.sleep(0.05)
            assert stopped == [session_id]
    finally:
        app.state.archive_close_coordinator = None
        release_first_wait.set()
        _sessions_common._session_status_cache.pop(session_id, None)
        await _drain_detached_stops()


async def test_archive_without_stop_when_idle_writes_no_deferral_label(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A parentless, childless web archive keeps its timing, with no label."""
    session = await create_test_session(client, name="archive-no-idle-label")
    session_id = session["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    stopped: list[str] = []

    async def _recording_stop(sid: str, *_args: object, **_kwargs: object) -> None:
        stopped.append(sid)

    _sessions_common._session_status_cache[session_id] = "running"
    try:
        with patch.object(_sessions_facade, "_best_effort_stop", _recording_stop):
            resp = await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
            await _drain_detached_stops()
        assert resp.status_code == 200
        row = conv_store.get_conversation(session_id)
        assert row is not None
        assert ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY not in row.labels
        # No wait: the teardown ran as before.
        assert stopped == [session_id]
    finally:
        _sessions_common._session_status_cache.pop(session_id, None)


async def test_failed_child_lookup_writes_the_deferral_label(
    app,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A failed child lookup must not be read as "no children".

    The route cannot prove the session is childless when the DB read
    raises, so the archive defers the teardown: a running tree must not be
    cut just because the child check failed.
    """
    session = await create_test_session(client, name="archive-lookup-fail")
    session_id = session["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    stopped: list[str] = []

    async def _recording_stop(sid: str, *_args: object, **_kwargs: object) -> None:
        stopped.append(sid)

    async def _skip_wait(*_args: object, **_kwargs: object) -> None:
        return None

    coordinator = app.state.archive_close_coordinator
    app.state.archive_close_coordinator = None
    try:
        with (
            patch.object(
                SqlAlchemyConversationStore,
                "list_child_conversation_ids_by_parent",
                side_effect=RuntimeError("transient DB error"),
            ),
            patch.object(_sessions_facade, "_best_effort_stop", _recording_stop),
            patch.object(_sessions_orchestration, "_wait_for_archive_idle", _skip_wait),
        ):
            resp = await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
            assert resp.status_code == 200
            await _drain_detached_stops()
        row = conv_store.get_conversation(session_id)
        assert row is not None
        assert row.labels.get(ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY) == str(row.archive_revision)
        # The deferred teardown honoured the wait and then proceeded: the
        # label is not just left inert.
        assert stopped == [session_id]
    finally:
        app.state.archive_close_coordinator = coordinator


async def test_web_archive_of_child_defers_teardown_without_the_flag(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A child archived from the web still finishes its admitted turn.

    D5: its parent forces the deferral even though the client sent no
    ``stop_when_idle``, so the label names the revision and the teardown
    waits for the tree to read idle.
    """
    parent = await create_test_session(client, name="archive-defer-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=parent["id"],
        agent_id=parent["agent_id"],
    )

    stopped: list[str] = []
    wait_started = asyncio.Event()
    real_wait = _sessions_orchestration._wait_for_archive_idle

    async def _recording_stop(sid: str, *_args: object, **_kwargs: object) -> None:
        stopped.append(sid)

    async def _recording_wait(
        wait_session_id: str,
        revision: int,
        conversation_store: object,
    ) -> None:
        wait_started.set()
        await real_wait(wait_session_id, revision, conversation_store)

    _sessions_common._session_status_cache[child.id] = "running"
    try:
        with (
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_SETTLE_S", 0.05),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_POLL_S", 0.02),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_MAX_WAIT_S", 20.0),
            patch.object(_sessions_facade, "_best_effort_stop", _recording_stop),
            patch.object(_sessions_facade, "_wait_for_archive_idle", _recording_wait),
            patch.object(_sessions_orchestration, "_wait_for_archive_idle", _recording_wait),
        ):
            resp = await client.patch(f"/v1/sessions/{child.id}", json={"archived": True})
            assert resp.status_code == 200
            row = conv_store.get_conversation(child.id)
            assert row is not None
            assert row.labels.get(ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY) == str(row.archive_revision)
            await asyncio.wait_for(wait_started.wait(), timeout=5.0)
            # The running child keeps the teardown parked.
            await asyncio.sleep(0.2)
            assert stopped == []

            _sessions_common._session_status_cache[child.id] = "idle"
            for _ in range(100):
                if stopped:
                    break
                await asyncio.sleep(0.05)
            assert stopped == [child.id]
    finally:
        _sessions_common._session_status_cache.pop(child.id, None)
        await _drain_detached_stops()


async def test_web_archive_of_parent_with_child_defers_teardown(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A parentless session with sub-sessions also defers for its tree.

    D4: archiving the mother from the web must finish the admitted turns
    of her whole tree before the runner is torn down.
    """
    session = await create_test_session(client, name="archive-defer-mother")
    session_id = session["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=session_id,
        agent_id=session["agent_id"],
    )

    stopped: list[str] = []
    wait_started = asyncio.Event()
    real_wait = _sessions_orchestration._wait_for_archive_idle

    async def _recording_stop(sid: str, *_args: object, **_kwargs: object) -> None:
        stopped.append(sid)

    async def _recording_wait(
        wait_session_id: str,
        revision: int,
        conversation_store: object,
    ) -> None:
        wait_started.set()
        await real_wait(wait_session_id, revision, conversation_store)

    _sessions_common._session_status_cache[child.id] = "running"
    try:
        with (
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_SETTLE_S", 0.05),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_POLL_S", 0.02),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_MAX_WAIT_S", 20.0),
            patch.object(_sessions_facade, "_best_effort_stop", _recording_stop),
            patch.object(_sessions_facade, "_wait_for_archive_idle", _recording_wait),
            patch.object(_sessions_orchestration, "_wait_for_archive_idle", _recording_wait),
        ):
            resp = await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
            assert resp.status_code == 200
            row = conv_store.get_conversation(session_id)
            assert row is not None
            assert row.labels.get(ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY) == str(row.archive_revision)
            await asyncio.wait_for(wait_started.wait(), timeout=5.0)
            # The running child keeps the teardown parked.
            await asyncio.sleep(0.2)
            assert stopped == []

            _sessions_common._session_status_cache[child.id] = "idle"
            for _ in range(100):
                if stopped:
                    break
                await asyncio.sleep(0.05)
            assert stopped == [session_id]
    finally:
        _sessions_common._session_status_cache.pop(child.id, None)
        await _drain_detached_stops()


async def test_close_off_host_fences_a_deferred_archive_without_a_label(
    app,
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """``close_on_archive=false`` keeps no label but still fences new starts.

    The store writes the deferral label only when the host closes CLIs on
    archive, so this host has no teardown to wait for; the fence posted at
    the archive moment still stops a queued drain from starting a turn.
    """
    session = await create_test_session(client, name="archive-close-off-fence")
    session_id = session["id"]
    host_id = "7a2b3c4d5e6f1234567890abcdef0123"
    host_store = HostStore(db_uri)
    app.state.host_store = host_store
    host_store.upsert_on_connect(host_id, "archive-fence-host", "local")
    host_store.replace_cli_retention_policy(
        host_id,
        CliRetentionPolicy(
            idle_threshold_minutes=60,
            max_idle_clis=10,
            close_on_archive=False,
        ),
        expected_revision=0,
    )
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv_store.set_host_id(session_id, host_id, workspace="/tmp/archive-close-off")
    conv_store.set_runner_id(session_id, "b1b2c3d4e5f61234567890abcdef0123")
    conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=session_id,
        agent_id=session["agent_id"],
    )
    captured = CapturingRunnerClient()

    async def _runner_client(*_args: object, **_kwargs: object) -> CapturingRunnerClient:
        return captured

    with patch.object(_sessions_facade, "_get_runner_client", _runner_client):
        resp = await client.patch(f"/v1/sessions/{session_id}", json={"archived": True})
        await _drain_detached_stops()
    assert resp.status_code == 200
    row = conv_store.get_conversation(session_id)
    assert row is not None
    assert ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY not in row.labels
    assert [(p["url"], p["json"]) for p in captured.posted] == [
        (
            f"/v1/sessions/{session_id}/cli-retention/archive-state",
            {
                "archive_scope_id": session_id,
                "archive_revision": row.archive_revision,
                "archived": True,
            },
        )
    ]


async def test_archive_fence_sets_the_revision_and_unarchive_clears_it(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """The deferred archive fences the child's runner; unarchive clears it.

    D5 defers the runner release too, and a turn end drains buffered
    messages into a new turn — the fence posted at the archive moment stops
    those starts. The unarchive's newer revision supersedes it.
    """
    parent = await create_test_session(client, name="archive-fence-parent")
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=parent["id"],
        agent_id=parent["agent_id"],
    )
    conv_store.set_runner_id(child.id, "b1b2c3d4e5f61234567890abcdef0123")
    captured = CapturingRunnerClient()

    async def _runner_client(*_args: object, **_kwargs: object) -> CapturingRunnerClient:
        return captured

    _sessions_common._session_status_cache[child.id] = "idle"
    try:
        with (
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_SETTLE_S", 0.05),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_POLL_S", 0.02),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_MAX_WAIT_S", 20.0),
            patch.object(_sessions_facade, "_get_runner_client", _runner_client),
        ):
            archived = await client.patch(
                f"/v1/sessions/{child.id}",
                json={"archived": True},
            )
            assert archived.status_code == 200
            await _drain_detached_stops()
        row = conv_store.get_conversation(child.id)
        assert row is not None
        assert row.archive_revision == 1
        assert (
            f"/v1/sessions/{child.id}/cli-retention/archive-state",
            {
                "archive_scope_id": child.id,
                "archive_revision": 1,
                "archived": True,
            },
        ) in [(p["url"], p["json"]) for p in captured.posted]

        captured.posted.clear()
        with patch.object(_sessions_facade, "_get_runner_client", _runner_client):
            unarchived = await client.patch(
                f"/v1/sessions/{child.id}",
                json={"archived": False},
            )
            assert unarchived.status_code == 200
            await _drain_detached_stops()
        assert (
            f"/v1/sessions/{child.id}/cli-retention/archive-state",
            {
                "archive_scope_id": child.id,
                "archive_revision": 2,
                "archived": False,
            },
        ) in [(p["url"], p["json"]) for p in captured.posted]
    finally:
        _sessions_common._session_status_cache.pop(child.id, None)
        await _drain_detached_stops()


async def test_idle_wait_reads_persisted_live_status_without_a_cache_entry(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """A member missing from the status cache is read from its persisted row.

    Audit r3 #1: a server restart or another replica has no status-cache
    entry for the member; it must not read that as idle while the row says
    running. Once the row settles to idle the teardown proceeds.
    """
    session = await create_test_session(client, name="archive-idle-persisted")
    session_id = session["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    stopped: list[str] = []

    async def _recording_stop(sid: str, *_args: object, **_kwargs: object) -> None:
        stopped.append(sid)

    _sessions_common._session_status_cache.pop(session_id, None)
    conv_store.set_session_live_status(session_id, "running")
    try:
        with (
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_SETTLE_S", 0.2),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_POLL_S", 0.02),
            patch.object(_sessions_facade, "_ARCHIVE_IDLE_MAX_WAIT_S", 20.0),
            patch.object(_sessions_facade, "_best_effort_stop", _recording_stop),
        ):
            resp = await client.patch(
                f"/v1/sessions/{session_id}",
                json={"archived": True, "stop_when_idle": True},
            )
            assert resp.status_code == 200
            # The persisted running status keeps the teardown parked past
            # the settle window even though the cache has no entry.
            await asyncio.sleep(0.4)
            assert stopped == []

            conv_store.set_session_live_status(session_id, "idle")
            for _ in range(100):
                if stopped:
                    break
                await asyncio.sleep(0.05)
            assert stopped == [session_id]
    finally:
        await _drain_detached_stops()


async def test_client_cannot_seed_the_archive_deferral_label(
    client: httpx.AsyncClient,
) -> None:
    """A client-supplied deferral label is rejected like other reserved keys."""
    session = await create_test_session(client, name="archive-forged-label")
    session_id = session["id"]

    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={
            "archived": True,
            "labels": {ARCHIVE_STOP_WHEN_IDLE_LABEL_KEY: "1"},
        },
    )
    assert resp.status_code == 400

    listed = await client.get(f"/v1/sessions/{session_id}")
    assert listed.status_code == 200
    assert listed.json()["archived"] is False


async def test_client_cannot_seed_the_archive_worktree_delete_label(
    client: httpx.AsyncClient,
) -> None:
    """A client-supplied worktree-delete label is rejected like other reserved keys."""
    session = await create_test_session(client, name="archive-forged-delete-label")
    session_id = session["id"]

    resp = await client.patch(
        f"/v1/sessions/{session_id}",
        json={
            "archived": True,
            "labels": {ARCHIVE_DELETE_WORKTREE_LABEL_KEY: "1"},
        },
    )
    assert resp.status_code == 400

    listed = await client.get(f"/v1/sessions/{session_id}")
    assert listed.status_code == 200
    assert listed.json()["archived"] is False


async def test_unarchive_after_archive_accepts_new_user_work(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unarchiving lifts the fence so a later user message is accepted."""
    session = await create_test_session(client, name="archive-unarchive-work")
    session_id = session["id"]
    message = {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": "resume the work"}],
        },
    }

    # The flag defers the teardown, so keep the idle settle short.
    with (
        patch.object(_sessions_facade, "_ARCHIVE_IDLE_SETTLE_S", 0.05),
        patch.object(_sessions_facade, "_ARCHIVE_IDLE_POLL_S", 0.02),
        patch.object(_sessions_facade, "_ARCHIVE_IDLE_MAX_WAIT_S", 20.0),
    ):
        archived = await client.patch(
            f"/v1/sessions/{session_id}",
            json={"archived": True, "stop_when_idle": True},
        )
        assert archived.status_code == 200
        await _drain_detached_stops()

    rejected = await client.post(f"/v1/sessions/{session_id}/events", json=message)
    assert rejected.status_code == 409

    unarchived = await client.patch(f"/v1/sessions/{session_id}", json={"archived": False})
    assert unarchived.status_code == 200
    await _drain_detached_stops()

    # A live runner proves the endpoint got past the archive fence: without
    # it the message would still be dispatchable but fail later on runner
    # binding, which is unrelated to what this test asserts.
    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(202, json={"queued": True})),
        base_url="http://runner",
    )

    async def _get_runner_client(*_args: object, **_kwargs: object) -> httpx.AsyncClient:
        return fake_runner

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        _get_runner_client,
    )
    try:
        accepted = await client.post(f"/v1/sessions/{session_id}/events", json=message)
    finally:
        await fake_runner.aclose()
    assert accepted.status_code == 202, accepted.text


async def test_child_sessions_include_archived(
    client: httpx.AsyncClient,
    db_uri: str,
) -> None:
    """``include_archived`` surfaces an archived child and its flag."""
    session = await create_test_session(client, name="archive-child-list")
    parent_id = session["id"]
    conv_store = SqlAlchemyConversationStore(db_uri)
    child = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=parent_id,
        agent_id=session["agent_id"],
    )

    # The child's parent forces the idle deferral, so keep the settle short.
    with (
        patch.object(_sessions_facade, "_ARCHIVE_IDLE_SETTLE_S", 0.05),
        patch.object(_sessions_facade, "_ARCHIVE_IDLE_POLL_S", 0.02),
        patch.object(_sessions_facade, "_ARCHIVE_IDLE_MAX_WAIT_S", 20.0),
    ):
        archived = await client.patch(f"/v1/sessions/{child.id}", json={"archived": True})
        assert archived.status_code == 200
        await _drain_detached_stops()

    default = await client.get(f"/v1/sessions/{parent_id}/child_sessions")
    assert default.status_code == 200
    assert [row["id"] for row in default.json()["data"]] == []

    included = await client.get(
        f"/v1/sessions/{parent_id}/child_sessions",
        params={"include_archived": "true"},
    )
    assert included.status_code == 200
    rows = {row["id"]: row for row in included.json()["data"]}
    assert rows[child.id]["archived"] is True
