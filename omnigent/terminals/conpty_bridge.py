"""Dispatch terminal attach to the instance's backend, preserving the wire protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable

from fastapi import WebSocket, WebSocketDisconnect

from omnigent.inner.async_utils import run_sync_on_thread
from omnigent.inner.terminal import TerminalInstance
from omnigent.terminals.conpty import ConPtyTerminalInstance
from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket
from omnigent.terminals.ws_common import (
    WS_CLOSE_TERMINAL_NOT_FOUND,
    _coalesce_limit_after_input,
    _forward_terminal_to_ws,
)


async def bridge_terminal_to_websocket(
    websocket: WebSocket,
    instance: TerminalInstance,
    *,
    read_only: bool = False,
    on_client_interaction: Callable[[], None] | None = None,
    on_input: Callable[[], None] | None = None,
) -> None:
    if isinstance(instance, ConPtyTerminalInstance):
        await bridge_conpty_to_websocket(
            websocket,
            instance,
            read_only=read_only,
            on_client_interaction=on_client_interaction,
            on_input=on_input,
        )
    elif instance.backend == "tmux":
        await bridge_tmux_control_to_websocket(
            websocket,
            socket_path=str(instance.socket_path),
            tmux_target=instance.tmux_target,
            read_only=read_only,
            on_client_interaction=on_client_interaction,
            on_input=on_input,
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
) -> None:
    seed = instance.attach_snapshot()
    last_input: float | None = None

    def interaction() -> None:
        if on_client_interaction is not None:
            on_client_interaction()

    async def forward() -> None:
        await websocket.send_json(
            {
                "type": "pty_info",
                "windows_pty": {"backend": "conpty", "build": instance.windows_build},
            }
        )
        if seed.output:
            await websocket.send_bytes(seed.output)
        await run_sync_on_thread(instance.request_repaint)
        await _forward_terminal_to_ws(
            websocket,
            seed.queue,
            max_coalesce_bytes=lambda: _coalesce_limit_after_input(last_input),
        )

    async def receive() -> None:
        nonlocal last_input
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data, text = message.get("bytes"), message.get("text")
            if read_only:
                continue
            if data:
                interaction()
                if on_input is not None:
                    on_input()
                last_input = time.monotonic()
                await run_sync_on_thread(instance.write_input, data)
            elif text:
                try:
                    frame = json.loads(text)
                except (ValueError, TypeError):
                    continue
                if not isinstance(frame, dict) or frame.get("type") != "resize":
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
                await run_sync_on_thread(instance.resize, cols, rows)

    interaction()
    forwarder = asyncio.create_task(forward())
    receiver = asyncio.create_task(receive())
    overflow = asyncio.create_task(seed.queue.overflow.wait())
    tasks = {forwarder, receiver, overflow}
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if not task.cancelled():
                task.result()
    except (WebSocketDisconnect, OSError, RuntimeError):
        pass
    finally:
        instance.unsubscribe(seed.queue)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        interaction()
        code = (
            1013
            if seed.queue.overflow.is_set()
            else WS_CLOSE_TERMINAL_NOT_FOUND
            if instance._exit_done.is_set()
            else 1000
        )
        with contextlib.suppress(RuntimeError, WebSocketDisconnect):
            await websocket.close(code=code)
