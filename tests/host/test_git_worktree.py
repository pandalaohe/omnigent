"""Tests for host-side git worktree operations.

Exercises ``omnigent.host.git_worktree`` against real ``git`` in a
temp repository — the operations run actual ``git worktree add`` /
``remove`` / ``branch -D`` so a regression in argv construction, repo-
root resolution, or removal ordering fails loud here.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

import omnigent.host.git_worktree as git_worktree_module
from omnigent.host.git_worktree import (
    CreatedWorktree,
    WorktreeError,
    create_worktree,
    list_worktrees,
    read_folder_facts,
    read_worktree_status,
    remove_worktree,
    validate_branch_name,
    validate_worktree_path_template,
)

# The fork's worktree layout, now expressed as a location template
# (was hard-coded behind ``entry=``).
_ENTRY_TEMPLATE = "{entry}/.worktrees/{repo}/{branch}"

# Deterministic identity + config so the tests don't depend on the
# developer's global git config (user.name / init.defaultBranch).
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo: Path, *args: str) -> None:
    """Run a git command in ``repo``, raising on failure.

    :param repo: Repository directory to run in.
    :param args: Git arguments after ``git``, e.g. ``("add", ".")``.
    """
    import os

    subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
    )


def _current_branch(path: Path) -> str:
    """Return the checked-out branch name at ``path``.

    :param path: A work tree (main or linked worktree) directory.
    :returns: Branch name, e.g. ``"feature/login"``.
    """
    import os

    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=path,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout.strip()


def _rev_parse(path: Path, ref: str = "HEAD") -> str:
    """Return the commit sha that ``ref`` resolves to at ``path``.

    :param path: A work tree directory.
    :param ref: Ref to resolve, e.g. ``"HEAD"`` or ``"develop"``.
    :returns: The 40-char commit sha.
    """
    import os

    return subprocess.run(
        ["git", "rev-parse", ref],
        cwd=path,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout.strip()


def _branch_exists(repo: Path, branch: str) -> bool:
    """Return whether ``branch`` exists in ``repo``.

    :param repo: Repository directory.
    :param branch: Branch name to check, e.g. ``"feature/login"``.
    :returns: ``True`` if the local branch exists.
    """
    import os

    out = subprocess.run(
        ["git", "branch", "--list", branch],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout.strip()
    return out != ""


def _worktree_count(repo: Path) -> int:
    """Return how many worktrees are registered for ``repo``.

    :param repo: Repository directory.
    :returns: Worktree count, where ``1`` means only the main work
        tree exists (no linked worktree was added).
    """
    import os

    out = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
    ).stdout
    # --porcelain emits one "worktree <path>" line per worktree.
    return out.count("worktree ")


@pytest.fixture(autouse=True)
def _isolated_git_ignores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the developer's global and system git config out of every test.

    A global ignore such as ``/.worktrees/`` would otherwise decide
    whether the exclude tests see a line written. The ceiling keeps git's
    repo discovery from finding the checkout that hosts the pytest tmp
    directory, which would turn "not a repository" tests into repo hits.
    """
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


@pytest.fixture()
def git_repo(tmp_path: Path) -> Iterator[Path]:
    """Create a one-commit git repo and yield its resolved root.

    :returns: Iterator yielding the repo root path (realpath, so it
        matches what ``git rev-parse --show-toplevel`` returns).
    """
    # Resolve so comparisons match git's realpath output (macOS
    # /tmp -> /private/tmp).
    repo = (tmp_path / "myrepo").resolve()
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("hi")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    yield repo


