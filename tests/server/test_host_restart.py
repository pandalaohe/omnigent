"""Restart phase deadlines, preview binding and reliable result delivery."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from omnigent.host.frames import HostHelloFrame, HostRestartResultFrame, decode_host_frame
from omnigent.server.host_registry import HostRegistry


@pytest.mark.asyncio
async def test_slow_restart_ack_fits_rpc_and_stop_phase(monkeypatch):
    from omnigent.server import host_restart

    now = [0.0]
    registry = HostRegistry()
    restarts = registry.restarts
    restarts.clock = lambda: now[0]
    conn = registry.register(
        "host_restart", AsyncMock(), HostHelloFrame("old", 1, "test", started_at=1), None
    )
    budgets = []
    replies = []

    async def scaled_wait(future, timeout):
        budgets.append(timeout)
        return await asyncio.wait_for(future, timeout=timeout / 100)

    monkeypatch.setattr(
        host_restart, "asyncio", SimpleNamespace(**{**vars(asyncio), "wait_for": scaled_wait})
    )

    def send(connection, raw):
        frame = decode_host_frame(raw)

        async def later():
            await asyncio.sleep(0.4)  # 40 virtual seconds, beyond the old RPC/stop cutoff.
            now[0] += 40
            connection.pending_restarts[frame.request_id].set_result(
                HostRestartResultFrame(frame.request_id, True, supervisor="launchd")
            )

        replies.append(asyncio.create_task(later()))

    monkeypatch.setattr(registry, "send_text", send)
    try:
        preview = await restarts.preview(
            conn, owner=None, requester=None, force=True, drain_timeout_s=0, caller_on_host=False
        )
        started = now[0]
        record = await restarts.restart(
            preview["preview_id"],
            conn,
            owner=None,
            requester=None,
            force=True,
            drain_timeout_s=0,
            notify=AsyncMock(),
        )
        restarts.advance(record)
        assert record.phase != "failed"
        assert budgets == [60, 60]
        assert record.stop_deadline - started > budgets[-1]
        now[0] = record.stop_deadline
        restarts.advance(record)
        assert record.phase == "failed"
        assert record.reason == "host did not restart"
        assert not conn.pending_restarts
    finally:
        for task in replies:
            task.cancel()
        await asyncio.gather(*replies, return_exceptions=True)
        await restarts.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("reconnect", [True, False])
async def test_restart_deadlines_and_notice_retry(monkeypatch, reconnect):
    now = [100.0]
    registry = HostRegistry()
    restarts = registry.restarts
    restarts.clock = lambda: now[0]
    conn = registry.register(
        "host_restart",
        AsyncMock(),
        HostHelloFrame("old", 1, "test", started_at=1, supervised="launchd"),
        None,
    )

    def reply(connection, raw):
        frame = decode_host_frame(raw)
        connection.pending_restarts[frame.request_id].set_result(
            HostRestartResultFrame(
                frame.request_id,
                True,
                supervisor="launchd",
                running_sessions=["requester", "busy"],
                runner_count=2,
            )
        )

    monkeypatch.setattr(registry, "send_text", reply)
    monkeypatch.setattr("omnigent.server.host_restart.NOTICE_RETRY_S", 0)
    preview = await restarts.preview(
        conn,
        owner=None,
        requester="requester",
        force=False,
        drain_timeout_s=120,
        caller_on_host=True,
    )
    assert restarts.active == {}
    notify = AsyncMock(side_effect=[False, True])
    record = await restarts.restart(
        preview["preview_id"],
        conn,
        owner=None,
        requester="requester",
        force=False,
        drain_timeout_s=120,
        notify=notify,
    )
    now[0] = 210  # 110 seconds draining must not spend the reconnect budget.
    restarts.advance(record)
    assert record.status()["phase"] == "draining"
    second_preview = await restarts.preview(
        conn,
        owner=None,
        requester="requester",
        force=False,
        drain_timeout_s=120,
        caller_on_host=True,
    )
    with pytest.raises(HTTPException, match="restart pending"):
        await restarts.restart(
            second_preview["preview_id"],
            conn,
            owner=None,
            requester="requester",
            force=False,
            drain_timeout_s=120,
            notify=notify,
        )
    restarts.stopping(
        conn,
        HostRestartResultFrame(
            record.request_id, True, running_sessions=["requester"], phase="stopping"
        ),
    )
    assert record.status()["phase"] == "stopping"
    registry.deregister(conn.host_id)
    assert record.status()["phase"] == "waiting_reconnect"
    if reconnect:
        now[0] = 225
        registry.register(
            conn.host_id, AsyncMock(), HostHelloFrame("new", 1, "test", started_at=2), None
        )
        assert record.status() == {
            "phase": "done",
            "reason": None,
            "offline_seconds": 15,
            "version": "new",
            "cut_sessions": ["requester"],
        }
    else:
        now[0] = 509
        restarts.advance(record)
        assert record.status()["phase"] == "waiting_reconnect"
        now[0] = 510
        restarts.advance(record)
        assert record.status()["phase"] == "failed"
        assert record.reason == "host did not reconnect"
    for _ in range(100):
        if notify.await_count == 2:
            break
        await asyncio.sleep(0.01)
    assert notify.await_count == 2
    assert restarts.active == {}
    await restarts.shutdown()


@pytest.mark.asyncio
async def test_preview_expiry_options_and_stopping_timeout(monkeypatch):
    now = [0.0]
    registry = HostRegistry()
    restarts = registry.restarts
    restarts.clock = lambda: now[0]
    conn = registry.register(
        "host_restart", AsyncMock(), HostHelloFrame("old", 1, "test", started_at=1), None
    )

    def reply(connection, raw):
        frame = decode_host_frame(raw)
        connection.pending_restarts[frame.request_id].set_result(
            HostRestartResultFrame(frame.request_id, True, supervisor="systemd")
        )

    monkeypatch.setattr(registry, "send_text", reply)
    preview = await restarts.preview(
        conn, owner="owner", requester=None, force=False, drain_timeout_s=120, caller_on_host=False
    )
    for owner, force in [("other", False), ("owner", True)]:
        with pytest.raises(HTTPException) as error:
            await restarts.restart(
                preview["preview_id"],
                conn,
                owner=owner,
                requester=None,
                force=force,
                drain_timeout_s=120,
                notify=AsyncMock(),
            )
        assert error.value.status_code == 409
    now[0] = 120
    with pytest.raises(HTTPException):
        restarts.get_preview(preview["preview_id"], conn, "owner")
    preview = await restarts.preview(
        conn, owner="owner", requester=None, force=False, drain_timeout_s=120, caller_on_host=False
    )
    record = await restarts.restart(
        preview["preview_id"],
        conn,
        owner="owner",
        requester=None,
        force=False,
        drain_timeout_s=120,
        notify=AsyncMock(),
    )
    now[0] = 304
    restarts.advance(record)
    assert record.phase == "draining"
    now[0] = 305
    restarts.advance(record)
    assert record.status()["phase"] == "failed"
    assert record.reason == "host did not restart"
    await restarts.shutdown()
