"""Real stdio boundary for the bounded, non-prompting catalog probe."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shlex
import sys
import time

import psutil
import pytest

from omnigent.inner.acp_harness import probe_acp_model_options
from omnigent.onboarding.acp_auth import AcpAgentEntry

# The existing fixed-reply primitive has no configurable advertisement or auth/hang
# faults. This peer records its protocol so a prompt or MCP relay is observable.
AGENT = """
import json, os, pathlib, signal, subprocess, sys, time
mode, trace = sys.argv[1:]
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(trace + ".pid").write_text(str(os.getpid()))
for line in sys.stdin:
    message = json.loads(line)
    with open(trace, "a") as log:
        log.write(json.dumps(message) + "\\n")
    method = message["method"]
    if mode in ("hang", "stubborn"):
        time.sleep(60)
    elif mode == "exit":
        sys.exit(1)
    print("probe-secret-canary", file=sys.stderr, flush=True)
    print("probe-secret-canary", flush=True)
    if method == "initialize":
        result = {
            "protocolVersion": 1,
            "agentCapabilities": {"sessionCapabilities": {"close": {}}},
            "_meta": {"omnigent": {"start_efforts": ["high"]}},
        }
    elif method == "session/new":
        if mode == "orphan":
            child = subprocess.Popen([sys.executable, "-c",
                "import os, pathlib, signal, sys, time; "
                "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)",
                trace + ".child.pid"], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            while not pathlib.Path(trace + ".child.pid").exists():
                time.sleep(0.01)
        if mode == "auth":
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32000, "message": "probe-secret-canary"}}), flush=True)
            continue
        result = {"sessionId": "probe-session"}
        if mode == "config":
            result["configOptions"] = [
                {"id": "effort", "category": "thought_level",
                 "options": [{"value": "deliberate"}]},
                {"id": "variant", "category": "model", "currentValue": "model-a",
                 "options": [{"value": "model-a", "name": "Model A"}, {"value": "model-b"}]},
                {"id": "access", "category": "mode",
                 "options": [{"value": "review", "name": "Review"}]},
            ]
        if mode == "legacy":
            result["models"] = {"currentModelId": "model-a",
                "availableModels": [{"modelId": "model-a"}, {"modelId": "model-b"}]}
            result["modes"] = {"availableModes": [{"id": "review", "name": "Review"}]}
    elif method == "session/close":
        result = {}
    else:
        raise RuntimeError("unexpected request")
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}), flush=True)
    if mode == "orphan" and method == "session/close":
        sys.exit(0)
"""


@pytest.mark.parametrize("mode", ["config", "legacy", "empty", "auth", "exit", "hang", "stubborn"])
async def test_probe_lifecycle_and_catalog(tmp_path, monkeypatch, caplog, mode):
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    script = tmp_path / "catalog_agent.py"
    script.write_text(AGENT)
    trace = tmp_path / "protocol.jsonl"
    entry = AcpAgentEntry(
        slug="neutral",
        name="Neutral",
        command=shlex.join([sys.executable, str(script), mode, str(trace)]),
    )
    start = time.monotonic()
    with caplog.at_level(logging.DEBUG):
        result = await probe_acp_model_options(entry, timeout_s=2)
    assert time.monotonic() - start < 3
    assert "probe-secret-canary" not in caplog.text + json.dumps(result)
    pid = int((tmp_path / "protocol.jsonl.pid").read_text())
    assert not psutil.pid_exists(pid)
    messages = [json.loads(line) for line in trace.read_text().splitlines()]
    expected = ["initialize"]
    if mode not in ("exit", "hang", "stubborn"):
        expected.append("session/new")
    if mode in ("config", "legacy", "empty"):
        expected.append("session/close")
    assert [message["method"] for message in messages] == expected
    if len(messages) >= 2:
        assert messages[1]["params"]["mcpServers"] == []
    if mode in ("auth", "exit", "hang", "stubborn"):
        assert result["error"]
    elif mode == "empty":
        assert result == {"models": [], "permission_options": [], "error": None}
    else:
        assert result["error"] is None
        assert [row["id"] for row in result["models"]] == ["model-a", "model-b"]
        assert result["models"][0]["isActive"] is True
        assert result["models"][0]["start_efforts"] == ["high"]
        assert result["permission_options"] == [{"value": "review", "label": "Review"}]
        if mode == "config":
            assert result["models"][0]["efforts"] == ["deliberate"]


async def test_probe_cleans_helper_after_agent_exits(tmp_path, monkeypatch):
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    script = tmp_path / "catalog_agent.py"
    script.write_text(AGENT)
    trace = tmp_path / "protocol.jsonl"
    entry = AcpAgentEntry(
        slug="neutral",
        name="Neutral",
        command=shlex.join([sys.executable, str(script), "orphan", str(trace)]),
    )
    child = None
    try:
        result = await probe_acp_model_options(entry, timeout_s=2)
        child = psutil.Process(int((tmp_path / "protocol.jsonl.child.pid").read_text()))
        deadline = time.monotonic() + 1
        while child.is_running() and child.status() != psutil.STATUS_ZOMBIE:
            if time.monotonic() >= deadline:
                pytest.fail("Model probe left the agent's helper running")
            await asyncio.sleep(0.01)
        assert result["error"] is None
    except psutil.NoSuchProcess:
        assert result["error"] is None
    finally:
        if child is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
