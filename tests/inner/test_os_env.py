"""Unit tests for :mod:`omnigent.inner.os_env` helper-env construction."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from typing import IO

import psutil
import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import (
    CallerProcessOSEnvironment,
    _child_shell_env,
    _project_root,
    _read_impl,
    _shell_impl,
    build_helper_env,
    create_os_environment,
)
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.runner.identity import (
    OMNIGENT_SESSION_ENV_VALUE,
    OMNIGENT_SESSION_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
)


def _inactive_policy() -> SandboxPolicy:
    """A ``sandbox.type: none`` policy (user opted out of sandboxing).

    :returns: An inactive :class:`SandboxPolicy` whose ``build_helper_env``
        branch mirrors the parent environment.
    """
    return SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def _active_policy() -> SandboxPolicy:
    """An active policy that drives ``build_helper_env``'s allowlist branch.

    ``build_helper_env`` only consults ``active`` and ``env_passthrough``;
    the ``backend_type`` is never activated here, so ``"none"`` is fine.

    :returns: An active :class:`SandboxPolicy`.
    """
    return SandboxPolicy(
        backend_type="none",
        active=True,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def test_build_helper_env_inactive_strips_binding_token() -> None:
    """``sandbox.type: none`` mirrors parent env MINUS the binding token.

    Opting out of sandboxing grants the agent broad
    file/network access, but it must NOT additionally leak the runner's
    control-plane auth secret. Asserts ``PATH`` survives (the opt-out
    still mirrors the parent env) while the token is dropped.

    :returns: None.
    """
    parent = {
        "PATH": "/usr/bin",
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: "bug-binding-token-secret",
    }

    env = build_helper_env(parent, _inactive_policy())

    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in env
    assert "bug-binding-token-secret" not in env.values()
    assert env["PATH"] == "/usr/bin"


def test_build_helper_env_active_drops_binding_token() -> None:
    """The active allowlist branch never admits the binding token.

    The deny-by-default allowlist excludes the token's name, so even if
    it is present in the parent env it does not reach the helper.

    :returns: None.
    """
    parent = {
        "PATH": "/usr/bin",
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: "bug-binding-token-secret",
    }

    env = build_helper_env(parent, _active_policy())

    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR not in env
    assert "bug-binding-token-secret" not in env.values()
    assert env["PATH"] == "/usr/bin"  # PATH is in the default allowlist


def test_build_helper_env_active_passes_omnigent_session_marker() -> None:
    """The ``OMNIGENT`` session marker survives the active allowlist.

    The marker (set once on the runner process) must reach an agent's
    sandboxed shell so code running there can detect it is inside an
    Omnigent session, the way ``CLAUDE_CODE`` / ``CODEX`` are visible in
    their own agents' shells.

    :returns: None.
    """
    parent = {
        "PATH": "/usr/bin",
        OMNIGENT_SESSION_ENV_VAR: OMNIGENT_SESSION_ENV_VALUE,
    }

    env = build_helper_env(parent, _active_policy())

    assert env[OMNIGENT_SESSION_ENV_VAR] == OMNIGENT_SESSION_ENV_VALUE


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
def test_build_helper_env_desktop_session_policy(active: bool, explicit: bool) -> None:
    parent = {
        "PATH": "/usr/bin",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
        "XDG_RUNTIME_DIR": "/run/user/1000",
    }

    policy = _active_policy() if active else _inactive_policy()
    if explicit:
        policy.env_passthrough = ["DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"]
    env = build_helper_env(parent, policy)

    assert env["PATH"] == "/usr/bin"
    if active:
        assert "DBUS_SESSION_BUS_ADDRESS" not in env
        assert "XDG_RUNTIME_DIR" not in env
    else:
        assert env == parent


# ---------------------------------------------------------------------------
# _shell_impl — timeout result shape
# ---------------------------------------------------------------------------


def test_shell_impl_environment_overrides_are_per_command(tmp_path: Path, monkeypatch) -> None:
    shell_path = shutil.which("bash") or shutil.which("sh")
    assert shell_path is not None
    monkeypatch.setenv("PI_SESSION_ID", "unrelated")

    def session_id(value: str | None) -> str:
        result = _shell_impl(
            command="printf '%s' \"${PI_SESSION_ID-unset}\"",
            timeout=10,
            shell_path=shell_path,
            cwd=tmp_path,
            env_overrides={"PI_SESSION_ID": value},
        )
        assert result["exit_code"] == 0
        return result["stdout"]

    assert session_id("conv_one") == "conv_one"
    assert session_id("conv_two") == "conv_two"
    assert session_id(None) == "unset"
    assert os.environ["PI_SESSION_ID"] == "unrelated"


@pytest.mark.parametrize("exit_code", [0, 7])
def test_shell_impl_replaces_undecodable_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    popen = subprocess.Popen

    def ascii_popen(*args, **kwargs):
        return popen(*args, encoding="ascii", **kwargs)

    monkeypatch.setattr(subprocess, "Popen", ascii_popen)
    result = _shell_impl(
        command=(
            "import sys; "
            "sys.stdout.buffer.write(b'out\\xff'); "
            "sys.stderr.buffer.write(b'err\\xff'); "
            f"sys.exit({exit_code})"
        ),
        timeout=10,
        shell_path=sys.executable,
        cwd=tmp_path,
    )

    assert result["stdout"] == "out\ufffd"
    assert result["stderr"] == "err\ufffd"
    assert result["exit_code"] == exit_code
    assert result["timed_out"] is False
    if exit_code:
        assert result["error"] == "Command exited with status 7: err\ufffd"
    else:
        assert "error" not in result


@pytest.mark.parametrize("missing_stream", ["stdout", "stderr", "both"])
@pytest.mark.parametrize("exit_code", [0, 7])
def test_shell_impl_handles_missing_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, missing_stream: str, exit_code: int
) -> None:
    communicate = subprocess.Popen.communicate

    def missing_output(process, *args, **kwargs):
        stdout, stderr = communicate(process, *args, **kwargs)
        return (
            None if missing_stream in ("stdout", "both") else stdout,
            None if missing_stream in ("stderr", "both") else stderr,
        )

    monkeypatch.setattr(subprocess.Popen, "communicate", missing_output)
    result = _shell_impl(
        command=f"import sys; print('out'); print('err', file=sys.stderr); sys.exit({exit_code})",
        timeout=10,
        shell_path=sys.executable,
        cwd=tmp_path,
    )

    assert result["stdout"] == ("out\n" if missing_stream == "stderr" else "")
    assert result["stderr"] == ("err\n" if missing_stream == "stdout" else "")
    assert result["exit_code"] == exit_code
    assert result["timed_out"] is False
    if exit_code:
        detail = {"stdout": ": err", "stderr": ": out", "both": ""}[missing_stream]
        assert result["error"] == f"Command exited with status 7{detail}"
    else:
        assert "error" not in result


def test_shell_impl_timeout_includes_exit_code(tmp_path: Path) -> None:
    """Timed-out shell commands still return the documented result fields."""
    shell_path = shutil.which("bash") or shutil.which("sh")
    assert shell_path is not None

    result = _shell_impl(
        command="sleep 2",
        timeout=1,
        shell_path=shell_path,
        cwd=tmp_path,
    )

    assert result["stdout"] == ""
    assert result["stderr"] == ""
    assert result["exit_code"] is None
    assert result["timed_out"] is True
    assert result["error"] == "Command timed out after 1 seconds"


@pytest.mark.skipif(os.name == "nt", reason="Exercises POSIX shell job and session inheritance")
@pytest.mark.parametrize("layout", ["foreground", "background", "detached", "background-detached"])
def test_shell_timeout_stops_descendants(tmp_path: Path, layout: str) -> None:
    """A helper shell timeout stops both its command and that command's child."""
    pid_file = tmp_path / "pids.json"
    script = tmp_path / "spawn_tree.py"
    script.write_text(
        "import json, os, psutil, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)', "
        "sys.argv[1]], start_new_session=sys.argv[2].endswith('detached'))\n"
        "identities = [[pid, psutil.Process(pid).create_time()] "
        "for pid in (os.getpid(), child.pid)]\n"
        "Path(sys.argv[1]).write_text(json.dumps(identities))\n"
        "time.sleep(60)\n"
    )
    command = shlex.join([sys.executable, str(script), str(pid_file), layout])
    command += " &" if layout.startswith("background") else "; :"
    os_env = create_os_environment(
        OSEnvSpec(cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert os_env is not None
    processes: list[psutil.Process] = []
    try:
        started = time.monotonic()
        result = asyncio.run(os_env.shell(command, timeout=1))
        elapsed = time.monotonic() - started
        assert result.get("timed_out") is True, result
        identities = json.loads(pid_file.read_text())
        assert len({pid for pid, _created in identities}) == 2, identities
        for pid, created in identities:
            with contextlib.suppress(psutil.NoSuchProcess):
                process = psutil.Process(pid)
                if process.create_time() == created:
                    processes.append(process)
        _gone, alive = psutil.wait_procs(processes, timeout=2)
        for process in alive:
            assert str(pid_file) in process.cmdline()
        assert not alive, f"Timed-out command left live descendants: {[p.pid for p in alive]}"
        assert elapsed < 10, f"Timeout cleanup took {elapsed:.1f}s instead of stopping the command"
    finally:
        for process in processes:
            with contextlib.suppress(psutil.NoSuchProcess):
                process.kill()
        psutil.wait_procs(processes, timeout=2)
        os_env.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["cancel", "close"])
async def test_shell_cancellation_stops_owned_tree(tmp_path: Path, action: str) -> None:
    """Cancelling a shell or closing its environment must stop detached descendants."""
    from omnigent.inner.async_utils import run_sync_on_thread

    marker = tmp_path / "pids.json"
    script = tmp_path / "tree.py"
    script.write_text(
        "import json, os, subprocess, sys, time\nfrom pathlib import Path\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "start_new_session=os.name != 'nt')\n"
        "Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]))\n"
        "time.sleep(60)\n"
    )
    argv = [sys.executable, str(script), str(marker)]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    environment = create_os_environment(
        OSEnvSpec(cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert environment is not None
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    task = asyncio.create_task(environment.shell(command, timeout=30))
    close_task = None
    processes: list[psutil.Process] = []
    try:
        async with asyncio.timeout(10):
            while not marker.exists():
                await asyncio.sleep(0.01)
        processes = [psutil.Process(pid) for pid in json.loads(marker.read_text())]
        if action == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            close_task = asyncio.create_task(run_sync_on_thread(environment.close))
            await asyncio.wait_for(asyncio.shield(close_task), timeout=3)
        _, alive = await asyncio.to_thread(psutil.wait_procs, processes, timeout=2)
        assert not alive, f"{action} left live processes: {[p.pid for p in alive]}"
        assert unrelated.poll() is None
        if action == "cancel":
            # Shared environments must remain usable after interrupting one request.
            assert (await environment.read(str(script)))["content"]
    finally:
        for process in processes:
            with contextlib.suppress(psutil.NoSuchProcess):
                process.kill()
        await asyncio.to_thread(psutil.wait_procs, processes, timeout=2)
        unrelated.terminate()
        await asyncio.to_thread(unrelated.wait, timeout=3)
        await run_sync_on_thread(environment.close)
        if close_task is not None:
            await close_task
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_cancelled_queued_shell_does_not_execute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled request waiting for the shared helper lock must have no side effects."""
    from omnigent.inner.async_utils import run_sync_on_thread

    environment = create_os_environment(
        OSEnvSpec(cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert environment is not None
    helper = environment._helper
    entered, done = threading.Event(), threading.Event()
    request = helper.request

    def queued(*args: object, **kwargs: object) -> object:
        entered.set()
        try:
            return request(*args, **kwargs)
        finally:
            done.set()

    monkeypatch.setattr(helper, "request", queued)
    marker = tmp_path / "must-not-run"
    argv = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).touch()"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)
    helper._lock.acquire()
    try:
        task = asyncio.create_task(environment.shell(command))
        assert await run_sync_on_thread(entered.wait, 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        helper._lock.release()
        assert await run_sync_on_thread(done.wait, 3)
        await run_sync_on_thread(environment.close)
    assert not marker.exists()


# ---------------------------------------------------------------------------
# _read_impl — binary file handling
# ---------------------------------------------------------------------------

_BINARY = b"\x89PNG\r\n\x1a\n\x00\x01\x02\xff"


def test_read_impl_binary_descriptor_for_agent(tmp_path: Path) -> None:
    """With no byte cap (agent ``sys_os_read`` path) binary is not inlined.

    The base64 payload would be useless to the model and could saturate the
    context window, so only a descriptor is returned.

    :returns: None.
    """
    f = tmp_path / "logo.png"
    f.write_bytes(_BINARY)

    result = _read_impl(f, offset=1, limit=2_000)

    assert result["encoding"] == "base64"
    assert result["content"] == ""
    assert result["total_bytes"] == len(_BINARY)
    # Not truncated — the payload was deliberately omitted, not cut short.
    assert result["truncated"] is False
    assert "note" in result


def test_read_impl_binary_inlined_within_cap(tmp_path: Path) -> None:
    """A byte cap larger than the file inlines the whole payload, untruncated.

    :returns: None.
    """
    f = tmp_path / "logo.png"
    f.write_bytes(_BINARY)

    result = _read_impl(f, offset=1, limit=2_000, max_bytes=10 * 1024 * 1024)

    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content"]) == _BINARY
    assert result["total_bytes"] == len(_BINARY)
    assert result["truncated"] is False


def test_read_impl_binary_truncated_at_cap(tmp_path: Path) -> None:
    """A byte cap smaller than the file truncates and flags it.

    :returns: None.
    """
    f = tmp_path / "logo.png"
    f.write_bytes(_BINARY)

    result = _read_impl(f, offset=1, limit=2_000, max_bytes=4)

    assert base64.b64decode(result["content"]) == _BINARY[:4]
    assert result["returned_bytes"] == 4
    assert result["total_bytes"] == len(_BINARY)
    assert result["truncated"] is True


def _make_large_binary(path: Path, size: int) -> None:
    """Write a sparse file with a binary prefix and a logical size of *size*.

    The 8 KB binary prefix forces the prefix-sniff to classify it binary; the
    ``truncate`` extends the (sparse) file to *size* without writing the bytes,
    so the test stays cheap while exercising a large logical file.

    :returns: None.
    """
    with path.open("wb") as fh:
        fh.write(b"\xff\xfe\x00\x01" * 2_048)  # 8 KB of non-UTF-8 bytes
        fh.truncate(size)


def test_read_impl_binary_descriptor_does_not_read_whole_file(tmp_path: Path) -> None:
    """The descriptor path is O(1): it stats the size, never reading content.

    Regression guard for inlining the whole file (``path.read_bytes()``) just
    to compute ``total_bytes`` — which would OOM on large workspace blobs.

    :returns: None.
    """
    size = 256 * 1024 * 1024  # 256 MB logical, only ~8 KB on disk
    f = tmp_path / "big.bin"
    _make_large_binary(f, size)

    tracemalloc.start()
    try:
        result = _read_impl(f, offset=1, limit=2_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["total_bytes"] == size
    assert result["content"] == ""
    # A full read would have allocated ~256 MB; bounded reads stay tiny.
    assert peak < 10 * 1024 * 1024


def test_read_impl_binary_cap_reads_only_the_cap(tmp_path: Path) -> None:
    """The byte-capped path reads at most ``max_bytes``, not the file.

    :returns: None.
    """
    size = 256 * 1024 * 1024
    f = tmp_path / "big.bin"
    _make_large_binary(f, size)

    tracemalloc.start()
    try:
        result = _read_impl(f, offset=1, limit=2_000, max_bytes=16)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["returned_bytes"] == 16
    assert result["total_bytes"] == size
    assert result["truncated"] is True
    assert peak < 10 * 1024 * 1024


@pytest.mark.parametrize(
    ("file_size", "byte_cap"),
    [
        pytest.param(11 * 1024**2, 10 * 1024**2, id="11MiB-10MiB-cap"),
        pytest.param(1024**3, 64 * 1024, id="1GiB-64KiB-cap"),
    ],
)
def test_read_impl_text_cap_does_not_read_entire_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, file_size: int, byte_cap: int
) -> None:
    """Oversized files, even with a huge first line, only read a bounded prefix."""
    path = tmp_path / "oversized.txt"
    with path.open("wb") as fh:
        fh.write(b"x" * (byte_cap + 1))
        fh.truncate(file_size)
    assert path.stat().st_size == file_size

    reads: list[int] = []
    original_open = Path.open

    class BoundedReader(io.BufferedReader):
        def read(self, size: int = -1) -> bytes:
            assert 0 <= size <= byte_cap + 1, "attempted an unbounded file read"
            data = super().read(size)
            reads.append(len(data))
            return data

    def guarded_open(file_path, mode="r", *args, **kwargs):
        if file_path != path:
            return original_open(file_path, mode, *args, **kwargs)
        assert mode == "rb", "text reads must enforce the byte cap before decoding"
        return BoundedReader(io.FileIO(path, "r"))

    monkeypatch.setattr(Path, "open", guarded_open)
    tracemalloc.start()
    try:
        result = _read_impl(path, offset=1, limit=None, max_bytes=byte_cap)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert result["encoding"] == "utf-8"
    assert result["content"] == "x" * byte_cap
    assert result["truncated"] is True
    assert result["total_lines"] is None
    assert sum(reads) <= byte_cap + 1 + 8192
    assert sum(reads) < file_size
    assert peak < max(4 * 1024**2, 4 * byte_cap)


@pytest.mark.parametrize(
    ("raw", "byte_cap", "expected", "truncated"),
    [
        (b"", 1, "", False),
        (b"abc", 3, "abc", False),
        (b"abcdef", 3, "abc", True),
        ("abcé".encode(), 4, "abc", True),
        ("abcé".encode(), 5, "abcé", False),
        ("a😀tail".encode(), 4, "a", True),
    ],
)
def test_read_impl_text_byte_cap_preserves_utf8(
    tmp_path: Path, raw: bytes, byte_cap: int, expected: str, truncated: bool
) -> None:
    """The byte cap distinguishes complete files from prefixes ending mid-codepoint."""
    path = tmp_path / "text.txt"
    path.write_bytes(raw)

    result = _read_impl(path, offset=1, limit=None, max_bytes=byte_cap)

    assert result["encoding"] == "utf-8"
    assert result["content"] == expected
    assert result["truncated"] is truncated


def test_read_impl_invalid_utf8_after_sniff_falls_back_to_bounded_binary(tmp_path: Path) -> None:
    """A text prefix followed by invalid UTF-8 retains the byte cap on fallback."""
    path = tmp_path / "late-binary.dat"
    raw = b"a" * 8192 + b"\xff" + b"b" * 128
    byte_cap = 8192 + 64
    path.write_bytes(raw)

    result = _read_impl(path, offset=1, limit=None, max_bytes=byte_cap)

    assert result["encoding"] == "base64"
    assert base64.b64decode(result["content"]) == raw[:byte_cap]
    assert result["returned_bytes"] == byte_cap
    assert result["total_bytes"] == len(raw)
    assert result["truncated"] is True


def test_read_impl_multibyte_char_straddling_sniff_boundary_is_text(tmp_path: Path) -> None:
    """A multi-byte char split across the 8 KB sniff boundary stays text.

    The incremental decoder must treat the truncated trailing sequence as
    *incomplete*, not invalid — otherwise valid UTF-8 would be misread as
    binary purely because of where the prefix happened to be cut.

    :returns: None.
    """
    # 8 KB sniff window cuts the 3-byte '€' (0xE2 0x82 0xAC) at byte 8191.
    text = "a" * 8_190 + "€" + "tail\n"
    f = tmp_path / "wide.txt"
    f.write_text(text, encoding="utf-8")

    result = _read_impl(f, offset=1, limit=2_000)

    assert result["encoding"] == "utf-8"
    assert result["content"] == text


def test_read_impl_nul_byte_file_classified_binary(tmp_path: Path) -> None:
    """A NUL byte marks a file binary even though ``\\x00`` is valid UTF-8.

    UTF-16/NUL-laden files decode cleanly as UTF-8, so without an explicit NUL
    check they'd be misread as text and line-windowed into garbage.

    :returns: None.
    """
    # UTF-16-LE-style ASCII: every byte is valid UTF-8, but the interleaved
    # NULs make this binary.
    f = tmp_path / "utf16.bin"
    f.write_bytes(b"H\x00e\x00l\x00l\x00o\x00")

    result = _read_impl(f, offset=1, limit=2_000)

    assert result["encoding"] == "base64"
    assert result["total_bytes"] == 10


# ---------------------------------------------------------------------------
# _child_shell_env — omnigent's own package root must not leak onto the
# PYTHONPATH of agent shell commands (it would shadow the project's packages).
# ---------------------------------------------------------------------------


def test_child_shell_env_strips_project_root_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """omnigent's project root is removed; a project entry is preserved.

    The helper prepends its project root to ``PYTHONPATH`` so it can import
    omnigent at startup. Commands the agent runs must not inherit that entry,
    or omnigent's ``site-packages`` shadows the project venv's own packages.

    :returns: None.
    """
    project_entry = "/opt/venvs/proj/lib/python3.13/site-packages"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(_project_root()), project_entry]))

    env = _child_shell_env()

    assert env["PYTHONPATH"] == project_entry


def test_child_shell_env_drops_var_when_only_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the sole entry is omnigent's root, ``PYTHONPATH`` is unset.

    Leaving an empty ``PYTHONPATH`` would put the shell command's cwd on
    ``sys.path``; dropping the var entirely avoids that surprise.

    :returns: None.
    """
    monkeypatch.setenv("PYTHONPATH", str(_project_root()))

    env = _child_shell_env()

    assert "PYTHONPATH" not in env


def test_child_shell_env_noop_without_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``PYTHONPATH`` in the parent env means nothing to strip.

    :returns: None.
    """
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv("PATH", "/usr/bin")

    env = _child_shell_env()

    assert "PYTHONPATH" not in env
    assert env["PATH"] == "/usr/bin"


# ---------------------------------------------------------------------------
# End-to-end: the real helper must not leak omnigent's package root into a
# sys_os_shell command's PYTHONPATH. Guards the wiring in _shell_impl, not
# just _child_shell_env in isolation.
# ---------------------------------------------------------------------------


def test_shell_command_does_not_see_omnigent_project_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell command's ``PYTHONPATH`` drops omnigent's root, keeps the rest.

    Spawns a real ``caller_process`` helper (``sandbox: none`` so it runs on
    every platform) with omnigent's root pre-seeded on ``PYTHONPATH`` — the
    same shape the helper spawn produces — and asserts the agent's command
    sees the sibling project entry but not omnigent's, so project subprocesses
    resolve their own packages.

    :returns: None.
    """
    project_entry = "/opt/venvs/proj/site-packages"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(_project_root()), project_entry]))
    if os.name == "nt":
        # ``bash.exe`` may be the WSL launcher, which intentionally does not
        # inherit arbitrary Windows variables unless WSLENV opts them in.
        # Exercise the Windows-native helper boundary through COMSPEC here.
        from omnigent.inner import os_env as os_env_mod

        real_which = os_env_mod.shutil.which
        monkeypatch.setattr(
            os_env_mod.shutil,
            "which",
            lambda name: None if name in {"bash", "sh"} else real_which(name),
        )

    os_env = create_os_environment(
        OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert os_env is not None
    try:
        command = "echo PP=%PYTHONPATH%" if os.name == "nt" else "echo PP=$PYTHONPATH"
        result = asyncio.run(os_env.shell(command))
    finally:
        os_env.close()

    out = result.get("stdout", "")
    assert project_entry in out
    assert str(_project_root()) not in out


# ---------------------------------------------------------------------------
# _HelperProcessClient — Windows config delivery without an active sandbox
# ---------------------------------------------------------------------------


def test_helper_windows_config_delivery_without_active_sandbox(tmp_path: Path) -> None:
    """The Windows config-file branch must start the helper with NO sandbox.

    Native Windows never has an active sandbox, but only an active sandbox
    creates the private scratch tmpdir the ``--config-file`` fallback writes
    into. The branch must create that tmpdir itself instead of asserting a
    precondition the platform can never meet (which left every OS tool call
    returning a bare error payload).
    """
    from unittest import mock

    from omnigent.inner import os_env as os_env_mod
    from omnigent.inner.sandbox import resolve_sandbox

    spec = OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none"))
    sandbox = resolve_sandbox(spec, tmp_path)
    assert not sandbox.active

    client = os_env_mod._HelperProcessClient(
        cwd=tmp_path,
        shell_path=(
            os.environ.get("COMSPEC", "cmd.exe")
            if os.name == "nt"
            else shutil.which("sh") or "/bin/sh"
        ),
        sandbox=sandbox,
    )
    try:
        with mock.patch.object(os_env_mod, "IS_WINDOWS", True):
            result = client.request({"op": "shell", "command": "echo tmpdir-ok", "timeout": 30})
        assert not result.get("error"), result
        assert "tmpdir-ok" in result.get("stdout", "")
        created = client._tmpdir
        assert created is not None and created.exists()
    finally:
        client.close()
    assert client._tmpdir is None
    assert not created.exists()


# Helper restarts: a request that may already have run is never sent twice.
# ---------------------------------------------------------------------------

_HELPER_BACKENDS = [
    "none",
    pytest.param(
        "linux_bwrap",
        marks=pytest.mark.skipif(
            sys.platform != "linux" or shutil.which("bwrap") is None,
            reason="requires Linux and bwrap",
        ),
    ),
]


def _helper_env(workspace: Path, backend: str) -> CallerProcessOSEnvironment:
    os_env = create_os_environment(
        OSEnvSpec(
            type="caller_process",
            cwd=str(workspace),
            sandbox=OSEnvSandboxSpec(
                type=backend,
                read_paths=[str(_project_root() / "omnigent")],
                write_paths=["."],
            ),
        )
    )
    assert isinstance(os_env, CallerProcessOSEnvironment)
    return os_env


class _UnsentRequest:
    """Helper stdin that fails like a dead pipe, so the helper never sees the request."""

    def __init__(self, stdin: IO[str]) -> None:
        self._stdin = stdin

    def write(self, text: str) -> int:
        raise BrokenPipeError("helper pipe is closed")

    def flush(self) -> None:
        self._stdin.flush()

    def close(self) -> None:
        self._stdin.close()


class _UnreadReply:
    """Helper stdout whose helper is killed before its reply is read."""

    def __init__(self, helper: subprocess.Popen[str]) -> None:
        assert helper.stdout is not None
        self._helper = helper
        self._stdout = helper.stdout

    def readline(self) -> str:
        self._helper.kill()
        self._helper.wait(timeout=10)
        return ""

    def close(self) -> None:
        self._stdout.close()


def _fail_next_request(
    os_env: CallerProcessOSEnvironment, *, delivered: bool
) -> subprocess.Popen[str]:
    helper = os_env._helper._proc
    assert helper is not None and helper.stdin is not None
    if delivered:
        helper.stdout = _UnreadReply(helper)  # type: ignore[assignment]
    else:
        helper.stdin = _UnsentRequest(helper.stdin)  # type: ignore[assignment]
    return helper


@pytest.mark.parametrize("backend", _HELPER_BACKENDS)
def test_shell_command_is_not_rerun_when_its_helper_dies_before_replying(
    tmp_path: Path, backend: str
) -> None:
    """A command that ran before its helper died is reported, not run again."""
    workspace = tmp_path.resolve()
    os_env = _helper_env(workspace, backend)
    try:
        result = asyncio.run(os_env.shell("echo once >> marker.txt; kill -9 $PPID"))
        assert "may already have run" in result.get("error", ""), result
        assert (workspace / "marker.txt").read_text() == "once\n"
        after = asyncio.run(os_env.shell("echo next"))
        assert after.get("stdout") == "next\n", after
    finally:
        os_env.close()


@pytest.mark.parametrize("backend", _HELPER_BACKENDS)
def test_read_is_answered_by_a_new_helper_when_the_first_dies_before_replying(
    tmp_path: Path, backend: str
) -> None:
    """A read has no side effects, so it is safe to send again."""
    workspace = tmp_path.resolve()
    (workspace / "notes.txt").write_text("kept\n")
    os_env = _helper_env(workspace, backend)
    try:
        assert asyncio.run(os_env.read("notes.txt")).get("content") == "kept\n"
        dead_helper = _fail_next_request(os_env, delivered=True)
        result = asyncio.run(os_env.read("notes.txt"))
        assert result.get("content") == "kept\n", result
        assert os_env._helper._proc is not dead_helper
    finally:
        os_env.close()


@pytest.mark.parametrize("backend", _HELPER_BACKENDS)
def test_request_that_never_reached_its_helper_runs_once_on_a_new_one(
    tmp_path: Path, backend: str
) -> None:
    """Any op is safe to send again when the helper never received it."""
    workspace = tmp_path.resolve()
    os_env = _helper_env(workspace, backend)
    try:
        assert asyncio.run(os_env.shell("true")).get("exit_code") == 0
        first_helper = _fail_next_request(os_env, delivered=False)
        result = asyncio.run(os_env.shell("echo once >> marker.txt"))
        assert result.get("exit_code") == 0, result
        assert (workspace / "marker.txt").read_text() == "once\n"
        assert os_env._helper._proc is not first_helper
    finally:
        os_env.close()


@pytest.mark.parametrize("backend", _HELPER_BACKENDS)
def test_close_stops_the_helper_and_releases_its_spawner_thread(
    tmp_path: Path, backend: str
) -> None:
    """Closing ends the helper and its spawning thread, and can be repeated."""
    os_env = _helper_env(tmp_path.resolve(), backend)
    assert asyncio.run(os_env.shell("true")).get("exit_code") == 0
    helper = os_env._helper._proc
    assert helper is not None
    spawner_threads = list(os_env._helper._spawner._threads)
    assert spawner_threads
    os_env.close()
    assert helper.wait(timeout=10) is not None
    for thread in spawner_threads:
        thread.join(timeout=10)
        assert not thread.is_alive()
    os_env.close()
    assert asyncio.run(os_env.shell("true")) == {"error": "OS environment helper is closed"}
