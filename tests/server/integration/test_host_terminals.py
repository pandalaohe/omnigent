"""Standalone terminals through the full server, host tunnel and runner tunnel."""

from __future__ import annotations

import asyncio
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
import uvicorn
import websockets
from sqlalchemy import func, select

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import SqlConversationMetadata
from omnigent.host.frames import (
    CAP_HOST_TERMINALS,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostRunnerStatusFrame,
    HostRunnerStatusResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.runner.host_terminals import create_host_terminal_app
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    PongFrame,
    decode_frame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.serve import serve_tunnel
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.accounts_config import AccountsConfig
from omnigent.server.accounts_store import SqlAlchemyAccountStore
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.oidc import mint_session_token
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.comment_store.sqlalchemy_store import SqlAlchemyCommentStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from tests._helpers.live_server import reserve_port
from tests._helpers.polling import poll_until_async

pytestmark = pytest.mark.asyncio
_HOST = "11aa11aa11aa11aa11aa11aa11aa11aa"
_KEY = b"synthetic-host-terminal-integration-signing-key"


@dataclass
class Server:
    app: object
    port: int
    server: uvicorn.Server | None = None
    task: asyncio.Task | None = None

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    async def start(self):
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("127.0.0.1", self.port))
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app, lifespan="off", log_level="warning", timeout_graceful_shutdown=2
            )
        )
        self.task = asyncio.create_task(self.server.serve(sockets=[sock]))
        await poll_until_async(lambda: self.server.started, timeout_s=5)

    async def stop(self):
        if self.server is not None:
            self.server.should_exit = True
        if self.task is not None:
            await asyncio.wait_for(self.task, 5)


@dataclass
class FakeHost:
    server: Server
    home: Path
    headers: dict[str, str]
    runner_tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    apps: list = field(default_factory=list)
    terminal_runner_id: str | None = None
    task: asyncio.Task | None = None

    async def start(self):
        socket = await websockets.connect(
            self.server.url.replace("http:", "ws:") + f"/v1/hosts/{_HOST}/tunnel",
            additional_headers=self.headers,
        )
        await socket.send(
            encode_host_frame(
                HostHelloFrame(
                    "test",
                    1,
                    "terminal-host",
                    runners=list(self.runner_tasks),
                    terminal_runner_id=self.terminal_runner_id,
                    capabilities=[CAP_HOST_TERMINALS],
                )
            )
        )

        async def listen():
            try:
                async for raw in socket:
                    try:
                        frame = decode_host_frame(raw)
                    except ValueError:
                        ping = decode_frame(raw)
                        if isinstance(ping, PingFrame):
                            await socket.send(encode_frame(PongFrame(ping.ts)))
                        continue
                    if isinstance(frame, HostLaunchRunnerFrame):
                        assert frame.purpose == "host_terminals"
                        assert frame.session_id is None and frame.harness is None
                        assert not frame.startup_required
                        if self.terminal_runner_id:
                            reply = HostLaunchRunnerResultFrame(
                                frame.request_id, "exists", exists=self.terminal_runner_id
                            )
                        else:
                            runner_id = token_bound_runner_id(frame.binding_token)
                            self.terminal_runner_id = runner_id
                            app = create_host_terminal_app(home=self.home, shells=["sh"])
                            cm = app.router.lifespan_context(app)
                            await cm.__aenter__()
                            self.apps.append((app, cm))
                            self.runner_tasks[runner_id] = asyncio.create_task(
                                serve_tunnel(
                                    app,
                                    server_url=self.server.url,
                                    runner_id=runner_id,
                                    runner_version="test",
                                    tunnel_token=frame.binding_token,
                                )
                            )
                            reply = HostLaunchRunnerResultFrame(
                                frame.request_id, "launched", runner_id=runner_id
                            )
                        await socket.send(encode_host_frame(reply))
                    elif isinstance(frame, HostRunnerStatusFrame):
                        active = self.runner_tasks.get(frame.runner_id)
                        await socket.send(
                            encode_host_frame(
                                HostRunnerStatusResultFrame(
                                    frame.request_id,
                                    "alive" if active and not active.done() else "dead",
                                )
                            )
                        )
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                await socket.close()

        self.task = asyncio.create_task(listen())
        await poll_until_async(
            lambda: self.server.app.state.host_registry.get(_HOST) is not None, timeout_s=5
        )

    async def stop(self):
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for task in self.runner_tasks.values():
            task.cancel()
        await asyncio.gather(*self.runner_tasks.values(), return_exceptions=True)
        for _app, cm in self.apps:
            await cm.__aexit__(None, None, None)


