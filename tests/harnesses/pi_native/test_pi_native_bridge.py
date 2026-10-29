"""Tests for omnigent.harnesses.pi_native.bridge inbox enqueue contract."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from omnigent.harnesses.pi_native import bridge as pi_native_bridge
from omnigent.native import native_bridge_common
from omnigent.permission_tiers.host import HostPermissionState, write_host_permission_state


@pytest.mark.parametrize(
    "changed",
    [
        {"id": "old-change"},
        {"session_id": "other-session"},
        {"target_tier": "full"},
        {"expected_revision": 1},
        {"ok": False},
        {"active_tools": ["read", 1]},
    ],
)
def test_permission_ack_rejects_replayed_or_mismatched_switch(changed):
    ack = {
        "id": "current-change",
        "session_id": "session-1",
        "target_tier": "ask",
        "expected_revision": 2,
        "ok": True,
        "active_tools": ["read"],
    }
    assert pi_native_bridge._pi_permission_ack_matches(
        ack,
        change_id="current-change",
        session_id="session-1",
        target_tier="ask",
        revision=2,
    )
    assert not pi_native_bridge._pi_permission_ack_matches(
        {**ack, **changed},
        change_id="current-change",
        session_id="session-1",
        target_tier="ask",
        revision=2,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "proof_tier,proof_revision,accepted", [("auto", 1, False), ("ask", 2, True)]
)
async def test_permission_switch_requires_current_installation_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    proof_tier: str,
    proof_revision: int,
    accepted: bool,
) -> None:
    from omnigent.harnesses.pi_native import bridge as pi_bridge

    pi_bridge.prepare_permission_installation(
        tmp_path, session_id="session-1", harness="pi-native", extension=Path(__file__)
    )
    write_host_permission_state(
        tmp_path / "permission_state.json",
        HostPermissionState(
            "session-1", "test-host", str(tmp_path), "auto", 1, "switching", "switch-transaction"
        ),
    )
    expected = json.loads((tmp_path / "permission_expected.json").read_text())
    (tmp_path / "permission_installation.json").write_text(
        json.dumps(
            {
                **{
                    key: expected[key]
                    for key in (
                        "version",
                        "installation_id",
                        "session_id",
                        "harness",
                        "extension_sha256",
                    )
                },
                "tier": proof_tier,
                "revision": proof_revision,
                "tools_verified": True,
                "active_tools": ["read"],
            }
        )
    )
    (tmp_path / "permission_ack.json").write_text(
        json.dumps(
            {
                "id": "switch-1",
                "session_id": "session-1",
                "target_tier": "ask",
                "expected_revision": 1,
                "ok": True,
                "active_tools": ["read"],
            }
        )
    )
    monkeypatch.setattr(pi_bridge, "bridge_dir_for_session_id", lambda _session_id: tmp_path)
    monkeypatch.setattr(
        pi_bridge, "enqueue_permission_tier_change", lambda *_args, **_kwargs: "switch-1"
    )

    assert await pi_bridge.apply_pi_permission_tier("session-1", "pi-native", "ask") is accepted


@pytest.mark.parametrize("harness", ["pi-native", "pi"])
@pytest.mark.parametrize("scope", ["user", "project", "sdk", "unknown", "bridge", "local"])
def test_runtime_owned_user_extension_registry_reaches_host_gate(
    tmp_path: Path, harness: str, scope: str
):
    from omnigent.inner.pi_executor import _generate_extension_js
    from omnigent.permission_tiers.host import HostPermissionState, write_host_permission_state
    from omnigent.runner.permission_tiers import PermissionTierRuntime

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for Pi bridge verification")
    bridge = tmp_path / "bridge"
    (bridge / "inbox").mkdir(parents=True)
    package = tmp_path / "user-package"
    package.mkdir()
    (package / "package.json").write_text("{}")
    entry = package / "index.js"
    entry.write_text("// user-installed extension\n")
    state = HostPermissionState(
        "extension-session", "test-host", str(tmp_path), "auto", 1, "ready"
    )
    write_host_permission_state(bridge / "permission_state.json", state)
    tools = [{"name": "sys_os_write", "description": "Write", "parameters": {"type": "object"}}]
    if harness == "pi-native":
        extension, config = pi_native_bridge.write_extension_files(
            bridge,
            session_id=state.session_id,
            server_url="http://mock",
            conversation_url="http://mock/conversation",
            tools=tools,
        )
    else:
        extension = tmp_path / "omnigent_pi_active" / "omnigent_tools.js"
        extension.parent.mkdir()
        extension.write_text(
            _generate_extension_js(
                12345,
                tools,
                "test-token",
                permission_bridge_dir=bridge,
                session_id=state.session_id,
            )
        )
        pi_native_bridge.prepare_permission_installation(
            bridge, session_id=state.session_id, harness=harness, extension=extension
        )
        config = bridge / "config.json"
    script = r"""