def test_create_worktree_places_sibling_of_repo_root(git_repo: Path) -> None:
    """A new worktree lands at ``<repo>-worktrees/<branch>`` with the branch checked out."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/login")
    expected = git_repo.parent / "myrepo-worktrees" / "feature-login"
    # Path proves the sibling layout + slash->dash dir sanitization;
    # a regression in _resolve_worktree_path would change this.
    assert created.worktree_path == str(expected)
    assert created.workspace == str(expected)
    assert Path(created.worktree_path).is_dir()
    # The branch is actually checked out in the worktree (not just the dir made).
    assert _current_branch(Path(created.worktree_path)) == "feature/login"
    assert isinstance(created, CreatedWorktree)


def test_safe_archive_preserves_dirty_and_untracked_then_keeps_branch(git_repo: Path) -> None:
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/safe")
    worktree = Path(created.worktree_path)
    (worktree / "untracked.txt").write_text("keep")
    assert read_worktree_status(str(worktree))["files"] == [
        {"path": "untracked.txt", "status": "??"}
    ]
    with pytest.raises(WorktreeError, match="dirty"):
        remove_worktree(worktree_path=str(worktree), branch="feature/safe", safe_only=True)
    assert worktree.exists()
    (worktree / "untracked.txt").unlink()
    (worktree / "README.md").write_text("changed")
    with pytest.raises(WorktreeError, match="dirty"):
        remove_worktree(worktree_path=str(worktree), branch="feature/safe", safe_only=True)
    (worktree / "README.md").write_text("hi")
    (worktree / "committed.txt").write_text("work")
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-qm", "unmerged work")
    assert read_worktree_status(str(worktree))["merged"] is False
    remove_worktree(worktree_path=str(worktree), branch="feature/safe", safe_only=True)
    assert not worktree.exists()
    assert _branch_exists(git_repo, "feature/safe")


def test_safe_archive_rejects_main_and_binding_mismatch(git_repo: Path) -> None:
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/safe")
    with pytest.raises(WorktreeError, match="protected"):
        remove_worktree(worktree_path=str(git_repo), branch="main", safe_only=True)
    with pytest.raises(WorktreeError, match="binding"):
        remove_worktree(
            worktree_path=created.worktree_path, branch="feature/other", safe_only=True
        )
    _git(Path(created.worktree_path), "checkout", "--detach")
    with pytest.raises(WorktreeError, match="protected"):
        remove_worktree(worktree_path=created.worktree_path, branch="feature/safe", safe_only=True)
    assert Path(created.worktree_path).exists()


@pytest.mark.parametrize("linked", [False, True])
def test_create_worktree_resolves_repo_root_from_subdir(git_repo: Path, linked: bool) -> None:
    """Preserve a nested workspace from either the main checkout or a linked worktree."""
    relative = Path("packages") / "my app"
    sub = git_repo / relative
    sub.mkdir(parents=True)
    (sub / "README.md").write_text("nested project")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-qm", "add nested project")
    if linked:
        first = create_worktree(repo_path=str(git_repo), branch_name="first")
        sub = Path(first.worktree_path) / relative
    created = create_worktree(repo_path=str(sub), branch_name="wip")
    # Worktree placement stays anchored at the main repository.
    assert created.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "wip")
    assert created.workspace == str(Path(created.worktree_path) / relative)
    assert (Path(created.workspace) / "README.md").read_text() == "nested project"
    assert _current_branch(Path(created.workspace)) == "wip"

    with pytest.raises(WorktreeError, match="expected worktree root"):
        remove_worktree(worktree_path=created.workspace, branch="wip", delete_branch=True)
    remove_worktree(worktree_path=created.worktree_path, branch="wip", delete_branch=True)
    assert not Path(created.worktree_path).exists()
    assert not _branch_exists(git_repo, "wip")


@pytest.mark.parametrize("existing_branch", [False, True])
@pytest.mark.parametrize("base_path_kind", ["missing", "file"])
def test_create_worktree_missing_subdir_leaves_no_worktree(
    git_repo: Path, existing_branch: bool, base_path_kind: str
) -> None:
    """A directory missing from the target revision fails before creating a worktree."""
    sub = git_repo / "new-project"
    if base_path_kind == "file":
        sub.write_text("a file in the old revision")
        _git(git_repo, "add", ".")
        _git(git_repo, "commit", "-qm", "add file")
    _git(git_repo, "branch", "old-base")
    if base_path_kind == "file":
        sub.unlink()
    sub.mkdir()
    (sub / "README.md").write_text("new project")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-qm", "add project")
    message = "does not exist" if base_path_kind == "missing" else "is not a directory"
    with pytest.raises(WorktreeError, match=message):
        create_worktree(
            repo_path=str(sub),
            branch_name="old-base" if existing_branch else "new-worktree",
            base_branch=None if existing_branch else "old-base",
            existing_branch=existing_branch,
        )
    assert _worktree_count(git_repo) == 1
    assert not _branch_exists(git_repo, "new-worktree")
    assert _branch_exists(git_repo, "old-base")


def test_create_worktree_from_linked_worktree_anchors_at_main_repo(git_repo: Path) -> None:
    """Creating a worktree while inside a LINKED worktree anchors at the MAIN repo.

    Resolving the repo root naively (``rev-parse --show-toplevel``) from a
    linked worktree would nest the new worktree under it
    (``…/feature-a-worktrees/feature-b``). ``_main_work_tree`` resolves to
    the main checkout so worktrees stay siblings
    (``…/myrepo-worktrees/feature-b``) — the fork-resume picker prefills a
    worktree as the source session's workspace, so this is the common path.
    """
    # First worktree, created off the main repo.
    first = create_worktree(repo_path=str(git_repo), branch_name="feature/a")
    first_path = Path(first.worktree_path)
    assert first_path == git_repo.parent / "myrepo-worktrees" / "feature-a"

    # Second worktree, requested from INSIDE the first (linked) worktree.
    second = create_worktree(repo_path=str(first_path), branch_name="feature/b")

    # Sibling of the MAIN repo, NOT nested under the first worktree. A
    # regression to --show-toplevel would put it under
    # ``feature-a-worktrees/`` and this fails.
    assert second.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "feature-b")
    assert "feature-a-worktrees" not in second.worktree_path
    assert Path(second.worktree_path).is_dir()
    assert _current_branch(Path(second.worktree_path)) == "feature/b"


def test_create_worktree_from_base_branch(git_repo: Path) -> None:
    """A worktree branches from the explicit base ref's tip, not HEAD."""
    # Advance develop with its own commit so it differs from main —
    # otherwise the test would pass even if base_branch were ignored
    # (both would resolve to the same single commit).
    _git(git_repo, "checkout", "-q", "-b", "develop")
    (git_repo / "dev.txt").write_text("dev-only")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-q", "-m", "dev commit")
    _git(git_repo, "checkout", "-q", "main")

    created = create_worktree(
        repo_path=str(git_repo), branch_name="from-develop", base_branch="develop"
    )
    assert _current_branch(Path(created.worktree_path)) == "from-develop"
    # Points at develop's tip, not main's — proves base_branch routed
    # the new branch to develop rather than falling back to HEAD.
    assert _rev_parse(Path(created.worktree_path)) == _rev_parse(git_repo, "develop")
    assert _rev_parse(Path(created.worktree_path)) != _rev_parse(git_repo, "main")


def test_create_worktree_unknown_base_branch_fails(git_repo: Path) -> None:
    """An unresolvable base ref fails loud (after the best-effort fetch)."""
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="x", base_branch="nope-not-a-branch")
    # Proves _ensure_base_resolvable rejects rather than silently
    # branching from HEAD when the requested base is missing.
    assert "base branch does not exist" in exc.value.message


@pytest.mark.parametrize("option_like", ["-f", "--exec-path"])
def test_create_worktree_option_like_base_branch_not_executed(
    git_repo: Path, option_like: str
) -> None:
    """A base_branch that looks like a git flag is rejected, never executed.

    ``base_branch`` is user-supplied and reaches ``git rev-parse`` and
    ``git worktree add`` argv. An option-like value (e.g. ``"-f"``, which
    is ``git worktree add``'s ``--force``) must be treated as an
    unresolvable rev, not parsed as a flag. This guards the end-to-end
    security property at the public API: the ref-resolution pre-check and
    the ``--end-of-options`` argv terminators together keep such a value
    from creating a worktree. A regression that let ``"-f"`` through as a
    flag would build a worktree from the wrong base (and force-create it)
    instead of failing — so the assertion below would see a linked
    worktree appear.
    """
    with pytest.raises(WorktreeError):
        create_worktree(repo_path=str(git_repo), branch_name="from-flag", base_branch=option_like)
    # Still only the main work tree — no linked worktree was added, proving
    # git treated the value as a (rejected) rev rather than a flag that
    # would have run `worktree add`. If `-f` were parsed as --force, the
    # count would be 2.
    assert _worktree_count(git_repo) == 1


def test_create_worktree_duplicate_branch_fails(git_repo: Path) -> None:
    """Creating two worktrees for the same branch name fails loud with the friendly error."""
    create_worktree(repo_path=str(git_repo), branch_name="dup")
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="dup")
    # The pre-check catches the existing branch before git's raw error;
    # we must NOT silently reuse the existing worktree.
    assert "already exists" in exc.value.message


