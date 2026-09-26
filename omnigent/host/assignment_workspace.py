"""Host-side assignment worktree preparation and release.

Serves ``host.assignment_prepare`` / ``host.assignment_release``: fetch
pinned input refs by explicit refspec, verify commits and manifests, and
add (or remove) one detached worktree per repository under
``<source>/.omnigent/worktrees/<assignment_id>/<repository_name>`` — or,
when the prepare frame carries the project's entry,
``<entry>/.worktrees/<main repo name>/<topic>``. When
``host.worktree_add_command`` is set, the configured command places each
worktree instead. Release locates the execution root in the source
repository's own worktree registry, so it finds it in either layout.

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
from omnigent.host.git_worktree import (
    WorktreeError,
    _main_work_tree,
    _run_git,
    ensure_entry_excluded,
)
from omnigent.host.worktree_command import WorktreeCommandError, run_worktree_command
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


def _main_worktree_name(source_directory: str) -> str:
    """Directory name of the source repository's main work tree."""
    return Path(_main_work_tree(source_directory)).name


def _main_worktree_names(
    repositories: Sequence[HostAssignmentPrepareRepository],
) -> dict[str, str]:
    """Main-worktree directory name per repository, for repositories that resolve.

    A source that is not (yet) a git working copy is simply absent; its
    prepare fails later with the ordinary ``source_invalid`` error.
    """
    names: dict[str, str] = {}
    for repository in repositories:
        if not os.path.isdir(repository.source_directory):
            continue
        try:
            names[repository.repository_name] = _main_worktree_name(repository.source_directory)
        except WorktreeError:
            continue
    return names


def _assignment_topics(
    repositories: Sequence[HostAssignmentPrepareRepository],
    assignment_id: str,
    main_names: dict[str, str],
) -> dict[str, str]:
    """Topic directory per repository for the entry layout.

    The assignment id is the topic unless two repositories in this frame
    share a main-worktree name; then every one of them is qualified with
    its repository name so their paths differ.
    """
    counts: dict[str, int] = {}
    for repository in repositories:
        name = main_names.get(repository.repository_name)
        if name is not None:
            counts[name] = counts.get(name, 0) + 1
    topics: dict[str, str] = {}
    for repository in repositories:
        name = main_names.get(repository.repository_name)
        if name is not None and counts[name] > 1:
            topics[repository.repository_name] = f"{assignment_id}-{repository.repository_name}"
        else:
            topics[repository.repository_name] = assignment_id
    return topics


def _makedirs_tracking(path: str) -> list[str]:
    """Create ``path`` with its parents; return the directories it created, outermost first."""
    missing: list[str] = []
    probe = path
    while probe and not os.path.isdir(probe):
        missing.append(probe)
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    os.makedirs(path, exist_ok=True)
    missing.reverse()
    return missing


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


@dataclass
class _ListedWorktree:
    """One record of ``git worktree list --porcelain``.

    :param path: Absolute worktree directory reported by git.
    :param detached: Whether the record is in detached-HEAD state.
    """

    path: str
    detached: bool


def _parse_worktree_list(porcelain: str) -> list[_ListedWorktree]:
    """Parse ``git worktree list --porcelain`` output into records, main first."""
    records: list[_ListedWorktree] = []
    current: str | None = None
    detached = False
    for line in porcelain.splitlines():
        if line.startswith("worktree "):
            if current is not None:
                records.append(_ListedWorktree(current, detached))
            current = line[len("worktree ") :].strip()
            detached = False
        elif line == "detached":
            detached = True
        elif line == "" and current is not None:
            records.append(_ListedWorktree(current, detached))
            current = None
            detached = False
    if current is not None:
        records.append(_ListedWorktree(current, detached))
    return records


def _is_assignment_worktree_path(
    path: str,
    *,
    legacy_path: str,
    main_name: str,
    assignment_id: str,
    repository_name: str,
) -> bool:
    """Whether a registered worktree path is this repository's assignment root.

    Matches the legacy derived path, or the entry layout's last three
    components ``.worktrees/<main repo name>/<topic>`` with ``<topic>``
    the assignment id or its repository-qualified form.
    """
    if _same_path(path, legacy_path):
        return True
    components = Path(path).parts
    if len(components) < 3 or components[-3] != ".worktrees" or components[-2] != main_name:
        return False
    return components[-1] in (assignment_id, f"{assignment_id}-{repository_name}")


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


