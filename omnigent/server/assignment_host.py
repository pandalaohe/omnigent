"""
Server-side proxies for the host assignment tunnel frames.

Like ``_host_worktree``: enqueue a ``host.assignment_prepare`` /
``host.assignment_release`` frame, register a future on the host
connection, and await the result with a timeout. The host (not the
server) runs git. Unlike the worktree proxies, a host-reported failure
is returned as a result frame rather than raised — the coordinator
needs the stable ``error_code`` to decide whether the assignment waits
with a visible reason.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from omnigent.host.frames import (
    HostAssignmentPrepareFrame,
    HostAssignmentPrepareResultFrame,
    HostAssignmentReleaseFrame,
    HostAssignmentReleaseResultFrame,
    encode_host_frame,
)
from omnigent.server.host_registry import HostConnection, HostRegistry

_logger = logging.getLogger(__name__)

# Above the host's own fetch budget (240 s) so the host's specific error
# surfaces instead of a generic server-side timeout.
_ASSIGNMENT_PREPARE_TIMEOUT_S: float = 300.0

_ASSIGNMENT_RELEASE_TIMEOUT_S: float = 120.0


class AssignmentProxyError(Exception):
    """
    Raised when the host cannot be reached for an assignment operation,
    or answers with a malformed result.

    A host-reported ``status: "failed"`` / ``"partial"`` is NOT this —
    it is returned as a result frame for the coordinator to interpret.

    :param message: Human-readable error suitable for the caller,
        e.g. ``"host 'abc' connection lost during assignment
        preparation"``.
    """

    def __init__(self, message: str) -> None:
        """
        Initialize with the user-facing error message.

        :param message: Error string surfaced to the API caller.
        """
        super().__init__(message)
        self.message = message


class AssignmentHostUnavailableError(AssignmentProxyError):
    """
    Raised when the host can't be reached for an assignment operation.

    Connection loss or no reply within the timeout — an infrastructure
    condition, not a host-reported failure. Subclasses
    :class:`AssignmentProxyError` so callers that catch the base type
    still catch it.
    """


async def _await_assignment_result(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    pending: dict[str, asyncio.Future[dict[str, Any]]],
    request_id: str,
    frame: str,
    op: str,
    timeout: float,
) -> dict[str, Any]:
    """
    Send an assignment frame and await its matching result over the tunnel.

    Shared plumbing for the prepare/release proxies: register a future on
    ``pending`` keyed by ``request_id``, enqueue ``frame``, await the
    reply, and clean up on every path.

    :param host_registry: Registry used to enqueue the outbound frame.
    :param host_conn: Live host connection.
    :param pending: The connection's pending-future map for this op
        (``pending_assignment_prepares`` or
        ``pending_assignment_releases``).
    :param request_id: Correlation id already embedded in ``frame``.
    :param frame: Encoded host frame to send.
    :param op: Short label for error messages, e.g.
        ``"assignment preparation"``.
    :param timeout: Seconds to wait for the host's reply.
    :returns: The host's result dict (``status`` plus op-specific
        fields).
    :raises AssignmentHostUnavailableError: On connection loss or no
        reply within ``timeout``.
    """
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    pending[request_id] = future
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError as exc:
            raise AssignmentHostUnavailableError(
                f"host '{host_conn.host_id}' connection lost during {op}"
            ) from exc
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise AssignmentHostUnavailableError(
                f"host '{host_conn.host_id}' did not respond to {op} within "
                f"{timeout:.0f}s (it may be running an older version "
                "that does not support assignments)"
            ) from exc
    finally:
        pending.pop(request_id, None)


def _optional_str(value: object) -> str | None:
    """Return ``value`` when it is a string, else ``None``."""
    return value if isinstance(value, str) else None


async def prepare_assignment_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    frame: HostAssignmentPrepareFrame,
) -> HostAssignmentPrepareResultFrame:
    """
    Send a ``host.assignment_prepare`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection to prepare the worktrees on.
    :param frame: The prepare request, carrying its own ``request_id``
        for correlation.
    :returns: The host's result frame — ``status "ok"`` with the
        repository → directory map, or ``status "failed"`` with a
        stable ``error_code`` for the coordinator to interpret.
    :raises AssignmentHostUnavailableError: If the host connection drops
        or doesn't respond within
        :data:`_ASSIGNMENT_PREPARE_TIMEOUT_S`.
    :raises AssignmentProxyError: If the host returns a malformed
        result.
    """
    encoded = encode_host_frame(frame)
    result = await _await_assignment_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_assignment_prepares,
        request_id=frame.request_id,
        frame=encoded,
        op="assignment preparation",
        timeout=_ASSIGNMENT_PREPARE_TIMEOUT_S,
    )
    status = result.get("status")
    directories = result.get("directories", {})
    if (
        not isinstance(status, str)
        or not isinstance(directories, dict)
        or not all(
            isinstance(name, str) and isinstance(path, str) for name, path in directories.items()
        )
    ):
        raise AssignmentProxyError("host returned an incomplete assignment prepare result")
    return HostAssignmentPrepareResultFrame(
        request_id=frame.request_id,
        status=status,
        directories=dict(directories),
        error_code=_optional_str(result.get("error_code")),
        error=_optional_str(result.get("error")),
        repository_name=_optional_str(result.get("repository_name")),
    )


async def release_assignment_on_host(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    frame: HostAssignmentReleaseFrame,
) -> HostAssignmentReleaseResultFrame:
    """
    Send a ``host.assignment_release`` frame and await the result.

    :param host_registry: Server-side registry; used to enqueue the
        outbound frame on the host's send queue.
    :param host_conn: Live host connection that owns the worktrees.
    :param frame: The release request, carrying its own ``request_id``
        for correlation.
    :returns: The host's result frame — ``status "ok"`` when every
        worktree is gone, otherwise ``"partial"`` with per-repository
        reasons.
    :raises AssignmentHostUnavailableError: If the host connection drops
        or doesn't respond within
        :data:`_ASSIGNMENT_RELEASE_TIMEOUT_S`.
    :raises AssignmentProxyError: If the host returns a malformed
        result.
    """
    encoded = encode_host_frame(frame)
    result = await _await_assignment_result(
        host_registry=host_registry,
        host_conn=host_conn,
        pending=host_conn.pending_assignment_releases,
        request_id=frame.request_id,
        frame=encoded,
        op="assignment release",
        timeout=_ASSIGNMENT_RELEASE_TIMEOUT_S,
    )
    status = result.get("status")
    removed = result.get("removed", [])
    failures = result.get("failures", {})
    if (
        not isinstance(status, str)
        or not isinstance(removed, list)
        or not all(isinstance(name, str) for name in removed)
        or not isinstance(failures, dict)
        or not all(
            isinstance(name, str) and isinstance(reason, str) for name, reason in failures.items()
        )
    ):
        raise AssignmentProxyError("host returned an incomplete assignment release result")
    return HostAssignmentReleaseResultFrame(
        request_id=frame.request_id,
        status=status,
        removed=list(removed),
        failures=dict(failures),
    )


def host_supports_assignments(host_conn: HostConnection) -> bool:
    """
    Return whether the host advertised the assignments capability.

    Hosts that connected before the capability existed decode
    ``assignments`` as ``False``; the coordinator never sends them a
    prepare frame, so a claim never turns into a full frame timeout.

    :param host_conn: Live host connection.
    :returns: ``True`` when the host's hello set ``assignments``.
    """
    return host_conn.hello.assignments