def test_create_worktree_existing_branch_no_worktree_fails(git_repo: Path) -> None:
    """A branch that exists WITHOUT a worktree is still rejected by the pre-check.

    Proves the pre-check keys off branch existence, not directory
    occupancy — creating a worktree for a plain pre-existing branch
    would otherwise hit git's raw error.
    """
    _git(git_repo, "branch", "preexisting")
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="preexisting")
    assert "already exists" in exc.value.message
    assert "preexisting" in exc.value.message


def test_create_worktree_existing_branch_recreates_after_dir_deleted(git_repo: Path) -> None:
    """A branch whose worktree directory was deleted can be checked back out.

    Simulates the deleted-worktree fork: the directory is rm'd from disk
    (leaving a stale registration), then ``existing_branch=True`` prunes
    the stale entry and adds a fresh worktree for the same branch.
    """
    import shutil

    created = create_worktree(repo_path=str(git_repo), branch_name="fix-1")
    shutil.rmtree(created.worktree_path)
    recreated = create_worktree(repo_path=str(git_repo), branch_name="fix-1", existing_branch=True)
    assert recreated.branch == "fix-1"
    assert Path(recreated.worktree_path).is_dir()
    assert _current_branch(Path(recreated.worktree_path)) == "fix-1"


def test_create_worktree_existing_branch_missing_branch_fails(git_repo: Path) -> None:
    """``existing_branch=True`` for a branch that doesn't exist fails loud."""
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="ghost", existing_branch=True)
    assert "does not exist" in exc.value.message
    assert _worktree_count(git_repo) == 1


def test_create_worktree_existing_branch_live_worktree_fails(git_repo: Path) -> None:
    """``existing_branch=True`` refuses a branch checked out in a LIVE worktree.

    Two sessions must never share one working tree; only a stale (deleted-
    from-disk) registration is pruned, a live one aborts.
    """
    create_worktree(repo_path=str(git_repo), branch_name="busy")
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(git_repo), branch_name="busy", existing_branch=True)
    assert "already checked out" in exc.value.message
    assert _worktree_count(git_repo) == 2


def test_create_worktree_existing_branch_rejects_base_branch(git_repo: Path) -> None:
    """``existing_branch`` + ``base_branch`` is contradictory and rejected."""
    _git(git_repo, "branch", "have")
    with pytest.raises(WorktreeError) as exc:
        create_worktree(
            repo_path=str(git_repo),
            branch_name="have",
            base_branch="main",
            existing_branch=True,
        )
    assert "base_branch" in exc.value.message


def test_create_worktree_non_repo_fails(tmp_path: Path) -> None:
    """A directory that isn't a git repo is rejected."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(plain), branch_name="x")
    assert "not a git repository" in exc.value.message


def test_create_worktree_entry_places_under_the_entry(git_repo: Path, tmp_path: Path) -> None:
    """With our template the worktree lands at ``<entry>/.worktrees/<repo>/<branch>``."""
    entry = (tmp_path / "project").resolve()
    created = create_worktree(
        repo_path=str(git_repo),
        branch_name="feature/login",
        entry=str(entry),
        path_template=_ENTRY_TEMPLATE,
    )
    assert created.worktree_path == str(entry / ".worktrees" / "myrepo" / "feature-login")
    assert Path(created.worktree_path).is_dir()
    assert _current_branch(Path(created.worktree_path)) == "feature/login"


def test_create_worktree_entry_from_linked_worktree_names_main_repo(
    git_repo: Path, tmp_path: Path
) -> None:
    """A linked-worktree source still names the MAIN worktree under the entry."""
    first = create_worktree(repo_path=str(git_repo), branch_name="feature/a")
    entry = (tmp_path / "project").resolve()
    second = create_worktree(
        repo_path=first.worktree_path,
        branch_name="feature/b",
        entry=str(entry),
        path_template=_ENTRY_TEMPLATE,
    )
    # ``myrepo`` is the main checkout's directory name, not ``feature-a``.
    assert second.worktree_path == str(entry / ".worktrees" / "myrepo" / "feature-b")


def test_create_worktree_entry_collision_gets_numeric_suffix(
    git_repo: Path, tmp_path: Path
) -> None:
    """A taken topic under the entry takes today's ``-2`` suffix."""
    entry = (tmp_path / "project").resolve()
    base = entry / ".worktrees" / "myrepo"
    base.mkdir(parents=True)
    (base / "feat-x").mkdir()
    created = create_worktree(
        repo_path=str(git_repo),
        branch_name="feat/x",
        entry=str(entry),
        path_template=_ENTRY_TEMPLATE,
    )
    assert created.worktree_path == str(base / "feat-x-2")
    assert _current_branch(Path(created.worktree_path)) == "feat/x"


def test_create_worktree_without_entry_keeps_sibling_layout(
    git_repo: Path, tmp_path: Path
) -> None:
    """``entry=None`` keeps the sibling path byte-identical to today's."""
    created = create_worktree(repo_path=str(git_repo), branch_name="wip", entry=None)
    assert created.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "wip")


def test_create_worktree_entry_refuses_symlinked_worktrees_dir(
    git_repo: Path, tmp_path: Path
) -> None:
    """A ``.worktrees`` symlinked out of the entry is refused; nothing is created."""
    entry = (tmp_path / "project").resolve()
    entry.mkdir()
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    (entry / ".worktrees").symlink_to(elsewhere)

    with pytest.raises(WorktreeError) as exc:
        create_worktree(
            repo_path=str(git_repo),
            branch_name="escape",
            entry=str(entry),
            path_template=_ENTRY_TEMPLATE,
        )

    assert "escapes the template anchor" in exc.value.message
    assert str(entry) in exc.value.message
    assert str(entry / ".worktrees" / "myrepo") in exc.value.message
    assert list(elsewhere.iterdir()) == []
    assert _worktree_count(git_repo) == 1


def test_create_worktree_existing_branch_entry_places_under_the_entry(
    git_repo: Path, tmp_path: Path
) -> None:
    """The existing-branch recreate path uses the entry layout too."""
    import shutil

    entry = (tmp_path / "project").resolve()
    created = create_worktree(
        repo_path=str(git_repo),
        branch_name="fix-1",
        entry=str(entry),
        path_template=_ENTRY_TEMPLATE,
    )
    shutil.rmtree(created.worktree_path)
    recreated = create_worktree(
        repo_path=str(git_repo),
        branch_name="fix-1",
        existing_branch=True,
        entry=str(entry),
        path_template=_ENTRY_TEMPLATE,
    )
    assert recreated.worktree_path == str(entry / ".worktrees" / "myrepo" / "fix-1")
    assert _current_branch(Path(recreated.worktree_path)) == "fix-1"