def _rollback(created: list[tuple[str, str, tuple[str, ...]]]) -> None:
    """Remove every worktree this call added; never mask the original error.

    Every path in ``created`` did not exist before this call, so whatever is
    left there (including a partial directory from a failed ``worktree add``)
    is ours to remove. ``cleanup_dirs`` names the directories to remove
    again when empty — the legacy per-assignment directory, or the
    directories this call created under an entry.
    """
    for toplevel, path, _cleanup_dirs in reversed(created):
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
    for _toplevel, _path, cleanup_dirs in reversed(created):
        for directory in reversed(cleanup_dirs):
            _remove_dir_if_empty(directory)


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
    created: list[tuple[str, str, tuple[str, ...]]],
    entry_path: str | None,
    main_names: dict[str, str],
    topics: dict[str, str],
    command: list[str] | None,
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
    if entry_path is not None:
        try:
            ensure_entry_excluded(entry_path)
        except (WorktreeError, OSError) as exc:
            message = exc.message if isinstance(exc, WorktreeError) else str(exc)
            return _fail_prepare(
                name, "worktree_failed", f"could not exclude .worktrees: {message}"
            )

    if command is not None:
        # The registered paths before the call: a command-reported path is
        # only ours when it was not already registered.
        try:
            listed_before = _run_git(["worktree", "list", "--porcelain"], cwd=toplevel)
        except WorktreeError as exc:
            return _fail_prepare(name, "worktree_failed", exc.message)
        if listed_before.returncode != 0:
            return _fail_prepare(
                name,
                "worktree_failed",
                _detail(
                    listed_before.stderr,
                    listed_before.returncode,
                    f"git worktree list failed for {toplevel}",
                ),
            )
        registered_before = _parse_worktree_list(listed_before.stdout)
        main_worktree = registered_before[0].path if registered_before else toplevel
        try:
            path = run_worktree_command(
                command,
                source=toplevel,
                topic=topics[name],
                entry=entry_path,
                mode=[f"--detach={entry.input_commit}"],
            )
        except WorktreeCommandError as exc:
            if exc.code == "EXISTS":
                detail = exc.detail or {}
                candidate = detail.get("path")
                if (
                    isinstance(candidate, str)
                    and not _same_path(candidate, toplevel)
                    and not _same_path(candidate, main_worktree)
                    and (
                        entry_path is None
                        or _contained_inside(
                            os.path.realpath(candidate), os.path.realpath(entry_path)
                        )
                    )
                    and _is_reusable_worktree(candidate, toplevel, common_dir, entry.input_commit)
                ):
                    # A retried prepare found the worktree this call would add.
                    prepared[name] = _PreparedRepository(
                        toplevel=toplevel,
                        worktree_path=candidate,
                        commit=entry.input_commit,
                        manifest=manifest,
                    )
                    return None
            error_code = (
                "source_invalid"
                if exc.code in ("NOT_A_REPO", "BARE_SOURCE", "AMBIGUOUS_SOURCE")
                else "worktree_failed"
            )
            return _fail_prepare(name, error_code, exc.message)
        if (
            any(_same_path(record.path, path) for record in registered_before)
            or _same_path(path, toplevel)
            or _same_path(path, main_worktree)
            or not _is_reusable_worktree(path, toplevel, common_dir, entry.input_commit)
        ):
            return _fail_prepare(
                name,
                "worktree_failed",
                "worktree command returned a path that is not a new detached "
                f"worktree of {toplevel}: {path}",
            )
        created.append((toplevel, path, ()))
        prepared[name] = _PreparedRepository(
            toplevel=toplevel,
            worktree_path=path,
            commit=entry.input_commit,
            manifest=manifest,
        )
        return None

    if entry_path is not None:
        try:
            main_name = main_names.get(name) or _main_worktree_name(toplevel)
        except WorktreeError as exc:
            return _fail_prepare(name, "worktree_failed", exc.message)
        path = os.path.join(entry_path, ".worktrees", main_name, topics[name])
        containment_root = os.path.realpath(entry_path)
        escape_message = f"worktree parent escapes the project entry: {os.path.dirname(path)}"
    else:
        path = _worktree_path(toplevel, assignment_id, name)
        containment_root = toplevel
        escape_message = f"worktree parent escapes the source checkout: {os.path.dirname(path)}"
    parent = os.path.dirname(path)
    if not _contained_inside(os.path.realpath(parent), containment_root):
        return _fail_prepare(name, "worktree_failed", escape_message)
    # makedirs stays bare: an OSError here propagates to prepare(), which
    # rolls back this call's worktrees before re-raising.
    created_dirs = _makedirs_tracking(parent)
    if not _contained_inside(os.path.realpath(parent), containment_root):
        return _fail_prepare(name, "worktree_failed", escape_message)
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
    created.append(
        (
            toplevel,
            path,
            tuple(created_dirs)
            if entry_path is not None
            else (os.path.join(toplevel, ".omnigent", "worktrees", assignment_id),),
        )
    )
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
    entry: str | None = None,
    command: list[str] | None = None,
) -> HostAssignmentPrepareResultFrame:
    """Prepare one detached worktree per repository for an assignment.

    Repositories are prepared in request order, stopping at the first
    failure. On any failure the worktrees added by this call are removed
    again, so a failed prepare leaves the source directories exactly as
    found except for the exclude line and the fetched refs.

    :param repositories: One entry per repository, in dispatch order.
    :param assignment_id: Assignment being prepared, e.g. ``"asg_abc"``.
    :param entry: The assignment project's entry directory on the host.
        When set, each worktree goes to
        ``<entry>/.worktrees/<main repo name>/<topic>`` with the
        assignment id as the topic (qualified with the repository name
        when two repositories share a main-worktree name), and the
        entry's repository gains an ``info/exclude`` line for it. ``None``
        keeps today's location under the source checkout.
    :param command: Configured external worktree command
        (``host.worktree_add_command``). ``None`` keeps the built-in
        location; when set, that command creates each detached worktree
        and its failure refuses the prepare — the built-in layout is
        never used as a fallback.
    :returns: ``status "ok"`` with the repository → directory map, or
        ``status "failed"`` with a stable ``error_code``. The returned
        frame carries an empty ``request_id``; the dispatcher stamps the
        request's id before replying.
    """
    with _lock_for_assignment(assignment_id):
        prepared: dict[str, _PreparedRepository] = {}
        created: list[tuple[str, str, tuple[str, ...]]] = []
        main_names = (
            _main_worktree_names(repositories) if entry is not None or command is not None else {}
        )
        topics = _assignment_topics(repositories, assignment_id, main_names)
        try:
            for repo_entry in repositories:
                failure = _prepare_one(
                    repo_entry,
                    assignment_id,
                    prepared,
                    created,
                    entry,
                    main_names,
                    topics,
                    command,
                )
                if failure is not None:
                    _rollback(created)
                    return failure
            missing = _check_required(repositories, prepared)
            if missing is not None:
                _rollback(created)
                return missing
        except Exception:
            _rollback(created)
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

    The execution root of each repository is located in that repository's
    own worktree registry: a detached worktree at the path this module
    would have derived under the source checkout, or one whose path ends
    ``…/.worktrees/<main repo name>/<topic>`` with ``<topic>`` the
    assignment id (or its repository-qualified form). Only that
    registered, detached worktree is removed — a branch worktree is never
    selected, and an entry edited after prepare does not hide the root.

    Removal never uses ``--force``: a worktree with leftover uncommitted
    changes is reported in ``failures`` and left in place. A missing
    worktree — including one with no registration at all — counts as
    removed.
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
            legacy_dir = os.path.join(toplevel, ".omnigent", "worktrees", assignment_id)
            try:
                listed = _run_git(["worktree", "list", "--porcelain"], cwd=toplevel)
            except WorktreeError as exc:
                failures[name] = exc.message
                continue
            if listed.returncode != 0:
                failures[name] = _detail(
                    listed.stderr,
                    listed.returncode,
                    f"git worktree list failed for {toplevel}",
                )
                continue
            records = _parse_worktree_list(listed.stdout)
            legacy_path = _worktree_path(toplevel, assignment_id, name)
            main_name = Path(records[0].path).name if records else ""
            target = next(
                (
                    record
                    for record in records[1:]
                    if record.detached
                    and _is_assignment_worktree_path(
                        record.path,
                        legacy_path=legacy_path,
                        main_name=main_name,
                        assignment_id=assignment_id,
                        repository_name=name,
                    )
                ),
                None,
            )
            if target is None or not os.path.lexists(target.path):
                # Nothing registered for this assignment (or its directory
                # is already gone): a missing worktree counts as removed,
                # and a stale registration is left for the user to prune.
                removed.append(name)
                _remove_dir_if_empty(legacy_dir)
                continue
            path = target.path
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
            _remove_dir_if_empty(legacy_dir)
        return HostAssignmentReleaseResultFrame(
            request_id="",
            status="ok" if not failures else "partial",
            removed=removed,
            failures=failures,
        )
