"""Private home and session-owned skill staging safety."""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.inner import codex_staging
from omnigent.inner.codex_staging import (
    CODEX_HOME_PREFIX,
    CODEX_SKILLS_PREFIX,
    _staging_root_path,
    codex_home_staging_root,
    link_codex_skills_dir,
    prepare_codex_skills_dir,
    reap_orphaned_codex_homes,
    remove_codex_home,
    write_codex_home_owner,
)


@pytest.fixture
def isolated_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX temp root")
def test_staging_root_is_private_and_under_tempdir(isolated_tempdir: Path) -> None:
    root = codex_home_staging_root()
    assert root.parent == isolated_tempdir
    assert root.is_dir()
    if hasattr(os, "getuid"):
        assert f"-{os.getuid()}" in root.name
        assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_staging_root_tightens_a_loose_preexisting_mode(isolated_tempdir: Path) -> None:
    root = codex_home_staging_root()
    root.chmod(0o770)
    assert stat.S_IMODE(codex_home_staging_root().stat().st_mode) == 0o700


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX directory symlinks")
def test_staging_root_resolves_symlinked_temp_ancestors(
    isolated_tempdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_temp = isolated_tempdir / "real-temp"
    real_temp.mkdir()
    temp_alias = isolated_tempdir / "temp-alias"
    temp_alias.symlink_to(real_temp, target_is_directory=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_alias))
    assert codex_home_staging_root().parent == real_temp.resolve()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership semantics")
def test_staging_root_refuses_a_symlink_squatting_its_name(isolated_tempdir: Path) -> None:
    outside = isolated_tempdir / "outside"
    outside.mkdir()
    _staging_root_path().symlink_to(outside)
    with pytest.raises(OSError):
        codex_home_staging_root()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership semantics")
def test_staging_root_refuses_a_root_owned_by_another_user(
    isolated_tempdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign_uid = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: foreign_uid)
    with pytest.raises(OSError):
        codex_home_staging_root()


def test_skills_refresh_preserves_mount_root_and_removes_old_contents(tmp_path: Path) -> None:
    root = tmp_path / f"{CODEX_SKILLS_PREFIX}session"
    root.mkdir(mode=0o700)
    skill = root / "old-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("old content")
    identity = root.stat().st_ino

    assert prepare_codex_skills_dir(root) == root.resolve()
    assert root.stat().st_ino == identity
    assert list(root.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX directory symlinks")
def test_skills_refresh_does_not_follow_child_symlinks(tmp_path: Path) -> None:
    root = tmp_path / f"{CODEX_SKILLS_PREFIX}session"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep")
    (root / "link").symlink_to(outside, target_is_directory=True)

    prepare_codex_skills_dir(root)

    assert marker.read_text() == "keep"
    assert list(root.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX directory symlinks")
def test_skills_refresh_rejects_symlink_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    marker = outside / "keep.txt"
    marker.write_text("keep")
    root = tmp_path / f"{CODEX_SKILLS_PREFIX}session"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        prepare_codex_skills_dir(root)
    assert marker.read_text() == "keep"


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX permission semantics")
@pytest.mark.parametrize("mode", [0o750, 0o770, 0o707])
def test_skills_refresh_rejects_nonprivate_root(tmp_path: Path, mode: int) -> None:
    root = tmp_path / f"{CODEX_SKILLS_PREFIX}session"
    root.mkdir(mode=mode)
    root.chmod(mode)
    with pytest.raises(OSError):
        prepare_codex_skills_dir(root)


def test_skills_refresh_rejects_unrelated_directory(tmp_path: Path) -> None:
    marker = tmp_path / "keep.txt"
    marker.write_text("keep")
    with pytest.raises(OSError):
        prepare_codex_skills_dir(tmp_path)
    assert marker.read_text() == "keep"


def _refuse_symlink(*_args: object, **_kwargs: object) -> None:
    # What Windows raises without Developer Mode or the symlink privilege.
    raise OSError(1314, "A required privilege is not held by the client")


def _skills_link_paths(root: Path) -> tuple[Path, Path]:
    skills_dir = root / f"{CODEX_SKILLS_PREFIX}session"
    skills_dir.mkdir(mode=0o700)
    home = root / "home"
    home.mkdir()
    return home / "skills", skills_dir


def test_skills_link_falls_back_to_a_junction_when_symlinks_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_skills, skills_dir = _skills_link_paths(tmp_path)
    junctions: list[tuple[str, str]] = []
    monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)
    monkeypatch.setattr(codex_staging, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(
        sys.modules,
        "_winapi",
        SimpleNamespace(CreateJunction=lambda target, link: junctions.append((target, link))),
    )

    link_codex_skills_dir(home_skills, skills_dir)

    assert junctions == [(str(skills_dir), str(home_skills))]


def test_skills_link_raises_off_windows_when_symlinks_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_skills, skills_dir = _skills_link_paths(tmp_path)
    monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)
    monkeypatch.setattr(codex_staging, "sys", SimpleNamespace(platform="linux"))

    with pytest.raises(OSError, match="privilege"):
        link_codex_skills_dir(home_skills, skills_dir)
    assert not home_skills.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
def test_skills_link_junction_resolves_into_the_granted_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_skills, skills_dir = _skills_link_paths(tmp_path)
    (skills_dir / "SKILL.md").write_text("body")
    monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)

    link_codex_skills_dir(home_skills, skills_dir)

    assert home_skills.is_junction()
    assert (home_skills / "SKILL.md").resolve() == (skills_dir / "SKILL.md").resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
