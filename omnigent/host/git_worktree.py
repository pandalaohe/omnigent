"""Host-side git worktree operations for session-start worktrees.

Runs ``git`` (via argv lists, never a shell) on the host in response to
``host.create_worktree`` / ``host.remove_worktree`` frames. Branch names
are validated against git ref-format rules before reaching argv. See
designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, overload

# fetch/add can be slow on large repos; bound it so git can't hang the
# host's tunnel loop.
_GIT_TIMEOUT_S: float = 120.0

# Max directory-collision suffixes (``-2`` .. ``-N``) before giving up.
_MAX_DIR_COLLISION_SUFFIX: int = 50

# Chars git refuses in a ref: space, control chars, ``~^:?*[\``, DEL.
# (``..``, leading ``-``/``.``, ``/`` edges, ``.lock``, ``@{`` are
# checked separately.)
_INVALID_BRANCH_CHARS = re.compile(r"[\x00-\x20~^:?*\[\\\x7f]")


class WorktreeError(Exception):
    """Raised when a git worktree operation fails.

    The message is user-facing and surfaced verbatim in the
    ``host.*_worktree_result`` frame's ``error`` field.

    :param message: Human-readable failure reason, e.g.
        ``"not a git repository: /tmp/x"``.
    """

    def __init__(self, message: str) -> None:
        """Initialize with the user-facing error message.

        :param message: Error string surfaced to the API caller.
        """
        super().__init__(message)
        self.message = message


def validate_branch_name(name: str) -> None:
    """Validate a git branch name against ``git check-ref-format`` rules.

    :param name: Proposed branch name, e.g. ``"feature/login"``.
    :raises WorktreeError: If the name is empty or violates any
        ref-format rule. The message names the specific violation.
    """
    if not name:
        raise WorktreeError("branch name must not be empty")
    if name.startswith("-"):
        raise WorktreeError(f"branch name must not start with '-': {name!r}")
    if name.startswith("/") or name.endswith("/"):
        raise WorktreeError(f"branch name must not start or end with '/': {name!r}")
    if name.endswith("."):
        raise WorktreeError(f"branch name must not end with '.': {name!r}")
    if any(part.endswith(".lock") for part in name.split("/")):
        raise WorktreeError(f"branch name path components must not end with '.lock': {name!r}")
    if ".." in name:
        raise WorktreeError(f"branch name must not contain '..': {name!r}")
    if "//" in name:
        raise WorktreeError(f"branch name must not contain '//': {name!r}")
    if "@{" in name:
        raise WorktreeError(f"branch name must not contain '@{{': {name!r}")
    if name == "@":
        raise WorktreeError("branch name must not be '@'")
    if _INVALID_BRANCH_CHARS.search(name):
        raise WorktreeError(
            f"branch name {name!r} contains an invalid character; spaces, "
            f"control characters, and any of ~ ^ : ? * [ \\ are not allowed"
        )
    # No path component may start with '.' (e.g. ".hidden" or "a/.b").
    if any(part.startswith(".") for part in name.split("/")):
        raise WorktreeError(f"branch name path components must not start with '.': {name!r}")


def _sanitize_dirname(branch_name: str) -> str:
    """Derive a single-segment directory name from a branch name.

    Slashes collapse to ``-`` so the worktree lives in one directory.

    :param branch_name: Validated branch name, e.g. ``"feature/login"``.
    :returns: Filesystem-safe single segment, e.g. ``"feature-login"``.
    """
    return branch_name.strip("/").replace("/", "-")


@overload
def _run_git(
    args: list[str], *, cwd: str, timeout: float = ..., text: Literal[True] = ...
) -> subprocess.CompletedProcess[str]: ...
@overload
def _run_git(
    args: list[str], *, cwd: str, timeout: float = ..., text: Literal[False]
) -> subprocess.CompletedProcess[bytes]: ...
def _run_git(
    args: list[str],
    *,
    cwd: str,
    timeout: float = _GIT_TIMEOUT_S,
    text: bool = True,
) -> subprocess.CompletedProcess[str] | subprocess.CompletedProcess[bytes]:
    """Run a git command, returning the completed process.

    :param args: Git argv *after* ``git``, e.g.
        ``["rev-parse", "--show-toplevel"]``. Passed as a list so no
        shell parsing occurs.
    :param cwd: Working directory to run git in, e.g.
        ``"/Users/alice/myrepo"``.
    :param timeout: Seconds before the command is killed, e.g.
        ``240.0`` for a fetch that pulls an unbounded object count.
        Defaults to :data:`_GIT_TIMEOUT_S`.
    :param text: When ``True`` (default) stdout/stderr are decoded text;
        pass ``False`` for raw bytes (e.g. hashing a blob byte-for-byte,
        where decoding would normalise newlines).
    :returns: The completed process with captured stdout/stderr.
    :raises WorktreeError: If git is not installed, or the command
        exceeds ``timeout``.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            # Keep failure diagnostics stable for repository classification.
            env={**os.environ, "LC_ALL": "C"},
            capture_output=True,
            text=text,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise WorktreeError("git is not installed on the host") from exc
    except subprocess.TimeoutExpired as exc:
        raise WorktreeError(f"git command timed out after {timeout:.0f}s") from exc


