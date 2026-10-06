"""Tests for the native Claude Code bridge executor."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge as claude_bridge
from omnigent.harnesses.claude_native.bridge import (
    REQUEST_SESSION_ID_ENV_VAR,
    ClaudePromptTimeout,
    ClaudeSignInPending,
    ClaudeTerminalDialog,
    ClaudeTerminalExited,
    TmuxSessionNotAdvertised,
)
from omnigent.inner import claude_native_executor
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import ExecutorConfig, ExecutorError, TurnComplete
from omnigent.inner.native_attachments import attachment_cache_dir
from omnigent.llms.context_window import ModelPricing

# Minimal valid 1x1 white PNG used for multimodal attachment tests.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)
_TINY_PNG_DATA_URI = f"data:image/png;base64,{_TINY_PNG_B64}"
_TINY_PNG_BYTES = base64.b64decode(_TINY_PNG_B64)


@pytest.mark.asyncio
async def test_run_turn_injects_user_message_without_streaming_transcript(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Web UI turns are typed into Claude's tmux pane only.

    The background transcript forwarder is the only path allowed to
    produce visible Omnigent chat items. This fails if the executor
    regresses to tailing JSONL and producing duplicate assistant text.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "claude.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    sent_messages: list[dict[str, Any]] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Capture the injected message and write a transcript line.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text typed into the Claude tmux pane.
        :param timeout_s: tmux-target readiness timeout (ignored
            here — the fake doesn't shell out).
        :returns: None.
        """
        del timeout_s
        sent_messages.append({"bridge_dir": bridge_dir_arg, "content": content})
        transcript_path.write_text("terminal-owned output\n", encoding="utf-8")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fake_inject_user_message,
    )

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello from web"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # The executor must deliver exactly the user's text to the
    # bridge. If this assertion changes shape, the harness has
    # picked up an extra envelope (metadata, framing, etc.) that
    # wasn't in the original CLAUDE_NATIVE design.
    assert sent_messages == [
        {
            "bridge_dir": bridge_dir,
            "content": "hello from web",
        }
    ]
    assert events == [TurnComplete(response=None)]
    assert not (bridge_dir / "transcript_forwarder.json").exists()
    assert not (bridge_dir / "transcript_forwarder.pause.json").exists()


@pytest.mark.asyncio
async def test_run_turn_does_not_advertise_active_omnigent_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The executor does not create a second AP-visible tool path.

    Claude-native chat visibility is terminal-originated. Web-chat
    submission is an input adapter, so tool activity must come back
    from Claude's transcript rather than from a transient Omnigent turn.
    """
    bridge_dir = tmp_path / "bridge"
    sent_messages: list[dict[str, Any]] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Capture a web-message injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text typed into the Claude tmux pane.
        :param timeout_s: tmux-target readiness timeout (ignored).
        :returns: None.
        """
        del timeout_s
        sent_messages.append({"bridge_dir": bridge_dir_arg, "content": content})

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "use a tool"}],
            tools=[
                {
                    "name": "sys_os_read",
                    "description": "Read a file.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            system_prompt="ignored",
        )
    ]

    assert sent_messages == [{"bridge_dir": bridge_dir, "content": "use a tool"}]
    assert events == [TurnComplete(response=None)]
    assert not (bridge_dir / "tool_relay.json").exists()


@pytest.mark.asyncio
async def test_run_turn_rejects_stale_session_after_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Old-session turns must not type into the post-``/clear`` Claude pane.

    The request session id comes from the harness spawn env. If it no
    longer matches the bridge's active session, the executor must fail
    before calling tmux injection.
    """
    (tmp_path / "bridge.json").write_text(
        '{"active_session_id": "conv_new"}',
        encoding="utf-8",
    )
    monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_old")

    def fail_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Fail if stale-session protection reaches tmux injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text that would be typed into tmux.
        :param timeout_s: tmux-target readiness timeout.
        :returns: Never returns.
        """
        del bridge_dir_arg, content, timeout_s
        raise AssertionError("stale session injected into tmux")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fail_inject_user_message,
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "old tab message"}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "no longer active after /clear" in events[0].message


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/login", "/logout"])
async def test_run_turn_points_auth_commands_at_omni_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
) -> None:
    """
    ``/login`` must not be typed into the pane as a prompt.

    Claude Code's sign-in is an interactive TUI handoff the bridge
    cannot drive, so the bridge escapes ``/login`` into plain text and
    the CLI answers it as an ordinary message. On an expired login that
    answer is "Login expired · Please run /login" — the instruction the
    user just followed, so the turn is wasted and the session is stuck.
    Fail the turn with the host command that does re-authenticate.
    """

    def fail_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Fail if an auth command reaches tmux injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text that would be typed into tmux.
        :param timeout_s: tmux-target readiness timeout.
        :returns: Never returns.
        """
        del bridge_dir_arg, content, timeout_s
        raise AssertionError("auth slash command injected into tmux")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fail_inject_user_message,
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": command}],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "omni setup" in events[0].message


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["/login", "/logout"])
async def test_enqueue_session_message_refuses_auth_commands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    command: str,
) -> None:
    """
    A live-steered ``/login`` must not be typed into the pane either.

    ``enqueue_session_message`` is the second injection path: a message
    sent while a turn is active. Refusing it (``False``) leaves the
    runner's buffered copy undelivered, so the message arrives as the
    next turn and ``run_turn``'s short-circuit answers it with the
    ``omni setup`` guidance. The monkeypatched injector raises, so this
    test fails if anything reaches tmux.
    """

    def fail_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Fail if an auth command reaches tmux injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text that would be typed into tmux.
        :param timeout_s: tmux-target readiness timeout.
        :returns: Never returns.
        """
        del bridge_dir_arg, content, timeout_s
        raise AssertionError("auth slash command injected into tmux")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fail_inject_user_message,
    )

    executor = ClaudeNativeExecutor(tmp_path)
    accepted = await executor.enqueue_session_message("session-key", command)

    assert accepted is False


@pytest.mark.asyncio
async def test_enqueue_session_message_rejects_stale_session_after_clear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Stale-session steering must not reach the post-``/clear`` Claude pane.
    """
    (tmp_path / "bridge.json").write_text(
        '{"active_session_id": "conv_new"}',
        encoding="utf-8",
    )
    monkeypatch.setenv(REQUEST_SESSION_ID_ENV_VAR, "conv_old")

    def fail_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Fail if stale-session steering reaches tmux injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text that would be typed into tmux.
        :param timeout_s: tmux-target readiness timeout.
        :returns: Never returns.
        """
        del bridge_dir_arg, content, timeout_s
        raise AssertionError("stale session injected into tmux")

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fail_inject_user_message,
    )

    executor = ClaudeNativeExecutor(tmp_path)
    injected = await executor.enqueue_session_message(
        session_key="main",
        content="old steering",
    )

    assert injected is False


