"""Regression tests for ``DELETE /v1/sessions/{id}`` session cleanup.

Covers two independent problems:

#1350 — native bridge dirs leak on session delete.
Each native session's bridge dir holds a per-conversation bridge token + MCP
config (secret material). ``DELETE /v1/sessions/{id}`` closes the pane but
historically never removed this SEPARATE dir, so token-bearing
``/tmp/omnigent-*`` (and ``~/.omnigent``) dirs accumulated even on a clean
delete. The delete path must now ``rmtree`` it — for ALL 11 native families,
not just the original 5.

#3728 — spec-fill generation guard prevents ABA cache repopulation.
Session ids are caller-supplied and can be reused.  A spec fill parked across
``delete_session`` + same-id recreate must NOT write its stale result into the
new session's cache.  ``_session_cache_generations`` is incremented (never
popped) on every delete so that a fill that captured generation N before the
delete is rejected by the N+1 current value.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
import sys
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.harnesses.antigravity_native.bridge import (
    bridge_dir_for_bridge_id as antigravity_bridge_dir,
)
from omnigent.harnesses.claude_native.bridge import bridge_dir_for_bridge_id, prepare_bridge_dir
from omnigent.harnesses.claude_native.bridge import (
    bridge_dir_for_bridge_id as claude_bridge_dir,
)
from omnigent.harnesses.codex_native.bridge import (
    bridge_dir_for_bridge_id as codex_bridge_dir,
)
from omnigent.harnesses.cursor_native.bridge import (
    bridge_dir_for_session_id as cursor_bridge_dir,
)
from omnigent.harnesses.goose_native.bridge import (
    bridge_dir_for_session_id as goose_bridge_dir,
)
from omnigent.harnesses.hermes_native.bridge import (
    bridge_dir_for_session_id as hermes_bridge_dir,
)
from omnigent.harnesses.kimi_native.bridge import (
    bridge_dir_for_session_id as kimi_bridge_dir,
)
from omnigent.harnesses.kiro_native.bridge import (
    bridge_dir_for_session_id as kiro_bridge_dir,
)
from omnigent.harnesses.opencode_native.bridge import (
    bridge_dir_for_bridge_id as opencode_bridge_dir,
)
from omnigent.harnesses.pi_native.bridge import (
    bridge_dir_for_session_id as pi_bridge_dir,
)
from omnigent.harnesses.qwen_native.bridge import (
    bridge_dir_for_session_id as qwen_bridge_dir,
)
from omnigent.inner.native_attachments import (
    attachment_cache_dir,
    materialize_attachment,
    session_attachment_dir,
)
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _runner_client
from tests.runner.helpers import NullServerClient

# One resolver per native family — the session_id-keyed dir each harness leaves
# behind. _delete_native_bridge_dirs falls back to session_id for every family
# (label-rotated ids resolve to the same dir under the NullServerClient stub),
# so keying purely on session_id exercises the cleanup for all 11.
BRIDGE_DIR_RESOLVERS: dict[str, Callable[[str], Path]] = {
    "antigravity": antigravity_bridge_dir,
    "claude": claude_bridge_dir,
    "codex": codex_bridge_dir,
    "cursor": cursor_bridge_dir,
    "goose": goose_bridge_dir,
    "hermes": hermes_bridge_dir,
    "kimi": kimi_bridge_dir,
    "kiro": kiro_bridge_dir,
    "opencode": opencode_bridge_dir,
    "pi": pi_bridge_dir,
    "qwen": qwen_bridge_dir,
}


@pytest.fixture
def app() -> FastAPI:
    """Build a runner app with a stub server client."""
    return create_runner_app(server_client=NullServerClient())  # type: ignore[arg-type]


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an HTTP client bound to the runner app via ASGI transport."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as c:
        yield c


async def test_delete_session_removes_native_bridge_dir(
    client: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    """``DELETE`` must remove the token-bearing claude-native bridge dir."""
    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = prepare_bridge_dir(session_id, workspace=tmp_path)
    assert bridge_dir == bridge_dir_for_bridge_id(session_id)
    # prepare_bridge_dir writes a bridge.json holding the bridge token.
    assert (bridge_dir / "bridge.json").exists()
    cached = materialize_attachment(
        {
            "type": "input_file",
            "filename": "hello.txt",
            "file_data": "data:text/plain;base64,aGk=",
        },
        bridge_dir,
    )
    assert cached is not None

    resp = await client.delete(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200

    assert not bridge_dir.exists(), "bridge dir (with token) must be deleted"
    assert not cached.exists()


async def test_delete_session_removes_session_attachment_dir(
    client: httpx.AsyncClient,
) -> None:
    """Design scenario 8: the by-path host copies go with the session."""
    session_id = f"conv_{uuid.uuid4().hex}"
    attachment_dir = session_attachment_dir(session_id)
    target = attachment_dir / "file_1" / "clip.mp4"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"hello")

    resp = await client.delete(f"/v1/sessions/{session_id}")
    assert resp.status_code == 200

    assert not attachment_dir.exists(), "session attachment dir must be deleted"


@pytest.mark.parametrize("family", ["codex", "antigravity", "claude"])
@pytest.mark.parametrize("labelled", [True, False])
@pytest.mark.parametrize("cleanup_suffix", ["", "/resources"])
async def test_delete_old_session_preserves_transferred_bridge(
    tmp_path: Path, family: str, labelled: bool, cleanup_suffix: str
) -> None:
    """Deleting either stale bridge reference must leave its current owner intact."""
    from omnigent.harnesses.antigravity_native.bridge import (
        AntigravityNativeBridgeState,
    )
    from omnigent.harnesses.antigravity_native.bridge import (
        write_bridge_state as write_antigravity_state,
    )
    from omnigent.harnesses.claude_native.bridge import write_active_session_id
    from omnigent.harnesses.codex_native.bridge import (
        CodexNativeBridgeState,
    )
    from omnigent.harnesses.codex_native.bridge import (
        read_bridge_state as read_codex_state,
    )
    from omnigent.harnesses.codex_native.bridge import (
        write_bridge_state as write_codex_state,
    )

    old_id, new_id = f"conv_{uuid.uuid4().hex}", f"conv_{uuid.uuid4().hex}"
    bridge_id = f"bridge_{uuid.uuid4().hex}" if labelled else old_id
    bridge_dir = BRIDGE_DIR_RESOLVERS[family](bridge_id)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    if family == "codex":
        write_codex_state(
            bridge_dir,
            CodexNativeBridgeState(
                session_id=old_id,
                socket_path="ws://127.0.0.1:9876",
                thread_id="thread_new",
                codex_home=str(bridge_dir / "codex-home"),
            ),
        )
    elif family == "antigravity":
        write_antigravity_state(
            bridge_dir,
            AntigravityNativeBridgeState(session_id=new_id, conversation_id="cascade_new"),
        )
    else:
        prepare_bridge_dir(old_id, bridge_id=bridge_id, workspace=tmp_path)
        write_active_session_id(bridge_dir, new_id)
    history = bridge_dir / "transcript.jsonl"
    history.write_text("history")
    config = bridge_dir / "codex-home" / "config.toml"
    if family == "codex":
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('model = "example-model"')
    cached = materialize_attachment(
        {"type": "input_file", "filename": "hello.txt", "file_data": "data:;base64,aGk="},
        bridge_dir,
    )
    assert cached is not None
    label_key = f"omnigent.{family}_native.bridge_id"
    if family == "claude":
        from omnigent.harnesses.claude_native.bridge import BRIDGE_ID_LABEL_KEY

        label_key = BRIDGE_ID_LABEL_KEY

    def labels(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/labels"):
            present = labelled or new_id in request.url.path
            return httpx.Response(200, json={"labels": {label_key: bridge_id} if present else {}})
        if request.method == "GET":
            return httpx.Response(200, json={"agent_id": "agent", "runner_id": "runner"})
        if request.url.path.endswith("/successor"):
            return httpx.Response(200, json={"id": new_id})
        if request.method == "PATCH" and request.url.path.endswith(old_id):
            # A failed unbind leaves the old session deletable through this runner.
            return httpx.Response(503, json={})
        if request.method == "PATCH" or request.url.path.endswith("/transfer"):
            return httpx.Response(200, json={})
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(labels), base_url="http://server"
    ) as server:
        if family == "codex":
            from omnigent.harnesses.codex_native.forwarder import (
                _create_thread_replacement_session,
            )

            assert (
                await _create_thread_replacement_session(
                    client=server,
                    old_session_id=old_id,
                    bridge_dir=bridge_dir,
                    app_server_url="ws://127.0.0.1:9876",
                    new_thread_id="thread_new",
                )
                == new_id
            )
            state = read_codex_state(bridge_dir)
            assert state is not None and state.session_id == new_id
        app = create_runner_app(server_client=server)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as runner:
            try:
                await app.state.finish_cli_release(old_id)
                if family == "codex":
                    assert config.read_text() == 'model = "example-model"'
                response = await runner.delete(f"/v1/sessions/{old_id}{cleanup_suffix}")
                assert response.status_code == 200, response.text
                assert history.read_text() == "history"
                assert cached.read_bytes() == b"hi"
                # The current owner's deletion still reclaims the shared data.
                response = await runner.delete(f"/v1/sessions/{new_id}")
                assert response.status_code == 200, response.text
                assert not bridge_dir.exists()
                assert not cached.exists()
            finally:
                shutil.rmtree(bridge_dir, ignore_errors=True)
                shutil.rmtree(attachment_cache_dir(bridge_dir), ignore_errors=True)


@pytest.mark.parametrize("family", ["codex", "antigravity", "claude"])
@pytest.mark.parametrize(
    "metadata", ["runtime-fields", "invalid-json", "encoding", "permission", "missing-owner"]
)
async def test_delete_preserves_unreadable_bridge_owner_and_cleans_other_families(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    family: str,
    metadata: str,
) -> None:
    """Bad ownership metadata protects one bridge without blocking independent cleanup."""
    session_id, successor_id = f"conv_{uuid.uuid4().hex}", f"conv_{uuid.uuid4().hex}"
    bridge_dir = BRIDGE_DIR_RESOLVERS[family](session_id)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    owner_file = bridge_dir / ("bridge.json" if family == "claude" else "state.json")
    owner_key = "active_session_id" if family == "claude" else "session_id"
    owner_file.write_text(json.dumps({owner_key: successor_id, "socket_path": None}))
    if metadata == "invalid-json":
        owner_file.write_text("{")
    elif metadata == "missing-owner":
        owner_file.write_text("{}")
    elif metadata == "encoding":
        owner_file.write_bytes(b"\xff")
    elif metadata == "permission":
        read_text = Path.read_text

        def deny_owner_read(path: Path, *args: Any, **kwargs: Any) -> str:
            if path == owner_file:
                raise PermissionError("test ownership read denied")
            return read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", deny_owner_read)
    history = bridge_dir / "history.jsonl"
    history.write_text("successor history")
    cache = attachment_cache_dir(bridge_dir)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / "attachment.txt").write_text("attachment")
    owned_dir = cursor_bridge_dir(session_id)
    owned_dir.mkdir(parents=True, exist_ok=True)
    (owned_dir / "history.jsonl").write_text("deleted session history")

    try:
        response = await client.delete(f"/v1/sessions/{session_id}")
        assert response.status_code == 200, response.text
        assert not owned_dir.exists(), "one unreadable owner must not block other families"
        assert history.read_text() == "successor history"
        assert (cache / "attachment.txt").read_text() == "attachment"
        if metadata != "runtime-fields":
            assert "preserving" in caplog.text.lower()
            assert str(bridge_dir) in caplog.text
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)
        shutil.rmtree(owned_dir, ignore_errors=True)


@pytest.mark.parametrize("family", ["codex", "antigravity", "claude"])
@pytest.mark.parametrize("metadata_present", [True, False])
async def test_delete_reclaims_owned_or_missing_metadata_bridge(
    client: httpx.AsyncClient, family: str, metadata_present: bool
) -> None:
    """An owned bridge needs no valid runtime fields; absent metadata keeps legacy cleanup."""
    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = BRIDGE_DIR_RESOLVERS[family](session_id)
    bridge_dir.mkdir(parents=True, exist_ok=True)
    if metadata_present:
        owner_file = bridge_dir / ("bridge.json" if family == "claude" else "state.json")
        owner_key = "conversation_id" if family == "claude" else "session_id"
        owner_file.write_text(json.dumps({owner_key: session_id, "socket_path": None}))
    cache = attachment_cache_dir(bridge_dir)
    cache.mkdir(parents=True, exist_ok=True)
    try:
        response = await client.delete(f"/v1/sessions/{session_id}")
        assert response.status_code == 200, response.text
        assert not bridge_dir.exists()
        assert not cache.exists()
    finally:
        shutil.rmtree(bridge_dir, ignore_errors=True)
        shutil.rmtree(cache, ignore_errors=True)


@pytest.mark.parametrize("suffix", ["", "/resources"])
async def test_cleanup_preserves_bridge_and_cache_while_metadata_writer_holds_lock(
    client: httpx.AsyncClient, tmp_path: Path, suffix: str
) -> None:
    """A busy metadata writer defers deletion; later cleanup can remove the owner."""
    from omnigent.native.native_bridge_common import bridge_dir_preparation_lock

    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = prepare_bridge_dir(session_id, workspace=tmp_path)
    attachment = materialize_attachment(
        {"type": "input_file", "filename": "input.txt", "file_data": "data:;base64,aGk="},
        bridge_dir,
    )
    assert attachment is not None
    config = (bridge_dir / "bridge.json").read_bytes()

    with bridge_dir_preparation_lock(bridge_dir):
        response = await client.delete(f"/v1/sessions/{session_id}{suffix}")
        assert response.status_code == 200
        assert (bridge_dir / "bridge.json").read_bytes() == config
        assert attachment.read_bytes() == b"hi"

    response = await client.delete(f"/v1/sessions/{session_id}{suffix}")
    assert response.status_code == 200
    assert not bridge_dir.exists()
    assert attachment.exists() == (suffix == "/resources")
    # Resource release retains transcript attachments; full deletion reclaims them.
    response = await client.delete(f"/v1/sessions/{session_id}")
    assert response.status_code == 200
    assert not attachment_cache_dir(bridge_dir).exists()


async def test_cleanup_preserves_bridge_while_separate_process_updates_owner(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    """A real metadata writer excludes cleanup across processes, then releases the lock."""
    from omnigent.harnesses.claude_native.bridge import read_active_session_id

    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = prepare_bridge_dir(session_id, workspace=tmp_path)
    attachment = materialize_attachment(
        {"type": "input_file", "filename": "input.txt", "file_data": "data:;base64,aGk="},
        bridge_dir,
    )
    assert attachment is not None
    config = (bridge_dir / "bridge.json").read_bytes()
    ready, release = tmp_path / "writer-ready", tmp_path / "writer-release"
    script = """