def test_create_worktree_bare_main_repository_refused(tmp_path: Path) -> None:
    """A bare main repository has no work tree to link and is refused."""
    bare = (tmp_path / "bare.git").resolve()
    bare.mkdir()
    _git(bare, "init", "-q", "--bare", "-b", "main")
    with pytest.raises(WorktreeError) as exc:
        create_worktree(repo_path=str(bare), branch_name="x")
    assert "bare" in exc.value.message


def test_create_worktree_entry_writes_exclude_at_repo_root(git_repo: Path, tmp_path: Path) -> None:
    """A fresh entry repo gains one exclude line per worktree, never the container."""
    entry = (tmp_path / "project").resolve()
    entry.mkdir()
    _git(entry, "init", "-q", "-b", "main")
    create_worktree(
        repo_path=str(git_repo), branch_name="one", entry=str(entry), path_template=_ENTRY_TEMPLATE
    )
    create_worktree(
        repo_path=str(git_repo), branch_name="two", entry=str(entry), path_template=_ENTRY_TEMPLATE
    )
    lines = (entry / ".git" / "info" / "exclude").read_text().splitlines()
    assert "/.worktrees/myrepo/one/" in lines
    assert "/.worktrees/myrepo/two/" in lines
    assert "/.worktrees/" not in lines
    # git init seeds the file with comment lines; the two worktree
    # lines are the only non-comment content.
    assert [line for line in lines if not line.startswith("#")] == [
        "/.worktrees/myrepo/one/",
        "/.worktrees/myrepo/two/",
    ]


def test_create_worktree_entry_exclude_already_ignored_writes_no_line(
    git_repo: Path, tmp_path: Path
) -> None:
    """A repo already ignoring ``/.worktrees/`` gains no new exclude line."""
    entry = (tmp_path / "project").resolve()
    entry.mkdir()
    _git(entry, "init", "-q", "-b", "main")
    exclude = entry / ".git" / "info" / "exclude"
    exclude.write_text("/.worktrees/\n")
    create_worktree(
        repo_path=str(git_repo), branch_name="one", entry=str(entry), path_template=_ENTRY_TEMPLATE
    )
    assert exclude.read_text().splitlines() == ["/.worktrees/"]


def test_create_worktree_entry_writes_exclude_from_subdirectory(
    git_repo: Path, tmp_path: Path
) -> None:
    """An entry below a repo top level gains its relative path in the exclude."""
    repo = (tmp_path / "outer").resolve()
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    entry = repo / "sub" / "project"
    create_worktree(
        repo_path=str(git_repo), branch_name="one", entry=str(entry), path_template=_ENTRY_TEMPLATE
    )
    lines = (repo / ".git" / "info" / "exclude").read_text().splitlines()
    assert [line for line in lines if not line.startswith("#")] == [
        "/sub/project/.worktrees/myrepo/one/"
    ]


def test_create_worktree_entry_outside_git_writes_no_exclude(
    git_repo: Path, tmp_path: Path
) -> None:
    """An entry outside any git working tree writes no exclude file."""
    entry = (tmp_path / "plain-project").resolve()
    created = create_worktree(
        repo_path=str(git_repo), branch_name="one", entry=str(entry), path_template=_ENTRY_TEMPLATE
    )
    assert Path(created.worktree_path).is_dir()
    assert not (entry / ".git").exists()


def test_create_worktree_unset_template_ignores_entry(git_repo: Path, tmp_path: Path) -> None:
    """Unset template keeps the sibling layout even with an entry; the entry is untouched."""
    entry = (tmp_path / "project").resolve()
    created = create_worktree(repo_path=str(git_repo), branch_name="wip", entry=str(entry))
    assert created.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "wip")
    assert not entry.exists()


def test_create_worktree_upstream_template_matches_unset(git_repo: Path) -> None:
    """The upstream layout written as a template renders the same path as unset."""
    created = create_worktree(
        repo_path=str(git_repo),
        branch_name="wip",
        path_template="{repo_parent}/{repo}-worktrees/{branch}",
    )
    assert created.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "wip")


def test_create_worktree_entry_template_without_entry_uses_repo_root(git_repo: Path) -> None:
    """``{entry}`` falls back to the main work tree when the session has no entry."""
    created = create_worktree(
        repo_path=str(git_repo),
        branch_name="feature/login",
        entry=None,
        path_template=_ENTRY_TEMPLATE,
    )
    assert created.worktree_path == str(git_repo / ".worktrees" / "myrepo" / "feature-login")
    assert _current_branch(Path(created.worktree_path)) == "feature/login"


def test_create_worktree_home_template_expands_user(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leading ``~`` renders under the host user's home directory."""
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    created = create_worktree(
        repo_path=str(git_repo), branch_name="wip", path_template="~/wt/{repo}/{branch}"
    )
    assert created.worktree_path == str(home / "wt" / "myrepo" / "wip")
    assert _current_branch(Path(created.worktree_path)) == "wip"


def test_create_worktree_unset_template_matches_upstream_formula(git_repo: Path) -> None:
    """Unset keeps upstream's sibling path for every source shape."""
    import shutil

    # A linked-worktree source anchors at the main repo.
    first = create_worktree(repo_path=str(git_repo), branch_name="feature/a")
    second = create_worktree(repo_path=first.worktree_path, branch_name="feature/b")
    assert second.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "feature-b")

    # A subdirectory source relocates ``workspace`` into the same subdirectory.
    sub = git_repo / "pkg"
    sub.mkdir()
    (sub / "README.md").write_text("nested")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-qm", "add pkg")
    nested = create_worktree(repo_path=str(sub), branch_name="wip")
    assert nested.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "wip")
    assert nested.workspace == str(Path(nested.worktree_path) / "pkg")
    assert (Path(nested.workspace) / "README.md").read_text() == "nested"

    # An existing-branch recreate lands at the same sibling path.
    shutil.rmtree(nested.worktree_path)
    recreated = create_worktree(repo_path=str(git_repo), branch_name="wip", existing_branch=True)
    assert recreated.worktree_path == str(git_repo.parent / "myrepo-worktrees" / "wip")


