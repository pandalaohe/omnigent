"""Host-side git worktree operations for session-start worktrees.

Runs ``git`` (via argv lists, never a shell) on the host in response to
``host.create_worktree`` / ``host.remove_worktree`` frames, and reads
display-only facts for ``host.folder_facts``. Branch names are validated
against git ref-format rules before reaching argv. See
designs/SESSION_GIT_WORKTREE.md.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, overload

from omnigent.git_urls import redact_remote_url

_logger = logging.getLogger(__name__)

# fetch/add can be slow on large repos; bound it so git can't hang the
# host's tunnel loop.
_GIT_TIMEOUT_S: float = 120.0

# Folder facts are display-only; a hung git (huge repo, network
# filesystem) must not hold the host's worker thread for minutes.
_FOLDER_FACTS_GIT_TIMEOUT_S: float = 10.0

# Max directory-collision suffixes (``-2`` .. ``-N``) before giving up.
_MAX_DIR_COLLISION_SUFFIX: int = 50

# Chars git refuses in a ref: space, control chars, ``~^:?*[\``, DEL.
# (``..``, leading ``-``/``.``, ``/`` edges, ``.lock``, ``@{`` are
# checked separately.)
_INVALID_BRANCH_CHARS = re.compile(r"[\x00-\x20~^:?*\[\\\x7f]")

# Longest accepted worktree location template (after trimming).
WORKTREE_PATH_TEMPLATE_MAX_CHARS: int = 512

# Tokens a worktree location template may substitute on the host.
WORKTREE_PATH_TEMPLATE_TOKENS: tuple[str, ...] = ("entry", "repo_parent", "repo", "branch")

_TEMPLATE_TOKEN_PATTERN = re.compile(r"\{([^{}]*)\}")
_TEMPLATE_DRIVE_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
_TEMPLATE_SEGMENT_SPLIT = re.compile(r"[/\\]")
_GITIGNORE_SPECIAL = re.compile(r"[*?\[\\]")

# Component names Windows reserves for devices; the name before the
# first dot is what counts (``con.txt`` is still ``CON``).
_WINDOWS_RESERVED_COMPONENTS = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)}
)
_WINDOWS_INVALID_COMPONENT_CHARS = frozenset('<>:"|?*')


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


def _exclude_worktree_dir(worktree_path: Path) -> None:
    """Add the worktree directory to the enclosing repo's exclude file.

    A worktree directory Omnigent creates inside another git working
    tree would otherwise show as untracked noise in that repository's
    status. The line is ``/<worktree dir relative to the tree's top
    level>/`` — only the worktree directory itself, never a container
    the template merely passes through — and is written at most once.
    Nothing is written when git already ignores the path, when the
    worktree lies outside any git working tree, and tracked ignore
    files (``.gitignore``) are never touched.

    :param worktree_path: Absolute worktree directory about to be
        created, e.g. ``Path("/Users/alice/project/.worktrees/myrepo/x")``.
    :raises WorktreeError: If the exclude file cannot be written.
    """
    parent = str(worktree_path.parent)
    top = _run_git(["rev-parse", "--show-toplevel"], cwd=parent)
    if top.returncode != 0:
        return
    toplevel = top.stdout.strip()
    # git reports a realpath top level (macOS /var -> /private/var).
    real_path = os.path.join(os.path.realpath(parent), worktree_path.name)
    relative = os.path.relpath(real_path, toplevel).replace(os.sep, "/")
    if relative.startswith("../") or relative == "..":
        return
    # Trailing slash: the directory does not exist yet, and directory-only rules need it.
    ignored = _run_git(["check-ignore", "-q", "--", relative + "/"], cwd=toplevel)
    if ignored.returncode == 0:
        return
    common = _run_git(["rev-parse", "--git-common-dir"], cwd=parent)
    if common.returncode != 0:
        return
    common_dir = common.stdout.strip()
    if not os.path.isabs(common_dir):
        common_dir = os.path.join(parent, common_dir)
    # Escaped so a name like ``[ab]`` matches itself, not ``a`` and ``b`` beside it.
    line = "/" + _GITIGNORE_SPECIAL.sub(r"\\\g<0>", relative) + "/"
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
        raise WorktreeError(f"could not write git exclude for {worktree_path}: {exc}") from exc


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


def list_worktrees(*, repo_path: str, for_cleanup: bool = False) -> list[WorktreeInfo]:
    """List the git worktrees of the repository containing ``repo_path``.

    Resolves the main work tree first (so a linked worktree resolves the
    same list as the main checkout), then parses
    ``git worktree list --porcelain``. The first record is always the
    main work tree; the rest are linked worktrees.

    :param repo_path: Absolute path inside a git repository — the
        directory the user picked, e.g. ``"/Users/alice/myrepo"``.
        If removed or replaced by a file, resolve from a surviving parent.
    :param for_cleanup: Recover a stored canonical workspace without following
        replacement symlinks. The caller must verify the recorded cleanup root.
    :returns: One :class:`WorktreeInfo` per worktree, main first.
    :raises WorktreeError: If ``repo_path`` is not a directory or not
        inside a git work tree, or if ``git worktree list`` fails.
    """
    search_path = Path(repo_path)
    # Picker paths may use symlinks; stored cleanup paths were already canonicalized.
    if for_cleanup:
        for ancestor in reversed((search_path, *search_path.parents)):
            if ancestor.is_symlink():
                search_path = ancestor.parent
                break
    while not search_path.is_dir() and search_path.parent != search_path:
        search_path = search_path.parent
    repo_root = _main_work_tree(str(search_path))
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


@dataclass
class FolderFacts:
    """Read-only git facts about one folder on the host.

    A missing path, a file, a folder outside any work tree, a bare
    repository, and a missing git binary are field values plus ``error``
    text — the settings dialog renders them; nothing raises.

    :param exists: Whether the path exists on the host.
    :param is_dir: Whether the path is a directory.
    :param is_repo: Whether the folder lies in a non-bare git work tree.
    :param toplevel: The work tree's root as git reports it, e.g.
        ``"/Users/alice/myrepo"``.
    :param branch: The checked-out branch, e.g. ``"main"``. ``None`` when
        HEAD is detached, unborn, or otherwise unresolvable.
    :param head: Full sha of the checked-out commit, or ``None``.
    :param detached: Whether HEAD points at a commit instead of a branch.
    :param dirty: ``True`` when ``git status`` lists any change, ``False``
        when clean, ``None`` when the status read timed out (unknown).
    :param remotes: Fetch remotes in ``git remote -v`` order, e.g.
        ``[{"name": "origin", "url": "https://h/x.git"}]``; URLs are
        credential-free.
    :param error: Why the facts are incomplete, or ``None`` when complete.
    """

    exists: bool
    is_dir: bool
    is_repo: bool
    toplevel: str | None = None
    branch: str | None = None
    head: str | None = None
    detached: bool = False
    dirty: bool | None = None
    remotes: list[dict[str, str]] = field(default_factory=list)
    error: str | None = None


def read_folder_facts(path: str) -> FolderFacts:
    """Read display-only git facts for a folder on the host.

    Every expected failure is a field value with ``error`` text, never an
    exception: a missing path, a file, a folder outside a git work tree, a
    bare repository, or a git binary that is not installed. Each git
    command is bounded by :data:`_FOLDER_FACTS_GIT_TIMEOUT_S`; a timed-out
    ``status`` leaves ``dirty`` unknown (``None``) instead of guessing.

    :param path: Absolute directory path on the host, e.g.
        ``"/Users/alice/myrepo"``.
    :returns: The folder's :class:`FolderFacts`.
    """
    if not os.path.exists(path):
        return FolderFacts(
            exists=False,
            is_dir=False,
            is_repo=False,
            error=f"path does not exist: {path}",
        )
    if not os.path.isdir(path):
        return FolderFacts(
            exists=True,
            is_dir=False,
            is_repo=False,
            error=f"path is not a directory: {path}",
        )
    try:
        top = _run_git(
            ["rev-parse", "--show-toplevel"], cwd=path, timeout=_FOLDER_FACTS_GIT_TIMEOUT_S
        )
    except WorktreeError as exc:
        return FolderFacts(exists=True, is_dir=True, is_repo=False, error=exc.message)
    if top.returncode != 0:
        stderr_lines = top.stderr.splitlines()
        if any(line.startswith("fatal: not a git repository") for line in stderr_lines):
            detail = f"not a git repository: {path}"
        else:
            detail = _git_error("git rev-parse --show-toplevel failed", top).message
        return FolderFacts(exists=True, is_dir=True, is_repo=False, error=detail)

    facts = FolderFacts(
        exists=True,
        is_dir=True,
        is_repo=True,
        toplevel=top.stdout.strip(),
    )
    try:
        branch = _run_git(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=path,
            timeout=_FOLDER_FACTS_GIT_TIMEOUT_S,
        )
        head = _run_git(["rev-parse", "HEAD"], cwd=path, timeout=_FOLDER_FACTS_GIT_TIMEOUT_S)
    except WorktreeError as exc:
        facts.error = exc.message
    else:
        if branch.returncode == 0:
            name = branch.stdout.strip()
            if name == "HEAD":
                # Detached: rev-parse names the pseudo-branch HEAD.
                facts.detached = True
            elif name:
                facts.branch = name
        if head.returncode == 0:
            facts.head = head.stdout.strip() or None
        if facts.branch is None and not facts.detached and head.returncode == 0:
            # A resolved HEAD that names no branch is detached in any shape.
            facts.detached = True

    try:
        status = _run_git(
            ["status", "--porcelain", "--untracked-files=normal"],
            cwd=path,
            timeout=_FOLDER_FACTS_GIT_TIMEOUT_S,
        )
    except WorktreeError as exc:
        facts.error = facts.error or exc.message
    else:
        if status.returncode == 0:
            facts.dirty = bool(status.stdout.strip())
        else:
            facts.error = facts.error or _git_error("git status failed", status).message

    try:
        remotes = _run_git(["remote", "-v"], cwd=path, timeout=_FOLDER_FACTS_GIT_TIMEOUT_S)
    except WorktreeError as exc:
        facts.error = facts.error or exc.message
    else:
        if remotes.returncode == 0:
            facts.remotes = _parse_folder_remotes(remotes.stdout)
        else:
            facts.error = facts.error or _git_error("git remote -v failed", remotes).message
    return facts


def _parse_folder_remotes(stdout: str) -> list[dict[str, str]]:
    """Parse the fetch lines of ``git remote -v`` into credential-free rows.

    :param stdout: Command stdout, e.g.
        ``"origin\\thttps://h/x.git (fetch)\\n"``.
    :returns: ``{"name", "url"}`` dicts in print order, exact duplicates
        removed; push and malformed lines are skipped.
    """
    remotes: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for line in stdout.splitlines():
        if not line.endswith("(fetch)"):
            continue
        name, sep, rest = line.partition("\t")
        if not sep:
            continue
        url = rest.removesuffix(" (fetch)").strip()
        if not name or not url:
            continue
        url = redact_remote_url(url)
        if (name, url) in seen:
            continue
        seen.add((name, url))
        remotes.append({"name": name, "url": url})
    return remotes


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


def validate_worktree_path_template(template: str) -> None:
    """Validate a worktree location template against the placement grammar.

    The grammar: segments separated by ``/`` (a Windows host also
    accepts ``\\``); the only substitutions are the four
    :data:`WORKTREE_PATH_TEMPLATE_TOKENS`; the first segment is exactly
    ``{entry}``, ``{repo_parent}`` or ``~``, or the whole template is an
    absolute literal (``/…`` or a drive ``X:/…`` / ``X:\\…``); the
    template must place worktrees per repository and branch, so
    ``{repo}`` and ``{branch}`` are both required. The server runs this
    when the setting is saved and the host again before rendering, so
    the message is user-facing.

    :param template: Raw template string, e.g.
        ``"{entry}/.worktrees/{repo}/{branch}"``.
    :raises WorktreeError: If any rule is broken; the message names
        the rule.
    """
    template = template.strip()
    if not template:
        raise WorktreeError("worktree location template must not be empty")
    if len(template) > WORKTREE_PATH_TEMPLATE_MAX_CHARS:
        raise WorktreeError(
            f"worktree location template must be at most "
            f"{WORKTREE_PATH_TEMPLATE_MAX_CHARS} characters"
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in template):
        raise WorktreeError("worktree location template must not contain control characters")
    for match in _TEMPLATE_TOKEN_PATTERN.finditer(template):
        name = match.group(1)
        if name not in WORKTREE_PATH_TEMPLATE_TOKENS:
            raise WorktreeError(
                f"worktree location template has unknown token {{{name}}}; "
                f"allowed tokens: {{entry}}, {{repo_parent}}, {{repo}}, {{branch}}"
            )
    remainder = template
    for token in WORKTREE_PATH_TEMPLATE_TOKENS:
        remainder = remainder.replace("{" + token + "}", "")
    if "{" in remainder or "}" in remainder:
        raise WorktreeError("worktree location template has a stray '{' or '}'")
    if "{repo}" not in template:
        raise WorktreeError("worktree location template must contain {repo}")
    if "{branch}" not in template:
        raise WorktreeError("worktree location template must contain {branch}")
    segments = _TEMPLATE_SEGMENT_SPLIT.split(template)
    first = segments[0]
    absolute = template.startswith("/") or _TEMPLATE_DRIVE_ABSOLUTE.match(template) is not None
    if first not in ("{entry}", "{repo_parent}", "~") and not absolute:
        raise WorktreeError(
            "worktree location template must start with {entry}, {repo_parent}, ~, "
            "or an absolute path"
        )
    if any("~" in segment for segment in segments[1:]):
        raise WorktreeError("worktree location template may use ~ only as the whole first segment")
    if any(segment in (".", "..") for segment in segments):
        raise WorktreeError("worktree location template must not contain '.' or '..' segments")


def _render_worktree_path_template(
    template: str, repo_root: str, branch_name: str, entry: str | None
) -> tuple[Path, Path | None]:
    """Substitute the template tokens into an absolute worktree path.

    ``{entry}`` falls back to the main work tree when the session has
    no entry. The anchor is the rendered first segment when that
    segment is ``{entry}``, ``{repo_parent}`` or ``~`` — the directory
    the worktree must stay inside — and ``None`` for an absolute
    literal.

    :param template: Validated template, e.g.
        ``"{entry}/.worktrees/{repo}/{branch}"``.
    :param repo_root: Absolute main work-tree root, e.g.
        ``"/Users/alice/myrepo"``.
    :param branch_name: Validated branch name, e.g. ``"feature/login"``.
    :param entry: Project entry directory on the host, or ``None``.
    :returns: ``(path, anchor)``, both absolute; ``anchor`` is ``None``
        for an absolute-literal template.
    :raises WorktreeError: If the rendered path is not absolute.
    """
    root = Path(repo_root)
    values = {
        "entry": entry if entry is not None else repo_root,
        "repo_parent": str(root.parent),
        "repo": root.name,
        "branch": _sanitize_dirname(branch_name),
    }
    # One pass, so a substituted value that itself contains ``{repo}`` is never re-expanded.
    rendered = _TEMPLATE_TOKEN_PATTERN.sub(lambda match: values[match.group(1)], template.strip())
    rendered = os.path.normpath(os.path.expanduser(rendered))
    if not os.path.isabs(rendered):
        raise WorktreeError(
            f"worktree location template renders to a non-absolute path: {rendered}"
        )
    first = _TEMPLATE_SEGMENT_SPLIT.split(template.strip())[0]
    anchor: Path | None = None
    if first == "~":
        anchor = Path(os.path.normpath(os.path.expanduser("~")))
    elif first in ("{entry}", "{repo_parent}"):
        anchor = Path(os.path.normpath(values[first.strip("{}")]))
    return Path(rendered), anchor


def _check_host_path_components(path: Path, *, windows: bool) -> None:
    """Refuse path components the host filesystem would reject.

    Only Windows needs the check: no component after the drive may be a
    reserved device name (``CON`` … ``LPT9``, the part before the first
    dot decides) or hold ``<>:"|?*``. Runs on the rendered path before
    any directory is created.

    :param path: Rendered worktree path.
    :param windows: Whether the host is Windows (``os.name == "nt"``).
    :raises WorktreeError: Naming the offending component.
    """
    if not windows:
        return
    for component in path.parts[1:]:
        stem = component.split(".", 1)[0].upper()
        if stem in _WINDOWS_RESERVED_COMPONENTS:
            raise WorktreeError(
                f"worktree location template renders a path component that is "
                f"reserved on Windows: {component}"
            )
        if any(char in _WINDOWS_INVALID_COMPONENT_CHARS for char in component):
            raise WorktreeError(
                f"worktree location template renders a path component with a "
                f"character Windows forbids: {component}"
            )


def _resolve_worktree_path(
    repo_root: str,
    branch_name: str,
    *,
    path_template: str | None = None,
    entry: str | None = None,
) -> tuple[Path, Path | None]:
    """Compute a collision-free worktree directory path.

    Without ``path_template`` the worktree goes to the sibling location
    ``<parent-of-repo-root>/<repo-name>-worktrees/<sanitized-branch>``
    (``entry`` is ignored). With ``path_template`` the template is
    validated and rendered and the worktree goes to the rendered path.
    Either way a numeric suffix is appended if the path already exists
    on disk.

    :param repo_root: Absolute repo work-tree root, e.g.
        ``"/Users/alice/myrepo"``.
    :param branch_name: Validated branch name, e.g.
        ``"feature/login"``.
    :param path_template: Worktree location template, e.g.
        ``"{entry}/.worktrees/{repo}/{branch}"``, or ``None`` for the
        sibling layout.
    :param entry: Project entry directory on the host, e.g.
        ``"/Users/alice/project"``; only fills the template's
        ``{entry}`` token.
    :returns: ``(path, anchor)`` — a path that does not yet exist, e.g.
        ``Path("/Users/alice/project/.worktrees/myrepo/feature-login")``,
        and the template's anchor directory (``None`` without a
        template or for an absolute-literal template).
    :raises WorktreeError: If the template is invalid, renders a
        non-absolute path, or no free path is found within
        :data:`_MAX_DIR_COLLISION_SUFFIX` attempts.
    """
    root = Path(repo_root)
    anchor: Path | None = None
    if path_template is None:
        base_dir = root.parent / f"{root.name}-worktrees"
        dirname = _sanitize_dirname(branch_name)
    else:
        validate_worktree_path_template(path_template)
        rendered, anchor = _render_worktree_path_template(
            path_template, repo_root, branch_name, entry
        )
        base_dir = rendered.parent
        dirname = rendered.name
    candidate = base_dir / dirname
    if not candidate.exists():
        return candidate, anchor
    for suffix in range(2, _MAX_DIR_COLLISION_SUFFIX + 1):
        candidate = base_dir / f"{dirname}-{suffix}"
        if not candidate.exists():
            return candidate, anchor
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
    :param workspace: Selected directory relocated into the new worktree.
    """

    worktree_path: str
    branch: str
    workspace: str