import sys
import time
from pathlib import Path
from omnigent.harnesses.claude_native import bridge

bridge_dir, ready, release = map(Path, sys.argv[1:4])
bridge._BRIDGE_ROOT = bridge_dir.parent
bridge._TRUSTED_PARENT = bridge_dir.parent
original_read = bridge._read_json_file

def paused_read(path):
    value = original_read(path)
    ready.touch()
    deadline = time.monotonic() + 30
    while not release.exists():
        if time.monotonic() > deadline:
            raise RuntimeError("writer release timed out")
        time.sleep(0.01)
    return value

bridge._read_json_file = paused_read
bridge.write_active_session_id(bridge_dir, sys.argv[4])
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(bridge_dir),
        str(ready),
        str(release),
        session_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(10):
            while not ready.exists():
                if process.returncode is not None:
                    _, stderr = await process.communicate()
                    pytest.fail(f"metadata writer exited before locking: {stderr.decode()}")
                await asyncio.sleep(0.01)
        response = await client.delete(f"/v1/sessions/{session_id}/resources")
        assert response.status_code == 200
        assert (bridge_dir / "bridge.json").read_bytes() == config
        assert attachment.read_bytes() == b"hi"
        release.touch()
        _, stderr = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 0, stderr.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.communicate()

    assert read_active_session_id(bridge_dir) == session_id
    response = await client.delete(f"/v1/sessions/{session_id}/resources")
    assert response.status_code == 200
    assert not bridge_dir.exists()
    assert attachment.read_bytes() == b"hi"
    response = await client.delete(f"/v1/sessions/{session_id}")
    assert response.status_code == 200
    assert not attachment_cache_dir(bridge_dir).exists()


