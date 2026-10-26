"""Tests for the opencode serve process manager + arg/env builders."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.harnesses.opencode_native import app_server as appsrv
from omnigent.harnesses.opencode_native.app_server import (
    OpenCodeCliNotFoundError,
    OpenCodeNativeServer,
    OpenCodeVersionError,
    build_opencode_attach_args,
    build_opencode_serve_args,
    check_opencode_version,
    filtered_server_env,
    find_opencode_cli,
    opencode_terminal_env,
    parse_opencode_version,
)


def test_parse_opencode_version() -> None:
    assert parse_opencode_version("opencode 1.17.7") == "1.17.7"
    assert parse_opencode_version("1.17.7") == "1.17.7"
    assert parse_opencode_version("v1.17.7-beta.1") == "1.17.7-beta.1"
    assert parse_opencode_version("no version here") is None


def test_check_version_in_range() -> None:
    check_opencode_version("1.17.7")
    check_opencode_version("1.17.99")
    check_opencode_version("1.18.0")
    check_opencode_version("1.18.16")


@pytest.mark.parametrize("version", ["1.16.0", "1.19.0", "2.0.0"])
def test_check_version_out_of_range_raises(version: str) -> None:
    with pytest.raises(OpenCodeVersionError):
        check_opencode_version(version)


def test_check_version_unparsable_raises() -> None:
    with pytest.raises(OpenCodeVersionError):
        check_opencode_version("not-a-version")


def test_find_opencode_cli_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appsrv.shutil, "which", lambda _name: None)
    with pytest.raises(OpenCodeCliNotFoundError):
        find_opencode_cli()


def test_find_opencode_cli_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert find_opencode_cli() == "/usr/bin/opencode"


def test_build_serve_args_has_explicit_host_port() -> None:
    args = build_opencode_serve_args(hostname="127.0.0.1", port=49231)
    assert args == ["serve", "--hostname", "127.0.0.1", "--port", "49231"]


def test_build_attach_args() -> None:
    args = build_opencode_attach_args(
        server_url="http://127.0.0.1:49231",
        workspace="/repo",
        session_id="ses_1",
    )
    assert args == [
        "attach",
        "http://127.0.0.1:49231",
        "--dir",
        "/repo",
        "--session",
        "ses_1",
    ]


def test_build_attach_args_without_session() -> None:
    args = build_opencode_attach_args(
        server_url="http://127.0.0.1:49231",
        workspace="/repo",
        session_id=None,
        opencode_args=("--extra",),
    )
    assert "--session" not in args
    assert args[-1] == "--extra"


def test_filtered_server_env_sets_xdg_and_password(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "secret-key")
    monkeypatch.setenv("RANDOM_UNRELATED", "nope")
    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")
    assert env["XDG_DATA_HOME"] == str(tmp_path / "xdg-data")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "xdg-config")
    assert env["OPENCODE_SERVER_PASSWORD"] == "pw"
    assert env["OPENCODE_SERVER_USERNAME"] == "opencode"
    assert env["ANTHROPIC_API_KEY"] == "secret-key"  # provider env passes through
    assert "RANDOM_UNRELATED" not in env  # unrelated env filtered out


def test_filtered_server_env_honors_runner_passthrough(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_ENV_PASSTHROUGH", "GH_TOKEN, GH_CONFIG_DIR, MISSING")
    monkeypatch.setenv("GH_TOKEN", "ghp_example")
    monkeypatch.setenv("GH_CONFIG_DIR", "/home/user/.config/gh")
    monkeypatch.setenv("UNLISTED_SECRET", "nope")

    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")

    assert env["GH_TOKEN"] == "ghp_example"
    assert env["GH_CONFIG_DIR"] == "/home/user/.config/gh"
    assert "MISSING" not in env
    assert "UNLISTED_SECRET" not in env


def test_filtered_server_env_drops_global_opencode_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Global OpenCode config env never leaks into the isolated session.

    ``OPENCODE_CONFIG`` / ``OPENCODE_CONFIG_CONTENT`` would re-introduce the
    parent shell's config/model/permission settings, defeating the
    per-session XDG isolation — so they are dropped even though they match
    the ``OPENCODE_`` passthrough prefix. Other ``OPENCODE_`` vars (and the
    server password we set) are unaffected.
    """
    monkeypatch.setenv("OPENCODE_CONFIG", "/home/user/.config/opencode/opencode.json")
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"model": "evil/model"}')
    monkeypatch.setenv("OPENCODE_DISABLE_AUTOUPDATE", "1")
    monkeypatch.setenv(
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH", "OPENCODE_CONFIG,OPENCODE_CONFIG_CONTENT"
    )
    env = filtered_server_env(bridge_dir=tmp_path, auth_secret="pw")
    assert "OPENCODE_CONFIG" not in env
    assert "OPENCODE_CONFIG_CONTENT" not in env
    # An unrelated OPENCODE_ var is still passed through (not config leakage).
    assert env["OPENCODE_DISABLE_AUTOUPDATE"] == "1"
    # The per-session XDG dirs remain the only config source.
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / "xdg-config")


