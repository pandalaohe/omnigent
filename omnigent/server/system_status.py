"""Server-side system-status hub for the resource monitor.

One hub per server keeps, per workspace, an inventory of hosts (state,
latest snapshot, a per-minute history) plus the server process's own
points. Hosts push snapshots over their tunnel; lifecycle callbacks keep
the inventory current. A fixed tick closes each host's minute bucket,
trims history, evaluates findings, and bumps a revision that nudges
connected clients when the finding set changes.

History and thresholds live in two small files under
``<data dir>/system-status/`` — no database, no migration, and at most
five minutes of history lost on a crash. History is bounded to 24 h.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from omnigent.host.frames import CAP_RESOURCE_SNAPSHOT, HostResourceSnapshotFrame
from omnigent.server.performance_metrics import (
    ServerMetricsSnapshot,
    ServerPerformanceMetrics,
)

_logger = logging.getLogger(__name__)

HISTORY_TTL_S = 24 * 60 * 60.0
OFFLINE_RED_AFTER_S = 120.0
# A viewer lease older than this no longer keeps a host at the fast cadence.
FAST_LEASE_S = 25.0
# Close the history files every fifth minute-tick.
FLUSH_EVERY_TICKS = 5
# How long sampler-cost samples feed the footer's host CPU estimate.
_OVERHEAD_WINDOW_S = 10 * 60.0

_MEM_CONSECUTIVE_POINTS = 2
_SERVER_5XX_WINDOW_POINTS = 5
_SERVER_5XX_MIN_REQUESTS = 20

_CPU_SUSTAIN_MIN_RANGE = (1, 1440)

_DEFAULT_SETTINGS: dict[str, float] = {
    "cpu_pct": 85.0,
    "cpu_sustain_min": 10,
    "mem_pct": 90.0,
    "disk_pct": 90.0,
    "server_5xx_pct": 5.0,
}

# Rough per-item sizes for the labelled in-memory estimate; the footer
# presents the result as an estimate, not a measurement.
_POINT_ESTIMATE_BYTES = 256
_ROW_ESTIMATE_BYTES = 128

_STATE_ONLINE = "online"
_STATE_OFFLINE = "offline"
_STATE_NEEDS_UPDATE = "needs_update"


@dataclass
class _HostEntry:
    """Server-side state for one ``(workspace, host)`` target."""

    host_id: str
    workspace_id: int
    owner: str | None
    name: str
    state: str
    since: float
    last_snapshot: dict[str, Any] | None = None
    last_runner_count: int = 0
    points: list[dict[str, Any]] = field(default_factory=list)
    minute_buffer: list[dict[str, Any]] = field(default_factory=list)
    # ``(received_at, sampler_cpu_ms, interval_s, monitor_rss_delta)`` for the
    # last 10 minutes; feeds the footer without keeping whole frames around.
    overhead_samples: deque[tuple[float, float, int, int]] = field(
        default_factory=lambda: deque(maxlen=256)
    )


class SystemStatusHub:
    """Inventory, history, findings and persistence for the system-status view.

    All methods run on the server's event loop; the in-memory structures are
    not locked.

    :param data_dir: Directory holding the ``system-status/`` state files.
    :param metrics: Server metrics tracker whose ``last_snapshot`` (written by
        the metrics publisher) supplies the server's own points. ``None``
        disables the server target.
    """

    def __init__(self, data_dir: Path, metrics: ServerPerformanceMetrics | None) -> None:
        self._data_dir = data_dir
        self._metrics = metrics
        self._state_dir = data_dir / "system-status"
        self._history_path = self._state_dir / "history.json"
        self._settings_path = self._state_dir / "settings.json"
        self._started_at = time.time()
        self._entries: dict[tuple[int, str], _HostEntry] = {}
        self._server_points: list[dict[str, Any]] = []
        self._findings: dict[tuple[int | None, str], dict[str, Any]] = {}
        self._viewer_leases: dict[tuple[int, str], float] = {}
        self._settings = self._read_settings()
        self._revision = 0
        self._tick_count = 0
        self._hub_cpu_ms = 0.0
        self._last_server_counters: tuple[int, int] | None = None

    # ── Host lifecycle ───────────────────────────────────────────

    def host_changed(
        self,
        *,
        host_id: str,
        workspace_id: int,
        owner: str | None,
        name: str | None,
        conn_capabilities: list[str] | None,
        now: float,
    ) -> None:
        """Record a host connect / disconnect / capability change.

        ``conn_capabilities`` is ``None`` for a disconnect; otherwise it is
        the hello's capability list, and a missing
        :data:`CAP_RESOURCE_SNAPSHOT` marks the host ``needs_update``.

        :param host_id: Host identifier.
        :param workspace_id: Workspace the connection belongs to.
        :param owner: Host owner, or ``None`` when unknown.
        :param name: Host name from the hello, or ``None`` on a disconnect.
        :param conn_capabilities: Capabilities of the live connection, or
            ``None`` when the host is gone.
        :param now: Current wall-clock time.
        """
        started = time.thread_time()
        try:
            entry = self._entries.get((workspace_id, host_id))
            if entry is None:
                entry = _HostEntry(
                    host_id=host_id,
                    workspace_id=workspace_id,
                    owner=owner,
                    name=name or "",
                    state=_STATE_OFFLINE,
                    since=now,
                )
                self._entries[(workspace_id, host_id)] = entry
            if owner is not None:
                entry.owner = owner
            if name:
                entry.name = name
            if conn_capabilities is None:
                self._set_state(entry, _STATE_OFFLINE, now)
            elif CAP_RESOURCE_SNAPSHOT not in conn_capabilities:
                self._set_state(entry, _STATE_NEEDS_UPDATE, now)
            else:
                self._set_state(entry, _STATE_ONLINE, now)
        finally:
            self._accumulate_cpu(started)

    def ingest(
        self,
        *,
        host_id: str,
        workspace_id: int,
        frame: HostResourceSnapshotFrame,
        now: float,
    ) -> None:
        """Record one host snapshot on the open host's minute bucket.

        :param host_id: Host identifier.
        :param workspace_id: Workspace the host belongs to.
        :param frame: Decoded ``host.resource_snapshot`` frame.
        :param now: Current wall-clock time.
        """
        started = time.thread_time()
        try:
            entry = self._entries.get((workspace_id, host_id))
            if entry is None:
                # A snapshot can beat the connect callback; owner arrives with
                # the next ``host_changed``.
                entry = _HostEntry(
                    host_id=host_id,
                    workspace_id=workspace_id,
                    owner=None,
                    name="",
                    state=_STATE_ONLINE,
                    since=now,
                )
                self._entries[(workspace_id, host_id)] = entry
            self._set_state(entry, _STATE_ONLINE, now)
            snapshot = asdict(frame)
            entry.last_snapshot = snapshot
            entry.last_runner_count = int(frame.runner_count)
            entry.minute_buffer.append({"t": now, "snapshot": snapshot})
            entry.overhead_samples.append(
                (
                    now,
                    float(frame.sampler_cpu_ms),
                    int(frame.interval_s),
                    int(frame.monitor_rss_delta),
                )
            )
            self._prune_overhead(entry, now)
        finally:
            self._accumulate_cpu(started)

    # ── Tick ─────────────────────────────────────────────────────

    def tick(self, now: float) -> bool:
        """Roll one minute forward: points, trim, findings, nudge.

        :param now: Current wall-clock time.
        :returns: ``True`` when the set of ``(finding id, level)`` changed, so
            the caller knows a nudge was published.
        """
        started = time.thread_time()
        try:
            self._tick_count += 1
            if self._metrics is not None and self._metrics.last_snapshot is not None:
                self._append_server_point(now, self._metrics.last_snapshot)
            for entry in self._entries.values():
                self._close_minute(entry, now)
                self._prune_overhead(entry, now)

            cutoff = now - HISTORY_TTL_S
            self._server_points = _points_fresh(self._server_points, cutoff)
            for key, entry in list(self._entries.items()):
                entry.points = _points_fresh(entry.points, cutoff)
                if (
                    not entry.points
                    and entry.state == _STATE_OFFLINE
                    and now - entry.since > HISTORY_TTL_S
                ):
                    del self._entries[key]

            changed = self._evaluate_findings(now)
            if changed is not None:
                self._revision += 1
                self._announce(changed)
            if self._tick_count % FLUSH_EVERY_TICKS == 0:
                self.flush()
            return changed is not None
        finally:
            self._accumulate_cpu(started)

    def _append_server_point(self, now: float, snapshot: ServerMetricsSnapshot) -> None:
        req = 0
        err = 0
        if self._last_server_counters is not None:
            req = max(0, snapshot.total_completed - self._last_server_counters[0])
            err = max(0, snapshot.total_failed - self._last_server_counters[1])
        self._last_server_counters = (snapshot.total_completed, snapshot.total_failed)
        self._server_points.append(
            {
                "t": now,
                "cpu": float(snapshot.process_cpu_percent),
                "rss": int(snapshot.rss_bytes),
                "in_flight": int(snapshot.in_flight),
                "websockets": int(snapshot.active_websockets),
                "req": req,
                "err": err,
                "load1": snapshot.load_average_1m,
                "disk_pct": self._disk_pct(),
            }
        )

    def _disk_pct(self) -> float:
        try:
            import psutil

            usage = psutil.disk_usage(str(self._data_dir))
            if usage.total <= 0:
                return 0.0
            return float(usage.used) / float(usage.total) * 100.0
        except Exception:  # noqa: BLE001 — a missing volume must not fail a tick
            _logger.debug("system status could not read disk usage", exc_info=True)
            return 0.0

    def _close_minute(self, entry: _HostEntry, now: float) -> None:
        buffer = entry.minute_buffer
        if not buffer:
            return
        entry.minute_buffer = []
        cpu_values = [float(sample["snapshot"]["machine"]["cpu_pct"]) for sample in buffer]
        last_machine = buffer[-1]["snapshot"]["machine"]
        session_cpu: dict[str, float] = {}
        for sample in buffer:
            for row in sample["snapshot"]["processes"]:
                session_id = row.get("session_id")
                if session_id is None:
                    continue
                session_cpu[session_id] = session_cpu.get(session_id, 0.0) + float(
                    row.get("cpu_pct", 0.0)
                )
        top = [
            [session_id, cpu_pct]
            for session_id, cpu_pct in sorted(
                session_cpu.items(), key=lambda item: item[1], reverse=True
            )[:3]
        ]
        entry.points.append(
            {
                "t": now,
                "cpu_avg": sum(cpu_values) / len(cpu_values),
                "cpu_max": max(cpu_values),
                "mem_used": int(last_machine["mem_used"]),
                "mem_total": int(last_machine["mem_total"]),
                "disk_pct": _machine_disk_pct(last_machine),
                "load1": last_machine["load1"],
                "top": top,
            }
        )

    def _prune_overhead(self, entry: _HostEntry, now: float) -> None:
        cutoff = now - _OVERHEAD_WINDOW_S
        while entry.overhead_samples and entry.overhead_samples[0][0] < cutoff:
            entry.overhead_samples.popleft()

    # ── Findings ─────────────────────────────────────────────────

    def _evaluate_findings(self, now: float) -> set[int] | None:
        """Rebuild findings, preserving ``since`` while a condition holds.

        :returns: Workspaces to announce to, or ``None`` when the
            ``(id, level)`` set did not change.
        """
        previous = self._findings
        current: dict[tuple[int | None, str], dict[str, Any]] = {}

        server_finding = self._server_5xx_finding(now, previous)
        if server_finding is not None:
            current[(None, server_finding["id"])] = server_finding
        for (workspace_id, _host_id), entry in self._entries.items():
            for finding in self._host_findings(entry, now, previous):
                current[(workspace_id, finding["id"])] = finding

        previous_pairs = {(key, finding["level"]) for key, finding in previous.items()}
        current_pairs = {(key, finding["level"]) for key, finding in current.items()}
        self._findings = current
        if previous_pairs == current_pairs:
            return None

        affected: set[int] = set()
        server_changed = False
        for key, _level in previous_pairs ^ current_pairs:
            workspace_id = key[0]
            if workspace_id is None:
                server_changed = True
            else:
                affected.add(workspace_id)
        if server_changed:
            # A server finding is visible to admins in every workspace that
            # has an inventory entry; with no entries the current workspace
            # still gets the nudge its subscribers may be waiting for.
            affected.update(workspace_id for workspace_id, _host_id in self._entries)
            if not affected:
                from omnigent.db.db_models import current_workspace_id

                affected.add(current_workspace_id())
        return affected

    def _server_5xx_finding(
        self,
        now: float,
        previous: dict[tuple[int | None, str], dict[str, Any]],
    ) -> dict[str, Any] | None:
        points = self._server_points[-_SERVER_5XX_WINDOW_POINTS:]
        if len(points) < _SERVER_5XX_WINDOW_POINTS:
            return None
        total_req = sum(int(point.get("req", 0)) for point in points)
        if total_req < _SERVER_5XX_MIN_REQUESTS:
            return None
        total_err = sum(int(point.get("err", 0)) for point in points)
        ratio = total_err / total_req * 100.0
        if ratio <= self._settings["server_5xx_pct"]:
            return None
        return {
            "id": "server:5xx",
            "target": "server",
            "kind": "5xx",
            "level": "red",
            "since": _carry_since(previous, None, "server:5xx", now),
            "detail": f"{ratio:.1f}% of {total_req} requests failed",
            "top_session": None,
        }

    def _host_findings(
        self,
        entry: _HostEntry,
        now: float,
        previous: dict[tuple[int | None, str], dict[str, Any]],
    ) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        points = entry.points
        target = entry.host_id
        cpu_sustain_min = int(self._settings["cpu_sustain_min"])

        if len(points) >= cpu_sustain_min and all(
            float(point.get("cpu_avg", 0.0)) > self._settings["cpu_pct"]
            for point in points[-cpu_sustain_min:]
        ):
            findings.append(
                {
                    "id": f"{target}:cpu",
                    "target": target,
                    "kind": "cpu",
                    "level": "amber",
                    "since": _carry_since(previous, entry.workspace_id, f"{target}:cpu", now),
                    "detail": (
                        f"cpu above {self._settings['cpu_pct']:g}% for {cpu_sustain_min} minutes"
                    ),
                    "top_session": _top_session(points[-1]),
                }
            )

        recent = points[-_MEM_CONSECUTIVE_POINTS:]
        if len(recent) >= _MEM_CONSECUTIVE_POINTS and all(
            _mem_pct(point) > self._settings["mem_pct"] for point in recent
        ):
            findings.append(
                {
                    "id": f"{target}:mem",
                    "target": target,
                    "kind": "mem",
                    "level": "amber",
                    "since": _carry_since(previous, entry.workspace_id, f"{target}:mem", now),
                    "detail": (
                        f"memory above {self._settings['mem_pct']:g}% for "
                        f"{_MEM_CONSECUTIVE_POINTS} minutes"
                    ),
                    "top_session": _top_session(points[-1]),
                }
            )

        if points and float(points[-1].get("disk_pct", 0.0)) > self._settings["disk_pct"]:
            findings.append(
                {
                    "id": f"{target}:disk",
                    "target": target,
                    "kind": "disk",
                    "level": "amber",
                    "since": _carry_since(previous, entry.workspace_id, f"{target}:disk", now),
                    "detail": f"disk above {self._settings['disk_pct']:g}%",
                    "top_session": _top_session(points[-1]),
                }
            )

        if (
            entry.state == _STATE_OFFLINE
            and entry.last_runner_count >= 1
            and now - entry.since > OFFLINE_RED_AFTER_S
        ):
            findings.append(
                {
                    "id": f"{target}:offline",
                    "target": target,
                    "kind": "offline",
                    "level": "red",
                    "since": _carry_since(
                        previous,
                        entry.workspace_id,
                        f"{target}:offline",
                        entry.since + OFFLINE_RED_AFTER_S,
                    ),
                    "detail": (
                        f"offline for {int(now - entry.since)}s with "
                        f"{entry.last_runner_count} runner(s)"
                    ),
                    "top_session": _top_session(points[-1]) if points else None,
                }
            )
        return findings

    def _announce(self, workspaces: set[int]) -> None:
        from omnigent.db.db_models import workspace_scope
        from omnigent.server.routes._sessions.helpers import announce_system_status_changed

        for workspace_id in workspaces:
            with workspace_scope(workspace_id):
                announce_system_status_changed()

    # ── Reads ────────────────────────────────────────────────────

    def view(
        self,
        *,
        user_id: str | None,
        is_admin: bool,
        own_host_ids: set[str],
        own_offline_hosts: list[dict[str, Any]],
        workspace_id: int,
        summary: bool,
    ) -> dict[str, Any]:
        """Build the system-status view for one caller.

        :param user_id: Caller's user id (``None`` in single-user mode).
        :param is_admin: Whether the caller sees the server card and every
            host in the workspace.
        :param own_host_ids: Host ids from the caller's host-store rows.
        :param own_offline_hosts: Host-store rows with no live connection;
            added when the hub has no entry for them.
        :param workspace_id: Workspace to read.
        :param summary: When true, return only revision / level / findings.
        :returns: The view payload.
        """
        started = time.thread_time()
        try:
            now = time.time()
            visible = [
                entry
                for entry in self._entries.values()
                if entry.workspace_id == workspace_id
                and (is_admin or entry.owner == user_id or entry.host_id in own_host_ids)
            ]
            visible_ids = {entry.host_id for entry in visible}

            findings = [
                finding
                for (finding_workspace, _id), finding in self._findings.items()
                if finding_workspace == workspace_id
                and (is_admin or finding["target"] in visible_ids)
            ]
            if is_admin:
                findings.extend(
                    finding
                    for (finding_workspace, _id), finding in self._findings.items()
                    if finding_workspace is None
                )
            findings.sort(key=lambda finding: (finding["level"] != "red", finding["id"]))
            if any(finding["level"] == "red" for finding in findings):
                level = "red"
            elif findings:
                level = "amber"
            else:
                level = "ok"

            if summary:
                return {"revision": self._revision, "level": level, "findings": findings}

            hosts: list[dict[str, Any]] = [
                {
                    "host_id": entry.host_id,
                    "owner": entry.owner,
                    "name": entry.name,
                    "state": entry.state,
                    "since": entry.since,
                    "last_snapshot": entry.last_snapshot,
                    "last_runner_count": entry.last_runner_count,
                }
                for entry in visible
            ]
            for host in own_offline_hosts:
                host_id = host.get("host_id")
                if not isinstance(host_id, str) or host_id in visible_ids:
                    continue
                hosts.append(
                    {
                        "host_id": host_id,
                        "owner": user_id,
                        "name": str(host.get("name") or host_id),
                        "state": _STATE_OFFLINE,
                        "since": None,
                        "last_snapshot": None,
                        "last_runner_count": 0,
                    }
                )

            server: dict[str, Any] | None = None
            if is_admin:
                server = {
                    "state": _STATE_ONLINE,
                    "since": self._started_at,
                    "last_point": self._server_points[-1] if self._server_points else None,
                }
            return {
                "revision": self._revision,
                "level": level,
                "findings": findings,
                "server": server,
                "hosts": hosts,
                "monitor_overhead": self._monitor_overhead(visible, now),
            }
        finally:
            self._accumulate_cpu(started)

    def history(
        self,
        target: str,
        *,
        user_id: str | None,
        is_admin: bool,
        own_host_ids: set[str],
        workspace_id: int,
    ) -> list[dict[str, Any]] | None:
        """Return one target's 24 h points, or ``None`` when not visible.

        :param target: ``"server"`` or a host id.
        :param user_id: Caller's user id (``None`` in single-user mode).
        :param is_admin: Whether the caller may read the server target.
        :param own_host_ids: Host ids from the caller's host-store rows.
        :param workspace_id: Workspace to read.
        :returns: Age-filtered points, or ``None`` for an unknown /
            inaccessible target.
        """
        cutoff = time.time() - HISTORY_TTL_S
        if target == "server":
            if not is_admin:
                return None
            return _points_fresh(self._server_points, cutoff)
        entry = self._entries.get((workspace_id, target))
        if entry is None:
            return None
        if not (is_admin or entry.owner == user_id or target in own_host_ids):
            return None
        return _points_fresh(entry.points, cutoff)

    def mark_viewer(self, host_ids: list[str], workspace_id: int, now: float) -> None:
        """Record a viewer lease so the fast-mode loop keeps sampling.

        :param host_ids: Hosts the viewer is currently watching.
        :param workspace_id: Workspace of those hosts.
        :param now: Current wall-clock time.
        """
        for host_id in host_ids:
            self._viewer_leases[(workspace_id, host_id)] = now

    def fast_hosts(self, now: float) -> list[tuple[int, str]]:
        """Return leased ``(workspace_id, host_id)`` pairs, pruning stale ones.

        :param now: Current wall-clock time.
        """
        cutoff = now - FAST_LEASE_S
        for key in [key for key, seen in self._viewer_leases.items() if seen < cutoff]:
            del self._viewer_leases[key]
        return list(self._viewer_leases)

    def _monitor_overhead(self, entries: list[_HostEntry], now: float) -> dict[str, Any]:
        wall_s = max(0.0, now - self._started_at)
        cpu_pct = (self._hub_cpu_ms / 1000.0) / wall_s * 100.0 if wall_s > 0 else 0.0
        point_count = len(self._server_points)
        row_count = 0
        for entry in self._entries.values():
            point_count += len(entry.points) + len(entry.minute_buffer)
            snapshot = entry.last_snapshot
            if snapshot is not None:
                processes = snapshot.get("processes")
                if isinstance(processes, list):
                    row_count += len(processes)
        hosts: dict[str, Any] = {}
        for entry in entries:
            samples = [
                sample
                for sample in entry.overhead_samples
                if sample[0] >= now - _OVERHEAD_WINDOW_S
            ]
            if samples:
                mean_cpu_ms = sum(sample[1] for sample in samples) / len(samples)
                interval_s = samples[-1][2] or 60
                host_cpu_pct = mean_cpu_ms / (interval_s * 1000.0) * 100.0
                rss_delta = samples[-1][3]
            else:
                host_cpu_pct = 0.0
                rss_delta = 0
            hosts[entry.host_id] = {"cpu_pct": host_cpu_pct, "rss_delta": rss_delta}
        return {
            "server": {
                "cpu_pct": cpu_pct,
                "mem_estimate_bytes": point_count * _POINT_ESTIMATE_BYTES
                + row_count * _ROW_ESTIMATE_BYTES,
                "estimated": True,
            },
            "hosts": hosts,
        }

    # ── Settings ─────────────────────────────────────────────────

    def get_settings(self) -> dict[str, float]:
        """Return a copy of the effective thresholds."""
        return dict(self._settings)

    def put_settings(self, payload: dict[str, Any]) -> dict[str, float]:
        """Validate, store and return the thresholds.

        :param payload: Partial or full settings mapping.
        :raises ValueError: On an unknown key, a non-number, or a value
            outside the key's range: ``(0, 100]`` for the ``*_pct`` keys,
            whole minutes in ``[1, 1440]`` for ``cpu_sustain_min``.
        """
        settings = dict(self._settings)
        for key, value in payload.items():
            settings[key] = _coerce_setting(key, value)
        self._settings = settings
        self._write_settings()
        return dict(settings)

    def _read_settings(self) -> dict[str, float]:
        payload = _read_json(self._settings_path)
        settings = dict(_DEFAULT_SETTINGS)
        if not isinstance(payload, dict):
            return settings
        for key, value in payload.items():
            try:
                settings[key] = _coerce_setting(key, value)
            except ValueError:
                continue
        return settings

    def _write_settings(self) -> None:
        try:
            _atomic_write_json(self._settings_path, dict(self._settings))
        except OSError:
            _logger.warning("system-status settings write failed", exc_info=True)

    # ── Persistence ──────────────────────────────────────────────

    def load(self) -> None:
        """Load history at startup, dropping points older than 24 h."""
        payload = _read_json(self._history_path)
        if not isinstance(payload, dict):
            return
        now = time.time()
        cutoff = now - HISTORY_TTL_S

        server = payload.get("server")
        if isinstance(server, list):
            self._server_points = _points_fresh(
                [point for point in server if isinstance(point, dict)], cutoff
            )

        hosts = payload.get("hosts")
        if not isinstance(hosts, dict):
            return
        for key, value in hosts.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            workspace_text, separator, host_id = key.partition(":")
            if not separator:
                continue
            try:
                workspace_id = int(workspace_text)
            except ValueError:
                continue
            points = _points_fresh(
                [point for point in value.get("points", []) if isinstance(point, dict)],
                cutoff,
            )
            if not points:
                continue
            owner = value.get("owner")
            name = value.get("name")
            self._entries[(workspace_id, host_id)] = _HostEntry(
                host_id=host_id,
                workspace_id=workspace_id,
                owner=owner if isinstance(owner, str) else None,
                name=name if isinstance(name, str) else "",
                state=_STATE_OFFLINE,
                since=float(points[-1].get("t", now)),
                points=points,
            )

    def flush(self) -> None:
        """Write history atomically; a failure is logged and ignored."""
        payload = {
            "version": 1,
            "server": self._server_points,
            "hosts": {
                f"{workspace_id}:{host_id}": {
                    "owner": entry.owner,
                    "name": entry.name,
                    "points": entry.points,
                }
                for (workspace_id, host_id), entry in self._entries.items()
            },
        }
        try:
            _atomic_write_json(self._history_path, payload)
        except OSError:
            _logger.warning("system-status history write failed", exc_info=True)

    # ── Self-cost ────────────────────────────────────────────────

    def _accumulate_cpu(self, started: float) -> None:
        self._hub_cpu_ms += max(0.0, (time.thread_time() - started) * 1000.0)

    def _set_state(self, entry: _HostEntry, state: str, now: float) -> None:
        if entry.state != state:
            entry.state = state
            entry.since = now


def _points_fresh(points: list[dict[str, Any]], cutoff: float) -> list[dict[str, Any]]:
    """Drop points whose ``t`` is older than *cutoff*."""
    return [point for point in points if float(point.get("t", 0.0)) >= cutoff]


def _coerce_setting(key: str, value: Any) -> float:
    """Return one validated settings value.

    :raises ValueError: On an unknown key, a non-number, a fractional
        ``cpu_sustain_min``, or a value outside the key's range.
    """
    if key not in _DEFAULT_SETTINGS:
        raise ValueError(f"unknown system-status setting {key!r}")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"system-status setting {key!r} must be a number")
    numeric = float(value)
    if key == "cpu_sustain_min":
        if not numeric.is_integer():
            raise ValueError(f"system-status setting {key!r} must be a whole number of minutes")
        low, high = _CPU_SUSTAIN_MIN_RANGE
        if not low <= int(numeric) <= high:
            raise ValueError(f"system-status setting {key!r} must be in [{low}, {high}]")
        return int(numeric)
    if not 0.0 < numeric <= 100.0:
        raise ValueError(f"system-status setting {key!r} must be in (0, 100]")
    return numeric


def _mem_pct(point: dict[str, Any]) -> float:
    total = float(point.get("mem_total", 0.0))
    if total <= 0:
        return 0.0
    return float(point.get("mem_used", 0.0)) / total * 100.0


def _machine_disk_pct(machine: dict[str, Any]) -> float:
    total = float(machine.get("disk_total", 0.0))
    if total <= 0:
        return 0.0
    return float(machine.get("disk_used", 0.0)) / total * 100.0


def _top_session(point: dict[str, Any]) -> str | None:
    top = point.get("top")
    if not isinstance(top, list) or not top:
        return None
    first = top[0]
    if isinstance(first, list) and first:
        return str(first[0])
    return None


def _carry_since(
    previous: dict[tuple[int | None, str], dict[str, Any]],
    workspace_id: int | None,
    finding_id: str,
    fallback: float,
) -> float:
    """Keep a holding finding's original ``since`` across ticks."""
    old = previous.get((workspace_id, finding_id))
    if old is None:
        return fallback
    return float(old["since"])


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        _logger.warning("ignoring corrupt system-status file %s", path, exc_info=True)
        return None


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