@pytest.mark.parametrize("relative_path", ["linked", "nested/linked"])
def test_skills_refresh_does_not_follow_junctions(tmp_path: Path, relative_path: str) -> None:
    """Refreshing the grant removes junctions without deleting their outside targets."""
    import _winapi

    _, skills_dir = _skills_link_paths(tmp_path)
    identity = skills_dir.stat().st_ino
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep")
    junction = skills_dir / relative_path
    junction.parent.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(outside), str(junction))

    prepare_codex_skills_dir(skills_dir)

    assert marker.read_text() == "keep"
    assert skills_dir.stat().st_ino == identity
    assert list(skills_dir.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
def test_skills_refresh_rejects_junction_root(tmp_path: Path) -> None:
    """A junction cannot stand in for the private directory owned by this session."""
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep")
    root = tmp_path / f"{CODEX_SKILLS_PREFIX}session"
    _winapi.CreateJunction(str(outside), str(root))

    with pytest.raises(OSError):
        prepare_codex_skills_dir(root)

    assert marker.read_text() == "keep"


def _make_home(root: Path, name: str = "home") -> Path:
    home = root / f"{CODEX_HOME_PREFIX}{name}"
    home.mkdir(parents=True)
    return home


def _marker(home: Path) -> Path:
    return home.with_name(home.name + ".owner")


def _set_mtime(path: Path, *, days: int) -> None:
    when = time.time() - days * 24 * 60 * 60
    os.utime(path, (when, when))


def _age_tree(path: Path, *, days: int) -> None:
    for current, dirnames, filenames in os.walk(path):
        for name in [*dirnames, *filenames]:
            _set_mtime(Path(current) / name, days=days)
    _set_mtime(path, days=days)


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX symlink semantics")
def test_owner_marker_never_writes_through_a_planted_name(tmp_path: Path) -> None:
    home = _make_home(tmp_path, "planted")
    target = tmp_path / "target.txt"
    target.write_text("keep")
    marker = _marker(home)
    marker.symlink_to(target)

    with pytest.raises(FileExistsError):
        write_codex_home_owner(home)

    assert target.read_text() == "keep"
    assert marker.is_symlink()

    fresh = _make_home(tmp_path, "fresh")
    write_codex_home_owner(fresh)
    written = _marker(fresh)
    assert written.read_text().split()[0] == str(os.getpid())
    assert stat.S_IMODE(written.stat().st_mode) == 0o600


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="SIGKILL is POSIX-only")
def test_hard_killed_owner_home_survives_and_is_reaped(tmp_path: Path) -> None:
    worktree = Path(__file__).resolve().parents[2]
    script = (
        "import tempfile\n"
        "from pathlib import Path\n"
        "from omnigent.inner.codex_staging import (\n"
        "    CODEX_HOME_PREFIX, codex_home_staging_root, write_codex_home_owner,\n"
        ")\n"
        "root = codex_home_staging_root()\n"
        "home = Path(tempfile.mkdtemp(prefix=CODEX_HOME_PREFIX, dir=root))\n"
        "write_codex_home_owner(home)\n"
        "(home / 'config.toml').write_text('x')\n"
        "print(home, flush=True)\n"
        "import time\n"
        "time.sleep(600)\n"
    )
    (tmp_path / "tmp").mkdir()
    (tmp_path / "data").mkdir()
    env = {
        **os.environ,
        "TMPDIR": str(tmp_path / "tmp"),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        cwd=worktree,
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        home = Path(proc.stdout.readline().strip())
        assert home.name.startswith(CODEX_HOME_PREFIX)
        assert home.is_dir()
        assert _marker(home).exists()
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)
        # Nothing in the killed process removed the home: the leak.
        assert home.is_dir()
        assert reap_orphaned_codex_homes(home.parent) == 1
        assert not home.exists()
        assert not _marker(home).exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)


