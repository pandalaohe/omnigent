"""Tests for host-side assignment worktree preparation and release.

Exercises ``omnigent.host.assignment_workspace`` against real ``git`` in
temp repositories: a bare "remote", a source working copy cloned from it,
and input commits pushed to ``refs/omnigent/assignments/<id>/input/<name>``
— the same ref layout the dispatch tool publishes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.host.assignment_workspace import prepare, release
from omnigent.host.frames import (
    HostAssignmentPrepareRepository,
    HostAssignmentReleaseRepository,
)
from omnigent.project_context import manifest_digest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not on PATH")

# Deterministic identity so the tests don't depend on the developer's
# global git config (user.name / init.defaultBranch).
_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_ASSIGNMENT_ID = "0123456789abcdef0123456789abcdef"
_MANIFEST_PATH = ".agents/project/manifest.json"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd`` without asserting, returning the process."""
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        text=True,
        check=False,
    )


def _git_ok(cwd: Path, *args: str) -> str:
    """Run git in ``cwd``, asserting success and returning stdout."""
    result = _git(cwd, *args)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout.strip()


def _make_remote_and_source(tmp_path: Path, name: str = "myrepo") -> tuple[Path, Path]:
    """Create a bare remote and a source working copy with one commit.

    :returns: ``(remote, source)`` as resolved absolute paths.
    """
    remote = (tmp_path / f"{name}-remote").resolve()
    remote.mkdir()
    _git_ok(remote, "init", "-q", "-b", "main", "--bare")
    source = (tmp_path / name).resolve()
    source.mkdir()
    _git_ok(source, "init", "-q", "-b", "main")
    (source / "README.md").write_text("hi\n")
    _git_ok(source, "add", ".")
    _git_ok(source, "commit", "-q", "-m", "init")
    _git_ok(source, "remote", "add", "origin", str(remote))
    return remote, source


def _commit_all(source: Path, message: str) -> str:
    """Commit everything in ``source`` and return the new HEAD sha."""
    _git_ok(source, "add", "-A")
    _git_ok(source, "commit", "-q", "-m", message)
    return _git_ok(source, "rev-parse", "HEAD")


def _write_manifest(source: Path, payload: dict[str, object]) -> None:
    """Write the manifest file in ``source`` (uncommitted)."""
    manifest = source / _MANIFEST_PATH
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload))


def _push_input(
    source: Path, commit: str, name: str = "root", assignment_id: str = _ASSIGNMENT_ID
) -> None:
    """Publish ``commit`` as the assignment input ref on the remote."""
    _git_ok(
        source,
        "push",
        "-q",
        "origin",
        f"{commit}:refs/omnigent/assignments/{assignment_id}/input/{name}",
    )


def _entry(
    source: Path,
    remote: Path,
    commit: str,
    digest: str,
    name: str = "root",
    manifest_path: str = _MANIFEST_PATH,
    assignment_id: str = _ASSIGNMENT_ID,
) -> HostAssignmentPrepareRepository:
    """Build a prepare entry pointing at the local test repos."""
    return HostAssignmentPrepareRepository(
        repository_name=name,
        source_directory=str(source),
        remote_url=str(remote),
        input_ref=f"refs/omnigent/assignments/{assignment_id}/input/{name}",
        input_commit=commit,
        context_manifest_path=manifest_path,
        manifest_digest=digest,
    )


def _release_entry(source: Path, name: str = "root") -> HostAssignmentReleaseRepository:
    """Build a release entry pointing at the local test source."""
    return HostAssignmentReleaseRepository(repository_name=name, source_directory=str(source))


def _setup_basic(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """Remote + source with a manifest commit pushed as the input ref.

    :returns: ``(remote, source, commit, digest)``.
    """
    remote, source = _make_remote_and_source(tmp_path)
    _write_manifest(
        source,
        {
            "version": 1,
            "context": [".agents/project/PROJECT.md"],
            "instructions": ["AGENTS.md"],
        },
    )
    (source / ".agents" / "project" / "PROJECT.md").write_text("context\n")
    (source / "AGENTS.md").write_text("instructions\n")
    commit = _commit_all(source, "add manifest and context")
    blob = _git_ok(source, "cat-file", "blob", f"{commit}:{_MANIFEST_PATH}")
    digest = manifest_digest(blob.encode("utf-8"))
    _push_input(source, commit)
    return remote, source, commit, digest


def _worktree_count(source: Path) -> int:
    """Count registered worktrees (1 = main only)."""
    return _git_ok(source, "worktree", "list", "--porcelain").count("worktree ")


def _exclude_lines(source: Path) -> list[str]:
    """Read the repo's info/exclude lines."""
    return (source / ".git" / "info" / "exclude").read_text().splitlines()


def _tracked_snapshot(source: Path) -> tuple[dict[str, bytes], str]:
    """Snapshot every file outside .git/.omnigent plus porcelain status."""
    files: dict[str, bytes] = {}
    for path in sorted(source.rglob("*")):
        if ".git" in path.parts or ".omnigent" in path.parts:
            continue
        if path.is_file():
            files[str(path.relative_to(source))] = path.read_bytes()
    return files, _git_ok(source, "status", "--porcelain")


def test_prepare_happy_path(tmp_path: Path) -> None:
    """A prepare checks out the pinned commit under .omnigent/worktrees."""
    remote, source, commit, digest = _setup_basic(tmp_path)

    result = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)

    assert result.status == "ok"
    expected = os.path.join(
        os.path.realpath(source), ".omnigent", "worktrees", _ASSIGNMENT_ID, "root"
    )
    assert result.directories == {"root": expected}
    assert _git_ok(Path(expected), "rev-parse", "HEAD") == commit
    assert _exclude_lines(source).count("/.omnigent/") == 1

    # A second prepare reuses the worktree instead of adding another one.
    again = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)
    assert again.status == "ok"
    assert again.directories == result.directories
    assert _worktree_count(source) == 2
    assert _exclude_lines(source).count("/.omnigent/") == 1