def test_create_worktree_template_exclude_names_only_the_worktree_dir(
    git_repo: Path, tmp_path: Path
) -> None:
    """A template passing through a container excludes the worktree dir, not the container."""
    entry = (tmp_path / "project").resolve()
    entry.mkdir()
    _git(entry, "init", "-q", "-b", "main")
    created = create_worktree(
        repo_path=str(git_repo),
        branch_name="feat",
        entry=str(entry),
        path_template="{entry}/src/wt/{repo}/{branch}",
    )
    assert created.worktree_path == str(entry / "src" / "wt" / "myrepo" / "feat")
    lines = (entry / ".git" / "info" / "exclude").read_text().splitlines()
    assert [line for line in lines if not line.startswith("#")] == ["/src/wt/myrepo/feat/"]
    assert "/src/" not in lines


def _ignored(repo: Path, relative: str) -> bool:
    """Whether git ignores ``relative`` (a directory path) inside ``repo``."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", "--", relative], cwd=repo, capture_output=True, check=False
    )
    return result.returncode == 0


def test_create_worktree_template_exclude_escapes_glob_characters(
    git_repo: Path, tmp_path: Path
) -> None:
    """A ``[ab]`` directory in the path is excluded literally, never as a glob over siblings."""
    entry = (tmp_path / "project").resolve()
    entry.mkdir()
    _git(entry, "init", "-q", "-b", "main")
    create_worktree(
        repo_path=str(git_repo),
        branch_name="feat",
        entry=str(entry),
        path_template="{entry}/src/[ab]/{repo}/{branch}",
    )
    lines = (entry / ".git" / "info" / "exclude").read_text().splitlines()
    assert [line for line in lines if not line.startswith("#")] == ["/src/\\[ab]/myrepo/feat/"]
    assert _ignored(entry, "src/[ab]/myrepo/feat/")
    assert not _ignored(entry, "src/a/myrepo/feat/")
    assert not _ignored(entry, "src/b/myrepo/feat/")


def test_create_worktree_template_exclude_respects_directory_rule(
    git_repo: Path, tmp_path: Path
) -> None:
    """A ``.gitignore`` rule naming the worktree directory itself means no exclude line."""
    entry = (tmp_path / "project").resolve()
    entry.mkdir()
    _git(entry, "init", "-q", "-b", "main")
    (entry / ".gitignore").write_text("/src/wt/myrepo/feat/\n")
    create_worktree(
        repo_path=str(git_repo),
        branch_name="feat",
        entry=str(entry),
        path_template="{entry}/src/wt/{repo}/{branch}",
    )
    exclude = entry / ".git" / "info" / "exclude"
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    assert [line for line in lines if not line.startswith("#")] == []


def test_create_worktree_home_template_refuses_symlink_escape(
    git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlinked directory escaping a non-entry anchor is refused; nothing is created."""
    home = (tmp_path / "home").resolve()
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    (home / "wt").symlink_to(outside)

    with pytest.raises(WorktreeError) as exc:
        create_worktree(
            repo_path=str(git_repo), branch_name="escape", path_template="~/wt/{repo}/{branch}"
        )

    assert "escapes the template anchor" in exc.value.message
    assert str(home) in exc.value.message
    assert str(home / "wt" / "myrepo") in exc.value.message
    assert list(outside.iterdir()) == []
    assert _worktree_count(git_repo) == 1


def test_create_worktree_mkdir_failure_is_worktree_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a template, a refused parent-directory creation is a WorktreeError naming the path."""
    real_mkdir = Path.mkdir

    def refuse_worktrees_dir(self: Path, *args: object, **kwargs: object) -> None:
        if self.name == "myrepo-worktrees":
            raise PermissionError("denied")
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", refuse_worktrees_dir)
    with pytest.raises(WorktreeError) as exc:
        create_worktree(
            repo_path=str(git_repo),
            branch_name="x",
            path_template="{repo_parent}/{repo}-worktrees/{branch}",
        )
    assert "could not create worktree directory" in exc.value.message
    assert "myrepo-worktrees" in exc.value.message


@pytest.mark.parametrize(
    "template",
    [
        # Relative: no anchor token, no absolute path.
        "wt/{repo}/{branch}",
        # A token outside the four allowed names.
        "{entry}/{foo}/{repo}/{branch}",
        # {branch} is required so worktrees of one repo stay distinct.
        "{entry}/.worktrees/{repo}",
        # {repo} is required so worktrees of different repos stay distinct.
        "{entry}/.worktrees/{branch}",
        # No '..' segments.
        "{entry}/../{repo}/{branch}",
        # A brace that is not part of a token.
        "{entry}/{repo}/{branch}{",
        # '~' is only valid as the whole first segment.
        "~x/{repo}/{branch}",
        # A first-segment token must be the whole segment.
        "{entry}x/{repo}/{branch}",
        # 513 characters after trimming.
        "{entry}/" + "a" * 489 + "/{repo}/{branch}",
        # A control character.
        "{entry}/bad\tdir/{repo}/{branch}",
    ],
)
def test_validate_worktree_path_template_rejects_bad(template: str) -> None:
    """Broken templates are refused with a message naming the rule."""
    with pytest.raises(WorktreeError, match="worktree location template"):
        validate_worktree_path_template(template)


@pytest.mark.parametrize(
    "template",
    [
        _ENTRY_TEMPLATE,
        "{repo_parent}/{repo}-worktrees/{branch}",
        "~/wt/{repo}/{branch}",
        "/data/wt/{repo}/{branch}",
        "C:/wt/{repo}/{branch}",
    ],
)
def test_validate_worktree_path_template_accepts_good(template: str) -> None:
    """The fork value, the upstream layout, and home / absolute forms pass."""
    validate_worktree_path_template(template)  # must not raise


@pytest.mark.parametrize(
    ("path", "component"),
    [
        ("C:/wt/CON/x", "CON"),
        ("C:/wt/con.txt/x", "con.txt"),
        ("C:/wt/a:b/x", "a:b"),
    ],
)
def test_check_host_path_components_windows_rejects(path: str, component: str) -> None:
    """A reserved device name (any extension) or a forbidden char is refused."""
    with pytest.raises(WorktreeError) as exc:
        git_worktree_module._check_host_path_components(Path(path), windows=True)
    assert component in exc.value.message


def test_check_host_path_components_windows_accepts_ordinary_names() -> None:
    """``console`` merely starts with a reserved name; it is not one."""
    git_worktree_module._check_host_path_components(Path("C:/wt/console/x"), windows=True)


def test_remove_worktree_deletes_dir_and_branch(git_repo: Path) -> None:
    """``delete_branch=True`` removes the directory AND the branch."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/login")
    remove_worktree(
        worktree_path=created.worktree_path, branch="feature/login", delete_branch=True
    )
    # Directory gone (git worktree remove --force ran)...
    assert not Path(created.worktree_path).exists()
    # ...and the branch deleted (git branch -D ran, after the worktree
    # was removed — git would refuse otherwise).
    assert not _branch_exists(git_repo, "feature/login")


