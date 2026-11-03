"""Runner-side idle CLI inspection and release contracts."""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import bridge_dir_for_bridge_id
from omnigent.inner.native_attachments import (
    attachment_cache_dir,
    materialize_attachment,
    session_attachment_dir,
)
from omnigent.runner import create_runner_app
from omnigent.terminals.pane_reaper import NativePaneReaper, PaneRef
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


@pytest.mark.parametrize(
    "family,credentials",
    [
        (
            "codex",
            ("codex-home/auth.json", "codex-home/.credentials.json", "codex-home/config.toml"),
        ),
        ("hermes", ("hermes_home/.env", "hermes_home/auth.json", "hermes_home/config.yaml")),
        ("opencode", ("xdg-data/opencode/auth.json", "xdg-config/opencode/opencode.json")),
        (
            "pi",
            (
                "pi-agent/models.json",
                "pi-agent/mcp-auth.json",
                "pi-agent/mcp.json",
                "pi-agent/omnigent-auth/auth-test.json",
                "pi-agent/omnigent-auth/auth-test.result.json",
                "pi-agent/omnigent-auth/token-test.json",
            ),
        ),
    ],
)
async def test_idle_release_scrubs_recreatable_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, family: str, credentials: tuple[str, ...]
) -> None:
    bridge = importlib.import_module(f"omnigent.harnesses.{family}_native.bridge")
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / family)
    home = (
        bridge.bridge_dir_for_session_id
        if family in {"pi", "hermes"}
        else bridge.bridge_dir_for_bridge_id
    )("conv_scrub")
    for name in credentials:
        path = home / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}")
    history = home / "history.jsonl"
    history.write_text("history")
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    await app.state.finish_cli_release("conv_scrub")
    assert history.read_text() == "history"
    assert all(not (home / name).exists() for name in credentials)


@pytest.mark.parametrize(
    "family,outcome",
    [("codex", "retry"), ("codex", "delete"), ("codex", "confirmed"), ("opencode", "confirmed")],
)
async def test_idle_release_checks_own_codex_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    family: str,
    outcome: str,
) -> None:
    from omnigent.harnesses.codex_native import app_server as codex_module
    from omnigent.harnesses.opencode_native.app_server import OpenCodeNativeServer
    from omnigent.runner.native import orchestration
    from tests.harnesses.codex_native.app_server._support import _test_app_server

    bridge = importlib.import_module(f"omnigent.harnesses.{family}_native.bridge")
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / family)
    session_id = "conv_exit_gate"
    home = bridge.prepare_bridge_dir(session_id)
    auth = home / ("codex-home/auth.json" if family == "codex" else "xdg-data/opencode/auth.json")
    auth.parent.mkdir(parents=True)
    auth.write_text("{}")
    census_available = outcome == "confirmed"
    if family == "codex":
        backend = _test_app_server(tmp_path, auth.parent, home, tmp_path)
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "pass", start_new_session=True
        )
        await process.wait()
        backend.proc = process
        backend.process_group_id = process.pid
        backend.process_registry_tag = "release-close-test"
        monkeypatch.setattr(
            codex_module,
            "_tagged_process_group_members",
            lambda *_: [] if census_available else None,
        )
        monkeypatch.setattr(codex_module, "unregister_codex_native_process", lambda *_: None)
    else:
        backend = OpenCodeNativeServer(
            bridge_dir=home, workspace=tmp_path, opencode_path=sys.executable
        )
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        process.wait()
        backend.process = process
    registry = "_AUTO_CODEX_APP_SERVERS" if family == "codex" else "_AUTO_OPENCODE_SERVERS"
    monkeypatch.setattr(orchestration, registry, {session_id: backend})
    if family == "opencode":
        await orchestration.teardown_opencode_native_server(session_id)
        assert backend.process_exit_confirmed and not backend.exit_confirmed
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    async with _runner_client(app) as client:
        if outcome != "confirmed":
            for _ in range(2):
                with pytest.raises(RuntimeError, match="exit is not confirmed"):
                    await client.post(
                        f"/v1/sessions/{session_id}/cli-retention/release",
                        json={"reason": "archive"},
                    )
                assert auth.exists()
                assert app.state.cli_runtime_lifecycle.phase(session_id) == "live"
                assert not backend.exit_confirmed
            if outcome == "delete":
                assert (await client.delete(f"/v1/sessions/{session_id}")).status_code == 200
                auth.parent.mkdir(parents=True)
                auth.write_text("{}")
            else:
                census_available = True
        response = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release", json={"reason": "archive"}
        )
        assert response.status_code == 200
        assert not auth.exists()
        if family == "codex":
            assert backend.exit_confirmed is (outcome != "delete")


async def test_idle_release_scrubs_pi_token_temporaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.pi_native import bridge

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "pi")
    home = bridge.prepare_bridge_dir("conv_pi_temp")
    token_temp = home / "pi-agent/omnigent-auth/token-test.json.123.tmp"
    token_temp.parent.mkdir(parents=True)
    token_temp.write_text("{}")
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    await app.state.finish_cli_release("conv_pi_temp")
    assert not token_temp.exists()


