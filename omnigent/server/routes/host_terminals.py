"""Owner-only host terminal REST and relay attach routes."""

from __future__ import annotations

import asyncio
import contextlib
import secrets

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket

from omnigent.entities.host_terminal import (
    HostTerminalCreate,
    HostTerminalList,
    HostTerminalResource,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_HOST_TERMINALS
from omnigent.runner.routing import RunnerRouter
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
from omnigent.server._runner_ws_tunnel import _TunneledWSConn
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection
from omnigent.server.host_terminals import HostTerminalService
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import host_absent_error, resolve_host_owner
from omnigent.server.routes.terminal_attach import _RunnerWSClosed, _shuttle_ws_frames
from omnigent.stores.host_store import Host
from omnigent.stores.permission_store import PermissionStore


def create_host_terminals_router(
    service: HostTerminalService,
    *,
    auth_provider: AuthProvider | None,
    runner_router: RunnerRouter,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    router = APIRouter()

    async def authorize(connection: Request | WebSocket) -> tuple[Host, HostConnection]:
        user = require_user(connection, auth_provider)
        try:
            host = await asyncio.to_thread(
                resolve_host_owner,
                user_id=user,
                host_id=connection.path_params["host_id"],
                host_store=service.host_store,
            )
        except HTTPException as exc:
            code = ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.FORBIDDEN
            raise OmnigentError(str(exc.detail), code=code) from exc
        if runner_router.host_is_on_another_replica(host.host_id):
            raise OmnigentError("host is on another replica", code=ErrorCode.WRONG_REPLICA)
        conn = service.hosts.get(host.host_id)
        if conn is None:
            raise host_absent_error(host)
        if CAP_HOST_TERMINALS not in conn.hello.capabilities:
            raise OmnigentError("host terminals are not supported", code=ErrorCode.CONFLICT)
        return host, conn

    async def proxy(
        runner_id: str, method: str, path: str, *, payload: dict[str, object] | None = None
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                transport=WSTunnelTransport(service.tunnels, runner_id), base_url="http://runner"
            ) as client:
                response = await client.request(method, path, json=payload)
        except httpx.TransportError as exc:
            raise OmnigentError(
                "terminal runner is reconnecting", code=ErrorCode.RUNNER_UNAVAILABLE
            ) from exc
        if response.status_code >= 400:
            # Preserve only the expected public status; never forward runner internals.
            code = (
                ErrorCode.NOT_FOUND
                if response.status_code == 404
                else ErrorCode.RUNNER_UNAVAILABLE
                if response.status_code >= 500
                else ErrorCode.CONFLICT
            )
            raise OmnigentError(
                "terminal not found"
                if response.status_code == 404
                else "terminal operation failed",
                code=code,
            )
        return response

    def with_attach_path(host_id: str, value: HostTerminalResource) -> HostTerminalResource:
        return value.model_copy(
            update={"attach_path": f"/v1/hosts/{host_id}/terminals/{value.terminal_id}/attach"}
        )

    @router.post("/hosts/{host_id}/terminals", status_code=201)
    async def create(request: Request, body: HostTerminalCreate) -> HostTerminalResource:
        host, conn = await authorize(request)
        if body.elevated:
            if permission_store is not None and not await asyncio.to_thread(
                permission_store.is_admin, host.user_id
            ):
                raise OmnigentError("admin access required", code=ErrorCode.FORBIDDEN)
            if "terminal_elevated_v1" not in conn.hello.capabilities:
                raise OmnigentError("admin terminals are not enabled", code=ErrorCode.CONFLICT)
        runner_id = await service.runner(host, conn, launch=True)
        assert runner_id is not None
        response = await proxy(
            runner_id,
            "POST",
            "/v1/host-terminals",
            payload={"elevated": body.elevated, "session_key": secrets.token_hex(16)},
        )
        return with_attach_path(host.host_id, HostTerminalResource.model_validate(response.json()))

    @router.get("/hosts/{host_id}/terminals")
    async def list_terminals(request: Request) -> HostTerminalList:
        host, conn = await authorize(request)
        runner_id = await service.runner(host, conn, launch=False)
        if runner_id is None:
            return HostTerminalList(terminals=[])
        response = await proxy(runner_id, "GET", "/v1/host-terminals")
        page = HostTerminalList.model_validate(response.json())
        return HostTerminalList(
            terminals=[with_attach_path(host.host_id, terminal) for terminal in page.terminals]
        )

    @router.delete("/hosts/{host_id}/terminals/{terminal_id}", status_code=204)
    async def delete(request: Request, terminal_id: str) -> Response:
        host, conn = await authorize(request)
        runner_id = await service.runner(host, conn, launch=False)
        if runner_id is None:
            raise OmnigentError("terminal not found", code=ErrorCode.NOT_FOUND)
        await proxy(runner_id, "DELETE", f"/v1/host-terminals/{terminal_id}")
        return Response(status_code=204)

    @router.websocket("/hosts/{host_id}/terminals/{terminal_id}/attach")
    async def attach(websocket: WebSocket, terminal_id: str) -> None:
        try:
            host, conn = await authorize(websocket)
            if websocket.query_params.get("read_only", "false").lower() != "false":
                raise OmnigentError(
                    "host terminal watchers are not supported", code=ErrorCode.FORBIDDEN
                )
            runner_id = await service.runner(host, conn, launch=False)
            session = service.tunnels.get(runner_id) if runner_id is not None else None
            if session is None:
                raise OmnigentError("terminal not found", code=ErrorCode.NOT_FOUND)
        except OmnigentError as exc:
            await websocket.accept()
            code = {
                ErrorCode.WRONG_REPLICA: 4400,
                ErrorCode.NOT_FOUND: 4404,
                ErrorCode.FORBIDDEN: 4403,
                ErrorCode.UNAUTHORIZED: 4403,
            }.get(exc.code, 4500)
            await websocket.close(code=code, reason=str(exc))
            return
        await websocket.accept()
        try:
            async with _TunneledWSConn(
                registry=service.tunnels,
                session=session,
                runner_path=f"/v1/host-terminals/{terminal_id}/attach",
            ) as runner_ws:
                await _shuttle_ws_frames(websocket, runner_ws)
        except _RunnerWSClosed as exc:
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=exc.code or 4500, reason=exc.reason or "")
        except (ConnectionError, RuntimeError):
            with contextlib.suppress(RuntimeError):
                await websocket.close(code=4500, reason="terminal runner is reconnecting")

    return router