@pytest.mark.asyncio
async def test_enqueue_session_message_injects_steering_into_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    In-flight server messages are typed into Claude's tmux pane.

    This catches regressions where web UI steering is accepted by the
    harness but never reaches the native Claude Code process.
    """
    sent_messages: list[dict[str, Any]] = []

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """
        Capture a steering injection.

        :param bridge_dir_arg: Bridge directory passed by the executor.
        :param content: Text typed into the Claude tmux pane.
        :param timeout_s: tmux-target readiness timeout (ignored).
        :returns: None.
        """
        del timeout_s
        sent_messages.append({"bridge_dir": bridge_dir_arg, "content": content})

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fake_inject_user_message,
    )

    executor = ClaudeNativeExecutor(tmp_path)
    accepted = await executor.enqueue_session_message("session-key", "steer me")

    assert accepted is True
    # Steering injection delivers raw text only — no envelope. The
    # session_key is intentionally NOT included since there is one
    # tmux pane per conversation; mixing in routing metadata would
    # cause Claude to see arbitrary key-value pairs as user input.
    assert sent_messages == [
        {
            "bridge_dir": tmp_path,
            "content": "steer me",
        }
    ]


@pytest.mark.asyncio
async def test_concurrent_injections_do_not_overlap_in_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Two injections must not write to the tmux pane at the same time.

    Repro for the claude-native "12"/"23" message-combining symptom.
    ``inject_user_message`` is not atomic: it issues several ``tmux
    send-keys`` calls in sequence (clear line, type literal text, send
    Enter). The executor runs each injection via ``asyncio.to_thread``
    and does NOT serialize them, so a ``run_turn`` injection and a
    mid-turn ``enqueue_session_message`` injection can land in the
    thread pool concurrently and interleave their keystrokes against the
    same pane — e.g. typing "1" and "2" into one prompt as "12".

    This test drives those two real code paths concurrently. The fake
    ``inject_user_message`` records the maximum number of injections
    inside its (otherwise atomic) critical region at once. The invariant
    under test is that the executor serializes terminal writes, so that
    maximum must be 1.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)

    state = {"now": 0, "max": 0}
    state_lock = threading.Lock()
    release = threading.Event()

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """Record peak concurrency, then hold the call open until released.

        :param bridge_dir_arg: Bridge directory (ignored).
        :param content: Text that would be typed into tmux (ignored).
        :param timeout_s: tmux-target readiness timeout (ignored).
        :returns: None.
        """
        del bridge_dir_arg, content, timeout_s
        with state_lock:
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
        # Hold the keystroke sequence open so a second, concurrent
        # injection (if the executor fails to serialize) is observed
        # inside this critical region at the same time, bumping max to 2.
        release.wait(timeout=2.0)
        with state_lock:
            state["now"] -= 1

    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        fake_inject_user_message,
    )

    executor = ClaudeNativeExecutor(tmp_path)

    async def _drive_run_turn() -> None:
        """Consume a run_turn (the initial-message injection path)."""
        async for _ in executor.run_turn(
            messages=[{"role": "user", "content": "one"}],
            tools=[],
            system_prompt="",
        ):
            pass

    # Path A: run_turn injection. Path B: mid-turn steering injection.
    # Both call inject_user_message via asyncio.to_thread concurrently.
    run_turn_task = asyncio.create_task(_drive_run_turn())
    enqueue_task = asyncio.create_task(executor.enqueue_session_message("k", "two"))

    # Sync gate: wait until at least one injection is inside the region.
    for _ in range(200):
        if state["max"] >= 1:
            break
        await asyncio.sleep(0.01)
    # Give the second injection a chance to enter concurrently. With
    # proper serialization it cannot — it would block until the first
    # releases — so max stays 1. Without it, both enter and max hits 2.
    for _ in range(50):
        if state["max"] >= 2:
            break
        await asyncio.sleep(0.01)

    release.set()
    await asyncio.gather(run_turn_task, enqueue_task)

    # max == 2 means both injections wrote to the pane simultaneously,
    # which is exactly the interleaving that combines "1" and "2" into
    # "12". A correct executor serializes terminal writes → max == 1.
    assert state["max"] == 1, (
        f"concurrent injections overlapped in the tmux pane "
        f"(peak concurrency {state['max']}); the executor must serialize "
        f"terminal writes so keystrokes from different messages cannot "
        f"interleave into a single prompt (the '12'/'23' bug)."
    )


# -- Multimodal attachment tests ------------------------------------------


def _stub_inject(
    sent: list[dict[str, Any]],
) -> Any:
    """
    Build a fake ``inject_user_message`` that captures calls.

    :param sent: Mutable list that receives one dict per invocation,
        keyed by ``bridge_dir`` and ``content``.
    :returns: Callable matching ``inject_user_message``'s signature.
    """

    def _fake(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        del timeout_s
        sent.append({"bridge_dir": bridge_dir_arg, "content": content})

    return _fake


@pytest.mark.asyncio
async def test_run_turn_materializes_image_to_bridge_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An ``input_image`` block with a resolved data URI is decoded to a
    file in the bridge directory and referenced by path in the text
    injected into Claude's terminal.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": _TINY_PNG_DATA_URI,
                            "filename": "screenshot.png",
                        },
                        {"type": "input_text", "text": "what is this?"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # Turn completes successfully after injection.
    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    injected = sent[0]["content"]

    # Attachment reference line appears before the user's text.
    # If the image block was silently dropped (pre-fix behavior),
    # the injected text would be just "what is this?" with no path.
    assert "[Attached:" in injected, (
        "Image block was dropped — _content_to_text did not materialize it"
    )
    assert "screenshot.png" in injected
    assert "what is this?" in injected
    # Attachment line must come before user text.
    attach_pos = injected.index("[Attached:")
    text_pos = injected.index("what is this?")
    assert attach_pos < text_pos, (
        "Attachment reference should precede user text so Claude sees the "
        "file path before the question"
    )

    # The file was written to disk with the correct content.
    uploads = attachment_cache_dir(tmp_path)
    written = list(uploads.iterdir())
    # Exactly 1 file — the materialized PNG.
    assert len(written) == 1, (
        f"Expected 1 written file, got {len(written)}. If 0, the attachment was not materialized."
    )
    assert written[0].name == "screenshot.png"
    # Byte-level check: decoded content matches the original PNG.
    assert written[0].read_bytes() == _TINY_PNG_BYTES


@pytest.mark.asyncio
async def test_run_turn_keeps_image_between_surrounding_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The live native turn preserves text-image-text semantic order."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(claude_native_executor, "inject_user_message", _stub_inject(sent))

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "before"},
                        {
                            "type": "input_image",
                            "image_url": _TINY_PNG_DATA_URI,
                            "filename": "middle.png",
                        },
                        {"type": "input_text", "text": "after"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    injected = sent[0]["content"]
    assert injected.index("before") < injected.index("[Attached:") < injected.index("after")


async def test_resize_notice_uses_hidden_hook_context(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Claude terminal input excludes the framework notice."""
    from omnigent.inner.native_attachments import framework_notice_block, resize_notice

    dimensions = {"width": 6000, "height": 4000}
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(claude_native_executor, "inject_user_message", _stub_inject(sent))
    executor = ClaudeNativeExecutor(tmp_path)

    async for _ in executor.run_turn(
        [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "inspect this"},
                    framework_notice_block(dimensions),
                ],
            },
            {"role": "developer", "content": "unrelated context"},
        ],
        [],
        "",
    ):
        pass

    assert sent[0]["content"] == "inspect this"
    assert (tmp_path / "pending_framework_context.txt").read_text() == resize_notice(dimensions)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [RuntimeError("injection failed"), ClaudePromptTimeout("timeout")]
)
async def test_failed_injection_clears_framework_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: Exception
) -> None:
    from omnigent.inner.native_attachments import framework_notice_block

    def fail_inject(*args: Any, **kwargs: Any) -> None:
        assert (tmp_path / "pending_framework_context.txt").exists()
        raise error

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(claude_native_executor, "kill_session", lambda *args, **kwargs: None)
    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "inspect this"},
                        framework_notice_block({"width": 6000, "height": 4000}),
                    ],
                }
            ],
            [],
            "",
        )
    ]
    assert isinstance(events[0], ExecutorError)
    assert not (tmp_path / "pending_framework_context.txt").exists()
    assert not await executor.enqueue_session_message(
        "session-key",
        [
            {"type": "input_text", "text": "inspect this"},
            framework_notice_block({"width": 6000, "height": 4000}),
        ],
    )
    assert not (tmp_path / "pending_framework_context.txt").exists()