@pytest.mark.parametrize("sequence", ["label-lookup", "pane-reaper", "rejected-tail"])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_idle_release_keeps_exit_evidence_after_real_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sequence: str,
    confirmed: bool,
) -> None:
    from omnigent.harnesses.codex_native import app_server, bridge
    from omnigent.runner.native import orchestration
    from omnigent.runner.resource_registry import SessionResourceRegistry
    from omnigent.terminals.registry import TerminalRegistry
    from tests.harnesses.codex_native.app_server._support import _test_app_server

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "codex-native")
    session_id = "conv_exit_evidence"
    home = bridge.prepare_bridge_dir(session_id)
    codex_home = bridge.codex_home_for_bridge_dir(home)
    backend = _test_app_server(tmp_path, codex_home, home, tmp_path)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass", start_new_session=True
    )
    await process.wait()
    backend.proc = process
    backend.process_group_id = process.pid
    backend.process_registry_tag = "release-exit-evidence"
    census_available = confirmed
    monkeypatch.setattr(
        app_server, "_tagged_process_group_members", lambda *_: [] if census_available else None
    )
    monkeypatch.setattr(app_server, "unregister_codex_native_process", lambda *_: None)
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", {session_id: backend})
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_BRIDGE_DIRS", {session_id: home})
    bridge.write_bridge_state(
        home,
        bridge.CodexNativeBridgeState(
            session_id=session_id,
            socket_path=str(backend.socket_path),
            thread_id="parent_thread",
            codex_home=str(codex_home),
        ),
    )
    bridge.update_active_child_turn(home, "old_child", turn_id="old_turn")
    client = NullServerClient()
    lookup_entered = False

    async def get_labels(url, **kwargs):
        nonlocal lookup_entered
        if not lookup_entered:
            lookup_entered = True
            await orchestration.teardown_codex_native_app_server(session_id)
        return httpx.Response(200, json={"labels": {}})

    if sequence == "label-lookup":
        monkeypatch.setattr(client, "get", get_labels)
    terminals = TerminalRegistry()
    app = create_runner_app(
        terminal_registry=terminals,
        resource_registry=SessionResourceRegistry(terminal_registry=terminals),
        server_client=client,  # type: ignore[arg-type]
    )
    if sequence in {"pane-reaper", "rejected-tail"}:
        await app.state.native_pane_reaper._reap(
            PaneRef(
                session_id,
                "terminal_codex_main",
                "codex",
                tmp_path / "tmux.sock",
                allow_busy_release=True,
            )
        )
        if sequence == "rejected-tail":
            assert (
                await app.state.native_pane_reaper._finish_retired(session_id, "codex", "stale")
                is None
            )
    if not confirmed:
        with pytest.raises(RuntimeError, match="exit is not confirmed"):
            await app.state.finish_cli_release(session_id)
        assert not backend.exit_confirmed
        assert bridge.read_bridge_state(home).helper_stop_pending
        assert bridge.read_active_child_turns(home)["old_child"]["turn_id"] == "old_turn"
        census_available = True
    await app.state.finish_cli_release(session_id)
    assert backend.exit_confirmed
    assert not bridge.read_bridge_state(home).helper_stop_pending
    assert bridge.read_active_child_turns(home, strict=True) == {}