def test_remove_worktree_keeps_branch_when_flag_false(git_repo: Path) -> None:
    """``delete_branch=False`` removes the directory but keeps the branch."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/keep")
    remove_worktree(
        worktree_path=created.worktree_path, branch="feature/keep", delete_branch=False
    )
    assert not Path(created.worktree_path).exists()
    # Branch survives — only the checkout directory was removed.
    assert _branch_exists(git_repo, "feature/keep")


def test_remove_worktree_missing_path_fails(git_repo: Path) -> None:
    """Removing a non-existent worktree path fails loud."""
    with pytest.raises(WorktreeError) as exc:
        remove_worktree(
            worktree_path=str(git_repo.parent / "myrepo-worktrees" / "ghost"),
            branch=None,
            delete_branch=False,
        )
    assert "does not exist" in exc.value.message


def test_list_worktrees_returns_main_first(git_repo: Path) -> None:
    """With no linked worktrees, only the main tree is listed."""
    result = list_worktrees(repo_path=str(git_repo))
    assert len(result) == 1
    main = result[0]
    assert main.path == str(git_repo)
    assert main.branch == "main"
    assert main.is_main is True
    assert main.detached is False
    assert isinstance(main.updated_at, int)


@pytest.mark.parametrize(
    "remote_url",
    [
        "https://github.com/omnigent-ai/omnigent.git",
        "https://gitlab.com/acme/repo.git",
        "git@github-personal:omnigent-ai/omnigent.git",
        "ssh://git@github.enterprise.example/omnigent-ai/omnigent.git",
        "https://[invalid/repo",
    ],
)
def test_list_worktrees_does_not_depend_on_remote_url(git_repo: Path, remote_url: str) -> None:
    """Local worktree support is independent of the configured remote."""
    _git(git_repo, "remote", "add", "origin", remote_url)
    result = list_worktrees(repo_path=str(git_repo))
    assert len(result) == 1
    assert result[0].path == str(git_repo)
    assert result[0].branch == "main"
    assert result[0].is_main is True


def test_list_worktrees_includes_linked(git_repo: Path) -> None:
    """A created worktree shows up with its branch and is not flagged main."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/login")
    result = list_worktrees(repo_path=str(git_repo))
    # Main first, then the linked worktree.
    assert result[0].is_main is True
    linked = next(w for w in result if not w.is_main)
    assert linked.path == created.worktree_path
    assert linked.branch == "feature/login"
    assert linked.detached is False
    assert isinstance(linked.updated_at, int)


def test_list_worktrees_fetches_all_timestamps_with_one_git_command(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Timestamp metadata stays O(1) as the number of worktrees grows."""
    for index in range(4):
        create_worktree(repo_path=str(git_repo), branch_name=f"feature/{index}")

    original_run_git = git_worktree_module._run_git
    show_calls: list[list[str]] = []

    def run_git(args: list[str], *, cwd: str) -> subprocess.CompletedProcess[str]:
        if args[:3] == ["show", "-s", "--format=%H%x00%ct"]:
            show_calls.append(args)
        return original_run_git(args, cwd=cwd)

    monkeypatch.setattr(git_worktree_module, "_run_git", run_git)

    result = list_worktrees(repo_path=str(git_repo))

    assert len(result) == 5
    assert len(show_calls) == 1
    assert len(show_calls[0][3:]) == len({_rev_parse(Path(worktree.path)) for worktree in result})
    assert all(isinstance(worktree.updated_at, int) for worktree in result)


def test_list_worktrees_from_linked_resolves_same_list(git_repo: Path) -> None:
    """Listing from inside a linked worktree resolves the main repo's full list."""
    created = create_worktree(repo_path=str(git_repo), branch_name="feature/a")
    nested = Path(created.worktree_path) / "nested"
    nested.mkdir()
    # A subdirectory of the linked worktree resolves both checkouts.
    result = list_worktrees(repo_path=str(nested))
    paths = {w.path for w in result}
    assert str(git_repo) in paths
    assert created.worktree_path in paths


def test_list_worktrees_reports_detached_head(git_repo: Path) -> None:
    """A detached-HEAD worktree lists with ``branch=None`` and ``detached=True``."""
    head = _rev_parse(git_repo)
    wt = git_repo.parent / "myrepo-worktrees" / "detached"
    wt.parent.mkdir(parents=True, exist_ok=True)
    # Add a worktree checked out at a bare commit → detached HEAD.
    _git(git_repo, "worktree", "add", "--detach", str(wt), head)
    result = list_worktrees(repo_path=str(git_repo))
    detached = next(w for w in result if w.path == str(wt))
    assert detached.branch is None
    assert detached.detached is True


def test_list_worktrees_non_git_path_fails(tmp_path: Path) -> None:
    """A non-git directory fails loud (the route maps this to 'no worktrees')."""
    plain = (tmp_path / "plain").resolve()
    plain.mkdir()
    with pytest.raises(WorktreeError) as exc:
        list_worktrees(repo_path=str(plain))
    assert exc.value.message == f"not a git repository: {plain}"


def test_list_worktrees_preserves_invalid_config_error(git_repo: Path) -> None:
    """A broken config is not evidence that an existing repository is non-Git."""
    (git_repo / ".git" / "config").write_text("[broken\n")
    with pytest.raises(WorktreeError, match=r"git worktree list failed.*bad config line"):
        list_worktrees(repo_path=str(git_repo))


@pytest.mark.parametrize(
    "stderr",
    [
        "fatal: cannot access '.git/config': Permission denied",
        "fatal: detected dubious ownership in repository at '/repo'",
        "",
    ],
)
def test_list_worktrees_preserves_probe_failure(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, stderr: str
) -> None:
    """Unknown probe failures retain diagnostics instead of claiming non-Git."""
    monkeypatch.setenv("LC_ALL", "fr_FR.UTF-8")
    failed = subprocess.CompletedProcess(
        args=["git", "worktree", "list", "--porcelain"],
        returncode=128,
        stdout="",
        stderr=stderr,
    )
    with patch("omnigent.host.git_worktree.subprocess.run", return_value=failed) as run:
        with pytest.raises(WorktreeError) as exc:
            list_worktrees(repo_path=str(git_repo))
    suffix = f": {stderr}" if stderr else ""
    assert exc.value.message == f"git worktree list failed (exit 128){suffix}"
    assert run.call_args.kwargs["env"]["LC_ALL"] == "C"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "-leading",
        "a..b",
        "a/.hidden",
        "x.lock",
        "x.lock/y",
        "a b",
        "a~b",
        "a:b",
        "/lead",
        "trail/",
    ],
)
def test_validate_branch_name_rejects_bad(bad: str) -> None:
    """Branch names violating git ref-format are rejected before reaching argv."""
    with pytest.raises(WorktreeError):
        validate_branch_name(bad)