@pytest.mark.asyncio
async def test_run_turn_image_only_no_text_still_injects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A message with only an image (no text) materializes the file and
    injects the path reference. The executor must not yield an error.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": _TINY_PNG_DATA_URI,
                            "filename": "photo.png",
                        },
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # Must complete, not error — an image-only message is valid input.
    # If _content_to_text returned "" (dropping the image), the
    # executor would yield ExecutorError instead of TurnComplete.
    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    assert "photo.png" in sent[0]["content"]


@pytest.mark.asyncio
async def test_run_turn_unresolved_file_id_emits_visible_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An ``input_image`` block with only a ``file_id`` (content resolver
    did not run) injects a visible could-not-load marker with the text.

    Silently skipping the block made the model hallucinate the image;
    the marker tells both the model and the user the attachment is gone.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "file_id": "file_abc123"},
                        {"type": "input_text", "text": "analyze this"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    # The unresolved image block becomes a visible marker, not a silent drop.
    assert sent[0]["content"] == ("[Attachment file_abc123 could not be loaded]\n\nanalyze this")
    # No uploads directory created — nothing to materialize.
    assert not (attachment_cache_dir(tmp_path)).exists()


@pytest.mark.asyncio
async def test_run_turn_materializes_zip_outside_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A ZIP reaches Claude by absolute cache path without changing the checkout."""
    import json

    workspace = tmp_path / "repo"
    workspace.mkdir()
    (tmp_path / "bridge.json").write_text(json.dumps({"workspace": str(workspace)}))

    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(claude_native_executor, "inject_user_message", _stub_inject(sent))

    zip_bytes = b"PK\x03\x04 fake zip"
    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "file_data": (
                                "data:application/zip;base64,"
                                f"{base64.b64encode(zip_bytes).decode()}"
                            ),
                            "filename": "bundle.zip",
                        },
                        {"type": "input_text", "text": "what is in here?"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    materialized = attachment_cache_dir(tmp_path) / "bundle.zip"
    assert materialized.read_bytes() == zip_bytes
    assert list(workspace.iterdir()) == []
    assert f"[Attached: {materialized}]" in sent[0]["content"]


@pytest.mark.asyncio
async def test_run_turn_materializes_zip_without_workspace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Attachment delivery works before any workspace is recorded at launch."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(claude_native_executor, "inject_user_message", _stub_inject(sent))

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_file",
                            "file_data": "data:application/zip;base64,UEsDBA==",
                            "filename": "bundle.zip",
                        },
                        {"type": "input_text", "text": "unpack this"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    assert (
        sent[0]["content"]
        == f"[Attached: {attachment_cache_dir(tmp_path) / 'bundle.zip'}]\n\nunpack this"
    )


@pytest.mark.asyncio
async def test_run_turn_dedup_same_filename(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Two different images under the same filename produce distinct files
    (the second gets a unique suffix instead of overwriting the first).
    Identical bytes would instead reuse the existing file.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    other_bytes = b"not-the-same-png"
    image_block = {
        "type": "input_image",
        "image_url": _TINY_PNG_DATA_URI,
        "filename": "dup.png",
    }
    other_block = {
        "type": "input_image",
        "image_url": "data:image/png;base64," + base64.b64encode(other_bytes).decode(),
        "filename": "dup.png",
    }
    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [image_block, other_block],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    uploads = attachment_cache_dir(tmp_path)
    written = sorted(uploads.iterdir())
    # Two distinct files, not one overwritten file.
    assert len(written) == 2, (
        f"Expected 2 files (dedup suffix), got {len(written)}. "
        "If 1, the second image overwrote the first."
    )
    # Both payloads survive, each in its own file.
    assert {f.read_bytes() for f in written} == {_TINY_PNG_BYTES, other_bytes}


@pytest.mark.asyncio
async def test_run_turn_image_without_filename_gets_generated_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An image block without a ``filename`` field gets a generated name
    with the correct extension derived from the MIME type.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": _TINY_PNG_DATA_URI},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    uploads = attachment_cache_dir(tmp_path)
    written = list(uploads.iterdir())
    assert len(written) == 1
    # Generated name should have .png extension from the data URI MIME.
    assert written[0].suffix == ".png", (
        f"Expected .png extension, got {written[0].suffix}. "
        "MIME-to-extension mapping may be missing for image/png."
    )


@pytest.mark.asyncio
async def test_enqueue_session_message_materializes_image(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Steering messages with multimodal content blocks also materialize
    attachments (same path as ``run_turn``).
    """
    from omnigent.harnesses.claude_native.bridge import CLAUDE_FRAMEWORK_CONTEXT_FILE
    from omnigent.inner.native_attachments import framework_notice_block, resize_notice

    dimensions = {"width": 6000, "height": 4000}
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    accepted = await executor.enqueue_session_message(
        "session-key",
        [
            {
                "type": "input_image",
                "image_url": _TINY_PNG_DATA_URI,
                "filename": "steering_img.png",
            },
            framework_notice_block(dimensions),
            {"type": "input_text", "text": "look at this"},
        ],
    )

    assert accepted is True
    assert len(sent) == 1
    injected = sent[0]["content"]
    assert "steering_img.png" in injected
    assert "look at this" in injected
    assert "downscaled" not in injected
    assert (tmp_path / CLAUDE_FRAMEWORK_CONTEXT_FILE).read_text() == resize_notice(dimensions)
    # File was written to the bridge directory.
    written = list((attachment_cache_dir(tmp_path)).iterdir())
    assert len(written) == 1
    assert written[0].name == "steering_img.png"
    assert await executor.enqueue_session_message("session-key", "follow-up")
    assert not (tmp_path / CLAUDE_FRAMEWORK_CONTEXT_FILE).exists()


@pytest.mark.asyncio
async def test_run_turn_malformed_data_uri_emits_visible_marker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    An image block with a malformed data URI injects a visible
    could-not-load marker with the text; the turn does not error.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,NOT_VALID_BASE64!@#",
                        },
                        {"type": "input_text", "text": "still send this"},
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    # Turn completes — the bad image becomes a marker, text is injected.
    assert events == [TurnComplete(response=None)]
    assert len(sent) == 1
    assert sent[0]["content"] == ("[Attachment attachment could not be loaded]\n\nstill send this")
    # No file written for the malformed URI.
    assert not (attachment_cache_dir(tmp_path)).exists()


@pytest.mark.asyncio
async def test_run_turn_path_traversal_filename_sanitized(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A filename with path traversal components is stripped to the base name.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        _stub_inject(sent),
    )

    executor = ClaudeNativeExecutor(tmp_path)
    events = [
        event
        async for event in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_image",
                            "image_url": _TINY_PNG_DATA_URI,
                            "filename": "../../.bashrc",
                        },
                    ],
                }
            ],
            tools=[],
            system_prompt="ignored",
        )
    ]

    assert events == [TurnComplete(response=None)]
    uploads = attachment_cache_dir(tmp_path)
    written = list(uploads.iterdir())
    assert len(written) == 1
    assert written[0].name == ".bashrc"
    assert written[0].parent == uploads


