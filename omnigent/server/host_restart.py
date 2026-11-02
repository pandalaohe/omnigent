"""In-memory restart previews and phase tracking, scoped to a host's workspace."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from omnigent.host.frames import HostRestartFrame, HostRestartResultFrame, encode_host_frame
from omnigent.host.restart import REQUESTER_GRACE_S

if TYPE_CHECKING:
    from omnigent.server.host_registry import HostConnection, HostRegistry

_logger = logging.getLogger(__name__)
PREVIEW_TTL_S = 120
RECONNECT_TIMEOUT_S = 120
STOP_TIMEOUT_S = 30
RPC_TIMEOUT_S = 30
NOTICE_RETRY_S = 5


@dataclass
class RestartPreview:
    conn: HostConnection
    owner: str | None
    requester: str | None
    force: bool
    drain_timeout_s: int
    expires: float
    payload: dict[str, Any]
    tool_approval_id: str | None = None
    tool_approved: bool = False


@dataclass
class RestartRecord:
    request_id: str
    preview: RestartPreview
    stop_deadline: float
    phase: str = "draining"
    reason: str | None = None
    dropped_at: float | None = None
    offline_seconds: int | None = None
    version: str | None = None
    cut_sessions: list[str] = field(default_factory=list)

    def status(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "reason": self.reason,
            "offline_seconds": self.offline_seconds,
            "version": self.version,
            "cut_sessions": list(self.cut_sessions),
        }


class HostRestarts:
    """Own one restart per host and retain its result through notice delivery."""

    def __init__(
        self, registry: HostRegistry, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.registry = registry
        self.clock = clock
        self.previews: dict[str, RestartPreview] = {}
        self.records: dict[tuple[int, str, str], RestartRecord] = {}
        self.active: dict[tuple[int, str], RestartRecord] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    async def _rpc(self, conn: HostConnection, frame: HostRestartFrame) -> HostRestartResultFrame:
        future: asyncio.Future[HostRestartResultFrame] = asyncio.get_running_loop().create_future()
        conn.pending_restarts[frame.request_id] = future
        try:
            self.registry.send_text(conn, encode_host_frame(frame))
            return await asyncio.wait_for(future, RPC_TIMEOUT_S)
        except (TimeoutError, ConnectionError) as exc:
            raise HTTPException(409, "host did not acknowledge restart") from exc
        finally:
            conn.pending_restarts.pop(frame.request_id, None)

    async def preview(
        self,
        conn: HostConnection,
        *,
        owner: str | None,
        requester: str | None,
        force: bool,
        drain_timeout_s: int,
        caller_on_host: bool,
    ) -> dict[str, Any]:
        result = await self._rpc(conn, HostRestartFrame(secrets.token_hex(16)))
        if not result.accepted or result.supervisor is None:
            raise HTTPException(409, result.reason or "host is not supervised")
        now = self.clock()
        self.previews = {key: value for key, value in self.previews.items() if value.expires > now}
        preview_id = secrets.token_hex(16)
        payload = {
            "preview_id": preview_id,
            "host_id": conn.host_id,
            "supervisor": result.supervisor,
            "running_sessions": result.running_sessions,
            "runner_count": result.runner_count,
            "caller_on_host": caller_on_host,
        }
        self.previews[preview_id] = RestartPreview(
            conn, owner, requester, force, drain_timeout_s, now + PREVIEW_TTL_S, payload
        )
        return payload

    def get_preview(
        self, preview_id: str | None, conn: HostConnection, owner: str | None
    ) -> RestartPreview:
        preview = self.previews.get(preview_id or "")
        if (
            preview is None
            or preview.expires <= self.clock()
            or preview.conn is not conn
            or preview.owner != owner
        ):
            raise HTTPException(409, "preview required")
        return preview

    async def restart(
        self,
        preview_id: str | None,
        conn: HostConnection,
        *,
        owner: str | None,
        requester: str | None,
        force: bool,
        drain_timeout_s: int,
        notify: Callable[[RestartRecord], Awaitable[bool]],
    ) -> RestartRecord:
        preview = self.get_preview(preview_id, conn, owner)
        if (preview.requester, preview.force, preview.drain_timeout_s) != (
            requester,
            force,
            drain_timeout_s,
        ):
            raise HTTPException(409, "preview required for these restart options")
        key = (conn.workspace_id, conn.host_id)
        if key in self.active:
            raise HTTPException(409, "restart pending")
        record = RestartRecord(
            secrets.token_hex(16),
            preview,
            self.clock()
            + (0 if force else drain_timeout_s)
            + (REQUESTER_GRACE_S if requester else 0)
            + STOP_TIMEOUT_S,
            cut_sessions=list(preview.payload["running_sessions"]),
        )
        self.active[key] = record
        self.records[(*key, record.request_id)] = record
        self.previews.pop(preview_id or "", None)
        try:
            result = await self._rpc(
                conn, HostRestartFrame(record.request_id, False, requester, drain_timeout_s, force)
            )
        except BaseException:
            # An uncertain acknowledgement can still be followed by a restart.
            # Keep monitoring until its phase deadline rather than permit a duplicate.
            self._track(self._monitor(record, notify))
            raise
        if not result.accepted:
            self._finish(record, "failed", result.reason or "restart refused")
            self._track(self._monitor(record, notify))
            raise HTTPException(409, result.reason or "restart refused")
        self._track(self._monitor(record, notify))
        _logger.info(
            "Host restart admitted: host=%s requester=%s owner=%s request=%s",
            conn.host_id,
            requester,
            owner,
            record.request_id,
        )
        return record

    def _track(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def stopping(self, conn: HostConnection, result: HostRestartResultFrame) -> None:
        record = self.active.get((conn.workspace_id, conn.host_id))
        if (
            record is not None
            and record.preview.conn is conn
            and record.request_id == result.request_id
        ):
            if record.phase == "draining":
                record.phase = "stopping"
                record.cut_sessions = list(result.running_sessions)

    def disconnected(self, conn: HostConnection) -> None:
        record = self.active.get((conn.workspace_id, conn.host_id))
        if record is not None and record.preview.conn is conn and record.dropped_at is None:
            record.dropped_at = self.clock()
            record.phase = "waiting_reconnect"

    def connected(self, conn: HostConnection) -> None:
        record = self.active.get((conn.workspace_id, conn.host_id))
        if record is None or record.preview.conn is conn:
            return
        previous = record.preview.conn.hello.started_at
        started = conn.hello.started_at
        if previous is None or started is None or started <= previous:
            return
        self.disconnected(record.preview.conn)
        assert record.dropped_at is not None
        if self.clock() - record.dropped_at <= RECONNECT_TIMEOUT_S:
            record.offline_seconds = int(self.clock() - record.dropped_at)
            record.version = conn.hello.version
            self._finish(record, "done")

    def advance(self, record: RestartRecord) -> None:
        """Evaluate separate stop and reconnect deadlines."""
        if record.phase in {"done", "failed"}:
            return
        now = self.clock()
        if record.dropped_at is not None:
            if now - record.dropped_at >= RECONNECT_TIMEOUT_S:
                record.offline_seconds = int(now - record.dropped_at)
                self._finish(record, "failed", "host did not reconnect")
        elif now >= record.stop_deadline:
            self._finish(record, "failed", "host did not restart")

    def _finish(self, record: RestartRecord, phase: str, reason: str | None = None) -> None:
        record.phase, record.reason = phase, reason
        conn = record.preview.conn
        self.active.pop((conn.workspace_id, conn.host_id), None)

    async def _monitor(
        self, record: RestartRecord, notify: Callable[[RestartRecord], Awaitable[bool]]
    ) -> None:
        while record.phase not in {"done", "failed"}:
            self.advance(record)
            if record.phase not in {"done", "failed"}:
                await asyncio.sleep(0.25)
        if record.preview.requester is None:
            return
        while True:
            try:
                if await asyncio.wait_for(notify(record), 120):
                    return
            except Exception:
                _logger.warning(
                    "Restart notice delivery pending: %s", record.request_id, exc_info=True
                )
            await asyncio.sleep(NOTICE_RETRY_S)

    async def shutdown(self) -> None:
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
