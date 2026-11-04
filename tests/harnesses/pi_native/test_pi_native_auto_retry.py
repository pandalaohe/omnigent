"""Run Pi's real input handler against the same record the Python executor writes."""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omnigent.native.auto_retry import (
    AutoRetryGuard,
    auto_retry_scope,
    observe_turn,
    prepare_submission,
    read_receipt,
)


@pytest.mark.parametrize("human_first", [True, False])
@pytest.mark.parametrize("helper_failed", [False, True, "epoch"])
def test_pi_input_hook_interleavings(tmp_path, human_first, helper_failed):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for resident Pi extension")
    snapshot = observe_turn(tmp_path, "child", "failed-turn", idle=True)
    guard = AutoRetryGuard(
        session_id="child",
        chain_id="c" * 32,
        input_id="a" * 32,
        expected_turn_id="failed-turn",
        expected_input_generation=4,
        expected_native_generation=snapshot.generation,
        admission_deadline=time.time() + 60,
        attempt=1,
        of=3,
    )
    with auto_retry_scope(guard):
        prepare_submission(tmp_path, native_idle=True)
    (tmp_path / "config.json").write_text(
        json.dumps(
            {
                "sessionId": "child",
                "bridgeDir": str(tmp_path),
                "autoRetryPython": sys.executable,
                "serverUrl": "http://omnigent.test",
            }
        )
    )
    script = r"""
const assert = require('assert').strict;
const fs = require('fs');
const path = require('path');
const [extensionPath, bridgeDir, humanFirst, helperFailed] = process.argv.slice(1);
const cp = require('child_process');
const realSpawn = cp.spawnSync;
const realWrite = fs.writeFileSync;
let failing = false;
let epochFailed = false;
cp.spawnSync = (...args) => failing ? {status: 1} : realSpawn(...args);
fs.writeFileSync = (name, ...args) => {
  const epochTemporary = String(name).includes('auto-retry-intervention')
    && String(name).endsWith('.tmp');
  if (epochFailed && epochTemporary)
    throw new Error('epoch replacement unavailable');
  return realWrite(name, ...args);
};
process.env.OMNIGENT_PI_NATIVE_CONFIG = path.join(bridgeDir, 'config.json');
const handlers = {};
const events = [];
global.fetch = async (_url, request) => {
  events.push(JSON.parse(request.body));
  return {ok: true, json: async () => ({})};
};
require(extensionPath)({registerCommand() {}, on(name, handler) {handlers[name] = handler;}});
const ctx = {ui: {notify() {}, setStatus() {}}};
const read = () => JSON.parse(fs.readFileSync(path.join(bridgeDir, 'auto-retry.json'), 'utf8'));
(async () => {
  if (humanFirst === 'false') {
    assert.notEqual((await handlers.input({text: 'retry'}, ctx))?.action, 'handled');
    assert.equal(read().receipt.state, 'admitted');
    assert.equal(read().receipt.guard.input_id, 'a'.repeat(32));
    await handlers.agent_start({}, ctx);
    await handlers.message_end({message: {role: 'user', content: 'retry', timestamp: 1}}, ctx);
    await new Promise(resolve => setImmediate(resolve));
    const automatic = events.find(e => e.type === 'external_conversation_item'
      && e.data.item_type === 'message');
    const running = events.find(e => e.type === 'external_session_status'
      && e.data.input_id === 'a'.repeat(32));
    assert.equal(automatic.data.input_id, 'a'.repeat(32));
    assert.equal(automatic.data.input_turn_id, running.data.turn_id);
    assert.equal(automatic.data.item_data.auto_retry.input_id, 'a'.repeat(32));
    await handlers.input({text: 'retry'}, ctx);
    await handlers.message_end({message: {role: 'user', content: 'retry', timestamp: 2}}, ctx);
    await new Promise(resolve => setImmediate(resolve));
    const echoes = events.filter(e => e.type === 'external_conversation_item'
      && e.data.item_type === 'message');
    assert.equal(echoes.length, 2);
    assert.equal(echoes[1].data.input_id, undefined);
    assert.equal(echoes[1].data.input_turn_id, undefined);
    assert.equal(echoes[1].data.item_data.auto_retry, undefined);
  }
  failing = helperFailed === 'true';
  epochFailed = helperFailed === 'epoch';
  const human = await handlers.input({text: 'please replace the request'}, ctx);
  failing = false;
  epochFailed = false;
  assert.notEqual(human?.action, 'handled');
  const generation = read().snapshot.generation;
  if (humanFirst === 'true') {
    assert.equal((await handlers.input({text: 'retry'}, ctx)).action, 'handled');
    assert.equal(read().snapshot.generation, generation);
  }
  if (helperFailed === 'false' || humanFirst === 'true')
    assert.equal(read().receipt.state, 'superseded');
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
    extension = (
        Path(__file__).resolve().parents[3]
        / "omnigent/resources/pi_native/omnigent_pi_native_extension.js"
    )
    result = subprocess.run(
        [
            node,
            "-e",
            script,
            str(extension),
            str(tmp_path),
            str(human_first).lower(),
            str(helper_failed).lower(),
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert read_receipt(tmp_path, guard.input_id).reason == "superseded_by_human_input"


def test_pi_turn_edges_survive_a_workspace_with_its_own_omnigent_package(tmp_path):
    """A Pi child working inside an omnigent checkout still reports its turn end."""
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for resident Pi extension")
    workspace = tmp_path / "workspace"
    shadow = workspace / "omnigent" / "native"
    shadow.mkdir(parents=True)
    (workspace / "omnigent" / "__init__.py").write_text("")
    (shadow / "__init__.py").write_text("")
    (shadow / "auto_retry.py").write_text("raise SystemExit('shadowed')\n")
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    (bridge / "config.json").write_text(
        json.dumps(
            {
                "sessionId": "child",
                "bridgeDir": str(bridge),
                "autoRetryPython": sys.executable,
                "serverUrl": "http://omnigent.test",
            }
        )
    )
    script = r"""
const assert = require("assert").strict;
const path = require("path");
const [extensionPath, bridgeDir] = process.argv.slice(1);
process.env.OMNIGENT_PI_NATIVE_CONFIG = path.join(bridgeDir, "config.json");
const posted = [];
global.fetch = async (_url, request) => {
  posted.push(JSON.parse(request.body));
  return {ok: true, status: 204};
};
global.setInterval = () => ({fakeInterval: true});
const handlers = {};
require(extensionPath)({registerCommand() {}, on(name, handler) {handlers[name] = handler;}});
const ctx = {ui: {setTitle() {}, setStatus() {}, notify() {}}};
(async () => {
  await handlers.agent_start({}, ctx);
  await handlers.message_end({message: {role: "assistant", timestamp: 2, stopReason: "stop",
    content: [{type: "text", text: "done"}]}}, ctx);
  await handlers.agent_end({}, ctx);
  const terminal = posted.filter(e => e.type === "external_session_status").at(-1).data;
  assert.equal(terminal.status, "idle");
  assert.equal(terminal.turn_outcome, "completed");
  await handlers.session_shutdown();
})().catch(error => { console.error(error); process.exit(1); });
"""
    extension = (
        Path(__file__).resolve().parents[3]
        / "omnigent/resources/pi_native/omnigent_pi_native_extension.js"
    )
    result = subprocess.run(
        [node, "-e", script, str(extension), str(bridge)],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
