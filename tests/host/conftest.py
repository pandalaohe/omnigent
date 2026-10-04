"""Shared fixtures for host tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_host_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point a ``HostProcess`` built without ``config_path`` at an absent file.

    Its default is the developer's ``~/.omnigent/config.yaml``, whose
    configured commands (``host.pre_launch_command``) would otherwise run in
    every launch test.
    """
    monkeypatch.setattr("omnigent.host.connect.CONFIG_PATH", tmp_path / "no-config.yaml")
