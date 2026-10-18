"""Fixtures confined to the Codex app-server tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_reaper_ps(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> None:
    """Fake scans for unit tests; real subprocess cleanup needs real ownership evidence."""
    if request.node.originalname in {
        "test_start_failure_snapshots_owned_process_before_cleanup",
        "test_start_failure_diagnostics_cannot_mask_error_or_cleanup",
        "test_readiness_failure_records_app_server_state_before_cleanup",
    }:
        return
    from omnigent.harnesses.codex_native import process_registry

    monkeypatch.setattr(process_registry, "_ps_output", lambda _columns: "")
