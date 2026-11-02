"""Admission and find-or-launch for sessionless terminal runners."""

from __future__ import annotations

import asyncio
import secrets
import threading
from dataclasses import asdict, dataclass

import jwt

from omnigent.db.db_models import current_workspace_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostLaunchRunnerFrame, HostRunnerStatusFrame, encode_host_frame
from omnigent.runner.identity import token_bound_runner_id
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.auth import AuthProvider
from omnigent.server.host_registry import HostConnection, HostLaunchResult, HostRegistry
from omnigent.stores.host_store import Host, HostStore

HOST_TERMINAL_TOKEN_PREFIX = "ht1."
_TOKEN_AUDIENCE = "omnigent-host-terminal-tunnel"
_TOKEN_ISSUER = "omnigent-host-terminal"


@dataclass(frozen=True)
class HostTerminalClaim:
    workspace_id: int
    host_id: str
    owner: str
    account_generation: str | None
    nonce: str
    purpose: str = "host_terminals"
    workspace: str = "~"


class HostTerminalService:
    def __init__(
        self,
        host_registry: HostRegistry,
        host_store: HostStore,
        tunnels: TunnelRegistry,
        *,
        auth_provider: AuthProvider | None,
        account_store: SqlAlchemyAccountStore | None = None,
        reconnect_timeout_s: float = 30,
        launch_timeout_s: float = 30,
    ) -> None:
        self.hosts = host_registry
        self.host_store = host_store
        self.tunnels = tunnels
        self._secret = auth_provider.host_terminal_signing_secret if auth_provider else None
        self._accounts = account_store
        self.reconnect_timeout_s = reconnect_timeout_s
        self.launch_timeout_s = launch_timeout_s
        self._lock = threading.RLock()
        self._records: dict[tuple[int, str], HostTerminalClaim] = {}
        self._bindings: dict[tuple[int, str], HostTerminalClaim] = {}
        self._pending: dict[tuple[int, str], asyncio.Task[str | None]] = {}

    def _validate(self, claim: HostTerminalClaim) -> Host:
        if claim.workspace_id != current_workspace_id() or claim.workspace != "~":
            raise ValueError("terminal workspace changed")
        host = self.host_store.get_host(claim.host_id)
        if (
            host is None
            or host.user_id != claim.owner
            or host.account_generation != claim.account_generation
        ):
            raise ValueError("terminal host authority changed")
        if self._accounts is not None:
            account = self._accounts.get_user(claim.owner)
            if account is None or account.account_generation != claim.account_generation:
                raise ValueError("terminal account authority changed")
        return host

    def mint(self, host: Host) -> tuple[str, str]:
        claim = HostTerminalClaim(
            workspace_id=current_workspace_id(),
            host_id=host.host_id,
            owner=host.user_id,
            account_generation=host.account_generation,
            nonce=secrets.token_hex(16),
        )
        self._validate(claim)
        if self._secret is not None:
            token = HOST_TERMINAL_TOKEN_PREFIX + jwt.encode(
                {**asdict(claim), "aud": _TOKEN_AUDIENCE, "iss": _TOKEN_ISSUER},
                self._secret,
                algorithm="HS256",
            )
        else:
            token = HOST_TERMINAL_TOKEN_PREFIX + secrets.token_urlsafe(32)
        runner_id = token_bound_runner_id(token)
        if self._secret is None:
            with self._lock:
                self._records[(claim.workspace_id, runner_id)] = claim
        return token, runner_id

    def admit(self, token: str) -> HostTerminalClaim:
        """Verify before generic auth; a host declaration never grants admission."""
        if not token.startswith(HOST_TERMINAL_TOKEN_PREFIX):
            raise ValueError("not a terminal-purpose binding")
        runner_id = token_bound_runner_id(token)
        key = (current_workspace_id(), runner_id)
        if self._secret is None:
            with self._lock:
                claim = self._records.get(key)
            if claim is None:
                raise ValueError("terminal launch record missing")
        else:
            try:
                payload = jwt.decode(
                    token[len(HOST_TERMINAL_TOKEN_PREFIX) :],
                    self._secret,
                    algorithms=["HS256"],
                    audience=_TOKEN_AUDIENCE,
                    issuer=_TOKEN_ISSUER,
                    options={
                        "require": [
                            "purpose",
                            "workspace_id",
                            "host_id",
                            "owner",
                            "nonce",
                            "workspace",
                        ]
                    },
                )
            except jwt.PyJWTError as exc:
                raise ValueError("invalid terminal binding") from exc
            generation = payload.get("account_generation")
            if (
                payload.get("purpose") != "host_terminals"
                or "account_generation" not in payload
                or type(payload.get("workspace_id")) is not int
                or not isinstance(payload.get("host_id"), str)
                or not isinstance(payload.get("owner"), str)
                or not isinstance(payload.get("nonce"), str)
                or not payload["nonce"]
                or (generation is not None and not isinstance(generation, str))
                or payload.get("workspace") != "~"
            ):
                raise ValueError("invalid terminal binding tuple")
            claim = HostTerminalClaim(
                payload["workspace_id"],
                payload["host_id"],
                payload["owner"],
                generation,
                payload["nonce"],
            )
        self._validate(claim)
        with self._lock:
            self.hosts.record_terminal_runner(claim.host_id, runner_id)
            self._bindings[key] = claim
        return claim

    def is_terminal_runner(self, runner_id: str) -> bool:
        with self._lock:
            return (current_workspace_id(), runner_id) in self._bindings

    def validate_runner(self, host_id: str, runner_id: str) -> None:
        with self._lock:
            claim = self._bindings.get((current_workspace_id(), runner_id))
        if claim is None or claim.host_id != host_id:
            raise OmnigentError(
                "terminal runner is reconnecting", code=ErrorCode.RUNNER_UNAVAILABLE
            )
        try:
            self._validate(claim)
        except ValueError as exc:
            raise OmnigentError(
                "terminal runner authority changed", code=ErrorCode.FORBIDDEN
            ) from exc

    def _forget(self, host_id: str, runner_id: str) -> None:
        self.hosts.forget_terminal_runner(host_id, runner_id)
        conn = self.hosts.get(host_id)
        if conn is not None and conn.hello.terminal_runner_id == runner_id:
            conn.hello.terminal_runner_id = None
        with self._lock:
            key = (current_workspace_id(), runner_id)
            self._bindings.pop(key, None)
            self._records.pop(key, None)

    async def runner(self, host: Host, conn: HostConnection, *, launch: bool) -> str | None:
        key = (current_workspace_id(), host.host_id)
        with self._lock:
            task = self._pending.get(key)
            if task is None and launch:
                task = asyncio.create_task(self._find_or_launch(host, conn, launch=True))
                self._pending[key] = task

                def release(completed: asyncio.Task[str | None]) -> None:
                    with self._lock:
                        if self._pending.get(key) is completed:
                            self._pending.pop(key)
                    if not completed.cancelled():
                        completed.exception()

                task.add_done_callback(release)
        if task is not None:
            return await asyncio.shield(task)
        return await self._find_or_launch(host, conn, launch=False)

    async def _find_or_launch(
        self, host: Host, conn: HostConnection, *, launch: bool
    ) -> str | None:
        runner_id = self.hosts.terminal_runner(host.host_id) or conn.hello.terminal_runner_id
        if runner_id is not None:
            connected = self.tunnels.get(runner_id)
            if connected is None:
                connected = await self.tunnels.wait_for_runner(
                    runner_id, timeout_s=self.reconnect_timeout_s
                )
            if connected is not None:
                await asyncio.to_thread(self.validate_runner, host.host_id, runner_id)
                return runner_id
            status = await self._status(conn, runner_id)
            if status != "dead":
                raise OmnigentError(
                    "terminal runner is reconnecting", code=ErrorCode.RUNNER_UNAVAILABLE
                )
            self._forget(host.host_id, runner_id)
        if not launch:
            return None
        # Re-read authority before issuing a new process capability.
        current = await asyncio.to_thread(self.host_store.get_host, host.host_id)
        if current is None or current.user_id != host.user_id:
            raise OmnigentError("host authority changed", code=ErrorCode.FORBIDDEN)
        token, runner_id = await asyncio.to_thread(self.mint, current)
        conn.hello.terminal_runner_id = runner_id
        request_id = secrets.token_hex(16)
        future: asyncio.Future[HostLaunchResult] = asyncio.get_running_loop().create_future()
        conn.pending_launches[request_id] = future
        try:
            self.hosts.send_text(
                conn,
                encode_host_frame(
                    HostLaunchRunnerFrame(
                        request_id=request_id,
                        binding_token=token,
                        workspace="~",
                        purpose="host_terminals",
                    )
                ),
            )
            result = await asyncio.wait_for(future, self.launch_timeout_s)
        except (ConnectionError, TimeoutError) as exc:
            # A lost result does not prove the process died. Hello/status reconciles retries.
            raise OmnigentError(
                "terminal runner launch is reconnecting", code=ErrorCode.RUNNER_UNAVAILABLE
            ) from exc
        finally:
            conn.pending_launches.pop(request_id, None)
        existing = result.get("exists")
        if result.get("status") == "exists" and existing:
            self._forget(host.host_id, runner_id)
            runner_id = existing
        elif result.get("status") != "launched" or result.get("runner_id") != runner_id:
            # Registry teardown synthesizes a failure without runner_id. Only a daemon
            # result can confirm that no process was spawned; a lost result stays pending.
            if result.get("status") == "failed" and "runner_id" in result:
                self._forget(host.host_id, runner_id)
                message = "terminal runner launch failed"
            else:
                message = "terminal runner launch is reconnecting"
            raise OmnigentError(message, code=ErrorCode.RUNNER_UNAVAILABLE)
        # Keep the identity while offline so a subsequent open waits rather than replaces it.
        conn.hello.terminal_runner_id = runner_id
        if (
            await self.tunnels.wait_for_runner(runner_id, timeout_s=self.reconnect_timeout_s)
            is None
        ):
            raise OmnigentError(
                "terminal runner is reconnecting", code=ErrorCode.RUNNER_UNAVAILABLE
            )
        await asyncio.to_thread(self.validate_runner, host.host_id, runner_id)
        return runner_id

    async def _status(self, conn: HostConnection, runner_id: str) -> str | None:
        request_id = secrets.token_hex(16)
        future: asyncio.Future[dict[str, str | None]] = asyncio.get_running_loop().create_future()
        conn.pending_runner_status[request_id] = future
        try:
            self.hosts.send_text(
                conn,
                encode_host_frame(
                    HostRunnerStatusFrame(request_id, runner_id, purpose="host_terminals")
                ),
            )
            result = await asyncio.wait_for(future, self.launch_timeout_s)
            return result.get("status")
        except (ConnectionError, TimeoutError):
            return None
        finally:
            conn.pending_runner_status.pop(request_id, None)