def test_prepare_leaves_dirty_source_untouched(tmp_path: Path) -> None:
    """Uncommitted work in the bound directory is byte-identical after prepare."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    (source / "README.md").write_text("local edits\n")
    (source / "scratch.txt").write_text("untracked notes\n")
    before_files, before_status = _tracked_snapshot(source)
    assert "README.md" in before_status and "scratch.txt" in before_status

    result = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)

    assert result.status == "ok"
    assert _tracked_snapshot(source) == (before_files, before_status)
    assert ".omnigent" not in _git_ok(source, "status", "--porcelain")


def test_prepare_missing_context_leaves_no_worktree(tmp_path: Path) -> None:
    """A manifest path absent at the commit fails naming repository:path."""
    remote, source = _make_remote_and_source(tmp_path)
    _write_manifest(source, {"version": 1, "context": ["nope.md"]})
    commit = _commit_all(source, "manifest names a missing path")
    blob = _git_ok(source, "cat-file", "blob", f"{commit}:{_MANIFEST_PATH}")
    _push_input(source, commit)

    result = prepare(
        [_entry(source, remote, commit, manifest_digest(blob.encode("utf-8")))],
        _ASSIGNMENT_ID,
    )

    assert result.status == "failed"
    assert result.error_code == "context_missing"
    assert result.repository_name == "root"
    assert "root:nope.md" in (result.error or "")
    assert _worktree_count(source) == 1


def test_prepare_commit_mismatch(tmp_path: Path) -> None:
    """An input commit the ref does not resolve to is refused."""
    remote, source, _commit, digest = _setup_basic(tmp_path)

    result = prepare([_entry(source, remote, "f" * 40, digest)], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "commit_mismatch"
    assert result.repository_name == "root"
    assert _worktree_count(source) == 1


def test_prepare_digest_mismatch(tmp_path: Path) -> None:
    """A manifest blob hashing differently than pinned is refused."""
    remote, source, commit, _ = _setup_basic(tmp_path)

    result = prepare([_entry(source, remote, commit, "sha256:" + "f" * 64)], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "manifest_digest_mismatch"
    assert _worktree_count(source) == 1


def test_prepare_missing_manifest(tmp_path: Path) -> None:
    """An input commit without the manifest file is refused."""
    remote, source = _make_remote_and_source(tmp_path)
    commit = _git_ok(source, "rev-parse", "HEAD")
    _push_input(source, commit)

    result = prepare([_entry(source, remote, commit, "sha256:" + "0" * 64)], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "manifest_missing"
    assert _worktree_count(source) == 1


def test_prepare_fetch_failed(tmp_path: Path) -> None:
    """An unreachable remote fails with git's trimmed stderr."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    entry = _entry(source, remote, commit, digest)
    broken = HostAssignmentPrepareRepository(
        repository_name=entry.repository_name,
        source_directory=entry.source_directory,
        remote_url=str(tmp_path / "no-such-remote"),
        input_ref=entry.input_ref,
        input_commit=entry.input_commit,
        context_manifest_path=entry.context_manifest_path,
        manifest_digest=entry.manifest_digest,
    )

    result = prepare([broken], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "fetch_failed"
    assert _worktree_count(source) == 1


@pytest.mark.parametrize("source_arg", ["plain-dir", "missing-dir"])
def test_prepare_not_a_working_copy(tmp_path: Path, source_arg: str) -> None:
    """A plain directory (or a missing one) is not a valid source."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    target = plain if source_arg == "plain-dir" else tmp_path / "missing-dir"
    entry = _entry(source, remote, commit, digest)
    broken = HostAssignmentPrepareRepository(
        repository_name=entry.repository_name,
        source_directory=str(target),
        remote_url=entry.remote_url,
        input_ref=entry.input_ref,
        input_commit=entry.input_commit,
        context_manifest_path=entry.context_manifest_path,
        manifest_digest=entry.manifest_digest,
    )

    result = prepare([broken], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "source_invalid"


def test_prepare_second_repository_failure_rolls_back_first(tmp_path: Path) -> None:
    """A later repository's failure removes the earlier worktree again."""
    remote_root, source_root = _make_remote_and_source(tmp_path, "root-repo")
    _write_manifest(
        source_root,
        {
            "version": 1,
            "context": ["AGENTS.md"],
            "cross_repo": [{"repository": "docs", "paths": ["api.md"]}],
        },
    )
    (source_root / "AGENTS.md").write_text("instructions\n")
    commit_root = _commit_all(source_root, "manifest with cross_repo")
    blob = _git_ok(source_root, "cat-file", "blob", f"{commit_root}:{_MANIFEST_PATH}")
    _push_input(source_root, commit_root, name="root")

    remote_docs, source_docs = _make_remote_and_source(tmp_path, "docs-repo")
    (source_docs / "api.md").write_text("api\n")
    commit_docs = _commit_all(source_docs, "api doc")
    _push_input(source_docs, commit_docs, name="docs")

    result = prepare(
        [
            _entry(
                source_root,
                remote_root,
                commit_root,
                manifest_digest(blob.encode("utf-8")),
                name="root",
            ),
            _entry(
                source_docs,
                remote_docs,
                commit_docs,
                manifest_digest(b'{"version": 1}'),
                name="docs",
            ),
        ],
        _ASSIGNMENT_ID,
    )

    # The docs repo has no manifest at its commit, so its own entry fails.
    assert result.status == "failed"
    assert result.error_code == "manifest_missing"
    assert result.repository_name == "docs"
    assert _worktree_count(source_root) == 1
    assert _worktree_count(source_docs) == 1


def test_prepare_cross_repo_happy_path(tmp_path: Path) -> None:
    """Two repositories with a cross_repo path present in the sibling."""
    remote_root, source_root = _make_remote_and_source(tmp_path, "root-repo")
    _write_manifest(
        source_root,
        {
            "version": 1,
            "context": ["AGENTS.md"],
            "cross_repo": [{"repository": "docs", "paths": ["api.md"]}],
        },
    )
    (source_root / "AGENTS.md").write_text("instructions\n")
    commit_root = _commit_all(source_root, "manifest with cross_repo")
    blob_root = _git_ok(source_root, "cat-file", "blob", f"{commit_root}:{_MANIFEST_PATH}")
    _push_input(source_root, commit_root, name="root")

    remote_docs, source_docs = _make_remote_and_source(tmp_path, "docs-repo")
    _write_manifest(source_docs, {"version": 1})
    (source_docs / "api.md").write_text("api\n")
    commit_docs = _commit_all(source_docs, "api doc and manifest")
    blob_docs = _git_ok(source_docs, "cat-file", "blob", f"{commit_docs}:{_MANIFEST_PATH}")
    _push_input(source_docs, commit_docs, name="docs")

    result = prepare(
        [
            _entry(
                source_root,
                remote_root,
                commit_root,
                manifest_digest(blob_root.encode("utf-8")),
                name="root",
            ),
            _entry(
                source_docs,
                remote_docs,
                commit_docs,
                manifest_digest(blob_docs.encode("utf-8")),
                name="docs",
            ),
        ],
        _ASSIGNMENT_ID,
    )

    assert result.status == "ok"
    assert set(result.directories) == {"root", "docs"}
    assert _git_ok(Path(result.directories["root"]), "rev-parse", "HEAD") == commit_root
    assert _git_ok(Path(result.directories["docs"]), "rev-parse", "HEAD") == commit_docs


def test_prepare_unknown_cross_repo_is_manifest_invalid(tmp_path: Path) -> None:
    """A cross_repo entry naming no assignment repository names it."""
    remote, source = _make_remote_and_source(tmp_path)
    _write_manifest(
        source,
        {"version": 1, "cross_repo": [{"repository": "ghost", "paths": ["x.md"]}]},
    )
    commit = _commit_all(source, "manifest names an unknown repo")
    blob = _git_ok(source, "cat-file", "blob", f"{commit}:{_MANIFEST_PATH}")
    _push_input(source, commit)

    result = prepare(
        [_entry(source, remote, commit, manifest_digest(blob.encode("utf-8")))],
        _ASSIGNMENT_ID,
    )

    assert result.status == "failed"
    assert result.error_code == "manifest_invalid"
    assert "'ghost'" in (result.error or "")
    assert _worktree_count(source) == 1


def test_release_removes_worktree_and_assignment_dir(tmp_path: Path) -> None:
    """Release removes the worktree and the assignment directory when empty."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    prepared = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)
    assert prepared.status == "ok"
    worktree = Path(prepared.directories["root"])
    assert worktree.is_dir()

    result = release([_release_entry(source)], _ASSIGNMENT_ID)

    assert result.status == "ok"
    assert result.removed == ["root"]
    assert result.failures == {}
    assert not worktree.exists()
    assert not (source / ".omnigent" / "worktrees" / _ASSIGNMENT_ID).exists()

    # A missing worktree counts as removed.
    repeat = release([_release_entry(source)], _ASSIGNMENT_ID)
    assert repeat.status == "ok"
    assert repeat.removed == ["root"]


def test_release_with_uncommitted_changes_reports_and_keeps(tmp_path: Path) -> None:
    """A dirty worktree is reported, never force-removed."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    prepared = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)
    assert prepared.status == "ok"
    worktree = Path(prepared.directories["root"])
    (worktree / "AGENTS.md").write_text("dirty in the worktree\n")

    result = release([_release_entry(source)], _ASSIGNMENT_ID)

    assert result.status == "partial"
    assert result.removed == []
    assert "root" in result.failures
    assert worktree.is_dir()
    assert (worktree / "AGENTS.md").read_text() == "dirty in the worktree\n"

    _git_ok(worktree, "checkout", "--", ".")
    clean = release([_release_entry(source)], _ASSIGNMENT_ID)
    assert clean.status == "ok"
    assert clean.removed == ["root"]


def test_prepare_ordinary_directory_at_worktree_path_is_not_reused(tmp_path: Path) -> None:
    """A plain directory at the worktree path is refused, never adopted."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    target = source / ".omnigent" / "worktrees" / _ASSIGNMENT_ID / "root"
    target.mkdir(parents=True)
    marker = target / "notes.txt"
    marker.write_text("mine\n")

    result = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "worktree_failed"
    assert str(target) in (result.error or "")
    assert marker.read_text() == "mine\n"
    assert _worktree_count(source) == 1


def test_prepare_stale_registration_reports_path_without_pruning(tmp_path: Path) -> None:
    """A stale registration blocks add with the path named; others are not pruned."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    prepared = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)
    assert prepared.status == "ok"
    worktree = Path(prepared.directories["root"])
    sibling = source / ".omnigent" / "worktrees" / _ASSIGNMENT_ID / "sibling"
    _git_ok(source, "worktree", "add", "--detach", "--", str(sibling), commit)
    shutil.rmtree(sibling)
    shutil.rmtree(worktree)

    result = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "worktree_failed"
    assert str(worktree) in (result.error or "")
    assert str(sibling) in _git_ok(source, "worktree", "list", "--porcelain")


@pytest.mark.parametrize(
    "bad_name", ["/abs", "../escape", "..", ".hidden", "a/b", "x.lock", "trailing.", ""]
)
def test_prepare_invalid_repository_name_refused_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_name: str
) -> None:
    """A hostile repository name fails naming the field, with no git call made."""
    from omnigent.host import assignment_workspace

    remote, source, commit, digest = _setup_basic(tmp_path)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("no git call allowed")

    monkeypatch.setattr(assignment_workspace, "_run_git", _boom)

    result = prepare([_entry(source, remote, commit, digest, name=bad_name)], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "source_invalid"
    assert "repository_name" in (result.error or "")


def test_prepare_invalid_assignment_id_refused_before_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-hex assignment id fails naming the field, with no git call made."""
    from omnigent.host import assignment_workspace

    remote, source, commit, digest = _setup_basic(tmp_path)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("no git call allowed")

    monkeypatch.setattr(assignment_workspace, "_run_git", _boom)

    result = prepare([_entry(source, remote, commit, digest)], "asg_test01")

    assert result.status == "failed"
    assert result.error_code == "source_invalid"
    assert "assignment_id" in (result.error or "")


def test_prepare_unexpected_input_ref_refused_before_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An input ref outside the assignment's namespace is refused before fetching."""
    from omnigent.host import assignment_workspace

    remote, source, commit, digest = _setup_basic(tmp_path)

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("no git call allowed")

    monkeypatch.setattr(assignment_workspace, "_run_git", _boom)
    entry = _entry(source, remote, commit, digest)
    forged = HostAssignmentPrepareRepository(
        repository_name=entry.repository_name,
        source_directory=entry.source_directory,
        remote_url=entry.remote_url,
        input_ref="refs/heads/other",
        input_commit=entry.input_commit,
        context_manifest_path=entry.context_manifest_path,
        manifest_digest=entry.manifest_digest,
    )

    result = prepare([forged], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "fetch_failed"
    assert "unexpected input ref" in (result.error or "")


@pytest.mark.parametrize("bad_value", ["xyz", "F" * 40, "0" * 39, "0" * 65])
def test_prepare_malformed_input_commit_refused(tmp_path: Path, bad_value: str) -> None:
    """A non-hex input commit is refused without trusting it in git argv."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    entry = _entry(source, remote, commit, digest)
    forged = HostAssignmentPrepareRepository(
        repository_name=entry.repository_name,
        source_directory=entry.source_directory,
        remote_url=entry.remote_url,
        input_ref=entry.input_ref,
        input_commit=bad_value,
        context_manifest_path=entry.context_manifest_path,
        manifest_digest=entry.manifest_digest,
    )

    result = prepare([forged], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "commit_mismatch"
    assert _worktree_count(source) == 1


def test_prepare_escaping_manifest_path_refused(tmp_path: Path) -> None:
    """A manifest path escaping the repo is refused before any blob read."""
    remote, source, commit, digest = _setup_basic(tmp_path)

    result = prepare(
        [_entry(source, remote, commit, digest, manifest_path="../escape.json")],
        _ASSIGNMENT_ID,
    )

    assert result.status == "failed"
    assert result.error_code == "manifest_invalid"
    assert _worktree_count(source) == 1


def test_prepare_symlinked_omnigent_dir_refused(tmp_path: Path) -> None:
    """A `.omnigent` symlink pointing outside the checkout fails containment."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (source / ".omnigent").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not supported")

    result = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "worktree_failed"
    assert _worktree_count(source) == 1


def test_prepare_symlinked_omnigent_with_outside_worktree_refused_before_reuse(
    tmp_path: Path,
) -> None:
    """A symlinked `.omnigent` is refused before any registered worktree is reused."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    outside = (tmp_path / "outside").resolve()
    outside.mkdir()
    try:
        (source / ".omnigent").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not supported")
    target = source / ".omnigent" / "worktrees" / _ASSIGNMENT_ID / "root"
    _git_ok(source, "worktree", "add", "--detach", "--", str(target), commit)
    outside_target = outside / "worktrees" / _ASSIGNMENT_ID / "root"
    assert outside_target.is_dir()
    assert _git_ok(outside_target, "rev-parse", "HEAD") == commit

    result = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)

    assert result.status == "failed"
    assert result.error_code == "worktree_failed"
    assert "worktree parent escapes the source checkout" in (result.error or "")
    assert outside_target.is_dir()
    assert _git_ok(outside_target, "rev-parse", "HEAD") == commit

    # A target that does not yet exist outside must not create directories there.
    fresh_id = "abcdef0123456789abcdef0123456789"
    _push_input(source, commit, assignment_id=fresh_id)
    missing = prepare([_entry(source, remote, commit, digest, assignment_id=fresh_id)], fresh_id)
    assert missing.status == "failed"
    assert missing.error_code == "worktree_failed"
    assert not (outside / "worktrees" / fresh_id).exists()


def test_prepare_mkdir_race_keeps_other_caller_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent prepare winning the path race is never rolled back."""
    from omnigent.host import assignment_workspace

    remote_root, source_root = _make_remote_and_source(tmp_path, "root-repo")
    _write_manifest(source_root, {"version": 1, "context": ["AGENTS.md"]})
    (source_root / "AGENTS.md").write_text("instructions\n")
    commit_root = _commit_all(source_root, "manifest and context")
    blob_root = _git_ok(source_root, "cat-file", "blob", f"{commit_root}:{_MANIFEST_PATH}")
    _push_input(source_root, commit_root, name="root")

    remote_docs, source_docs = _make_remote_and_source(tmp_path, "docs-repo")
    _write_manifest(source_docs, {"version": 1})
    commit_docs = _commit_all(source_docs, "manifest only")
    blob_docs = _git_ok(source_docs, "cat-file", "blob", f"{commit_docs}:{_MANIFEST_PATH}")
    _push_input(source_docs, commit_docs, name="docs")

    real_mkdir = os.mkdir

    def _racing_mkdir(path: str, *args: object, **kwargs: object) -> None:
        if str(path).endswith(os.path.join(_ASSIGNMENT_ID, "docs")):
            real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]
            (Path(path) / "keep.txt").write_text("theirs\n")
            raise FileExistsError(path)
        real_mkdir(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(assignment_workspace.os, "mkdir", _racing_mkdir)

    result = prepare(
        [
            _entry(
                source_root,
                remote_root,
                commit_root,
                manifest_digest(blob_root.encode("utf-8")),
                name="root",
            ),
            _entry(
                source_docs,
                remote_docs,
                commit_docs,
                manifest_digest(blob_docs.encode("utf-8")),
                name="docs",
            ),
        ],
        _ASSIGNMENT_ID,
    )

    assert result.status == "failed"
    assert result.error_code == "worktree_failed"
    assert "already exists" in (result.error or "")
    foreign = source_docs / ".omnigent" / "worktrees" / _ASSIGNMENT_ID / "docs"
    assert (foreign / "keep.txt").read_text() == "theirs\n"
    assert _worktree_count(source_root) == 1
    assert _worktree_count(source_docs) == 1


def test_prepare_manifest_with_crlf_hashes_raw_bytes(tmp_path: Path) -> None:
    """A CRLF manifest verifies against the digest of its raw bytes only."""
    remote, source = _make_remote_and_source(tmp_path)
    _git_ok(source, "config", "core.autocrlf", "false")
    raw = (json.dumps({"version": 1, "context": ["AGENTS.md"]}, indent=2) + "\n").encode("utf-8")
    raw_crlf = raw.replace(b"\n", b"\r\n")
    assert b"\r\n" in raw_crlf
    manifest_file = source / _MANIFEST_PATH
    manifest_file.parent.mkdir(parents=True, exist_ok=True)
    manifest_file.write_bytes(raw_crlf)
    (source / "AGENTS.md").write_text("instructions\n")
    commit = _commit_all(source, "crlf manifest")
    stored = subprocess.run(
        ["git", "cat-file", "blob", f"{commit}:{_MANIFEST_PATH}"],
        cwd=source,
        env={**os.environ, **_GIT_ENV},
        capture_output=True,
        check=True,
    ).stdout
    assert stored == raw_crlf
    _push_input(source, commit)

    result = prepare([_entry(source, remote, commit, manifest_digest(raw_crlf))], _ASSIGNMENT_ID)

    assert result.status == "ok"

    lf_digest = manifest_digest(raw_crlf.replace(b"\r\n", b"\n"))
    assert lf_digest != manifest_digest(raw_crlf)
    refused = prepare([_entry(source, remote, commit, lf_digest)], _ASSIGNMENT_ID)
    assert refused.status == "failed"
    assert refused.error_code == "manifest_digest_mismatch"


def test_prepare_required_check_timeout_rolls_back_created_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A TimeoutExpired in the context check still removes the new worktree."""
    import subprocess as _subprocess

    from omnigent.host import assignment_workspace

    remote, source, commit, digest = _setup_basic(tmp_path)
    fresh_id = "abcdef0123456789abcdef0123456789"
    _push_input(source, commit, assignment_id=fresh_id)

    def _boom(_entries: object, _prepared: object) -> None:
        raise _subprocess.TimeoutExpired("git", 120.0)

    monkeypatch.setattr(assignment_workspace, "_check_required", _boom)

    with pytest.raises(_subprocess.TimeoutExpired):
        prepare([_entry(source, remote, commit, digest, assignment_id=fresh_id)], fresh_id)

    assert _worktree_count(source) == 1
    assert not (source / ".omnigent" / "worktrees" / fresh_id).exists()


def test_prepare_makedirs_failure_rolls_back_first_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A makedirs failure on the second repository removes the first worktree."""
    remote_root, source_root = _make_remote_and_source(tmp_path, "root-repo")
    _write_manifest(source_root, {"version": 1, "context": ["AGENTS.md"]})
    (source_root / "AGENTS.md").write_text("instructions\n")
    commit_root = _commit_all(source_root, "manifest and context")
    blob_root = _git_ok(source_root, "cat-file", "blob", f"{commit_root}:{_MANIFEST_PATH}")
    _push_input(source_root, commit_root, name="root")

    remote_docs, source_docs = _make_remote_and_source(tmp_path, "docs-repo")
    _write_manifest(source_docs, {"version": 1})
    commit_docs = _commit_all(source_docs, "manifest only")
    blob_docs = _git_ok(source_docs, "cat-file", "blob", f"{commit_docs}:{_MANIFEST_PATH}")
    _push_input(source_docs, commit_docs, name="docs")

    real_makedirs = os.makedirs

    def _flaky(path: str, exist_ok: bool = False) -> None:
        if "docs-repo" in str(path):
            raise OSError("disk on fire")
        real_makedirs(path, exist_ok=exist_ok)

    monkeypatch.setattr(os, "makedirs", _flaky)

    with pytest.raises(OSError, match="disk on fire"):
        prepare(
            [
                _entry(
                    source_root,
                    remote_root,
                    commit_root,
                    manifest_digest(blob_root.encode("utf-8")),
                    name="root",
                ),
                _entry(
                    source_docs,
                    remote_docs,
                    commit_docs,
                    manifest_digest(blob_docs.encode("utf-8")),
                    name="docs",
                ),
            ],
            _ASSIGNMENT_ID,
        )

    assert _worktree_count(source_root) == 1
    assert _worktree_count(source_docs) == 1


def test_release_untracked_file_hidden_by_config_still_blocks(tmp_path: Path) -> None:
    """With showUntrackedFiles=no, an untracked file still blocks release."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    prepared = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)
    assert prepared.status == "ok"
    worktree = Path(prepared.directories["root"])
    _git_ok(source, "config", "status.showUntrackedFiles", "no")
    scratch = worktree / "scratch.txt"
    scratch.write_text("untracked\n")

    result = release([_release_entry(source)], _ASSIGNMENT_ID)

    assert result.status == "partial"
    assert result.removed == []
    assert "root" in result.failures
    assert "scratch.txt" in result.failures["root"]
    assert scratch.exists()
    assert worktree.is_dir()


def test_release_ignored_files_do_not_block(tmp_path: Path) -> None:
    """Ignored build outputs are not treated as work worth keeping."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    prepared = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)
    assert prepared.status == "ok"
    worktree = Path(prepared.directories["root"])
    with (source / ".git" / "info" / "exclude").open("a") as handle:
        handle.write("*.log\n")
    (worktree / "build.log").write_text("disposable\n")

    result = release([_release_entry(source)], _ASSIGNMENT_ID)

    assert result.status == "ok"
    assert result.removed == ["root"]
    assert not worktree.exists()


def test_prepare_serialized_per_assignment_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent prepares for one id serialize; other ids do not block."""
    import threading

    from omnigent.host import assignment_workspace

    _OTHER_ID = "abcdef0123456789abcdef0123456789"

    def _setup_named(name: str, assignment_id: str) -> tuple[Path, Path, str, str]:
        remote, source = _make_remote_and_source(tmp_path, name)
        _write_manifest(source, {"version": 1, "context": ["AGENTS.md"]})
        (source / "AGENTS.md").write_text("instructions\n")
        commit = _commit_all(source, "manifest and context")
        blob = _git_ok(source, "cat-file", "blob", f"{commit}:{_MANIFEST_PATH}")
        digest = manifest_digest(blob.encode("utf-8"))
        _push_input(source, commit, assignment_id=assignment_id)
        return remote, source, commit, digest

    remote_x, source_x, commit_x, digest_x = _setup_named("serial-x", _ASSIGNMENT_ID)
    remote_y, source_y, commit_y, digest_y = _setup_named("serial-y", _OTHER_ID)
    entry_x = _entry(source_x, remote_x, commit_x, digest_x, assignment_id=_ASSIGNMENT_ID)
    entry_y = _entry(source_y, remote_y, commit_y, digest_y, assignment_id=_OTHER_ID)

    real_run_git = assignment_workspace._run_git
    entered = threading.Event()
    proceed = threading.Event()
    b_made_git_call = threading.Event()

    def _fake_run_git(*args: object, **kwargs: object) -> object:
        name = threading.current_thread().name
        argv = args[0] if args else kwargs.get("args", [])
        if (
            name == "prep-a"
            and isinstance(argv, list)
            and len(argv) >= 2
            and argv[0] == "worktree"
            and argv[1] == "add"
        ):
            entered.set()
            assert proceed.wait(timeout=30), "gate was never released"
        if name == "prep-b":
            b_made_git_call.set()
        return real_run_git(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(assignment_workspace, "_run_git", _fake_run_git)

    outcomes: dict[str, object] = {}

    def _run_a() -> None:
        try:
            outcomes["a"] = prepare([entry_x], _ASSIGNMENT_ID)
        except BaseException as exc:
            outcomes["a-error"] = exc

    def _run_b() -> None:
        try:
            outcomes["b"] = prepare([entry_x], _ASSIGNMENT_ID)
        except BaseException as exc:
            outcomes["b-error"] = exc

    def _run_y() -> None:
        try:
            outcomes["y"] = prepare([entry_y], _OTHER_ID)
        except BaseException as exc:
            outcomes["y-error"] = exc

    thread_a = threading.Thread(target=_run_a, name="prep-a")
    thread_a.start()
    assert entered.wait(timeout=30), "A never reached worktree add"
    thread_b = threading.Thread(target=_run_b, name="prep-b")
    thread_b.start()
    try:
        thread_b.join(timeout=0.5)
        assert thread_b.is_alive(), "same-id prepare must wait while the first holds the lock"
        assert not b_made_git_call.is_set(), "blocked prepare made a git call"

        thread_y = threading.Thread(target=_run_y, name="prep-y")
        thread_y.start()
        thread_y.join(timeout=30)
        assert not thread_y.is_alive(), "different-id prepare must not block on another id"
        assert "y-error" not in outcomes, f"different-id prepare raised: {outcomes['y-error']!r}"
        assert outcomes["y"].status == "ok"  # type: ignore[union-attr]
        assert thread_b.is_alive(), "same-id prepare must still wait while the first is parked"
        assert not b_made_git_call.is_set(), "blocked prepare made a git call"
    finally:
        proceed.set()
    thread_a.join(timeout=30)
    thread_b.join(timeout=30)
    assert not thread_a.is_alive()
    assert not thread_b.is_alive()
    assert "a-error" not in outcomes, f"A raised: {outcomes.get('a-error')!r}"
    assert "b-error" not in outcomes, f"B raised: {outcomes.get('b-error')!r}"
    assert outcomes["a"].status == "ok"  # type: ignore[union-attr]
    assert outcomes["b"].status == "ok"  # type: ignore[union-attr]
    assert outcomes["b"].directories == outcomes["a"].directories  # type: ignore[union-attr]
    worktree = Path(outcomes["a"].directories["root"])  # type: ignore[union-attr]
    assert worktree.is_dir()
    assert _git_ok(worktree, "rev-parse", "HEAD") == commit_x


# ── entry layout: <entry>/.worktrees/<repo>/<topic> ──────────────────────


def _entry_repo(tmp_path: Path) -> Path:
    """Create a git-repo directory to act as the project entry."""
    entry = (tmp_path / "entry").resolve()
    entry.mkdir()
    _git_ok(entry, "init", "-q", "-b", "main")
    return entry


def test_prepare_under_entry_places_worktree_and_excludes(tmp_path: Path) -> None:
    """Prepare with an entry lands at ``<entry>/.worktrees/<repo>/<id>``."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    entry = _entry_repo(tmp_path)

    result = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID, entry=str(entry))

    assert result.status == "ok", result.error
    expected = entry / ".worktrees" / "myrepo" / _ASSIGNMENT_ID
    assert result.directories["root"] == str(expected)
    assert _git_ok(expected, "rev-parse", "HEAD") == commit
    # Detached, as every assignment execution root is.
    assert _git_ok(expected, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    # The entry's own repository gains the ignore line for .worktrees/.
    assert _exclude_lines(entry).count("/.worktrees/") == 1

    # A second prepare reuses the same worktree and writes no second line.
    again = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID, entry=str(entry))
    assert again.status == "ok", again.error
    assert again.directories == result.directories
    assert _exclude_lines(entry).count("/.worktrees/") == 1

    released = release([_release_entry(source)], _ASSIGNMENT_ID)
    assert released.status == "ok", released.failures
    assert not expected.exists()


