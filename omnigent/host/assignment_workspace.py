"""Host-side assignment worktree preparation and release.

Serves ``host.assignment_prepare`` / ``host.assignment_release``: fetch
pinned input refs by explicit refspec, verify commits and manifests, and
add (or remove) one detached worktree per repository under
``<source>/.omnigent/worktrees/<assignment_id>/<repository_name>``.

All git runs through :func:`omnigent.host.git_worktree._run_git` as argv
lists, never a shell. The attempt never checks out into the bound working
copy itself, so the user's uncommitted work there is never touched.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from omnigent.host.frames import (
    HostAssignmentPrepareRepository,
    HostAssignmentPrepareResultFrame,
    HostAssignmentReleaseRepository,
    HostAssignmentReleaseResultFrame,
)
from omnigent.host.git_worktree import WorktreeError, _run_git
from omnigent.project_context import (
    THIS_REPOSITORY_KEY,
    ManifestError,
    ProjectManifest,
    manifest_digest,
    parse_manifest,
    required_paths,
    validate_manifest_path,
)

_logger = logging.getLogger(__name__)

# A retried prepare may overlap the first after a server timeout, and git's
# failed-add cleanup frees a path another call can recreate, so path ownership
# only holds while one assignment's calls are serialized.
_assignment_locks_guard = threading.Lock()
_assignment_locks: dict[str, threading.Lock] = {}


def _lock_for_assignment(assignment_id: str) -> threading.Lock:
    """Return the process-wide lock serializing one assignment's calls."""
    with _assignment_locks_guard:
        lock = _assignment_locks.get(assignment_id)
        if lock is None:
            lock = threading.Lock()
            _assignment_locks[assignment_id] = lock
        return lock


# A fetch pulls an unbounded object count, unlike the local rev-parse /
# worktree ops sized for the default git timeout. Stays inside the
# server's 300 s prepare budget for the common single-repository case.
_FETCH_TIMEOUT_S = 240.0

# Git stderr folded into failure messages is trimmed so one pathological
# remote cannot blow up the result frame.
_ERROR_DETAIL_LIMIT = 500

_EXCLUDE_LINE = "/.omnigent/"

# Host-side boundaries (the frame decoder only checks strings, so the host
# re-validates every server value before any git call). The id and name
# rules mirror the server's dispatch validators.
_ASSIGNMENT_ID_RE = re.compile(r"[0-9a-f]{32}")
_REPOSITORY_NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,100}")
_INPUT_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")


def _valid_repository_name(name: str) -> bool:
    """Whether ``name`` is one safe ref-path segment like the server requires."""
    return (
        _REPOSITORY_NAME_RE.fullmatch(name) is not None
        and not name.startswith(".")
        and not name.endswith((".", ".lock"))
        and ".." not in name
    )


def _expected_input_ref(assignment_id: str, repository_name: str) -> str:
    """The only input ref this host will fetch for the repository."""
    return f"refs/omnigent/assignments/{assignment_id}/input/{repository_name}"


def _contained_inside(candidate: str, root: str) -> bool:
    """Whether realpath ``candidate`` is at or inside realpath ``root``."""
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:
        return False


@dataclass
class _PreparedRepository:
    """One repository prepared by this call, ready for context checks."""

    toplevel: str
    worktree_path: str
    commit: str
    manifest: ProjectManifest


def _detail(stderr: str | None, returncode: int, label: str) -> str:
    """Fold a failed git command into a short failure message."""
    trimmed = (stderr or "").strip()[:_ERROR_DETAIL_LIMIT]
    suffix = f": {trimmed}" if trimmed else ""
    return f"{label} (exit {returncode}){suffix}"


def _same_path(first: str, second: str) -> bool:
    """Compare two paths after canonicalisation.

    ``git rev-parse --show-toplevel`` prints forward slashes even on
    Windows, so plain string equality with ``os.path.realpath`` would
    never hold there.
    """
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def _worktree_path(source_toplevel: str, assignment_id: str, repository_name: str) -> str:
    """Derive the worktree path for one repository.

    Derived, never configurable: it stays inside the bound directory so
    it is inside the agent's path boundary whenever the binding is.
    """
    return os.path.join(source_toplevel, ".omnigent", "worktrees", assignment_id, repository_name)