@pytest.mark.parametrize(
    "sequence",
    [
        "legacy-reap",
        "rejected-token",
        "rejected-pane",
        "rejected-lock",
        "overlapping-tail",
        "ownerless-capture",
    ],
)
async def test_codex_release_does_not_pin_a_completed_or_abandoned_incarnation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sequence: str
) -> None:
    import gc
    import weakref
    from types import SimpleNamespace

    from omnigent.harnesses.codex_native import app_server, bridge
    from omnigent.runner.native import orchestration
    from omnigent.runner.resource_registry import (
        SessionResourceRegistry,
        _TerminalRoleRegistration,
    )
    from omnigent.terminals.registry import TerminalRegistry
    from tests.harnesses.codex_native.app_server._support import _test_app_server

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "codex-native")
    session_id = "conv_release_incarnations"
    home = bridge.prepare_bridge_dir(session_id)

    async def create_backend():
        backend = _test_app_server(
            tmp_path, bridge.codex_home_for_bridge_dir(home), home, tmp_path
        )
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "pass", start_new_session=True
        )
        await process.wait()
        backend.proc = process
        backend.process_group_id = process.pid
        backend.process_registry_tag = "release-incarnations"
        return backend

    old = await create_backend()
    monkeypatch.setattr(app_server, "_tagged_process_group_members", lambda *_: [])
    monkeypatch.setattr(app_server, "unregister_codex_native_process", lambda *_: None)
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_APP_SERVERS", {session_id: old})
    monkeypatch.setattr(orchestration, "_AUTO_CODEX_BRIDGE_DIRS", {session_id: home})
    terminals = TerminalRegistry()
    resources = SessionResourceRegistry(terminal_registry=terminals)
    server_client = NullServerClient()
    app = create_runner_app(
        terminal_registry=terminals,
        resource_registry=resources,
        server_client=server_client,  # type: ignore[arg-type]
    )
    reaper = app.state.native_pane_reaper
    if sequence == "ownerless-capture":
        # A failed teardown leaves an unconfirmed capture. The retiring owner
        # claims it and times out on the lifecycle lock while the release
        # caller borrows it and finishes the close; the completed backend must
        # still become collectible.
        entered, release_close = asyncio.Event(), asyncio.Event()
        close_calls = 0
        original_close = type(old).close

        async def paused_close(self) -> None:
            nonlocal close_calls
            close_calls += 1
            if close_calls == 2:
                entered.set()
                await release_close.wait()
            await original_close(self)

        monkeypatch.setattr(type(old), "close", paused_close)
        census_available = False
        monkeypatch.setattr(
            app_server,
            "_tagged_process_group_members",
            lambda *_: [] if census_available else None,
        )
        try:
            await app.state.finish_cli_release(session_id)
        except RuntimeError as failure:
            assert "exit is not confirmed" in str(failure)
            # Drop the traceback: its frames still hold the borrowed capture,
            # so only the abandoned pending record may keep the backend alive.
            failure.__traceback__ = None
        else:
            raise AssertionError("failed teardown left a confirmed Codex exit")
        assert not old.exit_confirmed

        token = app.state.cli_runtime_lifecycle.runtime_token(session_id)
        lock = app.state.cli_runtime_lifecycle.lock_for(session_id)
        borrower = None
        async with lock.shared():
            retirement = asyncio.create_task(reaper._finish_retired(session_id, "codex", token))
            try:
                # The retirement attempt claims the capture synchronously and
                # then parks on the exclusive lock, so wait for that waiter
                # before lending the capture to the release caller.
                for _ in range(200):
                    if getattr(lock, "_waiters", None):
                        break
                    await asyncio.sleep(0)
                else:
                    raise AssertionError("retirement never parked on the lifecycle lock")
                borrower = asyncio.create_task(app.state.finish_cli_release(session_id))
                await asyncio.wait_for(entered.wait(), timeout=10)
                # The owner times out and drops its ownership marker while the
                # release caller still holds the shared lock and the close.
                assert await asyncio.wait_for(retirement, timeout=10) == token
                assert not borrower.done()
                census_available = True
                release_close.set()
                await asyncio.wait_for(borrower, timeout=10)
            finally:
                release_close.set()
                pending = [retirement] if borrower is None else [retirement, borrower]
                await asyncio.gather(*pending, return_exceptions=True)
        assert old.exit_confirmed

        del pending, retirement, borrower
        old_reference = weakref.ref(old)
        del old
        gc.collect()
        assert old_reference() is None, (
            "a completed Codex backend stayed reachable through the abandoned pending capture"
        )
        return
    if sequence == "overlapping-tail":
        entered, release = asyncio.Event(), asyncio.Event()

        async def get_labels(*_args, **_kwargs):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={"labels": {}})

        monkeypatch.setattr(server_client, "get", get_labels)
        operation = asyncio.create_task(app.state.finish_cli_release(session_id))
        resumed = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=2)
            await old.close()
            resumed = await create_backend()
            close = Mock(wraps=resumed.close)
            monkeypatch.setattr(resumed, "close", close)
            orchestration._AUTO_CODEX_APP_SERVERS[session_id] = resumed
            orchestration._AUTO_CODEX_BRIDGE_DIRS[session_id] = home
            bridge.write_bridge_state(
                home,
                bridge.CodexNativeBridgeState(
                    session_id=session_id,
                    socket_path=str(resumed.socket_path),
                    thread_id="resumed_thread",
                    codex_home=str(bridge.codex_home_for_bridge_dir(home)),
                ),
            )
            bridge.update_active_child_turn(home, "new_child", turn_id="new_turn")
            assert await reaper._finish_retired(session_id, "codex", "stale") is None
            release.set()
            with pytest.raises(RuntimeError, match="Codex backend changed"):
                await operation
            close.assert_not_called()
            assert bridge.read_bridge_state(home).helper_stop_pending
            assert bridge.read_active_child_turns(home)["new_child"]["turn_id"] == "new_turn"
        finally:
            release.set()
            await asyncio.gather(operation, return_exceptions=True)
            if resumed is not None:
                await resumed.close()
        return
    if sequence == "legacy-reap":
        await reaper._reap(
            PaneRef(session_id, "terminal_codex_main", "codex", home, allow_busy_release=True)
        )
    else:
        token = app.state.cli_runtime_lifecycle.runtime_token(session_id)
        if sequence == "rejected-pane":
            pane = SimpleNamespace(
                socket_path=home / "tmux.sock", diagnostic_id="successor", elevated=False
            )
            terminals._by_conversation[session_id] = {("codex", "main"): pane}
            resources._terminal_roles[(session_id, "terminal_codex_main")] = (
                _TerminalRoleRegistration("codex-native", "successor")
            )
        if sequence == "rejected-lock":
            async with app.state.cli_runtime_lifecycle.lock_for(session_id).shared():
                assert await reaper._finish_retired(session_id, "codex", token) == token
        else:
            supplied_token = token if sequence == "rejected-pane" else "stale"
            assert await reaper._finish_retired(session_id, "codex", supplied_token) is None
        terminals._by_conversation.pop(session_id, None)
        await old.close()
    assert old.exit_confirmed

    resumed = await create_backend()
    orchestration._AUTO_CODEX_APP_SERVERS[session_id] = resumed
    orchestration._AUTO_CODEX_BRIDGE_DIRS[session_id] = home
    old_reference = weakref.ref(old)
    del old
    gc.collect()
    assert old_reference() is None
    bridge.write_bridge_state(
        home,
        bridge.CodexNativeBridgeState(
            session_id=session_id,
            socket_path=str(resumed.socket_path),
            thread_id="resumed_thread",
            codex_home=str(bridge.codex_home_for_bridge_dir(home)),
        ),
    )
    bridge.update_active_child_turn(home, "new_child", turn_id="new_turn")
    await app.state.finish_cli_release(session_id)
    assert resumed.exit_confirmed
    assert not bridge.read_bridge_state(home).helper_stop_pending
    assert bridge.read_active_child_turns(home, strict=True) == {}


@pytest.mark.parametrize("parent_link", [False, True])
async def test_idle_credential_scrub_never_follows_links_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, parent_link: bool
) -> None:
    from omnigent.harnesses.codex_native import bridge

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "codex")
    home = bridge.prepare_bridge_dir("conv_scrub")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "auth.json").write_text("source")
    if parent_link:
        (home / "codex-home").symlink_to(outside, target_is_directory=True)
    else:
        (home / "codex-home").mkdir()
        (home / "codex-home" / "auth.json").symlink_to(outside / "auth.json")
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    if parent_link:
        with pytest.raises(RuntimeError, match="credential scrub"):
            await app.state.finish_cli_release("conv_scrub")
    else:
        await app.state.finish_cli_release("conv_scrub")
        assert not (home / "codex-home" / "auth.json").is_symlink()
    assert (outside / "auth.json").read_text() == "source"


