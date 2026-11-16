"""Session-linked terminals discovered from their owning host runners."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import CAP_HOST_TERMINAL_LINKS
from omnigent.server.auth import AuthProvider
from omnigent.server.host_terminals import HostTerminalService
from omnigent.server.routes._auth_helpers import is_session_owner
from omnigent.server.routes._host_launch import resolve_host_owner
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

logger = logging.getLogger(__name__)


class LinkedHostTerminals:
    def __init__(
        self,
        terminals: HostTerminalService,
        store: ConversationStore,
        *,
        auth_provider: AuthProvider | None,
        permissions: PermissionStore | None,
    ) -> None:
        self.terminals = terminals
        self.store = store
        self.auth = auth_provider
        self.permissions = permissions

    async def conversation(self, session_id: str) -> Conversation:
        conv = await asyncio.to_thread(self.store.get_conversation, session_id)
        if conv is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        return conv

    async def agent_owner(self, conv: Conversation) -> str | None:
        if self.permissions is None or self.auth is None:
            return None
        grants = await asyncio.to_thread(
            self.permissions.list_for_sessions, [conv.root_conversation_id or conv.id]
        )
        owners = {grant.user_id for page in grants.values() for grant in page if grant.level >= 4}
        if len(owners) != 1:
            raise OmnigentError("Session owner is unavailable", code=ErrorCode.FORBIDDEN)
        return owners.pop()

    async def owns(self, user: str | None, conv: Conversation) -> bool:
        return await asyncio.to_thread(
            is_session_owner,
            user,
            conv,
            auth_provider=self.auth,
            permission_store=self.permissions,
        )

    async def target(self, host_id: str, user: str | None):
        host = await asyncio.to_thread(
            resolve_host_owner, user_id=user, host_id=host_id, host_store=self.terminals.host_store
        )
        conn = self.terminals.hosts.get(host_id)
        if conn is None:
            raise OmnigentError("Terminal host is offline", code=ErrorCode.RUNNER_UNAVAILABLE)
        if CAP_HOST_TERMINAL_LINKS not in conn.hello.capabilities:
            raise OmnigentError(
                "Update the host to open linked terminals", code=ErrorCode.CONFLICT
            )
        return host, conn

    async def call(
        self,
        host_id: str,
        user: str | None,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        launch: bool = False,
    ) -> Any:
        from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport

        host, conn = await self.target(host_id, user)
        runner_id = await self.terminals.runner(host, conn, launch=launch)
        if runner_id is None:
            raise OmnigentError("Terminal not found", code=ErrorCode.NOT_FOUND)
        try:
            async with httpx.AsyncClient(
                transport=WSTunnelTransport(self.terminals.tunnels, runner_id),
                base_url="http://runner",
                timeout=5,
            ) as client:
                response = await client.request(method, path, json=body)
        except httpx.HTTPError as exc:
            raise OmnigentError(
                "Terminal host unavailable", code=ErrorCode.RUNNER_UNAVAILABLE
            ) from exc
        if response.status_code >= 400:
            from omnigent.terminals.elevation import AGENT_ACCESS_REQUIRED

            if (
                response.status_code in (403, 409)
                and response.json().get("detail") == AGENT_ACCESS_REQUIRED
            ):
                raise OmnigentError(AGENT_ACCESS_REQUIRED, code=ErrorCode.CONFLICT)
            code = {403: ErrorCode.FORBIDDEN, 404: ErrorCode.NOT_FOUND}.get(
                response.status_code, ErrorCode.CONFLICT
            )
            raise OmnigentError("Terminal operation refused", code=code)
        return response.json() if response.status_code != 204 else None

    async def resource(self, sid: str, host_id: str, tid: str, user: str | None) -> dict[str, Any]:
        page = await self.call(host_id, user, "GET", "/v1/host-terminals")
        for terminal in page["terminals"]:
            if terminal["terminal_id"] == tid and terminal.get("linked_session_id") == sid:
                return terminal
        raise OmnigentError("Linked terminal not found", code=ErrorCode.NOT_FOUND)

    async def list(self, sid: str, user: str | None) -> dict[str, Any]:
        conv = await self.conversation(sid)
        if not await self.owns(user, conv):
            return {"terminals": [], "complete": True}

        async def collect(host_id: str) -> tuple[list[dict[str, Any]], bool]:
            conn = self.terminals.hosts.get(host_id)
            if conn is None or CAP_HOST_TERMINAL_LINKS not in conn.hello.capabilities:
                return [], True
            host = await asyncio.to_thread(self.terminals.host_store.get_host, host_id)
            if host is None or (user is not None and host.user_id != user):
                return [], True
            try:
                async with asyncio.timeout(6):
                    page = await self.call(host_id, user, "GET", "/v1/host-terminals")
                return [
                    {**terminal, "host_id": host_id, "host_name": conn.hello.name}
                    for terminal in page["terminals"]
                    if terminal.get("linked_session_id") == sid
                ], True
            except OmnigentError as exc:
                if exc.code == ErrorCode.NOT_FOUND:
                    return [], True
                return [], False
            except TimeoutError:
                return [], False

        pages = await asyncio.gather(
            *(collect(hid) for hid in self.terminals.hosts.online_host_ids())
        )
        return {
            "terminals": [t for page, _ in pages for t in page],
            "complete": all(ok for _, ok in pages),
        }

    def changed(self, sid: str) -> None:
        from omnigent.runtime import session_stream

        session_stream.publish(sid, {"type": "session.resource.changed", "session_id": sid})

    async def sweep(self, host_id: str | None = None) -> None:
        async def sweep_host(hid: str) -> None:
            host = await asyncio.to_thread(self.terminals.host_store.get_host, hid)
            conn = self.terminals.hosts.get(hid)
            if (
                host is None
                or conn is None
                or CAP_HOST_TERMINAL_LINKS not in conn.hello.capabilities
            ):
                return
            try:
                async with asyncio.timeout(10):
                    page = await self.call(hid, host.user_id, "GET", "/v1/host-terminals")
                    for terminal in page["terminals"]:
                        sid = terminal.get("linked_session_id")
                        if not sid:
                            continue
                        conv = await asyncio.to_thread(self.store.get_conversation, sid)
                        if conv is None or conv.archived:
                            tid = quote(terminal["terminal_id"], safe="")
                            await self.call(
                                hid, host.user_id, "DELETE", f"/v1/host-terminals/{tid}"
                            )
                            self.changed(sid)
            except (OmnigentError, TimeoutError):
                logger.debug("Linked terminal archive sweep deferred for host %s", hid)

        await asyncio.gather(
            *(
                sweep_host(hid)
                for hid in ([host_id] if host_id else self.terminals.hosts.online_host_ids())
            )
        )