def test_live_owner_home_is_never_reaped(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    home = _make_home(root, "live")
    write_codex_home_owner(home)
    (home / "config.toml").write_text("x")
    _age_tree(home, days=30)

    assert reap_orphaned_codex_homes(root) == 0
    assert home.is_dir()


def test_pid_reuse_marker_reads_as_dead(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    home = _make_home(root, "reused")
    _marker(home).write_text(f"{os.getpid()} 12345.0")

    assert reap_orphaned_codex_homes(root) == 1
    assert not home.exists()


def test_unmarked_fresh_home_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    home = _make_home(root, "fresh")

    assert reap_orphaned_codex_homes(root) == 0
    assert home.is_dir()


def test_unmarked_home_idle_past_retention_is_removed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    home = _make_home(root, "old")
    (home / "config.toml").write_text("x")
    _age_tree(home, days=8)

    assert reap_orphaned_codex_homes(root) == 1
    assert not home.exists()


def test_unmarked_home_with_recent_file_is_kept(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    home = _make_home(root, "mixed")
    (home / "config.toml").write_text("x")
    _age_tree(home, days=8)
    recent = home / "recent.txt"
    recent.write_text("x")
    _set_mtime(recent, days=6)
    _set_mtime(home, days=8)

    assert reap_orphaned_codex_homes(root) == 0
    assert home.is_dir()


def test_garbage_marker_behaves_like_unmarked(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    fresh = _make_home(root, "fresh-garbage")
    _marker(fresh).write_text("nonsense")
    old = _make_home(root, "old-garbage")
    (old / "config.toml").write_text("x")
    _marker(old).write_text("nonsense")
    _age_tree(old, days=8)

    assert reap_orphaned_codex_homes(root) == 1
    assert fresh.is_dir()
    assert not old.exists()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX directory symlinks")
def test_reap_does_not_follow_links_out_of_the_home(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    home = _make_home(root, "linked")
    _marker(home).write_text(f"{os.getpid()} 12345.0")
    (home / "skills").symlink_to(outside, target_is_directory=True)
    (home / "plugins").mkdir()
    (home / "plugins" / "cache").symlink_to(outside, target_is_directory=True)

    assert reap_orphaned_codex_homes(root) == 1
    assert not home.exists()
    assert (outside / "keep.txt").read_text() == "keep"


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX directory symlinks")
def test_reap_leaves_entries_it_does_not_own(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    other = root / "other-dir"
    other.mkdir()
    (other / "keep.txt").write_text("keep")
    file_entry = root / f"{CODEX_HOME_PREFIX}file"
    file_entry.write_text("keep")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    link = root / f"{CODEX_HOME_PREFIX}link"
    link.symlink_to(outside, target_is_directory=True)

    assert reap_orphaned_codex_homes(root) == 0
    assert other.is_dir()
    assert file_entry.read_text() == "keep"
    assert link.is_symlink()
    assert (outside / "keep.txt").read_text() == "keep"


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX directory symlinks")
def test_reap_refuses_a_symlinked_root(tmp_path: Path) -> None:
    real_root = tmp_path / "real-root"
    real_root.mkdir()
    home = _make_home(real_root, "dead")
    _marker(home).write_text(f"{os.getpid()} 12345.0")
    root_alias = tmp_path / "root-alias"
    root_alias.symlink_to(real_root, target_is_directory=True)

    assert reap_orphaned_codex_homes(root_alias) == 0
    assert home.is_dir()


def test_reap_missing_root_returns_zero(tmp_path: Path) -> None:
    assert reap_orphaned_codex_homes(tmp_path / "missing") == 0


def test_remove_codex_home_keeps_marker_when_a_child_survives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    home = _make_home(root, "stuck")
    write_codex_home_owner(home)
    marker = _marker(home)
    subdir = home / "sub"
    subdir.mkdir()
    (subdir / "file.txt").write_text("x")

    monkeypatch.setattr(codex_staging.shutil, "rmtree", lambda *args, **kwargs: None)
    assert remove_codex_home(home) is False
    assert marker.exists()

    monkeypatch.undo()
    assert remove_codex_home(home) is True
    assert not home.exists()
    assert not marker.exists()


def test_invalid_owner_markers_read_as_unknown(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    invalid_markers = [
        f"{os.getpid()} nan",
        f"{os.getpid()} inf",
        "-1 123.0",
        "0 123.0",
        f"{os.getpid()} -5.0",
    ]
    for index, content in enumerate(invalid_markers):
        _marker(_make_home(root, f"invalid-{index}")).write_text(content)
    dead = _make_home(root, "dead")
    _marker(dead).write_text(f"{os.getpid()} 12345.0")

    assert reap_orphaned_codex_homes(root) == 1
    for index in range(len(invalid_markers)):
        assert (root / f"{CODEX_HOME_PREFIX}invalid-{index}").is_dir()
    assert not dead.exists()


def test_stale_marker_without_home_is_removed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    stale = root / f"{CODEX_HOME_PREFIX}gone"
    _marker(stale).write_text(f"{os.getpid()} 12345.0")
    live = _make_home(root, "live")
    write_codex_home_owner(live)

    assert reap_orphaned_codex_homes(root) == 0
    assert not _marker(stale).exists()
    assert _marker(live).exists()


def test_staging_root_uses_data_dir_without_getuid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))

    assert _staging_root_path() == (tmp_path / "data").resolve() / "codex-homes"