async def test_idle_credential_scrub_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from omnigent.harnesses.codex_native import bridge

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "codex")
    home = bridge.prepare_bridge_dir("conv_scrub") / "codex-home"
    home.mkdir()
    auth = home / "auth.json"
    auth.write_text("{}")
    unlink = Path.unlink

    def fail(path, *args, **kwargs):
        if path == auth:
            raise PermissionError("fixture")
        return unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail)
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="credential scrub"):
        await app.state.finish_cli_release("conv_scrub")
    assert "credential scrub" in caplog.text


@pytest.mark.parametrize("source_available", [False, True])
async def test_codex_resume_recopies_scrubbed_stores_and_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_available: bool
) -> None:
    from omnigent.harnesses.codex_native import bridge
    from omnigent.inner.codex_executor import _populate_codex_home_config

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "codex")
    home = bridge.prepare_bridge_dir("conv_scrub") / "codex-home"
    source = tmp_path / "source"
    home.mkdir()
    source.mkdir()
    names = ("auth.json", ".credentials.json", "config.toml")
    for name in names:
        (source / name).write_text('model = "old"' if name.endswith("toml") else "{}")

    def no_link(*args, **kwargs):
        raise OSError("fixture: copy fallback")

    monkeypatch.setattr(Path, "symlink_to", no_link)
    _populate_codex_home_config(home, source)
    for name in names:
        replacement = source / "replacement"
        replacement.write_text('model = "fresh"' if name.endswith("toml") else '{"fresh":true}')
        replacement.replace(source / name)
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    await app.state.finish_cli_release("conv_scrub")
    assert all(not (home / name).exists() for name in names)
    if not source_available:
        source.rename(tmp_path / "unmounted-source")
    _populate_codex_home_config(home, source)
    if source_available:
        assert all((home / name).read_text() == (source / name).read_text() for name in names)
    else:
        assert all(not (home / name).exists() for name in names)


async def test_session_delete_does_not_trust_old_ownership_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.codex_native import bridge

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "codex")
    unrelated = bridge.prepare_bridge_dir("unrelated")
    (unrelated / "session.id").write_text("conv_old")
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    async with _runner_client(app) as client:
        assert (await client.delete("/v1/sessions/conv_old")).status_code == 200
    assert unrelated.exists()
    await app.state.finish_cli_release("unrelated")
    assert unrelated.exists()


async def test_scrubbed_opencode_read_failure_has_no_old_credential(tmp_path, monkeypatch) -> None:
    from omnigent.harnesses.opencode_native import bridge

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "opencode")
    source = tmp_path / "source-auth.json"
    source.write_text("{}")
    monkeypatch.setattr(bridge, "user_opencode_auth_path", lambda: source)
    home = bridge.prepare_bridge_dir("conv_scrub")
    target = bridge.seed_opencode_auth(home)
    assert target is not None
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    await app.state.finish_cli_release("conv_scrub")
    assert not target.exists()
    monkeypatch.setattr(bridge.shutil, "copyfile", Mock(side_effect=PermissionError("fixture")))
    assert bridge.seed_opencode_auth(home) is None
    assert not target.exists()


async def test_idle_release_keeps_agy_native_oauth(tmp_path, monkeypatch) -> None:
    from omnigent.harnesses.antigravity_native import bridge

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "agy")
    home = bridge.prepare_bridge_dir("conv_oauth")
    native_oauth = bridge.agy_gemini_dir(home) / "oauth_creds.json"
    native_oauth.parent.mkdir(parents=True)
    native_oauth.write_text("{}")
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    await app.state.finish_cli_release("conv_oauth")
    assert native_oauth.exists()


@pytest.mark.parametrize(
    "family,history_path,label_key",
    [
        (
            "codex",
            "codex-home/sessions/2026/11/02/rollout-test-thread.jsonl",
            "omnigent.codex_native.bridge_id",
        ),
        ("pi", "sessions/session.jsonl", None),
        (
            "antigravity",
            "agy-home/.gemini/antigravity/conversations/session.pb",
            "omnigent.antigravity_native.bridge_id",
        ),
        (
            "opencode",
            "xdg-data/opencode/opencode.db",
            "omnigent.opencode_native.bridge_id",
        ),
        ("hermes", "hermes_home/state.db", None),
    ],
)
async def test_idle_release_history_still_expires_or_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    family: str,
    history_path: str,
    label_key: str | None,
) -> None:
    """Kept native histories retain their dead-owner TTL and explicit delete path."""
    from omnigent.native import native_bridge_common

    bridge = importlib.import_module(f"omnigent.harnesses.{family}_native.bridge")
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / family)
    session_id = "conv_retained_history"
    labels = {label_key: "rotated-bridge"} if label_key else {}
    server_client = NullServerClient()
    monkeypatch.setattr(
        server_client, "get", AsyncMock(return_value=httpx.Response(200, json={"labels": labels}))
    )
    bridge_id = "rotated-bridge" if label_key else session_id
    if family == "hermes":
        bridge.build_hermes_native_spawn_env(session_id)
        bridge_dir = bridge.bridge_dir_for_session_id(session_id)
    else:
        bridge_dir = bridge.prepare_bridge_dir(bridge_id)
    history = bridge_dir / history_path
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text("native history")
    if family == "codex":
        bridge.write_bridge_state(
            bridge_dir,
            bridge.CodexNativeBridgeState(
                session_id=session_id,
                socket_path=str(bridge_dir / "old.sock"),
                thread_id="parent_thread",
                codex_home=str(bridge.codex_home_for_bridge_dir(bridge_dir)),
            ),
        )
        bridge.update_active_child_turn(bridge_dir, "old_child", turn_id="old_turn")
    app = create_runner_app(server_client=server_client)  # type: ignore[arg-type]
    await app.state.finish_cli_release(session_id)
    assert history.read_text() == "native history"
    if family == "codex":
        assert not bridge.read_bridge_state(bridge_dir).helper_stop_pending
        assert bridge.read_active_child_turns(bridge_dir, strict=True) == {}

    now = 2_000_000_000.0
    monkeypatch.setattr(native_bridge_common.time, "time", lambda: now)
    for path in bridge_dir.rglob("*"):
        if path.is_file():
            os.utime(path, (now - 69 * 86400, now - 69 * 86400))
    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: True)
    assert bridge.prune_orphaned_bridge_dirs() == 0
    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)
    assert bridge.prune_orphaned_bridge_dirs() == 0
    assert history.read_text() == "native history"
    monkeypatch.setattr(native_bridge_common.time, "time", lambda: now + 2 * 86400)
    assert bridge.prune_orphaned_bridge_dirs() == 1
    assert not bridge_dir.exists()

    history.parent.mkdir(parents=True)
    history.write_text("new history")
    async with _runner_client(app) as client:
        deleted = await client.delete(f"/v1/sessions/{session_id}")
    assert deleted.status_code == 200
    assert not bridge_dir.exists()