def test_prepare_under_entry_symlinked_worktrees_refused(tmp_path: Path) -> None:
    """A ``.worktrees`` symlinked out of the entry is refused; nothing is created."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    entry = (tmp_path / "entry").resolve()
    entry.mkdir()
    elsewhere = (tmp_path / "elsewhere").resolve()
    elsewhere.mkdir()
    (entry / ".worktrees").symlink_to(elsewhere)

    result = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID, entry=str(entry))

    assert result.status == "failed"
    assert result.error_code == "worktree_failed"
    assert "escapes the project entry" in (result.error or "")
    assert str(entry / ".worktrees") in (result.error or "")
    assert list(elsewhere.iterdir()) == []
    assert _worktree_count(source) == 1


def test_prepare_under_entry_qualifies_colliding_topics(tmp_path: Path) -> None:
    """Two sources with the same main-worktree name get repository-qualified topics."""
    remote_a, source_a, commit_a, digest_a = _setup_basic(tmp_path)
    second_root = tmp_path / "second"
    second_root.mkdir()
    remote_b, source_b, commit_b, digest_b = _setup_basic(second_root)
    # Each repository's input ref is published under its own name.
    _push_input(source_a, commit_a, name="a")
    _push_input(source_b, commit_b, name="b")
    entry = _entry_repo(tmp_path)

    result = prepare(
        [
            _entry(source_a, remote_a, commit_a, digest_a, name="a"),
            _entry(source_b, remote_b, commit_b, digest_b, name="b"),
        ],
        _ASSIGNMENT_ID,
        entry=str(entry),
    )

    assert result.status == "ok", result.error
    assert result.directories["a"] == str(entry / ".worktrees" / "myrepo" / f"{_ASSIGNMENT_ID}-a")
    assert result.directories["b"] == str(entry / ".worktrees" / "myrepo" / f"{_ASSIGNMENT_ID}-b")
    assert _git_ok(Path(result.directories["a"]), "rev-parse", "HEAD") == commit_a
    assert _git_ok(Path(result.directories["b"]), "rev-parse", "HEAD") == commit_b

    released = release(
        [_release_entry(source_a, "a"), _release_entry(source_b, "b")], _ASSIGNMENT_ID
    )
    assert released.status == "ok", released.failures
    assert sorted(released.removed) == ["a", "b"]


def test_prepare_entry_failure_keeps_legacy_per_assignment_dir(tmp_path: Path) -> None:
    """A failed prepare rolls back only what it created under the entry."""
    import dataclasses

    remote, source, commit, digest = _setup_basic(tmp_path)
    entry = _entry_repo(tmp_path)
    # An empty legacy directory from another assignment run: the entry
    # layout's rollback must not remove it.
    legacy_dir = source / ".omnigent" / "worktrees" / _ASSIGNMENT_ID
    legacy_dir.mkdir(parents=True)
    bad = dataclasses.replace(
        _entry(source, remote, commit, digest, name="b"), input_commit="b" * 40
    )

    result = prepare(
        [_entry(source, remote, commit, digest, name="a"), bad],
        _ASSIGNMENT_ID,
        entry=str(entry),
    )

    assert result.status == "failed"
    assert not (entry / ".worktrees" / "myrepo" / f"{_ASSIGNMENT_ID}-a").exists()
    assert legacy_dir.is_dir()


def test_release_finds_entry_layout_root_in_the_registry(tmp_path: Path) -> None:
    """Release locates a root created under the entry from the source registry."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    entry = _entry_repo(tmp_path)
    prepared = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID, entry=str(entry))
    root = Path(prepared.directories["root"])
    assert root.is_dir()

    released = release([_release_entry(source)], _ASSIGNMENT_ID)

    assert released.status == "ok", released.failures
    assert released.removed == ["root"]
    assert not root.exists()


