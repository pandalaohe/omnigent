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

from omnigent._platform import installed_interactive_shells, interactive_shell_args
from omnigent.entities.host_terminal import (
    HostTerminalAgentClose,
    HostTerminalAgentRead,
    HostTerminalAgentSend,
    HostTerminalList,
    HostTerminalResource,
    HostTerminalTierUpdate,
    RunnerHostTerminalCreate,
)
from omnigent.inner.terminal import TerminalInstance
from omnigent.native.native_coding_agents import native_shell_terminal_specs
from omnigent.terminals import TerminalRegistry
from omnigent.terminals.conpty_bridge import bridge_terminal_to_websocket
from omnigent.terminals.elevation import AGENT_ACCESS_REQUIRED

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
    if shells is not None and not shells:
        raise ValueError("host shell inventory is empty")
    shell = (shells or installed_interactive_shells())[0]
    base_spec = native_shell_terminal_specs([shell])[shell]
    if base_spec.os_env is None or isinstance(base_spec.os_env, str):
        raise RuntimeError("host shell requires a caller-process environment")
    spec = replace(
        base_spec,
        args=interactive_shell_args(shell, standalone=True),
        os_env=replace(base_spec.os_env, cwd=str(home)),
    )

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
            linked_session_id=instance.linked_session_id,
            agent_tier=instance.agent_tier,
            instance_id=instance.instance_id,
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
                    _REGISTRY_KEY,
                    "shell",
                    body.session_key,
                    spec,
                    elevated=body.elevated,
                    linked_session_id=body.linked_session_id,
                    agent_tier=body.agent_tier,
                )
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc
            created.setdefault(body.session_key, int(time.time()))
        finally:
            mutations -= 1
            if not entries() and not mutations:
                idle_since = time.monotonic()
        return resource(body.session_key, instance)

    def agent_operation(
        terminal_id: str,
        body: HostTerminalAgentRead | HostTerminalAgentSend | HostTerminalAgentClose,
    ) -> dict[str, str | int]:
        instance = registry.get(_REGISTRY_KEY, "shell", terminal_id)
        lock = registry.get_instance_lock(_REGISTRY_KEY, "shell", terminal_id)
        if instance is None or terminal_id not in created or lock is None:
            raise HTTPException(404, "terminal not found")
        with lock:
            if instance.linked_session_id != body.session_id:
                raise HTTPException(403, "terminal is not linked to this session")
            try:
                registry.require_agent_access(
                    _REGISTRY_KEY,
                    "shell",
                    terminal_id,
                    instance,
                    need="read" if isinstance(body, HostTerminalAgentRead) else "read_write",
                )
            except ValueError as exc:
                status = 403 if str(exc) == AGENT_ACCESS_REQUIRED else 409
                raise HTTPException(status, str(exc)) from exc
            if isinstance(body, HostTerminalAgentSend):
                return asyncio.run(instance.send(body.text, keys=body.keys))
            if isinstance(body, HostTerminalAgentClose):
                asyncio.run(close(terminal_id, instance))
                return {"status": "closed"}
            return asyncio.run(instance.read(scrollback=body.scrollback))

    @app.post("/v1/host-terminals/{terminal_id}/agent/close")
    def agent_close(terminal_id: str, body: HostTerminalAgentClose) -> dict[str, str | int]:
        return agent_operation(terminal_id, body)

    @app.patch("/v1/host-terminals/{terminal_id}")
    def set_agent_tier(terminal_id: str, body: HostTerminalTierUpdate) -> HostTerminalResource:
        try:
            instance = registry.set_agent_tier(
                _REGISTRY_KEY, "shell", terminal_id, body.instance_id, body.agent_tier
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return resource(terminal_id, instance)

    @app.post("/v1/host-terminals/{terminal_id}/agent/read")
    def agent_read(terminal_id: str, body: HostTerminalAgentRead) -> dict[str, str | int]:
        return agent_operation(terminal_id, body)

    @app.post("/v1/host-terminals/{terminal_id}/agent/send")
    def agent_send(terminal_id: str, body: HostTerminalAgentSend) -> dict[str, str | int]:
        return agent_operation(terminal_id, body)

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
        await bridge_terminal_to_websocket(
            websocket,
            instance,
            read_only=False,
            browser_resize=websocket.query_params.get("browser_resize") == "true",
            on_input=instance.note_input,
        )

    return app