@pytest.mark.parametrize("release_idle", [False, True])
@pytest.mark.parametrize("history_available", [False, True])
async def test_idle_release_preserves_codex_resume_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    release_idle: bool,
    history_available: bool,
) -> None:
    """Resume can replay an undelivered reply or fall back to its local rollout."""
    from omnigent.harnesses.codex_native import bridge, forwarder, main

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "codex-native")
    session_id = "conv_idle_resume"
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    bridge_dir = bridge.prepare_bridge_dir(session_id)
    codex_home = bridge.codex_home_for_bridge_dir(bridge_dir)
    rollout = main._codex_resume_rollout_path(codex_home, thread_id)
    rollout.parent.mkdir(parents=True)
    records = main._codex_rollout_records_from_session_items(
        [
            {
                "id": "reply",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "reply saved during outage"}],
            }
        ],
        session_id=session_id,
        external_session_id=thread_id,
        cwd=Path("/opt/work/project"),
        model_provider="test",
        cli_version="0.0.0",
    )
    rollout.write_text("".join(json.dumps(record) + "\n" for record in records))
    replay_data = {
        "item_type": "message",
        "item_data": {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "reply saved during outage"}],
        },
        "response_id": "turn_recovered",
        "source_id": "codex:item:recovered",
    }
    forwarder.append_dead_letter(
        bridge_dir,
        session_id=session_id,
        event_type="external_conversation_item",
        payload=replay_data,
        reason="http 503",
        http_status=503,
    )
    server_items: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/resources/files"):
            return httpx.Response(200, json={"data": [], "has_more": False})
        if not history_available:
            return httpx.Response(503)
        if request.method == "POST":
            body = json.loads(request.content)
            server_items.append(
                {"id": "recovered", "type": "message", **body["data"]["item_data"]}
            )
            return httpx.Response(200)
        return httpx.Response(200, json={"data": server_items, "has_more": False})

    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    app.state.native_pane_reaper = _FakePaneReaper()
    if release_idle:
        async with _runner_client(app) as client:
            snapshot = await client.get(
                f"/v1/sessions/{session_id}/cli-retention",
                params={"idle_threshold_seconds": 60, "host_id": "host-a", "policy_revision": 1},
            )
            released = await client.post(
                f"/v1/sessions/{session_id}/cli-retention/release",
                json={
                    "reason": "idle_pool_overflow",
                    "idle_threshold_seconds": 60,
                    "expected_activity_token": "pane:activity-1",
                    "runtime_generation": snapshot.json()["runtime_generation"],
                    "host_id": "host-a",
                    "policy_revision": 1,
                },
            )
        assert released.json()["status"] == "released"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        resumed = await main._ensure_local_codex_resume_rollout(
            client,
            session_id=session_id,
            external_session_id=thread_id,
            codex_home=codex_home,
            workspace=Path("/opt/work/project"),
            model_provider="test",
            codex_path=None,
        )
    assert "reply saved during outage" in resumed.read_text()
    if history_available:
        assert server_items, "the undelivered reply must reach server history before resume"


@pytest.mark.parametrize("stale_child_turn", [False, True])
async def test_idle_release_does_not_revoke_resumed_codex_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_child_turn: bool
) -> None:
    """A retired backend's helper state must not reach its resumed native session."""
    from omnigent.harnesses.codex_native import bridge

    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "codex-native")
    session_id = "conv_helper_resume"
    bridge_dir = bridge.prepare_bridge_dir(session_id)
    history = bridge.codex_home_for_bridge_dir(bridge_dir) / "sessions" / "rollout.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text("saved native history")
    old_state = bridge.CodexNativeBridgeState(
        session_id=session_id,
        socket_path=str(bridge_dir / "old.sock"),
        thread_id="parent_thread",
        codex_home=str(bridge.codex_home_for_bridge_dir(bridge_dir)),
    )
    bridge.write_bridge_state(bridge_dir, old_state)
    bridge.clear_active_child_turns(bridge_dir)
    if stale_child_turn:
        bridge.update_active_child_turn(
            bridge_dir, "old_child", session_id="conv_child", turn_id="old_turn"
        )
    else:
        bridge.set_helper_stop_pending(bridge_dir, True)

    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    app.state.native_pane_reaper = _FakePaneReaper()
    async with _runner_client(app) as client:
        snapshot = await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={"idle_threshold_seconds": 60, "host_id": "host-a", "policy_revision": 1},
        )
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={
                "reason": "idle_pool_overflow",
                "idle_threshold_seconds": 60,
                "expected_activity_token": "pane:activity-1",
                "runtime_generation": snapshot.json()["runtime_generation"],
                "host_id": "host-a",
                "policy_revision": 1,
            },
        )
    assert released.json()["status"] == "released"
    assert history.read_text() == "saved native history"
    bridge.write_bridge_state(bridge_dir, old_state)
    resumed = bridge.read_bridge_state(bridge_dir)
    assert resumed is not None
    assert (
        resumed.helper_stop_pending,
        bridge.read_active_child_turns(bridge_dir, strict=True),
    ) == (
        False,
        {},
    )


