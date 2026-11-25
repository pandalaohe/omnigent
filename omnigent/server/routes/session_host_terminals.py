"""Browser control and session-bound agent relay for linked host terminals."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import Field

from omnigent.entities.host_terminal import HostTerminalCreate
from omnigent.runner.identity import RUNNER_TUNNEL_TOKEN_HEADER, token_bound_runner_id
from omnigent.server.auth import effective_admin
from omnigent.server.linked_host_terminals import LinkedHostTerminals
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._terminal_access import require_terminal_browser_session
from omnigent.server.terminal_refusals import TerminalAccessDenied, TerminalRefusalRoute


class SessionHostTerminalCreate(HostTerminalCreate):
    host_id: str = Field(min_length=1)


def create_session_host_terminals_router(service: LinkedHostTerminals) -> APIRouter:
    router = APIRouter(route_class=TerminalRefusalRoute)

    async def authorize(request: Request, sid: str, *, agent: bool = False):
        conv = await service.conversation(sid)
        token = request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER)
        if agent or token:
            if not token or not conv.runner_id or token_bound_runner_id(token) != conv.runner_id:
                raise TerminalAccessDenied(
                    "runner_credential", "Only this session's bound runner may use this route"
                )
            user = await service.agent_owner(conv)
        else:
            user = require_user(request, service.auth)
            require_terminal_browser_session(request, service.auth)
        if not await service.owns(user, conv):
            raise TerminalAccessDenied(
                "not_owner", "Only the session owner may use linked terminals"
            )
        return user, conv

    @router.get("/sessions/{sid}/host-terminals")
    async def list_terminals(request: Request, sid: str) -> dict[str, Any]:
        if request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER):
            user, _ = await authorize(request, sid, agent=True)
            return await service.list(sid, user)
        conv = await service.conversation(sid)
        user = require_user(request, service.auth)
        if not await service.owns(user, conv):
            return {"terminals": [], "complete": True}
        await authorize(request, sid)
        return await service.list(sid, user)

    @router.post("/sessions/{sid}/host-terminals", status_code=201)
    async def create(request: Request, sid: str, body: SessionHostTerminalCreate) -> Any:
        require_terminal_browser_session(request, service.auth)
        user, conv = await authorize(request, sid)
        if conv.archived:
            raise HTTPException(409, "Unarchive the session before opening a terminal")
        _, conn = await service.target(body.host_id, user)
        if body.elevated:
            if service.permissions is not None and not effective_admin(
                user is not None and await asyncio.to_thread(service.permissions.is_admin, user)
            ):
                raise TerminalAccessDenied(
                    "admin_not_allowed", "Admin terminals require an administrator"
                )
            if "terminal_elevated_v1" not in conn.hello.capabilities:
                raise HTTPException(409, "Admin terminals are not enabled on this host")
        terminal = await service.call(
            body.host_id,
            user,
            "POST",
            "/v1/host-terminals",
            launch=True,
            body={
                **body.model_dump(exclude={"host_id"}, exclude_none=True),
                "linked_session_id": sid,
                "session_key": secrets.token_hex(16),
            },
        )
        service.changed(sid)
        return {**terminal, "host_id": body.host_id, "host_name": conn.hello.name}

    @router.patch("/sessions/{sid}/host-terminals/{host_id}/{tid}")
    async def set_tier(request: Request, sid: str, host_id: str, tid: str) -> Any:
        require_terminal_browser_session(request, service.auth)
        user, _ = await authorize(request, sid)
        await service.resource(sid, host_id, tid, user)
        body = await request.json()
        if not isinstance(body, dict) or body.get("agent_tier") not in (
            "off",
            "read",
            "read_write",
        ):
            raise HTTPException(400, "Invalid agent tier")
        if not isinstance(body.get("instance_id"), str):
            raise HTTPException(400, "instance_id is required")
        from omnigent.server.terminal_agent_access import (
            settle_terminal_access_switch,
            terminal_access_lock,
        )

        async with terminal_access_lock(sid, tid, body["instance_id"]):
            resource = await service.call(
                host_id, user, "PATCH", f"/v1/host-terminals/{quote(tid, safe='')}", body=body
            )
            await settle_terminal_access_switch(
                sid, tid, body["instance_id"], body["agent_tier"], service.store
            )
        service.changed(sid)
        return resource

    @router.delete("/sessions/{sid}/host-terminals/{host_id}/{tid}", status_code=204)
    async def delete(request: Request, sid: str, host_id: str, tid: str) -> Response:
        require_terminal_browser_session(request, service.auth)
        user, _ = await authorize(request, sid)
        resource = await service.resource(sid, host_id, tid, user)
        await service.call(host_id, user, "DELETE", f"/v1/host-terminals/{quote(tid, safe='')}")
        from omnigent.server.terminal_agent_access import end_terminal_access_requests

        await end_terminal_access_requests(sid, tid, resource.get("instance_id"), service.store)
        service.changed(sid)
        return Response(status_code=204)

    @router.post("/sessions/{sid}/host-terminals/{host_id}/{tid}/agent/{operation}")
    async def agent_operation(
        request: Request, sid: str, host_id: str, tid: str, operation: str
    ) -> Any:
        user, conv = await authorize(request, sid, agent=True)
        if conv.archived:
            raise HTTPException(409, "Session is archived")
        if operation not in ("read", "send", "close", "request_access"):
            raise HTTPException(404, "Unknown terminal operation")
        resource = await service.resource(sid, host_id, tid, user)
        if operation == "request_access":
            body = await request.json()
            tier = body.get("tier", "read_write")
            return {
                "status": "granted"
                if (
                    resource.get("instance_id") == body.get("instance_id")
                    and tier in ("read", "read_write")
                    and resource.get("agent_tier")
                    in (("read", "read_write") if tier == "read" else ("read_write",))
                )
                else "rejected"
            }
        body = await request.json()
        payload = {key: body[key] for key in ("text", "keys", "scrollback") if key in body}
        payload["session_id"] = sid
        result = await service.call(
            host_id,
            user,
            "POST",
            f"/v1/host-terminals/{quote(tid, safe='')}/agent/{operation}",
            body=payload,
        )
        if operation == "close":
            service.changed(sid)
        return result

    return router