def _server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> OpenCodeNativeServer:
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    return OpenCodeNativeServer(
        bridge_dir=tmp_path,
        workspace=tmp_path,
        port=49231,
        verify_version=False,
    )


@pytest.fixture
async def context_server(monkeypatch, tmp_path):
    """The real server/client owner with scripted REST, no CLI or provider process."""
    server = _server(monkeypatch, tmp_path)
    server.process = SimpleNamespace(returncode=None)
    requests = []

    async def respond(request):
        requests.append((request.method, request.url.path))
        return await responses[0](request)

    async def default(request):
        if request.url.path.endswith("/fork"):
            return httpx.Response(200, json={"id": "fork", "parentID": "primary"})
        if request.url.path.endswith("/message"):
            return httpx.Response(200, json=[])
        if request.url.path == "/event":
            return httpx.Response(200, text="", headers={"content-type": "text/event-stream"})
        if request.method == "GET" and request.url.path.startswith("/session/"):
            return httpx.Response(200, json={"id": request.url.path.rsplit("/", 1)[1]})
        return httpx.Response(200, json={"id": "primary"})

    responses = [default]
    async with httpx.AsyncClient(
        base_url="http://opencode.test", transport=httpx.MockTransport(respond)
    ) as http:
        constructor = appsrv.OpenCodeClient
        monkeypatch.setattr(
            appsrv, "OpenCodeClient", lambda *a, **kw: constructor(*a, client=http, **kw)
        )
        yield server, server.client(), responses, requests


async def test_context_v2_primary_resume_and_owned_fork(context_server):
    server, client, _, requests = context_server
    primary = await client.create_session()
    (created,) = server.context_source_v2("conversation").contexts
    assert created.vendor_id.value == primary.id
    assert created.key.source_owner_incarnation == server.resident_incarnation_id
    assert created.operation.action == "create" and created.provenance == "primary"
    assert created.execution_keys == ("primary",) and created.lifecycle == "live"
    assert await client.get_session("primary") is not None
    (resumed,) = server.context_source_v2("conversation").contexts
    assert resumed.key == created.key and resumed.operation.action == "resume"
    assert resumed.operation.operation_id != created.operation.operation_id
    await client.fork("primary")
    source = server.context_source_v2("conversation")
    parent, fork = source.contexts
    assert fork.operation.parent == parent.key and fork.operation.action == "fork"
    assert fork.provenance == "managed_side_chat" and fork.execution_keys == ("fork",)
    await client.get_session("fork")
    observed = server.context_source_v2("conversation").contexts[1]
    assert observed.key == fork.key and observed.operation == fork.operation
    assert observed.provenance == "managed_side_chat"
    assert not parent.aliases and not fork.aliases
    assert source.coverage == "partial" and source.revision_authority == "observation_only"
    assert {u.reason for u in source.unresolved} >= {
        "readonly_alias_provenance_unavailable",
        "unobserved_bridge_and_native_contexts",
    }
    assert all(row.stop_proof is row.continuity_proof is None for row in source.contexts)
    assert requests == [
        ("POST", "/session"),
        ("GET", "/session/primary"),
        ("POST", "/session/primary/fork"),
        ("GET", "/session/fork"),
    ]