def test_release_leaves_branch_worktree_at_assignment_topic(tmp_path: Path) -> None:
    """A branch worktree at the topic path is never selected, even at the legacy root."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    entry = _entry_repo(tmp_path)
    legacy = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)
    assert legacy.status == "ok", legacy.error
    legacy_root = Path(legacy.directories["root"])
    topic_path = entry / ".worktrees" / "myrepo" / _ASSIGNMENT_ID
    topic_path.parent.mkdir(parents=True)
    _git_ok(source, "worktree", "add", "-q", "-b", _ASSIGNMENT_ID, str(topic_path))

    released = release([_release_entry(source)], _ASSIGNMENT_ID)

    assert released.status == "ok", released.failures
    assert not legacy_root.exists()
    assert topic_path.is_dir()
    assert _git_ok(topic_path, "rev-parse", "--abbrev-ref", "HEAD") == _ASSIGNMENT_ID


def test_release_without_registration_counts_removed(tmp_path: Path) -> None:
    """Nothing registered for the assignment counts as already removed."""
    _remote, source, _commit, _digest = _setup_basic(tmp_path)

    released = release([_release_entry(source)], _ASSIGNMENT_ID)

    assert released.status == "ok"
    assert released.removed == ["root"]


def test_release_registered_path_missing_on_disk_counts_removed(tmp_path: Path) -> None:
    """A registered but deleted worktree counts as removed without pruning."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    prepared = prepare([_entry(source, remote, commit, digest)], _ASSIGNMENT_ID)
    root = Path(prepared.directories["root"])
    shutil.rmtree(root)

    released = release([_release_entry(source)], _ASSIGNMENT_ID)

    assert released.status == "ok"
    assert released.removed == ["root"]
    assert released.failures == {}
    # The stale registration is still listed: release never prunes.
    assert "worktree " in _git_ok(source, "worktree", "list", "--porcelain")


