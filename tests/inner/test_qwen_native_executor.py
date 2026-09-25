"""Unit tests for QwenNativeExecutor — the harness-side input-file injector.

Mirrors ``tests/inner/test_goose_native_executor.py`` (the closest analog), plus
qwen-specific coverage for the boot-order readiness gate (``_ensure_ready``) that
goose/cursor don't have — qwen drops a submit appended before its input watcher
starts, so the executor must wait for qwen to be ready before the first append.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.qwen_native import bridge as qwen_bridge
from omnigent.harnesses.qwen_native.bridge import events_file_path
from omnigent.inner import qwen_native_executor as qne
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.native.native_bridge_common import (
    read_agent_instructions_preamble,
    write_agent_instructions_preamble,
)


def test_supports_flags(tmp_path: Path) -> None:
    ex = qne.QwenNativeExecutor(bridge_dir=tmp_path)
    # Output is shown by the embedded terminal + mirrored by the forwarder, and
    # mid-turn steering is appended as another submit line.
    assert ex.supports_streaming() is False
    assert ex.supports_live_message_queue() is True


def test_content_to_text_plain_and_parts(tmp_path: Path) -> None:
    assert qne._content_to_text("hello", tmp_path) == "hello"
    blocks = [{"type": "input_text", "text": "a"}, {"type": "text", "text": "b"}]
    assert qne._content_to_text(blocks, tmp_path) == "a\n\nb"
    # Unknown / empty shapes degrade to empty rather than raising.
    assert qne._content_to_text(None, tmp_path) == ""
    assert qne._content_to_text([{"type": "image"}], tmp_path) == ""


def test_content_to_text_materializes_attachment(tmp_path: Path) -> None:
    # An image/file block carrying a base64 data URI is written to the bridge
    # dir and referenced by path (so qwen can open it), not inlined as base64.
    png = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    out = qne._content_to_text(
        [{"type": "input_image", "image_url": png}, {"type": "text", "text": "look"}],
        tmp_path,
    )
    assert "[Attached: " in out
    assert out.endswith("look")  # attachment lines precede the text


def test_latest_user_text_picks_last_user(tmp_path: Path) -> None:
    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    assert qne._latest_user_text(messages, tmp_path) == "second"
    assert qne._latest_user_text([{"role": "assistant", "content": "x"}], tmp_path) == ""


def test_bridge_dir_from_env_requires_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(qne.BRIDGE_DIR_ENV_VAR, raising=False)
    with pytest.raises(RuntimeError):
        qne._bridge_dir_from_env()


def test_bridge_dir_from_env_reads_var(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(qne.BRIDGE_DIR_ENV_VAR, str(tmp_path))
    assert qne._bridge_dir_from_env() == tmp_path


async def test_run_turn_injects_latest_user_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    order: list[str] = []
    monkeypatch.setattr(qne, "wait_for_ready", lambda *a, **k: order.append("ready") or True)

    def _record_submit(_bridge_dir: Path, *, content: str) -> None:
        order.append(f"submit:{content}")

    monkeypatch.setattr(qne, "submit_user_message", _record_submit)
    ex = qne.QwenNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "user", "content": "do it"}], [], "")]
    # Readiness is awaited BEFORE the submit (else qwen drops the first message).
    assert order == ["ready", "submit:do it"]
    assert len(events) == 1 and isinstance(events[0], TurnComplete)
    assert events[0].response is None  # output is terminal-originated


async def test_run_turn_errors_with_no_user_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    submitted: list[str] = []
    monkeypatch.setattr(qne, "wait_for_ready", lambda *a, **k: True)
    monkeypatch.setattr(qne, "submit_user_message", lambda *a, **k: submitted.append("x"))
    ex = qne.QwenNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "assistant", "content": "x"}], [], "")]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert submitted == []  # nothing injected when there's no user text


async def test_run_turn_surfaces_bridge_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qne, "wait_for_ready", lambda *a, **k: True)

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("input file gone")

    monkeypatch.setattr(qne, "submit_user_message", _boom)
    ex = qne.QwenNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "user", "content": "hi"}], [], "")]
    assert len(events) == 1 and isinstance(events[0], ExecutorError)


async def test_enqueue_session_message_injects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    monkeypatch.setattr(qne, "wait_for_ready", lambda *a, **k: True)
    monkeypatch.setattr(
        qne, "submit_user_message", lambda bridge_dir, *, content: seen.append(content)
    )
    ex = qne.QwenNativeExecutor(bridge_dir=tmp_path)
    assert await ex.enqueue_session_message("main", "steer") is True
    assert seen == ["steer"]
    # Empty content is a no-op (no injection).
    assert await ex.enqueue_session_message("main", "") is False
    assert seen == ["steer"]


async def test_ensure_ready_does_not_latch_on_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready_timeouts: list[float] = []

    def _timeout(*_a: object, timeout_s: float, **_k: object) -> bool:
        ready_timeouts.append(timeout_s)
        return False  # readiness gate times out every time

    submitted: list[str] = []
    monkeypatch.setattr(qne, "wait_for_ready", _timeout)
    monkeypatch.setattr(
        qne, "submit_user_message", lambda _b, *, content: submitted.append(content)
    )
    write_agent_instructions_preamble(tmp_path, "be terse")
    ex = qne.QwenNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "user", "content": "a"}], [], "")]
    # A timeout fails the turn instead of appending a message qwen's watcher
    # would skip: nothing written, the preamble stays staged, and ``_ready``
    # stays unlatched so the next turn re-checks.
    assert len(events) == 1 and isinstance(events[0], ExecutorError)
    assert "not sent" in events[0].message
    assert submitted == []
    assert read_agent_instructions_preamble(tmp_path) == "be terse"
    assert ex._ready is False
    assert await ex.enqueue_session_message("main", "steer") is False
    assert submitted == []
    # The executor waits under its own widened bound, not the bridge default.
    assert ready_timeouts == [qne._READY_TIMEOUT_S, qne._READY_TIMEOUT_S]
    assert qne._READY_TIMEOUT_S > 30.0


class _FakeClock:
    """Stand-in for the bridge's ``time`` module: polling advances fake time.

    Installed in place of ``omnigent.harnesses.qwen_native.bridge.time`` so
    ``wait_for_ready``'s poll sleeps cost no real time, and appends the boot
    signal to the events file only once fake time reaches *ready_at*.
    """

    def __init__(self, events_file: Path, ready_at: float) -> None:
        self.now = 0.0
        self._events_file = events_file
        self._ready_at = ready_at
        #: Fake time the boot signal was appended at, or ``None`` if never.
        self.boot_signal_at: float | None = None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        if self.boot_signal_at is None and self.now >= self._ready_at:
            self.boot_signal_at = self.now
            self._events_file.write_text(
                json.dumps({"type": "system", "subtype": "session_start"}) + "\n",
                encoding="utf-8",
            )


async def test_run_turn_waits_for_a_late_boot_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The boot signal lands at fake time 45 s — beyond the bridge's 30 s default,
    # inside the executor's widened bound. With the original 30 s bound the poll
    # gives up (and the turn now fails) before the signal ever appears.
    clock = _FakeClock(events_file_path(tmp_path), ready_at=45.0)
    monkeypatch.setattr(qwen_bridge, "time", clock)

    submitted: list[tuple[float, str]] = []
    monkeypatch.setattr(
        qne,
        "submit_user_message",
        lambda _b, *, content: submitted.append((clock.now, content)),
    )

    ex = qne.QwenNativeExecutor(bridge_dir=tmp_path)
    events = [e async for e in ex.run_turn([{"role": "user", "content": "first"}], [], "")]
    # No append before the boot signal (and thus none before ~45 s); exactly one
    # after it, then the turn completes and readiness latches.
    assert clock.boot_signal_at is not None and clock.boot_signal_at >= 45.0
    assert len(submitted) == 1
    append_at, content = submitted[0]
    assert content == "first"
    assert append_at >= clock.boot_signal_at
    assert len(events) == 1 and isinstance(events[0], TurnComplete)
    assert ex._ready is True


async def test_ensure_ready_is_latched_across_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ready_calls = {"n": 0}

    def _wait(*_a: object, **_k: object) -> bool:
        ready_calls["n"] += 1
        return True

    monkeypatch.setattr(qne, "wait_for_ready", _wait)
    monkeypatch.setattr(qne, "submit_user_message", lambda *a, **k: None)
    ex = qne.QwenNativeExecutor(bridge_dir=tmp_path)
    async for _ in ex.run_turn([{"role": "user", "content": "one"}], [], ""):
        pass
    await ex.enqueue_session_message("main", "two")
    async for _ in ex.run_turn([{"role": "user", "content": "three"}], [], ""):
        pass
    # Readiness is awaited once per session, not once per turn (warm turns don't
    # re-block on the events-file poll).
    assert ready_calls["n"] == 1