async def test_context_v2_concurrent_resume_and_failure_retry_keep_one_identity(context_server):
    server, client, responses, _ = context_server
    await client.create_session()
    key = server.context_source_v2("conversation").contexts[0].key
    entered, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def interleaved(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            entered.set()
            await release.wait()
        else:
            raise httpx.ReadTimeout("newest reply lost")
        return httpx.Response(200, json={"id": "primary"})

    responses[0] = interleaved
    old = asyncio.create_task(client.get_session("primary"))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        with pytest.raises(httpx.ReadTimeout):
            await client.get_session("primary")
        assert len(server.context_source_v2("conversation").contexts) == 1
        assert server.context_source_v2("conversation").contexts[0].lifecycle == "unknown"
        release.set()
        await old
        assert server.context_source_v2("conversation").contexts[0].lifecycle == "unknown"
        responses[0] = lambda r: asyncio.sleep(
            0, result=httpx.Response(200, json={"id": "primary"})
        )
        await client.get_session("primary")
        (resumed,) = server.context_source_v2("conversation").contexts
        assert resumed.key == key and resumed.lifecycle == "live"
    finally:
        release.set()
        old.cancel()
        await asyncio.gather(old, return_exceptions=True)


@pytest.mark.parametrize(
    "outcome", ["success", "lost", "cancel", "close", "generation", "incarnation"]
)
async def test_context_v2_pending_reply_cannot_bind_replacement(context_server, outcome):
    server, client, responses, _ = context_server
    entered, finish = asyncio.Event(), asyncio.Event()

    async def pending(request):
        entered.set()
        await finish.wait()
        if outcome == "lost":
            raise httpx.ReadTimeout("reply lost")
        return httpx.Response(200, json={"id": "late"})

    responses[0] = pending
    task = asyncio.create_task(client.create_session())
    close = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        (before,) = server.context_source_v2("conversation").contexts
        assert before.lifecycle == "starting" and before.vendor_id is None
        assert before.operation.action == "create" and before.execution_keys == ()
        if outcome == "close":
            close = server.close()
            assert server.context_source_v2("conversation").contexts[0].lifecycle == "closing"
        elif outcome == "generation":
            server._start_generation += 1
        elif outcome == "incarnation":
            server.resident_incarnation_id = "replacement"
        if outcome == "cancel":
            task.cancel()
        finish.set()
        if outcome in {"lost", "cancel"}:
            with pytest.raises(httpx.ReadTimeout if outcome == "lost" else asyncio.CancelledError):
                await task
        else:
            await task
        source = server.context_source_v2("conversation")
        if outcome == "incarnation":
            assert source.contexts == ()
            assert before.key.context_incarnation in {u.candidate for u in source.unresolved}
        else:
            (after,) = source.contexts
            assert after.key == before.key
            assert after.lifecycle == (
                "live" if outcome == "success" else "closing" if outcome == "close" else "unknown"
            )
            assert (after.vendor_id is not None) is (outcome == "success")
    finally:
        finish.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if close is not None:
            close.close()


async def test_context_v2_forwarder_retirement_is_not_context_absence(context_server):
    from omnigent.harnesses.opencode_native.forwarder import OpenCodeNativeForwarder

    server, client, _, _ = context_server
    await client.create_session()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200))
    ) as sink:
        forwarder = OpenCodeNativeForwarder(
            session_id="conversation",
            opencode_session_id="primary",
            opencode_client=client,
            server_client=sink,
        )
        await forwarder.run(max_reconnects=0)
    source = server.context_source_v2("conversation")
    assert source.contexts[0].lifecycle == "unknown"
    assert source.contexts[0].vendor_id.value == "primary"
    assert "forwarder_not_observed_live" in {row.reason for row in source.unresolved}
    server._start_generation += 1
    client.observe_forwarder(True)
    assert server._context_forwarder is False