def _git_common_dir(cwd: str) -> str:
    """Resolve the shared git dir for the repository containing ``cwd``."""
    result = _run_git(["rev-parse", "--git-common-dir"], cwd=cwd)
    if result.returncode != 0:
        raise WorktreeError(f"could not resolve git dir for {cwd}")
    common = result.stdout.strip()
    if not os.path.isabs(common):
        common = os.path.join(cwd, common)
    return os.path.normpath(common)


def _ensure_excluded(common_dir: str) -> None:
    """Add ``/.omnigent/`` to the repository's ``info/exclude``.

    The worktrees live inside the bound directory; without the exclude
    entry the host's ``git status`` would show them as untracked noise.
    ``.gitignore`` is a tracked project file and is never touched.
    """
    exclude = Path(common_dir) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    raw = exclude.read_bytes() if exclude.exists() else b""
    if _EXCLUDE_LINE.encode("utf-8") in raw.splitlines():
        return
    with exclude.open("ab") as handle:
        if raw and not raw.endswith(b"\n"):
            handle.write(b"\n")
        handle.write(_EXCLUDE_LINE.encode("utf-8") + b"\n")


def _listed_as_detached(worktree_list: str, path: str) -> bool:
    """Whether ``path`` is registered as a detached worktree in porcelain output."""
    current: str | None = None
    detached = False
    for line in worktree_list.splitlines():
        if line.startswith("worktree "):
            if current is not None and _same_path(current, path):
                return detached
            current = line[len("worktree ") :].strip()
            detached = False
        elif line == "detached":
            detached = True
        elif line == "" and current is not None:
            if _same_path(current, path):
                return detached
            current = None
    if current is None or not detached:
        return False
    return _same_path(current, path)


def _is_reusable_worktree(path: str, toplevel: str, common_dir: str, commit: str) -> bool:
    """Return whether ``path`` is already the worktree this call would add."""
    try:
        # rev-parse HEAD alone succeeds from any subdir of the checkout, so
        # the toplevel must equal the candidate itself, not just share a repo.
        shown = _run_git(["rev-parse", "--show-toplevel"], cwd=path)
        if shown.returncode != 0 or not _same_path(shown.stdout.strip(), path):
            return False
        if not _same_path(_git_common_dir(path), common_dir):
            return False
        listed = _run_git(["worktree", "list", "--porcelain"], cwd=toplevel)
        if listed.returncode != 0 or not _listed_as_detached(listed.stdout, path):
            return False
        head = _run_git(["rev-parse", "HEAD"], cwd=path)
    except (WorktreeError, OSError):
        return False
    if head.returncode != 0:
        return False
    return head.stdout.strip() == commit


def _remove_dir_if_empty(path: str) -> None:
    """Remove ``path`` when it is an empty directory; ignore otherwise."""
    with contextlib.suppress(OSError):
        os.rmdir(path)


def _rollback(created: list[tuple[str, str]], assignment_id: str) -> None:
    """Remove every worktree this call added; never mask the original error.

    Every path in ``created`` did not exist before this call, so whatever is
    left there (including a partial directory from a failed ``worktree add``)
    is ours to remove.
    """
    for toplevel, path in reversed(created):
        try:
            result = _run_git(["worktree", "remove", "--force", "--", path], cwd=toplevel)
        except (WorktreeError, OSError) as exc:
            _logger.warning("Assignment rollback failed for %s: %s", path, exc)
        else:
            if result.returncode != 0:
                _logger.warning(
                    "Assignment rollback failed for %s: %s",
                    path,
                    (result.stderr or "").strip()[:_ERROR_DETAIL_LIMIT],
                )
        if os.path.lexists(path):
            try:
                if os.path.islink(path) or not os.path.isdir(path):
                    os.unlink(path)
                else:
                    shutil.rmtree(path)
            except OSError as exc:
                _logger.warning("Assignment rollback failed for %s: %s", path, exc)
    for toplevel, _ in reversed(created):
        _remove_dir_if_empty(os.path.join(toplevel, ".omnigent", "worktrees", assignment_id))


def _fail_prepare(
    repository_name: str, error_code: str, error: str
) -> HostAssignmentPrepareResultFrame:
    """Build a failed prepare result (the dispatcher stamps ``request_id``)."""
    return HostAssignmentPrepareResultFrame(
        request_id="",
        status="failed",
        error_code=error_code,
        error=error,
        repository_name=repository_name,
    )


