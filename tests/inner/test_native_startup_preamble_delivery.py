"""First-message delivery of the launch-staged instructions preamble.

Pi / cursor / kiro / goose prepend the staged text to the first message they
inject and clear it only after that injection lands, so a failed first turn
retries with the instructions instead of losing them.
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from omnigent.inner import cursor_native_executor as cne
from omnigent.inner import goose_native_executor as gne
from omnigent.inner import kiro_native_executor as kne
from omnigent.inner import pi_native_executor as pne
from omnigent.inner.cursor_native_executor import CursorNativeExecutor
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.inner.goose_native_executor import GooseNativeExecutor
from omnigent.inner.kiro_native_executor import KiroNativeExecutor
from omnigent.inner.pi_native_executor import PiNativeExecutor
from omnigent.native.native_bridge_common import (
    read_agent_instructions_preamble,
    write_agent_instructions_preamble,
)

#: (executor class, module holding the injection sink, sink name, whether the
#: sink's failure propagates instead of surfacing as an ``ExecutorError``).
_CASES = [
    pytest.param(PiNativeExecutor, pne, "enqueue_user_message", True, id="pi"),
    pytest.param(CursorNativeExecutor, cne, "inject_user_message", False, id="cursor"),
    pytest.param(KiroNativeExecutor, kne, "inject_user_message", False, id="kiro"),
    pytest.param(GooseNativeExecutor, gne, "inject_user_message", False, id="goose"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("executor_cls", "module", "sink", "raises_through"), _CASES)
async def test_first_turn_delivers_staged_preamble_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executor_cls: type[Any],
    module: ModuleType,
    sink: str,
    raises_through: bool,
) -> None:
    """The first injected message is wrapped, and the file is cleared after."""
    del raises_through
    injected: list[str] = []
    monkeypatch.setattr(module, sink, lambda bridge_dir, content: injected.append(content))
    write_agent_instructions_preamble(tmp_path, "be terse")
    ex = executor_cls(bridge_dir=tmp_path)

    events = [e async for e in ex.run_turn([{"role": "user", "content": "first"}], [], "")]
    assert events and isinstance(events[-1], TurnComplete)
    assert injected[0].startswith("<omnigent_agent_instructions>")
    assert "be terse" in injected[0]
    assert injected[0].endswith("first")
    assert read_agent_instructions_preamble(tmp_path) is None

    # The second turn injects plain text — the preamble was consumed once.
    async for _ in ex.run_turn([{"role": "user", "content": "second"}], [], ""):
        pass
    assert injected[1] == "second"


@pytest.mark.asyncio
@pytest.mark.parametrize(("executor_cls", "module", "sink", "raises_through"), _CASES)
async def test_failed_first_injection_keeps_preamble_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    executor_cls: type[Any],
    module: ModuleType,
    sink: str,
    raises_through: bool,
) -> None:
    """A failed first injection leaves the staged instructions on disk."""

    def _boom(bridge_dir: Path, content: str) -> None:
        raise RuntimeError("tmux target not advertised")

    monkeypatch.setattr(module, sink, _boom)
    write_agent_instructions_preamble(tmp_path, "be terse")
    ex = executor_cls(bridge_dir=tmp_path)
    messages = [{"role": "user", "content": "first"}]

    if raises_through:
        with pytest.raises(RuntimeError):
            [e async for e in ex.run_turn(messages, [], "")]
    else:
        events = [e async for e in ex.run_turn(messages, [], "")]
        assert any(isinstance(e, ExecutorError) for e in events)
    assert read_agent_instructions_preamble(tmp_path) == "be terse"