async def test_context_v2_close_retains_runner_tombstone(context_server, monkeypatch):
    from omnigent.runner.native import orchestration

    server, client, _, _ = context_server
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    monkeypatch.setattr(orchestration, "_CLOSING_NATIVE_BACKENDS", {})
    monkeypatch.setattr(orchestration, "_RETIRED_NATIVE_CONTEXTS", [])
    monkeypatch.setattr(orchestration, "_RETIRED_NATIVE_OWNERS", {})
    monkeypatch.setattr(orchestration, "_native_backend_exit_coverage_complete", True)
    monkeypatch.setattr(orchestration, "_native_context_history_overflow", False)
    await client.create_session()
    key = server.context_source_v2("conversation").contexts[0].key
    entered, release = asyncio.Event(), asyncio.Event()

    async def close():
        entered.set()
        await release.wait()
        server.process = None
        server.process_exit_confirmed = True
        server._mark_contexts("unknown")

    monkeypatch.setattr(server, "_close_locked", close)
    task = asyncio.create_task(
        orchestration._close_native_backend("conversation", "opencode-native", server)
    )
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert (
            orchestration.native_context_source_v2("conversation").contexts[0].lifecycle
            == "closing"
        )
    finally:
        release.set()
        await task
    source = orchestration.native_context_source_v2("conversation")
    assert source.contexts[0].key == key and source.contexts[0].lifecycle == "unknown"
    assert source.contexts[0].stop_proof is None and source.coverage == "partial"


async def test_context_v2_retired_gap_survives_backend_replacement(context_server, monkeypatch):
    from omnigent.runner.native import orchestration
    from omnigent.startup_protocol import UnresolvedContextV2

    server, _, _, _ = context_server
    old = server.context_source_v2("conversation").model_copy(
        update={
            "producer_incarnation": "old-owner",
            "contexts": (),
            "unresolved": (UnresolvedContextV2(candidate=None, reason="context_history_budget"),),
        }
    )
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {"conversation": server})
    monkeypatch.setattr(orchestration, "_STARTING_NATIVE_BACKENDS", {})
    monkeypatch.setattr(orchestration, "_CLOSING_NATIVE_BACKENDS", {})
    monkeypatch.setattr(orchestration, "_RETIRED_NATIVE_CONTEXTS", [old])
    monkeypatch.setattr(orchestration, "_RETIRED_NATIVE_OWNERS", {})
    monkeypatch.setattr(orchestration, "_native_backend_exit_coverage_complete", True)
    monkeypatch.setattr(orchestration, "_native_context_history_overflow", False)
    source = orchestration.native_context_source_v2("conversation")
    assert "context_history_budget" in {u.reason for u in source.unresolved}
    assert source.coverage == "partial"
    monkeypatch.setattr(orchestration, "_RETIRED_NATIVE_CONTEXTS", [old] * 1000)
    source = orchestration.native_context_source_v2("conversation")
    assert source.contexts == () and source.unresolved[0].reason == "context_history_budget"


