"""Turn-end ``/clear`` for a session with an armed handover rotation.

``sys_session_handover(rotate=true)`` arms a pending rotation on the runner;
the session's next native idle edge consumes it and types ``/clear`` into the
session's terminal. A session archived (or rotated) since the request, or one
whose ``omnigent.rotate_requested`` label is gone, must not be cleared.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.runner import app as runner_app
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
)

CONV_ID = "conv_rotation_target"


class _RotationServerClient:
    """Answers session snapshots and label reads for the rotation coroutine."""

    def __init__(
        self,
        *,
        archived: bool = False,
        rotate_label: str | None = "0",
        harness: str = "claude-native",
        succession_status: str = "done",
        succession_http_status: int = 200,
        fail_binding: bool = False,
        parent_session_id: str | None = None,
    ) -> None:
        self.archived = archived
        self.archive_revision: int | None = 0
        self.enabled = True
        self.harness = harness
        self.succession_status = succession_status
        self.succession_http_status = succession_http_status
        self.fail_binding = fail_binding
        self.parent_session_id = parent_session_id
        self.created: list[dict[str, Any]] = []
        self.succession: list[dict[str, Any]] = []
        self.bound: list[dict[str, Any]] = []
        self.errors: list[dict[str, Any]] = []
        self.labels: dict[str, str] = {}
        if rotate_label is not None:
            self.labels["omnigent.rotate_requested"] = rotate_label

    async def get(self, url: str, **kwargs: Any) -> httpx.Response:
        del kwargs
        path = url.split("?")[0]
        if path.endswith("/auto-rotation"):
            return httpx.Response(
                200,
                request=httpx.Request("GET", f"http://test{url}"),
                json={
                    "enabled": self.enabled,
                    "eligible": not self.archived,
                    "rotation_pending": bool(self.labels.get("omnigent.rotate_requested")),
                },
            )
        if path.endswith("/labels"):
            return httpx.Response(200, json={"labels": self.labels})
        return httpx.Response(
            200,
            request=httpx.Request("GET", f"http://test{url}"),
            json={
                "archived": self.archived,
                "archive_revision": self.archive_revision,
                "labels": self.labels,
                "harness": self.harness,
                "agent_id": "ag_rotation",
                "runner_id": "runner_rotation",
                "workspace": "/opt/work/sample",
                "parent_session_id": self.parent_session_id,
            },
        )

    async def post(self, url: str, **kwargs: Any) -> httpx.Response:
        if (
            url.endswith("/events")
            and kwargs.get("json", {}).get("type") == "external_conversation_item"
        ):
            self.errors.append(kwargs["json"]["data"]["item_data"])
        if url.endswith("/successor"):
            if not self.created:
                self.created.append({"id": "conv_pi_next"})
            return httpx.Response(
                201,
                json={"id": "conv_pi_next"},
                request=httpx.Request("POST", f"http://test{url}"),
            )
        if url.endswith("/succession"):
            self.succession.append(kwargs["json"])
            return httpx.Response(
                self.succession_http_status,
                json={"status": self.succession_status},
                request=httpx.Request("POST", f"http://test{url}"),
            )
        return httpx.Response(200, json={})

    async def patch(self, url: str, **kwargs: Any) -> httpx.Response:
        self.bound.append(kwargs["json"])
        return httpx.Response(
            503 if self.fail_binding else 200,
            json={},
            request=httpx.Request("PATCH", f"http://test{url}"),
        )


def _build_app(
    server_client: _RotationServerClient, process_manager: Any = None, **kwargs: Any
) -> Any:
    """Build a runner app wired to a claude-native spec and the test server."""
    spec = AgentSpec(
        spec_version=1,
        name="t",
        executor=ExecutorSpec(type="omnigent", config={"harness": server_client.harness}),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    return create_runner_app(
        process_manager=process_manager or _FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=server_client,  # type: ignore[arg-type]
        **kwargs,
    )


async def _seed_session(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/sessions",
        json={"session_id": CONV_ID, "agent_id": "ag_rotation"},
    )
    assert response.status_code == 201, response.text


async def _post_idle(client: httpx.AsyncClient) -> None:
    response = await client.post(
        f"/v1/sessions/{CONV_ID}/events",
        json={"type": "external_session_status", "data": {"status": "idle"}},
    )
    assert response.status_code in (200, 204), response.text


async def _wait_for_injections(injected: list[Any], *, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not injected:
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for the rotation injector")
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
def _clean_pending_rotations() -> Iterator[None]:
    """Keep the module-level pending-rotation set per-test."""
    saved = set(runner_app._pending_rotations)
    runner_app._pending_rotations.clear()
    try:
        yield
    finally:
        runner_app._pending_rotations.clear()
        runner_app._pending_rotations.update(saved)


@pytest.mark.asyncio
async def test_idle_edge_clears_session_with_pending_rotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending rotation types /clear once at the native idle edge."""
    injected: list[tuple[Any, str]] = []

    def _fake_inject(
        bridge_dir: Any,
        *,
        command: str,
        timeout_s: float,
        ready_timeout_s: float,
        auto_confirm: bool = False,
        confirm_hint: str | None = None,
    ) -> None:
        del timeout_s, ready_timeout_s, auto_confirm, confirm_hint
        injected.append((bridge_dir, command))

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    server = _RotationServerClient()
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client)
        runner_app.record_pending_rotation(CONV_ID)
        await _post_idle(client)
        await _wait_for_injections(injected)

    assert [command for _bridge_dir, command in injected] == ["/clear"]
    assert runner_app.pop_pending_rotation(CONV_ID) is False


