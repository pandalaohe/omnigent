"""Unit tests for the leaked-process reaper.

Spawns real, disposable child processes (plain ``sleep`` loops whose
command line or environment carries the attribution markers) so the
matcher and the TERM→KILL escalation are exercised against live pids —
no server, no daemon.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import Mock

import psutil
import pytest
from execnet.gateway_io import popen_bootstrapline

from omnigent.inner import _proc
from omnigent.testing.process_reaper import (
    OWNER_MARKER_NAME,
    find_leaked_omnigent_processes,
    reap_leaked_omnigent_processes,
    sweep_dead_owner_dirs,
    write_owner_marker,
)
from tests._helpers.polling import poll_until

# A child that lives until reaped. The argv embeds marker tokens so the
# matcher can attribute it by command line.
_SLEEP_CHILD = "import time\ntime.sleep(120)\n"

# A child that ignores SIGTERM, forcing the reaper's KILL escalation.
_STUBBORN_CHILD = (
    "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(120)\n"
)


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    """A fake session data dir used as the attribution key."""
    d = tmp_path / "omnigent-pytest-abc123"
    d.mkdir()
    return d


def _spawn(code: str, *argv_markers: str, env: dict[str, str] | None = None) -> subprocess.Popen:
    """Spawn a disposable python child carrying attribution markers.

    :param code: Python source the child runs.
    :param argv_markers: Extra argv tokens (visible in its command line).
    :param env: Optional environment override for the child.
    :returns: The child process handle.
    """
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from omnigent.testing.process_reaper import exit_when_parent_dies; "
            f"exit_when_parent_dies({os.getpid()})\n" + code,
            *argv_markers,
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _kill_quietly(proc: subprocess.Popen) -> None:
    """Best-effort cleanup for a test child."""
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)


@pytest.mark.parametrize("cause", [PermissionError(13, "unreadable"), psutil.AccessDenied(12345)])
def test_permission_wrapped_system_error_does_not_attribute_process(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, cause: Exception
) -> None:
    proc = Mock(spec=psutil.Process, pid=12345)

    def unreadable() -> list[str]:
        raise SystemError("proc_cmdline returned with an exception set") from cause

    proc.cmdline.side_effect = unreadable
    monkeypatch.setattr(psutil, "process_iter", lambda: [proc])
    assert reap_leaked_omnigent_processes(data_dir) == ([], [])
    proc.environ.assert_not_called()
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


def test_unrelated_system_error_remains_visible(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path
) -> None:
    proc = Mock(spec=psutil.Process, pid=12345)
    proc.cmdline.side_effect = SystemError("unexpected process inspection failure")
    monkeypatch.setattr(psutil, "process_iter", lambda: [proc])
    with pytest.raises(SystemError, match="unexpected process inspection failure"):
        reap_leaked_omnigent_processes(data_dir)
    proc.terminate.assert_not_called()
    proc.kill.assert_not_called()


def test_matches_by_cmdline_reference(data_dir: Path) -> None:
    """A process whose argv references the data dir is attributed."""
    child = _spawn(_SLEEP_CHILD, "omnigent-server", f"sqlite:///{data_dir}/chat.db")
    try:
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid in {p.pid for p in found}
    finally:
        _kill_quietly(child)


def test_matches_by_environment(data_dir: Path) -> None:
    """A process whose OMNIGENT_DATA_DIR env equals the data dir is attributed."""
    child = _spawn(
        _SLEEP_CHILD,
        "omnigent-daemon-marker",
        env={"OMNIGENT_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"},
    )
    try:
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid in {p.pid for p in found}
    finally:
        _kill_quietly(child)


def test_ignores_processes_without_omnigent_in_cmdline(data_dir: Path) -> None:
    """Matching the data dir alone is not enough — 'omnigent' must appear in argv.

    Uses ``/bin/sleep`` rather than ``sys.executable``: a repo-venv
    interpreter path can itself contain "omnigent", which would defeat
    the point of this negative case.
    """
    child = subprocess.Popen(
        ["sleep", "120"],
        env={"OMNIGENT_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid not in {p.pid for p in found}
    finally:
        _kill_quietly(child)


@pytest.mark.parametrize("invocation", ["pytest", "execnet"])
def test_ignores_pytest_processes(data_dir: Path, invocation: str) -> None:
    """A concurrent pytest carrying the data dir is never attributed.

    xdist workers and nested pytest runs are exec'd with the parent's
    environment (their conftest re-points ``os.environ`` only after
    exec), so a live pytest can legitimately reference this session's
    data dir without being a leak.
    """
    env = {"OMNIGENT_DATA_DIR": str(data_dir), "PATH": "/usr/bin:/bin"}
    if invocation == "execnet":
        child = subprocess.Popen(
            [sys.executable, "-u", "-c", popen_bootstrapline, "omnigent-suite"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            env=env,
        )
        assert child.stdin is not None
        child.stdin.write(repr(_SLEEP_CHILD) + "\n")
        child.stdin.flush()
        child.stdin.close()
    else:
        child = _spawn(_SLEEP_CHILD, "-m", "pytest", "omnigent-suite", env=env)
    try:
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid not in {p.pid for p in found}
    finally:
        _kill_quietly(child)


def test_pytest_guard_is_token_based_not_substring(data_dir: Path) -> None:
    """An orphan whose data-dir NAME contains 'pytest' is still attributed.

    The real data dirs are named ``omnigent-pytest-<rand>``, and a leaked
    server's ``--database-uri`` embeds that name — a substring guard
    would exempt exactly the orphans this module exists to reap.
    """
    child = _spawn(_SLEEP_CHILD, "omnigent-server", f"sqlite:///{data_dir}/chat.db")
    try:
        assert "pytest" in str(data_dir)  # the fixture mimics the real prefix
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid in {p.pid for p in found}
    finally:
        _kill_quietly(child)


def test_ignores_sibling_dir_with_matching_prefix(data_dir: Path) -> None:
    """A sibling session dir that merely PREFIX-matches is never attributed.

    ``mkdtemp`` names are random-suffixed, so one session's dir can be a
    string prefix of another's (``omnigent-pytest-a`` vs
    ``omnigent-pytest-abcd``). A substring match on the command line would
    attribute — and kill — the sibling session's process.
    """
    sibling = Path(f"{data_dir}bcd")  # e.g. .../omnigent-pytest-abc123bcd
    child = _spawn(_SLEEP_CHILD, "omnigent-server", f"sqlite:///{sibling}/chat.db")
    try:
        found = find_leaked_omnigent_processes(data_dir)
        assert child.pid not in {p.pid for p in found}
    finally:
        _kill_quietly(child)


def test_blank_data_dir_matches_nothing() -> None:
    """A degenerate attribution key must not sweep the whole machine."""
    assert find_leaked_omnigent_processes("") == []
    assert find_leaked_omnigent_processes("/") == []


@pytest.mark.posix_only
@pytest.mark.parametrize("helper", ["sleeper", "mock-server"])
def test_helper_exits_when_pytest_is_killed(tmp_path: Path, helper: str) -> None:
    """Exercise the real helper entry points when no fixture finalizer can run."""
    ready = tmp_path / "ready"
    if helper == "sleeper":
        setup = (
            "from tests.testing.test_process_reaper import _spawn, _SLEEP_CHILD\n"
            "child = _spawn(_SLEEP_CHILD, 'omnigent-leak')\n"
        )
    else:
        setup = (
            "from tests.e2e.conftest import _mock_llm_server_process\n"
            "journey = _mock_llm_server_process(Path(sys.argv[1]).parent)\n"
            "next(journey)\n"
            "child = psutil.Process().children()[0]\n"
        )
    code = (
        "import os, psutil, sys, time\nfrom pathlib import Path\n"
        + setup
        + "Path(sys.argv[1]).write_text(str(child.pid))\ntime.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", code, str(ready)])
    child = None
    try:
        poll_until(lambda: ready.exists(), timeout_s=15, interval_s=0.05)
        child = psutil.Process(int(ready.read_text()))
        assert child.is_running()
        parent.kill()
        parent.wait(timeout=5)
        poll_until(lambda: not _proc.process_alive(child.pid), timeout_s=5, interval_s=0.05)
    finally:
        _kill_quietly(parent)
        if child is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()


@pytest.mark.posix_only
@pytest.mark.parametrize("detached", [False, True], ids=["fork", "fork-setsid"])
def test_forked_helper_watches_the_original_owner(tmp_path: Path, detached: bool) -> None:
    ready = tmp_path / "ready"
    helper_code = (
        "import json, os, sys, time\nfrom pathlib import Path\n"
        "from omnigent.testing.process_reaper import exit_when_parent_dies\n"
        "exit_when_parent_dies(int(sys.argv[1]))\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        + (" os.setsid()\n" if detached else "")
        + " Path(sys.argv[2]).write_text(json.dumps([os.getppid(), os.getpid()]))\n"
        "time.sleep(60)\n"
    )
    owner_code = (
        "import os, subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {helper_code!r}, "
        "str(os.getpid()), sys.argv[1]])\n"
        "time.sleep(60)\n"
    )
    owner = subprocess.Popen([sys.executable, "-c", owner_code, str(ready)])
    tree: list[psutil.Process] = []
    try:
        poll_until(ready.exists, timeout_s=10, interval_s=0.05)
        tree = [psutil.Process(pid) for pid in json.loads(ready.read_text())]
        assert all(proc.is_running() for proc in tree)
        owner.kill()
        owner.wait(timeout=5)
        poll_until(
            lambda: all(not _proc.process_alive(proc.pid) for proc in tree),
            timeout_s=5,
            interval_s=0.05,
        )
    finally:
        _kill_quietly(owner)
        for proc in tree:
            with contextlib.suppress(psutil.NoSuchProcess):
                proc.kill()


def test_reap_terminates_leaked_process(data_dir: Path) -> None:
    """reap TERMs an attributed process and reports its command line."""
    child = _spawn(_SLEEP_CHILD, "omnigent-daemon-marker", f"{data_dir}/chat.db")
    try:
        reaped, survivors = reap_leaked_omnigent_processes(data_dir, timeout=10)
        assert any("omnigent-daemon-marker" in cmd for cmd in reaped)
        assert survivors == []
        child.wait(timeout=10)
        assert not psutil.pid_exists(child.pid) or child.poll() is not None
    finally:
        _kill_quietly(child)


def test_reap_escalates_to_kill(data_dir: Path) -> None:
    """A TERM-ignoring orphan is KILLed after the grace period."""
    child = _spawn(_STUBBORN_CHILD, "omnigent-daemon-marker", f"{data_dir}/chat.db")
    try:
        # Give the child a beat to install its SIGTERM handler.
        time.sleep(1.0)
        reaped, survivors = reap_leaked_omnigent_processes(data_dir, timeout=2)
        assert any("omnigent-daemon-marker" in cmd for cmd in reaped)
        assert survivors == []
        child.wait(timeout=10)
        assert child.poll() is not None
    finally:
        _kill_quietly(child)


def test_reap_with_no_leaks_returns_empty(data_dir: Path) -> None:
    """No attributed processes → no-op, empty report."""
    assert reap_leaked_omnigent_processes(data_dir, timeout=1) == ([], [])


# ── dead-owner sweep ─────────────────────────────────────────


def _dead_owner_marker(directory: Path) -> subprocess.Popen:
    """Mark *directory* owned by a freshly killed child, and return it."""
    owner = _spawn(_SLEEP_CHILD, "omnigent-owner")
    try:
        write_owner_marker(directory, pid=owner.pid)
    except BaseException:
        _kill_quietly(owner)
        raise
    _kill_quietly(owner)
    return owner


def test_sweep_reaps_and_removes_dead_owner_dir(tmp_path: Path) -> None:
    """A dead owner's dir is swept: its leak is reaped and the dir removed."""
    d = tmp_path / "omnigent-pytest-dead"
    d.mkdir()
    owner = _dead_owner_marker(d)
    leak: subprocess.Popen | None = None
    try:
        leak = _spawn(
            _SLEEP_CHILD,
            "omnigent-leak",
            env={"OMNIGENT_DATA_DIR": str(d), "PATH": "/usr/bin:/bin"},
        )
        swept, reaped, survivors = sweep_dead_owner_dirs(tmp_path, "omnigent-pytest-", timeout=5)
        assert swept == [d]
        assert any("omnigent-leak" in cmd for cmd in reaped)
        leak.wait(timeout=10)
        assert not d.exists()
        assert survivors == []
    finally:
        if leak is not None:
            _kill_quietly(leak)
        _kill_quietly(owner)