# ── configured worktree command ───────────────────────────


_FAKE_WORKTREE_COMMAND = Path(__file__).resolve().parent / "_fake_worktree_command.py"


def _fake_command(record: Path, behave: str = "ok") -> list[str]:
    """Build the fake command's argv for one record file and behave mode."""
    return [
        sys.executable,
        str(_FAKE_WORKTREE_COMMAND),
        f"--fake-record={record}",
        f"--fake-behave={behave}",
    ]


def _recorded_argv(record: Path) -> list[str]:
    """Return the argv recorded by the last fake-command invocation."""
    return json.loads(record.read_text().splitlines()[-1])["argv"]


def test_prepare_with_command_creates_detached_worktree(tmp_path: Path) -> None:
    """The command's path becomes the execution root; release still finds it."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    entry = _entry_repo(tmp_path)
    record = tmp_path / "record.jsonl"

    result = prepare(
        [_entry(source, remote, commit, digest)],
        _ASSIGNMENT_ID,
        entry=str(entry),
        command=_fake_command(record),
    )

    assert result.status == "ok", result.error
    expected = entry / ".worktrees" / "myrepo" / _ASSIGNMENT_ID
    assert result.directories == {"root": str(expected)}
    assert _git_ok(expected, "rev-parse", "HEAD") == commit
    assert _git_ok(expected, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"
    assert f"--detach={commit}" in _recorded_argv(record)

    released = release([_release_entry(source)], _ASSIGNMENT_ID)
    assert released.status == "ok", released.failures
    assert released.removed == ["root"]
    assert not expected.exists()


def test_prepare_with_command_reuses_the_existing_worktree(tmp_path: Path) -> None:
    """A retried prepare reuses the command's EXISTS path instead of adding one."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    entry = _entry_repo(tmp_path)
    record = tmp_path / "record.jsonl"
    command = _fake_command(record)

    first = prepare(
        [_entry(source, remote, commit, digest)], _ASSIGNMENT_ID, entry=str(entry), command=command
    )
    assert first.status == "ok", first.error

    second = prepare(
        [_entry(source, remote, commit, digest)], _ASSIGNMENT_ID, entry=str(entry), command=command
    )

    assert second.status == "ok", second.error
    assert second.directories == first.directories
    assert _worktree_count(source) == 2
    assert len(record.read_text().splitlines()) == 2