def _prepare_one(
    entry: HostAssignmentPrepareRepository,
    assignment_id: str,
    prepared: dict[str, _PreparedRepository],
    created: list[tuple[str, str]],
) -> HostAssignmentPrepareResultFrame | None:
    """Prepare one repository; ``None`` means success (registered in ``prepared``)."""
    name = entry.repository_name

    # Host-side boundaries first: the frame decoder only checks strings, so
    # these pure checks run before any git call.
    if _ASSIGNMENT_ID_RE.fullmatch(assignment_id) is None:
        return _fail_prepare(name, "source_invalid", f"invalid assignment_id {assignment_id!r}")
    if not _valid_repository_name(name):
        return _fail_prepare(name, "source_invalid", f"invalid repository_name {name!r}")
    if _INPUT_COMMIT_RE.fullmatch(entry.input_commit) is None:
        return _fail_prepare(
            name, "commit_mismatch", f"invalid input_commit {entry.input_commit!r}"
        )
    if entry.input_ref != _expected_input_ref(assignment_id, name):
        return _fail_prepare(name, "fetch_failed", f"unexpected input ref {entry.input_ref!r}")
    try:
        manifest_path = validate_manifest_path(entry.context_manifest_path)
    except ManifestError as exc:
        return _fail_prepare(name, "manifest_invalid", str(exc))

    if not os.path.isdir(entry.source_directory):
        return _fail_prepare(
            name, "source_invalid", f"not a git working copy: {entry.source_directory}"
        )
    try:
        top = _run_git(["rev-parse", "--show-toplevel"], cwd=entry.source_directory)
    except WorktreeError as exc:
        return _fail_prepare(name, "worktree_failed", exc.message)
    if top.returncode != 0 or not _same_path(top.stdout.strip(), entry.source_directory):
        return _fail_prepare(
            name, "source_invalid", f"not a git working copy: {entry.source_directory}"
        )
    toplevel = os.path.realpath(top.stdout.strip())

    refspec = f"{entry.input_ref}:{entry.input_ref}"
    try:
        fetched = _run_git(
            ["fetch", "--no-tags", "--", entry.remote_url, refspec],
            cwd=toplevel,
            timeout=_FETCH_TIMEOUT_S,
        )
    except WorktreeError as exc:
        return _fail_prepare(name, "fetch_failed", exc.message)
    if fetched.returncode != 0:
        return _fail_prepare(
            name, "fetch_failed", _detail(fetched.stderr, fetched.returncode, "git fetch failed")
        )

    try:
        resolved = _run_git(
            [
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{entry.input_ref}^{{commit}}",
            ],
            cwd=toplevel,
        )
    except WorktreeError as exc:
        return _fail_prepare(name, "worktree_failed", exc.message)
    if resolved.returncode != 0:
        return _fail_prepare(
            name, "commit_mismatch", f"input ref {entry.input_ref!r} not found after fetch"
        )
    if resolved.stdout.strip() != entry.input_commit:
        return _fail_prepare(
            name,
            "commit_mismatch",
            f"input ref {entry.input_ref!r} resolves to {resolved.stdout.strip()}, "
            f"expected {entry.input_commit}",
        )

    try:
        blob_out = _run_git(
            ["cat-file", "blob", f"{entry.input_commit}:{manifest_path}"],
            cwd=toplevel,
            text=False,
        )
    except WorktreeError as exc:
        return _fail_prepare(name, "worktree_failed", exc.message)
    if blob_out.returncode != 0:
        return _fail_prepare(
            name,
            "manifest_missing",
            f"manifest not found at {entry.input_commit}:{entry.context_manifest_path}",
        )
    blob = blob_out.stdout
    actual_digest = manifest_digest(blob)
    if actual_digest != entry.manifest_digest:
        return _fail_prepare(
            name,
            "manifest_digest_mismatch",
            f"manifest digest mismatch for {entry.context_manifest_path!r}: "
            f"expected {entry.manifest_digest}, got {actual_digest}",
        )
    try:
        manifest = parse_manifest(blob)
    except ManifestError as exc:
        return _fail_prepare(name, "manifest_invalid", str(exc))

    try:
        common_dir = _git_common_dir(toplevel)
        _ensure_excluded(common_dir)
    except (WorktreeError, OSError) as exc:
        message = exc.message if isinstance(exc, WorktreeError) else str(exc)
        return _fail_prepare(name, "worktree_failed", f"could not exclude .omnigent: {message}")

    path = _worktree_path(toplevel, assignment_id, name)
    parent = os.path.dirname(path)
    if not _contained_inside(os.path.realpath(parent), toplevel):
        return _fail_prepare(
            name, "worktree_failed", f"worktree parent escapes the source checkout: {parent}"
        )
    # makedirs stays bare: an OSError here propagates to prepare(), which
    # rolls back this call's worktrees before re-raising.
    os.makedirs(parent, exist_ok=True)
    if not _contained_inside(os.path.realpath(parent), toplevel):
        return _fail_prepare(
            name, "worktree_failed", f"worktree parent escapes the source checkout: {parent}"
        )
    # Never prune here: it scans the whole repository and drops other
    # worktrees' registrations while their volumes are unmounted. A stale
    # registration at this path surfaces as an add failure naming the path.
    try:
        os.mkdir(path)
    except FileExistsError:
        if _is_reusable_worktree(path, toplevel, common_dir, entry.input_commit):
            prepared[name] = _PreparedRepository(
                toplevel=toplevel,
                worktree_path=path,
                commit=entry.input_commit,
                manifest=manifest,
            )
            return None
        return _fail_prepare(name, "worktree_failed", f"worktree path already exists: {path}")
    # mkdir takes ownership: a retried prepare can race on the same path.
    created.append((toplevel, path))
    try:
        added = _run_git(
            ["worktree", "add", "--detach", "--", path, entry.input_commit], cwd=toplevel
        )
    except WorktreeError as exc:
        return _fail_prepare(name, "worktree_failed", exc.message)
    if added.returncode != 0:
        return _fail_prepare(
            name,
            "worktree_failed",
            _detail(added.stderr, added.returncode, f"git worktree add failed for {path}"),
        )
    prepared[name] = _PreparedRepository(
        toplevel=toplevel,
        worktree_path=path,
        commit=entry.input_commit,
        manifest=manifest,
    )
    return None