def test_build_argv(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _server(monkeypatch, tmp_path)
    replacement = _server(monkeypatch, tmp_path)
    assert server.resident_incarnation_id != replacement.resident_incarnation_id
    assert not server.exit_confirmed
    server.port = 49231
    argv = server.build_argv()
    assert argv[0] == "/usr/bin/opencode"
    assert argv[1:] == ["serve", "--hostname", "127.0.0.1", "--port", "49231"]


def test_base_url_and_auth_headers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _server(monkeypatch, tmp_path)
    assert server.base_url == "http://127.0.0.1:49231"
    assert server.auth_headers["Authorization"].startswith("Basic ")


def test_terminal_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    server = _server(monkeypatch, tmp_path)
    env = opencode_terminal_env(server)
    assert env["OPENCODE_SERVER_PASSWORD"] == server.auth_secret
    assert env["XDG_DATA_HOME"] == str(server.xdg_data_home)


async def test_close_serializes_restart_and_skips_stale_queued_close(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    server = _server(monkeypatch, tmp_path)
    entered, resume = asyncio.Event(), asyncio.Event()
    terminated: list[object] = []

    class _Process:
        returncode = 0

        def poll(self):
            return self.returncode

        def wait(self, _timeout):
            return self.returncode

    old_process, new_process = _Process(), _Process()
    server.process = old_process  # type: ignore[assignment]
    server._resident_spawned = True
    old_id = server.resident_incarnation_id

    async def to_thread(fn, *args):
        if fn is appsrv._proc.terminate_tree:
            terminated.append(args[0])
            entered.set()
            await resume.wait()
        else:
            return fn(*args)
        return None

    async def spawn():
        server.resident_incarnation_id = "new-incarnation"
        server.process = new_process  # type: ignore[assignment]
        server.exit_confirmed = False

    monkeypatch.setattr(appsrv.asyncio, "to_thread", to_thread)
    monkeypatch.setattr(server, "_start", spawn)
    closing = asyncio.create_task(server.close())
    await asyncio.wait_for(entered.wait(), timeout=1)
    starting = asyncio.create_task(server.start())
    stale_close = asyncio.create_task(server.close())
    try:
        await asyncio.sleep(0)
        assert not starting.done()
        assert terminated == [old_process]
        # OpenCode has no tree proof; simulate a future authoritative owner proof.
        server.exit_confirmed = True
    finally:
        resume.set()
        await asyncio.gather(closing, starting, stale_close)
    assert server.resident_incarnation_id != old_id
    assert server.process is new_process
    assert not server.exit_confirmed
    assert terminated == [old_process]


async def test_start_polls_until_ready(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from omnigent.runner.native import orchestration

    server = _server(monkeypatch, tmp_path)
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", {})
    monkeypatch.setattr(orchestration, "_AUTO_OPENCODE_SERVERS", {})
    monkeypatch.setattr(orchestration, "_CLOSING_NATIVE_BACKENDS", {})
    monkeypatch.setattr(orchestration, "_native_backend_exit_coverage_complete", True)
    monkeypatch.setattr(orchestration, "_RETIRED_NATIVE_OWNERS", {})
    started: dict[str, object] = {}

    class _FakeProc:
        pid = None
        returncode = None

        def poll(self) -> None:
            return None

        def wait(self, _timeout: float | None = None) -> int:
            self.returncode = 0
            return 0

    def fake_popen(argv, **kwargs):  # type: ignore[no-untyped-def]
        started["argv"] = argv
        started["env"] = kwargs.get("env")
        return _FakeProc()

    async def fake_wait(self: OpenCodeNativeServer) -> None:
        started["ready"] = True

    monkeypatch.setattr(appsrv.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", fake_wait)
    monkeypatch.setattr(appsrv._proc, "terminate_tree", lambda _process: None)
    construction_id = server.resident_incarnation_id
    await server.start()
    assert server.resident_incarnation_id != construction_id
    assert started["ready"] is True
    assert started["argv"][1] == "serve"
    assert server.process is not None
    assert server.process.pid is None
    incarnation_id, process = server.resident_incarnation_id, server.process
    with pytest.raises(RuntimeError, match="exit is not confirmed"):
        await server.start()
    assert server.process is process
    assert server.resident_incarnation_id == incarnation_id
    await orchestration._close_native_backend("owned", "opencode-native", server)
    assert server.process_exit_confirmed
    assert not server.exit_confirmed
    closing = orchestration.native_backend_resident_inventory()
    (retired,) = closing.owners
    assert retired.incarnation_id == incarnation_id
    assert retired.closing and retired.process_exit_confirmed and not retired.exit_confirmed
    assert not closing.exit_coverage_complete
    with pytest.raises(RuntimeError, match="exit is not confirmed"):
        await server.start()
    # A future lifecycle owner can supply authoritative descendant-exit proof.
    server.exit_confirmed = True
    await orchestration._close_native_backend("owned", "opencode-native", server)
    assert (orchestration.native_backend_resident_inventory()).owners == closing.owners
    assert not orchestration.native_backend_resident_inventory().exit_coverage_complete
    await server.start()
    assert server.resident_incarnation_id != incarnation_id
    assert not server.exit_confirmed


async def test_start_closes_process_when_readiness_is_cancelled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Cancelling the readiness wait must reap the OpenCode server."""
    server = _server(monkeypatch, tmp_path)

    class _FakeProc:
        pid = None
        returncode: int | None = None
        terminated = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def wait(self, _timeout: float | None = None) -> int:
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    process = _FakeProc()
    readiness_started = asyncio.Event()

    async def parked_wait(self: OpenCodeNativeServer) -> None:
        readiness_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(appsrv.subprocess, "Popen", lambda argv, **kwargs: process)
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", parked_wait)

    start_task = asyncio.create_task(server.start())
    await readiness_started.wait()
    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert process.terminated is True
    assert server.process is None
    assert server.process_exit_confirmed
    assert not server.exit_confirmed


async def test_start_closes_process_when_readiness_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A readiness probe that gives up must not leave ``opencode serve`` running."""
    server = _server(monkeypatch, tmp_path)

    class _FakeProc:
        pid = None
        returncode: int | None = None
        terminated = False

        def poll(self) -> int | None:
            return self.returncode

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = -15

        def wait(self, _timeout: float | None = None) -> int:
            assert self.returncode is not None
            return self.returncode

        def kill(self) -> None:
            self.returncode = -9

    process = _FakeProc()

    async def failing_wait(self: OpenCodeNativeServer) -> None:
        raise RuntimeError("opencode serve did not become ready")

    monkeypatch.setattr(appsrv.subprocess, "Popen", lambda argv, **kwargs: process)
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", failing_wait)

    with pytest.raises(RuntimeError, match="did not become ready"):
        await server.start()

    assert process.terminated is True
    assert server.process is None
    assert server.process_exit_confirmed
    assert not server.exit_confirmed


async def test_start_raises_on_unsupported_version_without_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(appsrv, "resolve_opencode_version", lambda _path: "1.19.0")
    monkeypatch.delenv("OMNIGENT_OPENCODE_SKIP_VERSION_CHECK", raising=False)
    server = OpenCodeNativeServer(
        bridge_dir=tmp_path,
        workspace=tmp_path,
        port=49231,
        verify_version=True,
    )

    class _FakeProc:
        pid = 4242

        def poll(self) -> None:
            return None

    async def fake_wait(self: OpenCodeNativeServer) -> None:
        return None

    monkeypatch.setattr(appsrv.subprocess, "Popen", lambda argv, **kwargs: _FakeProc())
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", fake_wait)
    with pytest.raises(OpenCodeVersionError):
        await server.start()


async def test_start_skips_version_gate_when_env_set(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(appsrv.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(appsrv, "resolve_opencode_version", lambda _path: "1.19.0")
    monkeypatch.setenv("OMNIGENT_OPENCODE_SKIP_VERSION_CHECK", "1")
    server = OpenCodeNativeServer(
        bridge_dir=tmp_path,
        workspace=tmp_path,
        port=49231,
        verify_version=True,
    )

    class _FakeProc:
        pid = 4242

        def poll(self) -> None:
            return None

    async def fake_wait(self: OpenCodeNativeServer) -> None:
        return None

    monkeypatch.setattr(appsrv.subprocess, "Popen", lambda argv, **kwargs: _FakeProc())
    monkeypatch.setattr(OpenCodeNativeServer, "_wait_until_ready", fake_wait)
    await server.start()
    assert server.version == "1.19.0"
    assert server.process is not None


def test_find_opencode_cli_absolute_executable(tmp_path: Path) -> None:
    exe = tmp_path / "opencode"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert appsrv.find_opencode_cli(str(exe)) == str(exe)


def test_resolve_opencode_version_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(
        appsrv.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="opencode 1.17.7\n", stderr=""),
    )
    assert appsrv.resolve_opencode_version("/x/opencode") == "1.17.7"


def test_resolve_opencode_version_run_error_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("cannot exec")

    monkeypatch.setattr(appsrv.subprocess, "run", _boom)
    with pytest.raises(appsrv.OpenCodeVersionError):
        appsrv.resolve_opencode_version("/x/opencode")


def test_resolve_opencode_version_unparseable_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(
        appsrv.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="no version here", stderr=""),
    )
    with pytest.raises(appsrv.OpenCodeVersionError):
        appsrv.resolve_opencode_version("/x/opencode")


def test_list_opencode_cli_model_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    monkeypatch.setattr(appsrv, "find_opencode_cli", lambda _path=None: "/x/opencode")

    captured: dict[str, object] = {}

    def _fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="\x1b[32mopencode-go/glm-5.2\x1b[0m\nopencode-go/kimi-k2.7-code\n",
            stderr="",
        )

    monkeypatch.setattr(appsrv.subprocess, "run", _fake_run)

    options = appsrv.list_opencode_cli_model_options(
        env={"XDG_DATA_HOME": "/xdg/data", "XDG_CONFIG_HOME": "/xdg/config"},
    )

    assert [option["id"] for option in options] == [
        "opencode-go/glm-5.2",
        "opencode-go/kimi-k2.7-code",
    ]
    assert captured["env"] == {"XDG_DATA_HOME": "/xdg/data", "XDG_CONFIG_HOME": "/xdg/config"}
