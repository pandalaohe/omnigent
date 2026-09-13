"""Close-in-flight observability for TerminalRegistry.close.

Registry absence does not prove a pane finished closing: ``close`` pops the
instance out of ``_by_conversation`` under the lock BEFORE awaiting
``instance.close()`` (upstream ordering, must not change), and restores it
when the close times out or raises. Between the pop and the settle, the
terminal is invisible to ``list_for_conversation`` while its pane is still
alive. These tests pin the observation point: while ``close`` is awaiting a
slow inner close, the in-flight accessor reports True and the listing
reports nothing; afterwards the accessor reports False, including on the
timeout-restore path (no stale mark).
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from omnigent.inner.terminal import TerminalInstance
from omnigent.terminals import registry as registry_mod
from omnigent.terminals.registry import TerminalRegistry


async def _wait_until_empty(reg: TerminalRegistry, conv: str) -> None:
    """Wait until the conversation lists no terminals (close popped it)."""
    for _ in range(500):
        if reg.list_for_conversation(conv) == []:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("close did not pop the instance in time")


async def test_close_marks_in_flight_while_awaiting(tmp_path: Path) -> None:
    """Slow inner close: accessor True + listing empty mid-flight, False after."""
    reg = TerminalRegistry()
    started = asyncio.Event()
    release = asyncio.Event()

    instance = TerminalInstance(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "codex.sock",
        private_dir=tmp_path / "codex",
        running=True,
    )

    async def _slow_close() -> None:
        started.set()
        await release.wait()

    instance.close = _slow_close  # type: ignore[method-assign]
    reg._by_conversation["conv_close_flight"] = {("codex", "main"): instance}
    reg._instance_locks[("conv_close_flight", "codex", "main")] = threading.Lock()

    task = asyncio.create_task(reg.close("conv_close_flight", "codex", "main"))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        await _wait_until_empty(reg, "conv_close_flight")

        assert reg.is_close_in_flight("conv_close_flight", "codex", "main") is True
        assert reg.list_for_conversation("conv_close_flight") == []

        release.set()
        assert await asyncio.wait_for(task, timeout=5) is True
    finally:
        release.set()
        if not task.done():
            task.cancel()
    assert reg.is_close_in_flight("conv_close_flight", "codex", "main") is False


async def test_close_timeout_restores_instance_without_stale_mark(tmp_path: Path) -> None:
    """Timed-out inner close restores the instance AND clears the mark."""
    reg = TerminalRegistry()
    instance = TerminalInstance(
        name="codex",
        session_key="main",
        socket_path=tmp_path / "codex.sock",
        private_dir=tmp_path / "codex",
        running=True,
    )

    async def _hang_forever() -> None:
        await asyncio.sleep(999)

    instance.close = _hang_forever  # type: ignore[method-assign]
    reg._by_conversation["conv_close_timeout"] = {("codex", "main"): instance}
    lock = threading.Lock()
    reg._instance_locks[("conv_close_timeout", "codex", "main")] = lock

    original = registry_mod._CLOSE_TIMEOUT_S
    registry_mod._CLOSE_TIMEOUT_S = 0.01
    try:
        assert await reg.close("conv_close_timeout", "codex", "main") is False
    finally:
        registry_mod._CLOSE_TIMEOUT_S = original

    assert reg.get("conv_close_timeout", "codex", "main") is instance
    assert reg.is_close_in_flight("conv_close_timeout", "codex", "main") is False
