"""The host load CLI reads only its local file and always emits schema 1."""

import errno
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from omnigent.cli import cli
from omnigent.host.load_level import utc_iso
from omnigent.util.atomic_json import atomic_write_json


@pytest.mark.parametrize(
    "state",
    [
        "fresh",
        "transient-failure",
        "missing",
        "malformed",
        "warmup",
        "stale",
        "unknown",
        "bad-time",
        "bad-cores",
        "future",
        "bad-lines",
        "bad-level",
    ],
)
def test_host_load_contract(state: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    now = time.time()
    path = tmp_path / "run" / "host-load.json"
    payload = {
        "schema": 1,
        "host_id": "host_a",
        "sampled_at": utc_iso(now),
        "reading_at": utc_iso(now),
        "cpu_count": 24,
        "load1": 96.0,
        "load_equiv": 96.0,
        "estimate": False,
        "load_per_core": 4.0,
        "mem_pct": 50.0,
        "level": "heavy_hold",
        "reason": "load",
        "lines": {"heavy": 4.0, "new_session": 5.0},
        "lines_synced_at": None,
        "path": str(path),
    }
    if state == "warmup":
        payload.update(
            reading_at=None, load1=None, load_equiv=None, load_per_core=None, level="unknown"
        )
    elif state == "transient-failure":
        payload.update(cpu_count=None, load1=None, load_equiv=None, load_per_core=None)
        payload["reading_at"] = utc_iso(now - 60)
    elif state == "stale":
        payload["reading_at"] = utc_iso(now - 200)
    elif state == "unknown":
        payload["level"] = "unknown"
    elif state == "bad-time":
        payload["sampled_at"] = "invalid"
    elif state == "bad-cores":
        payload["cpu_count"] = True
    elif state == "future":
        payload["reading_at"] = utc_iso(now + 60)
    elif state == "bad-lines":
        payload["lines"] = {"heavy": 5, "new_session": 4}
    elif state == "bad-level":
        payload["level"] = []
    if state not in {"missing", "malformed"}:
        atomic_write_json(path, payload)
    elif state == "malformed":
        path.parent.mkdir()
        path.write_text("{")
    result = CliRunner().invoke(cli, ["host", "load", "--json"])
    fresh = state in {"fresh", "transient-failure"}
    assert result.exit_code == (0 if fresh else 3), result.output
    decoded = json.loads(result.output)
    assert decoded.keys() == payload.keys()
    assert decoded["level"] == ("heavy_hold" if fresh else "unknown")
    assert decoded["path"] == str(path)
    if state == "fresh":
        assert decoded == payload


def test_host_load_text_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    result = CliRunner().invoke(cli, ["host", "load"])
    assert result.exit_code == 3
    assert "level: unknown" in result.output


@pytest.mark.parametrize(
    "args", [["host", "load", "--json"], ["host", "load"]], ids=["json", "text"]
)
@pytest.mark.parametrize(
    "exc",
    [
        BrokenPipeError("broken pipe"),
        OSError(errno.EINVAL, "Invalid argument"),
        OSError(errno.EPIPE, "Broken pipe"),
    ],
    ids=["broken-pipe", "einval", "epipe"],
)
def test_host_load_broken_stdout_exits_quietly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: list[str], exc: OSError
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))

    def fail_echo(*_args: object, **_kwargs: object) -> None:
        raise exc

    monkeypatch.setattr(click, "echo", fail_echo)
    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.output == ""


def test_host_load_unrelated_oserror_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))

    def fail_echo(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "input/output error")

    monkeypatch.setattr(click, "echo", fail_echo)
    result = CliRunner().invoke(cli, ["host", "load", "--json"])
    assert isinstance(result.exception, OSError)
    assert result.exception.errno == errno.EIO


def _clean_subprocess_env(data_dir: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in {"OMNIGENT", "OMNIGENT_RUNNER_ID", "OMNIGENT_SESSION_ID"}
    }
    env["OMNIGENT_DATA_DIR"] = str(data_dir)
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    return env


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX closed-pipe SIGPIPE/EPIPE probe")
def test_host_load_closed_pipe_exits_quietly_through_shutdown(tmp_path: Path) -> None:
    """Real closed stdout pipe exits 1 with empty stderr and no crash report."""
    read_fd, write_fd = os.pipe()
    os.close(read_fd)
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                "import sys; from omnigent.cli import main; "
                "sys.argv = ['omnigent', 'host', 'load', '--json']; main()",
            ],
            stdin=subprocess.DEVNULL,
            stdout=write_fd,
            stderr=subprocess.PIPE,
            text=True,
            env=_clean_subprocess_env(tmp_path),
            timeout=30,
        )
    finally:
        os.close(write_fd)

    assert proc.returncode == 1
    assert proc.stderr == ""
    assert not list((tmp_path / "crashes").glob("crash-*.md"))


def test_host_load_windows_nul_eof_einval_exits_quietly_through_shutdown(
    tmp_path: Path,
) -> None:
    """Real main() + click.echo flush raising EINVAL under Windows NUL/EOF exits quietly."""
    marker = tmp_path / "browser-opened"
    script = textwrap.dedent(
        f"""
        import ctypes, errno, io, sys, types, webbrowser
        from pathlib import Path
        from omnigent.cli import main

        class _FailingRaw(io.RawIOBase):
            def writable(self) -> bool:
                return True

            def write(self, _b: object) -> int:
                raise OSError(errno.EINVAL, "Invalid argument")

        class _FakeNulStdin(io.StringIO):
            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 0

        class _FakeNulStderr:
            def __init__(self, wrapped: object) -> None:
                self._wrapped = wrapped

            def isatty(self) -> bool:
                return True

            def fileno(self) -> int:
                return 2

            def __getattr__(self, name: str) -> object:
                return getattr(self._wrapped, name)

        class _FakeGetConsoleMode:
            argtypes: list[object] = []
            restype: object = None

            def __call__(self, _handle: ctypes.c_void_p, _mode_ptr: object) -> int:
                return 0

        sys.platform = "win32"
        sys.modules["msvcrt"] = types.SimpleNamespace(get_osfhandle=lambda fd: 100 + fd)
        ctypes.WinDLL = lambda *_a, **_k: types.SimpleNamespace(
            GetConsoleMode=_FakeGetConsoleMode()
        )
        webbrowser.open = lambda *_a, **_k: Path({str(marker)!r}).write_text("opened") or True
        sys.stdin = _FakeNulStdin("")
        sys.stderr = _FakeNulStderr(sys.stderr)
        sys.stdout = io.TextIOWrapper(io.BufferedWriter(_FailingRaw()), encoding="utf-8")
        sys.argv = ["omnigent", "host", "load", "--json"]
        main()
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=_clean_subprocess_env(tmp_path),
        timeout=30,
    )

    assert proc.returncode == 1
    assert proc.stderr == ""
    assert not marker.exists()
    assert not list((tmp_path / "crashes").glob("crash-*.md"))
