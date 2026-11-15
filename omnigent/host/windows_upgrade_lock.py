"""Serialize upgrade owner changes while allowing the uv/helper handoff."""

from __future__ import annotations

import contextlib
import importlib
import json
import math
import os
import time
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import TypedDict

import psutil

from omnigent._platform import IS_WINDOWS
from omnigent.util.atomic_json import atomic_write_json


class Owner(TypedDict):
    pid: int
    create_time: float


def process_owner(pid: int) -> Owner:
    return {"pid": pid, "create_time": psutil.Process(pid).create_time()}


@contextlib.contextmanager
def owner_guard(path: Path) -> Iterator[None]:
    # Never unlink the guard: every contender must lock the same file object.
    with path.with_suffix(path.suffix + ".guard").open("a+b") as stream:
        if IS_WINDOWS:
            msvcrt = importlib.import_module("msvcrt")

            if not os.fstat(stream.fileno()).st_size:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)

            def unlock() -> None:
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)

            def unlock() -> None:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

        try:
            yield
        finally:
            stream.seek(0)
            unlock()


def _read_owner(path: Path) -> Owner | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or type(value.get("pid")) is not int
            or value["pid"] <= 0
            or type(value.get("create_time")) not in (int, float)
            or not math.isfinite(value["create_time"])
        ):
            raise ValueError("invalid owner identity")
        return {"pid": value["pid"], "create_time": float(value["create_time"])}
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError) as exc:
        raise RuntimeError(f"Unreadable upgrade lock: {path}") from exc


def _recover_damaged_owner(path: Path) -> Owner:
    """Replace damaged metadata only after an old, inactive upgrade is proven."""
    try:
        if time.time() - path.stat().st_mtime < 1800:
            raise RuntimeError("lock is less than 30 minutes old")
        for process in psutil.process_iter():
            try:
                if "omnigent.host.windows_upgrade_helper" in process.cmdline():
                    raise RuntimeError("an upgrade helper is still running")
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
    except (OSError, psutil.Error, RuntimeError) as exc:
        raise RuntimeError(
            f"Cannot recover upgrade lock {path}: {exc}. "
            "Check running Windows upgrade helpers, process inspection permissions, "
            "and that the lock is at least 30 minutes old."
        ) from exc
    current = process_owner(os.getpid())
    atomic_write_json(path, dict(current))
    return current


def _owner_live(owner: Owner) -> bool:
    try:
        return process_owner(owner["pid"]) == owner
    except psutil.NoSuchProcess:
        return False


def acquire_lock(path: Path, *, handoff_from: Collection[int] = ()) -> Owner:
    with owner_guard(path):
        try:
            owner = _read_owner(path)
        except RuntimeError:
            return _recover_damaged_owner(path)
        if owner is not None:
            if _owner_live(owner):
                if owner["pid"] not in handoff_from:
                    raise RuntimeError("Another Windows upgrade is already running.")
                current = process_owner(os.getpid())
                atomic_write_json(path, dict(current))
                return current
            path.unlink()
        current = process_owner(os.getpid())
        with path.open("x", encoding="utf-8") as stream:
            json.dump(current, stream)
        return current


def transfer_lock(path: Path, expected: Owner, pid: int) -> Owner:
    with owner_guard(path):
        if _read_owner(path) != expected:
            raise RuntimeError("Upgrade lock owner changed before handoff.")
        next_owner = process_owner(pid)
        atomic_write_json(path, dict(next_owner))
        return next_owner


def release_lock(path: Path, expected: Owner) -> None:
    with owner_guard(path):
        if _read_owner(path) == expected:
            path.unlink()