@pytest.mark.asyncio
async def test_run_turn_applies_routed_model_before_message_under_one_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A routed model is switched via ``/model`` BEFORE the message, in order.

    Intelligent routing delivers the picked model on the turn (adapter maps
    ``request.model_override`` -> ``config.model``). The executor must type
    ``/model <routed>`` and then inject the message as one sequence under
    ``_inject_lock`` — never as a separate racing writer. This test records
    the call order across both injectors and asserts ``/model`` lands first,
    then the message, exactly once each. A regression that dropped the switch
    (or ran it concurrently) would fail the ordering assertion.

    The typed argument is the session's alias for the routed catalog id:
    ``/model`` rejects a bare gateway id and silently keeps the old model.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    bridge_dir = tmp_path / "bridge"
    calls: list[tuple[str, str]] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        """Record the ``/model`` switch keystroke and its auto_confirm flag."""
        del bridge_dir_arg, timeout_s
        calls.append(("slash", command, auto_confirm))

    def fake_inject_user_message(
        bridge_dir_arg: Path,
        *,
        content: str,
        timeout_s: float = 30.0,
    ) -> None:
        """Record the message inject."""
        del bridge_dir_arg, timeout_s
        calls.append(("message", content))

    # No ucode profile at launch -> unknown baseline -> the routed model is
    # treated as a change and switched.
    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {"ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-5"},
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "review this function"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-sonnet-5"),
        )
    ]

    assert calls == [
        # auto_confirm=True mirrors the manual picker path so the switch is
        # accepted if the CLI ever pops a confirmation dialog.
        ("slash", "/model sonnet", True),
        ("message", "review this function"),
    ], f"Expected /model (auto_confirm) then message, in order; got {calls}."
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_uses_the_custom_model_slot_id_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A model pinned to the custom picker slot is applied exactly."""
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    slash_calls: list[str] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del bridge_dir_arg, timeout_s, auto_confirm, confirm_hint
        slash_calls.append(command)

    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "databricks-claude-sonnet-4-6",
            "ANTHROPIC_CUSTOM_MODEL_OPTION": "databricks-claude-sonnet-5",
        },
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        lambda bridge_dir_arg, *, content, timeout_s=30.0: None,
    )

    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-sonnet-5"),
        )
    ]

    assert slash_calls == ["/model databricks-claude-sonnet-5"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_types_a_managed_picker_row_verbatim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A routed model of no Claude family is spelled by the pane's picker.

    A workspace-managed picker lists every served model by its own id, and
    ``/model`` takes those verbatim — but no alias pin covers them, so the
    switch was skipped and the turn ran on the launch model.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    slash_calls: list[str] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del bridge_dir_arg, timeout_s, auto_confirm, confirm_hint
        slash_calls.append(command)

    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "system.ai.claude-opus-4-8[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "system.ai.claude-sonnet-4-6[1m]",
        },
    )
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_picker_values",
        lambda _bridge: ["system.ai.claude-opus-4-8[1m]", "system.ai.glm-5-3"],
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        lambda bridge_dir_arg, *, content, timeout_s=30.0: None,
    )

    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="system.ai.glm-5-3"),
        )
    ]

    assert slash_calls == ["/model system.ai.glm-5-3"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_skips_switch_for_untranslatable_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A routed id this session can't spell fails open — message still sent.

    Typing a value ``/model`` doesn't accept leaves the pane on its old
    model while reporting success, so the switch is skipped instead.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    slash_calls: list[str] = []
    msg_calls: list[str] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del bridge_dir_arg, timeout_s, auto_confirm, confirm_hint
        slash_calls.append(command)

    def fake_inject_user_message(
        bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir_arg, timeout_s
        msg_calls.append(content)

    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    # Only opus is pinned, so a sonnet id has no spelling this pane accepts:
    # the bare "sonnet" alias would resolve to a vendor id the gateway rejects.
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-8"},
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-sonnet-5"),
        )
    ]

    assert slash_calls == []
    assert msg_calls == ["hello"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_skips_switch_when_the_family_pin_drifted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A mismatched family pin must not be spoken as its alias.

    The workspace serves two opus generations and ``opus`` is pinned to the
    newer one, so ``/model opus`` would move the pane off the routed model
    while the transcript claimed it ran.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    slash_calls: list[str] = []
    msg_calls: list[str] = []

    monkeypatch.setattr(claude_native_executor, "read_launch_model", lambda _bridge: None)
    monkeypatch.setattr(
        claude_native_executor,
        "read_model_env",
        lambda _bridge: {"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5"},
    )
    monkeypatch.setattr(
        claude_native_executor,
        "inject_slash_command",
        lambda bridge_dir_arg, *, command, timeout_s=30.0, auto_confirm=False, confirm_hint=None: (
            slash_calls.append(command)
        ),
    )
    monkeypatch.setattr(
        claude_native_executor,
        "inject_user_message",
        lambda bridge_dir_arg, *, content, timeout_s=30.0: msg_calls.append(content),
    )

    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-opus-4-8"),
        )
    ]

    assert slash_calls == []
    assert msg_calls == ["hello"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_without_model_override_injects_message_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    No routed model -> no ``/model`` typed, message injected as before.

    A non-routing turn (config.model None) must not touch the model at all —
    typing ``/model`` on every turn would be wrong and noisy.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    bridge_dir = tmp_path / "bridge"
    slash_calls: list[str] = []
    msg_calls: list[str] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del bridge_dir_arg, timeout_s, auto_confirm, confirm_hint
        slash_calls.append(command)

    def fake_inject_user_message(
        bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir_arg, timeout_s
        msg_calls.append(content)

    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model=None),
        )
    ]

    assert slash_calls == [], f"No /model expected without a routed model; got {slash_calls}."
    assert msg_calls == ["hi"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_skips_model_switch_when_already_on_that_model(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A routed model equal to the pane's launch model types no ``/model``.

    ``read_launch_model`` reports what Claude booted with; if routing picks
    that same model there is nothing to switch, so the executor must inject
    the message only. Prevents a pointless ``/model`` (and the turn churn it
    would add) when the pane is already correct.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    bridge_dir = tmp_path / "bridge"
    slash_calls: list[str] = []
    msg_calls: list[str] = []

    def fake_inject_slash_command(
        bridge_dir_arg: Path,
        *,
        command: str,
        timeout_s: float = 30.0,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del bridge_dir_arg, timeout_s, auto_confirm, confirm_hint
        slash_calls.append(command)

    def fake_inject_user_message(
        bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir_arg, timeout_s
        msg_calls.append(content)

    monkeypatch.setattr(
        claude_native_executor,
        "read_launch_model",
        lambda _bridge: "databricks-claude-opus-4-8",
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-opus-4-8"),
        )
    ]

    assert slash_calls == [], f"No /model expected when already on that model; got {slash_calls}."
    assert msg_calls == ["hello"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_a_routed_first_message_switches_the_model_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The replay of a routed first message must not re-issue ``/model``.

    First-message routing blocks the prompt, types the switch itself, then
    replays the prompt with the same ``model_override``. Seeding the baseline
    from ``launch_model`` (written once at bridge prepare) made the replay
    compare against the PRE-switch model and type a second, redundant
    ``/model`` — visible in the transcript ahead of the very first turn.
    """
    monkeypatch.delenv(REQUEST_SESSION_ID_ENV_VAR, raising=False)
    bridge_dir = tmp_path / "bridge"
    slash_calls: list[str] = []
    msg_calls: list[str] = []

    def fake_inject_slash_command(bridge_dir_arg: Path, *, command: str, **kwargs: object) -> None:
        del bridge_dir_arg, kwargs
        slash_calls.append(command)

    def fake_inject_user_message(
        bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0
    ) -> None:
        del bridge_dir_arg, timeout_s
        msg_calls.append(content)

    # The launch model is stale — the turn router already moved the pane, and
    # only the statusLine capture knows it.
    monkeypatch.setattr(
        claude_native_executor,
        "read_launch_model",
        lambda _bridge: "databricks-claude-sonnet-5",
    )
    monkeypatch.setattr(
        claude_native_executor,
        "read_claude_status_model",
        lambda _bridge: "claude-opus-4-8",
    )
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", fake_inject_slash_command)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", fake_inject_user_message)

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            system_prompt="",
            config=ExecutorConfig(model="databricks-claude-opus-4-8"),
        )
    ]

    assert slash_calls == [], (
        f"The router already switched the pane; the replay must type nothing. Got {slash_calls}."
    )
    assert msg_calls == ["hello"]
    assert events == [TurnComplete(response=None)]


@pytest.mark.asyncio
async def test_run_turn_reaps_tmux_before_reporting_prompt_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A readiness timeout cannot leave a failed turn's pane alive."""
    bridge_dir = tmp_path / "bridge"
    killed: list[Path] = []

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudePromptTimeout("terminal did not become ready")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(
        claude_native_executor,
        "kill_session",
        lambda bridge_dir_arg, *, timeout_s: killed.append(bridge_dir_arg),
    )

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert killed == [bridge_dir]
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)


