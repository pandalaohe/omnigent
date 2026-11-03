"""Windows shell terminals: raw-byte replay, process exit and bounded subscribers."""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import re
import shutil
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from omnigent.inner.async_utils import run_sync_cleanup, run_sync_on_thread
from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.inner.os_env import OSEnvironment
from omnigent.inner.terminal import TerminalInstance, TerminalResult, terminal_pane_environment
from omnigent.native.native_coding_agents import native_coding_agent_for_terminal_name
from omnigent.terminals.conpty_process import PseudoConsoleProcess, registered_user_environment

_RING_BYTES = 4 * 1024 * 1024
_SUBSCRIBER_BYTES = 8 * 1024 * 1024
_MODE_SEQUENCE = re.compile(rb"\x1b\[\?([0-9;]+)([hl])")
_MODES = (1, 25, 1004, 1049, 2004, 1003, 1006)
_KEYS = {
    "Enter": b"\r",
    "Tab": b"\t",
    "BSpace": b"\x7f",
    "Escape": b"\x1b",
    "Up": b"\x1b[A",
    "Down": b"\x1b[B",
    "Right": b"\x1b[C",
    "Left": b"\x1b[D",
    "Home": b"\x1b[H",
    "End": b"\x1b[F",
    "PPage": b"\x1b[5~",
    "NPage": b"\x1b[6~",
    "F1": b"\x1bOP",
    "F2": b"\x1bOQ",
    "F3": b"\x1bOR",
    "F4": b"\x1bOS",
    **{
        f"F{index}": f"\x1b[{code}~".encode()
        for index, code in enumerate((15, 17, 18, 19, 20, 21, 23, 24), 5)
    },
    **{f"C-{chr(96 + index)}": bytes([index]) for index in range(1, 27)},
}


def validate_windows_terminal(
    name: str, spec: TerminalEnvSpec, effective: OSEnvSpec, parent: OSEnvironment | None
) -> None:
    """Reject contracts the Windows shell backend cannot enforce, before allocation."""
    sandbox = effective.sandbox
    inherited = getattr(parent, "sandbox", None) if spec.os_env in (None, "inherit") else None
    if (
        spec.tmux_start_on_attach
        or spec.keep_alive_after_exit
        or native_coding_agent_for_terminal_name(name) is not None
        or effective.type != "caller_process"
        or (
            sandbox is not None
            and (sandbox.type != "none" or sandbox.requires_network_enforcement)
        )
        or (inherited is not None and inherited.active)
    ):
        raise RuntimeError(
            "Native terminal harnesses (tmux/PTY) are not supported on Windows; "
            "only unsandboxed shell terminals are available"
        )


def windows_terminal_environment(
    registered: Mapping[str, str],
    runner: Mapping[str, str],
    overrides: Mapping[str, str],
    unset: list[str],
    launch: Mapping[str, str],
) -> dict[str, str]:
    """Reconstruct user env, then apply exact case-insensitive overrides and exclusions."""
    env = {name.upper(): value for name, value in registered.items()}
    for name, value in runner.items():
        env.setdefault(name.upper(), value)
    env.update({name.upper(): value for name, value in overrides.items()})
    env = terminal_pane_environment(
        inherit_env=False,
        env=env,
        env_unset=[name.upper() for name in unset],
        launch_environment=launch,
    )
    env.update(TERM="xterm-256color", COLORTERM="truecolor")
    return env


@dataclass
class _OutputState:
    chunks: deque[bytes] = field(default_factory=deque)
    size: int = 0
    limit: int = _RING_BYTES
    modes: dict[int, bool] = field(default_factory=lambda: {mode: mode == 25 for mode in _MODES})
    carry: bytes = b""

    def append(self, chunk: bytes) -> None:
        pending = self.carry + chunk
        for match in _MODE_SEQUENCE.finditer(pending):
            for raw in match[1].split(b";"):
                if not raw or len(raw) > 5:
                    continue
                mode = int(raw)
                if mode in self.modes:
                    self.modes[mode] = match[2] == b"h"
        last_escape = pending.rfind(b"\x1b")
        tail = pending[last_escape:] if last_escape >= 0 else b""
        self.carry = (
            tail if len(tail) <= 128 and re.fullmatch(rb"\x1b(?:\[\??[0-9;]*)?", tail) else b""
        )
        self.chunks.append(chunk)
        self.size += len(chunk)
        overflow = self.size > self.limit
        while self.size > self.limit and self.chunks:
            self.size -= len(self.chunks.popleft())
        if overflow and self.chunks:
            retained = b"".join(self.chunks).partition(b"\r\n")[2]
            self.chunks = deque([retained]) if retained else deque()
            self.size = len(retained)

    def snapshot(self) -> bytes:
        return b"".join(self.chunks)

    def prelude(self) -> bytes:
        return b"".join(
            f"\x1b[?{mode}{'h' if enabled else 'l'}".encode()
            for mode, enabled in self.modes.items()
        )