async def test_cleanup_resources_without_cached_history_leaves_no_restore_flag(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """A session with no cached history has no ``[Attached:]`` lines to restore."""
    from omnigent.runner.app import _session_histories_ref

    session_id = f"conv_{uuid.uuid4().hex}"
    assert session_id not in _session_histories_ref

    resp = await client.delete(f"/v1/sessions/{session_id}/resources")

    assert resp.status_code == 200
    assert session_id not in app.state.attachment_restore_pending


async def test_cleanup_resources_with_cached_history_sets_the_restore_flag(
    app: FastAPI,
    client: httpx.AsyncClient,
) -> None:
    """Cleanup keeps the restore flag for a session whose cached history survives."""
    from omnigent.runner.app import _session_histories_ref

    session_id = f"conv_{uuid.uuid4().hex}"
    _session_histories_ref[session_id] = []
    try:
        resp = await client.delete(f"/v1/sessions/{session_id}/resources")

        assert resp.status_code == 200
        assert session_id in app.state.attachment_restore_pending
    finally:
        _session_histories_ref.pop(session_id, None)
        app.state.attachment_restore_pending.discard(session_id)


@pytest.mark.parametrize("family", sorted(BRIDGE_DIR_RESOLVERS))
@pytest.mark.parametrize("bridge_present", [True, False])
async def test_cleanup_resources_removes_native_bridge_dir(
    client: httpx.AsyncClient,
    family: str,
    bridge_present: bool,
) -> None:
    """Resource cleanup removes every family's bridge dir but keeps attachments.

    ``DELETE /v1/sessions/{id}/resources`` (``cleanup_session_resources``)
    releases a session that keeps running, so the token-bearing bridge dir must
    go (else the #1350 leak persists) while the attachment dirs its transcript
    reads stay. All 11 native families create such a dir.
    """
    session_id = f"conv_{uuid.uuid4().hex}"
    bridge_dir = BRIDGE_DIR_RESOLVERS[family](session_id)
    # Materialize the token-bearing dir the harness would have left behind.
    if bridge_present:
        bridge_dir.mkdir(parents=True, exist_ok=True)
        (bridge_dir / "bridge.json").write_text(json.dumps({"active_session_id": session_id}))
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
        resp = await client.delete(f"/v1/sessions/{session_id}/resources")
        assert resp.status_code == 200

        assert not bridge_dir.exists(), (
            f"{family} bridge dir must be deleted on the real /resources path"
        )
        assert attachment_cache_dir(bridge_dir).exists()
        assert by_path.exists()
    finally:
        shutil.rmtree(attachment_cache_dir(bridge_dir), ignore_errors=True)
        shutil.rmtree(session_attachment_dir(session_id), ignore_errors=True)


# ── #3728: spec-fill generation guard ───────────────────────────────────────

_AGENT_ID_GENERATION = "ag_test_generation"


class _AgentSnapshotServerClient(NullServerClient):
    """Server client whose session GET returns a valid agent_id."""

    def __init__(self, session_id: str, agent_id: str) -> None:
        self._session_id = session_id
        self._agent_id = agent_id

    class _SessionResp:
        status_code = 200

        def __init__(self, agent_id: str) -> None:
            self._payload = {"agent_id": agent_id, "created_at": 0}
            self.text = json.dumps(self._payload)

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            pass

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        if url.rstrip("/").endswith(self._session_id):
            return self._SessionResp(self._agent_id)
        return self._Response()


async def test_spec_fill_parked_across_delete_discards(tmp_path: Path) -> None:
    """A spec fill that starts before delete must not repopulate the cache.

    Sequence:
    1. Terminal launch triggers ``_resolve_session_spec_entry`` → spec
       resolver parks (generation captured = 0).
    2. ``DELETE /v1/sessions/{id}`` increments generation to 1.
    3. Spec resolver unparks → generation check: 1 ≠ 0 → discard.
    4. Second terminal launch triggers a fresh fill → resolver called again.

    If the generation guard were absent or broken (pop+reset to 0), the stale
    fill from step 1 would write its result and step 4 would NOT invoke the
    resolver a second time.
    """
    session_id = "conv_test_generation_aba"

    resolver_started = asyncio.Event()
    resolver_gate = asyncio.Event()
    resolve_count = 0

    async def _pausable_resolver(agent_id: str, sid: str | None = None) -> AgentSpec:
        nonlocal resolve_count
        resolve_count += 1
        resolver_started.set()
        await resolver_gate.wait()
        return AgentSpec(
            spec_version=1,
            name=agent_id,
            executor=ExecutorSpec(type="omnigent"),
        )

    app = create_runner_app(
        server_client=_AgentSnapshotServerClient(session_id, _AGENT_ID_GENERATION),  # type: ignore[arg-type]
        spec_resolver=_pausable_resolver,
    )

    async with _runner_client(app) as client:
        # Phase 1: start a terminal launch that parks in the spec resolver.
        launch_task = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "bash", "session_key": "main"},
            )
        )
        await asyncio.wait_for(resolver_started.wait(), timeout=5.0)

        # Phase 2: delete the session — increments the generation counter.
        del_resp = await client.delete(f"/v1/sessions/{session_id}")
        assert del_resp.status_code == 200

        # Phase 3: release the resolver — the fill completes and must discard
        # its result because the generation no longer matches.
        resolver_gate.set()
        await asyncio.wait_for(launch_task, timeout=5.0)

        count_after_stale_fill = resolve_count
        assert count_after_stale_fill >= 1, "resolver should have been called at least once"

        # Phase 4: trigger a second spec fill for the same session id.
        # If the stale fill had populated the cache, the resolver would NOT
        # be called again.  A correctly guarded fill leaves the cache empty,
        # so the next fill calls the resolver a second time.
        resolver_started.clear()
        resolver_gate.clear()

        launch_task2 = asyncio.create_task(
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "bash", "session_key": "main"},
            )
        )

        # Wait for the resolver to start again (proves a fresh fill began).
        try:
            await asyncio.wait_for(resolver_started.wait(), timeout=5.0)
        except TimeoutError:
            pytest.fail(
                "spec resolver was not called a second time — stale fill "
                "from before deletion may have repopulated the cache (ABA)"
            )

        resolver_gate.set()
        await asyncio.wait_for(launch_task2, timeout=5.0)

        assert resolve_count > count_after_stale_fill, (
            "resolver was not called after delete: stale fill may have "
            "populated the spec cache across the session boundary"
        )