@pytest.mark.posix_only
def test_sweep_reaps_resolved_spelling_of_symlinked_root(tmp_path: Path) -> None:
    """A symlinked root is swept for leaks carrying the resolved data-dir path.

    conftest resolves its data dir (``/tmp`` → ``/private/tmp`` on macOS), so
    a leak's ``OMNIGENT_DATA_DIR`` names the resolved spelling while the swept
    entry is the unresolved one; both spellings must be reaped.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    d = real / "omnigent-pytest-dead"
    d.mkdir()
    owner = _dead_owner_marker(d)
    leak: subprocess.Popen | None = None
    try:
        leak = _spawn(
            _SLEEP_CHILD,
            "omnigent-leak",
            env={"OMNIGENT_DATA_DIR": str(d.resolve()), "PATH": "/usr/bin:/bin"},
        )
        sweep_dead_owner_dirs(link, "omnigent-pytest-", timeout=5)
        leak.wait(timeout=10)
        assert not d.exists()
    finally:
        if leak is not None:
            _kill_quietly(leak)
        _kill_quietly(owner)


def test_sweep_keeps_dir_owned_by_live_process(tmp_path: Path) -> None:
    """A live owner's dir is kept, and its processes are left running."""
    d = tmp_path / "omnigent-pytest-live"
    d.mkdir()
    write_owner_marker(d)
    leak = _spawn(
        _SLEEP_CHILD,
        "omnigent-leak",
        env={"OMNIGENT_DATA_DIR": str(d), "PATH": "/usr/bin:/bin"},
    )
    try:
        swept, _reaped, _survivors = sweep_dead_owner_dirs(tmp_path, "omnigent-pytest-", timeout=5)
        assert swept == []
        assert leak.poll() is None
        assert d.exists()
    finally:
        _kill_quietly(leak)


