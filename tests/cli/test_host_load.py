"""The host load CLI reads only its local file and always emits schema 1."""

import ctypes
import errno
import io
import json
import sys
import threading
import time
import types
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from omnigent import cli_diagnostics
from omnigent import crash_handler as ch
from omnigent.cli import cli, main
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


def test_host_load_main_windows_nul_eof_einval_exits_quietly_without_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows DEVNULL stdin/stderr (isatty True, non-console, EOF) + EINVAL exits quietly."""

    class _FakeNulStream(io.StringIO):
        def __init__(self, fd: int) -> None:
            super().__init__("")
            self._fd = fd

        def isatty(self) -> bool:
            return True

        def fileno(self) -> int:
            return self._fd

    class _FakeGetConsoleMode:
        argtypes: list[object] = []
        restype: object = None

        def __call__(self, _handle: ctypes.c_void_p, _mode_ptr: object) -> int:
            return 0

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_NO_UPDATE_CHECK", "1")
    for var in ("OMNIGENT", "OMNIGENT_RUNNER_ID", "OMNIGENT_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "argv", ["omnigent", "host", "load", "--json"])
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    monkeypatch.setattr(threading, "excepthook", threading.excepthook)
    fake_stdin = _FakeNulStream(0)
    fake_stderr = _FakeNulStream(2)
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "stderr", fake_stderr)
    monkeypatch.setitem(
        sys.modules, "msvcrt", types.SimpleNamespace(get_osfhandle=lambda fd: 100 + fd)
    )
    fake_kernel32 = types.SimpleNamespace(GetConsoleMode=_FakeGetConsoleMode())
    monkeypatch.setattr(ctypes, "WinDLL", lambda *_args, **_kwargs: fake_kernel32, raising=False)
    monkeypatch.setattr(ch, "_enable_faulthandler", lambda: None)
    monkeypatch.setattr(cli_diagnostics, "setup_cli_logging", lambda _argv: None)

    called: list[str] = []
    monkeypatch.setattr(ch, "handle_crash", lambda *_a, **_k: called.append("handle_crash"))
    monkeypatch.setattr(ch, "_prompt_yes_no", lambda *_a: called.append("prompt") or True)
    monkeypatch.setattr(ch, "_open_browser", lambda _url: called.append("browser") or True)

    def fail_echo(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EINVAL, "Invalid argument")

    monkeypatch.setattr(click, "echo", fail_echo)

    with pytest.raises(SystemExit) as exc_info:
        main()

    assert exc_info.value.code == 1
    assert called == []
    assert fake_stderr.getvalue() == ""
    assert not list((tmp_path / "crashes").glob("crash-*.md"))