def _git_error(label: str, result: subprocess.CompletedProcess[str]) -> WorktreeError:
    """Build a WorktreeError from a failed git command.

    Includes the exit code (always present) and stderr when non-empty,
    so no invented "unknown error" fallback is needed.

    :param label: What failed, e.g. ``"git worktree add failed"``.
    :param result: The completed process with a non-zero return code.
    :returns: A :class:`WorktreeError` with code + stderr detail.
    """
    detail = result.stderr.strip()
    suffix = f": {detail}" if detail else ""
    return WorktreeError(f"{label} (exit {result.returncode}){suffix}")


def _contained_inside(candidate: str, root: str) -> bool:
    """Whether realpath ``candidate`` is at or inside realpath ``root``.

    :param candidate: Path to check, e.g. a worktree parent directory.
    :param root: Path the candidate must be inside, e.g. the entry.
    :returns: ``True`` when ``candidate`` resolves inside ``root``.
    """
    try:
        return os.path.commonpath([candidate, root]) == root
    except ValueError:
        # Different drives (Windows) can't share a common path.
        return False


def ensure_entry_excluded(entry: str) -> None:
    """Add the entry's ``.worktrees/`` directory to the enclosing repo's exclude file.

    Worktree directories Omnigent creates under the entry would
    otherwise show as untracked noise in the enclosing repository's
    status. The line is ``/<entry relative to the tree's top level>/.worktrees/``,
    matching the ``/.omnigent/`` mechanism assignments already use, and is
    written at most once. Tracked ignore files (``.gitignore``) are never
    touched, and an entry outside any git working tree writes nothing.

    :param entry: Absolute entry directory on the host, e.g.
        ``"/Users/alice/project"``.
    :raises WorktreeError: If the exclude file cannot be written.
    """
    if not os.path.isdir(entry):
        return
    top = _run_git(["rev-parse", "--show-toplevel"], cwd=entry)
    if top.returncode != 0:
        return
    toplevel = top.stdout.strip()
    common = _run_git(["rev-parse", "--git-common-dir"], cwd=entry)
    if common.returncode != 0:
        return
    common_dir = common.stdout.strip()
    if not os.path.isabs(common_dir):
        common_dir = os.path.join(entry, common_dir)
    relative = os.path.relpath(os.path.join(entry, ".worktrees"), toplevel)
    line = "/" + relative.replace(os.sep, "/") + "/"
    exclude = Path(common_dir) / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        raw = exclude.read_bytes() if exclude.exists() else b""
        if line.encode("utf-8") in raw.splitlines():
            return
        with exclude.open("ab") as handle:
            if raw and not raw.endswith(b"\n"):
                handle.write(b"\n")
            handle.write(line.encode("utf-8") + b"\n")
    except OSError as exc:
        raise WorktreeError(f"could not write git exclude for {entry}: {exc}") from exc


