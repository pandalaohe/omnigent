"""Cooperate with private-home publishers and preserve concurrent config edits."""

from __future__ import annotations

import errno
import inspect
import json
import logging
import os
import shutil
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from typing import Any, ParamSpec, TypeVar, cast

_logger = logging.getLogger(__name__)
_TIMEOUT = 15.0
_POLL = 0.1
_local = threading.local()
P = ParamSpec("P")
R = TypeVar("R")


class ConfigChanged(Exception):
    pass


class HomeBusy(Exception):
    """The home cannot be updated now; retry on the next synchronization."""


def _windows_lock(fd: int, *, release: bool = False) -> None:
    import msvcrt

    os.lseek(fd, 0, os.SEEK_SET)
    mode = msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK  # pyrefly: ignore[missing-attribute]
    msvcrt.locking(fd, mode, 1)  # pyrefly: ignore[missing-attribute]


def _lock(fd: int, *, release: bool = False) -> None:
    if os.name == "nt":
        _windows_lock(fd, release=release)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _snapshots() -> dict[Path, bytes | None]:
    if not hasattr(_local, "snapshots"):
        _local.snapshots = {}
    return _local.snapshots


@contextmanager
def home_lock(config_path: Path) -> Iterator[None]:
    """Hold the publisher's exclusive lock, reentrant within one thread."""
    path = config_path.parent.resolve() / config_path.name
    snapshots = _snapshots()
    if path in snapshots:
        yield
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.parent / ".collab-generations" / "home.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        deadline = time.monotonic() + _TIMEOUT
        while True:
            try:
                _lock(handle.fileno())
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise HomeBusy from error
                time.sleep(min(_POLL, remaining))
        try:
            # Readers skip the locked byte zero and reconstruct the leading brace.
            handle.seek(0)
            handle.truncate()
            holder = {
                "writer_id": f"pid:{os.getpid()}",
                "pid": os.getpid(),
                "acquired_at": time.time(),
            }
            handle.write((json.dumps(holder, sort_keys=True) + "\n").encode())
            handle.flush()
            snapshots[path] = _read(path)
            yield
        finally:
            snapshots.pop(path, None)
            _lock(handle.fileno(), release=True)


def config_writer(
    argument: str, *, home: bool = False, busy: Any = None
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Lock before reading and rerun a writer if its checked replacement conflicts."""

    def decorate(function: Callable[P, R]) -> Callable[P, R]:
        signature = inspect.signature(function)

        @wraps(function)
        def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            bound = signature.bind(*args, **kwargs)
            # Retryable writers may accept a one-shot stream of launch overrides.
            for name, value in bound.arguments.items():
                if isinstance(value, Iterator):
                    bound.arguments[name] = tuple(value)
            target = Path(bound.arguments[argument])
            path = target / "config.toml" if home else target
            path = path.parent.resolve() / path.name
            if path in _snapshots():
                return function(*bound.args, **bound.kwargs)
            try:
                with home_lock(path):
                    for _ in range(3):
                        try:
                            return function(*bound.args, **bound.kwargs)
                        except ConfigChanged:
                            _snapshots()[path] = _read(path)
                _logger.warning(
                    "Codex config sync conflict: changed repeatedly; retry on next sync"
                )
            except HomeBusy:
                _logger.warning("Codex home lock busy; retry on next sync")
            return cast(R, busy(bound.arguments) if callable(busy) else busy)

        return wrapped

    return decorate


def replace_config(temporary: str | Path, path: Path) -> None:
    """Compare against the read under the home lock immediately before replacing."""
    key = path.parent.resolve() / path.name
    snapshots = _snapshots()
    if key not in snapshots:
        raise RuntimeError("Codex config replacement requires the home lock")
    if _read(path) != snapshots[key]:
        raise ConfigChanged
    if snapshots[key] is None:
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise ConfigChanged from None
    else:
        os.replace(temporary, path)
    snapshots[key] = _read(path)


def write_config(path: Path, content: str) -> None:
    """Atomically publish a private copy, preserving its file permissions."""
    descriptor, temporary = tempfile.mkstemp(prefix=".config.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        replace_config(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def remove_config(path: Path) -> None:
    """Discard a probe's old copy while retaining its home lock."""
    key = path.parent.resolve() / path.name
    if _read(path) != _snapshots()[key]:
        raise ConfigChanged
    path.unlink(missing_ok=True)
    _snapshots()[key] = None


@contextmanager
def config_journal(path: Path) -> Iterator[None]:
    """Roll back a prepared sidecar when the config compare rejects publication."""
    before = _read(path)
    try:
        yield
    except ConfigChanged:
        if before is None:
            path.unlink(missing_ok=True)
        else:
            descriptor, temporary = tempfile.mkstemp(prefix=".journal.", dir=path.parent)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(before)
                os.replace(temporary, path)
            finally:
                Path(temporary).unlink(missing_ok=True)
        raise


def copy_config(source: Path, path: Path) -> None:
    """Publish an initial copy without changing its bytes or metadata."""
    descriptor, temporary = tempfile.mkstemp(prefix=".config.", dir=path.parent)
    os.close(descriptor)
    try:
        shutil.copy2(source, temporary)
        replace_config(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
