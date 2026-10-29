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
global.fetch = async () => ({ok: true});
require(extensionPath)({registerCommand() {}, on(name, handler) {handlers[name] = handler;}});
const ctx = {ui: {notify() {}, setStatus() {}}};
const read = () => JSON.parse(fs.readFileSync(path.join(bridgeDir, 'auto-retry.json'), 'utf8'));
(async () => {
  if (humanFirst === 'false') {
    assert.notEqual((await handlers.input({text: 'retry'}, ctx))?.action, 'handled');
    assert.equal(read().receipt.state, 'admitted');
    assert.equal(read().receipt.guard.input_id, 'a'.repeat(32));
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