def _main_work_tree(repo_path: str) -> str:
    """Resolve the MAIN work tree for any path inside a git repo.

    ``git worktree list --porcelain`` enumerates every work tree of the
    repository; its first entry is always the main one (the checkout all
    linked worktrees share). Run from ``repo_path``, this resolves the
    same main work tree whether the user picked the main checkout, a
    subdirectory, or a *linked worktree* — so a new worktree is always
    created as a sibling of the MAIN repo (e.g.
    ``…/myrepo-worktrees/<branch>``) rather than nested inside a worktree
    the session happened to start in (which ``rev-parse --show-toplevel``
    would produce: ``…/myrepo-worktrees/feature-worktrees/<branch>``).

    :param repo_path: Absolute path inside a git repository — the
        directory the user picked, e.g.
        ``"/Users/alice/myrepo-worktrees/feature"``.
    :returns: Absolute path of the main work tree, e.g.
        ``"/Users/alice/myrepo"``.
    :raises WorktreeError: If ``repo_path`` is not a directory, not
        inside a git work tree, the git command fails, or the main
        repository is bare (a bare repository has no work tree to link
        a worktree to).
    """
    if not Path(repo_path).is_dir():
        raise WorktreeError(f"path is not a directory: {repo_path}")
    result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_path)
    if result.returncode != 0:
        if any(
            line.startswith("fatal: not a git repository") for line in result.stderr.splitlines()
        ):
            raise WorktreeError(f"not a git repository: {repo_path}")
        raise _git_error("git worktree list failed", result)
    lines = result.stdout.splitlines()
    for index, line in enumerate(lines):
        # Porcelain format: the first record's ``worktree <path>`` line is
        # the main work tree; linked worktrees follow.
        if line.startswith("worktree "):
            # A bare repository's only record carries a ``bare`` line right
            # after its path — there is no working tree to branch off.
            for record_line in lines[index + 1 :]:
                if record_line == "" or record_line.startswith("worktree "):
                    break
                if record_line == "bare":
                    raise WorktreeError(f"bare main repository is not supported: {repo_path}")
            return line[len("worktree ") :].strip()
    raise WorktreeError(f"could not resolve main work tree for {repo_path}")


@dataclass
class WorktreeInfo:
    """One entry from ``git worktree list``.

    :param path: Absolute worktree directory, e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Checked-out branch without the ``refs/heads/``
        prefix, e.g. ``"feature/login"``. ``None`` when the worktree
        is in detached-HEAD state.
    :param is_main: ``True`` for the repository's main work tree (the
        first ``git worktree list`` record), ``False`` for linked
        worktrees.
    :param detached: ``True`` when the worktree has a detached HEAD
        (no branch checked out).
    :param updated_at: Unix epoch seconds of the checked-out HEAD commit, or
        ``None`` when the commit timestamp cannot be resolved.
    """

    path: str
    branch: str | None
    is_main: bool
    detached: bool
    updated_at: int | None = None


def _commit_updated_ats(repo_root: str, heads: list[str | None]) -> dict[str, int]:
    """Return checked-out commit timestamps with one bounded local git call."""
    unique_heads = list(dict.fromkeys(head for head in heads if head is not None))
    if not unique_heads:
        return {}
    result = _run_git(["show", "-s", "--format=%H%x00%ct", *unique_heads], cwd=repo_root)
    if result.returncode != 0:
        return {}
    timestamps: dict[str, int] = {}
    for line in result.stdout.splitlines():
        head, separator, raw_timestamp = line.partition("\0")
        if separator == "":
            continue
        try:
            timestamps[head] = int(raw_timestamp)
        except ValueError:
            continue
    return timestamps


