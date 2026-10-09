"""Private Codex homes and explicitly granted, session-owned skill directories.

Homes stay outside the workspace. Skills live in a separate directory granted
only to their owning session; no sandbox discovers grants by scanning temp homes.
"""

from __future__ import annotations

import contextlib
import math
import os
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import psutil

# Prefix of each per-conversation home created under the staging root. Also
# what identifies an Omnigent-private codex home to nested launches (see
# ``_is_omnigent_private_codex_home`` in ``codex_executor``).
CODEX_HOME_PREFIX = "omnigent-codex-home-"
CODEX_SKILLS_PREFIX = "omnigent-codex-skills-"
CODEX_HOME_OWNER_SUFFIX = ".owner"
CODEX_HOME_ORPHAN_RETENTION_SECONDS = 7 * 24 * 60 * 60


def _staging_root_path() -> Path:
    if not hasattr(os, "getuid"):
        # OS temp cleaners delete unopened files inside a live home, so
        # Windows keeps homes in Omnigent's data dir.
        from omnigent.process_logging import data_dir

        return data_dir().resolve() / "codex-homes"
    # The shared temp root must not route private homes through another user.
    return Path(tempfile.gettempdir()).resolve() / f"omnigent-codex-homes-{os.getuid()}"


def codex_home_staging_root() -> Path:
    """Create-and-return the root that per-conversation CODEX_HOMEs live under.

    :returns: The per-user staging root, verified private (``0o700``) to the
        current user.
    :raises OSError: When the root cannot be created (e.g. an unwritable
        system temp dir), or exists but is not a real directory owned by the
        current user without group/other write access. Callers fall back to a
        plain unpredictable temp-dir home. Skills use a separate directory,
        so this fallback does not change their sandbox visibility.
    """
    root = _staging_root_path()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not hasattr(os, "getuid"):
        # Windows temp dirs are already per-user; POSIX ownership semantics
        # don't apply.
        return root
    # ``mkdir(exist_ok=True)`` silently accepts a pre-existing path — even a
    # symlink to a directory — and the name is predictable in a shared temp
    # dir, so another principal could have planted it first. Codex later
    # reads ``config.toml``/``auth.json`` from homes under this root by
    # pathname, so refuse anything that is not a real directory we own.
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode) or root_stat.st_uid != os.getuid():
        raise OSError(
            f"codex home staging root {root} is not a directory owned by the current user"
        )
    if root_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        # A pre-existing root may carry a permissive umask-derived mode;
        # tighten it — failing loud when we cannot — so no other principal
        # can rename homes out from under live sessions.
        root.chmod(0o700)
    return root


def _codex_home_owner_path(home: Path) -> Path:
    # Beside the home so a partial delete never loses it and its writers cannot forge it.
    return home.with_name(home.name + CODEX_HOME_OWNER_SUFFIX)


