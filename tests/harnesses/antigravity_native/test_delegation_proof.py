"""Executable-bound delegation evidence; all authority files use a throwaway home."""

from __future__ import annotations

import json

import pytest

from omnigent.harnesses.antigravity_native import bridge
from omnigent.harnesses.antigravity_native import delegation_proof as proof
from omnigent.permission_tiers.host import HostPermissionState, write_host_permission_state
from omnigent.util.atomic_json import atomic_write_json


@pytest.fixture
def attested(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    directory = tmp_path / "bridge-工作's space"
    hooks = bridge.agy_gemini_dir(directory) / "config" / "hooks.json"
    atomic_write_json(hooks, {"omnigent-guard": bridge._agy_guard_hook(directory)})
    executable = tmp_path / "agy"
    executable.write_bytes(b"tested engine")
    proof.prepare_launch(directory)
    fingerprint = proof._fingerprint(directory, [executable])
    atomic_write_json(
        directory / proof.FINGERPRINT_FILE,
        {
            **fingerprint,
            "live": True,
            "started_at": "2026-01-01T00:00:00+00:00",
        },
    )
    atomic_write_json(
        proof.proof_path(),
        {
            "version": 1,
            "fingerprint": fingerprint["fingerprint"],
            "failed": False,
            "date": "2026-01-01T00:00:00+00:00",
            "evidence": "approved proof run",
        },
    )
    return directory, executable


def test_fingerprint_is_session_independent_but_executable_bound(attested, tmp_path):
    directory, executable = attested
    assert proof.delegation_status(directory) == "proven"
    other = tmp_path / "other-bridge"
    atomic_write_json(
        bridge.agy_gemini_dir(other) / "config" / "hooks.json",
        {
            "omnigent-guard": bridge._agy_guard_hook(other),
        },
    )
    assert proof._fingerprint(directory, [executable]) == proof._fingerprint(other, [executable])
    executable.write_bytes(b"updated engine")
    assert proof.delegation_status(directory) == "missing"


@pytest.mark.parametrize("target", ["proof", "fingerprint"])
@pytest.mark.parametrize("damage", ["missing", "corrupt", "symlink"])
def test_absent_and_unreadable_evidence_have_distinct_outcomes(attested, tmp_path, target, damage):
    directory, _ = attested
    path = proof.proof_path() if target == "proof" else directory / proof.FINGERPRINT_FILE
    if damage == "missing":
        path.unlink()
    elif damage == "corrupt":
        path.write_text("[]")
    else:
        destination = tmp_path / "replacement.json"
        destination.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(destination)
    assert proof.delegation_status(directory) == ("missing" if damage == "missing" else "failed")


def test_known_failure_revokes_host_proof_and_cannot_be_reopened_by_missing_launch(attested):
    directory, _ = attested
    proof.mark_failed(directory, "Uncovered child write")
    assert proof.delegation_status(directory) == "failed"
    (directory / proof.DISABLED_FILE).unlink()
    (directory / proof.FINGERPRINT_FILE).unlink()
    assert proof.delegation_status(directory) == "failed"


def test_hook_receipts_bind_step_and_call_without_storing_arguments(attested):
    directory, _ = attested
    payload = {
        "conversationId": "child",
        "stepIdx": 4,
        "toolCall": {"name": "write_file", "args": {"Content": "private task text"}},
    }
    kwargs = {
        "session_id": "session",
        "host_id": "host",
        "revision": 3,
        "call_id": "agy-hook:example",
    }
    proof.record_hook(directory, payload, **kwargs)
    record = proof.hook_receipts(directory, "child")[0]
    assert record["step"] == 4 and record["name"] == "write_file"
    assert "private task text" not in json.dumps(record)
    proof.record_hook(directory, payload, **kwargs, finished=True)
    records = proof.hook_receipts(directory, "child")
    assert len(records) == 1 and records[0]["finished"] is True


@pytest.mark.parametrize("symlink_fence", [False, True])
def test_record_command_binds_a_ready_restricted_session(attested, monkeypatch, symlink_fence):
    from omnigent.harnesses.antigravity_native import rpc

    directory, executable = attested
    state = HostPermissionState("session", "host", "/opt/work/project", "ask", 3, "ready")
    write_host_permission_state(directory / "permission_state.json", state)
    bridge.write_bridge_state(directory, bridge.AntigravityNativeBridgeState("session", "cascade"))
    monkeypatch.setattr(rpc, "resolve_language_server_port", lambda _cid: 4242)
    monkeypatch.setattr(rpc, "running_agy_executables", lambda _port: (executable,))
    proof.mark_failed(directory, "old failure")
    if symlink_fence:
        original = proof.proof_path().read_bytes()
        marker = directory / proof.DISABLED_FILE
        marker.unlink()
        marker.symlink_to(directory / "absent-target")
        with pytest.raises(ValueError, match="symlink"):
            proof.main(
                ["record", "--bridge-dir", str(directory), "--evidence", "approved proof run"]
            )
        assert proof.proof_path().read_bytes() == original
        return
    assert (
        proof.main(["record", "--bridge-dir", str(directory), "--evidence", "approved proof run"])
        == 0
    )
    assert proof.delegation_status(directory) == "proven"
    assert not (directory / proof.DISABLED_FILE).exists()


def test_preparing_a_new_launch_invalidates_old_observations(attested):
    directory, _ = attested
    before = proof.proof_path().read_bytes()
    proof.prepare_launch(directory)
    assert proof.delegation_status(directory) == "missing"
    assert proof.proof_path().read_bytes() == before