@pytest.fixture
def setup(runtime_init, db_uri, tmp_path):
    accounts = SqlAlchemyAccountStore(db_uri)
    owner = accounts.create_user_with_password("terminal-owner", "synthetic-hash", is_admin=True)
    other = accounts.create_user_with_password("other-admin", "synthetic-hash", is_admin=True)

    def headers(account):
        return {
            "Authorization": "Bearer "
            + mint_session_token(
                account.id, _KEY, 3600, "accounts", account_generation=account.account_generation
            ),
            "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
        }

    def make_server(port=None, *, auth_mode="accounts"):
        artifacts = LocalArtifactStore(str(tmp_path / uuid.uuid4().hex))
        provider = (
            UnifiedAuthProvider(
                auth_mode,
                accounts_config=AccountsConfig(_KEY, 8, "http://testserver", None, 3600, 60),
                local_single_user=False,
            )
            if auth_mode is not None
            else None
        )
        app = create_app(
            agent_store=SqlAlchemyAgentStore(db_uri),
            file_store=SqlAlchemyFileStore(db_uri),
            conversation_store=SqlAlchemyConversationStore(db_uri),
            artifact_store=artifacts,
            agent_cache=AgentCache(
                artifact_store=artifacts, cache_dir=tmp_path / uuid.uuid4().hex
            ),
            comment_store=SqlAlchemyCommentStore(db_uri),
            host_store=HostStore(db_uri),
            auth_provider=provider,
            account_store=accounts if auth_mode == "accounts" else None,
            host_sharded=True,
            server_config={"startup_admission": {"enabled": True, "mode": "startup_only"}},
        )
        app.state.host_terminals.reconnect_timeout_s = 2
        app.state.host_terminals.launch_timeout_s = 2
        if port is None:
            with reserve_port() as held:
                port = held.getsockname()[1]
        return Server(app, port)

    from omnigent.server.routes._host_launch import configure_host_sharding

    try:
        yield make_server, headers(owner), headers(other), owner
    finally:
        configure_host_sharding(False)


async def assert_shell(server: Server, headers: dict[str, str], terminal: dict):
    async with websockets.connect(
        server.url.replace("http:", "ws:") + terminal["attach_path"], additional_headers=headers
    ) as ws:
        challenge = uuid.uuid4().hex
        await ws.send(f"printf 'HOST_%s\\n' '{challenge}'\n".encode())
        output = b""
        async with asyncio.timeout(5):
            while f"HOST_{challenge}".encode() not in output:
                value = await ws.recv()
                output += value if isinstance(value, bytes) else value.encode()


def conversation_count(app):
    with app.state.host_store._session("test_count_conversations") as session:
        return session.scalar(select(func.count()).select_from(SqlConversationMetadata))


async def test_standalone_terminal_with_full_startup_queue_and_session_churn(setup, tmp_path):
    make_server, owner_headers, other_headers, owner = setup
    server = make_server()
    host = FakeHost(server, tmp_path, owner_headers)
    await server.start()
    try:
        await host.start()
        startups = server.app.state.startup_store
        with account_authority_scope(owner.id, owner.account_generation):
            startups.configure(_HOST, mode="startup_only")
            for _ in range(3):
                startups.enqueue(
                    owner_user_id=owner.id,
                    host_id=_HOST,
                    session_id=uuid.uuid4().hex,
                    operation_key=uuid.uuid4().hex,
                    input_id=uuid.uuid4().hex,
                )
            assert startups.reserve(_HOST) is not None
            assert startups.reserve(_HOST) is not None
            assert startups.reserve(_HOST) is None
        before = conversation_count(server.app)
        async with httpx.AsyncClient(base_url=server.url, headers=owner_headers) as client:
            assert (await client.get(f"/v1/hosts/{_HOST}/terminals")).json() == {"terminals": []}
            assert host.runner_tasks == {}
            responses = await asyncio.gather(
                *(client.post(f"/v1/hosts/{_HOST}/terminals", json={}) for _ in range(2))
            )
            assert all(response.status_code == 201 for response in responses), [
                response.text for response in responses
            ]
            terminals = [response.json() for response in responses]
            assert len(host.runner_tasks) == 1 and conversation_count(server.app) == before
            await assert_shell(server, owner_headers, terminals[0])
            async with websockets.connect(
                server.url.replace("http:", "ws:")
                + terminals[0]["attach_path"]
                + "?read_only=true",
                additional_headers=owner_headers,
            ) as watcher:
                with pytest.raises(websockets.exceptions.ConnectionClosed) as denied:
                    await watcher.recv()
                assert denied.value.rcvd.code == 4403
            assert not (await client.get("/v1/runners")).json()["data"]
            assert (
                await client.post(f"/v1/hosts/{_HOST}/terminals", json={"elevated": True})
            ).status_code == 409
            assert (
                await client.post(f"/v1/hosts/{_HOST}/terminals", json={"command": "ignored"})
            ).status_code == 422
            async with httpx.AsyncClient(base_url=server.url) as anonymous:
                assert (await anonymous.get(f"/v1/hosts/{_HOST}/terminals")).status_code == 401
            async with httpx.AsyncClient(base_url=server.url, headers=other_headers) as other:
                assert (
                    await other.post(f"/v1/hosts/{_HOST}/terminals", json={})
                ).status_code == 403
            with account_authority_scope(owner.id, owner.account_generation):
                conversations = server.app.state.startup_admission.conversations
                conv = conversations.create_conversation(
                    host_id=_HOST, workspace="/opt/work/sample"
                )
                conversations.update_conversation(conv.id, archived=True)
            await assert_shell(server, owner_headers, terminals[0])
            assert len((await client.get(f"/v1/hosts/{_HOST}/terminals")).json()["terminals"]) == 2
            assert (
                await client.delete(f"/v1/hosts/{_HOST}/terminals/{terminals[0]['terminal_id']}")
            ).status_code == 204
            assert len((await client.get(f"/v1/hosts/{_HOST}/terminals")).json()["terminals"]) == 1
    finally:
        await host.stop()
        await server.stop()


