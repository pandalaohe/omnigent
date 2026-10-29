"""Wire parsing for program status; real launch coverage lives with the runner."""

from __future__ import annotations

import base64
import contextlib
import itertools
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from omnigent.inner import _proc
from omnigent.terminals import program_status
from omnigent.terminals.program_status import ProgramStatus, ProgramStatusParser, parse_report
from tests._helpers.polling import poll_until


@pytest.mark.parametrize("ending", [b"\x07", b"\x1b\\"])
def test_fragmented_reports_queries_and_resets(ending: bytes) -> None:
    events = []
    parser = ProgramStatusParser(
        on_report=events.append,
        on_query=lambda: events.append("query"),
        on_reset=lambda full: events.append(("reset", full)),
    )
    stream = (
        b"screen\x1b]7501;?"
        + ending
        + b"\x1b]7501;state=blocked:app=claude-code:kind=question:msg=QW5zd2VyPw"
        + ending
        + b"\x1b]7501;state=working:app=claude-code"
        + ending
        + b"\x1b]133;A;redraw=0"
        + ending
        + b"\x1bc"
    )
    output = b"".join(parser.feed(bytes([byte])) for byte in stream)
    assert output == stream.replace(b"\x1b]7501;?" + ending, b"")
    assert events == [
        "query",
        ProgramStatus("blocked", app="claude-code", kind="question", message="Answer?"),
        ProgramStatus("working", app="claude-code"),
        ("reset", False),
        ("reset", True),
    ]


@pytest.mark.parametrize(
    "body",
    [
        b"app=claude-code",
        b"state=future",
        b"state=working:id=",
        b"state=working:id=a//b",
        b"state=working:id=bad!",
        b"state=working:id=" + b"a/" * 8 + b"a",
        b"state=working:app=" + b"a" * 33,
        b"state=working:" + b"k" * 17 + b"=value",
        b"state=working:msg=a",
        b"state=working:msg=a:msg=QQ",
        b"state=working:app=" + b"a" * 33 + b":app=claude-code",
        b"state=working:title=/w==",
        b"state=working:msg=" + base64.b64encode(b"line\nbreak"),
        b"state=working:msg=" + base64.b64encode("\u0085".encode()),
        b"state=working:title=" + base64.b64encode(b"a" * 193),
        b"state=working:msg=" + base64.b64encode(b"a" * 2049),
    ],
)
def test_invalid_reports_are_discarded_whole(body: bytes) -> None:
    assert parse_report(body) is None


def test_unknown_keys_and_malformed_pairs_do_not_discard_valid_state() -> None:
    assert parse_report(
        b" broken : state=idle : future=x : state=working : msg=invalid! : kind=auth"
    ) == ProgramStatus("working")


def test_other_control_strings_and_oversized_reports_cannot_change_status() -> None:
    events = []
    parser = ProgramStatusParser(
        on_report=events.append, on_query=lambda: None, on_reset=lambda _: None
    )
    parser.feed(b"\x1bPignored\x1b]7501;state=error\x07\x1b\\")
    parser.feed(b"\x1b]0;title\x1b]7501;state=error\x07")
    parser.feed(b"\x1b]7501;state=error:unknown=" + b"x" * 4096 + b"\x1b\\")
    parser.feed(b"\x1b]7501;state=error\x18")
    parser.feed(b"\x1b]7501;state=idle\x07")
    assert events == [ProgramStatus("idle")]


@pytest.mark.parametrize("chunk_size", [1, 7, 65536])
def test_output_is_preserved_except_capability_query(chunk_size: int) -> None:
    stream = (
        b"text\x1b[?1049h\x1b[31mred\x1b[0m\n"
        b"\x1b]7501;?\x07\x1b[c"
        b"\x1bPnested\x1b]7501;?\x07\x1b\\"
        b"\x1b]7501;state=working:msg=" + b"a" * 5000 + b"\x1b\\"
        b"\x1b]7501;cancelled\x18\x1b]0;title\x07\x1b\x1bXignored\x1b\\"
        b"\x1b]unfinished"
    )
    queries = []
    parser = ProgramStatusParser(
        on_report=lambda _: None, on_query=lambda: queries.append(True), on_reset=lambda _: None
    )
    output = (
        b"".join(
            parser.feed(stream[i : i + chunk_size]) for i in range(0, len(stream), chunk_size)
        )
        + parser.finish()
    )
    assert output == stream.replace(b"\x1b]7501;?\x07\x1b[c", b"\x1b[c")
    assert queries == [True]