@pytest.mark.asyncio
async def test_idle_edge_skips_archived_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An archived session is never cleared, even with the label still set."""
    injected: list[str] = []

    def _fake_inject(bridge_dir: Any, *, command: str, **kwargs: Any) -> None:
        del bridge_dir, kwargs
        injected.append(command)

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    server = _RotationServerClient(archived=True)
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client)
        runner_app.record_pending_rotation(CONV_ID)
        await _post_idle(client)
        await asyncio.sleep(0.2)

    assert injected == []


@pytest.mark.asyncio
async def test_idle_edge_skips_session_without_rotate_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleared/voided ``omnigent.rotate_requested`` skips the injection."""
    injected: list[str] = []

    def _fake_inject(bridge_dir: Any, *, command: str, **kwargs: Any) -> None:
        del bridge_dir, kwargs
        injected.append(command)

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    server = _RotationServerClient(rotate_label=None)
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client)
        runner_app.record_pending_rotation(CONV_ID)
        await _post_idle(client)
        await asyncio.sleep(0.2)

    assert injected == []


@pytest.mark.asyncio
async def test_idle_edge_without_pending_rotation_injects_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No armed rotation means the idle edge never reads or clears the session."""
    injected: list[str] = []

    def _fake_inject(bridge_dir: Any, *, command: str, **kwargs: Any) -> None:
        del bridge_dir, kwargs
        injected.append(command)

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", _fake_inject)
    server = _RotationServerClient()
    app = _build_app(server)

    async with _runner_client(app) as client:
        await _seed_session(client)
        await _post_idle(client)
        await asyncio.sleep(0.2)

    assert injected == []


@pytest.mark.asyncio
@pytest.mark.parametrize("succession_status", ["done", "pending", "noop", "retired"])
async def test_pi_rpc_turn_end_releases_only_after_succession(succession_status: str) -> None:
    server = _RotationServerClient(harness="pi", succession_status=succession_status)
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = _build_app(server, manager)
    async with _runner_client(app, permission_harness="pi") as client:
        await _seed_session(client)
        runner_app.record_pending_rotation(CONV_ID)
        app.state.on_proxy_stream_end(CONV_ID)
        deadline = asyncio.get_running_loop().time() + 2.0
        while not server.succession and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)

    assert len(server.created) == 1
    assert len(server.bound) == 1
    assert server.succession == [{"target_session_id": "conv_pi_next", "allow_empty": True}]
    assert manager.released == ([CONV_ID] if succession_status == "done" else [])


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_type", [True, False])
async def test_pi_rpc_user_clear_rotates_without_handover(explicit_type: bool) -> None:
    server = _RotationServerClient(harness="pi", rotate_label=None)
    harness_client = _ScriptedHarnessClient([])
    manager = _FakeProcessManager(harness_client)
    app = _build_app(server, manager)
    async with _runner_client(app, permission_harness="pi") as client:
        await _seed_session(client)
        body: dict[str, Any] = {"content": [{"type": "input_text", "text": "/clear"}]}
        if explicit_type:
            body["type"] = "message"
        response = await client.post(f"/v1/sessions/{CONV_ID}/events", json=body)

    assert response.status_code == 202
    assert not harness_client.posted_bodies
    assert len(server.created) == 1
    assert server.succession == [{"target_session_id": "conv_pi_next", "allow_empty": True}]
    assert manager.released == [CONV_ID]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["binding", "succession", "pending"])
async def test_pi_rpc_retry_keeps_old_process_and_reuses_successor(failure: str) -> None:
    server = _RotationServerClient(
        harness="pi",
        rotate_label=None,
        fail_binding=failure == "binding",
        succession_http_status=409 if failure == "succession" else 200,
        succession_status="pending" if failure == "pending" else "done",
    )
    harness_client = _ScriptedHarnessClient([])
    manager = _FakeProcessManager(harness_client)
    app = _build_app(server, manager)
    message = {"type": "message", "content": [{"type": "input_text", "text": "/clear"}]}
    async with _runner_client(app, permission_harness="pi") as client:
        await _seed_session(client)
        first = await client.post(f"/v1/sessions/{CONV_ID}/events", json=message)
        assert first.status_code == 503
        assert manager.released == []
        assert not harness_client.posted_bodies
        server.fail_binding = False
        server.succession_http_status = 200
        server.succession_status = "done"
        second = await client.post(f"/v1/sessions/{CONV_ID}/events", json=message)
    assert second.status_code == 202
    assert len(server.created) == 1
    assert manager.released == [CONV_ID]
    assert all(body["target_session_id"] == "conv_pi_next" for body in server.succession)


@pytest.mark.asyncio
async def test_pi_child_clear_never_creates_a_top_level_successor() -> None:
    server = _RotationServerClient(harness="pi", rotate_label=None, parent_session_id="mother")
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = _build_app(server, manager)
    async with _runner_client(app, permission_harness="pi") as client:
        await _seed_session(client)
        response = await client.post(
            f"/v1/sessions/{CONV_ID}/events",
            json={"type": "message", "content": [{"type": "input_text", "text": "/clear"}]},
        )
    assert response.status_code == 409
    assert not server.created and not server.succession and not manager.released


@pytest.mark.asyncio
async def test_pi_retired_rotation_is_a_final_conflict() -> None:
    server = _RotationServerClient(harness="pi", succession_status="retired")
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = _build_app(server, manager)
    async with _runner_client(app, permission_harness="pi") as client:
        await _seed_session(client)
        result = await client.post(
            f"/v1/sessions/{CONV_ID}/events",
            json={"type": "message", "content": [{"type": "input_text", "text": "/clear"}]},
        )
    assert result.status_code == 409
    assert result.json()["detail"]["error"] == "succession_retired"
    assert not manager.released


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["control", "text", "idle"])
@pytest.mark.parametrize("failure", [False, True])
async def test_agy_clear_entries_share_confirmation_and_report_failure(
    monkeypatch: pytest.MonkeyPatch, trigger: str, failure: bool
) -> None:
    from omnigent.harnesses.antigravity_native import reader

    calls: list[str] = []

    async def clear(**kwargs: Any) -> str:
        calls.append(kwargs["old_session_id"])
        if failure:
            raise reader.AntigravityClearError("clear_no_new_root: no new root appeared")
        return "conv_agy_next"

    monkeypatch.setattr(reader, "clear_session", clear, raising=False)
    server = _RotationServerClient(harness="antigravity-native")
    app = _build_app(server)
    async with _runner_client(app, permission_harness="antigravity-native") as client:
        await _seed_session(client)
        if trigger == "idle":
            runner_app.record_pending_rotation(CONV_ID)
            await _post_idle(client)
            deadline = asyncio.get_running_loop().time() + 2
            while not calls and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
        else:
            body = (
                {"type": "clear"}
                if trigger == "control"
                else {"type": "message", "content": [{"type": "input_text", "text": "/clear"}]}
            )
            result = await client.post(f"/v1/sessions/{CONV_ID}/events", json=body)
            assert result.status_code == (503 if failure else 200), result.text
            if failure:
                assert "clear_no_new_root" in result.text
    assert calls == [CONV_ID]
    assert len(server.errors) == int(failure)
    if failure:
        assert "clear_no_new_root" in server.errors[0]["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "harness", ["claude-native", "codex-native", "pi", "pi-native", "antigravity-native"]
)
async def test_retrigger_and_idle_share_guard_and_skip_superseded(monkeypatch, tmp_path, harness):
    from omnigent.harnesses.antigravity_native import reader
    from omnigent.inner.terminal import TerminalInstance
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.terminals import TerminalRegistry

    server = _RotationServerClient(harness=harness)
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    effects = []
    monkeypatch.setattr(
        claude_native_bridge,
        "inject_slash_command",
        lambda *args, **kw: effects.append(kw["command"]),
    )
    monkeypatch.setattr(
        claude_native_bridge, "_run_tmux", lambda *args: effects.append(args) or ""
    )

    async def clear(**kwargs):
        effects.append(kwargs["old_session_id"])
        return "next"

    monkeypatch.setattr(reader, "clear_session", clear)
    options = {}
    if harness in {"codex-native", "pi-native"}:
        registry = TerminalRegistry()
        terminal = "codex" if harness == "codex-native" else "pi"
        registry._by_conversation[CONV_ID] = {
            (terminal, "main"): TerminalInstance(
                name=terminal,
                session_key="main",
                socket_path=tmp_path / "socket",
                private_dir=tmp_path,
                running=True,
            )
        }
        options["resource_registry"] = SessionResourceRegistry(terminal_registry=registry)
    app = _build_app(server, manager, **options)
    original_get = server.get
    entered, release = asyncio.Event(), asyncio.Event()
    async with _runner_client(app, permission_harness=harness) as client:
        await _seed_session(client)

        async def blocked_get(url, **kwargs):
            if not entered.is_set():
                entered.set()
                await release.wait()
            return await original_get(url, **kwargs)

        server.get = blocked_get
        first = asyncio.create_task(client.post(f"/v1/sessions/{CONV_ID}/pending-rotation"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        second = await client.post(f"/v1/sessions/{CONV_ID}/pending-rotation")
        assert second.json() == {"status": "in_flight"}
        runner_app.record_pending_rotation(CONV_ID)
        if harness == "pi":
            app.state.on_proxy_stream_end(CONV_ID)
        else:
            await _post_idle(client)
        idle_tasks = [
            task
            for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__name__", None) == "_run_pending_rotation"
        ]
        await asyncio.wait_for(asyncio.gather(*idle_tasks), timeout=2)
        release.set()
        result = await first
        assert result.json() == {"status": "started"}
        if harness == "pi":
            assert len(server.succession) == 1
        elif harness in {"codex-native", "pi-native"}:
            command = "/clear" if harness == "codex-native" else "/new"
            assert sum(command in row for row in effects) == 1
        else:
            assert len(effects) == 1
        before = list(effects)
        # The command has returned, but the successor label is still absent.
        runner_app.record_pending_rotation(CONV_ID)
        if harness == "pi":
            app.state.on_proxy_stream_end(CONV_ID)
        else:
            await _post_idle(client)
        idle_tasks = [
            task
            for task in asyncio.all_tasks()
            if getattr(task.get_coro(), "__name__", None) == "_run_pending_rotation"
        ]
        await asyncio.wait_for(asyncio.gather(*idle_tasks), timeout=2)
        result = await client.post(f"/v1/sessions/{CONV_ID}/pending-rotation")
        assert result.json() == {"status": "in_flight"}
        assert effects == before
        assert len(server.succession) == int(harness == "pi")
        server.labels["omnigent.succeeded_by"] = "next"
        result = await client.post(f"/v1/sessions/{CONV_ID}/pending-rotation")
        assert result.json() == {"status": "superseded"}
        assert effects == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "harness", ["claude-native", "codex-native", "pi", "pi-native", "antigravity-native"]
)
@pytest.mark.parametrize("busy_source", ["native", "turn"])
async def test_recovery_rechecks_busy_after_server_validation(monkeypatch, harness, busy_source):
    effects = []
    monkeypatch.setattr(
        claude_native_bridge, "inject_slash_command", lambda *args, **kw: effects.append(kw)
    )
    server = _RotationServerClient(harness=harness)
    app = _build_app(server)
    original_get = server.get
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocked_get(url, **kwargs):
        if url.endswith("/auto-rotation"):
            entered.set()
            await release.wait()
        return await original_get(url, **kwargs)

    async with _runner_client(app, permission_harness=harness) as client:
        await _seed_session(client)
        server.get = blocked_get
        first = asyncio.create_task(client.post(f"/v1/sessions/{CONV_ID}/pending-rotation"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        if busy_source == "native" and harness != "pi":
            response = await client.post(
                f"/v1/sessions/{CONV_ID}/events",
                json={"type": "external_session_status", "data": {"status": "running"}},
            )
            assert response.status_code in (200, 204)
        else:
            app.state.active_turns[CONV_ID] = None
        release.set()
        assert (await first).json() == {"status": "busy"}
        app.state.active_turns.pop(CONV_ID, None)
    assert not effects and not server.created and not server.succession


@pytest.mark.asyncio
async def test_recovery_holds_runtime_boundary_through_injection(monkeypatch):
    server = _RotationServerClient()
    app = _build_app(server)
    original_get = server.get
    entered, release = asyncio.Event(), asyncio.Event()
    attempting, started = asyncio.Event(), asyncio.Event()
    effects = []

    async def blocked_get(url, **kwargs):
        if url.endswith("/auto-rotation"):
            entered.set()
            await release.wait()
        return await original_get(url, **kwargs)

    def inject(*args, **kwargs):
        assert not started.is_set()
        effects.append(kwargs["command"])

    monkeypatch.setattr(claude_native_bridge, "inject_slash_command", inject)
    async with _runner_client(app) as client:
        await _seed_session(client)
        server.get = blocked_get
        rotation = asyncio.create_task(client.post(f"/v1/sessions/{CONV_ID}/pending-rotation"))
        await asyncio.wait_for(entered.wait(), timeout=2)

        async def start_turn():
            attempting.set()
            async with app.state.cli_runtime_lifecycle.lock_for(CONV_ID).shared():
                started.set()

        turn = asyncio.create_task(start_turn())
        await asyncio.wait_for(attempting.wait(), timeout=2)
        assert not started.is_set()
        release.set()
        assert (await rotation).json() == {"status": "started"}
        await asyncio.wait_for(turn, timeout=2)
    assert effects == ["/clear"]


@pytest.mark.asyncio
async def test_recovery_can_heal_pane_under_rotation_lock(monkeypatch, tmp_path):
    from omnigent.runner.resource_registry import SessionResourceView
    from omnigent.terminals import TerminalRegistry
    from tests.runner.test_app_sessions_native_session_change_heal import _plant_dead_claude_pane

    effects = []

    async def launch(ctx):
        effects.append("heal")
        return SessionResourceView(
            id="terminal_claude_main", type="terminal", session_id=ctx.session_id, name="claude"
        )

    monkeypatch.setattr("omnigent.runner.native.orchestration._launch_claude", launch)
    monkeypatch.setattr("omnigent.runner.native._launch_claude", launch)
    monkeypatch.setattr(
        claude_native_bridge,
        "inject_slash_command",
        lambda *args, **kw: effects.append(kw["command"]),
    )
    registry = TerminalRegistry()
    app = _build_app(_RotationServerClient(), terminal_registry=registry)
    async with _runner_client(app) as client:
        await _seed_session(client)
        effects.clear()
        _plant_dead_claude_pane(
            registry, CONV_ID, tmp_path, claude_native_bridge.bridge_dir_for_bridge_id(CONV_ID)
        )
        result = await asyncio.wait_for(
            client.post(f"/v1/sessions/{CONV_ID}/pending-rotation"), timeout=2
        )
        assert result.json() == {"status": "started"}
    assert effects == ["heal", "/clear"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["switch_off", "stale_revision", "missing_revision", "expected_revision"]
)
async def test_recovery_fails_closed_on_disabled_or_stale_request(monkeypatch, failure):
    effects = []
    monkeypatch.setattr(
        claude_native_bridge, "inject_slash_command", lambda *args, **kwargs: effects.append(args)
    )
    server = _RotationServerClient()
    if failure == "switch_off":
        server.enabled = False
    elif failure == "stale_revision":
        server.archive_revision = 1
    elif failure == "missing_revision":
        server.archive_revision = None
    app = _build_app(server)
    async with _runner_client(app) as client:
        await _seed_session(client)
        result = await client.post(
            f"/v1/sessions/{CONV_ID}/pending-rotation",
            json={"archive_revision": "1" if failure == "expected_revision" else "0"},
        )
        assert result.json() == {"status": "not_requested"}
        assert effects == []