const fs = require('fs');
const assert = require('assert').strict;
const { EventEmitter } = require('events');
let policyCalls = 0;
require('net').createConnection = (_options, connected) => {
  const socket = new EventEmitter();
  socket.destroy = () => {};
  socket.write = (line) => {
    const request = JSON.parse(line);
    if (request.kind === 'policy_eval') policyCalls++;
    queueMicrotask(() => socket.emit('data', JSON.stringify({ id: request.id,
      verdict: { block: true, reason: 'untrusted registration' } }) + '\n'));
  };
  queueMicrotask(connected);
  return socket;
};
process.env.OMNIGENT_PI_NATIVE_CONFIG = process.argv[2];
process.env.OMNIGENT_PI_PERMISSION_INLINE = process.argv[1];
global.setInterval = () => ({ unref() {} });
global.fetch = async (url) => {
  if (url.endsWith('/policies/evaluate')) policyCalls++;
  return { ok: true, status: 200, json: async () => ({
    result: 'POLICY_ACTION_DENY', reason: 'untrusted registration' }) };
};
const scope = process.argv[5];
const name = scope === 'user' ? 'google_search' : 'sys_os_write';
const tools = [{ name, sourceInfo: { scope, origin: 'package',
  source: 'npm:sample-extension', path: process.argv[3], baseDir: process.argv[4] } }];
if (scope === 'sdk') tools[0].sourceInfo = { scope: 'temporary', source: 'sdk',
  origin: 'top-level', path: '<sdk:sys_os_write>' };
if (scope === 'unknown') delete tools[0].sourceInfo;
if (scope === 'local') tools[0].sourceInfo = { scope: 'user', source: 'local',
  origin: 'top-level', path: process.argv[3] };
if (scope === 'bridge') tools[0].sourceInfo = { scope: 'temporary', source: 'inline',
  origin: 'top-level', path: '<inline:omnigent-permission>' };
const handlers = {};
let active = [name];
const pi = { on: (name, fn) => { handlers[name] = fn; }, registerTool() {}, registerCommand() {},
  getAllTools: () => tools, getActiveTools: () => active,
  setActiveTools: (names) => { active = names; } };