def list_worktrees(*, repo_path: str) -> list[WorktreeInfo]:
    """List the git worktrees of the repository containing ``repo_path``.

    Resolves the main work tree first (so a linked worktree resolves the
    same list as the main checkout), then parses
    ``git worktree list --porcelain``. The first record is always the
    main work tree; the rest are linked worktrees.

    :param repo_path: Absolute path inside a git repository — the
        directory the user picked, e.g. ``"/Users/alice/myrepo"``.
    :returns: One :class:`WorktreeInfo` per worktree, main first.
    :raises WorktreeError: If ``repo_path`` is not a directory or not
        inside a git work tree, or if ``git worktree list`` fails.
    """
    repo_root = _main_work_tree(repo_path)
    result = _run_git(["worktree", "list", "--porcelain"], cwd=repo_root)
    if result.returncode != 0:
        raise _git_error("git worktree list failed", result)

    records: list[tuple[str, str | None, bool, str | None]] = []
    path: str | None = None
    branch: str | None = None
    head: str | None = None
    detached = False
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
            branch = None
            head = None
            detached = False
        elif line.startswith("HEAD "):
            head = line[len("HEAD ") :].strip()
        elif line.startswith("branch "):
            ref = line[len("branch ") :].strip()
            branch = ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref
        elif line == "detached":
            detached = True
        elif line == "" and path is not None:
            # Blank line terminates a record.
            records.append((path, branch, detached, head))
            path = None
    # The porcelain output may omit a trailing blank line for the last record.
    if path is not None:
        records.append((path, branch, detached, head))
    updated_ats = _commit_updated_ats(repo_root, [record[3] for record in records])
    return [
        WorktreeInfo(
            path=worktree_path,
            branch=worktree_branch,
            is_main=index == 0,
            detached=worktree_detached,
            updated_at=updated_ats.get(worktree_head) if worktree_head is not None else None,
        )
        for index, (worktree_path, worktree_branch, worktree_detached, worktree_head) in enumerate(
            records
        )
    ]