@pytest.mark.parametrize("good", ["feature/login", "fix-123", "a/b/c", "release_2", "v1.2"])
def test_validate_branch_name_accepts_good(good: str) -> None:
    """Well-formed branch names pass validation."""
    validate_branch_name(good)  # must not raise


@pytest.mark.parametrize("branch_has_directory", [True, False])
def test_existing_branch_directory_probe_ignores_same_named_tag(
    git_repo: Path, branch_has_directory: bool
) -> None:
    """Directory validation must inspect the branch that worktree add checks out."""
    before = _rev_parse(git_repo)
    source = git_repo / "web"
    source.mkdir()
    (source / "index.txt").write_text("tracked")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "add web")
    after = _rev_parse(git_repo)
    _git(git_repo, "branch", "feature", after if branch_has_directory else before)
    _git(git_repo, "tag", "feature", before if branch_has_directory else after)
    if branch_has_directory:
        created = create_worktree(
            repo_path=str(source), branch_name="feature", existing_branch=True
        )
        assert Path(created.workspace).is_dir()
        assert (
            next(
                tree.branch
                for tree in list_worktrees(repo_path=created.worktree_path)
                if tree.path == created.worktree_path
            )
            == "feature"
        )
    else:
        with pytest.raises(WorktreeError, match="does not exist"):
            create_worktree(repo_path=str(source), branch_name="feature", existing_branch=True)


@pytest.mark.parametrize("replacement", ["file", "symlink"])
def test_remove_worktree_rejects_replaced_root(git_repo: Path, replacement: str) -> None:
    """A stale cleanup path must not remove a symlink target or raise an unhandled OS error."""
    original = create_worktree(repo_path=str(git_repo), branch_name="original")
    target = create_worktree(repo_path=str(git_repo), branch_name="target")
    remove_worktree(worktree_path=original.worktree_path)
    path = Path(original.worktree_path)
    if replacement == "file":
        path.write_text("replacement")
    else:
        path.symlink_to(target.worktree_path, target_is_directory=True)
    with pytest.raises(WorktreeError):
        remove_worktree(worktree_path=str(path), branch="original", delete_branch=True)
    assert Path(target.worktree_path).is_dir()
    assert _branch_exists(git_repo, "original")
    assert _branch_exists(git_repo, "target")


def test_worktree_picker_accepts_symlinked_prefix(git_repo: Path) -> None:
    """Picker paths may include a legitimate symlink such as macOS /tmp."""
    alias = git_repo.parent / "alias"
    alias.symlink_to(git_repo.parent, target_is_directory=True)
    created = create_worktree(repo_path=str(git_repo), branch_name="feature")
    trees = list_worktrees(repo_path=str(alias / git_repo.name))
    assert [tree.path for tree in trees] == [str(git_repo), created.worktree_path]


@pytest.mark.parametrize("mode", ["base", "head", "existing"])
def test_directory_validation_survives_revision_moving(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Ref movement cannot make creation succeed with a missing session directory."""
    before = _rev_parse(git_repo)
    source = git_repo / "web"
    source.mkdir()
    (source / "index.txt").write_text("tracked")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "web")
    validated = _rev_parse(git_repo)
    _git(git_repo, "branch", "moving")
    real_run = git_worktree_module._run_git
    moved = False

    def move_after_validation(args: list[str], *, cwd: str) -> subprocess.CompletedProcess[str]:
        nonlocal moved
        result = real_run(args, cwd=cwd)
        if args[:2] == ["cat-file", "-t"] and not moved:
            ref = "main" if mode == "head" else "moving"
            _git(git_repo, "update-ref", f"refs/heads/{ref}", before)
            moved = True
        return result

    monkeypatch.setattr(git_worktree_module, "_run_git", move_after_validation)
    if mode == "existing":
        with pytest.raises(WorktreeError, match="changed during worktree creation"):
            create_worktree(repo_path=str(source), branch_name="moving", existing_branch=True)
        assert len(list_worktrees(repo_path=str(git_repo))) == 1
        assert _rev_parse(git_repo, "refs/heads/moving") == before
    else:
        created = create_worktree(
            repo_path=str(source),
            branch_name="new",
            base_branch="moving" if mode == "base" else None,
        )
        assert Path(created.workspace).is_dir()
        assert _rev_parse(Path(created.worktree_path)) == validated
    assert moved


@pytest.mark.parametrize("auto_track", ["true", "false", "always", "simple", "inherit"])
def test_subdirectory_creation_preserves_remote_tracking(git_repo: Path, auto_track: str) -> None:
    """Pinned checkouts retain Git's native tracking policy for the requested start ref."""
    source = git_repo / "web"
    source.mkdir()
    (source / "index.txt").write_text("tracked")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "web")
    _git(git_repo, "remote", "add", "origin", str(git_repo))
    _git(git_repo, "fetch", "origin")
    _git(git_repo, "config", "branch.autoSetupMerge", auto_track)
    root_created = create_worktree(
        repo_path=str(git_repo), branch_name="root-pick", base_branch="origin/main"
    )
    nested_created = create_worktree(
        repo_path=str(source), branch_name="nested-pick", base_branch="origin/main"
    )
    root_upstream = _rev_parse(Path(root_created.worktree_path), "@{upstream}")
    nested_upstream = _rev_parse(Path(nested_created.worktree_path), "@{upstream}")
    assert nested_upstream == root_upstream
    assert Path(nested_created.workspace).is_dir()


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_failed_pinned_checkout_rolls_back_without_hiding_original_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, rollback_fails: bool
) -> None:
    """Checkout errors remain visible even when rollback also fails."""
    source = git_repo / "web"
    source.mkdir()
    (source / "index.txt").write_text("tracked")
    _git(git_repo, "add", ".")
    _git(git_repo, "commit", "-m", "web")
    real_run = git_worktree_module._run_git

    def fail_checkout(args: list[str], *, cwd: str) -> subprocess.CompletedProcess[str]:
        if args[:2] == ["checkout", "--force"]:
            return subprocess.CompletedProcess(args, 1, "", "checkout failed")
        if rollback_fails and args[:2] == ["worktree", "remove"]:
            return subprocess.CompletedProcess(args, 1, "", "rollback failed")
        return real_run(args, cwd=cwd)

    monkeypatch.setattr(git_worktree_module, "_run_git", fail_checkout)
    with pytest.raises(WorktreeError, match="could not check out validated"):
        create_worktree(repo_path=str(source), branch_name="new")
    assert _branch_exists(git_repo, "new") is rollback_fails
    assert len(list_worktrees(repo_path=str(git_repo))) == (2 if rollback_fails else 1)