def _object_exists(toplevel: str, commit: str, path: str) -> bool:
    """Return whether ``path`` exists at ``commit`` without reading it."""
    result = _run_git(["cat-file", "-e", f"{commit}:{path}"], cwd=toplevel)
    return result.returncode == 0


def _check_required(
    entries: Sequence[HostAssignmentPrepareRepository],
    prepared: dict[str, _PreparedRepository],
) -> HostAssignmentPrepareResultFrame | None:
    """Check manifest-required paths across the prepared map, in request order."""
    for entry in entries:
        info = prepared[entry.repository_name]
        required = required_paths(info.manifest)
        for path in required[THIS_REPOSITORY_KEY]:
            if not _object_exists(info.toplevel, info.commit, path):
                return _fail_prepare(
                    entry.repository_name,
                    "context_missing",
                    f"required context missing: {entry.repository_name}:{path}",
                )
        for repository_name, paths in required.items():
            if repository_name == THIS_REPOSITORY_KEY:
                continue
            sibling = prepared.get(repository_name)
            if sibling is None:
                return _fail_prepare(
                    entry.repository_name,
                    "manifest_invalid",
                    f"manifest names an unknown repository: {repository_name!r}",
                )
            for path in paths:
                if not _object_exists(sibling.toplevel, sibling.commit, path):
                    return _fail_prepare(
                        repository_name,
                        "context_missing",
                        f"required context missing: {repository_name}:{path}",
                    )
    return None


