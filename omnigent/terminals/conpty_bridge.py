"""Dispatch terminal attach to the instance's backend, preserving the wire protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import WebSocket, WebSocketDisconnect

from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket
from omnigent.terminals.host_clipboard import HostClipboard
from omnigent.terminals.ws_common import (
    WS_CLOSE_TERMINAL_NOT_FOUND,
    _coalesce_limit_after_input,
    _forward_terminal_to_ws,
    make_terminal_pong_frame,
    terminal_replay_params,
)

if TYPE_CHECKING:
    from omnigent.inner.terminal import TerminalInstance
    from omnigent.terminals.conpty import ConPtyTerminalInstance

_logger = logging.getLogger(__name__)
_REPLAY_WINDOW_BYTES = 256 * 1024
_REPLAY_EXIT_ACK_GRACE_S = 5.0
_INPUT_QUEUE_BYTES = 1024 * 1024
_INPUT_QUEUE_FRAMES = 256


async def bridge_terminal_to_websocket(
    websocket: WebSocket,
    instance: TerminalInstance,
    *,
    read_only: bool = False,
    browser_resize: bool = False,
    on_client_interaction: Callable[[], None] | None = None,
    on_input: Callable[[], None] | None = None,
) -> None:
    if instance.backend == "tmux":
        await bridge_tmux_control_to_websocket(
            websocket,
            socket_path=str(instance.socket_path),
            tmux_target=instance.tmux_target,
            read_only=read_only,
            browser_resize=browser_resize,
            on_client_interaction=on_client_interaction,
            on_input=on_input,
        )
    elif instance.backend == "conpty":
        from omnigent.terminals.conpty import ConPtyTerminalInstance

        if not isinstance(instance, ConPtyTerminalInstance):
            raise ValueError("invalid ConPTY terminal instance")
        await bridge_conpty_to_websocket(
            websocket,
            instance,
            read_only=read_only,
            on_client_interaction=on_client_interaction,
            on_input=on_input,
            replay=terminal_replay_params(websocket.query_params),
        )
    else:
        raise ValueError("unsupported terminal backend")


async def bridge_conpty_to_websocket(
    websocket: WebSocket,
    instance: ConPtyTerminalInstance,
    *,
    read_only: bool = False,
    on_client_interaction: Callable[[], None] | None = None,
    on_input: Callable[[], None] | None = None,
    replay: dict[str, str] | None = None,
) -> None:
    seed = None if replay else instance.attach_snapshot()
    last_input: float | None = None
    send_lock = asyncio.Lock()
    host_clipboard = HostClipboard(websocket, send_lock, read_only=read_only)
    received_ack = asyncio.Event()
    sent = acknowledged = 0
    input_queue: asyncio.Queue[bytes | tuple[int, int] | None] = asyncio.Queue(_INPUT_QUEUE_FRAMES)
    queued_input_bytes = 0
    input_overflow = False

    def interaction() -> None:
        if on_client_interaction is not None:
            on_client_interaction()

    async def replay_from(offset: int, through: int, prelude: bytes | None) -> None:
        nonlocal sent, acknowledged
        sent = acknowledged = offset
        async with send_lock:
            await websocket.send_json(
                {
                    "type": "replay_meta",
                    "epoch": instance.instance_id,
                    "offset": offset,
                    "through": through,
                    "prelude": None if prelude is None else prelude.decode("ascii"),
                }
            )
        if prelude is not None and not instance._exit_done.is_set():
            await run_sync_on_thread(instance.request_repaint)

    async def forward_replay(request: dict[str, str]) -> None:
        nonlocal sent
        loop = asyncio.get_running_loop()
        exit_deadline: float | None = None

        async def wait_for_ack() -> bool:
            nonlocal exit_deadline
            received_ack.clear()
            while True:
                if exit_deadline is None and instance._exit_done.is_set():
                    exit_deadline = loop.time() + _REPLAY_EXIT_ACK_GRACE_S
                timeout = (
                    _REPLAY_EXIT_ACK_GRACE_S
                    if exit_deadline is None
                    else exit_deadline - loop.time()
                )
                if timeout <= 0:
                    return False
                try:
                    await asyncio.wait_for(received_ack.wait(), timeout)
                    return True
                except TimeoutError:
                    if exit_deadline is not None:
                        return False

        raw_offset = request.get("since_offset")
        await replay_from(
            *instance.replay_start(
                request.get("replay_epoch"), int(raw_offset) if raw_offset is not None else None
            )
        )
        await host_clipboard.advertise()
        while True:
            room = _REPLAY_WINDOW_BYTES - (sent - acknowledged)
            if room <= 0:
                if not await wait_for_ack():
                    return
                continue
            data = instance.read_replay(sent, min(room, _coalesce_limit_after_input(last_input)))
            if data is None:
                # Finish queued writes before resetting xterm after the ring wraps.
                if sent != acknowledged:
                    if not await wait_for_ack():
                        return
                    continue
                await replay_from(*instance.replay_start(None, None))
            elif data:
                sent += len(data)
                async with send_lock:
                    await websocket.send_bytes(data)
            elif not await instance.wait_for_output(sent):
                # Keep the process-exit close ordered after the final parsed output.
                while sent != acknowledged and await wait_for_ack():
                    pass
                return

    async def forward() -> None:
        async with send_lock:
            await websocket.send_json(
                {
                    "type": "pty_info",
                    "windows_pty": {"backend": "conpty", "build": instance.windows_build},
                }
            )
            if seed is not None and seed.output:
                await websocket.send_bytes(seed.output)
        if seed is None:
            assert replay is not None
            await forward_replay(replay)
            return
        await host_clipboard.advertise()
        await run_sync_on_thread(instance.request_repaint)
        await _forward_terminal_to_ws(
            websocket,
            seed.queue,
            max_coalesce_bytes=lambda: _coalesce_limit_after_input(last_input),
            send_lock=send_lock,
        )

    async def apply_input(frame: bytes | tuple[int, int]) -> None:
        if not isinstance(frame, bytes):
            await run_sync_on_thread(instance.resize, *frame, force=not replay)
            return
        try:
            await run_sync_on_thread(instance.write_input, frame)
        except OSError as exc:
            # Log the OS error without the input bytes.
            _logger.warning(
                "ConPTY input write failed (%s): %s", type(exc).__name__, exc.strerror or exc
            )
            raise

    async def apply_queued_input() -> None:
        nonlocal queued_input_bytes
        while True:
            frame = await input_queue.get()
            # Drain this socket's accepted backlog before another socket writes.
            async with instance.attach_input_lock:
                while frame is not None:
                    if isinstance(frame, bytes):
                        queued_input_bytes -= len(frame)
                    await apply_input(frame)
                    if input_queue.empty():
                        break
                    frame = input_queue.get_nowait()
                if frame is None:
                    return

    async def submit_input(frame: bytes | tuple[int, int]) -> bool:
        nonlocal queued_input_bytes, input_overflow
        if not replay:
            async with instance.attach_input_lock:
                await apply_input(frame)
            return True
        # A slow PTY write must not prevent this socket from receiving output ACKs.
        size = len(frame) if isinstance(frame, bytes) else 0
        # Allow one large paste on an empty queue, as on the legacy transport.
        if input_queue.full() or (
            size and queued_input_bytes and queued_input_bytes + size > _INPUT_QUEUE_BYTES
        ):
            input_overflow = True
            return False
        queued_input_bytes += size
        input_queue.put_nowait(frame)
        return True

    async def receive() -> None:
        nonlocal last_input, acknowledged
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data, text = message.get("bytes"), message.get("text")
            if data and not read_only:
                interaction()
                if on_input is not None:
                    on_input()
                last_input = time.monotonic()
                if not await submit_input(data):
                    return
            elif text:
                try:
                    frame = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if not isinstance(frame, dict):
                    continue
                if replay and frame.get("type") == "output_ack":
                    offset = frame.get("offset")
                    if type(offset) is int and acknowledged < offset <= sent:
                        acknowledged = offset
                        received_ack.set()
                    continue
                pong = make_terminal_pong_frame(frame)
                if pong is not None:
                    async with send_lock:
                        await websocket.send_text(pong)
                    continue
                if frame.get("type") == "host-clipboard-read":
                    await host_clipboard.handle(frame)
                    continue
                if read_only or frame.get("type") != "resize":
                    continue
                cols, rows = frame.get("cols"), frame.get("rows")
                if not (
                    type(cols) is int
                    and type(rows) is int
                    and 1 <= cols <= 32767
                    and 1 <= rows <= 32767
                ):
                    continue
                interaction()
                if not await submit_input((cols, rows)):
                    return

    interaction()
    forwarder = asyncio.create_task(forward())
    receiver = asyncio.create_task(receive())
    tasks: set[asyncio.Task[object]] = {forwarder, receiver}
    writer: asyncio.Task[None] | None = None
    if seed is not None:
        tasks.add(asyncio.create_task(seed.queue.overflow.wait()))
    elif not read_only:
        writer = asyncio.create_task(apply_queued_input())
        tasks.add(writer)
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled():
                task.result()
    except (WebSocketDisconnect, OSError, RuntimeError):
        pass
    finally:
        if seed is not None:
            instance.unsubscribe(seed.queue)
        socket_tasks = tasks - {writer} if writer is not None else tasks
        for task in socket_tasks:
            task.cancel()
        await asyncio.gather(*socket_tasks, return_exceptions=True)
        await host_clipboard.close()
        interaction()
        code, reason = 1000, ""
        if seed is not None and seed.queue.overflow.is_set():
            code = 1013
        elif instance._exit_done.is_set():
            code, reason = WS_CLOSE_TERMINAL_NOT_FOUND, "terminal process exited"
        elif input_overflow:
            code, reason = 1008, "terminal input backlog full"
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=code, reason=reason)
        if writer is not None and not writer.done():
            # Delivery is independent of the socket after the bridge accepted it.
            finish = asyncio.create_task(input_queue.put(None))
            try:
                await asyncio.wait({finish, writer}, return_when=asyncio.FIRST_COMPLETED)
                await asyncio.gather(writer, return_exceptions=True)
            finally:
                finish.cancel()
                await asyncio.gather(finish, return_exceptions=True)
