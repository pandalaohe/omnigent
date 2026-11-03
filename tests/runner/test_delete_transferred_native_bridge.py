"""Session cleanup must exclude in-flight native ownership publication."""

from __future__ import annotations

import importlib
import json
import selectors
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.harnesses.antigravity_native import bridge as agy_bridge
from omnigent.harnesses.codex_native import bridge as codex_bridge
from omnigent.inner.native_attachments import materialize_attachment
from omnigent.native import native_bridge_common
from omnigent.runner.native.orchestration import _delete_native_bridge_dirs

_FAMILIES = ("codex", "antigravity")


@pytest.fixture(autouse=True)
def _isolate_native_roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """All candidates touched by cleanup and their caches stay disposable."""
    for family in (
        "antigravity",
        "claude",
        "codex",
        "cursor",
        "goose",
        "hermes",
        "kimi",
        "kiro",
        "opencode",
        "pi",
        "qwen",
    ):
        module = importlib.import_module(f"omnigent.harnesses.{family}_native.bridge")
        monkeypatch.setattr(module, "_BRIDGE_ROOT", tmp_path / family)
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))


def _bridge_dir(family: str, bridge_id: str) -> Path:
    module = codex_bridge if family == "codex" else agy_bridge
    return module.bridge_dir_for_bridge_id(bridge_id)


def _write_owner(family: str, bridge_dir: Path, session_id: str) -> None:
    if family == "codex":
        codex_bridge.write_bridge_state(
            bridge_dir,
            codex_bridge.CodexNativeBridgeState(
                session_id=session_id,
                socket_path="ws://127.0.0.1:12345",
                thread_id="thread_old",
                codex_home=str(bridge_dir / "codex-home"),
            ),
        )
    else:
        agy_bridge.write_bridge_state(
            bridge_dir,
            agy_bridge.AntigravityNativeBridgeState(
                session_id=session_id, conversation_id="cascade_old"
            ),
        )


def _cache_attachment(bridge_dir: Path) -> Path:
    cached = materialize_attachment(
        {"type": "input_file", "filename": "note.txt", "file_data": "data:text/plain;base64,aGk="},
        bridge_dir,
    )
    assert cached is not None
    return cached


@pytest.mark.parametrize("family", _FAMILIES)
async def test_cleanup_skips_bridge_and_cache_during_preparation(family: str) -> None:
    """Cleanup is nonblocking and leaves both paths while the stable lock is held."""
    bridge_dir = _bridge_dir(family, "conv_old")
    _write_owner(family, bridge_dir, "conv_old")
    cached = _cache_attachment(bridge_dir)
    with native_bridge_common.bridge_dir_preparation_lock(bridge_dir):
        await _delete_native_bridge_dirs(server_client=None, session_id="conv_old")
        assert (bridge_dir / "state.json").exists()
        assert cached.exists()
    await _delete_native_bridge_dirs(server_client=None, session_id="conv_old")
    assert not bridge_dir.exists()
    assert not cached.exists()


_PAUSED_OWNER_WRITER = """
import importlib
import os
import sys
from pathlib import Path

family, directory = sys.argv[1:]
bridge_dir = Path(directory)
bridge = importlib.import_module(f"omnigent.harnesses.{family}_native.bridge")
replace = os.replace
def paused_replace(source, destination):
    if Path(destination) == bridge_dir / "state.json":
        print("ready", flush=True)
        assert sys.stdin.readline() == "release\\n"
    return replace(source, destination)
os.replace = paused_replace
if family == "codex":
    state = bridge.CodexNativeBridgeState(
        session_id="conv_new", socket_path="ws://127.0.0.1:12345",
        thread_id="thread_new", codex_home=str(bridge_dir / "codex-home"),
    )
else:
    state = bridge.AntigravityNativeBridgeState(
        session_id="conv_new", conversation_id="cascade_new",
    )
bridge.write_bridge_state(bridge_dir, state)
"""


@pytest.mark.parametrize("family", _FAMILIES)
async def test_cleanup_preserves_inflight_owner_write_in_another_process(family: str) -> None:
    """A supported writer's lock covers publication while the old owner is visible."""
    bridge_dir = _bridge_dir(family, "conv_old")
    _write_owner(family, bridge_dir, "conv_old")
    cached = _cache_attachment(bridge_dir)
    process = subprocess.Popen(
        [sys.executable, "-c", _PAUSED_OWNER_WRITER, family, str(bridge_dir)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=10), "owner writer did not reach publication"
        assert process.stdout.readline().strip() == "ready"
        assert json.loads((bridge_dir / "state.json").read_text())["session_id"] == "conv_old"
        await _delete_native_bridge_dirs(server_client=None, session_id="conv_old")
        assert (bridge_dir / "state.json").exists()
        assert cached.exists()
    finally:
        try:
            _, stderr = process.communicate("release\n", timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise
    assert process.returncode == 0, stderr
    assert json.loads((bridge_dir / "state.json").read_text())["session_id"] == "conv_new"
    await _delete_native_bridge_dirs(server_client=None, session_id="conv_old")
    assert cached.exists()