def write_codex_home_owner(home: Path) -> None:
    """Record the process that created *home* for the orphan sweep.

    The marker stores the pid plus the process creation time, so a reused pid
    reads as a dead owner rather than a live one. It sits beside *home*, not
    inside it.

    :param home: The private CODEX_HOME to mark.
    :raises OSError: When the marker name already exists.
    """
    pid = os.getpid()
    create_time = psutil.Process(pid).create_time()
    # O_CREAT|O_EXCL refuses an existing name, a planted symlink included.
    fd = os.open(_codex_home_owner_path(home), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as marker:
        marker.write(f"{pid} {create_time!r}")


def _codex_home_owner_alive(home: Path) -> bool | None:
    """Return whether *home*'s recorded owner process is still alive.

    The marker lives beside *home*; a missing, malformed, or non-positive
    marker is unknown.

    :param home: A home possibly carrying a sibling owner marker.
    :returns: ``True``/``False`` for a live/dead owner, or ``None`` when the
        marker is missing, unreadable, or unparseable.
    """
    try:
        pid_text, create_time_text = (
            _codex_home_owner_path(home).read_text(encoding="utf-8").split()
        )
        pid = int(pid_text)
        recorded = float(create_time_text)
    except (OSError, ValueError):
        return None
    if pid <= 0 or not math.isfinite(recorded) or recorded <= 0:
        return None
    try:
        actual = psutil.Process(pid).create_time()
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # Cannot tell; never delete a possibly live home.
        return True
    # Linux derives creation times from a boot time that can shift by about a second.
    return abs(actual - recorded) < 1.0


def _codex_home_idle_since(home: Path, cutoff: float) -> bool:
    """Return whether every entry under *home* was last modified by *cutoff*.

    :param home: A home with no live owner marker.
    :param cutoff: Epoch seconds; an entry newer than this means "not idle".
    :returns: ``True`` only when every file and directory, including *home*
        itself, has an mtime at or before *cutoff*. Any error keeps the home.
    """
    walk_error = False

    def _record_error(_error: OSError) -> None:
        nonlocal walk_error
        walk_error = True

    try:
        if os.lstat(home).st_mtime > cutoff:
            return False
        for dirpath, dirnames, filenames in os.walk(
            home, followlinks=False, onerror=_record_error
        ):
            for name in [*dirnames, *filenames]:
                if os.lstat(Path(dirpath) / name).st_mtime > cutoff:
                    return False
            # Never walk into a link or junction that leaves the home.
            dirnames[:] = [
                name
                for name in dirnames
                if not os.path.islink(Path(dirpath) / name)
                and not os.path.isjunction(Path(dirpath) / name)
            ]
    except OSError:
        return False
    return not walk_error


def _rmtree_retry_read_only(func: Callable[..., object], path: str, exc: BaseException) -> None:
    """``shutil.rmtree`` ``onexc``: clear a read-only bit and retry the removal.

    Windows refuses to unlink a read-only file (a git clone's pack files are). POSIX
    unlink ignores the file's mode, and a pathname retry there would bypass rmtree's
    fd-based link safety, so the handler re-raises off Windows.
    A link or junction is never chmod'ed, so no target outside the tree is
    touched; any other failure re-raises *exc*.
    """
    if (
        sys.platform != "win32"
        or func not in (os.unlink, os.rmdir)
        or not isinstance(exc, PermissionError)
        or os.path.islink(path)
        or os.path.isjunction(path)
    ):
        raise exc
    os.chmod(path, stat.S_IMODE(os.lstat(path).st_mode) | stat.S_IWRITE)
    func(path)


def _rmtree_retry_read_only_or_skip(
    func: Callable[..., object], path: str, exc: BaseException
) -> None:
    # ``ignore_errors=True`` once the read-only retry has had its turn.
    with contextlib.suppress(OSError):
        _rmtree_retry_read_only(func, path, exc)


def remove_codex_home(home: Path) -> bool:
    """Remove *home* without ever following a link out of it.

    ``shutil.rmtree`` never follows a symlink or junction out of the tree: it
    refuses a linked root and, since Python 3.8, unlinks Windows junctions
    rather than entering them. On Windows a read-only file is made writable
    before its removal is retried; a link never is. The sibling owner marker is
    removed last, so a home Windows cannot fully delete yet (a file still open
    in an exiting process) keeps its dead-owner marker for the next pass.

    :param home: A private CODEX_HOME.
    :returns: ``True`` when the home was fully removed.
    """
    shutil.rmtree(home, onexc=_rmtree_retry_read_only_or_skip)
    if os.path.lexists(home):
        return False
    with contextlib.suppress(OSError):
        _codex_home_owner_path(home).unlink(missing_ok=True)
    return True


def reap_orphaned_codex_homes(root: Path | None = None) -> int:
    """Remove Codex homes left behind by a hard-killed runner.

    A home whose recorded owner process is gone is removed; an unmarked home
    is removed only once every entry in it has been idle for
    ``CODEX_HOME_ORPHAN_RETENTION_SECONDS``. A live owner's home is never
    touched. A sibling owner marker whose home is already gone is removed too.

    :param root: Staging root to sweep; ``None`` uses
        :func:`codex_home_staging_root`.
    :returns: The number of homes removed.
    """
    if root is None:
        try:
            root = codex_home_staging_root()
        except OSError:
            return 0
    try:
        if root.is_symlink() or root.is_junction():
            return 0
        entries = list(root.iterdir())
    except OSError:
        return 0
    reaped = 0
    for entry in entries:
        if not entry.name.startswith(CODEX_HOME_PREFIX):
            continue
        try:
            if entry.name.endswith(CODEX_HOME_OWNER_SUFFIX):
                home = entry.with_name(entry.name[: -len(CODEX_HOME_OWNER_SUFFIX)])
                if not os.path.lexists(home):
                    with contextlib.suppress(OSError):
                        entry.unlink()
                continue
            if entry.is_symlink() or entry.is_junction() or not entry.is_dir():
                continue
            alive = _codex_home_owner_alive(entry)
            if alive is True:
                continue
            if alive is None and not _codex_home_idle_since(
                entry, time.time() - CODEX_HOME_ORPHAN_RETENTION_SECONDS
            ):
                continue
            if remove_codex_home(entry):
                reaped += 1
        except (OSError, psutil.Error):
            continue
    return reaped


def prepare_codex_skills_dir(path: Path) -> Path:
    """Validate and empty an owned skill directory without replacing its inode.

    Existing sandbox mounts keep this directory across harness restarts. Clear
    old contents so a narrower skill filter cannot retain previously loaded skills.
    """
    root = path.parent.resolve() / path.name
    root_stat = root.lstat()
    if (
        not root.name.startswith(CODEX_SKILLS_PREFIX)
        or not stat.S_ISDIR(root_stat.st_mode)
        or root.is_junction()
    ):
        raise OSError("Codex skills must use a dedicated session staging directory")
    if hasattr(os, "getuid") and (
        root_stat.st_uid != os.getuid() or stat.S_IMODE(root_stat.st_mode) != 0o700
    ):
        raise OSError("Codex skills staging directory must be private to the current user")
    for child in root.iterdir():
        if child.is_junction():
            child.rmdir()
        elif child.is_symlink() or not child.is_dir():
            child.unlink()
        else:
            shutil.rmtree(child, onexc=_rmtree_retry_read_only)
    return root


def link_codex_skills_dir(link_path: Path, skills_dir: Path) -> None:
    """Link a skill-discovery entry to a directory without copying its target.

    Codex publishes resolved skill paths, keeping read grants tied to the target.

    :param link_path: Home-level ``skills`` entry or individual skill; must not exist yet.
    :param skills_dir: Session-owned skills directory or selected source directory.
    :raises OSError: When the platform can create neither a symlink nor, on
        Windows, a directory junction.
    """
    try:
        link_path.symlink_to(skills_dir, target_is_directory=True)
    except OSError:
        if sys.platform == "win32":
            # Windows refuses symlinks without Developer Mode or the symlink
            # privilege; a junction needs neither and resolves the same way.
            import _winapi

            _winapi.CreateJunction(str(skills_dir), str(link_path))
        else:
            raise
