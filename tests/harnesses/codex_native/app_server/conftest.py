"""Shared fixtures for the native Codex app server tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_ucode_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Point ucode state at an empty per-test path.

    A Databricks-profile launch reads ``~/.ucode/state.json`` for its gateway and
    auth, so without this the tests would depend on the machine running them.

    :param tmp_path: Per-test directory that holds no ucode state.
    :param monkeypatch: Fixture used to redirect the state path.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.ucode_state._STATE_PATH", tmp_path / "no-ucode-state.json"
    )


@pytest.fixture(autouse=True)
def _no_real_reaper_ps(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Fake scans for unit tests; real subprocess cleanup needs real ownership evidence."""
    if request.node.originalname in {
        "test_start_failure_snapshots_owned_process_before_cleanup",
        "test_start_failure_diagnostics_cannot_mask_error_or_cleanup",
        "test_readiness_failure_records_app_server_state_before_cleanup",
        "test_start_respawns_a_silent_app_server_on_the_same_transport",
    }:
        return
    from omnigent.harnesses.codex_native import process_registry

    monkeypatch.setattr(process_registry, "_ps_output", lambda _columns: "")
