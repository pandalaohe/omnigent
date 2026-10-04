"""Fixtures confined to the Codex app-server tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_reaper_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any process scan in these app-server unit tests starts with an empty fake table."""
    from omnigent.harnesses.codex_native import process_registry

    monkeypatch.setattr(process_registry, "_ps_output", lambda _columns: "")