require(process.argv[1])(pi);
Promise.resolve(handlers.session_start({}, { isIdle: () => true })).then(async () => {
  if (scope === 'user') tools[0].sourceInfo.scope = 'project';
  const verdict = await handlers.tool_call({ toolName: name,
    toolCallId: 'changed-source', input: {} }, { isIdle: () => true });
  if (scope === 'bridge') {
    assert.equal(verdict, undefined);
    assert.equal(policyCalls, 0);
    return;
  }
  assert.equal(verdict.block, true);
  const expected = scope === 'user' ? /registration changed/ : /untrusted registration/;
  assert.match(verdict.reason, expected);
  assert.equal(policyCalls, scope === 'user' ? 0 : 1);
}).catch((err) => {
  console.error(err); process.exitCode = 1;
});
"""
    result = subprocess.run(
        [node, "-e", script, str(extension), str(config), str(entry), str(package), scope],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert pi_native_bridge.verify_pi_permission_installation(bridge, "auto")
    runtime = PermissionTierRuntime()
    runtime._states[state.session_id] = state
    runtime._paths[state.session_id] = bridge / "permission_state.json"
    runtime._harnesses[state.session_id] = harness
    binding, decision = runtime.inspect(
        state.session_id,
        call_id="search",
        name="google_search" if scope == "user" else "sys_os_write",
        arguments={"path": str(tmp_path / "note.txt"), "content": "x"},
        expected_revision=1,
    )
    allowed = scope in ("user", "bridge")
    assert decision.action.value == ("ALLOW" if allowed else "DENY"), decision.reason
    assert (
        runtime.authorize(
            state.session_id,
            call_id=binding.call_id,
            name=binding.name,
            arguments_digest=binding.arguments_digest,
            expected_revision=1,
        )
        == allowed
    )


def _inbox_files(bridge_dir: Path) -> list[str]:
    """Return the inbox's ``*.json`` filenames in the extension's read order.

    The Pi extension delivers files via ``readdirSync(...).sort()`` — plain
    lexicographic order — so sorting the names here mirrors delivery order.

    :param bridge_dir: A prepared Pi bridge directory.
    :returns: Sorted inbox filenames.
    """
    return sorted(p.name for p in (bridge_dir / "inbox").glob("*.json"))


def test_enqueue_preserves_send_order_across_types(tmp_path: Path) -> None:
    """Inbox filenames sort in enqueue order regardless of payload type.

    The extension delivers inbox files in lexicographic order. The payload id
    is a random uuid (no time ordering), and an ``interrupt_`` id sorts before
    a ``msg_`` id — so without an ordering prefix a message queued before an
    interrupt would be delivered *after* it. This pins that send order is
    preserved even when a message is followed by an interrupt.
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    first = pi_native_bridge.enqueue_user_message(bridge_dir, "first")
    interrupt = pi_native_bridge.enqueue_interrupt(bridge_dir)
    second = pi_native_bridge.enqueue_user_message(bridge_dir, "second")

    files = _inbox_files(bridge_dir)
    assert len(files) == 3, files
    # Delivery order must match enqueue order, not the lexicographic order of
    # the raw ids (where "interrupt_" would jump ahead of "msg_").
    assert first in files[0], files
    assert interrupt in files[1], files
    assert second in files[2], files


def test_enqueue_compact_payload_shape(tmp_path: Path) -> None:
    """A queued compact request round-trips as well-formed JSON the extension reads.

    The extension dispatches on ``payload.type == "compact"`` and feeds
    ``payload.custom_instructions`` (when present) to Pi's
    ``ctx.compact({customInstructions})``. The id must equal the returned
    ``compact_`` id (the extension dedups on it).
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    compact_id = pi_native_bridge.enqueue_compact(bridge_dir)

    (path,) = list((bridge_dir / "inbox").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["id"] == compact_id
    assert compact_id.startswith("compact_")
    assert payload["type"] == "compact"
    assert isinstance(payload["created_at"], (int, float))
    # No instructions given → key omitted so the extension uses Pi's default
    # summarisation rather than passing an empty customInstructions string.
    assert "custom_instructions" not in payload


def test_enqueue_compact_includes_custom_instructions(tmp_path: Path) -> None:
    """Non-blank custom instructions ride along; blank ones are dropped.

    Pi's ``compact({customInstructions})`` focuses the summary on the given
    text. An empty/whitespace string must be dropped so it doesn't override
    Pi's default summarisation with a no-op instruction.
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    pi_native_bridge.enqueue_compact(bridge_dir, "focus on the refactor")
    pi_native_bridge.enqueue_compact(bridge_dir, "   ")

    payloads = sorted(
        (json.loads(p.read_text(encoding="utf-8")) for p in (bridge_dir / "inbox").glob("*.json")),
        key=lambda payload: payload["created_at"],
    )
    assert len(payloads) == 2, payloads
    with_instructions = [p for p in payloads if "custom_instructions" in p]
    assert len(with_instructions) == 1, payloads
    assert with_instructions[0]["custom_instructions"] == "focus on the refactor"