class _OutputQueue(asyncio.Queue[bytes | None]):
    def __init__(self, lock: threading.RLock) -> None:
        super().__init__()
        self.loop = asyncio.get_running_loop()
        self.lock = lock
        self.pending_bytes = 0
        self.active = True
        self.overflow = asyncio.Event()

    def offer(self, chunk: bytes | None) -> None:
        # Reservations include callbacks which have not yet reached the event loop.
        with self.lock:
            if not self.active or self.loop.is_closed():
                return
            if chunk is not None:
                if self.pending_bytes + len(chunk) > _SUBSCRIBER_BYTES:
                    self.loop.call_soon_threadsafe(self.overflow.set)
                    self.active = False
                    return
                self.pending_bytes += len(chunk)
            with contextlib.suppress(RuntimeError):
                self.loop.call_soon_threadsafe(self._deliver, chunk)

    def _deliver(self, chunk: bytes | None) -> None:
        if self.active:
            super().put_nowait(chunk)

    def get_nowait(self) -> bytes | None:
        chunk = super().get_nowait()
        if chunk is not None:
            with self.lock:
                self.pending_bytes -= len(chunk)
        return chunk


@dataclass(frozen=True)
class AttachSeed:
    output: bytes
    queue: _OutputQueue


@dataclass
class ConPtyTerminalInstance(TerminalInstance):
    backend: ClassVar[str] = "conpty"
    cols: int = 80
    rows: int = 24
    _process: PseudoConsoleProcess | None = field(default=None, init=False, repr=False)
    _output: _OutputState = field(default_factory=_OutputState, init=False, repr=False)
    _output_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)
    _subscribers: list[_OutputQueue] = field(default_factory=list, init=False, repr=False)
    _reader_done: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _exit_done: threading.Event = field(default_factory=threading.Event, init=False, repr=False)
    _reader: threading.Thread | None = field(default=None, init=False, repr=False)
    _on_activity: Callable[[], None] | None = field(default=None, init=False, repr=False)
    _on_exit: Callable[[], None] | None = field(default=None, init=False, repr=False)
    _cleanup_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _cleaned: bool = field(default=False, init=False, repr=False)

    @property
    def windows_build(self) -> int:
        return int(platform.version().rsplit(".", 1)[-1]) if sys.platform == "win32" else 0

    async def launch(self, *, cwd: Path | None = None) -> None:
        if self.running:
            return
        if (self.sandbox_policy is not None and self.sandbox_policy.active) or self.egress_rules:
            raise RuntimeError("sandboxed Windows terminals are not supported")
        await run_sync_cleanup(self._spawn, cwd or self.private_dir, component="conpty_spawn")

    def _spawn(self, cwd: Path) -> None:
        registered = registered_user_environment() if self.inherit_env else {}
        runner = os.environ if self.inherit_env else {}
        launch = self.lifecycle_trace.launch_environment(self.diagnostic_id)
        env = windows_terminal_environment(registered, runner, self.env, self.env_unset, launch)
        self._process = PseudoConsoleProcess.spawn(
            [self.command, *self.args], str(cwd), env, self.cols, self.rows
        )
        self.running = True
        self.launch_cwd = str(cwd)
        self._reader = threading.Thread(
            target=self._read_output, name="conpty-output", daemon=True
        )
        self._reader.start()
        threading.Thread(target=self._wait_exit, name="conpty-exit", daemon=True).start()

    def _read_output(self) -> None:
        assert self._process is not None
        try:
            while chunk := self._process.read_chunk():
                with self._output_lock:
                    self._output.append(chunk)
                    self._screen_changed_at = time.monotonic()
                    for queue in self._subscribers:
                        queue.offer(chunk)
                    callback = self._on_activity
                if callback is not None:
                    self._fire_watch_callback(callback, "activity")
        except OSError:
            pass  # The process waiter, not pipe EOF, owns liveness.
        finally:
            self._reader_done.set()

    def _wait_exit(self) -> None:
        assert self._process is not None
        try:
            self._last_exit_status = self._process.wait()
            self.running = False
            self._reader_done.wait(0.05)
            self._process.close(output_drained=self._reader_done)
            if not self._reader_done.wait(5):
                raise TimeoutError("terminal output reader did not finish")
        except OSError as exc:
            from omnigent.inner.terminal import logger

            logger.warning("ConPTY exit cleanup failed (%s)", type(exc).__name__)
        finally:
            with self._output_lock:
                self.running = False
                self._exit_done.set()
                for queue in self._subscribers:
                    queue.offer(None)
                callback, self._on_exit = self._on_exit, None
            if callback is not None:
                self._fire_watch_callback(callback, "exit")

    def attach_snapshot(self) -> AttachSeed:
        with self._output_lock:
            queue = _OutputQueue(self._output_lock)
            self._subscribers.append(queue)
            data = self._output.prelude()
            if not self._output.modes[1049]:
                data += self._output.snapshot()
            if self._exit_done.is_set():
                queue.offer(None)
            return AttachSeed(data, queue)

    def unsubscribe(self, queue: _OutputQueue) -> None:
        with self._output_lock:
            queue.active = False
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def request_repaint(self) -> None:
        self.resize(self.cols, self.rows)

    def resize(self, cols: int, rows: int) -> None:
        if self._process is not None:
            self._process.resize(cols, rows)
            self.cols, self.rows = cols, rows

    def write_input(self, data: bytes) -> None:
        if self._process is None or not self.running:
            raise OSError("terminal is not running")
        self.note_input()
        self._process.write(data)

    async def send(self, text: str | None = None, *, keys: str = "Enter") -> TerminalResult:
        if not self.running:
            return {"error": "Terminal is not running"}
        payload = bytearray((text or "").encode("utf-8"))
        for key in keys.split():
            encoded = _KEYS.get(key)
            if key.startswith("M-") and len(key) == 3:
                encoded = b"\x1b" + key[2:].encode("utf-8")
            if encoded is None:
                return {"error": f"Unknown terminal key: {key}"}
            payload.extend(encoded)
        try:
            if payload:
                await run_sync_on_thread(self.write_input, bytes(payload))
        except OSError as exc:
            return {"error": str(exc)}
        return {"status": "sent"}

    async def read(self, scrollback: int = 0, *, join_wrapped: bool = False) -> TerminalResult:
        import pyte

        if not self.running:
            return {"error": "Terminal is not running"}
        del join_wrapped  # Native sign-in's joined capture is not a Windows caller.
        with self._output_lock:
            data, cols, rows = self._output.snapshot(), self.cols, self.rows
        screen = pyte.HistoryScreen(cols, rows, history=max(0, scrollback))
        pyte.ByteStream(screen).feed(data)
        history = [
            "".join(row[col].data for col in range(cols)).rstrip() for row in screen.history.top
        ]
        text = "\n".join([*history, *screen.display])
        self._remember_pane_snapshot(text)
        return {
            "terminal": f"{self.name}:{self.session_key}",
            "screen": text,
            "scrollback_lines": scrollback,
        }

    async def is_alive(self) -> bool:
        if not self.running or self._process is None:
            return False
        return await run_sync_on_thread(self._process.exit_code) is None

    async def probe_alive(self) -> bool | None:
        return await self.is_alive()

    async def server_pid(self) -> int | None:
        return None

    def pane_pid_sync(self) -> int | None:
        return self._process.pid if self._process is not None and self.running else None

    async def kill_server(self) -> None:
        await self.close()

    async def set_conversation_link(self, conversation_link: str | None) -> None:
        self.conversation_link = conversation_link

    def start_idle_watcher_thread(
        self,
        on_idle: Callable[[], None] | None = None,
        *,
        on_activity: Callable[[], None] | None = None,
        on_exit: Callable[[], None] | None = None,
        on_tick: Callable[[], None] | None = None,
        idle_threshold_s: float | None = None,
        poll_interval_s: float | None = None,
        pane_probe_interval_s: Callable[[], float | None] | None = None,
        replace: bool = False,
    ) -> None:
        del on_idle, on_tick, idle_threshold_s, poll_interval_s, pane_probe_interval_s
        with self._output_lock:
            if not replace and (self._on_activity is not None or self._on_exit is not None):
                return
            self._on_activity = on_activity
            self._on_exit = on_exit
            exited = self._exit_done.is_set()
            if exited:
                self._on_exit = None
        if exited and on_exit is not None:
            self._fire_watch_callback(on_exit, "exit")

    async def close(self) -> None:
        self.agent_access = False
        self._elevated_watch_stop.set()
        with self._output_lock:
            self._on_activity = self._on_exit = None
        await run_sync_cleanup(self._close, component="conpty_close")

    def _close(self) -> None:
        with self._cleanup_lock:
            if self._cleaned:
                return
            self._close_owned()
            self._cleaned = True

    def _close_owned(self) -> None:
        if self._process is not None:
            self._process.close(output_drained=self._reader_done)
            if self._reader is not None and not self._reader_done.wait(5):
                raise TimeoutError("terminal reader cleanup pending")
        self.running = False
        if self.os_env is not None:
            self.os_env.close()
        if self.private_dir.exists():
            shutil.rmtree(self.private_dir)
        observer, self.on_elevated_exit = self.on_elevated_exit, None
        if self.elevated and observer is not None:
            observer()