def test_sweep_keeps_dir_without_owner_marker(tmp_path: Path) -> None:
    """A dir with no marker is never swept (a concurrent run may be mid-start)."""
    d = tmp_path / "omnigent-pytest-nomarker"
    d.mkdir()
    assert sweep_dead_owner_dirs(tmp_path, "omnigent-pytest-") == ([], [], [])
    assert d.exists()


def test_sweep_treats_reused_pid_as_dead(tmp_path: Path) -> None:
    """A marker whose pid exists with a different create time is a reused pid."""
    d = tmp_path / "omnigent-pytest-reused"
    d.mkdir()
    (d / OWNER_MARKER_NAME).write_text(
        json.dumps({"pid": os.getpid(), "create_time": 0.0}), encoding="utf-8"
    )
    swept, _reaped, _survivors = sweep_dead_owner_dirs(tmp_path, "omnigent-pytest-")
    assert swept == [d]
    assert not d.exists()


def test_sweep_ignores_dead_owner_dir_without_prefix(tmp_path: Path) -> None:
    """A dead owner's dir whose name lacks the prefix is left alone."""
    d = tmp_path / "other-prefix-dead"
    d.mkdir()
    owner = _dead_owner_marker(d)
    try:
        assert sweep_dead_owner_dirs(tmp_path, "omnigent-pytest-") == ([], [], [])
        assert d.exists()
    finally:
        _kill_quietly(owner)