def _local_branch_exists(repo_root: str, branch_name: str) -> bool:
    """Return whether a local branch already exists in the repo.

    :param repo_root: Absolute repo work-tree root, e.g.
        ``"/Users/alice/myrepo"``.
    :param branch_name: Branch name to check, e.g. ``"feature/login"``.
    :returns: ``True`` if ``refs/heads/<branch_name>`` resolves.
    """
    return (
        _run_git(
            ["rev-parse", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=repo_root,
        ).returncode
        == 0
    )


def _resolve_worktree_path(repo_root: str, branch_name: str, *, entry: str | None = None) -> Path:
    """Compute a collision-free worktree directory path.

    With ``entry`` the worktree goes to
    ``<entry>/.worktrees/<repo-name>/<sanitized-branch>``; without it, to
    today's sibling location
    ``<parent-of-repo-root>/<repo-name>-worktrees/<sanitized-branch>``.
    Either way a numeric suffix is appended if the path already exists on
    disk.

    :param repo_root: Absolute repo work-tree root, e.g.
        ``"/Users/alice/myrepo"``.
    :param branch_name: Validated branch name, e.g.
        ``"feature/login"``.
    :param entry: Project entry directory on the host, or ``None`` for
        the sibling layout, e.g. ``"/Users/alice/project"``.
    :returns: A path that does not yet exist, e.g.
        ``Path("/Users/alice/project/.worktrees/myrepo/feature-login")``.
    :raises WorktreeError: If no free path is found within
        :data:`_MAX_DIR_COLLISION_SUFFIX` attempts.
    """
    root = Path(repo_root)
    base_dir = (
        Path(entry) / ".worktrees" / root.name
        if entry is not None
        else root.parent / f"{root.name}-worktrees"
    )
    dirname = _sanitize_dirname(branch_name)
    candidate = base_dir / dirname
    if not candidate.exists():
        return candidate
    for suffix in range(2, _MAX_DIR_COLLISION_SUFFIX + 1):
        candidate = base_dir / f"{dirname}-{suffix}"
        if not candidate.exists():
            return candidate
    raise WorktreeError(
        f"could not find a free worktree directory under {base_dir} "
        f"after {_MAX_DIR_COLLISION_SUFFIX} attempts"
    )


def _ensure_base_resolvable(repo_root: str, base_branch: str) -> None:
    """Make ``base_branch`` resolvable, fetching once if needed.

    If the base ref doesn't resolve locally (e.g. a remote-tracking
    branch not yet fetched), attempt a single ``git fetch`` and
    re-check. A fetch failure (offline) is not fatal on its own — the
    subsequent re-check produces the user-facing error.

    :param repo_root: Absolute repo work-tree root, e.g.
        ``"/Users/alice/myrepo"``.
    :param base_branch: Base ref the user requested, e.g. ``"main"``
        or ``"origin/main"``.
    :raises WorktreeError: If the base ref cannot be resolved even
        after a fetch attempt.
    """
    # --end-of-options forces git to treat the user-supplied base_branch as a
    # rev, never an option, so a value like "--exec-path" can't inject a git
    # flag (argv-only, no shell). Note: a bare "--" would not work here — git
    # rev-parse treats args after "--" as pathspecs, not revs.
    if (
        _run_git(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", base_branch], cwd=repo_root
        ).returncode
        == 0
    ):
        return
    # Best-effort fetch from the default remote, then re-verify.
    _run_git(["fetch"], cwd=repo_root)
    if (
        _run_git(
            ["rev-parse", "--verify", "--quiet", "--end-of-options", base_branch], cwd=repo_root
        ).returncode
        != 0
    ):
        raise WorktreeError(f"base branch does not exist: {base_branch}")


@dataclass
class CreatedWorktree:
    """Result of a successful worktree creation.

    :param worktree_path: Absolute path of the created worktree
        directory, e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: The branch checked out in the worktree, e.g.
        ``"feature/login"``.
    """

    worktree_path: str
    branch: str


def create_worktree(
    *,
    repo_path: str,
    branch_name: str,
    base_branch: str | None = None,
    existing_branch: bool = False,
    entry: str | None = None,
    command: list[str] | None = None,
) -> CreatedWorktree:
    """Create a git worktree with a new — or existing — branch checked out.

    Resolves the repo root, picks a collision-free directory, and runs
    ``git worktree add -b`` (fetching once if ``base_branch`` isn't
    locally resolvable). With ``existing_branch`` the branch must already
    exist and not be checked out in any live worktree; stale registrations
    (a worktree whose directory was deleted from disk) are pruned first,
    and the branch is checked out without ``-b`` — the recreate path for a
    deleted worktree.

    With ``command`` (the host's ``host.worktree_add_command``), the same
    pre-checks run, then the configured command creates the worktree and
    the built-in layout is not used at all: a command failure refuses the
    worktree rather than falling back.

    :param repo_path: Absolute path inside the source repo — the
        directory the user picked, e.g. ``"/Users/alice/myrepo"``.
    :param branch_name: New branch to create and check out, e.g.
        ``"feature/login"``. With ``existing_branch``, the pre-existing
        branch to check out instead.
    :param base_branch: Optional base ref, e.g. ``"main"``. ``None``
        branches from the repo's current ``HEAD``. Invalid with
        ``existing_branch`` (an existing branch has no base to fork).
    :param existing_branch: When ``True``, check out the pre-existing
        ``branch_name`` into a fresh worktree instead of creating a new
        branch.
    :param entry: The session project's entry directory on the host.
        When set, the worktree is created at
        ``<entry>/.worktrees/<main repo name>/<topic>`` and the entry's
        repository gains an ``info/exclude`` line for it; when ``None``,
        today's sibling location under the repo's parent is used.
    :param command: Configured external worktree command. ``None`` keeps
        the built-in behaviour; when set, that command creates the
        worktree (see :func:`omnigent.host.worktree_command.run_worktree_command`).
    :returns: The created worktree's path and branch.
    :raises WorktreeError: If the branch name is invalid, the path is
        not a git repo, the base ref can't be resolved,
        ``git worktree add`` fails (e.g. the branch already exists in
        create mode, is missing or still checked out in
        existing-branch mode), or the worktree directory would resolve
        outside the entry. With ``command``, any command failure
        (including no usable path) also raises.
    """
    validate_branch_name(branch_name)
    if existing_branch and base_branch is not None:
        raise WorktreeError("base_branch cannot be set when checking out an existing branch")
    # Always create the worktree off the MAIN work tree, even when
    # ``repo_path`` is itself a linked worktree (e.g. the fork-resume
    # picker prefilled a worktree as the source). Otherwise the new
    # worktree would nest under the picked worktree
    # (``…/feature-worktrees/<branch>``); resolving to the main repo keeps
    # all worktrees as siblings (``…/myrepo-worktrees/<branch>``).
    repo_root = _main_work_tree(repo_path)
    if existing_branch:
        if not _local_branch_exists(repo_root, branch_name):
            raise WorktreeError(
                f"branch {branch_name!r} does not exist; cannot recreate its worktree"
            )
        # A deleted worktree directory leaves a stale registration that
        # keeps the branch "in use" — prune it so the add below can
        # check the branch out again. Prune only drops registrations
        # whose directories are gone; live worktrees are untouched.
        _run_git(["worktree", "prune"], cwd=repo_root)
        live = next(
            (wt for wt in list_worktrees(repo_path=repo_root) if wt.branch == branch_name),
            None,
        )
        if live is not None:
            raise WorktreeError(
                f"branch {branch_name!r} is already checked out at {live.path}; "
                "remove that worktree first or choose a different branch name"
            )
    # Friendly pre-check before git's raw "branch already exists" error.
    # We don't reuse the existing worktree: two sessions sharing one
    # working tree would clobber each other (designs/SESSION_GIT_WORKTREE.md).
    elif _local_branch_exists(repo_root, branch_name):
        raise WorktreeError(
            f"a branch named {branch_name!r} already exists; choose a different branch name"
        )
    if base_branch is not None:
        _ensure_base_resolvable(repo_root, base_branch)
    if command is not None:
        # Imported here: git_worktree is imported by worktree_command, so a
        # module-level import would be circular.
        from omnigent.host.worktree_command import run_worktree_command

        if existing_branch:
            mode = [f"--branch={branch_name}"]
        else:
            mode = [f"--new-branch={branch_name}"]
            if base_branch is not None:
                mode.append(f"--base={base_branch}")
        if entry is not None:
            ensure_entry_excluded(entry)
        # Snapshot before the call: a path already registered here was not
        # created by this call, even when the command switched its branch.
        # The branch pre-checks alone cannot see such an adopted worktree.
        registered = {
            os.path.realpath(record.path) for record in list_worktrees(repo_path=repo_root)
        }
        # The caller's repo_path, not the resolved repo_root: the command
        # derives the default base from the source's own HEAD.
        path = run_worktree_command(
            command,
            source=repo_path,
            topic=branch_name,
            entry=entry,
            mode=mode,
        )
        path_real = os.path.realpath(path)
        if path_real in registered or not any(
            not record.is_main
            and os.path.realpath(record.path) == path_real
            and record.branch == branch_name
            for record in list_worktrees(repo_path=repo_root)
        ):
            raise WorktreeError(
                f"worktree command returned a path that is not a worktree on "
                f"branch {branch_name}: {path}"
            )
        return CreatedWorktree(worktree_path=path, branch=branch_name)
    worktree_path = _resolve_worktree_path(repo_root, branch_name, entry=entry)
    if entry is not None and not _contained_inside(
        os.path.realpath(worktree_path.parent), os.path.realpath(entry)
    ):
        # Checked before creating anything too, so a ``.worktrees`` symlink
        # out of the entry leaves no directory behind.
        raise WorktreeError(
            f"worktree directory escapes the project entry: {worktree_path.parent}"
        )
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    if entry is not None:
        # Re-checked after ``makedirs``: a component that resolved inside
        # the entry may be replaced by a link before the directory exists.
        if not _contained_inside(os.path.realpath(worktree_path.parent), os.path.realpath(entry)):
            raise WorktreeError(
                f"worktree directory escapes the project entry: {worktree_path.parent}"
            )
        ensure_entry_excluded(entry)

    if existing_branch:
        # --end-of-options: treat the branch as a rev, never a git flag
        # (argv-only, no shell). No ``-b`` — the branch already exists.
        add_args = ["worktree", "add", str(worktree_path), "--end-of-options", branch_name]
    else:
        add_args = ["worktree", "add", "-b", branch_name, str(worktree_path)]
        if base_branch is not None:
            # --end-of-options: treat base_branch as a rev, never a git flag,
            # so a user-supplied value starting with '-' can't inject an
            # option.
            add_args += ["--end-of-options", base_branch]
    result = _run_git(add_args, cwd=repo_root)
    if result.returncode != 0:
        raise _git_error("git worktree add failed", result)
    return CreatedWorktree(worktree_path=str(worktree_path), branch=branch_name)


def _main_repo_for_worktree(worktree_path: str) -> str:
    """Find the main repository work tree for a linked worktree.

    Uses ``git rev-parse --git-common-dir`` (which points at the
    shared ``.git`` of the main work tree) and returns that directory's
    parent. Run from inside the worktree so the relative result
    resolves correctly.

    :param worktree_path: Absolute path of a linked worktree, e.g.
        ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :returns: Absolute path of the main repo work tree, e.g.
        ``"/Users/alice/myrepo"``.
    :raises WorktreeError: If ``worktree_path`` is missing or not part
        of a git repository.
    """
    if not Path(worktree_path).exists():
        raise WorktreeError(f"worktree path does not exist: {worktree_path}")
    result = _run_git(["rev-parse", "--git-common-dir"], cwd=worktree_path)
    if result.returncode != 0:
        raise WorktreeError(f"not a git worktree: {worktree_path}")
    common_dir = Path(result.stdout.strip())
    if not common_dir.is_absolute():
        common_dir = (Path(worktree_path) / common_dir).resolve()
    return str(common_dir.parent)


def remove_worktree(
    *,
    worktree_path: str,
    branch: str | None = None,
    delete_branch: bool = False,
) -> None:
    """Remove a git worktree and optionally delete its branch.

    Removes the directory with ``--force``, then (if requested) deletes
    the branch — in that order, since git refuses to delete a branch
    still checked out in a linked worktree. ``git worktree remove``
    refuses to remove the main work tree.

    :param worktree_path: Absolute path of the worktree to remove,
        e.g. ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Branch to delete when ``delete_branch`` is
        ``True``, e.g. ``"feature/login"``. ``None`` skips branch
        deletion.
    :param delete_branch: When ``True``, run ``git branch -D`` on
        ``branch`` after removing the worktree directory.
    :raises WorktreeError: If the worktree path is missing/invalid, or
        a git command fails.
    """
    main_repo = _main_repo_for_worktree(worktree_path)
    remove_result = _run_git(
        ["worktree", "remove", "--force", worktree_path],
        cwd=main_repo,
    )
    if remove_result.returncode != 0:
        raise _git_error("git worktree remove failed", remove_result)
    if delete_branch and branch is not None:
        branch_result = _run_git(["branch", "-D", branch], cwd=main_repo)
        if branch_result.returncode != 0:
            raise _git_error("git branch -D failed", branch_result)