async def test_restart_waits_for_verified_redial_without_second_runner(
    setup, tmp_path, monkeypatch
):
    make_server, owner_headers, _, _owner = setup
    server = make_server()
    host = FakeHost(server, tmp_path, owner_headers)
    real_connect = websockets.connect
    pause_runner = False

    def dial(uri, *args, **kwargs):
        if pause_runner and "/runners/" in str(uri):
            raise ConnectionError("test runner network pause")
        return real_connect(uri, *args, **kwargs)

    monkeypatch.setattr(websockets, "connect", dial)
    await server.start()
    try:
        await host.start()
        async with httpx.AsyncClient(base_url=server.url, headers=owner_headers) as client:
            original = (await client.post(f"/v1/hosts/{_HOST}/terminals", json={})).json()
        pause_runner = True
        await server.stop()
        await host.task
        assert not next(iter(host.runner_tasks.values())).done()
        server = make_server(server.port)
        host.server = server
        await server.start()
        await host.start()
        assert server.app.state.host_registry.terminal_runner(_HOST) is None
        async with httpx.AsyncClient(base_url=server.url, headers=owner_headers) as client:
            opening = asyncio.create_task(client.post(f"/v1/hosts/{_HOST}/terminals", json={}))
            await asyncio.sleep(0.05)  # custom-lint: disable=sleep-then-assert -- Paused redial.
            assert not opening.done() and len(host.runner_tasks) == 1
            pause_runner = False
            second = await opening
            assert second.status_code == 201, second.text
            assert len(host.runner_tasks) == 1
            await assert_shell(server, owner_headers, original)
            assert len((await client.get(f"/v1/hosts/{_HOST}/terminals")).json()["terminals"]) == 2
    finally:
        await host.stop()
        await server.stop()


@pytest.mark.parametrize("auth_mode", ["header", None])
async def test_restart_without_signing_key_ends_old_terminal_runner(setup, tmp_path, auth_mode):
    make_server, _, _, _ = setup
    server = make_server(auth_mode=auth_mode)
    host = server.app.state.host_store.upsert_on_connect(_HOST, "terminal-host", "terminal-owner")
    token, runner_id = server.app.state.host_terminals.mint(host)
    app = create_host_terminal_app(home=tmp_path, shells=["sh"])
    task = None
    await server.start()
    try:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://runner"
            ) as client:
                assert (
                    await client.post("/v1/host-terminals", json={"session_key": uuid.uuid4().hex})
                ).status_code == 201
            assert app.state.has_active_work()
            task = asyncio.create_task(
                serve_tunnel(
                    app,
                    server_url=server.url,
                    runner_id=runner_id,
                    runner_version="test",
                    tunnel_token=token,
                )
            )
            await server.app.state.tunnel_registry.wait_for_runner(runner_id, timeout_s=5)
            assert server.app.state.host_terminals.is_terminal_runner(runner_id)
            await server.stop()
            server = make_server(server.port, auth_mode=auth_mode)
            await server.start()
            with pytest.raises(RuntimeError, match="close code 4004"):
                await asyncio.wait_for(asyncio.shield(task), 5)
        assert not app.state.has_active_work()
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await server.stop()


async def test_two_replicas_readdress_http_and_ws_to_host_owner(setup, tmp_path):
    make_server, owner_headers, _, _owner = setup
    wrong, right = make_server(), make_server()
    host = FakeHost(right, tmp_path, owner_headers)
    await wrong.start()
    await right.start()
    try:
        await host.start()
        async with httpx.AsyncClient(base_url=wrong.url, headers=owner_headers) as client:
            for method, path, body in [
                ("GET", f"/v1/hosts/{_HOST}/terminals", None),
                ("POST", f"/v1/hosts/{_HOST}/terminals", {}),
                ("DELETE", f"/v1/hosts/{_HOST}/terminals/{uuid.uuid4().hex}", None),
            ]:
                response = await client.request(method, path, json=body)
                assert response.status_code == 400, response.text
                assert response.json()["error"]["code"] == "wrong_replica"
        async with websockets.connect(
            wrong.url.replace("http:", "ws:")
            + f"/v1/hosts/{_HOST}/terminals/{uuid.uuid4().hex}/attach",
            additional_headers=owner_headers,
        ) as ws:
            with pytest.raises(websockets.exceptions.ConnectionClosed) as closed:
                await ws.recv()
            assert closed.value.rcvd.code == 4400
        async with httpx.AsyncClient(base_url=right.url, headers=owner_headers) as client:
            created = await client.post(f"/v1/hosts/{_HOST}/terminals", json={})
            assert created.status_code == 201, created.text
            await assert_shell(right, owner_headers, created.json())
    finally:
        await host.stop()
        await wrong.stop()
        await right.stop()