@pytest.mark.parametrize(
    ("refusal", "error_code"),
    [("refuse:NOT_A_REPO", "source_invalid"), ("refuse:GIT_REFUSED", "worktree_failed")],
)
def test_prepare_with_command_refusal_maps_the_code(
    tmp_path: Path, refusal: str, error_code: str
) -> None:
    """Refusal codes map to prepare codes and leave no legacy directory behind."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    entry = _entry_repo(tmp_path)
    record = tmp_path / "record.jsonl"

    result = prepare(
        [_entry(source, remote, commit, digest)],
        _ASSIGNMENT_ID,
        entry=str(entry),
        command=_fake_command(record, refusal),
    )

    assert result.status == "failed"
    assert result.error_code == error_code
    assert not (source / ".omnigent" / "worktrees" / _ASSIGNMENT_ID).exists()
    assert not (entry / ".worktrees" / "myrepo" / _ASSIGNMENT_ID).exists()
    assert _worktree_count(source) == 1


def test_prepare_with_command_rolls_back_first_when_second_refuses(tmp_path: Path) -> None:
    """A second repository's command refusal removes the first's worktree."""
    remote_root, source_root = _make_remote_and_source(tmp_path, "root-repo")
    _write_manifest(source_root, {"version": 1})
    commit_root = _commit_all(source_root, "manifest only")
    blob_root = _git_ok(source_root, "cat-file", "blob", f"{commit_root}:{_MANIFEST_PATH}")
    _push_input(source_root, commit_root, name="root")

    remote_docs, source_docs = _make_remote_and_source(tmp_path, "docs-repo")
    _write_manifest(source_docs, {"version": 1})
    commit_docs = _commit_all(source_docs, "manifest only")
    blob_docs = _git_ok(source_docs, "cat-file", "blob", f"{commit_docs}:{_MANIFEST_PATH}")
    _push_input(source_docs, commit_docs, name="docs")

    entry = _entry_repo(tmp_path)
    # A plain directory at the second command's reported path makes it refuse
    # with EXISTS, which is not a reusable worktree.
    (entry / ".worktrees" / "docs-repo" / _ASSIGNMENT_ID).mkdir(parents=True)
    record = tmp_path / "record.jsonl"

    result = prepare(
        [
            _entry(
                source_root,
                remote_root,
                commit_root,
                manifest_digest(blob_root.encode("utf-8")),
                name="root",
            ),
            _entry(
                source_docs,
                remote_docs,
                commit_docs,
                manifest_digest(blob_docs.encode("utf-8")),
                name="docs",
            ),
        ],
        _ASSIGNMENT_ID,
        entry=str(entry),
        command=_fake_command(record),
    )

    assert result.status == "failed"
    assert result.error_code == "worktree_failed"
    assert not (entry / ".worktrees" / "root-repo" / _ASSIGNMENT_ID).exists()
    assert _worktree_count(source_root) == 1
    assert _worktree_count(source_docs) == 1


def test_prepare_with_command_source_path_refused_before_rollback(tmp_path: Path) -> None:
    """A command reporting the source checkout is refused; rollback never removes it."""
    import dataclasses

    remote_root, source_root = _make_remote_and_source(tmp_path, "root-repo")
    _write_manifest(source_root, {"version": 1})
    commit_root = _commit_all(source_root, "manifest only")
    blob_root = _git_ok(source_root, "cat-file", "blob", f"{commit_root}:{_MANIFEST_PATH}")
    _push_input(source_root, commit_root, name="root")

    remote_docs, source_docs = _make_remote_and_source(tmp_path, "docs-repo")
    _write_manifest(source_docs, {"version": 1})
    commit_docs = _commit_all(source_docs, "manifest only")
    blob_docs = _git_ok(source_docs, "cat-file", "blob", f"{commit_docs}:{_MANIFEST_PATH}")
    _push_input(source_docs, commit_docs, name="docs")
    # The second repository fails after the first, so a wrongly accepted
    # source path would be rolled back (and the source deleted).
    broken_docs = dataclasses.replace(
        _entry(
            source_docs,
            remote_docs,
            commit_docs,
            manifest_digest(blob_docs.encode("utf-8")),
            name="docs",
        ),
        remote_url=str(tmp_path / "no-such-remote"),
    )
    record = tmp_path / "record.jsonl"

    result = prepare(
        [
            _entry(
                source_root,
                remote_root,
                commit_root,
                manifest_digest(blob_root.encode("utf-8")),
                name="root",
            ),
            broken_docs,
        ],
        _ASSIGNMENT_ID,
        # The entry contains both sources, so the command's reported
        # source path passes the entry containment check.
        entry=str(tmp_path),
        command=_fake_command(record, "outside"),
    )

    assert result.status == "failed"
    assert result.error_code == "worktree_failed"
    assert "not a new detached worktree" in (result.error or "")
    assert (source_root / "README.md").read_text() == "hi\n"
    assert _git_ok(source_root, "log", "-1", "--format=%s") == "manifest only"
    assert _worktree_count(source_root) == 1


def test_prepare_with_command_exists_source_path_not_reused(tmp_path: Path) -> None:
    """An EXISTS refusal whose detail names the source checkout is never reused."""
    remote, source, commit, digest = _setup_basic(tmp_path)
    # A detached source at the pinned commit satisfies every reuse check, so
    # only the source/main exclusions keep it from being adopted.
    _git_ok(source, "checkout", "-q", "--detach", commit)
    record = tmp_path / "record.jsonl"

    result = prepare(
        [_entry(source, remote, commit, digest)],
        _ASSIGNMENT_ID,
        command=_fake_command(record, "exists-source"),
    )

    assert result.status == "failed"
    assert result.error_code == "worktree_failed"
    assert not (source / ".omnigent" / "worktrees" / _ASSIGNMENT_ID).exists()
    assert _git_ok(source, "rev-parse", "HEAD") == commit
    assert _worktree_count(source) == 1