def _mock_relay(monkeypatch: pytest.MonkeyPatch, events: list[object]) -> None:
    """Keep relay on its real control path without forking or signaling a process."""
    import fcntl
    import pty
    import select
    import signal
    import termios
    import tty

    clock = itertools.count(step=0.5)
    monkeypatch.setattr(pty, "fork", lambda: (32123, 91))
    monkeypatch.setattr(fcntl, "ioctl", lambda *_: b"\0" * 8)
    monkeypatch.setattr(fcntl, "fcntl", lambda *_: 0)
    monkeypatch.setattr(program_status.os, "set_blocking", lambda *_: None)
    monkeypatch.setattr(termios, "tcgetattr", lambda _: ["original"])
    monkeypatch.setattr(termios, "tcsetattr", lambda *_: events.append("termios"))
    monkeypatch.setattr(tty, "setraw", lambda _: None)
    monkeypatch.setattr(signal, "signal", lambda *_: None)
    monkeypatch.setattr(select, "select", lambda *_: ([91], [], []))
    monkeypatch.setattr(program_status.os, "read", lambda *_: b"")
    monkeypatch.setattr(program_status.os, "close", lambda fd: events.append(("close", fd)))
    monkeypatch.setattr(program_status.os, "killpg", lambda *_: events.append("killpg"))
    monkeypatch.setattr(program_status.os, "kill", lambda *_: events.append("kill"))
    monkeypatch.setattr(program_status.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(program_status.time, "sleep", lambda _: None)


def test_relay_closes_master_before_waiting_for_child(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[object] = []
    _mock_relay(monkeypatch, events)
    state_file = tmp_path / "status.json"

    def waitpid(pid: int, options: int) -> tuple[int, int]:
        assert (pid, options) == (32123, os.WNOHANG)
        events.append("wait")
        return (pid, 7 << 8) if ("close", 91) in events else (0, 0)

    monkeypatch.setattr(program_status.os, "waitpid", waitpid)
    assert program_status.relay(state_file, "claude-code", ["unused"]) == 7
    assert events.index(("close", 91)) < events.index("wait")
    assert "killpg" not in events
    assert "kill" not in events
    assert events[-1] == "termios"
    assert not state_file.exists()


def test_relay_unreapable_child_has_bounded_failure_and_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    events: list[object] = []
    _mock_relay(monkeypatch, events)
    state_file = tmp_path / "status.json"

    def waitpid(pid: int, options: int) -> tuple[int, int]:
        assert (pid, options) == (32123, os.WNOHANG)
        events.append("wait")
        return 0, 0

    monkeypatch.setattr(program_status.os, "waitpid", waitpid)
    with pytest.raises(RuntimeError, match=r"relay \d+: child 32123 did not reap"):
        program_status.relay(state_file, "claude-code", ["unused"])
    assert events.index(("close", 91)) < events.index("killpg")
    assert events.index("killpg") < events.index("kill") < events.index("termios")
    assert events.count("wait") < 20
    assert not state_file.exists()


def test_relay_preserves_normal_child_exit_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import select

    events: list[object] = []
    _mock_relay(monkeypatch, events)
    state_file = tmp_path / "status.json"
    polls = iter([([], [], []), ([91], [], [])])
    monkeypatch.setattr(select, "select", lambda *_: next(polls))

    def waitpid(pid: int, options: int) -> tuple[int, int]:
        assert (pid, options) == (32123, os.WNOHANG)
        return pid, 23 << 8

    monkeypatch.setattr(program_status.os, "waitpid", waitpid)
    assert program_status.relay(state_file, "claude-code", ["unused"]) == 23
    assert events == [("close", 91), "termios"]
    assert not state_file.exists()


def test_relay_reaps_exiting_child_after_outer_output_disconnect(tmp_path: Path) -> None:
    """A real controlling PTY exit completes when relay releases its master."""
    state_file = tmp_path / "status.json"
    go_file = tmp_path / "go"
    wrote_file = tmp_path / "wrote"
    wrapper = """import errno, os, sys, threading, time
from pathlib import Path
from omnigent.terminals import program_status

real_waitpid = os.waitpid
real_write = os.write
def guarded_signal(*args):
    raise AssertionError('unexpected real signal attempt')
def observed_waitpid(pid, options):
    child, status = real_waitpid(pid, options)
    if child:
        print(f'reaped:{os.waitstatus_to_exitcode(status)}', flush=True)
    return child, status
def disconnected_write(fd, data):
    if fd == 1 and data:
        watchdog = threading.Timer(8, lambda: os._exit(90))
        watchdog.daemon = True
        watchdog.start()
        Path(sys.argv[2]).touch()
        deadline = time.monotonic() + 5
        while not Path(sys.argv[3]).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not Path(sys.argv[3]).exists():
            raise AssertionError('child did not write to controlling tty')
        time.sleep(0.5)
        raise OSError(errno.EIO, 'outer output disconnected')
    return real_write(fd, data)
os.kill = os.killpg = guarded_signal
os.waitpid = observed_waitpid
os.write = disconnected_write
try:
    program_status.relay(Path(sys.argv[1]), 'claude-code', sys.argv[4:])
except OSError as exc:
    if exc.errno != errno.EIO:
        raise
    print('outer-output-eio', flush=True)
except AssertionError as exc:
    print(str(exc), flush=True)
    raise SystemExit(42)
else:
    raise AssertionError('expected outer output disconnect')
"""
    child = """import os, sys, time
from pathlib import Path
os.write(1, b'r')
deadline = time.monotonic() + 5
while not Path(sys.argv[1]).exists() and time.monotonic() < deadline:
    time.sleep(0.01)
if not Path(sys.argv[1]).exists():
    os._exit(91)
tty_fd = os.open('/dev/tty', os.O_WRONLY)
os.write(tty_fd, b'x' * 63)
Path(sys.argv[2]).touch()
os._exit(23)
"""
    master, slave = os.openpty()
    try:
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                wrapper,
                str(state_file),
                str(go_file),
                str(wrote_file),
                sys.executable,
                "-c",
                child,
                str(go_file),
                str(wrote_file),
            ],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process:
            os.close(slave)
            slave = -1
            output, _ = process.communicate(timeout=12)
        assert process.returncode == 0, output
        assert b"reaped:23\n" in output
        assert b"outer-output-eio\n" in output
        assert b"Traceback" not in output
        assert b"unexpected real signal attempt" not in output
        assert not state_file.exists()
    finally:
        os.close(master)
        if slave != -1:
            os.close(slave)


@pytest.mark.parametrize("failure", ["missing", "not-executable", "stdin-eof", "stdin-eio"])
def test_relay_process_failure_cleanup(tmp_path: Path, failure: str) -> None:
    """Exercise exec errors and input loss with a real child and PTY."""
    state_file = tmp_path / "status.json"
    ready = tmp_path / "ready"
    command = [str(tmp_path / "command")]
    wrapper = "from omnigent.terminals.program_status import main; main()"
    if failure == "not-executable":
        Path(command[0]).write_text("#!/bin/sh\nexit 0\n")
    if failure.startswith("stdin-"):
        command = [
            sys.executable,
            "-c",
            "import os,signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGHUP, lambda *_: "
            "(time.sleep(0.05), print('graceful-exit-tail', flush=True), exit(23))); "
            # Publish readiness only after the PID file is fully written and closed.
            f"ready = Path({str(ready)!r}); pending = ready.with_suffix('.tmp'); "
            "pending.write_text(str(os.getpid())); pending.replace(ready); "
            "time.sleep(30)",
        ]
        # Inject only the outer input failure; fork, inner PTY and child are real.
        wrapper = (
            "import errno,os\n"
            "from omnigent.terminals.program_status import main\n"
            "read = os.read\n"
            "def disconnected_read(fd, size):\n"
            " if fd == 0:\n"
            + (
                "  raise OSError(errno.EIO, 'terminal disconnected')\n"
                if failure == "stdin-eio"
                else "  return b''\n"
            )
            + " return read(fd, size)\n"
            "os.read = disconnected_read\n"
            "main()\n"
        )
    master, slave = os.openpty()
    try:
        with subprocess.Popen(
            [
                sys.executable,
                "-c",
                wrapper,
                "--state-file",
                str(state_file),
                "--app",
                "claude-code",
                "--",
                *command,
            ],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        ) as process:
            if failure.startswith("stdin-"):
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    assert process.poll() is None
                    time.sleep(0.01)
                assert ready.exists(), "child never became ready"
                os.write(master, b"x")
            output, _ = process.communicate(timeout=10)
            assert b"Traceback" not in output, output
            if failure.startswith("stdin-"):
                assert process.returncode == 23, output
                assert b"graceful-exit-tail" in output
                with pytest.raises(ProcessLookupError):
                    os.kill(int(ready.read_text()), 0)
            else:
                assert process.returncode == (127 if failure == "missing" else 126), output
                assert b"cannot exec" in output
            assert not state_file.exists()
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.posix_only
@pytest.mark.parametrize("failure", ["owner", "parent", "pty"])
def test_relay_exits_despite_output_backpressure(tmp_path: Path, failure: str) -> None:
    """Dead runners/panes must release a relay even when nobody drains output."""
    state_file = tmp_path / "status.json"
    wrapper_pid_file = tmp_path / "wrapper"
    owner = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    command = [
        sys.executable,
        str(Path(program_status.__file__)),
        "--state-file",
        str(state_file),
        "--app",
        "claude-code",
    ]
    if failure != "pty":
        command.extend(["--owner-pid", str(owner.pid)])
    command.extend(
        [
            "--",
            sys.executable,
            "-c",
            "import os,signal; "
            "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
            "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "exec(\"while True: os.write(1, b'x' * 65536)\")",
        ]
    )
    parent_code = (
        "import json, subprocess, sys, time; from pathlib import Path; "
        "child = subprocess.Popen(json.loads(sys.argv[1]), start_new_session=True); "
        "Path(sys.argv[2]).write_text(str(child.pid)); time.sleep(60)"
    )
    master, slave = os.openpty()
    wrapper = child = None
    parent = None
    try:
        parent = subprocess.Popen(
            [sys.executable, "-c", parent_code, json.dumps(command), str(wrapper_pid_file)],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        os.close(slave)
        slave = -1
        poll_until(lambda: state_file.exists(), timeout_s=10, interval_s=0.05)
        wrapper = psutil.Process(int(wrapper_pid_file.read_text()))
        child = psutil.Process(json.loads(state_file.read_text())["pid"])
        # Leave stdout unread until both its pipe and the outer PTY are full.
        time.sleep(0.3)
        if failure == "owner":
            owner.kill()
            owner.wait(timeout=5)
        elif failure == "parent":
            parent.kill()
            parent.wait(timeout=5)
        else:
            os.close(master)
            master = -1
        poll_until(lambda: not state_file.exists(), timeout_s=7, interval_s=0.05)
        poll_until(lambda: not _proc.process_alive(wrapper.pid), timeout_s=3, interval_s=0.05)
        assert not _proc.process_alive(child.pid)
        if failure != "parent":
            assert parent.poll() is None
        if failure != "owner":
            assert owner.poll() is None
    finally:
        for proc in (wrapper, child):
            if proc is not None:
                with contextlib.suppress(psutil.NoSuchProcess):
                    proc.kill()
        for proc in (parent, owner):
            if proc is not None:
                if proc.poll() is None:
                    proc.kill()
                proc.communicate(timeout=5)
        if master != -1:
            os.close(master)
        if slave != -1:
            os.close(slave)


@pytest.mark.posix_only
@pytest.mark.parametrize("reader_delay", [0, 3.5], ids=["immediate", "delayed"])
def test_relay_drains_output_larger_than_its_buffer(tmp_path: Path, reader_delay: float) -> None:
    master, slave = os.openpty()
    try:
        with subprocess.Popen(
            [
                sys.executable,
                str(Path(program_status.__file__)),
                "--state-file",
                str(tmp_path / "status.json"),
                "--app",
                "claude-code",
                "--",
                sys.executable,
                "-c",
                "import os; os.write(1, b'x' * 80000)",
            ],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            time.sleep(reader_delay)
            output, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
        assert output == b"x" * 80000
    finally:
        os.close(master)
        os.close(slave)
