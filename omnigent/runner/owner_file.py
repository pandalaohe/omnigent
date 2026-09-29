"""Runner-written ownership records for the host resource sampler.

The host cannot infer which conversation owns a harness or tmux process from
the process table alone (the zygote forks harnesses, and a tmux server
daemonizes away), so the runner records each pid's conversation id at spawn
in a per-runner file under ``<data dir>/run/owners/``. The host sampler reads
them and deletes files whose runner is gone.

Every function here is best-effort: a monitor must never break the process
launch path, so errors are logged at debug and swallowed.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psutil

from omnigent.process_logging import data_dir

_logger = logging.getLogger(__name__)

OwnerKind = Literal["harness", "tmux"]

_OWNERS_RELATIVE_PATH = Path("run") / "owners"

# create_time is a float stored through JSON; a tiny tolerance absorbs any
# representation drift while still rejecting a reused pid's different time.
_CREATE_TIME_TOLERANCE_S = 1e-6

# Writers are threads of one runner process; the lock keeps the
# read-modify-write of that runner's file from interleaving. The host only
# reads (the file is replaced atomically), so it needs no lock.
_write_lock = threading.Lock()


@dataclass(frozen=True)
class OwnerEntry:
    """One live ownership record: a process and the conversation it serves.

    :param pid: Owned process id, e.g. a harness or tmux server pid.
    :param conversation_id: Conversation the process belongs to.
    :param kind: ``"harness"`` or ``"tmux"``.
    """

    pid: int
    conversation_id: str
    kind: str


def _owners_dir(root: Path) -> Path:
    return root / _OWNERS_RELATIVE_PATH


def write_owner_entry(*, pid: int, conversation_id: str, kind: OwnerKind) -> None:
    """Record *pid*'s owning conversation in this runner's owner file.

    Each write reloads the file, drops entries whose ``(pid, create_time)``
    is no longer live, then adds or replaces this entry and atomically
    replaces the file. A failure is logged at debug and ignored — a runner
    must never fail a launch because the monitor could not record it.

    :param pid: The owned process, e.g. a harness or tmux server pid.
    :param conversation_id: Conversation the process serves.
    :param kind: ``"harness"`` or ``"tmux"``.
    """
    try:
        with _write_lock:
            path = _owners_dir(data_dir()) / f"{os.getpid()}.json"
            runner_create_time = psutil.Process(os.getpid()).create_time()
            entry_create_time = psutil.Process(pid).create_time()
            kept: list[dict[str, object]] = []
            for entry in _load_entries(path):
                entry_pid = entry.get("pid")
                if entry_pid == pid:
                    continue
                if _process_is_live(entry_pid, entry.get("create_time")):
                    kept.append(entry)
            kept.append(
                {
                    "pid": pid,
                    "create_time": entry_create_time,
                    "conversation_id": conversation_id,
                    "kind": kind,
                }
            )
            _atomic_write_json(
                path,
                {
                    "runner_pid": os.getpid(),
                    "runner_create_time": runner_create_time,
                    "entries": kept,
                },
            )
    except Exception:  # noqa: BLE001 — the monitor must not break a spawn
        _logger.debug("owner-file write failed", exc_info=True)


def read_owner_entries(data_dir: Path) -> dict[int, OwnerEntry]:
    """Return ``pid -> OwnerEntry`` for every live ownership record.

    Files whose runner ``(pid, create_time)`` is no longer live are deleted;
    entries whose process is gone or whose pid was reused are ignored.

    :param data_dir: Data directory holding ``run/owners/``.
    :returns: Live pid → record mapping, empty on any failure.
    """
    try:
        return _read_owner_records(data_dir)
    except Exception:  # noqa: BLE001 — the monitor must not fail its caller
        _logger.debug("owner-file read failed", exc_info=True)
        return {}


def _read_owner_records(data_dir: Path) -> dict[int, OwnerEntry]:
    records: dict[int, OwnerEntry] = {}
    directory = _owners_dir(data_dir)
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return records
    for path in paths:
        payload = _load_payload(path)
        if not isinstance(payload, dict):
            continue
        runner_pid = payload.get("runner_pid")
        if not _process_is_live(runner_pid, payload.get("runner_create_time")):
            # A crashed runner left this file behind; its entries can never
            # be validated again.
            with contextlib.suppress(OSError):
                path.unlink()
            continue
        entries = payload.get("entries")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            pid = entry.get("pid")
            conversation_id = entry.get("conversation_id")
            kind = entry.get("kind")
            if not isinstance(pid, int) or isinstance(pid, bool):
                continue
            if not isinstance(conversation_id, str) or not isinstance(kind, str):
                continue
            if not _process_is_live(pid, entry.get("create_time")):
                continue
            records[pid] = OwnerEntry(pid=pid, conversation_id=conversation_id, kind=kind)
    return records


def _load_payload(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _load_entries(path: Path) -> list[dict[str, object]]:
    payload = _load_payload(path)
    if not isinstance(payload, dict):
        return []
    entries = payload.get("entries")
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict)]


def _process_is_live(pid: object, create_time: object) -> bool:
    """Return whether *pid* is running with the recorded create time."""
    if not isinstance(pid, int) or isinstance(pid, bool):
        return False
    if isinstance(create_time, bool) or not isinstance(create_time, (int, float)):
        return False
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return False
        return abs(process.create_time() - float(create_time)) < _CREATE_TIME_TOLERANCE_S
    except (psutil.Error, OSError, ValueError):
        return False


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
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
