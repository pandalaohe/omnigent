"""Terminal-only runner surfaces and inactivity lifetime."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from omnigent.runner import _entry
from omnigent.runner.host_terminals import create_host_terminal_app
from omnigent.runner.identity import RUNNER_PURPOSE_ENV_VAR
from tests._helpers.polling import poll_until


def test_terminal_app_real_tmux_roundtrip_and_no_session_surface(tmp_path: Path) -> None:
    app = create_host_terminal_app(home=tmp_path, shells=["sh"])
    with TestClient(app) as client:
        assert client.get("/v1/host-terminals").json() == {"terminals": []}
        assert not app.state.has_active_work()
        old_idle = app.state.host_terminal_idle_since()
        created = client.post("/v1/host-terminals", json={"session_key": uuid.uuid4().hex})
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["elevated"] is False
        assert app.state.has_active_work()
        with client.websocket_connect(body["attach_path"]) as websocket:
            challenge = uuid.uuid4().hex
            websocket.send_bytes(f"printf 'ROUNDTRIP_%s\\n' '{challenge}'\n".encode())
            observed = b""
            while f"ROUNDTRIP_{challenge}".encode() not in observed:
                packet = websocket.receive()
                observed += packet.get("bytes", b"")
        assert client.post("/v1/sessions/session/events", json={}).status_code == 404
        assert client.get("/v1/host-terminals").json()["terminals"] == [body]
        assert (
            client.post(
                "/v1/host-terminals", json={"session_key": uuid.uuid4().hex, "elevated": True}
            ).status_code
            == 409
        )
        assert client.delete(f"/v1/host-terminals/{body['terminal_id']}").status_code == 204
        assert not app.state.has_active_work()
        assert app.state.host_terminal_idle_since() >= old_idle


def test_entry_chooses_terminal_only_app_before_harness_bootstrap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(RUNNER_PURPOSE_ENV_VAR, "host_terminals")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_RUNNER_INTERACTIVE_SHELLS", '["sh"]')
    import omnigent.runtime.harnesses.process_manager as managers

    monkeypatch.setattr(
        managers, "HarnessProcessManager", Mock(side_effect=AssertionError("harness boot"))
    )
    app = _entry.create_app()
    assert app.state.runner_purpose == "host_terminals"
    assert not hasattr(app.state, "terminal_attach_handler")
    assert not any("sessions" in route.path for route in app.routes)


def test_failed_launch_is_hidden_and_retries_live_instance_cleanup(monkeypatch, tmp_path):
    from omnigent.inner.terminal import TerminalInstance

    original_launch = TerminalInstance.launch
    original_close = TerminalInstance.close
    close_calls = 0

    async def launch_then_fail(instance, **kwargs):
        await original_launch(instance, **kwargs)
        raise RuntimeError("launch interrupted after spawn")

    async def fail_first_close(instance):
        nonlocal close_calls
        close_calls += 1
        if close_calls == 1:
            raise RuntimeError("cleanup temporarily unavailable")
        await original_close(instance)

    monkeypatch.setattr(TerminalInstance, "launch", launch_then_fail)
    monkeypatch.setattr(TerminalInstance, "close", fail_first_close)
    app = create_host_terminal_app(home=tmp_path, shells=["sh"])
    with TestClient(app, raise_server_exceptions=False) as client:
        key = uuid.uuid4().hex
        assert client.post("/v1/host-terminals", json={"session_key": key}).status_code == 500
        assert app.state.has_active_work()
        response = client.get("/v1/host-terminals")
        assert response.status_code == 200 and response.json() == {"terminals": []}
        with client.websocket_connect(f"/v1/host-terminals/{key}/attach") as websocket:
            assert websocket.receive()["code"] == 4404
        poll_until(lambda: not app.state.has_active_work(), timeout_s=5)
        assert close_calls >= 2


@pytest.mark.asyncio
async def test_open_terminal_holds_runner_past_two_hours_then_empty_uses_close_clock(
    tmp_path: Path,
) -> None:
    app = create_host_terminal_app(home=tmp_path, shells=["sh"])
    with TestClient(app) as client:
        response = client.post("/v1/host-terminals", json={"session_key": uuid.uuid4().hex})
        body = response.json()
        loop = asyncio.get_running_loop()
        closed = asyncio.Event()
        empty_age_s = 0
        monitor = asyncio.create_task(
            _entry._run_inactivity_monitor(
                idle_timeout_s=60,
                get_last_activity=lambda: (
                    loop.time() - 7200
                    if app.state.has_active_work()
                    else app.state.host_terminal_idle_since() - empty_age_s
                ),
                has_active_work=app.state.has_active_work,
                request_shutdown=closed.set,
                poll_interval_s=0.001,
            )
        )
        await asyncio.sleep(0.02)
        assert not closed.is_set()
        client.delete(f"/v1/host-terminals/{body['terminal_id']}")
        await asyncio.sleep(0.02)
        assert not closed.is_set()
        empty_age_s = 59
        await asyncio.sleep(0.01)
        assert not closed.is_set()
        empty_age_s = 61
        await asyncio.wait_for(closed.wait(), 1)
        await monitor


@pytest.mark.asyncio
async def test_terminal_entry_uses_binding_only_and_no_direct_listener(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import omnigent.runner.direct_attach as direct_attach
    import omnigent.runner.transports.ws_tunnel.serve as tunnel

    monkeypatch.setenv(RUNNER_PURPOSE_ENV_VAR, "host_terminals")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://testserver")
    monkeypatch.setenv("OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN", "synthetic-terminal-binding")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_RUNNER_INTERACTIVE_SHELLS", '["sh"]')
    monkeypatch.setattr(_entry, "_runner_parent_pid_from_env", lambda: None)
    monkeypatch.setattr(_entry, "_install_signal_handlers", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        _entry, "_make_auth_token_factory", Mock(side_effect=AssertionError("owner credentials"))
    )
    monkeypatch.setattr(
        direct_attach,
        "start_direct_attach_listener",
        Mock(side_effect=AssertionError("direct attach")),
    )
    received = {}

    async def serve(app, **kwargs):
        received.update(kwargs)
        assert app.state.runner_purpose == "host_terminals"

    async def monitor(**kwargs):
        received["idle_timeout_s"] = kwargs["idle_timeout_s"]
        await asyncio.Future()

    monkeypatch.setattr(tunnel, "serve_tunnel", serve)
    monkeypatch.setattr(_entry, "_run_inactivity_monitor", monitor)
    await _entry._run_tunnel_from_env()
    assert received["auth_token"] is None and received["auth_token_factory"] is None
    assert received["direct_attach_port"] is None and received["direct_attach_token"] is None
    assert received["idle_timeout_s"] == 60


@pytest.mark.parametrize("end", ["interrupt", "idle"])
def test_host_admin_terminal_uses_shared_launcher_and_lifecycle(monkeypatch, tmp_path, end):
    from omnigent.terminals import TerminalRegistry
    from omnigent.terminals.elevation import ELEVATED_TERMINALS_ENV, elevated_terminal_spec

    monkeypatch.setenv(ELEVATED_TERMINALS_ENV, "sudo")
    selected = []

    def launcher(spec, *, method):
        elevated = elevated_terminal_spec(spec, method=method)
        selected.append((elevated.command, elevated.args))
        return replace(
            elevated,
            command="/bin/sh",
            args=["-c", "trap 'exit 130' INT; printf 'Password:'; read -r reply; exit 1"],
        )

    monkeypatch.setattr("omnigent.terminals.registry.elevated_terminal_spec", launcher)
    registry = TerminalRegistry()
    app = create_host_terminal_app(home=tmp_path, shells=["sh"], terminal_registry=registry)
    with TestClient(app) as client:
        key = uuid.uuid4().hex
        response = client.post("/v1/host-terminals", json={"session_key": key, "elevated": True})
        assert response.status_code == 201, response.text
        body = response.json()
        assert selected == [("sudo", ["-i"])]
        assert body["elevated"] is True
        assert client.get("/v1/host-terminals").json() == {"terminals": [body]}
        instance = registry.get("host-terminals", "shell", key)
        assert instance is not None and instance.elevated and not instance.agent_access
        input_times = []
        note_input = instance.note_input

        def remember_input():
            note_input()
            input_times.append(instance.last_input_at)

        monkeypatch.setattr(instance, "note_input", remember_input)
        with client.websocket_connect(body["attach_path"]) as websocket:
            output = b""
            before = instance.last_input_at
            while b"Password:" not in output:
                output += websocket.receive_bytes()
            assert instance.last_input_at == before
            websocket.send_text('{"type":"resize","cols":100,"rows":40}')
            websocket.send_bytes(b"renew")
            poll_until(lambda: instance.last_input_at > before, timeout_s=3)
            renewed = instance.last_input_at
            websocket.send_text('{"type":"resize","cols":90,"rows":30}')
            websocket.send_bytes(b"input")
            poll_until(lambda: instance.last_input_at > renewed, timeout_s=3)
            assert len(input_times) == 2  # Attach and resize must not renew input idle.
            if end == "interrupt":
                websocket.send_bytes(b"\x03")
            else:
                instance.last_input_at -= 901
        poll_until(
            lambda: registry.get("host-terminals", "shell", key) is None and not instance.running,
            timeout_s=5,
        )
        assert not instance.running and not instance.agent_access
        assert client.get("/v1/host-terminals").json() == {"terminals": []}