class _FakePaneReaper:
    def __init__(self) -> None:
        self.managed: list[str] = []
        self.released: list[tuple[str, float, str]] = []
        self.released_now: list[str] = []

    def manage(self, conversation_id: str) -> None:
        self.managed.append(conversation_id)

    async def retention_snapshot(self, conversation_id: str, *, idle_threshold_s: float):
        del idle_threshold_s
        return {
            "family": "claude",
            "busy": False,
            "eligible": True,
            "idle_seconds": 120.0,
            "activity_token": "activity-1",
        }

    async def release_if_idle(
        self,
        conversation_id: str,
        *,
        idle_threshold_s: float,
        expected_activity_token: str,
    ) -> str:
        self.released.append((conversation_id, idle_threshold_s, expected_activity_token))
        return "released"

    async def release_now(self, conversation_id: str) -> str:
        self.released_now.append(conversation_id)
        return "released"

    def unmanage(self, conversation_id: str) -> None:
        if conversation_id in self.managed:
            self.managed.remove(conversation_id)

    def note_activity(self, conversation_id: str) -> None:
        del conversation_id


class _MissingPaneReaper(_FakePaneReaper):
    async def retention_snapshot(self, conversation_id: str, *, idle_threshold_s: float):
        del conversation_id, idle_threshold_s


@pytest.mark.asyncio
async def test_runner_reports_and_conditionally_releases_idle_native_cli() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "a1b2c3d4e5f61234567890abcdef0123"

    async with _runner_client(app) as client:
        snapshot = await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 7,
            },
        )
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={
                "reason": "idle_pool_overflow",
                "idle_threshold_seconds": 60,
                "expected_activity_token": "pane:activity-1",
                "runtime_generation": snapshot.json()["runtime_generation"],
                "host_id": "host-a",
                "policy_revision": 7,
            },
        )

    assert snapshot.status_code == 200
    assert snapshot.json()["eligible"] is True
    assert snapshot.json()["family"] == "claude"
    assert snapshot.json()["activity_token"] == "pane:activity-1"
    assert snapshot.json()["policy_revision"] == 7
    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert reaper.managed == [session_id]
    assert pm.managed_for_retention_calls == [session_id]
    assert session_id not in pm.managed_for_retention
    assert reaper.released == [(session_id, 60.0, "activity-1")]
    assert pm.released == [session_id]


@pytest.mark.asyncio
async def test_runner_archive_release_is_status_independent() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "b1b2c3d4e5f61234567890abcdef0123"

    async with _runner_client(app) as client:
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={"reason": "archive"},
        )

    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert reaper.released_now == [session_id]
    assert pm.released == [session_id]


@pytest.mark.asyncio
async def test_runner_reports_cli_release_failure_when_harness_process_survives() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm.release = AsyncMock(return_value=False)  # type: ignore[method-assign]
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="harness subprocess did not exit"):
        await app.state.finish_cli_release("session-a")


@pytest.mark.asyncio
async def test_runner_keeps_archive_fence_until_newer_unarchive_revision() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "ba1b2c3d4e5f61234567890abcdef012"

    async with _runner_client(app) as client:
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={"reason": "archive", "archive_revision": 3},
        )
        blocked = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "message", "role": "user", "content": []},
        )
        stale = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/archive-state",
            json={
                "archive_scope_id": session_id,
                "archive_revision": 2,
                "archived": False,
            },
        )
        cleared = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/archive-state",
            json={
                "archive_scope_id": session_id,
                "archive_revision": 4,
                "archived": False,
            },
        )

    assert released.status_code == 200
    assert blocked.status_code == 409
    assert blocked.json()["error"] == "session_archived"
    assert stale.status_code == 409
    assert cleared.status_code == 200
    assert app.state.cli_runtime_lifecycle.runtime_start_allowed(session_id)


@pytest.mark.asyncio
async def test_newer_unarchive_wins_while_old_archive_waits_to_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runner.native.interrupt import NativeInterruptRunner

    interrupt_started = asyncio.Event()
    allow_interrupt = asyncio.Event()

    async def _parked_stop(self, harness, session_id):
        del self, harness, session_id
        interrupt_started.set()
        await allow_interrupt.wait()

    monkeypatch.setattr(NativeInterruptRunner, "stop", _parked_stop)
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "bb1b2c3d4e5f61234567890abcdef012"

    async with _runner_client(app) as client:
        old_archive = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/cli-retention/release",
                json={
                    "reason": "archive",
                    "archive_scope_id": "root",
                    "archive_revision": 3,
                },
            )
        )
        await interrupt_started.wait()
        cleared = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/archive-state",
            json={
                "archive_scope_id": "root",
                "archive_revision": 4,
                "archived": False,
            },
        )
        allow_interrupt.set()
        stale = await old_archive

    assert cleared.status_code == 200
    assert stale.status_code == 409
    assert stale.json()["status"] == "stale_archive"
    assert reaper.released_now == []


@pytest.mark.asyncio
async def test_runner_does_not_retain_process_when_native_cli_is_absent() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _MissingPaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "c1b2c3d4e5f61234567890abcdef0123"
    pm.manage_for_retention(session_id)

    async with _runner_client(app) as client:
        snapshot = await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 3,
            },
        )

    assert snapshot.status_code == 200
    assert snapshot.json()["present"] is False
    assert session_id not in pm.managed_for_retention
    assert session_id not in reaper.managed