@pytest.mark.asyncio
async def test_run_turn_keeps_the_pane_when_a_terminal_dialog_blocks_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A dialog holding the terminal fails the turn without reaping the pane.

    The person answers the dialog in the embedded terminal and resends, so
    killing the pane would destroy the very thing they need. Only readiness
    timeouts reap; the dialog error takes the plain delivery-failure path.
    """
    bridge_dir = tmp_path / "bridge"
    killed: list[Path] = []

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudeTerminalDialog(
            "Claude Code is waiting for an answer in its terminal "
            "(New MCP server found in this project: e2e-noop), so the message was not delivered."
        )

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(
        claude_native_executor,
        "kill_session",
        lambda bridge_dir_arg, *, timeout_s: killed.append(bridge_dir_arg),
    )

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert killed == []
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)
    assert "waiting for an answer" in events[0].message


@pytest.mark.asyncio
async def test_run_turn_keeps_the_pane_when_a_sign_in_prompt_blocks_delivery(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A launcher sign-in prompt fails the turn with the link and leaves the pane.

    The person signs in from the card's link and the launcher continues into
    Claude Code on its own; reaping the pane would destroy that prompt. The
    error carries the semantic code, headline and next step.
    """
    bridge_dir = tmp_path / "bridge"
    killed: list[Path] = []

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudeSignInPending(
            "Claude Code is waiting for a sign-in in this session's terminal, "
            "so the message was not delivered.",
            title="Claude Code can't start until you sign in to Databricks",
            remediation="Open https://signin.example.com/device and enter code HQ7M-2KPD.",
        )

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(
        claude_native_executor,
        "kill_session",
        lambda bridge_dir_arg, *, timeout_s: killed.append(bridge_dir_arg),
    )

    executor = ClaudeNativeExecutor(bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert killed == []
    assert len(events) == 1
    error = events[0]
    assert isinstance(error, ExecutorError)
    assert error.code == "databricks_sign_in_pending"
    assert error.title == "Claude Code can't start until you sign in to Databricks"
    assert error.remediation is not None
    assert "HQ7M-2KPD" in error.remediation
    # The gate failed before the prompt was typed: the message never reached Claude Code.
    assert error.undelivered is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exit_status", "expect_error_level"),
    [("0", False), ("1", True), ("137", True), (None, True)],
)
async def test_run_turn_logs_a_closed_terminal_below_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    exit_status: str | None,
    expect_error_level: bool,
) -> None:
    """
    Closing Claude Code is not an Omnigent defect, so it is not an ERROR.

    Claude Code exits 0 on ``/quit`` or a closed window. Every turn sent
    afterwards must still fail — the pane is gone — but logging that as an
    ERROR reports the person's own teardown as a mid-session failure. A
    pane that died on its own (any other wait-status, or none recorded)
    keeps the ERROR and its traceback.
    """
    bridge_dir = tmp_path / "bridge"

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudeTerminalExited(
            "The Claude Code terminal has exited, so the message was not delivered.",
            exit_status=exit_status,
        )

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(
        claude_native_executor, "kill_session", lambda bridge_dir_arg, *, timeout_s: None
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.inner.claude_native_executor"):
        events = [
            event
            async for event in ClaudeNativeExecutor(bridge_dir).run_turn(
                messages=[{"role": "user", "content": "hi"}],
                tools=[],
                system_prompt="",
            )
        ]

    # The turn still fails: the pane is gone either way.
    assert len(events) == 1
    assert isinstance(events[0], ExecutorError)

    records = [r for r in caplog.records if "terminal exited" in r.getMessage()]
    assert len(records) == 1
    assert (records[0].levelno == logging.ERROR) is expect_error_level
    # A traceback only earns its place when something actually broke.
    assert bool(records[0].exc_info) is expect_error_level


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["message", "steering", "model"])
@pytest.mark.parametrize("watchdog", [False, True])
async def test_cancelled_delivery_drains_worker_before_unlocking(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    delivery: str,
    watchdog: bool,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    commands: list[tuple[str, ...]] = []
    reaped: list[Path] = []

    def capture(socket_path: str, tmux_target: str) -> str:
        started.set()
        assert release.wait(5), "test did not release the in-flight capture"
        return "────────────────\n❯ \n────────────────\n"

    def inject(*args: Any, **kwargs: Any) -> None:
        try:
            claude_bridge._wait_for_claude_prompt_ready("/tmp/sock", "main", timeout_s=30)
            claude_bridge._run_tmux("/tmp/sock", "send-keys", "late message", "Enter")
        finally:
            finished.set()

    def reap(bridge_dir: Path, *, timeout_s: float) -> None:
        assert finished.is_set()
        claude_bridge._check_injection_cancelled()
        reaped.append(bridge_dir)

    def run(cmd: list[str], **kwargs: Any) -> Any:
        commands.append(tuple(cmd))
        raise AssertionError("cancelled delivery sent keystrokes")

    monkeypatch.setattr(claude_bridge, "_capture_pane", capture)
    monkeypatch.setattr("subprocess.run", run)
    monkeypatch.setattr(claude_native_executor, "inject_user_message", inject)
    monkeypatch.setattr(claude_native_executor, "inject_slash_command", inject)
    monkeypatch.setattr(claude_native_executor, "kill_session", reap)
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    monkeypatch.setattr(
        executor, "_model_command_arg", lambda model: "sonnet" if delivery == "model" else None
    )

    async def deliver() -> None:
        async with asyncio.timeout(0.1 if watchdog else 5):
            if delivery == "steering":
                await executor.enqueue_session_message("session", "hello")
            else:
                async for _event in executor.run_turn(
                    messages=[{"role": "user", "content": "hello"}],
                    tools=[],
                    system_prompt="",
                ):
                    raise AssertionError("cancelled turn emitted a completion")

    task = asyncio.create_task(deliver())
    try:
        assert await asyncio.to_thread(started.wait, 2)
        if not watchdog:
            task.cancel()
        async with asyncio.timeout(2):
            while not task.cancelling():
                await asyncio.sleep(0.005)
        await asyncio.sleep(0)
        assert not task.done()
        assert executor._inject_lock.locked()
        if not watchdog:
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
        release.set()
        await asyncio.wait({task}, timeout=2)
        assert task.done(), "cancelled delivery did not finish after its capture was released"
        with pytest.raises(TimeoutError if watchdog else asyncio.CancelledError):
            task.result()
        assert finished.is_set()
        assert not executor._inject_lock.locked()
        assert reaped == [tmp_path / "bridge"]
        assert commands == []
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.wait({task})
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            task.result()


@pytest.mark.asyncio
async def test_run_turn_does_not_reap_unrelated_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Only the readiness failure that terminalizes the turn triggers cleanup."""
    killed: list[Path] = []

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise RuntimeError("tmux send-keys failed")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(
        claude_native_executor,
        "kill_session",
        lambda *args, **kwargs: killed.append(args[0]),
    )

    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert killed == []
    assert isinstance(events[0], ExecutorError)


@pytest.mark.asyncio
async def test_run_turn_reports_reap_failure_with_prompt_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed hard-stop remains visible beside the delivery error."""

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudePromptTimeout("terminal did not become ready")

    def fail_kill(bridge_dir_arg: Path, *, timeout_s: float) -> None:
        del bridge_dir_arg, timeout_s
        raise RuntimeError("tmux kill failed")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(claude_native_executor, "kill_session", fail_kill)

    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert isinstance(events[0], ExecutorError)
    assert "terminal did not become ready" in events[0].message
    assert "Cleanup also failed: tmux kill failed" in events[0].message


@pytest.mark.asyncio
async def test_run_turn_ignores_missing_tmux_during_timeout_reap(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A pane that exited during readiness polling needs no cleanup error."""

    def fail_inject(bridge_dir_arg: Path, *, content: str, timeout_s: float = 30.0) -> None:
        del bridge_dir_arg, content, timeout_s
        raise ClaudePromptTimeout("terminal did not become ready")

    def absent_kill(bridge_dir_arg: Path, *, timeout_s: float) -> None:
        del bridge_dir_arg, timeout_s
        raise TmuxSessionNotAdvertised("not advertised")

    monkeypatch.setattr(claude_native_executor, "inject_user_message", fail_inject)
    monkeypatch.setattr(claude_native_executor, "kill_session", absent_kill)

    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
        )
    ]

    assert isinstance(events[0], ExecutorError)
    assert events[0].message == "terminal did not become ready"


@pytest.mark.asyncio
async def test_keep_warm_maps_a_guard_skip_onto_the_receipt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A guard refusal comes back as the skip receipt, with the inject lock held throughout."""
    bridge_dir = tmp_path / "bridge"
    observed: dict[str, Any] = {}

    def fake_run(bridge_dir_arg: Path) -> claude_bridge.KeepWarmBtwResult:
        observed["bridge_dir"] = bridge_dir_arg
        observed["lock_held"] = executor._inject_lock.locked()
        return claude_bridge.KeepWarmBtwResult("skipped", "composer_draft")

    monkeypatch.setattr(claude_native_executor, "run_keep_warm_btw", fake_run)
    executor = ClaudeNativeExecutor(bridge_dir)

    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")

    assert observed == {"bridge_dir": bridge_dir, "lock_held": True}
    assert not executor._inject_lock.locked()
    assert receipt == {
        "attempt_id": "att-1",
        "outcome": "skipped",
        "reason": "composer_draft",
        "input_total": None,
        "cache_read": None,
        "cache_write": None,
        "cost_usd": None,
        "estimated": False,
    }


@pytest.mark.asyncio
async def test_keep_warm_with_an_injection_in_flight_skips_busy_without_waiting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An ``_inject_lock`` held past the bounded acquire wait means a real
    injection owns the pane: the ping waits only that budget, then answers
    ``skipped``/``busy`` — no queueing behind the holder, no bridge call."""
    bridge_dir = tmp_path / "bridge"

    def fail_run(bridge_dir_arg: Path) -> claude_bridge.KeepWarmBtwResult:
        del bridge_dir_arg
        raise AssertionError("run_keep_warm_btw must not be called")

    monkeypatch.setattr(claude_native_executor, "run_keep_warm_btw", fail_run)
    monkeypatch.setattr(claude_native_executor, "_KEEP_WARM_LOCK_WAIT_S", 0.05, raising=False)
    executor = ClaudeNativeExecutor(bridge_dir)
    release = asyncio.Event()

    async def holder() -> None:
        async with executor._inject_lock:
            await release.wait()

    holder_task = asyncio.create_task(holder())
    await asyncio.sleep(0)  # the holder owns the lock before the ping starts

    started = time.monotonic()
    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")
    waited = time.monotonic() - started

    # The skip came from the bounded wait expiring, not a free lock.
    assert waited >= 0.04
    assert executor._inject_lock.locked()
    release.set()
    await holder_task
    assert receipt == {
        "attempt_id": "att-1",
        "outcome": "skipped",
        "reason": "busy",
        "input_total": None,
        "cache_read": None,
        "cache_write": None,
        "cost_usd": None,
        "estimated": False,
    }


@pytest.mark.asyncio
async def test_keep_warm_queued_behind_a_woken_injection_skips_busy(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The release race: the holder releases ``_inject_lock`` with an
    injection queued on it, and the ping reaches the acquire before the
    woken injection resumes. The free lock still has a live waiter, so the
    ping queues behind it and the bounded wait skips it ``busy``; the
    injection — not the ping — gets the lock, and the bridge never runs."""
    bridge_dir = tmp_path / "bridge"

    def fail_run(bridge_dir_arg: Path) -> claude_bridge.KeepWarmBtwResult:
        del bridge_dir_arg
        raise AssertionError("run_keep_warm_btw must not be called")

    monkeypatch.setattr(claude_native_executor, "run_keep_warm_btw", fail_run)
    monkeypatch.setattr(claude_native_executor, "_KEEP_WARM_LOCK_WAIT_S", 0.05, raising=False)
    executor = ClaudeNativeExecutor(bridge_dir)
    injection_ran = asyncio.Event()
    finish_injection = asyncio.Event()

    await executor._inject_lock.acquire()  # the current holder

    async def injection() -> None:
        async with executor._inject_lock:
            injection_ran.set()
            await finish_injection.wait()

    injection_task = asyncio.create_task(injection())
    await asyncio.sleep(0)  # the injection parks on the held lock
    executor._inject_lock.release()  # wakes the injection; it has not resumed yet

    async def releaser() -> None:
        await asyncio.sleep(1.0)
        finish_injection.set()

    releaser_task = asyncio.create_task(releaser())
    # Awaiting the ping directly runs it into the acquire while the woken
    # injection is still queued to resume: the free lock has a live waiter,
    # so the ping queues behind it instead of seeing a free lock.
    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")

    assert injection_ran.is_set()
    assert executor._inject_lock.locked()
    releaser_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await releaser_task
    finish_injection.set()
    await injection_task
    assert receipt == {
        "attempt_id": "att-1",
        "outcome": "skipped",
        "reason": "busy",
        "input_total": None,
        "cache_read": None,
        "cache_write": None,
        "cost_usd": None,
        "estimated": False,
    }


@pytest.mark.asyncio
async def test_keep_warm_ok_receipt_estimates_cost_from_the_statusline_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    ``ok`` prices the session's current context tokens (input + cache
    creation + cache read from ``context.json``) as cache reads on the
    statusLine's model, flagged ``estimated``.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "context.json").write_text(
        json.dumps(
            {
                "context_window_size": 1000000,
                "model": "claude-sonnet-4-6",
                "current_usage": {
                    "input_tokens": 1000,
                    "cache_creation_input_tokens": 2000,
                    "cache_read_input_tokens": 34000,
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claude_native_executor,
        "run_keep_warm_btw",
        lambda _: claude_bridge.KeepWarmBtwResult("ok", None),
    )
    priced: dict[str, Any] = {}

    def fake_pricing(model: str) -> ModelPricing:
        priced["model"] = model
        return ModelPricing(
            input_per_token=3e-6,
            output_per_token=15e-6,
            cache_read_per_token=3e-7,
            cache_write_per_token=3.75e-6,
        )

    monkeypatch.setattr(claude_native_executor, "fetch_model_pricing", fake_pricing)
    executor = ClaudeNativeExecutor(bridge_dir)

    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")

    assert priced["model"] == "claude-sonnet-4-6"
    assert receipt["attempt_id"] == "att-1"
    assert receipt["outcome"] == "ok"
    assert receipt["reason"] is None
    assert receipt["input_total"] is None
    assert receipt["cache_read"] == 37000
    assert receipt["cache_write"] is None
    assert receipt["cost_usd"] == pytest.approx(0.0111)
    assert receipt["estimated"] is True


@pytest.mark.asyncio
async def test_keep_warm_ok_receipt_leaves_cost_none_when_unpriceable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No snapshot at all → tokens unknown; an unpriceable model → no dollar figure."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    monkeypatch.setattr(
        claude_native_executor,
        "run_keep_warm_btw",
        lambda _: claude_bridge.KeepWarmBtwResult("ok", None),
    )
    executor = ClaudeNativeExecutor(bridge_dir)

    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")

    assert receipt == {
        "attempt_id": "att-1",
        "outcome": "ok",
        "reason": None,
        "input_total": None,
        "cache_read": None,
        "cache_write": None,
        "cost_usd": None,
        "estimated": True,
    }

    (bridge_dir / "context.json").write_text(
        json.dumps(
            {
                "context_window_size": 1000000,
                "model": "some-unpriced-model",
                "current_usage": {"input_tokens": 37000},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_native_executor, "fetch_model_pricing", lambda _: None)

    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")

    assert receipt["cache_read"] == 37000
    assert receipt["cost_usd"] is None


@pytest.mark.asyncio
async def test_keep_warm_cost_prices_on_a_worker_thread_not_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The catalog lookup may hit the network, so pricing must not block the loop."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "context.json").write_text(
        json.dumps(
            {
                "context_window_size": 1000000,
                "model": "claude-sonnet-4-6",
                "current_usage": {"input_tokens": 1000},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claude_native_executor,
        "run_keep_warm_btw",
        lambda _: claude_bridge.KeepWarmBtwResult("ok", None),
    )
    loop_thread = threading.get_ident()
    priced_on: list[int] = []

    def fake_pricing(model: str) -> ModelPricing:
        del model
        priced_on.append(threading.get_ident())
        return ModelPricing(
            input_per_token=3e-6,
            output_per_token=15e-6,
            cache_read_per_token=3e-7,
            cache_write_per_token=3.75e-6,
        )

    monkeypatch.setattr(claude_native_executor, "fetch_model_pricing", fake_pricing)
    executor = ClaudeNativeExecutor(bridge_dir)

    receipt = await executor.keep_warm(attempt_id="att-thread", family="claude")

    assert receipt["cost_usd"] is not None
    assert priced_on and all(ident != loop_thread for ident in priced_on)


def _keep_warm_context_file(
    bridge_dir: Path, *, total_cost_usd: float, model: str = "claude-sonnet-4-6"
) -> None:
    """Write a statusLine snapshot carrying the given cumulative session cost."""
    payload = json.dumps(
        {
            "context_window_size": 1000000,
            "model": model,
            "current_usage": {
                "input_tokens": 1000,
                "cache_creation_input_tokens": 2000,
                "cache_read_input_tokens": 34000,
            },
            "total_cost_usd": total_cost_usd,
        }
    )
    # Atomic like the real statusLine writer: an injection's rewrite must
    # never read back torn now that pricing runs after the lock is released.
    tmp = bridge_dir / "context.json.tmp"
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, bridge_dir / "context.json")


def _keep_warm_pricing(model: str) -> ModelPricing:
    """The fixed price sheet the estimate assertions are worked from."""
    del model
    return ModelPricing(
        input_per_token=3e-6,
        output_per_token=15e-6,
        cache_read_per_token=3e-7,
        cache_write_per_token=3.75e-6,
    )


@pytest.mark.asyncio
async def test_keep_warm_ok_receipt_reports_the_measured_statusline_cost_delta(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    The statusLine's cumulative ``total_cost_usd`` includes the side
    question: when it moves after an ok ping, the receipt carries the real
    delta, ``estimated=False``, and a measured ``cache_result`` — here a
    $0.02 delta against a $0.0111 cache-read estimate is a hit.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _keep_warm_context_file(bridge_dir, total_cost_usd=1.00)

    def fake_run(bridge_dir_arg: Path) -> claude_bridge.KeepWarmBtwResult:
        # Claude Code settles the side question's cost into the snapshot.
        _keep_warm_context_file(bridge_dir_arg, total_cost_usd=1.02)
        return claude_bridge.KeepWarmBtwResult("ok", None)

    monkeypatch.setattr(claude_native_executor, "run_keep_warm_btw", fake_run)
    monkeypatch.setattr(claude_native_executor, "fetch_model_pricing", _keep_warm_pricing)
    executor = ClaudeNativeExecutor(bridge_dir)

    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")

    assert receipt == {
        "attempt_id": "att-1",
        "outcome": "ok",
        "reason": None,
        "input_total": None,
        "cache_read": 37000,
        "cache_write": None,
        "cost_usd": pytest.approx(0.02),
        "estimated": False,
        "cache_result": "hit",
    }


@pytest.mark.asyncio
async def test_keep_warm_ok_receipt_marks_a_measured_miss(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A delta past 4x the cache-read estimate means the ping rebuilt the cache: ``miss``."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _keep_warm_context_file(bridge_dir, total_cost_usd=1.00)

    def fake_run(bridge_dir_arg: Path) -> claude_bridge.KeepWarmBtwResult:
        _keep_warm_context_file(bridge_dir_arg, total_cost_usd=1.10)
        return claude_bridge.KeepWarmBtwResult("ok", None)

    monkeypatch.setattr(claude_native_executor, "run_keep_warm_btw", fake_run)
    monkeypatch.setattr(claude_native_executor, "fetch_model_pricing", _keep_warm_pricing)
    executor = ClaudeNativeExecutor(bridge_dir)

    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")

    # Estimate: 37000 tokens x $3e-7 = $0.0111; the $0.10 delta is > 4x that.
    assert receipt["cost_usd"] == pytest.approx(0.10)
    assert receipt["estimated"] is False
    assert receipt["cache_result"] == "miss"


@pytest.mark.asyncio
async def test_keep_warm_ok_receipt_falls_back_to_the_estimate_when_no_delta_lands(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A cumulative cost that never moves leaves today's estimate, with no ``cache_result``."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _keep_warm_context_file(bridge_dir, total_cost_usd=1.00)
    monkeypatch.setattr(
        claude_native_executor,
        "run_keep_warm_btw",
        lambda _: claude_bridge.KeepWarmBtwResult("ok", None),
    )
    monkeypatch.setattr(claude_native_executor, "fetch_model_pricing", _keep_warm_pricing)
    # Keep the settle poll's real-time cost at test scale.
    monkeypatch.setattr(claude_native_executor, "_KEEP_WARM_COST_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(claude_native_executor, "_KEEP_WARM_COST_POLL_TIMEOUT_S", 0.05)
    executor = ClaudeNativeExecutor(bridge_dir)

    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")

    assert receipt == {
        "attempt_id": "att-1",
        "outcome": "ok",
        "reason": None,
        "input_total": None,
        "cache_read": 37000,
        "cache_write": None,
        "cost_usd": pytest.approx(0.0111),
        "estimated": True,
    }


@pytest.mark.asyncio
async def test_keep_warm_measured_delta_without_an_estimate_carries_no_cache_result(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No priceable estimate: the real delta still lands, but no hit/miss is claimed."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _keep_warm_context_file(bridge_dir, total_cost_usd=1.00)

    def fake_run(bridge_dir_arg: Path) -> claude_bridge.KeepWarmBtwResult:
        _keep_warm_context_file(bridge_dir_arg, total_cost_usd=1.02)
        return claude_bridge.KeepWarmBtwResult("ok", None)

    monkeypatch.setattr(claude_native_executor, "run_keep_warm_btw", fake_run)
    monkeypatch.setattr(claude_native_executor, "_keep_warm_cost_usd", lambda *_: None)
    executor = ClaudeNativeExecutor(bridge_dir)

    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")

    assert receipt == {
        "attempt_id": "att-1",
        "outcome": "ok",
        "reason": None,
        "input_total": None,
        "cache_read": 37000,
        "cache_write": None,
        "cost_usd": pytest.approx(0.02),
        "estimated": False,
    }
    assert "cache_result" not in receipt


@pytest.mark.asyncio
async def test_keep_warm_slow_pricing_outlives_the_budget_and_reads_as_no_estimate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A catalog lookup slower than the remaining ping budget is cut off:
    the receipt still returns inside the budget with the measured delta and
    no estimate (hence no ``cache_result``)."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _keep_warm_context_file(bridge_dir, total_cost_usd=1.00)

    def fake_run(bridge_dir_arg: Path) -> claude_bridge.KeepWarmBtwResult:
        _keep_warm_context_file(bridge_dir_arg, total_cost_usd=1.02)
        return claude_bridge.KeepWarmBtwResult("ok", None)

    def slow_cost(model: str | None, cache_read: int | None) -> float:
        del model, cache_read
        time.sleep(5.0)  # outlives the remaining ping budget
        return 0.01

    monkeypatch.setattr(claude_native_executor, "run_keep_warm_btw", fake_run)
    monkeypatch.setattr(claude_native_executor, "_keep_warm_cost_usd", slow_cost)
    monkeypatch.setattr(claude_native_executor, "_KEEP_WARM_PING_BUDGET_S", 1.0, raising=False)
    executor = ClaudeNativeExecutor(bridge_dir)

    started = time.monotonic()
    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")
    elapsed = time.monotonic() - started

    assert elapsed < 3.0  # the 1 s budget, not the 5 s lookup, bounds the ping
    assert receipt == {
        "attempt_id": "att-1",
        "outcome": "ok",
        "reason": None,
        "input_total": None,
        "cache_read": 37000,
        "cache_write": None,
        "cost_usd": pytest.approx(0.02),
        "estimated": False,
    }
    assert "cache_result" not in receipt


@pytest.mark.asyncio
async def test_keep_warm_estimate_prices_the_model_read_under_the_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A model switch landing after the ping's exchange (here mid settle
    poll, before the off-lock estimate runs) must not reprice the ping: the
    estimate — and so the hit/miss call — follows the model snapshot read
    under the lock."""
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _keep_warm_context_file(bridge_dir, total_cost_usd=1.00)

    def fake_run(bridge_dir_arg: Path) -> claude_bridge.KeepWarmBtwResult:
        del bridge_dir_arg
        return claude_bridge.KeepWarmBtwResult("ok", None)

    def flipping_poll(bridge_dir_arg: Path, before: float, timeout_s: float) -> float | None:
        del before, timeout_s
        # The person switches to a far cheaper model right after the ping.
        _keep_warm_context_file(bridge_dir_arg, model="claude-cheap-1", total_cost_usd=1.02)
        return 1.02

    def fake_pricing(model: str) -> ModelPricing:
        return ModelPricing(
            input_per_token=3e-6,
            output_per_token=15e-6,
            cache_read_per_token=3e-7 if model == "claude-sonnet-4-6" else 3e-9,
            cache_write_per_token=3.75e-6,
        )

    monkeypatch.setattr(claude_native_executor, "run_keep_warm_btw", fake_run)
    monkeypatch.setattr(claude_native_executor, "_poll_total_cost_usd", flipping_poll)
    monkeypatch.setattr(claude_native_executor, "fetch_model_pricing", fake_pricing)
    executor = ClaudeNativeExecutor(bridge_dir)

    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")

    # Priced from the snapshot model: 37000 x $3e-7 = $0.0111, so the $0.02
    # delta is a hit; the switched model's $0.000111 estimate would read miss.
    assert receipt["cost_usd"] == pytest.approx(0.02)
    assert receipt["estimated"] is False
    assert receipt["cache_result"] == "hit"


@pytest.mark.asyncio
async def test_keep_warm_cost_window_excludes_an_injection_parked_behind_the_cost_poll(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    A real injection reaching ``_inject_lock`` while the ping's settle poll is
    running only acquires it after the poll read the ping's own +$0.02, so the
    injection's +$0.50 (written once it holds the lock) is not in the delta.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    _keep_warm_context_file(bridge_dir, total_cost_usd=1.00)

    def fake_run(bridge_dir_arg: Path) -> claude_bridge.KeepWarmBtwResult:
        del bridge_dir_arg
        # Claude Code settles the side question's cost a moment later — mid-poll.
        return claude_bridge.KeepWarmBtwResult("ok", None)

    real_poll = claude_native_executor._poll_total_cost_usd
    poll_started = threading.Event()
    finish_poll = threading.Event()
    order: list[str] = []

    def gated_poll(bridge_dir_arg: Path, before: float, timeout_s: float) -> float | None:
        del bridge_dir_arg
        poll_started.set()
        assert finish_poll.wait(timeout=5.0), "test must release the poll"
        after = real_poll(bridge_dir, before, timeout_s)
        order.append("poll")
        return after

    monkeypatch.setattr(claude_native_executor, "run_keep_warm_btw", fake_run)
    monkeypatch.setattr(claude_native_executor, "_poll_total_cost_usd", gated_poll)
    monkeypatch.setattr(claude_native_executor, "fetch_model_pricing", _keep_warm_pricing)
    executor = ClaudeNativeExecutor(bridge_dir)

    ping = asyncio.create_task(executor.keep_warm(attempt_id="att-1", family="claude"))
    assert await asyncio.to_thread(poll_started.wait, 5.0), "the settle poll must start"
    # The ping's own increment lands while the poll is still reading.
    _keep_warm_context_file(bridge_dir, total_cost_usd=1.02)

    async def injection() -> None:
        async with executor._inject_lock:
            order.append("injection")
            _keep_warm_context_file(bridge_dir, total_cost_usd=1.52)

    injection_task = asyncio.create_task(injection())
    for _ in range(10):
        await asyncio.sleep(0)  # the injection runs to the acquire and parks
    assert order == []  # still parked behind the ping's lock, poll not released yet
    finish_poll.set()

    receipt = await ping
    await injection_task

    assert order == ["poll", "injection"]
    assert receipt == {
        "attempt_id": "att-1",
        "outcome": "ok",
        "reason": None,
        "input_total": None,
        "cache_read": 37000,
        "cache_write": None,
        "cost_usd": pytest.approx(0.02),
        "estimated": False,
        "cache_result": "hit",
    }


@pytest.mark.asyncio
async def test_keep_warm_bridge_failure_reports_harness_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A tmux failure mid-sequence is one failed ping, and the lock is released."""

    def fail_run(bridge_dir_arg: Path) -> claude_bridge.KeepWarmBtwResult:
        del bridge_dir_arg
        raise RuntimeError("tmux command failed (rc=1): no server")

    monkeypatch.setattr(claude_native_executor, "run_keep_warm_btw", fail_run)
    executor = ClaudeNativeExecutor(tmp_path / "bridge")

    receipt = await executor.keep_warm(attempt_id="att-1", family="claude")

    assert not executor._inject_lock.locked()
    assert receipt == {
        "attempt_id": "att-1",
        "outcome": "failed",
        "reason": "harness_error",
        "input_total": None,
        "cache_read": None,
        "cache_write": None,
        "cost_usd": None,
        "estimated": False,
    }


@pytest.mark.asyncio
async def test_keep_warm_cancellation_drains_the_worker_before_releasing_the_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    Cancel mid-sequence: the inject lock stays held until the bridge
    worker has finished, and the cancel flag refuses the worker's next key.
    """
    started = threading.Event()
    release = threading.Event()
    observed: dict[str, Any] = {}

    def fake_run(bridge_dir_arg: Path) -> claude_bridge.KeepWarmBtwResult:
        del bridge_dir_arg
        started.set()
        assert release.wait(timeout=5.0), "test must release the worker"
        # The bridge runs this check before every key it sends.
        try:
            claude_bridge._check_injection_cancelled()
        except claude_bridge.ClaudeInjectionCancelled:
            observed["key_blocked"] = True
        return claude_bridge.KeepWarmBtwResult("ok", None)

    monkeypatch.setattr(claude_native_executor, "run_keep_warm_btw", fake_run)
    executor = ClaudeNativeExecutor(tmp_path / "bridge")
    task = asyncio.create_task(executor.keep_warm(attempt_id="att-1", family="claude"))
    assert await asyncio.to_thread(started.wait, 5.0), "the worker must start"

    task.cancel()
    for _ in range(10):
        await asyncio.sleep(0)
    assert not task.done(), "the ping must wait for the worker, not abandon it"
    assert executor._inject_lock.locked()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed["key_blocked"] is True
    assert not executor._inject_lock.locked()
