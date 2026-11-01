"""A terminal runner has no conversation, harness, tools or direct listener."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

from fastapi import FastAPI, HTTPException, Response, WebSocket

from omnigent._platform import installed_interactive_shells
from omnigent.entities.host_terminal import (
    HostTerminalList,
    HostTerminalResource,
    RunnerHostTerminalCreate,
)
from omnigent.inner.terminal import TerminalInstance
from omnigent.native.native_coding_agents import native_shell_terminal_specs
from omnigent.terminals import TerminalRegistry
from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket

_REGISTRY_KEY = "host-terminals"
_logger = logging.getLogger(__name__)


def create_host_terminal_app(
    *,
    home: Path,
    shells: list[str] | None = None,
    terminal_registry: TerminalRegistry | None = None,
) -> FastAPI:
    registry = terminal_registry if terminal_registry is not None else TerminalRegistry()
    created: dict[str, int] = {}
    idle_since = time.monotonic()
    mutations = 0
    shell = (shells or installed_interactive_shells())[0]
    base_spec = native_shell_terminal_specs([shell])[shell]
    if base_spec.os_env is None or isinstance(base_spec.os_env, str):
        raise RuntimeError("host shell requires a caller-process environment")
    spec = replace(base_spec, args=["-l"], os_env=replace(base_spec.os_env, cwd=str(home)))

    def entries():
        return registry.list_for_conversation(_REGISTRY_KEY)

    async def close(key: str, instance: TerminalInstance) -> None:
        nonlocal idle_since, mutations
        mutations += 1
        try:
            closed = await registry.close(_REGISTRY_KEY, "shell", key, expected=instance)
            if not closed and registry.get(_REGISTRY_KEY, "shell", key) is instance:
                raise HTTPException(503, "terminal close is pending")
            created.pop(key, None)
        finally:
            mutations -= 1
            if not entries() and not mutations:
                idle_since = time.monotonic()

    async def reap_exited() -> None:
        while True:
            await asyncio.sleep(2)
            for entry in entries():
                try:
                    if entry.session_key not in created or not await entry.instance.is_alive():
                        await close(entry.session_key, entry.instance)
                except (HTTPException, OSError, RuntimeError):
                    _logger.warning("Host terminal cleanup pending; retrying", exc_info=True)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        reaper = asyncio.create_task(reap_exited(), name="host-terminal-exit-reaper")
        try:
            yield
        finally:
            reaper.cancel()
            await asyncio.gather(reaper, return_exceptions=True)
            await registry.shutdown()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.runner_purpose = "host_terminals"
    app.state.has_active_work = lambda: bool(entries()) or mutations > 0
    app.state.host_terminal_idle_since = lambda: idle_since
    app.state.terminal_registry = registry

    def resource(key: str, instance: TerminalInstance) -> HostTerminalResource:
        return HostTerminalResource(
            terminal_id=key,
            elevated=instance.elevated,
            created_at=created[key],
            attach_path=f"/v1/host-terminals/{key}/attach",
        )

    @app.get("/v1/host-terminals")
    async def list_terminals() -> HostTerminalList:
        return HostTerminalList(
            terminals=[
                resource(entry.session_key, entry.instance)
                for entry in entries()
                if entry.session_key in created
            ]
        )

    @app.post("/v1/host-terminals", status_code=201)
    async def create_terminal(body: RunnerHostTerminalCreate) -> HostTerminalResource:
        nonlocal mutations, idle_since
        mutations += 1
        try:
            try:
                instance = await registry.launch(
                    _REGISTRY_KEY, "shell", body.session_key, spec, elevated=body.elevated
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            created.setdefault(body.session_key, int(time.time()))
        finally:
            mutations -= 1
            if not entries() and not mutations:
                idle_since = time.monotonic()
        return resource(body.session_key, instance)

    @app.delete("/v1/host-terminals/{terminal_id}", status_code=204)
    async def delete_terminal(terminal_id: str) -> Response:
        instance = registry.get(_REGISTRY_KEY, "shell", terminal_id)
        if instance is None or terminal_id not in created:
            raise HTTPException(404, "terminal not found")
        await close(terminal_id, instance)
        return Response(status_code=204)

    @app.websocket("/v1/host-terminals/{terminal_id}/attach")
    async def attach(websocket: WebSocket, terminal_id: str) -> None:
        await websocket.accept()
        instance = registry.get(_REGISTRY_KEY, "shell", terminal_id)
        if instance is None or terminal_id not in created or not await instance.is_alive():
            await websocket.close(code=4404, reason="terminal not found")
            return
        await bridge_tmux_control_to_websocket(
            websocket,
            socket_path=str(instance.socket_path),
            tmux_target=instance.tmux_target,
            read_only=False,
            on_input=instance.note_input,
        )

    return app