def prepare(
    repositories: Sequence[HostAssignmentPrepareRepository],
    assignment_id: str,
) -> HostAssignmentPrepareResultFrame:
    """Prepare one detached worktree per repository for an assignment.

    Repositories are prepared in request order, stopping at the first
    failure. On any failure the worktrees added by this call are removed
    again, so a failed prepare leaves the source directories exactly as
    found except for the exclude line and the fetched refs.

    :param repositories: One entry per repository, in dispatch order.
    :param assignment_id: Assignment being prepared, e.g. ``"asg_abc"``.
    :returns: ``status "ok"`` with the repository → directory map, or
        ``status "failed"`` with a stable ``error_code``. The returned
        frame carries an empty ``request_id``; the dispatcher stamps the
        request's id before replying.
    """
    with _lock_for_assignment(assignment_id):
        prepared: dict[str, _PreparedRepository] = {}
        created: list[tuple[str, str]] = []
        try:
            for entry in repositories:
                failure = _prepare_one(entry, assignment_id, prepared, created)
                if failure is not None:
                    _rollback(created, assignment_id)
                    return failure
            missing = _check_required(repositories, prepared)
            if missing is not None:
                _rollback(created, assignment_id)
                return missing
        except Exception:
            _rollback(created, assignment_id)
            raise
        return HostAssignmentPrepareResultFrame(
            request_id="",
            status="ok",
            directories={
                entry.repository_name: prepared[entry.repository_name].worktree_path
                for entry in repositories
            },
        )


def release(
    repositories: Sequence[HostAssignmentReleaseRepository],
    assignment_id: str,
) -> HostAssignmentReleaseResultFrame:
    """Remove an assignment's worktrees without discarding user content.

    Removal never uses ``--force``: a worktree with leftover uncommitted
    changes is reported in ``failures`` and left in place. A missing
    worktree counts as removed.
    Ignored files are project-declared disposables; only tracked/untracked changes block removal.

    :param repositories: One entry per repository, in dispatch order.
    :param assignment_id: Assignment being released, e.g. ``"asg_abc"``.
    :returns: ``status "ok"`` when every worktree is gone, otherwise
        ``"partial"`` with per-repository reasons. The returned frame
        carries an empty ``request_id``; the dispatcher stamps the
        request's id before replying.
    """
    with _lock_for_assignment(assignment_id):
        removed: list[str] = []
        failures: dict[str, str] = {}
        for entry in repositories:
            name = entry.repository_name
            if _ASSIGNMENT_ID_RE.fullmatch(assignment_id) is None:
                failures[name] = f"source_invalid: invalid assignment_id {assignment_id!r}"
                continue
            if not _valid_repository_name(name):
                failures[name] = f"source_invalid: invalid repository_name {name!r}"
                continue
            if not os.path.isdir(entry.source_directory):
                failures[name] = f"not a git working copy: {entry.source_directory}"
                continue
            try:
                top = _run_git(["rev-parse", "--show-toplevel"], cwd=entry.source_directory)
            except WorktreeError as exc:
                failures[name] = exc.message
                continue
            if top.returncode != 0 or not _same_path(top.stdout.strip(), entry.source_directory):
                failures[name] = f"not a git working copy: {entry.source_directory}"
                continue
            toplevel = os.path.realpath(top.stdout.strip())
            path = _worktree_path(toplevel, assignment_id, name)
            if not _contained_inside(os.path.realpath(os.path.dirname(path)), toplevel):
                failures[name] = f"worktree path escapes the source checkout: {path}"
                continue
            if not os.path.lexists(path):
                removed.append(name)
                _remove_dir_if_empty(
                    os.path.join(toplevel, ".omnigent", "worktrees", assignment_id)
                )
                continue
            try:
                # Explicit --untracked-files=all: the user's
                # status.showUntrackedFiles=no must not hide work we'd delete.
                status = _run_git(["status", "--porcelain", "--untracked-files=all"], cwd=path)
            except WorktreeError as exc:
                failures[name] = exc.message
                continue
            if status.returncode != 0:
                failures[name] = _detail(
                    status.stderr, status.returncode, f"git status failed for {path}"
                )
                continue
            if status.stdout.strip():
                preview = ", ".join(status.stdout.strip().splitlines()[:3])[:_ERROR_DETAIL_LIMIT]
                failures[name] = f"worktree has uncommitted or untracked files: {preview}"
                continue
            try:
                result = _run_git(["worktree", "remove", "--", path], cwd=toplevel)
            except WorktreeError as exc:
                failures[name] = exc.message
                continue
            if result.returncode != 0:
                failures[name] = (result.stderr or "").strip()[:_ERROR_DETAIL_LIMIT] or (
                    f"git worktree remove failed (exit {result.returncode})"
                )
                continue
            removed.append(name)
            _remove_dir_if_empty(os.path.join(toplevel, ".omnigent", "worktrees", assignment_id))
        return HostAssignmentReleaseResultFrame(
            request_id="",
            status="ok" if not failures else "partial",
            removed=removed,
            failures=failures,
        )