# ── delete-vs-create race: in-flight init must be cancelled ──────────────────


async def test_delete_session_cancels_inflight_init() -> None:
    """Deleting a session cancels its pending initialization before resource teardown."""
    from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient

    session_id = f"conv_{uuid.uuid4().hex}"
    resolver_started = asyncio.Event()
    resolver_cancelled = asyncio.Event()

    async def parked_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        resolver_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            resolver_cancelled.set()
            raise
        raise AssertionError("unreachable")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=parked_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        create_request = asyncio.create_task(
            client.post("/v1/sessions", json={"session_id": session_id, "agent_id": "ag_x"})
        )
        try:
            await asyncio.wait_for(resolver_started.wait(), timeout=5.0)

            resp = await client.delete(f"/v1/sessions/{session_id}")

            assert resp.status_code == 200
            try:
                await asyncio.wait_for(resolver_cancelled.wait(), timeout=5.0)
            except TimeoutError:
                pytest.fail(
                    "delete_session left the in-flight session init running; "
                    "a startup racing a delete leaks whatever it registers next"
                )
        finally:
            create_request.cancel()
            with contextlib.suppress(BaseException):
                await create_request


async def test_delete_session_survives_a_failing_inflight_init(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An init failure during cancellation is logged without aborting deletion."""
    from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient

    session_id = f"conv_{uuid.uuid4().hex}"
    resolver_started = asyncio.Event()

    class _InitUnwindError(Exception):
        """A failure the init path does not handle, so it reaches the task."""

    async def exploding_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        resolver_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Use an error that initialization does not convert to an HTTP response.
            raise _InitUnwindError("init cleanup exploded") from None
        raise AssertionError("unreachable")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=exploding_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        create_request = asyncio.create_task(
            client.post("/v1/sessions", json={"session_id": session_id, "agent_id": "ag_x"})
        )
        try:
            await asyncio.wait_for(resolver_started.wait(), timeout=5.0)

            with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
                resp = await client.delete(f"/v1/sessions/{session_id}")

            assert resp.status_code == 200, (
                "a failing in-flight init aborted the delete; the teardown that "
                f"reaps native resources never ran (body={resp.text[:200]})"
            )
            assert any(
                record.name == "omnigent.runner.app"
                and "failed while being cancelled" in record.getMessage()
                for record in caplog.records
            ), "the init failure was swallowed silently instead of being logged"
        finally:
            create_request.cancel()
            with contextlib.suppress(BaseException):
                await create_request


async def test_delete_session_bounds_the_wait_on_a_slow_init_unwind(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deletion proceeds after the timeout if cancelled initialization is still unwinding."""
    from omnigent.runner import app as runner_app_mod
    from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient

    session_id = f"conv_{uuid.uuid4().hex}"
    resolver_started = asyncio.Event()
    unwind_started = asyncio.Event()
    unwind_release = asyncio.Event()

    # Initialization stays blocked until explicitly released below.
    monkeypatch.setattr(runner_app_mod, "_SESSION_INIT_CANCEL_TIMEOUT_S", 0.05)

    async def slow_unwind_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        resolver_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            unwind_started.set()
            await unwind_release.wait()
            raise
        raise AssertionError("unreachable")

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=slow_unwind_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        create_request = asyncio.create_task(
            client.post("/v1/sessions", json={"session_id": session_id, "agent_id": "ag_x"})
        )
        try:
            await asyncio.wait_for(resolver_started.wait(), timeout=5.0)

            with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
                try:
                    resp = await asyncio.wait_for(
                        client.delete(f"/v1/sessions/{session_id}"), timeout=5.0
                    )
                except TimeoutError:
                    pytest.fail(
                        "delete_session blocked on a cancelled init that never "
                        "finished unwinding; the wait must be bounded"
                    )

            assert resp.status_code == 200
            assert unwind_started.is_set(), "the init was never cancelled"
            assert not unwind_release.is_set(), (
                "test bug: the init was released before DELETE returned, so this "
                "run does not exercise the bound"
            )
            assert any(
                record.name == "omnigent.runner.app"
                and "did not finish within" in record.getMessage()
                for record in caplog.records
            ), "the bounded wait timed out without logging a warning"
        finally:
            unwind_release.set()
            create_request.cancel()
            with contextlib.suppress(BaseException):
                await create_request