def test_enqueue_model_change_payload_shape(tmp_path: Path) -> None:
    """A queued model change round-trips as well-formed JSON the extension reads.

    The extension dispatches on ``payload.type == "model_change"`` and resolves
    ``payload.model`` against ``ctx.modelRegistry`` before calling Pi's
    ``setModel``. The id must equal the returned ``model_change_`` id (the
    extension dedups on it).
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    model_change_id = pi_native_bridge.enqueue_model_change(
        bridge_dir, "databricks-claude-sonnet-4-6"
    )

    (path,) = list((bridge_dir / "inbox").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["id"] == model_change_id
    assert model_change_id.startswith("model_change_")
    assert payload["type"] == "model_change"
    assert payload["model"] == "databricks-claude-sonnet-4-6"
    assert isinstance(payload["created_at"], (int, float))


def test_enqueue_user_message_payload_shape(tmp_path: Path) -> None:
    """A queued user message round-trips as well-formed JSON the extension reads.

    The extension dedups on ``payload.id`` and delivers ``payload.content`` via
    ``pi.sendUserMessage``, so the id must equal the returned ``msg_`` id and
    the content must be preserved verbatim.
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    message_id = pi_native_bridge.enqueue_user_message(bridge_dir, "hello world")

    (path,) = list((bridge_dir / "inbox").glob("*.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["id"] == message_id
    assert payload["type"] == "user_message"
    assert payload["content"] == "hello world"
    assert isinstance(payload["created_at"], (int, float))


def test_enqueue_leaves_no_partial_tmp_files(tmp_path: Path) -> None:
    """Only the final ``.json`` lands in the inbox — no ``.tmp`` residue.

    The atomic temp-write-then-rename exists so the 250 ms poller never reads a
    half-written file. A leftover ``.tmp`` (or a non-``.json`` name) would mean
    the rename contract regressed.
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)

    pi_native_bridge.enqueue_user_message(bridge_dir, "x")
    pi_native_bridge.enqueue_interrupt(bridge_dir)

    names = [p.name for p in (bridge_dir / "inbox").iterdir()]
    assert all(n.endswith(".json") for n in names), names
    assert not any(n.endswith(".tmp") for n in names), names


def test_prepare_bridge_dir_is_owner_only(tmp_path: Path, monkeypatch) -> None:
    """The bridge dir and its inbox are created 0o700 (per-session isolation).

    The bearer token written alongside the inbox makes owner-only perms the
    isolation boundary between sessions sharing ``~/.omnigent/pi-native``.
    """
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")

    bridge_dir = pi_native_bridge.prepare_bridge_dir("conv_perms")

    assert stat.S_IMODE((bridge_dir).stat().st_mode) == 0o700
    assert stat.S_IMODE((bridge_dir / "inbox").stat().st_mode) == 0o700


def test_clear_inbox_drops_leftover_payloads(tmp_path: Path) -> None:
    """clear_inbox empties the queue so a relaunched Pi process can't replay.

    A fresh Pi process starts with an empty dedup set; without clearing, a
    payload a prior process left behind would replay into the new session.
    """
    bridge_dir = tmp_path / "bridge"
    (bridge_dir / "inbox").mkdir(parents=True)
    pi_native_bridge.enqueue_user_message(bridge_dir, "stale")
    pi_native_bridge.enqueue_interrupt(bridge_dir)
    assert _inbox_files(bridge_dir), "precondition: inbox has payloads"

    pi_native_bridge.clear_inbox(bridge_dir)

    assert _inbox_files(bridge_dir) == []
    # The inbox dir itself survives (only its contents are dropped).
    assert (bridge_dir / "inbox").is_dir()


def test_clear_inbox_is_a_noop_without_an_inbox(tmp_path: Path) -> None:
    """clear_inbox tolerates a bridge dir that has no inbox yet."""
    pi_native_bridge.clear_inbox(tmp_path / "nonexistent")  # must not raise


def test_write_extension_files_embeds_tools(tmp_path: Path) -> None:
    """write_extension_files embeds the tool list so the extension can register it.

    The runner builds the session's Omnigent tool surface (sys_* tools) and
    passes it to write_extension_files; the extension reads ``config.tools`` and
    registers each via ``pi.registerTool``. The config must round-trip the list
    verbatim so the schemas reach the Pi agent unchanged.
    """
    bridge_dir = tmp_path / "bridge"
    tools = [
        {
            "name": "sys_os_read",
            "description": "Read a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
        {
            "name": "sys_os_shell",
            "description": "Run a command",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "sys_session_send",
            "description": "Send a session message",
            "parameters": {"type": "object", "properties": {}},
        },
    ]

    _ext, cfg = pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_abc",
        server_url="http://omnigent.test/",
        conversation_url="http://omnigent.test/c/conv_abc",
        auth_headers={"Authorization": "Bearer t"},
        tools=tools,
    )

    payload = json.loads(cfg.read_text(encoding="utf-8"))
    assert payload["sessionId"] == "conv_abc"
    # serverUrl trailing slash is stripped so the extension can append paths.
    assert payload["serverUrl"] == "http://omnigent.test"
    assert payload["tools"] == tools
    assert payload["planVisibleTools"] == ["read", "manage_todo_list", "sys_os_read"]


def test_write_extension_files_defaults_tools_to_empty(tmp_path: Path) -> None:
    """Omitting tools writes an empty list (Pi runs with its built-ins only)."""
    bridge_dir = tmp_path / "bridge"

    _ext, cfg = pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_abc",
        server_url="http://omnigent.test",
        conversation_url="http://omnigent.test/c/conv_abc",
    )

    payload = json.loads(cfg.read_text(encoding="utf-8"))
    assert payload["tools"] == []
    assert payload["planVisibleTools"] == ["read", "manage_todo_list"]


def test_write_extension_files_replaces_startup_instructions_on_relaunch(tmp_path: Path) -> None:
    bridge_dir = tmp_path / "bridge"
    arguments = {
        "session_id": "conv_abc",
        "server_url": "http://omnigent.test",
        "conversation_url": "http://omnigent.test/c/conv_abc",
    }
    _, config = pi_native_bridge.write_extension_files(
        bridge_dir, **arguments, startup_instructions="author\n\nglobal"
    )
    assert json.loads(config.read_text(encoding="utf-8"))["startupInstructions"] == (
        "author\n\nglobal"
    )

    # A relaunch without composed text replaces the old config completely.
    pi_native_bridge.write_extension_files(bridge_dir, **arguments, startup_instructions="  ")
    assert "startupInstructions" not in json.loads(config.read_text(encoding="utf-8"))


@pytest.mark.parametrize("successor", [None, "conv_next"])
def test_relaunch_keeps_pending_pi_successor_for_same_session(
    tmp_path: Path, successor: str | None
) -> None:
    bridge_dir = tmp_path / "bridge"
    _, cfg = pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_old",
        server_url="http://omnigent.test",
        conversation_url="http://omnigent.test/c/conv_old",
    )
    payload = json.loads(cfg.read_text(encoding="utf-8"))
    payload["pendingRotationSource"] = "conv_old"
    if successor is not None:
        payload["pendingSuccessorId"] = successor
    cfg.write_text(json.dumps(payload), encoding="utf-8")

    pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_old",
        server_url="http://omnigent.test",
        conversation_url="http://omnigent.test/c/conv_old",
    )
    relaunched = json.loads(cfg.read_text(encoding="utf-8"))
    assert relaunched["pendingRotationSource"] == "conv_old"
    assert relaunched.get("pendingSuccessorId") == successor

    pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_other",
        server_url="http://omnigent.test",
        conversation_url="http://omnigent.test/c/conv_other",
    )
    other = json.loads(cfg.read_text(encoding="utf-8"))
    assert "pendingSuccessorId" not in other


def test_refresh_config_auth_headers_replaces_only_auth(tmp_path: Path) -> None:
    """Refreshing the bearer rewrites only ``authHeaders``, leaving the rest.

    The baked launch token dies with the ~1h Databricks OAuth lifetime; the
    runner re-mints it into ``config.json`` each turn and the extension
    re-reads per request. The rewrite must touch only ``authHeaders`` so
    ``serverUrl`` / ``tools`` (read once at startup) stay intact.
    """
    bridge_dir = tmp_path / "bridge"
    pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_abc",
        server_url="http://omnigent.test",
        conversation_url="http://omnigent.test/c/conv_abc",
        auth_headers={"Authorization": "Bearer stale"},
        tools=[{"name": "t"}],
    )

    assert (
        pi_native_bridge.refresh_config_auth_headers(bridge_dir, {"Authorization": "Bearer fresh"})
        is True
    )
    payload = json.loads(pi_native_bridge.config_path(bridge_dir).read_text(encoding="utf-8"))
    assert payload["authHeaders"] == {"Authorization": "Bearer fresh"}
    # Untouched: the static fields the extension reads once at startup.
    assert payload["serverUrl"] == "http://omnigent.test"
    assert payload["tools"] == [{"name": "t"}]


def test_refresh_config_auth_headers_noops(tmp_path: Path) -> None:
    """No-op (returns False) on empty headers, a missing config, or no change."""
    bridge_dir = tmp_path / "bridge"
    # Missing config: nothing to rewrite.
    assert pi_native_bridge.refresh_config_auth_headers(bridge_dir, {"a": "b"}) is False

    pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_abc",
        server_url="http://omnigent.test",
        conversation_url="http://omnigent.test/c/conv_abc",
        auth_headers={"Authorization": "Bearer x"},
    )
    # Empty headers (local/unauthenticated) must never blank out a live token.
    assert pi_native_bridge.refresh_config_auth_headers(bridge_dir, {}) is False
    payload = json.loads(pi_native_bridge.config_path(bridge_dir).read_text(encoding="utf-8"))
    assert payload["authHeaders"] == {"Authorization": "Bearer x"}
    # Identical headers: skip the rewrite.
    assert (
        pi_native_bridge.refresh_config_auth_headers(bridge_dir, {"Authorization": "Bearer x"})
        is False
    )


def test_refresh_config_auth_headers_preserves_launch_written_headers(tmp_path: Path) -> None:
    """Bearer refresh merges over existing headers; launch-written extras survive.

    On guest-on-shared-host runners the extension config is written at launch
    with both the OAuth bearer and an ``X-Omnigent-Runner-Tunnel-Token`` header
    needed for the extension's ``/events`` POSTs to be authorised as self-access.
    The per-turn bearer refresh must not wipe that header — it only knows about
    the fresh bearer, not the tunnel token.
    """
    bridge_dir = tmp_path / "bridge"
    pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_abc",
        server_url="http://omnigent.test",
        conversation_url="http://omnigent.test/c/conv_abc",
        auth_headers={
            "Authorization": "Bearer stale",
            "X-Omnigent-Runner-Tunnel-Token": "tunnel-tok",
        },
    )

    assert (
        pi_native_bridge.refresh_config_auth_headers(bridge_dir, {"Authorization": "Bearer fresh"})
        is True
    )

    payload = json.loads(pi_native_bridge.config_path(bridge_dir).read_text(encoding="utf-8"))
    # Bearer is updated to the fresh value.
    assert payload["authHeaders"]["Authorization"] == "Bearer fresh"
    # Tunnel token written at launch is preserved across the bearer rotation.
    assert payload["authHeaders"]["X-Omnigent-Runner-Tunnel-Token"] == "tunnel-tok"


def test_inject_relay_into_config_writes_relay_fields(tmp_path: Path) -> None:
    """inject_relay_into_config writes relayUrl and relayToken into config.json."""
    from omnigent.harnesses.pi_native import bridge as pi_native_bridge

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    pi_native_bridge.write_extension_files(
        bridge_dir,
        session_id="conv_test",
        server_url="http://127.0.0.1:6767",
        conversation_url="http://127.0.0.1:6767/c/conv_test",
        auth_headers={"Authorization": "Bearer old"},
    )

    updated = pi_native_bridge.inject_relay_into_config(
        bridge_dir, "http://127.0.0.1:9999", "relay-tok"
    )

    assert updated is True
    payload = json.loads(pi_native_bridge.config_path(bridge_dir).read_text(encoding="utf-8"))
    assert payload["relayUrl"] == "http://127.0.0.1:9999"
    assert payload["relayToken"] == "relay-tok"
    # Existing fields are preserved.
    assert "serverUrl" in payload


def test_inject_relay_into_config_noops_when_config_absent(tmp_path: Path) -> None:
    """inject_relay_into_config returns False when config.json is missing."""
    from omnigent.harnesses.pi_native import bridge as pi_native_bridge

    bridge_dir = tmp_path / "missing"
    bridge_dir.mkdir()
    assert pi_native_bridge.inject_relay_into_config(bridge_dir, "http://x", "tok") is False


def test_pi_native_env_unset_parses_operator_denylist() -> None:
    """Comma-separated names parse sorted and deduplicated; blanks drop."""
    environ = {
        "OMNIGENT_PI_ENV_UNSET": " DEEPSEEK_API_KEY,ANTHROPIC_AUTH_TOKEN,,ANTHROPIC_AUTH_TOKEN "
    }
    assert pi_native_bridge.pi_native_env_unset(environ) == [
        "ANTHROPIC_AUTH_TOKEN",
        "DEEPSEEK_API_KEY",
    ]


def test_pi_native_env_unset_absent_or_empty_strips_nothing() -> None:
    """Unset or empty denylist means no scrubbing (safe default)."""
    assert pi_native_bridge.pi_native_env_unset({}) == []
    assert pi_native_bridge.pi_native_env_unset({"OMNIGENT_PI_ENV_UNSET": ""}) == []
    assert pi_native_bridge.pi_native_env_unset({"OMNIGENT_PI_ENV_UNSET": "  , ,"}) == []


# ── owner-pid marker + orphan prune (bridge-dir reaping) ────────────────────


def test_prepare_bridge_dir_writes_owner_pid_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_bridge_dir records the creating pid so the periodic sweep can
    prune the dir only when its owner is provably dead."""
    import os

    from omnigent.harnesses.pi_native.bridge import prepare_bridge_dir

    monkeypatch.setattr("omnigent.harnesses.pi_native.bridge._BRIDGE_ROOT", tmp_path / "pi-native")

    bridge_dir = prepare_bridge_dir("conv_owner_marker")

    assert (bridge_dir / "owner.pid").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_prune_orphaned_bridge_dirs_only_removes_dead_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prune removes only provably-dead-owner dirs held past the 70-day window."""
    import os
    import subprocess
    import sys
    import time

    from omnigent.harnesses.pi_native.bridge import prune_orphaned_bridge_dirs

    root = tmp_path / "pi-native"
    root.mkdir(parents=True)
    monkeypatch.setattr("omnigent.harnesses.pi_native.bridge._BRIDGE_ROOT", root)

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    dead_dir = root / "deadowner"
    dead_dir.mkdir()
    dead_marker = dead_dir / "owner.pid"
    dead_marker.write_text(str(dead.pid), encoding="utf-8")
    now = time.time()
    expired_at = now - 71 * 24 * 60 * 60
    os.utime(dead_marker, (expired_at, expired_at))

    held_dir = root / "heldowner"
    held_dir.mkdir()
    held_marker = held_dir / "owner.pid"
    held_marker.write_text(str(dead.pid), encoding="utf-8")
    held_at = now - 8 * 24 * 60 * 60
    os.utime(held_marker, (held_at, held_at))

    live_dir = root / "liveowner"
    live_dir.mkdir()
    (live_dir / "owner.pid").write_text(str(os.getpid()), encoding="utf-8")

    unmarked_dir = root / "unmarked"
    unmarked_dir.mkdir()

    pruned = prune_orphaned_bridge_dirs()

    assert pruned == 1
    assert not dead_dir.exists()
    assert held_dir.exists()
    assert live_dir.exists()
    assert unmarked_dir.exists()


@pytest.mark.parametrize(
    ("recent_marker", "recent_session", "expected_pruned"),
    [(True, False, 0), (False, True, 0), (False, False, 1), (None, True, 0), (None, False, 1)],
)
def test_prune_uses_latest_pi_session_activity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recent_marker: bool | None,
    recent_session: bool,
    expected_pruned: int,
) -> None:
    import os

    root = tmp_path / "pi-native"
    bridge = root / "session"
    sessions = bridge / "sessions"
    sessions.mkdir(parents=True)
    marker = bridge / "owner.pid"
    if recent_marker is not None:
        marker.write_text("999999")
    native_session = sessions / "session.jsonl"
    native_session.write_text('{"type":"message","message":{"usage":{"totalTokens":800000}}}\n')
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", root)
    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)
    now = 2_000_000_000.0
    monkeypatch.setattr(pi_native_bridge.time, "time", lambda: now)
    expired = now - native_bridge_common.ORPHAN_RETENTION_SECONDS
    for path, recent in ((marker, recent_marker), (native_session, recent_session)):
        if recent is None:
            continue
        stamp = now - 60 if recent else expired
        os.utime(path, (stamp, stamp))
    if recent_marker is None:
        for path in (bridge, sessions):
            os.utime(path, (expired, expired))

    assert pi_native_bridge.prune_orphaned_bridge_dirs() == expected_pruned
    assert native_session.exists() is (expected_pruned == 0)


def test_prune_retains_pi_session_on_scan_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "pi-native"
    bridge = root / "session"
    bridge.mkdir(parents=True)
    marker = bridge / "owner.pid"
    marker.write_text("999999")
    # An unreadable or malformed sessions directory must not cost native history.
    (bridge / "sessions").write_text("not a directory")
    monkeypatch.setattr(pi_native_bridge, "_BRIDGE_ROOT", root)
    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)
    monkeypatch.setattr(pi_native_bridge.time, "time", lambda: 3_000_000_000.0)

    assert pi_native_bridge.prune_orphaned_bridge_dirs() == 0
    assert marker.exists()


def test_write_agent_instructions_replaces_symlink_and_clears_stale_text(tmp_path: Path) -> None:
    target = tmp_path / "unrelated.md"
    target.write_text("unchanged")
    path = tmp_path / "agent_instructions.md"
    path.symlink_to(target)
    instructions = "  Authored role. π\nKeep whitespace.  "

    assert pi_native_bridge.write_agent_instructions(tmp_path, instructions) == path
    assert not path.is_symlink()
    assert target.read_text() == "unchanged"
    assert path.read_text() == instructions
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))

    assert pi_native_bridge.write_agent_instructions(tmp_path, "Updated role") == path
    assert path.read_text() == "Updated role"
    assert pi_native_bridge.write_agent_instructions(tmp_path, None) is None
    assert not path.exists()
    assert pi_native_bridge.write_agent_instructions(tmp_path, None) is None
