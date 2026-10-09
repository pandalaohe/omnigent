"""In-process registry of sound-alert ringer connections.

The web app's session-updates WebSocket doubles as the alert transport:
each connection announces its device with a ``hello`` frame, and a client
that detects a transition claims the alert over
``POST /v1/me/sound-alerts/claim``. The server keeps one alert id claimed
per user — so exactly one device plays each edge even when several clients
detect it — and picks the ringer connection that device should be: the one
most recently used, else the account's primary device, else the most
recently connected.

State is process-local and ephemeral, mirroring
:mod:`omnigent.server.presence`: connections die with the WebSocket that
registered them and claimed ids age out. The lock guards reads from the
claim route that may interleave with the WebSocket reader's writes.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from omnigent.db.workspace_cache import WorkspaceScopedCache

# A connection with user activity this recent is "the device the user is
# using"; anything older falls back to the account's primary device.
_ACTIVE_WINDOW_S = 300.0
# Per-owner claimed-id memory: newest 1000 claims, no older than 24 h.
_MAX_CLAIMED_IDS = 1000
_CLAIMED_TTL_S = 24 * 60 * 60.0
_USER_STOP_WINDOW_S = 60.0
_MAX_USER_STOP_NOTES = 200


@dataclass
class _Connection:
    """
    One open session-updates connection that can be asked to ring.

    :param conn_id: Server-minted connection id, e.g. ``"a3f9c2…"``.
    :param device_id: Stable client device id, e.g. ``"8f14…"``.
    :param device_label: Short human label, e.g. ``"Mac desktop app"``.
    :param can_ring: Whether this device is willing to play alerts
        (device switch on and not the iOS shell).
    :param connected_at: ``time.monotonic()`` at registration.
    :param last_activity: Last user activity instant, or ``None`` until
        the first ``activity`` frame.
    :param send: Async callable delivering one frame on this connection.
    """

    conn_id: str
    device_id: str
    device_label: str
    can_ring: bool
    connected_at: float
    last_activity: float | None
    send: Callable[[dict[str, Any]], Awaitable[None]]


# owner -> conn_id -> connection.
_connections: WorkspaceScopedCache[str, dict[str, _Connection]] = WorkspaceScopedCache()
# owner -> alert_id -> claimed_at (insertion-ordered for pruning).
_claimed: WorkspaceScopedCache[str, OrderedDict[str, float]] = WorkspaceScopedCache()
# owner -> session_id -> stopped_at (insertion-ordered for pruning).
_user_stops: WorkspaceScopedCache[str, OrderedDict[str, float]] = WorkspaceScopedCache()

_lock = threading.Lock()


def register(
    owner: str,
    conn_id: str,
    *,
    device_id: str,
    device_label: str,
    can_ring: bool,
    send: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    """
    Add or update one ringer connection for an owner.

    A second ``hello`` on the same connection updates its device fields
    but keeps ``connected_at`` and ``last_activity`` — a device that
    re-announces itself (e.g. after toggling its switch) is still the
    same session, so its recency signal must survive the re-announce.

    :param owner: User id, or ``RESERVED_USER_LOCAL`` when there is none.
    :param conn_id: Server-minted id unique to the connection.
    :param device_id: Stable client device id.
    :param device_label: Short human label for settings.
    :param can_ring: Whether the device may be chosen to play.
    :param send: Async callable delivering one frame over the connection.
    """
    with _lock:
        records = _connections.setdefault(owner, {})
        existing = records.get(conn_id)
        if existing is not None:
            existing.device_id = device_id
            existing.device_label = device_label
            existing.can_ring = can_ring
            existing.send = send
            return
        records[conn_id] = _Connection(
            conn_id=conn_id,
            device_id=device_id,
            device_label=device_label,
            can_ring=can_ring,
            connected_at=time.monotonic(),
            last_activity=None,
            send=send,
        )


def unregister(owner: str, conn_id: str) -> None:
    """
    Drop one connection, e.g. when its WebSocket closes.

    :param owner: Owner the connection registered under.
    :param conn_id: Connection id passed to :func:`register`.
    """
    with _lock:
        records = _connections.get(owner)
        if records is None:
            return
        records.pop(conn_id, None)
        if not records:
            _connections.pop(owner, None)


def touch(owner: str, conn_id: str) -> None:
    """
    Record user activity on one connection.

    :param owner: Owner the connection registered under.
    :param conn_id: Connection id passed to :func:`register`.
    """
    with _lock:
        record = _connections.get(owner, {}).get(conn_id)
        if record is not None:
            record.last_activity = time.monotonic()


def _prune_user_stops(stops: OrderedDict[str, float], now: float) -> None:
    """Drop stop notes past the age or count cap."""
    cutoff = now - _USER_STOP_WINDOW_S
    while stops and (len(stops) > _MAX_USER_STOP_NOTES or next(iter(stops.values())) < cutoff):
        stops.popitem(last=False)


def note_user_stop(owner: str, session_id: str) -> None:
    """
    Record the owner's recent stop or interrupt for one session.

    :param owner: User id, or ``RESERVED_USER_LOCAL`` when there is none.
    :param session_id: Session the user asked to stop.
    """
    with _lock:
        now = time.monotonic()
        stops = _user_stops.setdefault(owner, OrderedDict())
        stops.pop(session_id, None)
        stops[session_id] = now
        _prune_user_stops(stops, now)


def forget_user_stop(owner: str, session_id: str) -> None:
    """
    Remove a stop note when the requested stop did not land.

    :param owner: Owner passed to :func:`note_user_stop`.
    :param session_id: Session whose stop note should be removed.
    """
    with _lock:
        stops = _user_stops.get(owner)
        if stops is None:
            return
        _prune_user_stops(stops, time.monotonic())
        stops.pop(session_id, None)
        if not stops:
            _user_stops.pop(owner, None)


def _activity_basis(record: _Connection) -> float:
    """Most recent activity signal: last activity, else connect time."""
    return record.last_activity if record.last_activity is not None else record.connected_at


def _choose_ringer_locked(
    owner: str, primary_device_id: str | None, now: float
) -> _Connection | None:
    """
    Pick the connection that should play one alert.

    Prefers the most recently active connection (within
    :data:`_ACTIVE_WINDOW_S`), then the primary device's most recent
    connection when it is idle, then the most recent connection of any
    device. Ties break on the greatest conn id so the pick is stable.

    :param owner: Owner whose connections to choose from.
    :param primary_device_id: Account fallback device, or ``None``.
    :param now: Current ``time.monotonic()`` instant.
    :returns: The chosen connection, or ``None`` when none can ring.
    """
    records = [record for record in _connections.get(owner, {}).values() if record.can_ring]
    if not records:
        return None
    active = [
        record
        for record in records
        if record.last_activity is not None and now - record.last_activity <= _ACTIVE_WINDOW_S
    ]
    if active:
        return max(active, key=lambda record: (_activity_basis(record), record.conn_id))
    if primary_device_id is not None:
        primary = [record for record in records if record.device_id == primary_device_id]
        if primary:
            return max(primary, key=lambda record: (_activity_basis(record), record.conn_id))
    return max(records, key=lambda record: (_activity_basis(record), record.conn_id))


def choose_ringer(owner: str, primary_device_id: str | None, now: float) -> _Connection | None:
    """
    Pick the connection that should play one alert (public entry point).

    :param owner: Owner whose connections to choose from.
    :param primary_device_id: Account fallback device, or ``None``.
    :param now: Current ``time.monotonic()`` instant.
    :returns: The chosen connection, or ``None`` when none can ring.
    """
    with _lock:
        return _choose_ringer_locked(owner, primary_device_id, now)


def _prune_claims(claims: OrderedDict[str, float], now: float) -> None:
    """Drop the oldest claims past the count or age cap."""
    cutoff = now - _CLAIMED_TTL_S
    while claims and (len(claims) > _MAX_CLAIMED_IDS or next(iter(claims.values())) < cutoff):
        claims.popitem(last=False)


async def claim(
    owner: str,
    *,
    alert_id: str,
    session_id: str,
    level: str,
    primary_device_id: str | None,
) -> bool:
    """
    Claim one alert and deliver it to a single ringer connection.

    The first claim for an alert id wins; later claims are dropped without
    delivery, so overlapping detectors ring once. The id stays claimed
    even when nothing is deliverable — it was handled, not left pending.

    :param owner: User id, or ``RESERVED_USER_LOCAL`` when there is none.
    :param alert_id: Client-stable alert identity, e.g.
        ``"conv_a:done:200"``.
    :param session_id: Session the alert belongs to.
    :param level: Transition level: ``"done"``, ``"error"``, or
        ``"needs_response"``.
    :param primary_device_id: Account fallback device, or ``None``.
    :returns: ``True`` when a ringer connection accepted the frame.
    """
    now = time.monotonic()
    with _lock:
        stops = _user_stops.get(owner)
        if stops is not None:
            _prune_user_stops(stops, now)
            if not stops:
                _user_stops.pop(owner, None)
                stops = None
        claims = _claimed.setdefault(owner, OrderedDict())
        if alert_id in claims:
            return False
        claims[alert_id] = now
        _prune_claims(claims, now)
        if level in ("done", "error") and stops is not None and session_id in stops:
            return False
        record = _choose_ringer_locked(owner, primary_device_id, now)
    frame = {"type": "sound_alert", "alert_id": alert_id, "session_id": session_id, "level": level}
    # A dead connection is unregistered and skipped once; past that, further
    # retries would keep hammering connections known to be gone.
    for _ in range(2):
        if record is None:
            return False
        try:
            await record.send(frame)
            return True
        except Exception:  # noqa: BLE001 - a dead connection must not block the rest.
            unregister(owner, record.conn_id)
            with _lock:
                record = _choose_ringer_locked(owner, primary_device_id, now)
    return False


def reset_for_tests() -> None:
    """
    Clear all connections, claims, and stop notes.

    Test-isolation hook mirroring :func:`omnigent.server.presence.reset_for_tests`:
    the registry is module-global, so an entry leaked by one test is
    visible to every later test in the same process.
    """
    with _lock:
        _connections.clear()
        _claimed.clear()
        _user_stops.clear()