def test_read_folder_facts_clean_repo(git_repo: Path) -> None:
    """A clean checkout reports git's own branch, HEAD and work-tree root."""
    facts = read_folder_facts(str(git_repo))
    assert facts.exists is True
    assert facts.is_dir is True
    assert facts.is_repo is True
    assert facts.toplevel == _rev_parse(git_repo, "--show-toplevel")
    assert facts.branch == "main"
    assert facts.default_branch == "main"
    assert facts.head == _rev_parse(git_repo, "HEAD")
    assert facts.detached is False
    assert facts.dirty is False
    assert facts.remotes == []
    assert facts.error is None


def test_folder_default_branch_prefers_remote_head_over_current_branch(git_repo: Path) -> None:
    """A feature checkout must keep the remote's main branch as the base."""
    _git(git_repo, "remote", "add", "origin", "https://git.example.test/team/repo.git")
    _git(git_repo, "update-ref", "refs/remotes/origin/trunk", "HEAD")
    _git(git_repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    _git(git_repo, "checkout", "-b", "feature/settings")
    facts = read_folder_facts(str(git_repo))
    assert facts.branch == "feature/settings"
    assert facts.default_branch == "trunk"


def test_folder_default_branch_falls_back_to_master_then_unknown(git_repo: Path) -> None:
    """Without a remote HEAD, master is known; a lone feature branch is not."""
    _git(git_repo, "branch", "-m", "master")
    _git(git_repo, "checkout", "-b", "feature/settings")
    assert read_folder_facts(str(git_repo)).default_branch == "master"
    _git(git_repo, "branch", "-D", "master")
    assert read_folder_facts(str(git_repo)).default_branch is None


def test_read_folder_facts_dirty_repo(git_repo: Path) -> None:
    """An untracked file marks the folder dirty."""
    (git_repo / "new-file.txt").write_text("x")
    facts = read_folder_facts(str(git_repo))
    assert facts.dirty is True
    assert facts.error is None


def test_read_folder_facts_detached_head(git_repo: Path) -> None:
    """A detached HEAD reports no branch and ``detached`` true."""
    _git(git_repo, "checkout", "--detach", "HEAD")
    facts = read_folder_facts(str(git_repo))
    assert facts.is_repo is True
    assert facts.branch is None
    assert facts.head == _rev_parse(git_repo, "HEAD")
    assert facts.detached is True


def test_read_folder_facts_redacts_remote_credentials(git_repo: Path) -> None:
    """Remote URLs leave the host helper credential-free."""
    _git(
        git_repo,
        "remote",
        "add",
        "origin",
        "https://user:token@git.example.test/x.git?private_token=t#f",
    )
    facts = read_folder_facts(str(git_repo))
    assert facts.remotes == [{"name": "origin", "url": "https://git.example.test/x.git"}]


def test_read_folder_facts_not_a_repo(tmp_path: Path) -> None:
    """A plain directory is a field value, not an exception."""
    plain = tmp_path / "plain"
    plain.mkdir()
    facts = read_folder_facts(str(plain))
    assert facts.exists is True
    assert facts.is_dir is True
    assert facts.is_repo is False
    assert facts.branch is None
    assert facts.head is None
    assert facts.dirty is None
    assert facts.error is not None and "not a git repository" in facts.error


def test_read_folder_facts_missing_path(tmp_path: Path) -> None:
    """A missing path reports exists=false with error text."""
    facts = read_folder_facts(str(tmp_path / "missing"))
    assert facts.exists is False
    assert facts.is_dir is False
    assert facts.is_repo is False
    assert facts.error is not None and "does not exist" in facts.error


def test_read_folder_facts_file_path(tmp_path: Path) -> None:
    """A file is not a directory and not a repo."""
    file_path = tmp_path / "a-file.txt"
    file_path.write_text("x")
    facts = read_folder_facts(str(file_path))
    assert facts.exists is True
    assert facts.is_dir is False
    assert facts.is_repo is False
    assert facts.error is not None and "not a directory" in facts.error


def test_read_folder_facts_bare_repo(tmp_path: Path) -> None:
    """A bare repository has no work tree to read."""
    bare = (tmp_path / "bare.git").resolve()
    _git(tmp_path, "init", "-q", "--bare", str(bare))
    facts = read_folder_facts(str(bare))
    assert facts.exists is True
    assert facts.is_dir is True
    assert facts.is_repo is False
    assert facts.error is not None


def test_read_folder_facts_status_timeout_leaves_dirty_unknown(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out status read reports dirty=None instead of a guess."""
    real_run = git_worktree_module._run_git

    def timeout_status(
        args: list[str], *, cwd: str, timeout: float = 10.0
    ) -> subprocess.CompletedProcess[str]:
        if args[0] == "status":
            raise WorktreeError("git command timed out after 10s")
        return real_run(args, cwd=cwd, timeout=timeout)

    monkeypatch.setattr(git_worktree_module, "_run_git", timeout_status)
    facts = read_folder_facts(str(git_repo))
    assert facts.is_repo is True
    assert facts.dirty is None
    assert facts.error is not None and "timed out" in facts.error