def create_worktree(
    *,
    repo_path: str,
    branch_name: str,
    base_branch: str | None = None,
    existing_branch: bool = False,
    entry: str | None = None,
    path_template: str | None = None,
) -> CreatedWorktree:
    """Create a git worktree with a new — or existing — branch checked out.

    Resolves the repo root, picks a collision-free directory, and runs
    ``git worktree add -b`` (fetching once if ``base_branch`` isn't
    locally resolvable). With ``existing_branch`` the branch must already
    exist and not be checked out in any live worktree; stale registrations
    (a worktree whose directory was deleted from disk) are pruned first,
    and the branch is checked out without ``-b`` — the recreate path for a
    deleted worktree.

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
    :param entry: The session project's entry directory on the host,
        e.g. ``"/Users/alice/project"``. Only fills the template's
        ``{entry}`` token.
    :param path_template: The user's worktree location template, e.g.
        ``"{entry}/.worktrees/{repo}/{branch}"``. When set, the
        worktree is created at the rendered path (refused when it
        resolves outside the template's anchor) and, if it lands inside
        another git working tree, that repository gains an
        ``info/exclude`` line naming the worktree directory. ``None``
        keeps the upstream sibling location under the repo's parent,
        with neither check.
    :returns: The worktree root, branch, and relocated selected directory.
    :raises WorktreeError: If the branch name is invalid, the path is
        not a git repo, the base ref can't be resolved,
        ``git worktree add`` fails (e.g. the branch already exists in
        create mode, is missing or still checked out in
        existing-branch mode), the template is invalid, or the
        worktree directory would resolve outside the template's anchor.
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
    prefix = _run_git(["rev-parse", "--show-prefix"], cwd=repo_path)
    if prefix.returncode != 0:
        raise _git_error("could not resolve selected repository directory", prefix)
    relative_directory = prefix.stdout.rstrip("\n")
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
    validated_commit: str | None = None
    if relative_directory:
        revision = f"refs/heads/{branch_name}" if existing_branch else (base_branch or "HEAD")
        resolved = _run_git(
            ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"], cwd=repo_root
        )
        if resolved.returncode != 0:
            raise _git_error("could not resolve worktree revision", resolved)
        validated_commit = resolved.stdout.strip()
        directory = _run_git(
            ["cat-file", "-t", f"{validated_commit}:{relative_directory.rstrip('/')}"],
            cwd=repo_root,
        )
        if directory.returncode != 0:
            raise WorktreeError(
                f"selected directory {relative_directory!r} does not exist in {revision!r}; "
                "choose another directory or base branch"
            )
        if directory.stdout.strip() != "tree":
            raise WorktreeError(
                f"selected path {relative_directory!r} is not a directory in {revision!r}; "
                "choose another directory or base branch"
            )
    worktree_path, anchor = _resolve_worktree_path(
        repo_root, branch_name, path_template=path_template, entry=entry
    )
    if path_template is None:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        _check_host_path_components(worktree_path, windows=os.name == "nt")
        escape = f"worktree directory {worktree_path.parent} escapes the template anchor {anchor}"
        if anchor is not None and not _contained_inside(
            os.path.realpath(worktree_path.parent), os.path.realpath(anchor)
        ):
            # Checked before creating anything too, so a symlinked
            # directory under the anchor leaves no directory behind.
            raise WorktreeError(escape)
        try:
            worktree_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorktreeError(
                f"could not create worktree directory {worktree_path.parent}: {exc}"
            ) from exc
        # Re-checked after ``makedirs``: a component that resolved inside
        # the anchor may be replaced by a link before the directory exists.
        if anchor is not None and not _contained_inside(
            os.path.realpath(worktree_path.parent), os.path.realpath(anchor)
        ):
            raise WorktreeError(escape)
        _exclude_worktree_dir(worktree_path)

    if existing_branch:
        # --end-of-options: treat the branch as a rev, never a git flag
        # (argv-only, no shell). No ``-b`` — the branch already exists.
        add_args = ["worktree", "add", str(worktree_path), "--end-of-options", branch_name]
    else:
        add_args = ["worktree", "add", "-b", branch_name, str(worktree_path)]
        if validated_commit is not None:
            # Let Git set up tracking from the requested ref before checking out the pinned commit.
            add_args.insert(2, "--no-checkout")
        if base_branch is not None:
            add_args += ["--end-of-options", base_branch]
    result = _run_git(add_args, cwd=repo_root)
    if result.returncode != 0:
        raise _git_error("git worktree add failed", result)
    if validated_commit is not None:
        try:
            if existing_branch:
                checked_out = _run_git(["rev-parse", "--verify", "HEAD"], cwd=str(worktree_path))
                if checked_out.returncode != 0:
                    raise _git_error("could not verify worktree revision", checked_out)
                if checked_out.stdout.strip() != validated_commit:
                    raise WorktreeError(
                        f"branch {branch_name!r} changed during worktree creation; retry"
                    )
            else:
                # This request owns the new branch and its not-yet-populated checkout.
                checkout = _run_git(
                    ["checkout", "--force", "-B", branch_name, validated_commit],
                    cwd=str(worktree_path),
                )
                if checkout.returncode != 0:
                    raise _git_error("could not check out validated worktree revision", checkout)
        except WorktreeError:
            try:
                remove_worktree(
                    worktree_path=str(worktree_path),
                    branch=branch_name,
                    delete_branch=not existing_branch,
                )
            except WorktreeError:
                _logger.warning("Could not roll back worktree %s", worktree_path, exc_info=True)
            raise
    return CreatedWorktree(
        worktree_path=str(worktree_path),
        branch=branch_name,
        workspace=str(worktree_path / relative_directory),
    )


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

    :param worktree_path: Canonical absolute root of the worktree to remove,
        e.g. ``"/Users/alice/myrepo-worktrees/feature-login"``.
    :param branch: Branch to delete when ``delete_branch`` is
        ``True``, e.g. ``"feature/login"``. ``None`` skips branch
        deletion.
    :param delete_branch: When ``True``, run ``git branch -D`` on
        ``branch`` after removing the worktree directory.
    :raises WorktreeError: If the worktree path is missing/invalid, or
        a git command fails.
    """
    if not Path(worktree_path).exists():
        raise WorktreeError(f"worktree path does not exist: {worktree_path}")
    if not Path(worktree_path).is_dir():
        raise WorktreeError(f"worktree path is not a directory: {worktree_path}")
    main_repo = _main_repo_for_worktree(worktree_path)
    root = _run_git(["rev-parse", "--show-toplevel"], cwd=worktree_path)
    if root.returncode != 0:
        raise _git_error("could not resolve worktree root", root)
    if os.path.normcase(root.stdout.strip()) != os.path.normcase(os.path.abspath(worktree_path)):
        raise WorktreeError(f"path is not the expected worktree root: {worktree_path}")
    remove_result = _run_git(
        ["worktree", "remove", "--force", root.stdout.strip()],
        cwd=main_repo,
    )
    if remove_result.returncode != 0:
        raise _git_error("git worktree remove failed", remove_result)
    if delete_branch and branch is not None:
        branch_result = _run_git(["branch", "-D", branch], cwd=main_repo)
        if branch_result.returncode != 0:
            raise _git_error("git branch -D failed", branch_result)