@pytest.mark.asyncio
async def test_runner_retains_and_releases_supported_resident_harness() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    pm.retention_snapshot_result = {
        "present": True,
        "supported": True,
        "family": "codex",
        "busy": False,
        "eligible": True,
        "idle_seconds": 120.0,
        "activity_token": "0:123.0",
    }
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    app.state.native_pane_reaper = None
    session_id = "d1b2c3d4e5f61234567890abcdef0123"

    async with _runner_client(app) as client:
        snapshot = await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 11,
            },
        )
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={
                "reason": "idle_pool_overflow",
                "idle_threshold_seconds": 60,
                "expected_activity_token": "harness:0:123.0",
                "runtime_generation": snapshot.json()["runtime_generation"],
                "host_id": "host-a",
                "policy_revision": 11,
            },
        )

    assert snapshot.json()["family"] == "codex"
    assert snapshot.json()["activity_token"] == "harness:0:123.0"
    assert released.json()["status"] == "released"
    assert pm.retention_releases == [(session_id, 60.0, "0:123.0")]


@pytest.mark.asyncio
async def test_runner_rejects_release_from_superseded_policy_revision() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "e1b2c3d4e5f61234567890abcdef0123"

    async with _runner_client(app) as client:
        await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 8,
            },
        )
        stale = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={
                "reason": "idle_pool_overflow",
                "idle_threshold_seconds": 60,
                "expected_activity_token": "pane:activity-1",
                "runtime_generation": "stale-boot:0",
                "host_id": "host-a",
                "policy_revision": 7,
            },
        )

    assert stale.status_code == 409
    assert stale.json()["status"] == "stale_policy"
    assert reaper.released == []


@pytest.mark.asyncio
async def test_runner_reset_returns_pane_and_harness_to_legacy_reapers() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "f1b2c3d4e5f61234567890abcdef0123"

    async with _runner_client(app) as client:
        await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 4,
            },
        )
        reset = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/reset",
            json={"host_id": "host-a", "policy_revision": 5},
        )

    assert reset.status_code == 200
    assert reset.json()["status"] == "reset"
    assert session_id not in reaper.managed
    assert session_id not in pm.managed_for_retention


@pytest.mark.asyncio
async def test_runner_rejects_reset_older_than_observed_policy() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "ab1b2c3d4e5f61234567890abcdef012"

    async with _runner_client(app) as client:
        await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 9,
            },
        )
        stale = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/reset",
            json={"host_id": "host-a", "policy_revision": 8},
        )

    assert stale.status_code == 409
    assert stale.json()["status"] == "stale_policy"
    assert session_id in reaper.managed
    assert session_id in pm.managed_for_retention


async def test_stale_get_does_not_manage_or_snapshot() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    snapshot = AsyncMock(wraps=reaper.retention_snapshot)
    note_activity = Mock()
    reaper.retention_snapshot = snapshot
    reaper.note_activity = note_activity
    app.state.native_pane_reaper = reaper
    session_id = "session-stale-get"
    async with _runner_client(app) as client:
        reset = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/reset",
            json={"host_id": "host-a", "policy_revision": 5},
        )
        stale = await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={"idle_threshold_seconds": 60, "host_id": "host-a", "policy_revision": 4},
        )
    assert reset.status_code == 200
    assert stale.status_code == 409
    assert stale.json() == {"session_id": session_id, "status": "stale_policy"}
    assert reaper.managed == []
    assert pm.managed_for_retention_calls == []
    snapshot.assert_not_awaited()
    note_activity.assert_not_called()


async def test_cross_host_reset_rejects_without_unmanaging() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = _FakePaneReaper()
    app.state.native_pane_reaper = reaper
    session_id = "session-cross-host-reset"
    async with _runner_client(app) as client:
        await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={"idle_threshold_seconds": 60, "host_id": "host-b", "policy_revision": 1},
        )
        stale = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/reset",
            json={"host_id": "host-a", "policy_revision": 7},
        )
    assert stale.status_code == 409
    assert stale.json()["status"] == "stale_host"
    assert session_id in reaper.managed
    assert session_id in pm.managed_for_retention
    assert app.state.cli_runtime_lifecycle.policy_matches(session_id, host_id="host-b", revision=1)


async def test_reset_without_stored_host_is_accepted() -> None:
    app = create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]
    session_id = "session-after-restart"
    async with _runner_client(app) as client:
        reset = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/reset",
            json={"host_id": "host-a", "policy_revision": 3},
        )
    assert reset.status_code == 200
    assert reset.json()["status"] == "reset"


class _AbsentHarnessProcessManager(_FakeProcessManager):
    """ProcessManager stub whose resident harness died before the release."""

    async def release_if_retention_idle(
        self,
        conversation_id: str,
        *,
        idle_threshold_s: float,
        expected_activity_token: str,
    ) -> str:
        self.retention_releases.append(
            (conversation_id, idle_threshold_s, expected_activity_token)
        )
        self.managed_for_retention.discard(conversation_id)
        return "absent"


@pytest.mark.asyncio
async def test_runner_idle_release_cleans_up_when_harness_died_first() -> None:
    """An "absent" harness release still runs the session-level cleanup tail.

    The harness died between snapshot and release
    (``release_if_retention_idle`` -> ``"absent"``), so there is no
    subprocess left to close — but the forwarder, relay, codex
    app-server teardown, bridge dirs, router and spawn family are
    session-level pieces no reaper covers, and the lifecycle must not
    keep reporting a dead runtime as live.
    """
    pm = _AbsentHarnessProcessManager(_ScriptedHarnessClient([]))
    pm.retention_snapshot_result = {
        "present": True,
        "supported": True,
        "family": "codex",
        "busy": False,
        "eligible": True,
        "idle_seconds": 120.0,
        "activity_token": "0:123.0",
    }
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    app.state.native_pane_reaper = None
    session_id = "a91b2c3d4e5f61234567890abcdef012"

    async with _runner_client(app) as client:
        snapshot = await client.get(
            f"/v1/sessions/{session_id}/cli-retention",
            params={
                "idle_threshold_seconds": 60,
                "host_id": "host-a",
                "policy_revision": 11,
            },
        )
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={
                "reason": "idle_pool_overflow",
                "idle_threshold_seconds": 60,
                "expected_activity_token": "harness:0:123.0",
                "runtime_generation": snapshot.json()["runtime_generation"],
                "host_id": "host-a",
                "policy_revision": 11,
            },
        )

    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert pm.released == [session_id]
    assert app.state.cli_runtime_lifecycle.phase(session_id) == "absent"


@pytest.mark.asyncio
async def test_runner_idle_release_keeps_native_attachment_cache() -> None:
    """An idle release keeps both attachment dirs of a resumable session.

    The session lives on after the runtime is released, and its native
    transcript still references the cached files by path; a native turn
    reads them as they are, with no restore step.
    """
    pm = _AbsentHarnessProcessManager(_ScriptedHarnessClient([]))
    pm.retention_snapshot_result = {
        "present": True,
        "supported": True,
        "family": "codex",
        "busy": False,
        "eligible": True,
        "idle_seconds": 120.0,
        "activity_token": "0:123.0",
    }
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    app.state.native_pane_reaper = None
    session_id = "b02c3d4e5f6a71234567890abcdef013"
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    cached = materialize_attachment(
        {
            "type": "input_file",
            "filename": "bundle.zip",
            "file_data": "data:application/zip;base64,UEsDBA==",
        },
        bridge_dir,
    )
    assert cached is not None
    by_path = session_attachment_dir(session_id) / "file_1" / "clip.mp4"
    by_path.parent.mkdir(parents=True)
    by_path.write_bytes(b"clip")

    try:
        async with _runner_client(app) as client:
            snapshot = await client.get(
                f"/v1/sessions/{session_id}/cli-retention",
                params={
                    "idle_threshold_seconds": 60,
                    "host_id": "host-a",
                    "policy_revision": 11,
                },
            )
            released = await client.post(
                f"/v1/sessions/{session_id}/cli-retention/release",
                json={
                    "reason": "idle_pool_overflow",
                    "idle_threshold_seconds": 60,
                    "expected_activity_token": "harness:0:123.0",
                    "runtime_generation": snapshot.json()["runtime_generation"],
                    "host_id": "host-a",
                    "policy_revision": 11,
                },
            )

        assert released.status_code == 200
        assert released.json()["status"] == "released"
        assert pm.released == [session_id]
        assert cached.exists()
        assert by_path.exists()
    finally:
        shutil.rmtree(attachment_cache_dir(bridge_dir), ignore_errors=True)
        shutil.rmtree(session_attachment_dir(session_id), ignore_errors=True)


async def _never_busy(pane: PaneRef) -> bool:
    del pane
    return False


async def _never_reap(pane: PaneRef) -> None:
    del pane
    raise AssertionError("absent pane must not be reaped")


@pytest.mark.asyncio
async def test_runner_idle_release_cleans_up_when_pane_vanished_first() -> None:
    """An "absent" pane release still runs cleanup and retires reaper state.

    The pane was closed between snapshot and release
    (``release_if_idle`` -> ``"absent"``). The session-level cleanup
    tail must still run, the lifecycle must not keep reporting the
    runtime as live, and the reaper must drop its management and idle
    clock entries for the gone pane.
    """
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    reaper = NativePaneReaper(
        list_native_panes=list,
        is_busy=_never_busy,
        reap=_never_reap,
    )
    app.state.native_pane_reaper = reaper
    session_id = "a92b2c3d4e5f61234567890abcdef012"
    reaper.manage(session_id)
    reaper.note_activity(session_id)
    lifecycle = app.state.cli_runtime_lifecycle
    lifecycle.observe_policy(session_id, host_id="host-a", revision=3)

    async with _runner_client(app) as client:
        released = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={
                "reason": "idle_pool_overflow",
                "idle_threshold_seconds": 60,
                "expected_activity_token": "pane:stale-token",
                "runtime_generation": lifecycle.runtime_token(session_id),
                "host_id": "host-a",
                "policy_revision": 3,
            },
        )

    assert released.status_code == 200
    assert released.json()["status"] == "released"
    assert pm.released == [session_id]
    assert lifecycle.phase(session_id) == "absent"
    assert session_id not in reaper._managed_conversations
    assert session_id not in reaper._last_busy_at


async def test_archive_release_does_not_run_tail_while_orphan_owes_it() -> None:
    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(process_manager=pm, server_client=NullServerClient())  # type: ignore[arg-type]
    lifecycle = app.state.cli_runtime_lifecycle
    reaper = NativePaneReaper(
        list_native_panes=list,
        is_busy=_never_busy,
        reap=_never_reap,
        runtime_token=lifecycle.runtime_token,
    )
    app.state.native_pane_reaper = reaper
    session_id = "session-owed-tail"
    instance = object()
    reaper._record_pending(
        PaneRef(session_id, "terminal:codex:main", "codex", Path("/tmp/test.sock"), instance),
        instance,
    )
    async with _runner_client(app) as client:
        archive = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/archive-state",
            json={"archive_scope_id": "root", "archive_revision": 1, "archived": True},
        )
        release = await client.post(
            f"/v1/sessions/{session_id}/cli-retention/release",
            json={"reason": "archive", "archive_scope_id": "root", "archive_revision": 1},
        )
    assert archive.status_code == 200
    assert release.status_code == 500
    assert release.json()["status"] == "failed"
    assert pm.released == []
